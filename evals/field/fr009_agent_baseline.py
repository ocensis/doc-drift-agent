"""Competitive baseline: a general coding agent doing the same job.

drift-agent only earns its existence if it beats pointing a capable agent at
the repo and asking. This runs `claude -p` on the same frozen fixture, with the
same ground truth and the SAME scoring function as the tool (H.score), so the
two sides are directly comparable, and reports the two metrics that decide it —
wall clock and total tokens — alongside the recall/extras quality floor.

Two modes, and you usually need both (checklist §6):

  --mode constrained   (default) the agent wears drift-agent's own rules:
                        report only positive contradictions it can point at
                        current code for, ignore historical narration, read-only
                        tools. Answers "tool vs agent under the same handicap."

  --mode unconstrained the agent is free: full toolset, may reason across files
                        and about absence, no code-anchor requirement. Answers
                        "what can a capable agent do at all / is this a model
                        limit or a tool-architecture limit."

WARNING: the constrained mode imports the tool's own constraints onto the
baseline. Do NOT read a constrained-mode tie as "the tool matches a capable
agent" — that is comparing against a handicapped opponent (this was a real
error; see IT-0013). The unconstrained ceiling is the honest reference for
whether the task is solvable.

Usage:
    python evals/field/fr009_agent_baseline.py \
        --fixture evals/datasets/field/react-refactor-v1 \
        --ground-truth docs/field-reports/.../eval-ground-truth.json \
        --repeats 3 --mode unconstrained --dump /tmp/baseline.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _harness as H

_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["findings"],
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["doc", "line", "stale_statement", "why"],
                "properties": {
                    "doc": {"type": "string", "description": "repo-relative doc path"},
                    "line": {"type": "integer", "description": "1-indexed line of the claim"},
                    "stale_statement": {"type": "string"},
                    "why": {"type": "string"},
                },
            },
        }
    },
}

# Constrained: the tool's own discipline bolted onto the agent (code-anchor
# requirement + historical-narration exclusion + read-only).
_PROMPT_CONSTRAINED = """\
This repository's documentation may have fallen behind its code.

Between commit {baseline} and HEAD, the source was refactored. Find documentation
statements that the current code contradicts — descriptions of behavior,
architecture, module responsibilities, control flow, or operational guidance that
no longer match what the code does. The documentation is largely in Chinese;
judge the meaning, not the wording.

Report only positive contradictions you can point at specific current code for.
Do not report future plans, roadmap items, or passages that explicitly narrate a
previous state of the system (for example the background section of a decision
record). Do not report something merely because you could not find the code.

For each one give the documentation file path, the 1-indexed line of the stale
statement, the statement itself, and why the code contradicts it.
"""

# Unconstrained: investigate freely, reason across files and about absence.
_PROMPT_UNCONSTRAINED = """\
This repository was refactored between commit {baseline} and HEAD, and its
documentation may no longer match the code.

Find every documentation statement that is now inaccurate: descriptions of
behavior, architecture, module responsibilities, control flow, data shapes,
permissions, or operational guidance that the refactor has made wrong. The docs
are largely in Chinese; judge meaning, not wording.

Investigate freely. Read whatever files you need, follow the change across
files, reason about what a claim implies and whether the current code still
supports it. A statement is stale whether it is wrong because the code now does
something different, because a responsibility moved elsewhere, or because
something it describes no longer exists. Report diagrams and tables too.

For each one give the documentation file path, the 1-indexed line of the stale
statement, the statement itself, and why it is now inaccurate.
"""

_TOOLS_CONSTRAINED = [
    "--allowedTools", "Read", "Glob", "Grep",
    "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)", "Bash(git status:*)",
    "--disallowedTools", "Write", "Edit", "NotebookEdit", "WebFetch", "WebSearch",
    "--permission-mode", "dontAsk",
]
_TOOLS_UNCONSTRAINED = ["--permission-mode", "bypassPermissions"]


def _run_once(repo: Path, baseline: str, model: str, mode: str) -> dict[str, Any]:
    prompt = (_PROMPT_UNCONSTRAINED if mode == "unconstrained" else _PROMPT_CONSTRAINED)
    tools = _TOOLS_UNCONSTRAINED if mode == "unconstrained" else _TOOLS_CONSTRAINED
    command = [
        "claude", "-p", prompt.format(baseline=baseline),
        "--output-format", "json", "--model", model,
        "--json-schema", json.dumps(_RESPONSE_SCHEMA), *tools,
    ]
    started = time.monotonic()
    completed = subprocess.run(command, cwd=repo, capture_output=True, text=True)
    elapsed = time.monotonic() - started
    if completed.returncode != 0:
        return {"error": completed.stderr[-2000:], "wall_seconds": round(elapsed, 1)}
    try:
        payload = json.loads(completed.stdout)
    except ValueError:
        return {"error": f"unparseable stdout: {completed.stdout[:2000]}"}
    result = payload.get("result")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            result = {"findings": []}
    usage = payload.get("usage") or {}
    total = sum(
        int(usage.get(key, 0))
        for key in ("input_tokens", "cache_creation_input_tokens",
                    "cache_read_input_tokens", "output_tokens")
    )
    return {
        "wall_seconds": round(elapsed, 1),
        "num_turns": payload.get("num_turns"),
        "cost_usd": payload.get("total_cost_usd"),
        "tokens": total,
        "raw_findings": (result or {}).get("findings", []),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    H.add_target_arguments(parser)
    parser.add_argument("--ground-truth", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--model", default="claude-opus-4-8")
    parser.add_argument("--mode", choices=["constrained", "unconstrained"], default="constrained")
    parser.add_argument("--dump", type=Path, default=None)
    arguments = parser.parse_args()

    repo, baseline = H.resolve_target(arguments, parser)
    items, window, classes = H.load_ground_truth(arguments.ground_truth)
    spans = H.section_spans(repo, {str(item["doc"]) for item in items})

    runs: list[dict[str, Any]] = []
    hit_sets: list[set[str]] = []
    for index in range(arguments.repeats):
        # Fresh worktree per round: rounds must not observe each other, and a
        # stray write (unconstrained mode) would otherwise change later runs.
        if arguments.fixture:
            round_repo, _ = H.materialize_fixture(arguments.fixture)
        else:
            round_repo = repo
        record = _run_once(round_repo, baseline, arguments.model, arguments.mode)
        if "error" in record:
            print(f"round {index + 1}: FAILED {record['error'][:200]}", file=sys.stderr)
            runs.append({"run": index + 1, **record})
            continue
        findings = [
            {"doc": str(f.get("doc", "")), "line": int(f.get("line", 0) or 0)}
            for f in record.pop("raw_findings")
        ]
        hits, extras = H.score(findings, items, window, spans)
        hit_sets.append(hits)
        runs.append({
            "run": index + 1,
            "recall": f"{len(hits)}/{len(items)}",
            "recall_by_class": H.class_recall(hits, classes),
            "hits": sorted(hits),
            "extras": extras,
            "findings": findings,
            **record,
        })
        if arguments.dump is not None:
            arguments.dump.write_text(
                json.dumps(runs, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        print(
            f"round {index + 1}/{arguments.repeats} [{arguments.mode}]: {runs[-1]['recall']} "
            f"extras={extras} {record['wall_seconds']}s {record['tokens']} tokens",
            file=sys.stderr, flush=True,
        )

    union_hits = set().union(*hit_sets) if hit_sets else set()
    scored = [r for r in runs if "tokens" in r]
    summary: dict[str, Any] = {
        "mode": arguments.mode,
        "model": arguments.model,
        "union_recall": f"{len(union_hits)}/{len(items)}",
        "union_by_class": H.class_recall(union_hits, classes),
        "union_hits": sorted(union_hits),
        "runs": [{k: v for k, v in r.items() if k != "findings"} for r in runs],
    }
    if scored:
        n = len(scored)
        summary["mean_wall_seconds"] = round(sum(r["wall_seconds"] for r in scored) / n, 1)
        summary["mean_tokens"] = round(sum(r["tokens"] for r in scored) / n)
        summary["mean_extras"] = round(sum(r["extras"] for r in scored) / n, 1)
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()

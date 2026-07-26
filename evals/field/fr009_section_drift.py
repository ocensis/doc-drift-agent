"""FR-009 semantic section-drift recall/precision for the tool under test.

Runs the check pipeline with semantic analysis against the frozen fixture N
times and scores semantic_section_drift findings against the ground truth:
union recall (overall and per class) as the primary number, unconsumed
findings as extras. Model variance across rounds is expected, so union over
repeats is what matters — see union_over_repeats in _harness.

Before trusting any number this produces, read docs/evals/eval-validity-checklist.md.

Usage (frozen fixture — the stable, comparable mode):
    python evals/field/fr009_section_drift.py \
        --fixture path/to/fixture \
        --ground-truth docs/field-reports/.../eval-ground-truth.json \
        --repeats 5 --max-model-calls 56 --timeout-seconds 5400 \
        --dump-findings /tmp/findings.json

Usage (live repository — results shift with the target's worktree):
    python evals/field/fr009_section_drift.py \
        --repo /path/to/target --baseline <rev> --ground-truth ...
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _harness as H

from drift_agent import application
from drift_agent.domain.enums import RunMode
from drift_agent.domain.models import RunBudgets, RunRequest, ScopeSpec


def _findings_of(bundle: Any) -> list[dict[str, Any]]:
    return [
        {
            "doc": finding.doc_evidence.path,
            "line": finding.doc_evidence.line,
            "quote": (finding.old_value or {}).get("quote", "")[:200],
            "reason": finding.reason[:300],
            "code_path": finding.code_evidence.path,
        }
        for finding in bundle.findings
        if finding.kind == "semantic_section_drift"
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    H.add_target_arguments(parser)
    parser.add_argument("--ground-truth", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--max-model-calls", type=int, default=56)
    # The run-level wall clock, not the call budget, is what actually caps how
    # many sections get audited (checklist §1). The 120s default silently
    # truncates the queue and makes which sections get judged depend on API
    # latency, so eval runs must set this explicitly.
    parser.add_argument("--timeout-seconds", type=float, default=5_400.0)
    # Extras must be classifiable per FR-006, which needs the findings; a round
    # costs ~20 minutes, so never discard them silently.
    parser.add_argument("--dump-findings", type=Path, default=None)
    arguments = parser.parse_args()

    repo, baseline = H.resolve_target(arguments, parser)
    items, window, classes = H.load_ground_truth(arguments.ground_truth)
    spans = H.section_spans(repo, {str(item["doc"]) for item in items})

    runs: list[dict[str, Any]] = []
    hit_sets: list[set[str]] = []
    for index in range(arguments.repeats):
        bundle = application.run(
            RunRequest(
                mode=RunMode.CHECK,
                repo_path=repo,
                scope=ScopeSpec(kind="since", revision=baseline),
                semantic_analysis=True,
                budgets=RunBudgets(
                    max_model_calls_per_run=arguments.max_model_calls,
                    # Decouple the token budget from the call budget (checklist
                    # §1): a section audit costs ~11k input tokens, so a tight
                    # token cap would bind before max_model_calls does.
                    max_input_tokens_per_run=arguments.max_model_calls * 20_000,
                    timeout_seconds=arguments.timeout_seconds,
                ),
            )
        )
        findings = _findings_of(bundle)
        hits, extras = H.score(findings, items, window, spans)
        hit_sets.append(hits)
        runs.append(
            {
                "run": index + 1,
                # Measurement protocol: pin which model actually served the run
                # (strong profile falls back to OPENROUTER_MODEL when unset).
                "model": (
                    os.environ.get("OPENROUTER_STRONG_MODEL")
                    or os.environ.get("OPENROUTER_MODEL")
                    or ""
                ),
                "detector": os.environ.get("DRIFT_SEMANTIC_DETECTOR", "legacy_section"),
                "status": str(getattr(bundle.status, "value", bundle.status)),
                "model_calls": bundle.usage.model_calls,
                "cost_usd": round(bundle.usage.estimated_cost_usd, 4),
                "wall_seconds": round(bundle.usage.duration_ms / 1000, 1),
                "tokens": bundle.usage.input_tokens + bundle.usage.output_tokens,
                "recall": f"{len(hits)}/{len(items)}",
                "recall_by_class": H.class_recall(hits, classes),
                "hits": sorted(hits),
                "extras": extras,
                "findings": findings,
            }
        )
        # Flush after every round: a full-coverage round costs ~20 minutes, so a
        # crash or restart before the last one must not discard the earlier ones.
        if arguments.dump_findings is not None:
            arguments.dump_findings.write_text(
                json.dumps(runs, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        print(
            f"round {index + 1}/{arguments.repeats}: {runs[-1]['recall']} hits, "
            f"{extras} extras, {bundle.usage.model_calls} calls, "
            f"{runs[-1]['wall_seconds']}s, {runs[-1]['tokens']} tokens, "
            f"status={runs[-1]['status']}",
            file=sys.stderr,
            flush=True,
        )

    union_hits = set().union(*hit_sets) if hit_sets else set()
    scored = [run for run in runs if isinstance(run.get("tokens"), int)]
    summary = {
        "union_recall": f"{len(union_hits)}/{len(items)}",
        "union_by_class": H.class_recall(union_hits, classes),
        "union_hits": sorted(union_hits),
        "union_misses": sorted({str(item["label"]) for item in items} - union_hits),
        "union_at_3": H.union_over_repeats(hit_sets, 3),
        "runs": [{k: v for k, v in run.items() if k != "findings"} for run in runs],
    }
    if scored:
        summary["mean_extras"] = round(sum(int(r["extras"]) for r in scored) / len(scored), 1)
        summary["mean_tokens"] = round(sum(int(r["tokens"]) for r in scored) / len(scored))
        summary["mean_wall_seconds"] = round(
            sum(float(r["wall_seconds"]) for r in scored) / len(scored), 1
        )
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()

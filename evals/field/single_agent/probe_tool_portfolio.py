"""Local S0 capability probe for tool-portfolio handlers (no model or GT)."""

# The field harness is a sibling script rather than an installed package.
# ruff: noqa: E402

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

FIELD_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = FIELD_DIR.parent.parent
sys.path.insert(0, str(FIELD_DIR))
sys.path.insert(0, str(REPO_ROOT))

import _harness as H
from _portfolio_brief import PROFILE_SPECS, portfolio_runtime
from _portfolio_generic import paged_generic_runtime
from _portfolio_graph import codegraph_context_runtime, gitnexus_context_runtime
from _runner import AgentContext


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _summary(output: str) -> dict[str, Any]:
    first_line = output.partition("\n")[0]
    envelope: dict[str, Any] | None = None
    try:
        candidate = json.loads(first_line)
    except json.JSONDecodeError:
        candidate = None
    if isinstance(candidate, dict):
        envelope = {
            key: candidate[key]
            for key in ("kind", "page", "pages", "logical_items", "next_cursor")
            if key in candidate
        }
    decoded: object | None = None
    try:
        decoded = json.loads(output)
    except json.JSONDecodeError:
        pass
    graph_summary: dict[str, Any] | None = None
    if isinstance(decoded, dict) and isinstance(decoded.get("chunks"), list):
        chunks = decoded["chunks"]
        kind_counts: dict[str, int] = {}
        samples: list[dict[str, Any]] = []
        for raw_chunk in chunks:
            if not isinstance(raw_chunk, dict):
                continue
            kind = str(raw_chunk.get("kind", "unknown"))
            kind_counts[kind] = kind_counts.get(kind, 0) + 1
            if len(samples) < 8:
                samples.append(
                    {
                        key: (
                            value[:300] + "..."
                            if isinstance(value, str) and len(value) > 300
                            else value
                        )
                        for key, value in raw_chunk.items()
                    }
                )
        graph_summary = {
            "chunks_on_page": len(chunks),
            "kind_counts": kind_counts,
            "samples": samples,
        }
    return {
        "chars": len(output),
        "bytes": len(output.encode("utf-8")),
        "lines": len(output.splitlines()),
        "sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
        "envelope": envelope,
        "graph_summary": graph_summary,
    }


def _recover_graph_pages(
    *,
    first_output: str,
    handler: Any,
    arguments: dict[str, Any],
) -> tuple[dict[str, Any], float]:
    """Follow every graph cursor and verify lossless transport recovery."""

    started = time.monotonic()
    outputs = [first_output]
    pages: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    cursor: str | None = None
    expected_start = 0
    expected_total: int | None = None
    while True:
        decoded = json.loads(outputs[-1])
        if not isinstance(decoded, dict) or not isinstance(decoded.get("chunks"), list):
            raise RuntimeError("graph probe output is not a chunk envelope")
        page = decoded.get("page")
        if not isinstance(page, dict):
            raise RuntimeError("graph probe page metadata is missing")
        start = page.get("start")
        end = page.get("end")
        total = page.get("total")
        if (
            not isinstance(start, int)
            or not isinstance(end, int)
            or not isinstance(total, int)
            or start != expected_start
            or end - start != len(decoded["chunks"])
        ):
            raise RuntimeError("graph probe cursor pages are not contiguous")
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            raise RuntimeError("graph probe total changed across cursor pages")
        pages.append(page)
        records.extend(item for item in decoded["chunks"] if isinstance(item, dict))
        expected_start = end
        raw_cursor = decoded.get("next_cursor")
        cursor = raw_cursor if isinstance(raw_cursor, str) and raw_cursor else None
        if cursor is None:
            break
        outputs.append(handler({**arguments, "cursor": cursor}))
    if expected_total is None or expected_start != expected_total or len(records) != expected_total:
        raise RuntimeError("graph probe cursor did not recover every transport record")

    fragment_groups: dict[str, list[dict[str, Any]]] = {}
    plain_records = 0
    kind_counts: dict[str, int] = {}
    for record in records:
        kind = str(record.get("kind", "unknown"))
        kind_counts[kind] = kind_counts.get(kind, 0) + 1
        if record.get("transport") == "json_fragment":
            fragment_groups.setdefault(str(record.get("record_sha256")), []).append(record)
        else:
            plain_records += 1
    for digest, fragments in fragment_groups.items():
        ordered = sorted(fragments, key=lambda item: int(item["fragment_index"]))
        expected_count = int(ordered[0]["fragment_count"])
        if [int(item["fragment_index"]) for item in ordered] != list(range(expected_count)):
            raise RuntimeError("graph probe fragment sequence is incomplete")
        rendered = "".join(str(item["fragment"]) for item in ordered)
        if hashlib.sha256(rendered.encode()).hexdigest() != digest:
            raise RuntimeError("graph probe fragment digest does not reassemble")
        json.loads(rendered)
    recovery = {
        "pages": len(pages),
        "page_chars": [len(output) for output in outputs],
        "transport_records_recovered": len(records),
        "logical_records_recovered": plain_records + len(fragment_groups),
        "fragmented_logical_records": len(fragment_groups),
        "all_fragments_reassembled": True,
        "all_pages_chars": sum(len(output) for output in outputs),
        "records_sha256": hashlib.sha256(
            json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "kind_counts": kind_counts,
    }
    return recovery, time.monotonic() - started


def _probe_runtime(
    *,
    name: str,
    prepare: Any,
    context: AgentContext,
    tool: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    setup_started = time.monotonic()
    runtime = prepare(context)
    setup_seconds = time.monotonic() - setup_started
    handler_started = time.monotonic()
    handler = runtime.extra_tools[tool][1]
    output = handler(arguments)
    first_handler_seconds = time.monotonic() - handler_started
    recovery: dict[str, Any] | None = None
    recovery_seconds = 0.0
    if tool == "graph_context":
        recovery, recovery_seconds = _recover_graph_pages(
            first_output=output,
            handler=handler,
            arguments=arguments,
        )
    cleanup_seconds = 0.0
    if runtime.close is not None:
        cleanup_started = time.monotonic()
        runtime.close()
        cleanup_seconds = time.monotonic() - cleanup_started
    return {
        "profile": name,
        "tool": tool,
        "arguments": arguments,
        "output": _summary(output),
        "full_recovery": recovery,
        "timing": {
            "setup_seconds": round(setup_seconds, 6),
            "handler_seconds": round(first_handler_seconds, 6),
            "first_handler_seconds": round(first_handler_seconds, 6),
            "cursor_recovery_seconds": round(recovery_seconds, 6),
            "all_handler_seconds": round(first_handler_seconds + recovery_seconds, 6),
            "cleanup_seconds": round(cleanup_seconds, 6),
            "total_seconds": round(
                setup_seconds + first_handler_seconds + recovery_seconds + cleanup_seconds,
                6,
            ),
        },
        "metadata": runtime.metadata,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe local portfolio handlers without a model")
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--graph-target", action="append", default=[])
    parser.add_argument(
        "--profiles",
        nargs="+",
        choices=(
            "control",
            *PROFILE_SPECS,
            "codegraph_context",
            "gitnexus_context",
        ),
        default=(
            "control",
            *PROFILE_SPECS,
            "codegraph_context",
            "gitnexus_context",
        ),
    )
    arguments = parser.parse_args()
    repo_path, baseline_revision = H.materialize_fixture(arguments.fixture.resolve())
    context = AgentContext(
        repo_path=repo_path,
        baseline_revision=baseline_revision,
        head_revision=_git(repo_path, "rev-parse", "HEAD"),
    )
    targets = arguments.graph_target or ["runSpecialistLoop"]
    runs: list[dict[str, Any]] = []
    for profile_id in arguments.profiles:
        if profile_id == "control":
            runs.append(
                _probe_runtime(
                    name=profile_id,
                    prepare=paged_generic_runtime,
                    context=context,
                    tool="git_diff",
                    arguments={},
                )
            )
        elif profile_id in PROFILE_SPECS:
            profile = PROFILE_SPECS[profile_id]
            runs.append(
                _probe_runtime(
                    name=profile_id,
                    prepare=lambda inner, selected=profile: portfolio_runtime(inner, selected),
                    context=context,
                    tool="audit_brief",
                    arguments={},
                )
            )
        else:
            prepare = (
                codegraph_context_runtime
                if profile_id == "codegraph_context"
                else gitnexus_context_runtime
            )
            runs.append(
                _probe_runtime(
                    name=profile_id,
                    prepare=prepare,
                    context=context,
                    tool="graph_context",
                    arguments={
                        "targets": targets,
                        "question": "changed responsibilities, callers, and impact",
                        "include_source": True,
                        "max_chars": 12_000,
                    },
                )
            )
    artifact = {
        "stage": "S0-local-handler-probe",
        "model_called": False,
        "ground_truth_loaded": False,
        "fixture": str(arguments.fixture.resolve()),
        "baseline_revision": context.baseline_revision,
        "head_revision": context.head_revision,
        "head_tree": _git(repo_path, "rev-parse", "HEAD^{tree}"),
        "runs": runs,
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(json.dumps(artifact, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()

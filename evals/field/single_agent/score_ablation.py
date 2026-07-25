"""Offline scorer for raw single-agent ablation artifacts.

The generation scripts do not accept a ground-truth argument.  This separate
process is the only code in the experiment that opens benchmark labels.
"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FIELD_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = FIELD_DIR.parent.parent
sys.path.insert(0, str(FIELD_DIR))
sys.path.insert(0, str(REPO_ROOT))

import _harness as H
from _runner import BASE_TOOLS, PROTOCOL_VERSION, SPECIAL_TOOLS

EXPECTED_PAIR_KEYS = ("pair-1.1", "pair-2.1", "pair-3.1")
EXPECTED_AGENTS = ("seeded_tools_agent", "default_tools_agent")
UNBOUNDED_CONFIGURATION_FIELDS = (
    "agent_turn_limit",
    "model_call_limit",
    "input_token_limit",
    "run_timeout_seconds",
    "conversation_history_limit",
    "request_body_byte_limit",
    "read_file_line_limit",
    "tool_result_char_limit",
    "grep_match_limit",
    "grep_file_limit",
    "grep_file_byte_limit",
    "grep_line_char_limit",
    "list_entry_limit",
    "git_output_char_limit",
    "git_command_timeout_seconds",
    "read_briefing_char_limit",
    "extract_claim_char_limit",
    "worklist_char_limit",
    "list_findings_char_limit",
    "alignment_sites_per_doc_limit",
    "finding_string_length_limit",
    "finding_count_limit",
    "store_entry_limit",
)


@dataclass(frozen=True, slots=True)
class FrozenArtifact:
    """One stable raw-artifact snapshot captured before ground truth is opened."""

    path: Path
    raw: bytes
    sha256: str
    size_bytes: int
    mtime_ns: int
    device: int
    inode: int
    payload: dict[str, Any]


def _git_head(repo_path: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _git_worktree_is_clean(repo_path: Path) -> bool:
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return not status.strip()


def _target_repo(
    descriptor: dict[str, Any],
    cache: dict[str, tuple[Path, str]],
) -> tuple[Path, str]:
    key = json.dumps(descriptor, sort_keys=True)
    if key in cache:
        return cache[key]
    kind = str(descriptor.get("kind", ""))
    path = Path(str(descriptor.get("path", ""))).resolve()
    if kind == "fixture":
        resolved = H.materialize_fixture(path)
    elif kind == "repo":
        resolved = (path, "")
    else:
        raise ValueError(f"unknown target kind: {kind!r}")
    cache[key] = resolved
    return resolved


def _freeze_artifacts(artifact_paths: list[Path]) -> list[FrozenArtifact]:
    """Read exactly six stable, non-aliased raw artifacts without touching GT."""

    if len(artifact_paths) != 6:
        raise ValueError(f"expected exactly 6 raw artifacts, received {len(artifact_paths)}")

    frozen: list[FrozenArtifact] = []
    seen_paths: set[Path] = set()
    seen_files: set[tuple[int, int]] = set()
    seen_hashes: set[str] = set()
    for supplied_path in artifact_paths:
        path = supplied_path.resolve(strict=True)
        if path in seen_paths:
            raise ValueError(f"duplicate artifact path: {path}")
        before = path.stat()
        raw = path.read_bytes()
        after = path.stat()
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        if before_identity != after_identity or len(raw) != after.st_size:
            raise ValueError(f"artifact changed while it was being frozen: {path}")
        file_identity = (after.st_dev, after.st_ino)
        if file_identity in seen_files:
            raise ValueError(f"artifact paths alias the same file: {path}")
        digest = hashlib.sha256(raw).hexdigest()
        if digest in seen_hashes:
            raise ValueError(f"duplicate artifact content: {path}")
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(f"invalid raw artifact JSON: {path}") from error
        if not isinstance(payload, dict):
            raise ValueError(f"raw artifact root must be an object: {path}")

        seen_paths.add(path)
        seen_files.add(file_identity)
        seen_hashes.add(digest)
        frozen.append(
            FrozenArtifact(
                path=path,
                raw=raw,
                sha256=digest,
                size_bytes=after.st_size,
                mtime_ns=after.st_mtime_ns,
                device=after.st_dev,
                inode=after.st_ino,
                payload=payload,
            )
        )
    return frozen


def _validate_finding_channel(run: dict[str, Any], name: str) -> None:
    candidates = run.get(name)
    if not isinstance(candidates, list):
        raise ValueError(f"{run.get('pair_key')}/{run.get('agent')} {name} must be a list")
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            raise ValueError(
                f"{run.get('pair_key')}/{run.get('agent')} {name}[{index}] must be an object"
            )
        if not isinstance(candidate.get("doc"), str):
            raise ValueError(
                f"{run.get('pair_key')}/{run.get('agent')} {name}[{index}].doc is invalid"
            )
        line = candidate.get("line")
        if isinstance(line, bool) or not isinstance(line, int):
            raise ValueError(
                f"{run.get('pair_key')}/{run.get('agent')} {name}[{index}].line is invalid"
            )
        if not isinstance(candidate.get("quote"), str):
            raise ValueError(
                f"{run.get('pair_key')}/{run.get('agent')} {name}[{index}].quote is invalid"
            )


def _validate_conversation(run: dict[str, Any]) -> None:
    pair_key = str(run.get("pair_key", ""))
    agent = str(run.get("agent", ""))
    conversation = run.get("conversation")
    if not isinstance(conversation, dict):
        raise ValueError(f"{pair_key}/{agent} conversation is missing")
    failure = str(conversation.get("failure_reason") or "")
    if failure.startswith(("model:", "budget:")):
        raise ValueError(f"external generation failure in {pair_key}/{agent}: {failure}")
    if conversation.get("ok") is not True or failure:
        raise ValueError(
            f"run did not reach a valid terminal submit: {pair_key}/{agent}: {failure}"
        )

    actual_models = conversation.get("actual_models")
    if (
        not isinstance(actual_models, list)
        or len(actual_models) != 1
        or not isinstance(actual_models[0], str)
        or not actual_models[0]
    ):
        raise ValueError(f"{pair_key}/{agent} must record exactly one actual model")

    trace = conversation.get("turn_trace")
    turns = conversation.get("turns")
    if not isinstance(trace, list) or isinstance(turns, bool) or not isinstance(turns, int):
        raise ValueError(f"{pair_key}/{agent} has an invalid turn trace")
    if turns != len(trace) or turns < 1:
        raise ValueError(f"{pair_key}/{agent} turn count does not match its trace")
    traced_names: list[str] = []
    for expected_turn, entry in enumerate(trace, start=1):
        if not isinstance(entry, dict) or entry.get("turn") != expected_turn:
            raise ValueError(f"{pair_key}/{agent} has a non-contiguous turn trace")
        names = entry.get("tool_calls")
        if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
            raise ValueError(f"{pair_key}/{agent} has invalid trace tool calls")
        traced_names.extend(names)
    if traced_names.count("submit") != 1 or "submit" not in trace[-1]["tool_calls"]:
        raise ValueError(f"{pair_key}/{agent} must end at its only submit call")
    if "handoff" in traced_names:
        raise ValueError(f"{pair_key}/{agent} contains a forbidden handoff")

    tool_counts = conversation.get("tool_counts")
    if not isinstance(tool_counts, dict):
        raise ValueError(f"{pair_key}/{agent} tool counts are missing")
    counted: dict[str, int] = defaultdict(int)
    for name in traced_names:
        counted[name] += 1
    try:
        normalized_counts = {str(name): int(count) for name, count in tool_counts.items()}
    except (TypeError, ValueError) as error:
        raise ValueError(f"{pair_key}/{agent} tool counts are invalid") from error
    if dict(counted) != normalized_counts:
        raise ValueError(f"{pair_key}/{agent} tool counts do not match its trace")


def _timestamp_ns(container: dict[str, Any], field: str, location: str) -> int:
    value = container.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{location} {field} must be a positive integer timestamp")
    return value


def _preflight_artifacts(
    frozen_artifacts: list[FrozenArtifact],
) -> tuple[dict[str, tuple[Path, str]], dict[str, Any]]:
    """Fail closed on the complete batch before ground truth is first read."""

    expected_identities = {
        (pair_key, agent) for pair_key in EXPECTED_PAIR_KEYS for agent in EXPECTED_AGENTS
    }
    runs_by_identity: dict[tuple[str, str], dict[str, Any]] = {}
    frozen_by_identity: dict[tuple[str, str], FrozenArtifact] = {}
    target_cache: dict[str, tuple[Path, str]] = {}

    for frozen in frozen_artifacts:
        artifact = frozen.payload
        if artifact.get("protocol_version") != PROTOCOL_VERSION:
            raise ValueError(f"unsupported protocol in {frozen.path}")
        if artifact.get("langfuse_enabled") is not False:
            raise ValueError(f"observability state is not explicitly disabled: {frozen.path}")
        runs = artifact.get("runs")
        if not isinstance(runs, list) or len(runs) != 1 or not isinstance(runs[0], dict):
            raise ValueError(f"artifact must contain exactly one completed run: {frozen.path}")
        run = runs[0]
        agent = str(run.get("agent", ""))
        pair_key = str(run.get("pair_key", ""))
        identity = (pair_key, agent)
        if identity not in expected_identities:
            raise ValueError(f"unexpected pair/agent identity {identity!r} in {frozen.path}")
        if identity in runs_by_identity:
            raise ValueError(f"duplicate run for pair {pair_key!r}, agent {agent!r}")
        pair_id = pair_key.removesuffix(".1")
        if (
            artifact.get("agent") != agent
            or artifact.get("pair_id") != pair_id
            or run.get("run") != 1
            or run.get("protocol_version") != PROTOCOL_VERSION
        ):
            raise ValueError(f"artifact/run identity mismatch in {frozen.path}")

        for field in ("baseline_revision", "head_revision", "requested_model"):
            if artifact.get(field) != run.get(field):
                raise ValueError(f"artifact/run {field} mismatch in {frozen.path}")

        configuration = run.get("configuration")
        if not isinstance(configuration, dict):
            raise ValueError(f"configuration is missing in {frozen.path}")
        for limit_name in UNBOUNDED_CONFIGURATION_FIELDS:
            if limit_name not in configuration or configuration[limit_name] is not None:
                raise ValueError(f"experiment limit {limit_name} is active in {frozen.path}")
        if configuration.get("conversation_history_trimming") is not False:
            raise ValueError(f"conversation history trimming is active in {frozen.path}")
        if configuration.get("model") != {
            "profile": "strong",
            "reasoning_effort": "high",
            "temperature": 1.0,
        }:
            raise ValueError(f"unexpected model configuration in {frozen.path}")
        transport = configuration.get("transport")
        if not isinstance(transport, dict):
            raise ValueError(f"transport configuration is missing in {frozen.path}")

        tools = run.get("tools")
        if not isinstance(tools, dict):
            raise ValueError(f"tool metadata is missing in {frozen.path}")
        expected_menu = BASE_TOOLS + SPECIAL_TOOLS if agent == "seeded_tools_agent" else BASE_TOOLS
        if tuple(tools.get("names", [])) != expected_menu:
            raise ValueError(f"unexpected tool menu in {frozen.path}")
        if tuple(tools.get("base_names", [])) != BASE_TOOLS:
            raise ValueError(f"unexpected base-tool declaration in {frozen.path}")
        for hash_name in ("schema_sha256", "base_schema_sha256"):
            value = tools.get(hash_name)
            try:
                decoded_hash = bytes.fromhex(value) if isinstance(value, str) else b""
            except ValueError:
                decoded_hash = b""
            if len(decoded_hash) != 32:
                raise ValueError(f"invalid {hash_name} in {frozen.path}")

        _validate_conversation(run)
        allowed_names = set(expected_menu)
        traced_names = {
            str(name) for entry in run["conversation"]["turn_trace"] for name in entry["tool_calls"]
        }
        if not traced_names <= allowed_names:
            raise ValueError(f"trace contains a tool outside its menu in {frozen.path}")
        for channel_name in ("submission_only", "store", "delivered"):
            _validate_finding_channel(run, channel_name)
        raw_submit = run.get("raw_submit")
        if not isinstance(raw_submit, dict) or not isinstance(raw_submit.get("findings"), list):
            raise ValueError(f"valid terminal run lacks its raw submit in {frozen.path}")

        timing = run.get("timing")
        if not isinstance(timing, dict):
            raise ValueError(f"timing is missing in {frozen.path}")
        try:
            total_seconds = float(timing["total_seconds"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"total timing is invalid in {frozen.path}") from error
        if not math.isfinite(total_seconds) or total_seconds < 0:
            raise ValueError(f"total timing is invalid in {frozen.path}")

        artifact_started_at_ns = _timestamp_ns(
            artifact,
            "generation_started_at_ns",
            str(frozen.path),
        )
        artifact_completed_at_ns = _timestamp_ns(
            artifact,
            "generation_completed_at_ns",
            str(frozen.path),
        )
        run_started_at_ns = _timestamp_ns(run, "generation_started_at_ns", str(frozen.path))
        run_completed_at_ns = _timestamp_ns(
            run,
            "generation_completed_at_ns",
            str(frozen.path),
        )
        if artifact.get("completed_at_ns") != artifact_completed_at_ns:
            raise ValueError(f"artifact completion timestamps disagree in {frozen.path}")
        if run.get("completed_at_ns") != run_completed_at_ns:
            raise ValueError(f"run completion timestamps disagree in {frozen.path}")
        if not (
            artifact_started_at_ns
            <= run_started_at_ns
            <= run_completed_at_ns
            == artifact_completed_at_ns
            <= frozen.mtime_ns
        ):
            raise ValueError(f"generation timestamps are inconsistent in {frozen.path}")

        repo_path, fixture_baseline = _target_repo(artifact.get("target", {}), target_cache)
        if fixture_baseline and fixture_baseline != artifact.get("baseline_revision"):
            raise ValueError(f"fixture baseline mismatch in {frozen.path}")
        if _git_head(repo_path) != artifact.get("head_revision"):
            raise ValueError(f"target HEAD mismatch in {frozen.path}")
        if not _git_worktree_is_clean(repo_path):
            raise ValueError(f"target worktree is not frozen/clean for {frozen.path}")

        runs_by_identity[identity] = run
        frozen_by_identity[identity] = frozen

    if set(runs_by_identity) != expected_identities:
        missing = sorted(expected_identities - set(runs_by_identity))
        unexpected = sorted(set(runs_by_identity) - expected_identities)
        raise ValueError(f"incomplete three-pair batch; missing={missing}, unexpected={unexpected}")

    first_artifact = frozen_artifacts[0].payload
    first_run = first_artifact["runs"][0]
    for frozen in frozen_artifacts[1:]:
        artifact = frozen.payload
        run = artifact["runs"][0]
        for field in ("target", "baseline_revision", "head_revision", "requested_model"):
            if artifact.get(field) != first_artifact.get(field):
                raise ValueError(f"batch-wide artifact {field} mismatch in {frozen.path}")
        for field in (
            "baseline_revision",
            "head_revision",
            "prompt",
            "configuration",
            "requested_model",
        ):
            if run.get(field) != first_run.get(field):
                raise ValueError(f"batch-wide run {field} mismatch in {frozen.path}")
        if run["conversation"]["actual_models"] != first_run["conversation"]["actual_models"]:
            raise ValueError(f"batch-wide actual-model mismatch in {frozen.path}")

    base_schema_hashes = {run["tools"]["base_schema_sha256"] for run in runs_by_identity.values()}
    if len(base_schema_hashes) != 1:
        raise ValueError("base tool schemas differ across the six-run batch")
    for agent in EXPECTED_AGENTS:
        schema_hashes = {
            runs_by_identity[(pair_key, agent)]["tools"]["schema_sha256"]
            for pair_key in EXPECTED_PAIR_KEYS
        }
        if len(schema_hashes) != 1:
            raise ValueError(f"{agent} tool schema differs across pairs")
    for pair_key in EXPECTED_PAIR_KEYS:
        reasons = _pair_mismatch(
            runs_by_identity[(pair_key, "seeded_tools_agent")],
            runs_by_identity[(pair_key, "default_tools_agent")],
        )
        if reasons:
            raise ValueError(f"pair {pair_key} failed A=B+SPECIAL audit: {', '.join(reasons)}")

    artifact_manifest: list[dict[str, Any]] = []
    recorded_starts: list[int] = []
    recorded_completions: list[int] = []
    filesystem_derived_starts: list[int] = []
    filesystem_completions: list[int] = []
    for identity in sorted(expected_identities):
        run = runs_by_identity[identity]
        frozen = frozen_by_identity[identity]
        total_seconds = float(run["timing"]["total_seconds"])
        recorded_start_ns = int(frozen.payload["generation_started_at_ns"])
        recorded_completion_ns = int(frozen.payload["generation_completed_at_ns"])
        filesystem_derived_start_ns = frozen.mtime_ns - round(total_seconds * 1_000_000_000)
        recorded_starts.append(recorded_start_ns)
        recorded_completions.append(recorded_completion_ns)
        filesystem_derived_starts.append(filesystem_derived_start_ns)
        filesystem_completions.append(frozen.mtime_ns)
        artifact_manifest.append(
            {
                "path": str(frozen.path),
                "sha256": frozen.sha256,
                "size_bytes": frozen.size_bytes,
                "mtime_ns": frozen.mtime_ns,
                "pair_key": identity[0],
                "agent": identity[1],
                "total_seconds": total_seconds,
                "generation_started_at_ns": recorded_start_ns,
                "generation_completed_at_ns": recorded_completion_ns,
                "run_generation_started_at_ns": run["generation_started_at_ns"],
                "run_generation_completed_at_ns": run["generation_completed_at_ns"],
                "filesystem_derived_start_ns": filesystem_derived_start_ns,
            }
        )

    batch_timing = {
        "source": "runner-recorded-wall-clock",
        "method": (
            "launch spread and makespan use each one-run artifact's explicit "
            "generation_started_at_ns/generation_completed_at_ns"
        ),
        "earliest_generation_start_ns": min(recorded_starts),
        "latest_generation_start_ns": max(recorded_starts),
        "earliest_generation_completion_ns": min(recorded_completions),
        "latest_generation_completion_ns": max(recorded_completions),
        "launch_spread_seconds": round(
            (max(recorded_starts) - min(recorded_starts)) / 1_000_000_000,
            6,
        ),
        "batch_makespan_seconds": round(
            (max(recorded_completions) - min(recorded_starts)) / 1_000_000_000,
            6,
        ),
    }
    timing_audit = {
        "source": "filesystem-derived",
        "method": (
            "completion=stable raw artifact st_mtime_ns; "
            "start=completion-total_seconds from the frozen run"
        ),
        "precision_note": (
            "Approximate: total_seconds is rounded to milliseconds and artifact mtime includes "
            "final serialization/write latency."
        ),
        "earliest_derived_start_ns": min(filesystem_derived_starts),
        "latest_derived_start_ns": max(filesystem_derived_starts),
        "earliest_completion_mtime_ns": min(filesystem_completions),
        "latest_completion_mtime_ns": max(filesystem_completions),
        "launch_spread_seconds": round(
            (max(filesystem_derived_starts) - min(filesystem_derived_starts)) / 1_000_000_000,
            6,
        ),
        "batch_makespan_seconds": round(
            (max(filesystem_completions) - min(filesystem_derived_starts)) / 1_000_000_000,
            6,
        ),
    }
    audit = {
        "passed_before_ground_truth": True,
        "artifact_count": len(frozen_artifacts),
        "pair_keys": list(EXPECTED_PAIR_KEYS),
        "artifact_manifest": artifact_manifest,
        "batch_timing": batch_timing,
        "filesystem_timing": timing_audit,
    }
    return target_cache, audit


def _decode_ground_truth(raw: bytes) -> tuple[list[dict[str, Any]], int, dict[str, str]]:
    payload = json.loads(raw)
    items = payload["items"]
    window = int(payload.get("window_lines", 40))
    classes = {str(item["label"]): str(item.get("class", "prose")) for item in items}
    return items, window, classes


def _evidence_valid(
    repo_path: Path, findings: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate evidence after the conversation without repairing or re-anchoring it."""

    valid: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    root = repo_path.resolve()
    for index, finding in enumerate(findings):
        doc = str(finding.get("doc", ""))
        quote = str(finding.get("quote", ""))
        reason = ""
        try:
            line = int(finding.get("line", 0))
        except (TypeError, ValueError):
            line = 0
        target = (root / doc).resolve()
        if not doc or doc.startswith(("/", "~")) or target == root or root not in target.parents:
            reason = "doc_out_of_scope"
        elif not target.is_file():
            reason = "doc_missing"
        else:
            text = target.read_text(encoding="utf-8", errors="replace")
            total_lines = max(1, len(text.splitlines()))
            if not quote or quote not in text:
                reason = "quote_not_verbatim"
            elif line < 1 or line > total_lines:
                reason = "line_out_of_range"
            else:
                occurrence_lines: list[int] = []
                start = 0
                while True:
                    offset = text.find(quote, start)
                    if offset < 0:
                        break
                    occurrence_lines.append(1 + text.count("\n", 0, offset))
                    start = offset + 1
                if line not in occurrence_lines:
                    reason = "quote_line_mismatch"
                elif not str(finding.get("code_evidence", "")).strip():
                    reason = "code_evidence_missing"
        if reason:
            invalid.append({"index": index, "doc": doc, "line": line, "reason": reason})
        else:
            valid.append(dict(finding))
    return valid, invalid


def _score_channel(
    findings: list[dict[str, Any]],
    *,
    items: list[dict[str, Any]],
    window: int,
    classes: dict[str, str],
    spans: dict[str, list[tuple[int, int]]],
) -> dict[str, Any]:
    hits, extras = H.score(findings, items, window, spans)
    return {
        "recall": f"{len(hits)}/{len(items)}",
        "hits": sorted(hits),
        "recall_by_class": H.class_recall(hits, classes),
        "extras": extras,
        "findings": len(findings),
    }


def _score_run(
    run: dict[str, Any],
    *,
    repo_path: Path,
    items: list[dict[str, Any]],
    window: int,
    classes: dict[str, str],
    spans: dict[str, list[tuple[int, int]]],
) -> dict[str, Any]:
    channels: dict[str, Any] = {}
    raw_submit = run.get("raw_submit")
    raw_submit_findings = raw_submit.get("findings", []) if isinstance(raw_submit, dict) else []
    channel_inputs = {
        "raw_submit_diagnostic": raw_submit_findings,
        "submission_only": run.get("submission_only", []),
        "store": run.get("store", []),
        "delivered": run.get("delivered", []),
    }
    for name, candidates in channel_inputs.items():
        raw = [dict(item) for item in candidates if isinstance(item, dict)]
        evidence_valid, invalid = _evidence_valid(repo_path, raw)
        channels[name] = {
            "raw": _score_channel(
                raw,
                items=items,
                window=window,
                classes=classes,
                spans=spans,
            ),
            "evidence_valid": _score_channel(
                evidence_valid,
                items=items,
                window=window,
                classes=classes,
                spans=spans,
            ),
            "invalid_evidence": invalid,
        }
    failure = str(run.get("conversation", {}).get("failure_reason") or "")
    external_failure = failure.startswith(("model:", "budget:"))
    special_tool_sequence = [
        name
        for trace_entry in run.get("conversation", {}).get("turn_trace", [])
        for name in trace_entry.get("tool_calls", [])
        if name in SPECIAL_TOOLS
    ]
    return {
        "agent": run.get("agent"),
        "pair_key": run.get("pair_key"),
        "conversation_ok": bool(run.get("conversation", {}).get("ok")),
        "failure_reason": failure or None,
        "external_failure": external_failure,
        "channels": channels,
        "usage": run.get("usage", {}),
        "timing": run.get("timing", {}),
        "tool_counts": run.get("conversation", {}).get("tool_counts", {}),
        "special_tool_sequence": special_tool_sequence,
    }


def _pair_mismatch(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    mismatches: list[str] = []
    exact_fields = (
        "protocol_version",
        "baseline_revision",
        "head_revision",
        "prompt",
        "configuration",
        "requested_model",
    )
    for field in exact_fields:
        if a.get(field) != b.get(field):
            mismatches.append(field)
    if a.get("tools", {}).get("base_schema_sha256") != b.get("tools", {}).get("base_schema_sha256"):
        mismatches.append("base_tool_schema")
    a_models = a.get("conversation", {}).get("actual_models", [])
    b_models = b.get("conversation", {}).get("actual_models", [])
    if len(a_models) != 1 or a_models != b_models:
        mismatches.append("actual_model")
    a_failure = str(a.get("conversation", {}).get("failure_reason") or "")
    b_failure = str(b.get("conversation", {}).get("failure_reason") or "")
    if a_failure.startswith(("model:", "budget:")):
        mismatches.append("seeded_external_failure")
    if b_failure.startswith(("model:", "budget:")):
        mismatches.append("default_external_failure")
    if tuple(b.get("tools", {}).get("names", [])) != BASE_TOOLS:
        mismatches.append("default_tool_menu")
    if tuple(a.get("tools", {}).get("names", [])) != BASE_TOOLS + SPECIAL_TOOLS:
        mismatches.append("seeded_tool_menu")
    return mismatches


def _distribution(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"mean": None, "stdev": None, "min": None, "max": None}
    return {
        "mean": round(statistics.mean(values), 4),
        "stdev": round(statistics.pstdev(values), 4),
        "min": round(min(values), 4),
        "max": round(max(values), 4),
    }


def _arm_summary(
    agent: str,
    valid_pairs: list[dict[str, Any]],
    score_lookup: dict[tuple[Any, Any], dict[str, Any]],
) -> dict[str, Any]:
    rows = [score_lookup[(pair["pair_key"], agent)] for pair in valid_pairs]
    primaries = [row["channels"]["delivered"]["evidence_valid"] for row in rows]
    hit_sets = [set(primary["hits"]) for primary in primaries]
    hits = [float(len(primary["hits"])) for primary in primaries]
    extras = [float(primary["extras"]) for primary in primaries]
    calls = [float(row["usage"].get("model_calls", 0)) for row in rows]
    tool_call_totals = [
        float(row["usage"].get("tool_calls", sum(row["tool_counts"].values()))) for row in rows
    ]
    input_tokens = [float(row["usage"].get("input_tokens", 0)) for row in rows]
    output_tokens = [float(row["usage"].get("output_tokens", 0)) for row in rows]
    tokens = [
        input_count + output_count
        for input_count, output_count in zip(input_tokens, output_tokens, strict=True)
    ]
    costs = [float(row["usage"].get("estimated_cost_usd", 0.0)) for row in rows]
    walls = [float(row["timing"].get("total_seconds", 0.0)) for row in rows]
    costs_per_hit = [cost / hit for cost, hit in zip(costs, hits, strict=True) if hit > 0]
    tool_counts: dict[str, int] = defaultdict(int)
    for row in rows:
        for name, count in row["tool_counts"].items():
            tool_counts[str(name)] += int(count)
    union = set().union(*hit_sets) if hit_sets else set()
    return {
        "runs": len(rows),
        "completed_submissions": sum(bool(row["conversation_ok"]) for row in rows),
        "hits": _distribution(hits),
        "extras": _distribution(extras),
        "model_calls": _distribution(calls),
        "tool_call_count": _distribution(tool_call_totals),
        "input_tokens": _distribution(input_tokens),
        "output_tokens": _distribution(output_tokens),
        "actual_tokens": _distribution(tokens),
        "cost_usd": _distribution(costs),
        "wall_seconds": _distribution(walls),
        "cost_per_valid_hit": _distribution(costs_per_hit),
        "union_hits": sorted(union),
        "union_at_3": H.union_over_repeats(hit_sets, 3),
        "tool_calls": dict(sorted(tool_counts.items())),
        "invalid_evidence": sum(
            len(row["channels"]["delivered"]["invalid_evidence"]) for row in rows
        ),
    }


def score_artifacts(artifact_paths: list[Path], ground_truth: Path) -> dict[str, Any]:
    frozen_artifacts = _freeze_artifacts(artifact_paths)
    target_cache, protocol_audit = _preflight_artifacts(frozen_artifacts)

    gt_read_started_at_ns = time.time_ns()
    latest_artifact_mtime_ns = max(item.mtime_ns for item in frozen_artifacts)
    latest_recorded_completion_ns = max(
        int(item.payload["generation_completed_at_ns"]) for item in frozen_artifacts
    )
    if max(latest_artifact_mtime_ns, latest_recorded_completion_ns) >= gt_read_started_at_ns:
        raise ValueError(
            "raw artifact completion time is not earlier than the proposed ground-truth read"
        )
    raw_gt = ground_truth.read_bytes()
    items, window, classes = _decode_ground_truth(raw_gt)
    gt_docs = {str(item["doc"]) for item in items}
    rows: list[dict[str, Any]] = []
    raw_runs_by_pair: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)

    for frozen in frozen_artifacts:
        artifact_path = frozen.path
        artifact = frozen.payload
        repo_path, _fixture_baseline = _target_repo(artifact["target"], target_cache)
        spans = H.section_spans(repo_path, gt_docs)
        for raw_run in artifact.get("runs", []):
            scored = _score_run(
                raw_run,
                repo_path=repo_path,
                items=items,
                window=window,
                classes=classes,
                spans=spans,
            )
            scored["artifact"] = str(artifact_path.resolve())
            rows.append(scored)
            pair_key = str(raw_run.get("pair_key", ""))
            agent_name = str(raw_run.get("agent", ""))
            if agent_name in raw_runs_by_pair[pair_key]:
                raise ValueError(f"duplicate run for pair {pair_key!r}, agent {agent_name!r}")
            raw_runs_by_pair[pair_key][agent_name] = raw_run

    score_lookup = {(row["pair_key"], row["agent"]): row for row in rows}
    pairs: list[dict[str, Any]] = []
    for pair_key, arms in sorted(raw_runs_by_pair.items()):
        seeded_run = arms.get("seeded_tools_agent")
        default_run = arms.get("default_tools_agent")
        if seeded_run is None or default_run is None:
            pairs.append({"pair_key": pair_key, "valid": False, "reasons": ["missing_arm"]})
            continue
        reasons = _pair_mismatch(seeded_run, default_run)
        seeded_score = score_lookup[(pair_key, "seeded_tools_agent")]
        default_score = score_lookup[(pair_key, "default_tools_agent")]
        seeded_primary = seeded_score["channels"]["delivered"]["evidence_valid"]
        default_primary = default_score["channels"]["delivered"]["evidence_valid"]
        pairs.append(
            {
                "pair_key": pair_key,
                "valid": not reasons,
                "reasons": reasons,
                "seeded_recall": seeded_primary["recall"],
                "default_recall": default_primary["recall"],
                "delta_hits": len(seeded_primary["hits"]) - len(default_primary["hits"]),
                "delta_extras": seeded_primary["extras"] - default_primary["extras"],
            }
        )

    valid_pairs = [pair for pair in pairs if pair["valid"]]
    deltas = [int(pair["delta_hits"]) for pair in valid_pairs]
    extra_deltas = [int(pair["delta_extras"]) for pair in valid_pairs]
    seeded_hit_sets = [
        set(
            score_lookup[(pair["pair_key"], "seeded_tools_agent")]["channels"]["delivered"][
                "evidence_valid"
            ]["hits"]
        )
        for pair in valid_pairs
    ]
    default_hit_sets = [
        set(
            score_lookup[(pair["pair_key"], "default_tools_agent")]["channels"]["delivered"][
                "evidence_valid"
            ]["hits"]
        )
        for pair in valid_pairs
    ]
    seeded_union = set().union(*seeded_hit_sets) if seeded_hit_sets else set()
    default_union = set().union(*default_hit_sets) if default_hit_sets else set()
    positive_signal = (
        len(valid_pairs) >= 3
        and sum(delta > 0 for delta in deltas) >= 2
        and statistics.mean(deltas) >= 1
        and len(seeded_union) >= len(default_union)
        and statistics.mean(extra_deltas) <= 1
    )
    arm_summaries = {
        agent: _arm_summary(agent, valid_pairs, score_lookup)
        for agent in ("seeded_tools_agent", "default_tools_agent")
    }

    return {
        "protocol_version": PROTOCOL_VERSION,
        "protocol_audit": {
            **protocol_audit,
            "gt_read_started_at_ns": gt_read_started_at_ns,
            "all_artifacts_completed_before_gt_read": all(
                item.mtime_ns < gt_read_started_at_ns
                and int(item.payload["generation_completed_at_ns"]) < gt_read_started_at_ns
                for item in frozen_artifacts
            ),
        },
        "ground_truth": {
            "path": str(ground_truth.resolve()),
            "sha256": hashlib.sha256(raw_gt).hexdigest(),
            "items": len(items),
        },
        "primary_metric": "evidence-valid delivered recall",
        "runs": rows,
        "pairs": pairs,
        "summary": {
            "valid_pairs": len(valid_pairs),
            "seeded_wins": sum(delta > 0 for delta in deltas),
            "ties": sum(delta == 0 for delta in deltas),
            "default_wins": sum(delta < 0 for delta in deltas),
            "paired_mean_delta_hits": round(statistics.mean(deltas), 3) if deltas else None,
            "paired_median_delta_hits": statistics.median(deltas) if deltas else None,
            "seeded_union_hits": sorted(seeded_union),
            "default_union_hits": sorted(default_union),
            "seeded_union_at_3": H.union_over_repeats(seeded_hit_sets, 3),
            "default_union_at_3": H.union_over_repeats(default_hit_sets, 3),
            "preregistered_positive_signal": positive_signal,
            "batch_timing": protocol_audit["batch_timing"],
            "arms": arm_summaries,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Score completed raw single-Agent artifacts")
    parser.add_argument("--artifact", type=Path, action="append", required=True)
    parser.add_argument("--ground-truth", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=None)
    arguments = parser.parse_args()

    report = score_artifacts(arguments.artifact, arguments.ground_truth)
    rendered = json.dumps(report, ensure_ascii=False, indent=1)
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(rendered, encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()

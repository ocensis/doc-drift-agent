"""Offline scorer for the three-arm code-graph retrieval ablation.

Generation has no ground-truth access.  This process first freezes and
preflights the complete default/CodeGraph/GitNexus batch, then opens the
ground-truth file exactly once.  Agent-caused terminal failures are scored as
zero delivered findings; model/provider failures invalidate the batch.
"""

# The field harness is a sibling script rather than an installed package.
# ruff: noqa: E402

from __future__ import annotations

import argparse
import copy
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
from typing import Any, cast

FIELD_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = FIELD_DIR.parent.parent
sys.path.insert(0, str(FIELD_DIR))
sys.path.insert(0, str(REPO_ROOT))

import _harness as H
from _graph_runtime import (
    CODEGRAPH_AGENT,
    CODEGRAPH_TOOLS,
    CODEGRAPH_VERSION,
    EXPECTED_TOOL_MENUS,
    GITNEXUS_AGENT,
    GITNEXUS_TOOLS,
    GITNEXUS_VERSION,
    GRAPH_DEFAULT_AGENT,
    GRAPH_PROTOCOL_VERSION,
)
from _runner import (
    BASE_TOOLS,
    EvalSubmission,
)
from _runner import (
    PROTOCOL_VERSION as LEGACY_CONTROL_PROTOCOL_VERSION,
)

EXPECTED_PAIR_KEYS = ("pair-1.1", "pair-2.1", "pair-3.1")
EXPECTED_AGENTS = (GRAPH_DEFAULT_AGENT, CODEGRAPH_AGENT, GITNEXUS_AGENT)
EXPECTED_ARTIFACT_COUNT = len(EXPECTED_PAIR_KEYS) * len(EXPECTED_AGENTS)
LEGACY_CONTROL_AGENT = "default_tools_agent"
AGENT_FAILURE_REASONS = frozenset({"submit_schema_invalid", "no_tool_call"})
EXTERNAL_FAILURE_PREFIXES = ("model:", "budget:")
TREATMENT_TOOLS = {
    GRAPH_DEFAULT_AGENT: (),
    CODEGRAPH_AGENT: CODEGRAPH_TOOLS,
    GITNEXUS_AGENT: GITNEXUS_TOOLS,
}
COMPARISONS = {
    "codegraph_vs_default": (CODEGRAPH_AGENT, GRAPH_DEFAULT_AGENT),
    "gitnexus_vs_default": (GITNEXUS_AGENT, GRAPH_DEFAULT_AGENT),
}
EXPECTED_PACKAGE_VERSIONS = {
    CODEGRAPH_AGENT: CODEGRAPH_VERSION,
    GITNEXUS_AGENT: GITNEXUS_VERSION,
}
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
    """One immutable raw-artifact snapshot captured before GT is opened."""

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


def _git_tree(repo_path: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"],
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
        resolved = cast(tuple[Path, str], H.materialize_fixture(path))
    elif kind == "repo":
        resolved = (path, "")
    else:
        raise ValueError(f"unknown target kind: {kind!r}")
    cache[key] = resolved
    return resolved


def _freeze_artifacts(artifact_paths: list[Path]) -> list[FrozenArtifact]:
    """Freeze exactly nine stable, distinct artifacts without touching GT."""

    if len(artifact_paths) != EXPECTED_ARTIFACT_COUNT:
        raise ValueError(
            f"expected exactly {EXPECTED_ARTIFACT_COUNT} raw artifacts, "
            f"received {len(artifact_paths)}"
        )

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


def _timestamp_ns(container: dict[str, Any], field: str, location: str) -> int:
    value = container.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{location} {field} must be a positive integer timestamp")
    return value


def _nonnegative_seconds(value: object, *, field: str, location: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{location} {field} is invalid")
    try:
        seconds = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{location} {field} is invalid") from error
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError(f"{location} {field} is invalid")
    return seconds


def _validate_finding_channel(run: dict[str, Any], name: str) -> None:
    candidates = run.get(name)
    location = f"{run.get('pair_key')}/{run.get('agent')} {name}"
    if not isinstance(candidates, list):
        raise ValueError(f"{location} must be a list")
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            raise ValueError(f"{location}[{index}] must be an object")
        if not isinstance(candidate.get("doc"), str):
            raise ValueError(f"{location}[{index}].doc is invalid")
        line = candidate.get("line")
        if isinstance(line, bool) or not isinstance(line, int):
            raise ValueError(f"{location}[{index}].line is invalid")
        if not isinstance(candidate.get("quote"), str):
            raise ValueError(f"{location}[{index}].quote is invalid")


def _validate_conversation(run: dict[str, Any]) -> None:
    """Validate success or an attributable Agent failure, rejecting infra failures."""

    pair_key = str(run.get("pair_key", ""))
    agent = str(run.get("agent", ""))
    location = f"{pair_key}/{agent}"
    conversation = run.get("conversation")
    if not isinstance(conversation, dict):
        raise ValueError(f"{location} conversation is missing")
    failure = str(conversation.get("failure_reason") or "")
    if failure.startswith(EXTERNAL_FAILURE_PREFIXES):
        raise ValueError(f"external generation failure in {location}: {failure}")
    ok = conversation.get("ok") is True
    if ok and failure:
        raise ValueError(f"successful run records a failure in {location}: {failure}")
    if not ok and failure not in AGENT_FAILURE_REASONS:
        raise ValueError(f"unsupported terminal failure in {location}: {failure or 'missing'}")

    actual_models = conversation.get("actual_models")
    if (
        not isinstance(actual_models, list)
        or len(actual_models) != 1
        or not isinstance(actual_models[0], str)
        or not actual_models[0]
    ):
        raise ValueError(f"{location} must record exactly one actual model")

    trace = conversation.get("turn_trace")
    turns = conversation.get("turns")
    if not isinstance(trace, list) or isinstance(turns, bool) or not isinstance(turns, int):
        raise ValueError(f"{location} has an invalid turn trace")
    if turns != len(trace) or turns < 1:
        raise ValueError(f"{location} turn count does not match its trace")
    traced_names: list[str] = []
    for expected_turn, entry in enumerate(trace, start=1):
        if not isinstance(entry, dict) or entry.get("turn") != expected_turn:
            raise ValueError(f"{location} has a non-contiguous turn trace")
        names = entry.get("tool_calls")
        if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
            raise ValueError(f"{location} has invalid trace tool calls")
        traced_names.extend(names)
    if "handoff" in traced_names:
        raise ValueError(f"{location} contains a forbidden handoff")

    submit_count = traced_names.count("submit")
    if ok or failure == "submit_schema_invalid":
        if submit_count != 1 or "submit" not in trace[-1]["tool_calls"]:
            raise ValueError(f"{location} must end at its only submit call")
    elif failure == "no_tool_call":
        if submit_count or trace[-1]["tool_calls"]:
            raise ValueError(f"{location} no_tool_call failure has an invalid final turn")

    tool_counts = conversation.get("tool_counts")
    if not isinstance(tool_counts, dict):
        raise ValueError(f"{location} tool counts are missing")
    counted: dict[str, int] = defaultdict(int)
    for name in traced_names:
        counted[name] += 1
    try:
        normalized_counts = {str(name): int(count) for name, count in tool_counts.items()}
    except (TypeError, ValueError) as error:
        raise ValueError(f"{location} tool counts are invalid") from error
    if dict(counted) != normalized_counts:
        raise ValueError(f"{location} tool counts do not match its trace")

    raw_submit = run.get("raw_submit")
    if ok:
        if not isinstance(raw_submit, dict) or not isinstance(raw_submit.get("findings"), list):
            raise ValueError(f"valid terminal run lacks its raw submit in {location}")
        try:
            raw_findings = EvalSubmission.model_validate(raw_submit).model_dump(mode="json")[
                "findings"
            ]
        except ValueError as error:
            raise ValueError(
                f"valid terminal run has an invalid raw submit in {location}"
            ) from error
        if (
            run.get("store") != []
            or run.get("submission_only") != raw_findings
            or run.get("delivered") != raw_findings
        ):
            raise ValueError(f"{location} violates direct-delivery isolation")
    elif failure == "submit_schema_invalid":
        if not isinstance(raw_submit, dict):
            raise ValueError(f"invalid-submit run lacks its raw payload in {location}")
    elif raw_submit is not None:
        raise ValueError(f"no_tool_call run unexpectedly has a raw submit in {location}")

    if not ok:
        for channel_name in ("submission_only", "store", "delivered"):
            if run.get(channel_name) != []:
                raise ValueError(
                    f"Agent failure must score an empty {channel_name} channel in {location}"
                )


def _setup_value(setup: dict[str, Any], name: str) -> object:
    """Read a setup field from the root or its optional timing sub-object."""

    if name in setup:
        return setup[name]
    timing = setup.get("timing")
    if isinstance(timing, dict):
        return timing.get(name)
    return None


def _validate_setup(
    run: dict[str, Any],
    agent: str,
    *,
    reused_legacy_control: bool,
) -> None:
    """Fail closed on graph-index provenance without requiring transient paths."""

    setup = run.get("setup")
    location = f"{run.get('pair_key')}/{agent} setup"
    if not isinstance(setup, dict):
        raise ValueError(f"{location} must be an object")
    if reused_legacy_control:
        if agent != GRAPH_DEFAULT_AGENT:
            raise ValueError(f"{location} legacy setup is only valid for the control")
        if setup != {}:
            raise ValueError(f"{location} legacy control setup must be exactly empty")
        return
    if setup.get("isolated") is not True:
        raise ValueError(f"{location} does not prove index isolation")
    if setup.get("source_head") != run.get("head_revision"):
        raise ValueError(f"{location} source HEAD does not match the scored snapshot")
    if setup.get("agent_repo_clean") is not True:
        raise ValueError(f"{location} does not prove the Agent repository stayed clean")
    if setup.get("agent_repo_graph_dirs_absent") is not True:
        raise ValueError(f"{location} does not prove graph directories stayed hidden")
    if setup.get("index_success") is not True:
        raise ValueError(f"{location} does not record a successful index state")
    for field in ("installer_used", "mcp_used", "prompt_or_hook_injection"):
        if setup.get(field) is not False:
            raise ValueError(f"{location} has forbidden setup state {field}")
    if not isinstance(setup.get("query_calls"), list):
        raise ValueError(f"{location} query call audit is missing")
    if agent == GRAPH_DEFAULT_AGENT:
        provider = str(setup.get("provider", "none"))
        if provider != "none":
            raise ValueError(f"{location} unexpectedly declares provider {provider!r}")
        if setup.get("package_version") is not None or setup.get("binary_sha256") is not None:
            raise ValueError(f"{location} unexpectedly declares a graph binary")
        if setup.get("index_size_bytes") != 0 or setup.get("query_calls") != []:
            raise ValueError(f"{location} unexpectedly records graph work")
        for field in ("isolation_clone_seconds", "index_seconds"):
            if (
                _nonnegative_seconds(_setup_value(setup, field), field=field, location=location)
                != 0
            ):
                raise ValueError(f"{location} unexpectedly records {field}")
        return

    if setup.get("cleanup_success") is not True:
        raise ValueError(f"{location} does not prove isolated index cleanup")

    expected_provider = "codegraph" if agent == CODEGRAPH_AGENT else "gitnexus"
    if setup.get("provider") != expected_provider:
        raise ValueError(f"{location} provider must be {expected_provider!r}")
    package_version = setup.get("package_version")
    if package_version != EXPECTED_PACKAGE_VERSIONS[agent]:
        raise ValueError(f"{location} package version is not the pinned version")
    binary_sha256 = setup.get("binary_sha256")
    try:
        decoded_hash = bytes.fromhex(binary_sha256) if isinstance(binary_sha256, str) else b""
    except ValueError:
        decoded_hash = b""
    if len(decoded_hash) != 32:
        raise ValueError(f"{location} binary SHA-256 is invalid")
    index_size = setup.get("index_size_bytes")
    if isinstance(index_size, bool) or not isinstance(index_size, int) or index_size <= 0:
        raise ValueError(f"{location} index size is invalid")
    for field in ("isolation_clone_seconds", "index_seconds"):
        _nonnegative_seconds(_setup_value(setup, field), field=field, location=location)
    if agent == CODEGRAPH_AGENT:
        if setup.get("telemetry_disabled") is not True:
            raise ValueError(f"{location} does not prove telemetry was disabled")
        if setup.get("update_checks_disabled") is not True:
            raise ValueError(f"{location} does not prove update checks were disabled")
    elif (
        setup.get("registry_home_isolated") is not True
        or setup.get("wrapper_read_only_allowlist") is not True
        or setup.get("fts_status") != "available"
        or setup.get("graph_status") != "available"
        or setup.get("fts_extension_policy") != "load-only"
        or setup.get("embeddings_enabled") is not False
        or setup.get("gitnexus_config_present") is not False
    ):
        raise ValueError(f"{location} GitNexus isolation/read-only state is invalid")


def _normalize_source_run(
    artifact: dict[str, Any],
    run: dict[str, Any],
    *,
    path: Path,
) -> tuple[dict[str, Any], str, str, bool]:
    """Normalize only the legacy control identity while retaining its source."""

    source_agent = str(run.get("agent", ""))
    source_protocol = str(run.get("protocol_version", ""))
    if artifact.get("agent") != source_agent:
        raise ValueError(f"artifact/run source-agent mismatch in {path}")
    if artifact.get("protocol_version") != source_protocol:
        raise ValueError(f"artifact/run source-protocol mismatch in {path}")

    reused_legacy_control = source_agent == LEGACY_CONTROL_AGENT
    if reused_legacy_control:
        if source_protocol != LEGACY_CONTROL_PROTOCOL_VERSION:
            raise ValueError(f"legacy control has an unsupported protocol in {path}")
        normalized_agent = GRAPH_DEFAULT_AGENT
    else:
        if source_agent not in EXPECTED_AGENTS:
            raise ValueError(f"unexpected source agent {source_agent!r} in {path}")
        if source_protocol != GRAPH_PROTOCOL_VERSION:
            raise ValueError(
                f"graph protocol is required for source agent {source_agent!r} in {path}"
            )
        normalized_agent = source_agent

    normalized = copy.deepcopy(run)
    normalized["agent"] = normalized_agent
    normalized["protocol_version"] = GRAPH_PROTOCOL_VERSION
    normalized["source_agent"] = source_agent
    normalized["source_protocol_version"] = source_protocol
    normalized["reused_control"] = reused_legacy_control
    return normalized, source_agent, source_protocol, reused_legacy_control


def _validate_timing(
    run: dict[str, Any],
    *,
    path: Path,
    reused_legacy_control: bool,
) -> None:
    timing = run.get("timing")
    if not isinstance(timing, dict):
        raise ValueError(f"timing is missing in {path}")
    fields = ("setup_seconds", "agent_seconds", "total_seconds")
    if reused_legacy_control:
        if "cleanup_seconds" in timing:
            raise ValueError(f"legacy control timing unexpectedly includes cleanup in {path}")
    else:
        fields = (*fields, "cleanup_seconds")
    for field in fields:
        seconds = _nonnegative_seconds(timing.get(field), field=field, location=str(path))
        if reused_legacy_control and field == "setup_seconds" and seconds != 0:
            raise ValueError(f"legacy control setup time must be zero in {path}")


def _synthesize_legacy_control_metadata(
    run: dict[str, Any],
    *,
    repo_path: Path,
    source_tree: str,
) -> None:
    """Adapt the v3 control shape without changing the frozen raw artifact."""

    graph_dirs_absent = all(not (repo_path / name).exists() for name in (".codegraph", ".gitnexus"))
    if not graph_dirs_absent:
        raise ValueError("legacy control target contains a graph-provider directory")
    run["setup"] = {
        "provider": "none",
        "isolated": True,
        "source_head": run.get("head_revision"),
        "source_tree": source_tree,
        "agent_repo_clean": True,
        "agent_repo_graph_dirs_absent": True,
        "index_success": True,
        "package_version": None,
        "binary_sha256": None,
        "isolation_clone_seconds": 0.0,
        "index_seconds": 0.0,
        "index_size_bytes": 0,
        "index_stats": {},
        "installer_used": False,
        "mcp_used": False,
        "prompt_or_hook_injection": False,
        "query_calls": [],
        "cleanup_seconds": 0.0,
        "synthetic_from_legacy_control": True,
    }
    run["timing"] = {
        **run["timing"],
        "setup_seconds": 0.0,
        "cleanup_seconds": 0.0,
    }


def _pair_mismatch(treatment: dict[str, Any], control: dict[str, Any]) -> list[str]:
    mismatches: list[str] = []
    for field in (
        "protocol_version",
        "baseline_revision",
        "head_revision",
        "prompt",
        "configuration",
        "requested_model",
    ):
        if treatment.get(field) != control.get(field):
            mismatches.append(field)
    if treatment.get("tools", {}).get("base_schema_sha256") != control.get("tools", {}).get(
        "base_schema_sha256"
    ):
        mismatches.append("base_tool_schema")
    if treatment.get("tools", {}).get("base_names") != control.get("tools", {}).get("base_names"):
        mismatches.append("base_tool_names")
    treatment_models = treatment.get("conversation", {}).get("actual_models", [])
    control_models = control.get("conversation", {}).get("actual_models", [])
    if treatment_models != control_models:
        mismatches.append("actual_model")
    return mismatches


def _preflight_artifacts(
    frozen_artifacts: list[FrozenArtifact],
) -> tuple[
    dict[str, tuple[Path, str]],
    dict[str, Any],
    dict[Path, dict[str, Any]],
]:
    """Audit the complete nine-run batch before ground truth is first read."""

    expected_identities = {
        (pair_key, agent) for pair_key in EXPECTED_PAIR_KEYS for agent in EXPECTED_AGENTS
    }
    runs_by_identity: dict[tuple[str, str], dict[str, Any]] = {}
    frozen_by_identity: dict[tuple[str, str], FrozenArtifact] = {}
    normalized_runs_by_path: dict[Path, dict[str, Any]] = {}
    target_cache: dict[str, tuple[Path, str]] = {}

    for frozen in frozen_artifacts:
        artifact = frozen.payload
        if artifact.get("langfuse_enabled") is not False:
            raise ValueError(f"observability state is not explicitly disabled: {frozen.path}")
        runs = artifact.get("runs")
        if not isinstance(runs, list) or len(runs) != 1 or not isinstance(runs[0], dict):
            raise ValueError(f"artifact must contain exactly one completed run: {frozen.path}")
        run, source_agent, _source_protocol, reused_control = _normalize_source_run(
            artifact,
            runs[0],
            path=frozen.path,
        )
        agent = str(run["agent"])
        pair_key = str(run.get("pair_key", ""))
        identity = (pair_key, agent)
        if identity not in expected_identities:
            raise ValueError(f"unexpected pair/agent identity {identity!r} in {frozen.path}")
        if identity in runs_by_identity:
            raise ValueError(f"duplicate run for pair {pair_key!r}, agent {agent!r}")
        pair_id = pair_key.removesuffix(".1")
        if (
            artifact.get("agent") != source_agent
            or artifact.get("pair_id") != pair_id
            or run.get("run") != 1
            or run.get("protocol_version") != GRAPH_PROTOCOL_VERSION
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
        if not isinstance(configuration.get("transport"), dict):
            raise ValueError(f"transport configuration is missing in {frozen.path}")

        tools = run.get("tools")
        if not isinstance(tools, dict):
            raise ValueError(f"tool metadata is missing in {frozen.path}")
        expected_menu = tuple(EXPECTED_TOOL_MENUS[agent])
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
        _validate_setup(
            run,
            agent,
            reused_legacy_control=reused_control,
        )
        _validate_timing(run, path=frozen.path, reused_legacy_control=reused_control)

        artifact_started_at_ns = _timestamp_ns(
            artifact, "generation_started_at_ns", str(frozen.path)
        )
        artifact_completed_at_ns = _timestamp_ns(
            artifact, "generation_completed_at_ns", str(frozen.path)
        )
        run_started_at_ns = _timestamp_ns(run, "generation_started_at_ns", str(frozen.path))
        run_completed_at_ns = _timestamp_ns(run, "generation_completed_at_ns", str(frozen.path))
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
        source_tree = _git_tree(repo_path)
        if not reused_control and run["setup"].get("source_tree") != source_tree:
            raise ValueError(f"setup source tree mismatch in {frozen.path}")
        if not _git_worktree_is_clean(repo_path):
            raise ValueError(f"target worktree is not frozen/clean for {frozen.path}")
        if reused_control:
            _synthesize_legacy_control_metadata(
                run,
                repo_path=repo_path,
                source_tree=source_tree,
            )

        runs_by_identity[identity] = run
        frozen_by_identity[identity] = frozen
        normalized_runs_by_path[frozen.path] = run

    if set(runs_by_identity) != expected_identities:
        missing = sorted(expected_identities - set(runs_by_identity))
        unexpected = sorted(set(runs_by_identity) - expected_identities)
        raise ValueError(f"incomplete three-arm batch; missing={missing}, unexpected={unexpected}")

    first_artifact = frozen_artifacts[0].payload
    first_run = normalized_runs_by_path[frozen_artifacts[0].path]
    for frozen in frozen_artifacts[1:]:
        artifact = frozen.payload
        run = normalized_runs_by_path[frozen.path]
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
        raise ValueError("base tool schemas differ across the nine-run batch")
    for agent in EXPECTED_AGENTS:
        schema_hashes = {
            runs_by_identity[(pair_key, agent)]["tools"]["schema_sha256"]
            for pair_key in EXPECTED_PAIR_KEYS
        }
        if len(schema_hashes) != 1:
            raise ValueError(f"{agent} tool schema differs across pairs")
    for agent in (CODEGRAPH_AGENT, GITNEXUS_AGENT):
        versions = {
            str(runs_by_identity[(pair_key, agent)]["setup"]["package_version"])
            for pair_key in EXPECTED_PAIR_KEYS
        }
        if len(versions) != 1:
            raise ValueError(f"{agent} package version differs across pairs")
        binary_hashes = {
            str(runs_by_identity[(pair_key, agent)]["setup"]["binary_sha256"])
            for pair_key in EXPECTED_PAIR_KEYS
        }
        if len(binary_hashes) != 1:
            raise ValueError(f"{agent} binary differs across pairs")
    for pair_key in EXPECTED_PAIR_KEYS:
        control = runs_by_identity[(pair_key, GRAPH_DEFAULT_AGENT)]
        for treatment_agent in (CODEGRAPH_AGENT, GITNEXUS_AGENT):
            reasons = _pair_mismatch(runs_by_identity[(pair_key, treatment_agent)], control)
            if reasons:
                raise ValueError(
                    f"pair {pair_key} failed {treatment_agent}=default+graph audit: "
                    f"{', '.join(reasons)}"
                )

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
                "protocol_version": GRAPH_PROTOCOL_VERSION,
                "source_agent": run["source_agent"],
                "source_protocol_version": run["source_protocol_version"],
                "reused_control": run["reused_control"],
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
        "earliest_generation_start_ns": min(recorded_starts),
        "latest_generation_start_ns": max(recorded_starts),
        "earliest_generation_completion_ns": min(recorded_completions),
        "latest_generation_completion_ns": max(recorded_completions),
        "launch_spread_seconds": round(
            (max(recorded_starts) - min(recorded_starts)) / 1_000_000_000, 6
        ),
        "batch_makespan_seconds": round(
            (max(recorded_completions) - min(recorded_starts)) / 1_000_000_000, 6
        ),
    }
    filesystem_timing = {
        "source": "filesystem-derived",
        "precision_note": (
            "Approximate: total_seconds is rounded to milliseconds and artifact mtime "
            "includes final serialization/write latency."
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
    return (
        target_cache,
        {
            "passed_before_ground_truth": True,
            "artifact_count": len(frozen_artifacts),
            "pair_keys": list(EXPECTED_PAIR_KEYS),
            "agents": list(EXPECTED_AGENTS),
            "reused_control_artifacts": sum(
                bool(run["reused_control"]) for run in runs_by_identity.values()
            ),
            "artifact_manifest": artifact_manifest,
            "batch_timing": batch_timing,
            "filesystem_timing": filesystem_timing,
        },
        normalized_runs_by_path,
    )


def _decode_ground_truth(raw: bytes) -> tuple[list[dict[str, Any]], int, dict[str, str]]:
    payload = json.loads(raw)
    items = payload["items"]
    window = int(payload.get("window_lines", 40))
    classes = {str(item["label"]): str(item.get("class", "prose")) for item in items}
    return items, window, classes


def _evidence_valid(
    repo_path: Path, findings: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate submitted evidence without repairing or re-anchoring it."""

    valid: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    root = repo_path.resolve()
    for index, finding in enumerate(findings):
        doc = str(finding.get("doc", ""))
        quote = str(finding.get("quote", ""))
        reason = ""
        line_value = finding.get("line", 0)
        line = line_value if isinstance(line_value, int) and not isinstance(line_value, bool) else 0
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


def _phase_seconds(run: dict[str, Any], field: str) -> float:
    timing = run.get("timing", {})
    if field in timing:
        return float(timing[field])
    setup = run.get("setup", {})
    value = _setup_value(setup, field) if isinstance(setup, dict) else None
    return (
        0.0
        if value is None
        else _nonnegative_seconds(value, field=field, location=str(run.get("agent", "")))
    )


def _score_run(
    run: dict[str, Any],
    *,
    repo_path: Path,
    items: list[dict[str, Any]],
    window: int,
    classes: dict[str, str],
    spans: dict[str, list[tuple[int, int]]],
) -> dict[str, Any]:
    conversation = run.get("conversation", {})
    conversation_ok = conversation.get("ok") is True
    raw_submit = run.get("raw_submit")
    raw_submit_findings = (
        raw_submit.get("findings", []) if conversation_ok and isinstance(raw_submit, dict) else []
    )
    channel_inputs = {
        "raw_submit_diagnostic": raw_submit_findings,
        "submission_only": run.get("submission_only", []),
        "store": run.get("store", []),
        "delivered": run.get("delivered", []),
    }
    channels: dict[str, Any] = {}
    for name, candidates in channel_inputs.items():
        raw = [dict(item) for item in candidates if isinstance(item, dict)]
        evidence_valid, invalid = _evidence_valid(repo_path, raw)
        channels[name] = {
            "raw": _score_channel(raw, items=items, window=window, classes=classes, spans=spans),
            "evidence_valid": _score_channel(
                evidence_valid,
                items=items,
                window=window,
                classes=classes,
                spans=spans,
            ),
            "invalid_evidence": invalid,
        }

    agent = str(run.get("agent", ""))
    treatment_names = set(TREATMENT_TOOLS[agent])
    treatment_tool_sequence = [
        name
        for trace_entry in conversation.get("turn_trace", [])
        for name in trace_entry.get("tool_calls", [])
        if name in treatment_names
    ]
    failure = str(conversation.get("failure_reason") or "")
    normalized_tool_counts = {
        str(name): int(count) for name, count in conversation.get("tool_counts", {}).items()
    }
    return {
        "agent": agent,
        "source_agent": run.get("source_agent", agent),
        "source_protocol_version": run.get("source_protocol_version", run.get("protocol_version")),
        "reused_control": run.get("reused_control") is True,
        "pair_key": run.get("pair_key"),
        "conversation_ok": conversation_ok,
        "failure_reason": failure or None,
        "agent_failure": bool(failure in AGENT_FAILURE_REASONS),
        "channels": channels,
        "usage": run.get("usage", {}),
        "timing": {
            **run.get("timing", {}),
            "isolation_clone_seconds": _phase_seconds(run, "isolation_clone_seconds"),
            "index_seconds": _phase_seconds(run, "index_seconds"),
        },
        "setup": run.get("setup", {}),
        "tool_counts": normalized_tool_counts,
        "treatment_tool_sequence": treatment_tool_sequence,
    }


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
    pair_keys: list[str],
    score_lookup: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    rows = [score_lookup[(pair_key, agent)] for pair_key in pair_keys]
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
    setup_seconds = [float(row["timing"].get("setup_seconds", 0.0)) for row in rows]
    clone_seconds = [float(row["timing"]["isolation_clone_seconds"]) for row in rows]
    index_seconds = [float(row["timing"]["index_seconds"]) for row in rows]
    agent_seconds = [float(row["timing"].get("agent_seconds", 0.0)) for row in rows]
    cleanup_seconds = [float(row["timing"].get("cleanup_seconds", 0.0)) for row in rows]
    total_seconds = [float(row["timing"].get("total_seconds", 0.0)) for row in rows]
    costs_per_hit = [cost / hit for cost, hit in zip(costs, hits, strict=True) if hit > 0]
    tool_counts: dict[str, int] = defaultdict(int)
    for row in rows:
        for name, count in row["tool_counts"].items():
            tool_counts[str(name)] += int(count)
    union = set().union(*hit_sets) if hit_sets else set()
    treatment_tool_runs = sum(bool(row["treatment_tool_sequence"]) for row in rows)
    return {
        "runs": len(rows),
        "completed_submissions": sum(bool(row["conversation_ok"]) for row in rows),
        "agent_failures": sum(bool(row["agent_failure"]) for row in rows),
        "hits": _distribution(hits),
        "extras": _distribution(extras),
        "model_calls": _distribution(calls),
        "tool_call_count": _distribution(tool_call_totals),
        "input_tokens": _distribution(input_tokens),
        "output_tokens": _distribution(output_tokens),
        "actual_tokens": _distribution(tokens),
        "cost_usd": _distribution(costs),
        "setup_seconds": _distribution(setup_seconds),
        "isolation_clone_seconds": _distribution(clone_seconds),
        "index_seconds": _distribution(index_seconds),
        "agent_seconds": _distribution(agent_seconds),
        "cleanup_seconds": _distribution(cleanup_seconds),
        "total_seconds": _distribution(total_seconds),
        "cost_per_valid_hit": _distribution(costs_per_hit),
        "union_hits": sorted(union),
        "union_at_3": H.union_over_repeats(hit_sets, 3),
        "tool_calls": dict(sorted(tool_counts.items())),
        "treatment_tool_runs": treatment_tool_runs,
        "invalid_evidence": sum(
            len(row["channels"]["delivered"]["invalid_evidence"]) for row in rows
        ),
    }


def _comparison_summary(
    pair_rows: list[dict[str, Any]],
    *,
    treatment_agent: str,
    control_agent: str,
    score_lookup: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    deltas = [int(pair["delta_hits"]) for pair in pair_rows]
    extra_deltas = [int(pair["delta_extras"]) for pair in pair_rows]
    treatment_hit_sets = [
        set(
            score_lookup[(pair["pair_key"], treatment_agent)]["channels"]["delivered"][
                "evidence_valid"
            ]["hits"]
        )
        for pair in pair_rows
    ]
    control_hit_sets = [
        set(
            score_lookup[(pair["pair_key"], control_agent)]["channels"]["delivered"][
                "evidence_valid"
            ]["hits"]
        )
        for pair in pair_rows
    ]
    treatment_union = set().union(*treatment_hit_sets) if treatment_hit_sets else set()
    control_union = set().union(*control_hit_sets) if control_hit_sets else set()
    positive_signal = (
        len(pair_rows) >= 3
        and sum(delta > 0 for delta in deltas) >= 2
        and statistics.mean(deltas) >= 1
        and len(treatment_union) >= len(control_union)
        and statistics.mean(extra_deltas) <= 1
    )
    return {
        "treatment_agent": treatment_agent,
        "control_agent": control_agent,
        "valid_pairs": len(pair_rows),
        "treatment_wins": sum(delta > 0 for delta in deltas),
        "ties": sum(delta == 0 for delta in deltas),
        "control_wins": sum(delta < 0 for delta in deltas),
        "paired_mean_delta_hits": round(statistics.mean(deltas), 3) if deltas else None,
        "paired_median_delta_hits": statistics.median(deltas) if deltas else None,
        "paired_mean_delta_extras": (
            round(statistics.mean(extra_deltas), 3) if extra_deltas else None
        ),
        "treatment_union_hits": sorted(treatment_union),
        "control_union_hits": sorted(control_union),
        "treatment_union_at_3": H.union_over_repeats(treatment_hit_sets, 3),
        "control_union_at_3": H.union_over_repeats(control_hit_sets, 3),
        "preregistered_positive_signal": positive_signal,
    }


def score_artifacts(artifact_paths: list[Path], ground_truth: Path) -> dict[str, Any]:
    frozen_artifacts = _freeze_artifacts(artifact_paths)
    target_cache, protocol_audit, normalized_runs_by_path = _preflight_artifacts(frozen_artifacts)

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
    for frozen in frozen_artifacts:
        artifact = frozen.payload
        repo_path, _fixture_baseline = _target_repo(artifact["target"], target_cache)
        spans = H.section_spans(repo_path, gt_docs)
        normalized_run = normalized_runs_by_path[frozen.path]
        scored = _score_run(
            normalized_run,
            repo_path=repo_path,
            items=items,
            window=window,
            classes=classes,
            spans=spans,
        )
        scored["artifact"] = str(frozen.path)
        rows.append(scored)

    score_lookup = {(str(row["pair_key"]), str(row["agent"])): row for row in rows}
    pairs: list[dict[str, Any]] = []
    comparison_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for pair_key in EXPECTED_PAIR_KEYS:
        arm_metrics: dict[str, Any] = {}
        for agent in EXPECTED_AGENTS:
            row = score_lookup[(pair_key, agent)]
            primary = row["channels"]["delivered"]["evidence_valid"]
            arm_metrics[agent] = {
                "recall": primary["recall"],
                "hits": primary["hits"],
                "extras": primary["extras"],
                "conversation_ok": row["conversation_ok"],
                "failure_reason": row["failure_reason"],
            }
        comparisons: dict[str, Any] = {}
        for comparison_name, (treatment_agent, control_agent) in COMPARISONS.items():
            treatment = arm_metrics[treatment_agent]
            control = arm_metrics[control_agent]
            comparison = {
                "treatment_agent": treatment_agent,
                "control_agent": control_agent,
                "delta_hits": len(treatment["hits"]) - len(control["hits"]),
                "delta_extras": treatment["extras"] - control["extras"],
            }
            comparisons[comparison_name] = comparison
            comparison_rows[comparison_name].append({"pair_key": pair_key, **comparison})
        pairs.append(
            {
                "pair_key": pair_key,
                "valid": True,
                "arms": arm_metrics,
                "comparisons": comparisons,
            }
        )

    arm_summaries = {
        agent: _arm_summary(agent, list(EXPECTED_PAIR_KEYS), score_lookup)
        for agent in EXPECTED_AGENTS
    }
    comparison_summaries = {
        comparison_name: _comparison_summary(
            comparison_rows[comparison_name],
            treatment_agent=treatment_agent,
            control_agent=control_agent,
            score_lookup=score_lookup,
        )
        for comparison_name, (treatment_agent, control_agent) in COMPARISONS.items()
    }

    return {
        "protocol_version": GRAPH_PROTOCOL_VERSION,
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
        "agent_failure_policy": (
            "submit_schema_invalid and no_tool_call score zero delivered findings; "
            "model/provider failures invalidate the batch"
        ),
        "control_reuse_policy": (
            "single-agent-tool-ablation-v3-unbounded/default_tools_agent controls are "
            "accepted only with their legacy empty setup and missing cleanup timing; identity "
            "and zero graph/setup/cleanup metadata are normalized in memory only"
        ),
        "runs": rows,
        "pairs": pairs,
        "summary": {
            "valid_pairs": len(EXPECTED_PAIR_KEYS),
            "batch_timing": protocol_audit["batch_timing"],
            "arms": arm_summaries,
            "comparisons": comparison_summaries,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Score completed default/CodeGraph/GitNexus raw artifacts"
    )
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

"""Fail-closed offline scorer for the single-Agent tool-portfolio search.

Every input is a raw, one-run artifact produced without ground-truth access.
This scorer freezes and preflights the *entire* supplied stage before it opens
ground truth.  Model/provider failures invalidate the stage; attributable
Agent terminal failures remain observable zero-recall outcomes.
"""

# The field harness is a sibling script rather than an installed package.
# ruff: noqa: E402

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

FIELD_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = FIELD_DIR.parent.parent
sys.path.insert(0, str(FIELD_DIR))
sys.path.insert(0, str(REPO_ROOT))

import _harness as H
import _portfolio_gitnexus_exact_composite as _exact_composite_module
import _portfolio_gitnexus_focused_exact as _focused_exact_module
from _graph_runtime import (
    CODEGRAPH_PACKAGE_INTEGRITY,
    CODEGRAPH_VERSION,
    GITNEXUS_PACKAGE_INTEGRITY,
    GITNEXUS_VERSION,
)
from _portfolio_brief import AUDIT_BRIEF_DEFINITION
from _portfolio_codegraph_node_impact import (
    CODEGRAPH_NODE_IMPACT_DEFINITION,
    CODEGRAPH_NODE_IMPACT_PROFILE,
    CODEGRAPH_NODE_IMPACT_PROTOCOL,
)
from _portfolio_generic import paged_generic_runtime
from _portfolio_gitnexus_focused_exact import (
    GITNEXUS_FOCUSED_EXACT_DEFINITION,
    GITNEXUS_FOCUSED_EXACT_PROFILE_ID,
    GITNEXUS_FOCUSED_EXACT_PROTOCOL_VERSION,
    GITNEXUS_FOCUSED_EXACT_TOOL,
)
from _portfolio_native_graph import CODEGRAPH_EXPLORE_DIRECT_DEFINITION
from _runner import (
    BASE_TOOLS,
    TOOL_PORTFOLIO_GITNEXUS_FIRST_PROTOCOL_VERSION,
    TOOL_PORTFOLIO_GITNEXUS_FOCUSED_EXACT_PROTOCOL_VERSION,
    TOOL_PORTFOLIO_GITNEXUS_STRUCTURED_FIRST_PROTOCOL_VERSION,
    TOOL_PORTFOLIO_NATIVE_PROTOCOL_VERSION,
    TOOL_PORTFOLIO_PROTOCOL_VERSION,
    AgentContext,
    EvalSubmission,
    _result_trace,
    _trace_arguments,
)
from gitnexus_focused_exact_agent import GITNEXUS_FOCUSED_EXACT_SYSTEM_PROMPT
from score_graph_ablation import (
    _decode_ground_truth,
    _evidence_valid,
    _score_channel,
)

PROTOCOL_V1 = "single-agent-tool-portfolio-v1"
PROTOCOL_V2 = TOOL_PORTFOLIO_PROTOCOL_VERSION
PROTOCOL_V3 = TOOL_PORTFOLIO_NATIVE_PROTOCOL_VERSION
PROTOCOL_V4 = TOOL_PORTFOLIO_GITNEXUS_FIRST_PROTOCOL_VERSION
PROTOCOL_V5 = TOOL_PORTFOLIO_GITNEXUS_STRUCTURED_FIRST_PROTOCOL_VERSION
PROTOCOL_GITNEXUS_FOCUSED_EXACT = (
    TOOL_PORTFOLIO_GITNEXUS_FOCUSED_EXACT_PROTOCOL_VERSION
)
PROTOCOL_VERSION = PROTOCOL_V2
SUPPORTED_PROTOCOL_VERSIONS = frozenset(
    {
        PROTOCOL_V1,
        PROTOCOL_V2,
        PROTOCOL_V3,
        PROTOCOL_V4,
        PROTOCOL_V5,
        PROTOCOL_GITNEXUS_FOCUSED_EXACT,
    }
)
DEFAULT_CONTROL_AGENT = "portfolio_control_agent"
NATIVE_CONTROL_AGENT = "portfolio_native_control_agent"
GITNEXUS_FIRST_CONTROL_AGENT = "portfolio_gitnexus_first_control_agent"
GITNEXUS_STRUCTURED_FIRST_CONTROL_AGENT = (
    "portfolio_gitnexus_structured_first_control_agent"
)
GITNEXUS_FOCUSED_EXACT_CONTROL_AGENT = (
    "portfolio_gitnexus_focused_exact_control_agent"
)
AGENT_FAILURE_REASONS = frozenset({"submit_schema_invalid", "submit_not_solitary", "no_tool_call"})
EXTERNAL_FAILURE_PREFIXES = (
    "model:",
    "provider:",
    "transport:",
    "openrouter:",
    "budget:",
)
OPTIONAL_ADOPTION_RULES = {
    "audit_brief": ("exactly", 1),
    "graph_context": ("at_least", 1),
    "codegraph_explore": ("at_least", 1),
    "codegraph_node_impact": ("exactly", 1),
    "gitnexus_change_impact": ("at_least", 1),
    "gitnexus_structured_change": ("exactly", 1),
    "gitnexus_focused_exact": ("exactly", 1),
}
TOKEN_GUARDRAIL_MULTIPLIER = 1.2
STABILITY_MINIMUM_REPEATS = 3
STABILITY_MEAN_HIT_DELTA = 1.0
STABILITY_DIRECTION_NUMERATOR = 2
STABILITY_DIRECTION_DENOMINATOR = 3
EXPECTED_STREAMLAKE_ROUTING = {
    "require_parameters": True,
    "data_collection": "deny",
    "order": ["streamlake"],
    "only": ["streamlake"],
    "allow_fallbacks": False,
}
EXPECTED_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
EXPECTED_RESPONSE_BODY_BYTE_LIMIT = 1_048_576
EXPECTED_EXPORT_SCOPE = "code/docs/diff/derived-retrieval; ground truth excluded"
FORCED_AFTER_DIFF_TOOLS = (
    "graph_context",
    "codegraph_explore",
    "codegraph_node_impact",
    "gitnexus_change_impact",
    "gitnexus_focused_exact",
)
INITIAL_FORCED_TOOLS = ("gitnexus_structured_change",)
# Backward-compatible v2 fixture constant; v3 derives the selected name from
# each frozen tool menu.
FORCED_GRAPH_TOOL_CHOICE = {
    "type": "function",
    "function": {"name": "graph_context"},
}
EXPECTED_GRAPH_MANIPULATION_COMMON = {
    "trigger": "first_successful_git_diff",
    "automatic_target_generation": False,
    "same_turn_broad_scan_policy": "reject_without_execution",
    "post_submit_gate": False,
}
EXPECTED_INITIAL_MANIPULATION_COMMON = {
    "trigger": "initial_request",
    "automatic_target_generation": False,
    "same_turn_broad_scan_policy": "not_applicable",
    "post_submit_gate": False,
}
AGENT_OPTIONAL_TOOL_WHITELIST = {
    DEFAULT_CONTROL_AGENT: (),
    NATIVE_CONTROL_AGENT: (),
    GITNEXUS_FIRST_CONTROL_AGENT: (),
    GITNEXUS_STRUCTURED_FIRST_CONTROL_AGENT: (),
    GITNEXUS_FOCUSED_EXACT_CONTROL_AGENT: (),
    "brief_diff_agent": ("audit_brief",),
    "doc_map_agent": ("audit_brief",),
    "change_seed_agent": ("audit_brief",),
    "alignment_map_agent": ("audit_brief",),
    "brief_diff_doc_map_agent": ("audit_brief",),
    "codegraph_context_agent": ("graph_context",),
    "gitnexus_context_agent": ("graph_context",),
    "codegraph_explore_direct_agent": ("codegraph_explore",),
    "codegraph_explore_change_seed_agent": ("codegraph_explore", "audit_brief"),
    "codegraph_node_impact_agent": ("codegraph_node_impact",),
    "gitnexus_change_impact_agent": ("gitnexus_change_impact",),
    "gitnexus_change_impact_first_agent": ("gitnexus_change_impact",),
    "gitnexus_structured_change_first_agent": ("gitnexus_structured_change",),
    "gitnexus_focused_exact_agent": ("gitnexus_focused_exact",),
}
NATIVE_PROTOCOL_AGENTS = frozenset(
    {
        NATIVE_CONTROL_AGENT,
        "codegraph_explore_direct_agent",
        "codegraph_explore_change_seed_agent",
        "codegraph_node_impact_agent",
        "gitnexus_change_impact_agent",
    }
)
EXPECTED_FORWARD_CHILD_TOOL_DELTAS = {
    "codegraph_explore_change_seed_agent": {
        "parent": "codegraph_explore_direct_agent",
        "incremental_tools": ("audit_brief",),
    },
}
GITNEXUS_FIRST_PROTOCOL_AGENTS = frozenset(
    {
        GITNEXUS_FIRST_CONTROL_AGENT,
        "gitnexus_change_impact_first_agent",
    }
)
GITNEXUS_STRUCTURED_FIRST_PROTOCOL_AGENTS = frozenset(
    {
        GITNEXUS_STRUCTURED_FIRST_CONTROL_AGENT,
        "gitnexus_structured_change_first_agent",
    }
)
GITNEXUS_FOCUSED_EXACT_PROTOCOL_AGENTS = frozenset(
    {
        GITNEXUS_FOCUSED_EXACT_CONTROL_AGENT,
        "gitnexus_focused_exact_agent",
    }
)
GITNEXUS_FOCUSED_EXACT_SYSTEM_PROMPT_SHA256 = (
    "c08b3e1d69b5e9e2d4af527e632e7956ff86faa4ab7f796c8a9ee57c2bcff51c"
)
_SOURCE_REFERENCE = re.compile(
    r"(?<![\w./-])"
    r"(?P<path>(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.[A-Za-z0-9]+)"
    r":(?P<start>[1-9][0-9]*)(?:-(?P<end>[1-9][0-9]*))?"
)
_DOCUMENT_EXTENSIONS = frozenset({".md", ".mdx", ".rst", ".txt", ".adoc"})
_NON_SYMBOL_IDENTIFIERS = frozenset(
    {
        "and",
        "as",
        "async",
        "await",
        "break",
        "case",
        "catch",
        "class",
        "const",
        "continue",
        "debugger",
        "def",
        "default",
        "delete",
        "do",
        "elif",
        "else",
        "enum",
        "except",
        "export",
        "extends",
        "false",
        "finally",
        "fn",
        "for",
        "from",
        "function",
        "if",
        "implements",
        "import",
        "in",
        "instanceof",
        "interface",
        "is",
        "lambda",
        "let",
        "match",
        "namespace",
        "new",
        "none",
        "not",
        "null",
        "or",
        "package",
        "pass",
        "private",
        "protected",
        "public",
        "raise",
        "readonly",
        "return",
        "static",
        "struct",
        "super",
        "switch",
        "this",
        "throw",
        "trait",
        "true",
        "try",
        "type",
        "typeof",
        "var",
        "void",
        "while",
        "with",
        "yield",
    }
)

# These are experiment-wide ceilings, not the resumable page sizes exposed by
# individual repository tools.  Every field must be present and inactive.
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
    """One stable raw-artifact snapshot captured before GT is opened."""

    path: Path
    raw: bytes
    sha256: str
    size_bytes: int
    mtime_ns: int
    device: int
    inode: int
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class FrozenLaunchManifest:
    """Stable launch-plan/evidence snapshot captured before GT is opened."""

    path: Path
    raw: bytes
    sha256: str
    size_bytes: int
    mtime_ns: int
    device: int
    inode: int
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ReplayedDiff:
    """One frozen, successfully replayed git_diff result seen by the Agent."""

    position: tuple[int, int]
    content: str


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


def _git_baseline_is_ancestor(repo_path: Path, baseline: str, head: str) -> bool:
    if not baseline or not head:
        return False
    completed = subprocess.run(
        ["git", "merge-base", "--is-ancestor", baseline, head],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    return completed.returncode == 0


def _target_repo(
    descriptor: dict[str, Any],
    cache: dict[str, tuple[Path, str]],
) -> tuple[Path, str]:
    key = json.dumps(descriptor, ensure_ascii=False, sort_keys=True)
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
    """Freeze one or more stable, non-aliased artifacts without touching GT."""

    if not artifact_paths:
        raise ValueError("at least one raw artifact is required")

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


def _freeze_launch_manifest(path: Path) -> FrozenLaunchManifest:
    """Freeze the independently written launch manifest before any GT access."""

    resolved = path.resolve(strict=True)
    before = resolved.stat()
    raw = resolved.read_bytes()
    after = resolved.stat()
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
        raise ValueError("launch manifest changed while it was being frozen")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid launch manifest JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("launch manifest root must be an object")
    return FrozenLaunchManifest(
        path=resolved,
        raw=raw,
        sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=after.st_size,
        mtime_ns=after.st_mtime_ns,
        device=after.st_dev,
        inode=after.st_ino,
        payload=payload,
    )


def _verify_artifacts_remain_frozen(frozen_artifacts: list[FrozenArtifact]) -> None:
    """Ensure no input changed during the potentially long target preflight."""

    for frozen in frozen_artifacts:
        before = frozen.path.stat()
        raw = frozen.path.read_bytes()
        after = frozen.path.stat()
        expected = (
            frozen.device,
            frozen.inode,
            frozen.size_bytes,
            frozen.mtime_ns,
        )
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != expected or (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) != expected:
            raise ValueError(f"artifact changed after it was frozen: {frozen.path}")
        if hashlib.sha256(raw).hexdigest() != frozen.sha256:
            raise ValueError(f"artifact content changed after it was frozen: {frozen.path}")


def _verify_launch_manifest_remains_frozen(manifest: FrozenLaunchManifest) -> None:
    before = manifest.path.stat()
    raw = manifest.path.read_bytes()
    after = manifest.path.stat()
    expected = (manifest.device, manifest.inode, manifest.size_bytes, manifest.mtime_ns)
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != expected or (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ) != expected:
        raise ValueError("launch manifest changed after it was frozen")
    if hashlib.sha256(raw).hexdigest() != manifest.sha256:
        raise ValueError("launch manifest content changed after it was frozen")


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_json(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _timestamp_ns(container: dict[str, Any], field: str, location: str) -> int:
    value = container.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{location} {field} must be a positive integer timestamp")
    return value


def _nonnegative_int(value: object, *, field: str, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{location} {field} must be a non-negative integer")
    return value


def _nonnegative_number(value: object, *, field: str, location: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{location} {field} must be a non-negative number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{location} {field} must be a non-negative number")
    return number


def _valid_sha256(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64 or value != value.lower():
        return False
    try:
        return len(bytes.fromhex(value)) == 32
    except ValueError:
        return False


def _validate_child_parent_map(
    value: object,
    *,
    agents: list[str],
    control_agent: str,
    present: bool,
) -> dict[str, str]:
    """Validate optional same-stage forward-search ancestry from the manifest."""

    if not present:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("launch manifest child_parent_map must be an object")
    mapping: dict[str, str] = {}
    planned = set(agents)
    for child, parent in value.items():
        if not isinstance(child, str) or not child or not isinstance(parent, str) or not parent:
            raise ValueError("launch manifest child_parent_map names must be non-empty strings")
        if child not in planned or parent not in planned:
            raise ValueError("launch manifest child_parent_map references an unplanned agent")
        if child == control_agent or parent == control_agent:
            raise ValueError("launch manifest child_parent_map cannot use the control agent")
        if child == parent:
            raise ValueError("launch manifest child_parent_map cannot map an agent to itself")
        mapping[child] = parent
    for start in mapping:
        seen: set[str] = set()
        current = start
        while current in mapping:
            if current in seen:
                raise ValueError("launch manifest child_parent_map contains a cycle")
            seen.add(current)
            current = mapping[current]
    for child, specification in EXPECTED_FORWARD_CHILD_TOOL_DELTAS.items():
        if child not in planned:
            continue
        expected_parent = str(specification["parent"])
        if expected_parent not in planned or mapping.get(child) != expected_parent:
            raise ValueError(
                "launch manifest child_parent_map must register "
                f"{child}={expected_parent}"
            )
    return dict(sorted(mapping.items()))


def _validate_registered_forward_tool_deltas(
    child_parent_map: dict[str, str],
    runs_by_path: dict[Path, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Bind registered forward children to one exact model-visible tool delta."""

    first_run_by_agent: dict[str, dict[str, Any]] = {}
    for run in runs_by_path.values():
        first_run_by_agent.setdefault(str(run.get("agent", "")), run)

    validated: list[dict[str, Any]] = []
    expected_definitions = {
        "codegraph_explore": CODEGRAPH_EXPLORE_DIRECT_DEFINITION,
        "audit_brief": AUDIT_BRIEF_DEFINITION,
    }
    for child, specification in EXPECTED_FORWARD_CHILD_TOOL_DELTAS.items():
        if child not in child_parent_map:
            continue
        parent = str(specification["parent"])
        incremental_tools = tuple(cast(tuple[str, ...], specification["incremental_tools"]))
        if child_parent_map[child] != parent:
            raise ValueError(f"registered forward child {child!r} has the wrong parent")
        child_run = first_run_by_agent.get(child)
        parent_run = first_run_by_agent.get(parent)
        if child_run is None or parent_run is None:
            raise ValueError(f"registered forward child {child!r} lacks its paired parent run")
        if (
            child_run.get("protocol_version") != PROTOCOL_V3
            or parent_run.get("protocol_version") != PROTOCOL_V3
        ):
            raise ValueError(f"registered forward child {child!r} must use protocol v3")

        child_tools = cast(dict[str, Any], child_run["tools"])
        parent_tools = cast(dict[str, Any], parent_run["tools"])
        child_names = tuple(cast(list[str], child_tools["names"]))
        parent_names = tuple(cast(list[str], parent_tools["names"]))
        if child_names != (*parent_names, *incremental_tools):
            raise ValueError(
                f"registered forward child {child!r} does not add exactly "
                f"{incremental_tools!r} to {parent!r}"
            )

        child_definitions = cast(list[dict[str, Any]], child_tools["definitions"])
        parent_definitions = cast(list[dict[str, Any]], parent_tools["definitions"])
        if child_definitions[: len(parent_definitions)] != parent_definitions:
            raise ValueError(
                f"registered forward child {child!r} changes a parent tool definition"
            )
        child_by_name = {
            str(definition["function"]["name"]): definition
            for definition in child_definitions
        }
        for tool in ("codegraph_explore", *incremental_tools):
            if child_by_name.get(tool) != expected_definitions.get(tool):
                raise ValueError(
                    f"registered forward child {child!r} has an unexpected {tool} schema"
                )
        validated.append(
            {
                "child": child,
                "parent": parent,
                "incremental_tools": list(incremental_tools),
                "shared_tool_definitions": list(parent_names),
                "protocol_version": PROTOCOL_V3,
            }
        )
    return validated


def _validate_launch_manifest(
    manifest: FrozenLaunchManifest,
    frozen_artifacts: list[FrozenArtifact],
    *,
    control_agent: str,
) -> tuple[dict[str, Any], dict[Path, dict[str, Any]]]:
    """Validate the complete planned matrix and bind it to frozen artifacts."""

    payload = manifest.payload
    if (
        payload.get("raw_generation_only") is not True
        or payload.get("ground_truth_loaded") is not False
    ):
        raise ValueError("launch manifest is not raw-generation-only")
    if payload.get("fixture_export_scope") != EXPECTED_EXPORT_SCOPE:
        raise ValueError("launch manifest export scope does not explicitly exclude ground truth")
    authorization = payload.get("authorization_reference")
    if not isinstance(authorization, str) or not authorization.strip():
        raise ValueError("launch manifest authorization_reference must be non-empty")
    if payload.get("provider") != "streamlake" or payload.get("provider_fallbacks") is not False:
        raise ValueError("launch manifest must pin StreamLake without fallbacks")
    if payload.get("openrouter_base_url") != EXPECTED_OPENROUTER_BASE_URL:
        raise ValueError("launch manifest must pin the official OpenRouter endpoint")
    if payload.get("all_started_before_wait") is not True:
        raise ValueError("launch manifest did not start all jobs before waiting")
    if payload.get("schedule_mode") != "all-popen-starts-before-any-wait":
        raise ValueError("launch manifest schedule mode is invalid")

    agents = payload.get("planned_agents")
    pairs = payload.get("planned_pairs")
    schedule = payload.get("planned_schedule")
    jobs = payload.get("jobs")
    if (
        not isinstance(agents, list)
        or not agents
        or not all(isinstance(agent, str) and agent for agent in agents)
        or len(set(agents)) != len(agents)
    ):
        raise ValueError("launch manifest planned_agents is invalid")
    if payload.get("agents") != agents:
        raise ValueError("launch manifest agents disagree with planned_agents")
    unknown_agents = sorted(set(agents) - set(AGENT_OPTIONAL_TOOL_WHITELIST))
    if unknown_agents:
        raise ValueError(f"launch manifest contains unknown agents: {', '.join(unknown_agents)}")
    if control_agent not in agents:
        raise ValueError(f"contemporaneous control agent {control_agent!r} is missing")
    child_parent_map_present = "child_parent_map" in payload
    child_parent_map = _validate_child_parent_map(
        payload.get("child_parent_map"),
        agents=cast(list[str], agents),
        control_agent=control_agent,
        present=child_parent_map_present,
    )
    if (
        not isinstance(pairs, list)
        or not pairs
        or not all(isinstance(pair, str) and pair for pair in pairs)
        or len(set(pairs)) != len(pairs)
    ):
        raise ValueError("launch manifest planned_pairs is invalid")
    if payload.get("pair_count") != len(pairs):
        raise ValueError("launch manifest pair_count is inconsistent")
    expected_count = len(agents) * len(pairs)
    if payload.get("job_count") != expected_count:
        raise ValueError("launch manifest job_count is inconsistent with agents x pairs")
    if not isinstance(schedule, list) or len(schedule) != expected_count:
        raise ValueError("launch manifest planned schedule is incomplete")
    if not isinstance(jobs, list) or len(jobs) != expected_count:
        raise ValueError("launch manifest jobs are incomplete")

    expected_matrix = {(pair, agent) for pair in pairs for agent in agents}
    scheduled_matrix: set[tuple[str, str]] = set()
    schedule_by_ordinal: dict[int, tuple[str, str]] = {}
    for expected_ordinal, entry in enumerate(schedule, start=1):
        if not isinstance(entry, dict):
            raise ValueError("launch manifest planned schedule entry is invalid")
        ordinal = entry.get("schedule_ordinal")
        identity = (entry.get("pair_id"), entry.get("agent"))
        if ordinal != expected_ordinal or not all(isinstance(value, str) for value in identity):
            raise ValueError("launch manifest planned schedule order is invalid")
        normalized_identity = cast(tuple[str, str], identity)
        if normalized_identity in scheduled_matrix:
            raise ValueError("launch manifest planned schedule contains duplicates")
        scheduled_matrix.add(normalized_identity)
        schedule_by_ordinal[expected_ordinal] = normalized_identity
    if scheduled_matrix != expected_matrix:
        raise ValueError("launch manifest planned agents x pairs matrix is incomplete")
    for pair in pairs:
        if (pair, control_agent) not in scheduled_matrix:
            raise ValueError(f"launch manifest pair {pair!r} lacks its control")

    artifact_by_path = {artifact.path: artifact for artifact in frozen_artifacts}
    if len(artifact_by_path) != len(frozen_artifacts):
        raise ValueError("supplied artifacts are not unique")
    job_paths: set[Path] = set()
    jobs_by_path: dict[Path, dict[str, Any]] = {}
    job_matrix: set[tuple[str, str]] = set()
    frozen_at_ns = _timestamp_ns(payload, "artifact_frozen_at_ns", "launch manifest")
    if frozen_at_ns > manifest.mtime_ns:
        raise ValueError("launch manifest was written before its artifact freeze timestamp")
    earliest_start: int | None = None
    latest_snapshot: int | None = None
    starts: list[int] = []
    pair_job_windows: dict[str, list[tuple[int, int]]] = defaultdict(list)

    for entry in jobs:
        if not isinstance(entry, dict):
            raise ValueError("launch manifest job entry is invalid")
        ordinal = entry.get("schedule_ordinal")
        if (
            isinstance(ordinal, bool)
            or not isinstance(ordinal, int)
            or ordinal not in schedule_by_ordinal
        ):
            raise ValueError("launch manifest job schedule ordinal is invalid")
        pair_id = entry.get("pair_id")
        agent = entry.get("agent")
        if (pair_id, agent) != schedule_by_ordinal[ordinal]:
            raise ValueError("launch manifest job identity disagrees with its schedule")
        identity = cast(tuple[str, str], (pair_id, agent))
        if identity in job_matrix:
            raise ValueError("launch manifest jobs contain duplicate identities")
        job_matrix.add(identity)
        returncode = entry.get("returncode")
        if isinstance(returncode, bool) or not isinstance(returncode, int) or returncode != 0:
            raise ValueError(f"launch job {pair_id}/{agent} did not exit successfully")

        artifact_value = entry.get("artifact")
        if not isinstance(artifact_value, str) or not artifact_value:
            raise ValueError("launch manifest job artifact path is invalid")
        artifact_path = Path(artifact_value).resolve()
        if artifact_path in job_paths:
            raise ValueError("launch manifest reuses one artifact path")
        job_paths.add(artifact_path)
        frozen = artifact_by_path.get(artifact_path)
        if frozen is None:
            raise ValueError(f"launch manifest artifact missing from scorer input: {artifact_path}")
        snapshot = entry.get("artifact_snapshot")
        if not isinstance(snapshot, dict) or snapshot.get("exists") is not True:
            raise ValueError(f"launch job {pair_id}/{agent} lacks a completed artifact snapshot")
        expected_snapshot = {
            "sha256": frozen.sha256,
            "size_bytes": frozen.size_bytes,
            "mtime_ns": frozen.mtime_ns,
        }
        if any(snapshot.get(key) != value for key, value in expected_snapshot.items()):
            raise ValueError(f"launch manifest artifact snapshot mismatch: {artifact_path}")

        popen_started = _timestamp_ns(entry, "popen_started_at_ns", f"job {pair_id}/{agent}")
        child_exited = _timestamp_ns(entry, "child_exited_at_ns", f"job {pair_id}/{agent}")
        snapshot_at = _timestamp_ns(entry, "artifact_snapshot_at_ns", f"job {pair_id}/{agent}")
        if not popen_started <= child_exited <= snapshot_at <= frozen_at_ns:
            raise ValueError(f"launch job timestamps are inconsistent for {pair_id}/{agent}")
        if frozen.mtime_ns > snapshot_at:
            raise ValueError(f"artifact mtime is later than its launch snapshot: {artifact_path}")
        end_to_end = (snapshot_at - popen_started) / 1_000_000_000
        process_wall = (child_exited - popen_started) / 1_000_000_000
        snapshot_overhead = (snapshot_at - child_exited) / 1_000_000_000
        recorded_process_wall = _nonnegative_number(
            entry.get("process_wall_seconds"),
            field="process_wall_seconds",
            location=f"job {pair_id}/{agent}",
        )
        recorded_wall = _nonnegative_number(
            entry.get("end_to_end_wall_seconds"),
            field="end_to_end_wall_seconds",
            location=f"job {pair_id}/{agent}",
        )
        if abs(recorded_wall - round(end_to_end, 6)) > 1e-9:
            raise ValueError(f"launch wall time is inconsistent for {pair_id}/{agent}")
        recorded_snapshot_overhead = _nonnegative_number(
            entry.get("artifact_snapshot_overhead_seconds"),
            field="artifact_snapshot_overhead_seconds",
            location=f"job {pair_id}/{agent}",
        )
        if (
            abs(recorded_process_wall - round(process_wall, 6)) > 1e-9
            or abs(recorded_snapshot_overhead - round(snapshot_overhead, 6)) > 1e-9
        ):
            raise ValueError(f"launch wall-time components are inconsistent for {pair_id}/{agent}")
        jobs_by_path[artifact_path] = {
            "pair_id": pair_id,
            "agent": agent,
            "schedule_ordinal": ordinal,
            "popen_started_at_ns": popen_started,
            "child_exited_at_ns": child_exited,
            "artifact_snapshot_at_ns": snapshot_at,
            "process_seconds": process_wall,
            "artifact_snapshot_overhead_seconds": snapshot_overhead,
            "end_to_end_seconds": end_to_end,
        }
        starts.append(popen_started)
        pair_job_windows[str(pair_id)].append((popen_started, child_exited))
        earliest_start = (
            popen_started if earliest_start is None else min(earliest_start, popen_started)
        )
        latest_snapshot = (
            snapshot_at if latest_snapshot is None else max(latest_snapshot, snapshot_at)
        )

    if job_matrix != expected_matrix:
        raise ValueError("launch manifest completed jobs matrix is incomplete")
    if job_paths != set(artifact_by_path):
        extras = sorted(str(path) for path in set(artifact_by_path) - job_paths)
        raise ValueError(f"scorer received artifacts not present in launch manifest: {extras}")

    pair_time_windows: list[dict[str, Any]] = []
    for pair_id in pairs:
        windows = pair_job_windows[pair_id]
        latest_pair_start = max(start for start, _exit in windows)
        earliest_pair_exit = min(exit_ for _start, exit_ in windows)
        overlap_ns = earliest_pair_exit - latest_pair_start
        if overlap_ns < 0:
            raise ValueError(
                f"launch pair {pair_id!r} is not contemporaneous: "
                "a planned job exited before every paired job had started"
            )
        pair_time_windows.append(
            {
                "pair_id": pair_id,
                "job_count": len(windows),
                "latest_popen_started_at_ns": latest_pair_start,
                "earliest_child_exited_at_ns": earliest_pair_exit,
                "overlap_seconds": round(overlap_ns / 1_000_000_000, 9),
                "all_jobs_overlap": True,
            }
        )

    assert earliest_start is not None and latest_snapshot is not None
    batch_started = _timestamp_ns(payload, "batch_started_at_ns", "launch manifest")
    batch_completed = _timestamp_ns(payload, "batch_completed_at_ns", "launch manifest")
    if not batch_started <= earliest_start <= latest_snapshot <= frozen_at_ns == batch_completed:
        raise ValueError("launch manifest batch timestamps are inconsistent")
    expected_launch_spread = round((max(starts) - min(starts)) / 1e9, 6)
    expected_batch_makespan = round((batch_completed - batch_started) / 1e9, 6)
    if payload.get("launch_spread_seconds") != expected_launch_spread:
        raise ValueError("launch manifest launch_spread_seconds is inconsistent")
    if payload.get("batch_makespan_seconds") != expected_batch_makespan:
        raise ValueError("launch manifest batch_makespan_seconds is inconsistent")

    return (
        {
            "path": str(manifest.path),
            "sha256": manifest.sha256,
            "size_bytes": manifest.size_bytes,
            "mtime_ns": manifest.mtime_ns,
            "authorization_reference": authorization,
            "planned_agents": list(agents),
            "planned_pairs": list(pairs),
            "child_parent_map_present": child_parent_map_present,
            "child_parent_map": child_parent_map,
            "planned_schedule": list(schedule),
            "job_count": expected_count,
            "artifact_frozen_at_ns": frozen_at_ns,
            "openrouter_base_url": EXPECTED_OPENROUTER_BASE_URL,
            "pair_time_windows": pair_time_windows,
            "batch_timing": {
                "earliest_popen_start_ns": earliest_start,
                "latest_artifact_snapshot_ns": latest_snapshot,
                "launch_spread_seconds": round((max(starts) - min(starts)) / 1e9, 6),
                "batch_end_to_end_seconds": round((latest_snapshot - earliest_start) / 1e9, 6),
            },
        },
        jobs_by_path,
    )


def _validate_finding_channel(run: dict[str, Any], name: str) -> None:
    location = f"{run.get('pair_key')}/{run.get('agent')} {name}"
    candidates = run.get(name)
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


def _validate_provider_routing(value: object, *, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{location} provider_routing must be an object")
    if value != EXPECTED_STREAMLAKE_ROUTING:
        raise ValueError(f"{location} provider routing must exactly pin streamlake")
    return value


def _validate_prompt(prompt: object, *, location: str) -> dict[str, Any]:
    if not isinstance(prompt, dict):
        raise ValueError(f"prompt is missing in {location}")
    system = prompt.get("system")
    user = prompt.get("user")
    if not isinstance(system, str) or not isinstance(user, str):
        raise ValueError(f"prompt source text is missing in {location}")
    if prompt.get("system_sha256") != _hash_text(system):
        raise ValueError(f"system prompt hash mismatch in {location}")
    if prompt.get("user_sha256") != _hash_text(user):
        raise ValueError(f"user prompt hash mismatch in {location}")
    return prompt


def _validate_focused_exact_prompt(
    prompt: dict[str, Any],
    *,
    protocol_version: str,
    location: str,
) -> None:
    if protocol_version != PROTOCOL_GITNEXUS_FOCUSED_EXACT:
        return
    expected_protocol = GITNEXUS_FOCUSED_EXACT_PROTOCOL_VERSION
    if expected_protocol != PROTOCOL_GITNEXUS_FOCUSED_EXACT:
        raise ValueError("focused-exact runner/runtime protocol constants disagree")
    if (
        len(GITNEXUS_FOCUSED_EXACT_SYSTEM_PROMPT) != 2048
        or _hash_text(GITNEXUS_FOCUSED_EXACT_SYSTEM_PROMPT)
        != GITNEXUS_FOCUSED_EXACT_SYSTEM_PROMPT_SHA256
    ):
        raise ValueError("frozen focused-exact system prompt source changed")
    if (
        prompt.get("system") != GITNEXUS_FOCUSED_EXACT_SYSTEM_PROMPT
        or prompt.get("system_sha256")
        != GITNEXUS_FOCUSED_EXACT_SYSTEM_PROMPT_SHA256
    ):
        raise ValueError(f"focused-exact system prompt is not byte-exact in {location}")


def _validate_agent_protocol(agent: str, protocol_version: str, *, location: str) -> None:
    """Bind every treatment and contemporaneous control to its protocol family."""

    if agent in NATIVE_PROTOCOL_AGENTS:
        allowed = {PROTOCOL_V3}
    elif agent in GITNEXUS_FIRST_PROTOCOL_AGENTS:
        allowed = {PROTOCOL_V4}
    elif agent in GITNEXUS_STRUCTURED_FIRST_PROTOCOL_AGENTS:
        allowed = {PROTOCOL_V5}
    elif agent in GITNEXUS_FOCUSED_EXACT_PROTOCOL_AGENTS:
        allowed = {PROTOCOL_GITNEXUS_FOCUSED_EXACT}
    else:
        allowed = {PROTOCOL_V1, PROTOCOL_V2}
    if protocol_version not in allowed:
        expected = " or ".join(sorted(allowed))
        raise ValueError(
            f"{agent} is bound to protocol {expected}, not {protocol_version}, in {location}"
        )


def _forced_tool_from_names(names: object) -> str | None:
    if not isinstance(names, (list, tuple, set, frozenset)):
        return None
    selected = [
        tool for tool in (*FORCED_AFTER_DIFF_TOOLS, *INITIAL_FORCED_TOOLS) if tool in names
    ]
    if len(selected) > 1:
        raise ValueError("an Agent menu contains multiple forced-after-diff graph tools")
    return selected[0] if selected else None


def _expected_graph_manipulation(forced_tool: str | bool | None) -> dict[str, Any]:
    if isinstance(forced_tool, bool):
        forced_tool = "graph_context" if forced_tool else None
    return {
        "enabled": forced_tool is not None,
        **EXPECTED_GRAPH_MANIPULATION_COMMON,
        "next_request_tool_choice": forced_tool,
        "forced_request_count": 1 if forced_tool is not None else 0,
    }


def _expected_initial_manipulation(forced_tool: str | None) -> dict[str, Any]:
    return {
        "enabled": forced_tool is not None,
        **EXPECTED_INITIAL_MANIPULATION_COMMON,
        "initial_request_tool_choice": forced_tool,
        "forced_request_count": 1 if forced_tool is not None else 0,
        "auto_restored_after_initial_request": forced_tool is not None,
    }


def _validate_configuration(
    configuration: object,
    *,
    protocol_version: str,
    forced_tool: str | None = None,
    has_graph_tool: bool | None = None,
    location: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(configuration, dict):
        raise ValueError(f"configuration is missing in {location}")
    for limit_name in UNBOUNDED_CONFIGURATION_FIELDS:
        if limit_name not in configuration or configuration[limit_name] is not None:
            raise ValueError(f"experiment limit {limit_name} is active in {location}")
    if configuration.get("conversation_history_trimming") is not False:
        raise ValueError(f"conversation history trimming is active in {location}")
    if not isinstance(configuration.get("model"), dict) or not configuration["model"]:
        raise ValueError(f"model configuration is missing in {location}")
    transport = configuration.get("transport")
    if not isinstance(transport, dict):
        raise ValueError(f"transport configuration is missing in {location}")
    if transport.get("response_body_byte_limit") != EXPECTED_RESPONSE_BODY_BYTE_LIMIT:
        raise ValueError(f"transport response-body safety limit is invalid in {location}")

    normalized = dict(configuration)
    if has_graph_tool is not None:
        legacy_forced_tool = "graph_context" if has_graph_tool else None
        if forced_tool is not None and forced_tool != legacy_forced_tool:
            raise ValueError(f"conflicting graph manipulation inputs in {location}")
        forced_tool = legacy_forced_tool
    if protocol_version == PROTOCOL_V1:
        if "graph_manipulation" in configuration:
            raise ValueError(
                f"v1 configuration unexpectedly contains graph_manipulation in {location}"
            )
    else:
        expected = (
            _expected_initial_manipulation(forced_tool)
            if protocol_version in {PROTOCOL_V4, PROTOCOL_V5}
            else _expected_graph_manipulation(forced_tool)
        )
        if configuration.get("graph_manipulation") != expected:
            raise ValueError(f"graph_manipulation configuration is invalid in {location}")
        # The treatment menu intentionally controls this one field. Removing
        # it makes all remaining stage-wide configuration differences fatal.
        normalized.pop("graph_manipulation")
    return configuration, normalized


def _validate_submission_payload(
    payload: object,
    *,
    protocol_version: str,
) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ValueError("submission payload must be an object")
    candidate = dict(payload)
    if protocol_version == PROTOCOL_V1 and "findings" not in candidate:
        candidate["findings"] = []
    validated = EvalSubmission.model_validate(candidate).model_dump(mode="json")
    return cast(list[dict[str, Any]], validated["findings"])


def _validate_tool_metadata(
    tools: object,
    *,
    agent: str,
    protocol_version: str,
    location: str,
) -> tuple[tuple[str, ...], str, tuple[str, ...], str]:
    if not isinstance(tools, dict):
        raise ValueError(f"tool metadata is missing in {location}")
    names_value = tools.get("names")
    base_names_value = tools.get("base_names")
    if (
        not isinstance(names_value, list)
        or not names_value
        or not all(isinstance(name, str) and name for name in names_value)
        or len(set(names_value)) != len(names_value)
    ):
        raise ValueError(f"tool menu is invalid in {location}")
    expected_optional = AGENT_OPTIONAL_TOOL_WHITELIST.get(agent)
    if expected_optional is None:
        raise ValueError(f"unknown portfolio agent {agent!r} in {location}")
    expected_names = (*BASE_TOOLS, *expected_optional)
    if tuple(names_value) != expected_names:
        raise ValueError(f"{agent} tool menu violates its optional-tool whitelist")
    if not isinstance(base_names_value, list) or tuple(base_names_value) != BASE_TOOLS:
        raise ValueError(f"base tool declaration is invalid in {location}")

    definitions = tools.get("definitions")
    if not isinstance(definitions, list) or len(definitions) != len(expected_names):
        raise ValueError(f"complete tool definitions are missing in {location}")
    definition_names: list[str] = []
    for definition in definitions:
        if not isinstance(definition, dict):
            raise ValueError(f"tool definition is invalid in {location}")
        function = definition.get("function")
        name = function.get("name") if isinstance(function, dict) else None
        if not isinstance(name, str):
            raise ValueError(f"tool definition name is invalid in {location}")
        definition_names.append(name)
    if tuple(definition_names) != expected_names:
        raise ValueError(f"tool definition order/menu mismatch in {location}")
    if agent == "codegraph_node_impact_agent" and (
        definitions[-1] != CODEGRAPH_NODE_IMPACT_DEFINITION
    ):
        raise ValueError(f"{agent} tool schema does not match the frozen candidate")
    if agent == "gitnexus_focused_exact_agent" and (
        definitions[-1] != GITNEXUS_FOCUSED_EXACT_DEFINITION
    ):
        raise ValueError(f"{agent} tool schema does not match the frozen candidate")
    submit_definition = definitions[expected_names.index("submit")]
    submit_function = submit_definition.get("function")
    submit_parameters = (
        submit_function.get("parameters") if isinstance(submit_function, dict) else None
    )
    if not isinstance(submit_parameters, dict):
        raise ValueError(f"submit tool parameters are invalid in {location}")
    required = submit_parameters.get("required", [])
    if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
        raise ValueError(f"submit tool required fields are invalid in {location}")
    findings_required = "findings" in required
    if findings_required != (
        protocol_version
        in {
            PROTOCOL_V2,
            PROTOCOL_V3,
            PROTOCOL_V4,
            PROTOCOL_V5,
            PROTOCOL_GITNEXUS_FOCUSED_EXACT,
        }
    ):
        raise ValueError(f"submit tool findings requirement disagrees with protocol in {location}")

    rendered = _canonical_json(definitions)
    schema_hash = _hash_json(definitions)
    base_definitions = definitions[: len(BASE_TOOLS)]
    base_schema_hash = _hash_json(base_definitions)
    if tools.get("schema_sha256") != schema_hash:
        raise ValueError(f"complete tool-definition hash mismatch in {location}")
    if tools.get("base_schema_sha256") != base_schema_hash:
        raise ValueError(f"base tool-definition hash mismatch in {location}")
    if tools.get("canonical_schema_chars") != len(rendered):
        raise ValueError(f"tool-definition character count mismatch in {location}")

    schemas = tools.get("schemas")
    if not isinstance(schemas, list) or len(schemas) != len(definitions):
        raise ValueError(f"per-tool schema manifest is missing in {location}")
    for definition, schema in zip(definitions, schemas, strict=True):
        if not isinstance(schema, dict):
            raise ValueError(f"per-tool schema entry is invalid in {location}")
        definition_rendered = _canonical_json(definition)
        expected = {
            "name": definition["function"]["name"],
            "sha256": _hash_json(definition),
            "canonical_chars": len(definition_rendered),
        }
        if schema != expected:
            raise ValueError(f"per-tool schema hash mismatch in {location}")
    return expected_names, schema_hash, BASE_TOOLS, base_schema_hash


def _validate_usage_and_timing(run: dict[str, Any], *, location: str) -> None:
    usage = run.get("usage")
    if not isinstance(usage, dict):
        raise ValueError(f"{location} usage is missing")
    integer_fields = ("model_calls", "tool_calls", "input_tokens", "output_tokens")
    for field in integer_fields:
        _nonnegative_int(usage.get(field), field=f"usage.{field}", location=location)
    _nonnegative_number(
        usage.get("estimated_cost_usd"),
        field="usage.estimated_cost_usd",
        location=location,
    )

    timing = run.get("timing")
    if not isinstance(timing, dict):
        raise ValueError(f"{location} timing is missing")
    seconds = {
        field: _nonnegative_number(timing.get(field), field=f"timing.{field}", location=location)
        for field in ("setup_seconds", "agent_seconds", "cleanup_seconds", "total_seconds")
    }
    phase_sum = seconds["setup_seconds"] + seconds["agent_seconds"] + seconds["cleanup_seconds"]
    # Each value is rounded independently to milliseconds by the runner.
    if phase_sum > seconds["total_seconds"] + 0.01:
        raise ValueError(f"{location} phase timings exceed total_seconds")


def _repository_wide_trace_scan(entry: dict[str, Any]) -> bool:
    arguments = entry.get("arguments")
    arguments = arguments if isinstance(arguments, dict) else {}
    if entry.get("name") == "grep":
        # A cursor resumes an already-recorded query.  Its initial call is the
        # one that determines whether the scan was repository-wide.
        if str(arguments.get("cursor") or "").strip():
            return False
        glob = str(arguments.get("glob") or "").strip()
        while glob.startswith("./"):
            glob = glob[2:]
        return glob in {"", "*", "**"} or glob.startswith("**/")
    if entry.get("name") == "list_dir":
        if str(arguments.get("cursor") or "").strip():
            return False
        path = str(arguments.get("path") or ".").strip()
        return path in {"", ".", "./"}
    return False


def _validate_v2_manipulation(run: dict[str, Any], *, location: str) -> None:
    """Bind the one forced graph request across every frozen v2/v3 ledger."""

    conversation = cast(dict[str, Any], run["conversation"])
    turn_trace = cast(list[dict[str, Any]], conversation["turn_trace"])
    model_trace = cast(list[dict[str, Any]], conversation["model_call_trace"])
    transport_trace = cast(list[dict[str, Any]], conversation["transport_attempt_trace"])
    manipulation_trace = conversation.get("manipulation_trace")
    if not isinstance(manipulation_trace, list) or not all(
        isinstance(entry, dict) for entry in manipulation_trace
    ):
        raise ValueError(f"{location} v2 manipulation trace is missing")
    if len(turn_trace) != len(model_trace) or len(model_trace) != len(transport_trace):
        raise ValueError(f"{location} v2 manipulation ledgers cannot be aligned")

    forced_tool = _forced_tool_from_names(run["tools"]["names"])
    forced_tool_choice = (
        {"type": "function", "function": {"name": forced_tool}}
        if forced_tool is not None
        else None
    )
    forced_turns: list[int] = []
    for turn, (turn_entry, model_entry, transport_entry) in enumerate(
        zip(turn_trace, model_trace, transport_trace, strict=True),
        start=1,
    ):
        choices = (
            turn_entry.get("tool_choice"),
            model_entry.get("tool_choice"),
            transport_entry.get("tool_choice"),
        )
        if not choices[0] == choices[1] == choices[2]:
            raise ValueError(f"{location} tool_choice ledgers disagree on turn {turn}")
        choice = choices[0]
        if forced_tool_choice is not None and choice == forced_tool_choice:
            forced_turns.append(turn)
            if (
                turn_entry.get("forced_tool") != forced_tool
                or model_entry.get("forced_tool") != forced_tool
            ):
                raise ValueError(f"{location} forced graph request metadata is inconsistent")
            if turn_entry.get("tool_calls") == [] or any(
                name != forced_tool for name in turn_entry.get("tool_calls", [])
            ):
                raise ValueError(f"{location} forced graph response was not graph-only")
        elif choice == "auto":
            if (
                turn_entry.get("forced_tool") is not None
                or model_entry.get("forced_tool") is not None
            ):
                raise ValueError(f"{location} auto request incorrectly records a forced tool")
        else:
            raise ValueError(f"{location} contains an unsupported tool_choice on turn {turn}")

    tool_trace = cast(list[dict[str, Any]], conversation["tool_trace"])
    by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in manipulation_trace:
        event = entry.get("event")
        if not isinstance(event, str) or event not in {
            "forced_tool_request_armed",
            "forced_tool_request_started",
            "forced_tool_request_completed",
            "same_turn_broad_scan_rejected",
        }:
            raise ValueError(f"{location} manipulation trace contains an unsupported event")
        by_event[event].append(entry)
    rejected_event_positions: set[tuple[int, int, str]] = set()
    for event in by_event["same_turn_broad_scan_rejected"]:
        position = (
            event.get("conversation_turn"),
            event.get("ordinal"),
            event.get("tool"),
        )
        if (
            isinstance(position[0], bool)
            or not isinstance(position[0], int)
            or isinstance(position[1], bool)
            or not isinstance(position[1], int)
            or not isinstance(position[2], str)
            or event.get("policy") != "reject_without_execution"
            or position in rejected_event_positions
        ):
            raise ValueError(f"{location} same-turn broad-scan rejection event is invalid")
        matching = [
            entry
            for entry in tool_trace
            if (entry.get("turn"), entry.get("ordinal"), entry.get("name")) == position
        ]
        if (
            len(matching) != 1
            or matching[0].get("error") is not True
            or not _repository_wide_trace_scan(matching[0])
        ):
            raise ValueError(f"{location} broad-scan rejection event lacks its rejected tool trace")
        rejected_event_positions.add(cast(tuple[int, int, str], position))

    force_events = [
        entry
        for entry in manipulation_trace
        if str(entry.get("event", "")).startswith("forced_tool_")
    ]
    if forced_tool is None:
        if forced_turns or manipulation_trace:
            raise ValueError(f"{location} non-graph arm contains forced manipulation")
        return

    successful_diffs = [
        entry
        for entry in tool_trace
        if entry.get("name") == "git_diff" and entry.get("error") is False
    ]
    if not forced_turns:
        # A graph arm may terminate before it ever obtains a usable diff.  That
        # is an attributable Agent/adoption failure, not corrupt stage evidence.
        if force_events:
            raise ValueError(f"{location} graph arm contains a partial forced-tool ledger")
        if successful_diffs:
            raise ValueError(f"{location} successful git_diff lacks its forced graph request")
        return
    if len(forced_turns) != 1:
        raise ValueError(
            f"{location} graph arm must contain exactly one forced tool_choice request"
        )
    forced_turn = forced_turns[0]
    if (
        forced_tool == GITNEXUS_FOCUSED_EXACT_TOOL
        and turn_trace[forced_turn - 1].get("tool_calls") != [forced_tool]
    ):
        raise ValueError(
            f"{location} focused-exact forced response must contain exactly one tool call"
        )
    if forced_turn >= len(turn_trace) or any(
        entry.get("tool_choice") != "auto" for entry in model_trace[forced_turn:]
    ):
        raise ValueError(f"{location} did not restore auto after the forced graph request")

    prior_successful_diffs = [
        entry for entry in successful_diffs if int(entry["turn"]) < forced_turn
    ]
    if not prior_successful_diffs:
        raise ValueError(f"{location} forced graph request lacks a prior successful git_diff")

    first_successful_diff_turn = min(int(entry["turn"]) for entry in prior_successful_diffs)
    for entry in tool_trace:
        if int(entry["turn"]) != first_successful_diff_turn or not _repository_wide_trace_scan(
            entry
        ):
            continue
        position = (int(entry["turn"]), int(entry["ordinal"]), str(entry["name"]))
        if entry.get("error") is not True or position not in rejected_event_positions:
            raise ValueError(
                f"{location} first successful diff turn contains an executed broad scan"
            )
    for event in (
        "forced_tool_request_armed",
        "forced_tool_request_started",
        "forced_tool_request_completed",
    ):
        if len(by_event[event]) != 1:
            raise ValueError(f"{location} must record exactly one {event} event")

    armed = by_event["forced_tool_request_armed"][0]
    started = by_event["forced_tool_request_started"][0]
    completed = by_event["forced_tool_request_completed"][0]
    armed_turn = armed.get("conversation_turn")
    ordinals = armed.get("successful_ordinals")
    if (
        isinstance(armed_turn, bool)
        or not isinstance(armed_turn, int)
        or armed_turn + 1 != forced_turn
        or armed.get("trigger_tool") != "git_diff"
        or armed.get("forced_tool") != forced_tool
        or armed.get("automatic_target_generation") is not False
        or not isinstance(ordinals, list)
        or not ordinals
        or not all(isinstance(value, int) and not isinstance(value, bool) for value in ordinals)
        or not any(
            int(entry["turn"]) == armed_turn and int(entry["ordinal"]) in ordinals
            for entry in prior_successful_diffs
        )
    ):
        raise ValueError(f"{location} forced graph arming event is inconsistent")
    if started != {
        "event": "forced_tool_request_started",
        "conversation_turn": forced_turn,
        "tool": forced_tool,
        "trigger": "prior_successful_git_diff",
    }:
        raise ValueError(f"{location} forced graph start event is inconsistent")
    model_entry = model_trace[forced_turn - 1]
    turn_entry = turn_trace[forced_turn - 1]
    if completed != {
        "event": "forced_tool_request_completed",
        "conversation_turn": forced_turn,
        "tool": forced_tool,
        "request_id": model_entry.get("request_id"),
        "observed_tool_calls": turn_entry.get("tool_calls"),
        "auto_restored_for_next_request": True,
    }:
        raise ValueError(f"{location} forced graph completion event is inconsistent")


def _validate_v4_initial_manipulation(run: dict[str, Any], *, location: str) -> None:
    """Bind the v4 request-one GitNexus call across every frozen ledger."""

    conversation = cast(dict[str, Any], run["conversation"])
    turn_trace = cast(list[dict[str, Any]], conversation["turn_trace"])
    model_trace = cast(list[dict[str, Any]], conversation["model_call_trace"])
    transport_trace = cast(list[dict[str, Any]], conversation["transport_attempt_trace"])
    manipulation_trace = conversation.get("manipulation_trace")
    if not isinstance(manipulation_trace, list) or not all(
        isinstance(entry, dict) for entry in manipulation_trace
    ):
        raise ValueError(f"{location} v4 manipulation trace is missing")
    if len(turn_trace) != len(model_trace) or len(model_trace) != len(transport_trace):
        raise ValueError(f"{location} v4 manipulation ledgers cannot be aligned")

    forced_tool = _forced_tool_from_names(run["tools"]["names"])
    if forced_tool not in {None, "gitnexus_change_impact"}:
        raise ValueError(f"{location} v4 exposes an unsupported initial tool")
    forced_choice = (
        {"type": "function", "function": {"name": forced_tool}}
        if forced_tool is not None
        else None
    )
    for turn, (turn_entry, model_entry, transport_entry) in enumerate(
        zip(turn_trace, model_trace, transport_trace, strict=True),
        start=1,
    ):
        choices = (
            turn_entry.get("tool_choice"),
            model_entry.get("tool_choice"),
            transport_entry.get("tool_choice"),
        )
        if not choices[0] == choices[1] == choices[2]:
            raise ValueError(f"{location} v4 tool_choice ledgers disagree on turn {turn}")
        expected_choice: object = forced_choice if forced_tool is not None and turn == 1 else "auto"
        expected_forced = forced_tool if forced_tool is not None and turn == 1 else None
        if choices[0] != expected_choice:
            raise ValueError(f"{location} v4 has an invalid tool_choice on turn {turn}")
        if (
            turn_entry.get("forced_tool") != expected_forced
            or model_entry.get("forced_tool") != expected_forced
        ):
            raise ValueError(f"{location} v4 forced-tool metadata is inconsistent")

    if forced_tool is None:
        if manipulation_trace:
            raise ValueError(f"{location} v4 control contains forced manipulation")
        return

    if len(turn_trace) < 2:
        raise ValueError(f"{location} v4 did not resume auto after the initial request")
    if turn_trace[0].get("tool_calls") != [forced_tool]:
        raise ValueError(f"{location} v4 initial response was not one GitNexus call")
    tool_trace = cast(list[dict[str, Any]], conversation["tool_trace"])
    graph_calls = [entry for entry in tool_trace if entry.get("name") == forced_tool]
    if len(graph_calls) != 1:
        raise ValueError(f"{location} v4 must execute GitNexus exactly once")
    initial_call = graph_calls[0]
    if (
        initial_call.get("turn") != 1
        or initial_call.get("ordinal") != 1
        or initial_call.get("arguments") != {}
        or initial_call.get("arguments_sha256") != _hash_json({})
        or initial_call.get("error") is not False
    ):
        raise ValueError(f"{location} v4 initial GitNexus trace is invalid")
    if any(
        _repository_wide_trace_scan(entry)
        and (int(entry["turn"]), int(entry["ordinal"])) <= (1, 1)
        for entry in tool_trace
    ):
        raise ValueError(f"{location} v4 broad scan precedes initial GitNexus")

    expected_events = [
        {
            "event": "forced_tool_request_started",
            "conversation_turn": 1,
            "tool": forced_tool,
            "trigger": "initial_request",
        },
        {
            "event": "forced_tool_request_completed",
            "conversation_turn": 1,
            "tool": forced_tool,
            "request_id": model_trace[0].get("request_id"),
            "observed_tool_calls": [forced_tool],
            "auto_restored_for_next_request": True,
        },
    ]
    if manipulation_trace != expected_events:
        raise ValueError(f"{location} v4 initial manipulation events are inconsistent")

    fixed_arguments = {
        "scope": "compare",
        "base_ref": run.get("baseline_revision"),
        "limit": 500,
    }
    setup = run.get("setup")
    if (
        not isinstance(setup, dict)
        or setup.get("retrieval_profile")
        != "gitnexus-compact-cli-detect-changes"
        or setup.get("provider_surface") != "gitnexus-cli"
        or setup.get("output_contract")
        != (
            "complete-sanitized-native-cli-response; the CLI formatter may display "
            "only a subset of changed symbols or affected flows"
        )
        or setup.get("fixed_provider_arguments") != fixed_arguments
        or not isinstance(setup.get("query_calls"), list)
        or len(setup["query_calls"]) != 1
        or not isinstance(setup.get("provider_calls"), list)
        or len(setup["provider_calls"]) != 1
    ):
        raise ValueError(f"{location} v4 fixed GitNexus setup ledger is invalid")
    bound = _native_setup_call(run, initial_call, tool=forced_tool)
    markers = initial_call.get("native_graph_markers")
    if (
        bound is None
        or bound[0].get("arguments") != {}
        or bound[0].get("provider_arguments") != fixed_arguments
        or bound[1].get("arguments") != fixed_arguments
        or any(
            entry.get("scope") != "compare"
            or entry.get("base_ref") != run.get("baseline_revision")
            or entry.get("limit") != 500
            for entry in bound
        )
        or not isinstance(markers, dict)
        or markers.get("gitnexus_changed_symbols") is not True
        or markers.get("gitnexus_affected_flows") is not True
    ):
        raise ValueError(f"{location} v4 GitNexus provider/result binding is invalid")


def _validate_v5_structured_initial_manipulation(
    run: dict[str, Any],
    *,
    location: str,
) -> None:
    """Validate request-one official structured retrieval before GT is opened."""

    conversation = cast(dict[str, Any], run["conversation"])
    turn_trace = cast(list[dict[str, Any]], conversation["turn_trace"])
    model_trace = cast(list[dict[str, Any]], conversation["model_call_trace"])
    transport_trace = cast(list[dict[str, Any]], conversation["transport_attempt_trace"])
    manipulation_trace = conversation.get("manipulation_trace")
    if not isinstance(manipulation_trace, list) or not all(
        isinstance(entry, dict) for entry in manipulation_trace
    ):
        raise ValueError(f"{location} v5 manipulation trace is missing")
    if len(turn_trace) != len(model_trace) or len(model_trace) != len(transport_trace):
        raise ValueError(f"{location} v5 manipulation ledgers cannot be aligned")

    forced_tool = _forced_tool_from_names(run["tools"]["names"])
    if forced_tool not in {None, "gitnexus_structured_change"}:
        raise ValueError(f"{location} v5 exposes an unsupported initial tool")
    forced_choice = (
        {"type": "function", "function": {"name": forced_tool}}
        if forced_tool is not None
        else None
    )
    for turn, (turn_entry, model_entry, transport_entry) in enumerate(
        zip(turn_trace, model_trace, transport_trace, strict=True),
        start=1,
    ):
        choices = (
            turn_entry.get("tool_choice"),
            model_entry.get("tool_choice"),
            transport_entry.get("tool_choice"),
        )
        if not choices[0] == choices[1] == choices[2]:
            raise ValueError(f"{location} v5 tool_choice ledgers disagree on turn {turn}")
        expected_choice: object = forced_choice if forced_tool is not None and turn == 1 else "auto"
        expected_forced = forced_tool if forced_tool is not None and turn == 1 else None
        if choices[0] != expected_choice:
            raise ValueError(f"{location} v5 has an invalid tool_choice on turn {turn}")
        if (
            turn_entry.get("forced_tool") != expected_forced
            or model_entry.get("forced_tool") != expected_forced
        ):
            raise ValueError(f"{location} v5 forced-tool metadata is inconsistent")

    if forced_tool is None:
        if manipulation_trace:
            raise ValueError(f"{location} v5 control contains forced manipulation")
        return

    if len(turn_trace) < 2:
        raise ValueError(f"{location} v5 did not resume auto after the initial request")
    if turn_trace[0].get("tool_calls") != [forced_tool]:
        raise ValueError(f"{location} v5 initial response was not one structured call")
    tool_trace = cast(list[dict[str, Any]], conversation["tool_trace"])
    structured_calls = [entry for entry in tool_trace if entry.get("name") == forced_tool]
    if len(structured_calls) != 1:
        raise ValueError(f"{location} v5 must execute structured retrieval exactly once")
    initial_call = structured_calls[0]
    if (
        initial_call.get("turn") != 1
        or initial_call.get("ordinal") != 1
        or initial_call.get("arguments") != {}
        or initial_call.get("arguments_sha256") != _hash_json({})
        or initial_call.get("error") is not False
    ):
        raise ValueError(f"{location} v5 initial structured trace is invalid")

    expected_events = [
        {
            "event": "forced_tool_request_started",
            "conversation_turn": 1,
            "tool": forced_tool,
            "trigger": "initial_request",
        },
        {
            "event": "forced_tool_request_completed",
            "conversation_turn": 1,
            "tool": forced_tool,
            "request_id": model_trace[0].get("request_id"),
            "observed_tool_calls": [forced_tool],
            "auto_restored_for_next_request": True,
        },
    ]
    if manipulation_trace != expected_events:
        raise ValueError(f"{location} v5 initial manipulation events are inconsistent")

    fixed_arguments = {
        "scope": "compare",
        "base_ref": run.get("baseline_revision"),
    }
    setup = run.get("setup")
    expected_output_transport = {
        "complete_provider_output": True,
        "structured_json": True,
        "wrapper_truncation": False,
        "projection": False,
        "sanitization": "isolated_clone_path_only",
    }
    if (
        not isinstance(setup, dict)
        or setup.get("profile_id") != "gitnexus_official_structured_change"
        or setup.get("provider") != "gitnexus"
        or setup.get("package_version") != "1.6.9"
        or setup.get("implementation_mode")
        != "official-local-backend-structured-detect-changes"
        or setup.get("official_backend_export") != "LocalBackend"
        or setup.get("backend_module") != "dist/mcp/local/local-backend.js"
        or not _valid_sha256(setup.get("binary_sha256"))
        or not _valid_sha256(setup.get("backend_module_sha256"))
        or not _valid_sha256(setup.get("bridge_sha256"))
        or setup.get("fixed_provider_arguments") != fixed_arguments
        or set(fixed_arguments) != {"scope", "base_ref"}
        or setup.get("runtime_bindings") != {"repo": "isolated_index_clone"}
        or setup.get("model_controlled_provider_arguments") != []
        or "provider_limit" not in setup
        or setup.get("provider_limit") is not None
        or setup.get("cli_formatter_used") is not False
        or setup.get("output_transport") != expected_output_transport
        or setup.get("source_head") != run.get("head_revision")
        or not isinstance(setup.get("index_stats"), dict)
        or setup["index_stats"].get("lastCommit") != run.get("head_revision")
        or not isinstance(setup.get("query_calls"), list)
        or len(setup["query_calls"]) != 1
        or not isinstance(setup.get("provider_calls"), list)
        or len(setup["provider_calls"]) != 1
    ):
        raise ValueError(f"{location} v5 official structured setup metadata is invalid")
    setup_calls = setup.get("setup_calls")
    if (
        not isinstance(setup_calls, list)
        or len(setup_calls) != 1
        or not isinstance(setup_calls[0], dict)
        or setup_calls[0].get("operation") != "analyze"
        or setup_calls[0].get("exit_code") != 0
        or setup_calls[0].get("error") is not False
    ):
        raise ValueError(f"{location} v5 setup must contain only successful indexing")

    bound = _structured_setup_call(run, initial_call)
    metrics = initial_call.get("gitnexus_structured_result")
    if (
        bound is None
        or bound[0].get("exit_code") != 0
        or bound[0].get("error") is not False
        or bound[0].get("structured_json") is not True
        or bound[0].get("provider_error") is not False
        or bound[0].get("partial") is not False
        or bound[0].get("partial_value_valid") is not True
        or bound[0].get("summary_counts_match_arrays") is not True
        or not isinstance(metrics, dict)
    ):
        raise ValueError(f"{location} v5 structured provider/result binding is invalid")
    for count_field in (
        "changed_symbols_count",
        "affected_processes_count",
        "summary_changed_count",
        "summary_affected_count",
    ):
        count = bound[0].get(count_field)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError(f"{location} v5 structured result count is invalid")
    if (
        bound[0]["changed_symbols_count"] != bound[0]["summary_changed_count"]
        or bound[0]["affected_processes_count"]
        != bound[0]["summary_affected_count"]
    ):
        raise ValueError(f"{location} v5 summary counts disagree with result arrays")


def _validate_conversation(
    run: dict[str, Any],
    allowed_names: set[str],
    *,
    protocol_version: str,
) -> None:
    """Validate traces and distinguish attributable Agent outcomes from infra failure."""

    location = f"{run.get('pair_key')}/{run.get('agent')}"
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
    traced_calls: list[tuple[int, int, str]] = []
    for expected_turn, entry in enumerate(trace, start=1):
        if not isinstance(entry, dict) or entry.get("turn") != expected_turn:
            raise ValueError(f"{location} has a non-contiguous turn trace")
        names = entry.get("tool_calls")
        if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
            raise ValueError(f"{location} has invalid trace tool calls")
        for ordinal, name in enumerate(names, start=1):
            traced_calls.append((expected_turn, ordinal, name))
    traced_names = [name for _turn, _ordinal, name in traced_calls]
    if not set(traced_names) <= allowed_names:
        raise ValueError(f"{location} trace contains a tool outside its frozen menu")
    if "handoff" in traced_names:
        raise ValueError(f"{location} contains a forbidden handoff")

    if ok or failure == "submit_schema_invalid":
        if traced_names.count("submit") != 1 or trace[-1].get("tool_calls") != ["submit"]:
            raise ValueError(f"{location} must end at a terminal, solitary submit call")
    elif failure == "submit_not_solitary":
        final_calls = trace[-1].get("tool_calls")
        if (
            traced_names.count("submit") != 1
            or not isinstance(final_calls, list)
            or "submit" not in final_calls
            or len(final_calls) < 2
        ):
            raise ValueError(f"{location} submit_not_solitary trace is invalid")
    elif failure == "no_tool_call":
        if "submit" in traced_names or trace[-1].get("tool_calls") != []:
            raise ValueError(f"{location} no_tool_call failure has an invalid final turn")

    tool_counts = conversation.get("tool_counts")
    if not isinstance(tool_counts, dict):
        raise ValueError(f"{location} tool counts are missing")
    normalized_counts: dict[str, int] = {}
    for name, count in tool_counts.items():
        normalized = _nonnegative_int(
            count,
            field=f"tool_counts.{name}",
            location=location,
        )
        if normalized == 0:
            raise ValueError(f"{location} tool_counts must omit zero-count tools")
        normalized_counts[str(name)] = normalized
    if Counter(traced_names) != Counter(normalized_counts):
        raise ValueError(f"{location} tool counts do not match its turn trace")
    conversation_tool_calls = _nonnegative_int(
        conversation.get("tool_calls"),
        field="conversation.tool_calls",
        location=location,
    )
    if conversation_tool_calls != len(traced_names):
        raise ValueError(f"{location} conversation tool-call total is inconsistent")

    tool_trace = conversation.get("tool_trace")
    if not isinstance(tool_trace, list) or len(tool_trace) != len(traced_calls):
        raise ValueError(f"{location} tool trace does not cover every tool call")
    for expected, entry in zip(traced_calls, tool_trace, strict=True):
        if not isinstance(entry, dict):
            raise ValueError(f"{location} has an invalid tool-trace entry")
        observed = (entry.get("turn"), entry.get("ordinal"), entry.get("name"))
        if observed != expected:
            raise ValueError(f"{location} tool trace disagrees with its turn trace")
        if expected[2] != "submit" and not isinstance(entry.get("error"), bool):
            raise ValueError(f"{location} non-submit tool trace needs an error flag")
    if ok or failure == "submit_schema_invalid":
        if tool_trace[-1].get("terminal") is not True:
            raise ValueError(f"{location} submit trace is not terminal")
    elif failure == "submit_not_solitary":
        submit_traces = [entry for entry in tool_trace if entry.get("name") == "submit"]
        if len(submit_traces) != 1 or submit_traces[0].get("terminal") is not True:
            raise ValueError(f"{location} rejected submit trace is not terminal")

    usage = run["usage"]
    if usage.get("tool_calls") != len(traced_names):
        raise ValueError(f"{location} usage tool-call total is inconsistent")
    # The ledger counts transport retry attempts; the conversation trace counts
    # completed model turns.  A successful retry can therefore make this value
    # larger than ``turns`` without being an external stage failure.
    if int(usage["model_calls"]) < turns:
        raise ValueError(f"{location} model-call total is inconsistent")
    model_trace = conversation.get("model_call_trace")
    if not isinstance(model_trace, list) or len(model_trace) != turns:
        raise ValueError(f"{location} model trace does not cover every turn")
    traced_input = 0
    traced_output = 0
    for expected_turn, entry in enumerate(model_trace, start=1):
        if not isinstance(entry, dict) or entry.get("conversation_turn") != expected_turn:
            raise ValueError(f"{location} model trace is invalid")
        if entry.get("actual_model") != actual_models[0]:
            raise ValueError(f"{location} model trace disagrees on actual model")
        traced_input += _nonnegative_int(
            entry.get("input_tokens"), field="model_trace.input_tokens", location=location
        )
        traced_output += _nonnegative_int(
            entry.get("output_tokens"), field="model_trace.output_tokens", location=location
        )
        for optional_field in ("cached_tokens", "reasoning_tokens"):
            optional_value = entry.get(optional_field)
            if optional_value is not None:
                _nonnegative_int(
                    optional_value,
                    field=f"model_trace.{optional_field}",
                    location=location,
                )
    attempt_trace = conversation.get("transport_attempt_trace")
    if not isinstance(attempt_trace, list) or len(attempt_trace) != int(usage["model_calls"]):
        raise ValueError(f"{location} transport-attempt trace is incomplete")
    successful_attempts = 0
    for entry in attempt_trace:
        if not isinstance(entry, dict):
            raise ValueError(f"{location} transport-attempt trace is invalid")
        _nonnegative_number(
            entry.get("seconds"), field="transport_attempt.seconds", location=location
        )
        _nonnegative_int(
            entry.get("input_tokens"),
            field="transport_attempt.input_tokens",
            location=location,
        )
        _nonnegative_int(
            entry.get("output_tokens"),
            field="transport_attempt.output_tokens",
            location=location,
        )
        status = entry.get("status")
        if status == "success":
            successful_attempts += 1
        elif status == "error":
            raise ValueError(
                f"transport retry/failure invalidates the stage in {location}: "
                f"{entry.get('failure_reason') or 'unknown'}"
            )
        else:
            raise ValueError(f"{location} transport-attempt status is invalid")
    if successful_attempts != turns:
        raise ValueError(f"{location} transport attempts do not match completed turns")
    for expected_turn, (model_entry, transport_entry) in enumerate(
        zip(model_trace, attempt_trace, strict=True),
        start=1,
    ):
        for field in (
            "request_id",
            "actual_model",
            "input_tokens",
            "output_tokens",
        ):
            if model_entry.get(field) != transport_entry.get(field):
                raise ValueError(
                    f"{location} model/transport {field} mismatch on turn {expected_turn}"
                )
    if (
        int(usage["input_tokens"]) != traced_input
        or int(usage["output_tokens"]) != traced_output
    ):
        raise ValueError(f"{location} usage tokens do not equal completed model turns")
    if protocol_version in {
        PROTOCOL_V2,
        PROTOCOL_V3,
        PROTOCOL_GITNEXUS_FOCUSED_EXACT,
    }:
        _validate_v2_manipulation(run, location=location)
    elif protocol_version == PROTOCOL_V4:
        _validate_v4_initial_manipulation(run, location=location)
    elif protocol_version == PROTOCOL_V5:
        _validate_v5_structured_initial_manipulation(run, location=location)

    raw_submit = run.get("raw_submit")
    if ok:
        if not isinstance(raw_submit, dict):
            raise ValueError(f"{location} valid terminal run lacks its raw submit")
        try:
            findings = _validate_submission_payload(
                raw_submit,
                protocol_version=protocol_version,
            )
        except ValueError as error:
            raise ValueError(f"{location} valid terminal run has invalid submit data") from error
        if run.get("store") != [] or run.get("submission_only") != findings:
            raise ValueError(f"{location} violates direct-delivery isolation")
        if run.get("delivered") != findings:
            raise ValueError(f"{location} delivered findings differ from its submit")
    elif failure in {"submit_schema_invalid", "submit_not_solitary"}:
        if not isinstance(raw_submit, dict):
            raise ValueError(f"{location} rejected-submit run lacks its raw payload")
        if failure == "submit_schema_invalid":
            try:
                _validate_submission_payload(raw_submit, protocol_version=protocol_version)
            except ValueError:
                pass
            else:
                raise ValueError(
                    f"{location} submit_schema_invalid payload is valid under {protocol_version}"
                )
    elif raw_submit is not None:
        raise ValueError(f"{location} no_tool_call run unexpectedly has a raw submit")

    if not ok:
        for channel_name in ("submission_only", "store", "delivered"):
            if run.get(channel_name) != []:
                raise ValueError(
                    f"Agent failure must score an empty {channel_name} channel in {location}"
                )

    if (
        protocol_version
        in {
            PROTOCOL_V2,
            PROTOCOL_V3,
            PROTOCOL_V4,
            PROTOCOL_V5,
            PROTOCOL_GITNEXUS_FOCUSED_EXACT,
        }
        and raw_submit is not None
    ):
        findings_present = "findings" in raw_submit
        findings_value = raw_submit.get("findings")
        expected_shape = {
            "findings_field_present": findings_present,
            "findings_is_list": isinstance(findings_value, list),
            "finding_count": len(findings_value) if isinstance(findings_value, list) else None,
            "implicit_empty": not findings_present,
            "explicit_empty": (
                findings_present and isinstance(findings_value, list) and not findings_value
            ),
        }
        if conversation.get("submit_shape") != expected_shape:
            raise ValueError(f"{location} v2 submit_shape is missing or inconsistent")


def _target_is_present_in_diff(target: str, content: str) -> bool:
    """Match a requested graph target literally in the diff page the Agent saw."""

    if not target:
        return False
    # Symbol targets need identifier boundaries so a request for ``State`` is
    # not credited by an unrelated ``StateMachine`` declaration. Paths and
    # qualified targets retain exact literal matching, including separators.
    if re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", target):
        return (
            re.search(
                rf"(?<![A-Za-z0-9_$]){re.escape(target)}(?![A-Za-z0-9_$])",
                content,
            )
            is not None
        )
    return target in content


def _replay_successful_git_diffs(
    run: dict[str, Any],
    *,
    repo_path: Path,
) -> tuple[list[ReplayedDiff], int]:
    """Replay and authenticate Agent-visible git_diff pages from a frozen run.

    The trace stores arguments and result metadata while the runtime setup
    ledger stores a SHA-256 for the exact returned string. Replaying against
    the already preflighted baseline/HEAD repository lets adoption depend on
    the content the Agent actually received without persisting repository
    source in the score report. Any missing or inconsistent evidence is kept
    out of the returned list (fail closed).
    """

    trace = run.get("conversation", {}).get("tool_trace")
    setup = run.get("setup")
    if not isinstance(trace, list) or not isinstance(setup, dict):
        return [], 0
    traced_diffs = [entry for entry in trace if entry.get("name") == "git_diff"]
    if not traced_diffs:
        return [], 0
    handler_calls = setup.get("handler_calls")
    if not isinstance(handler_calls, list):
        return [], len(traced_diffs)
    frozen_diffs = [
        entry
        for entry in handler_calls
        if isinstance(entry, dict) and entry.get("tool") == "git_diff"
    ]
    if len(frozen_diffs) != len(traced_diffs):
        return [], len(traced_diffs)

    context = AgentContext(
        repo_path=repo_path,
        baseline_revision=str(run.get("baseline_revision", "")),
        head_revision=str(run.get("head_revision", "")),
    )
    runtime = paged_generic_runtime(context)
    handler = runtime.extra_tools["git_diff"][1]
    replayed: list[ReplayedDiff] = []
    failures = 0
    for traced, frozen_call in zip(traced_diffs, frozen_diffs, strict=True):
        arguments = traced.get("arguments")
        if not isinstance(arguments, dict):
            failures += 1
            continue
        try:
            content = str(handler(arguments))
        except (OSError, ValueError):
            failures += 1
            continue
        replay_call = runtime.metadata["handler_calls"][-1]
        replay_trace = _result_trace(content)
        trace_matches = (
            traced.get("error") is False
            and traced.get("arguments_sha256") == _hash_json(arguments)
            and all(traced.get(key) == value for key, value in replay_trace.items())
        )
        frozen_call_matches = (
            frozen_call.get("arguments") == replay_call.get("arguments")
            and frozen_call.get("error") is False
            and replay_call.get("error") is False
            and frozen_call.get("output_chars") == len(content)
            and _valid_sha256(frozen_call.get("output_sha256"))
            and frozen_call.get("output_sha256") == replay_call.get("output_sha256")
            and frozen_call.get("page_envelope") == replay_call.get("page_envelope")
        )
        if not trace_matches or not frozen_call_matches:
            failures += 1
            continue
        replayed.append(
            ReplayedDiff(
                position=(int(traced["turn"]), int(traced["ordinal"])),
                content=content,
            )
        )
    return replayed, failures


def _nonnegative_metric(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) >= 0
    )


def _codegraph_node_impact_setup(
    run: dict[str, Any],
    *,
    repo_path: Path,
) -> dict[str, Any] | None:
    """Authenticate the frozen CodeGraph index and diff guard without GT."""

    setup = run.get("setup")
    if not isinstance(setup, dict):
        return None
    binary_sha256 = setup.get("binary_sha256")
    dependencies = [
        "git",
        "filesystem",
        f"codegraph:{CODEGRAPH_VERSION}:{binary_sha256}",
    ]
    expected_output_transport = {
        "complete_provider_stdout_stderr": True,
        "pagination": False,
        "wrapper_truncation": False,
        "provider_internal_truncation_possible": True,
        "provider_truncation_notices_preserved": True,
        "projection": False,
        "sanitization": "isolated_clone_path_only",
    }
    expected_runtime_composition = {
        "isolated_clone_count": 1,
        "codegraph_index_count": 1,
        "generic_runtime_count": 1,
        "cleanup_callback_count": 1,
        "provider_queries_share_index": True,
    }
    expected_file_disambiguation = {
        "node": True,
        "impact": False,
        "impact_reason": "CodeGraph 1.5.0 CLI impact has no --file option",
    }
    setup_calls = setup.get("setup_calls")
    if (
        setup.get("profile_id") != CODEGRAPH_NODE_IMPACT_PROFILE
        or setup.get("provider") != "codegraph"
        or setup.get("package_version") != CODEGRAPH_VERSION
        or not _valid_sha256(binary_sha256)
        or setup.get("package_integrity") != CODEGRAPH_PACKAGE_INTEGRITY
        or setup.get("implementation_mode")
        != "native-node-source-plus-impact-depth3-parallel"
        or setup.get("candidate_protocol") != CODEGRAPH_NODE_IMPACT_PROTOCOL
        or setup.get("impact_depth") != 3
        or setup.get("ordered_path_available") is not False
        or setup.get("file_disambiguation") != expected_file_disambiguation
        or setup.get("isolated") is not True
        or setup.get("source_head") != run.get("head_revision")
        or setup.get("agent_repo_clean") is not True
        or setup.get("agent_repo_graph_dirs_absent") is not True
        or setup.get("index_success") is not True
        or setup.get("installer_used") is not False
        or setup.get("mcp_used") is not False
        or setup.get("prompt_or_hook_injection") is not False
        or setup.get("telemetry_disabled") is not True
        or setup.get("update_checks_disabled") is not True
        or setup.get("base_profile_id") != "paged_generic"
        or setup.get("tool_surface") != ["codegraph_node_impact"]
        or setup.get("dependencies") != dependencies
        or setup.get("dependency_sha256") != _hash_json(dependencies)
        or setup.get("output_transport") != expected_output_transport
        or setup.get("runtime_composition") != expected_runtime_composition
        or not isinstance(setup_calls, list)
        or len(setup_calls) != 2
    ):
        return None
    expected_setup_operations = ("init", "status")
    for record, operation in zip(setup_calls, expected_setup_operations, strict=True):
        if (
            not isinstance(record, dict)
            or record.get("operation") != operation
            or record.get("exit_code") != 0
            or record.get("error") is not False
            or not _nonnegative_metric(record.get("seconds"))
            or not isinstance(record.get("output_chars"), int)
            or isinstance(record.get("output_chars"), bool)
            or int(record["output_chars"]) < 0
            or not _valid_sha256(record.get("output_sha256"))
        ):
            return None

    index_binding = setup.get("index_binding")
    if not isinstance(index_binding, dict):
        return None
    expected_index_binding = {
        "provider": "codegraph",
        "package_version": CODEGRAPH_VERSION,
        "binary_sha256": binary_sha256,
        "source_head": run.get("head_revision"),
        "source_tree": setup.get("source_tree"),
        "isolated_clone_head_matches_source": True,
        "isolated_clone_tree_matches_source": True,
        "index_relative_path": ".codegraph",
        "status_initialized": True,
        "status_version": CODEGRAPH_VERSION,
        "index_state": "complete",
        "index_built_with_version": CODEGRAPH_VERSION,
        "pending_changes": {"added": 0, "modified": 0, "removed": 0},
        "pending_refs": 0,
        "worktree_mismatch": None,
    }
    tree = subprocess.run(
        ["git", "rev-parse", f"{run.get('head_revision', '')}^{{tree}}"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if (
        tree.returncode != 0
        or setup.get("source_tree") != tree.stdout.strip()
        or index_binding != expected_index_binding
        or setup.get("index_binding_sha256") != _hash_json(index_binding)
    ):
        return None

    completed = subprocess.run(
        [
            "git",
            "-c",
            "core.quotePath=false",
            "diff",
            "--no-ext-diff",
            "--no-renames",
            "--no-prefix",
            "--unified=0",
            str(run.get("baseline_revision", "")),
            str(run.get("head_revision", "")),
            "--",
        ],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
    )
    diff_guard = setup.get("diff_symbol_guard")
    if not isinstance(diff_guard, dict):
        return None
    changed_paths = diff_guard.get("changed_source_paths")
    eligible_count = diff_guard.get("eligible_symbol_count")
    if (
        completed.returncode != 0
        or diff_guard.get("source") != "baseline_to_head_changed_source_lines"
        or diff_guard.get("case_sensitive") is not True
        or diff_guard.get("exact_single_identifier_only") is not True
        or diff_guard.get("ground_truth_used") is not False
        or diff_guard.get("diff_sha256") != _hash_text(completed.stdout)
        or not isinstance(eligible_count, int)
        or isinstance(eligible_count, bool)
        or eligible_count < 1
        or not isinstance(changed_paths, list)
        or not changed_paths
        or not all(isinstance(path, str) and path for path in changed_paths)
        or changed_paths != sorted(set(changed_paths))
    ):
        return None
    return setup


def _codegraph_node_impact_setup_call(
    run: dict[str, Any],
    trace_entry: dict[str, Any],
    *,
    repo_path: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | None:
    """Bind one model call to both complete native provider streams."""

    setup = _codegraph_node_impact_setup(run, repo_path=repo_path)
    if setup is None:
        return None
    query_calls = setup.get("query_calls")
    provider_calls = setup.get("provider_calls")
    if (
        not isinstance(query_calls, list)
        or len(query_calls) != 1
        or not isinstance(provider_calls, list)
        or len(provider_calls) != 2
        or not isinstance(query_calls[0], dict)
        or not all(isinstance(call, dict) for call in provider_calls)
    ):
        return None
    query = cast(dict[str, Any], query_calls[0])
    node = cast(dict[str, Any], provider_calls[0])
    impact = cast(dict[str, Any], provider_calls[1])
    raw_arguments = query.get("arguments")
    normalized_arguments = query.get("normalized_arguments")
    if not isinstance(raw_arguments, dict) or not isinstance(normalized_arguments, dict):
        return None
    symbol = normalized_arguments.get("symbol")
    file = normalized_arguments.get("file")
    if (
        set(raw_arguments) - {"symbol", "file"}
        or not isinstance(symbol, str)
        or re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", symbol) is None
        or not isinstance(raw_arguments.get("symbol"), str)
        or str(raw_arguments["symbol"]).strip() != symbol
        or set(normalized_arguments) != ({"symbol", "file"} if file is not None else {"symbol"})
        or (file is not None and (not isinstance(file, str) or not file))
    ):
        return None
    matching_paths = query.get("matching_diff_paths")
    changed_paths = setup["diff_symbol_guard"]["changed_source_paths"]
    if (
        not isinstance(matching_paths, list)
        or not matching_paths
        or matching_paths != sorted(set(matching_paths))
        or not all(isinstance(path, str) and path in changed_paths for path in matching_paths)
        or (file is not None and file not in matching_paths)
    ):
        return None

    trace_result = trace_entry.get("codegraph_node_impact_result")
    if not isinstance(trace_result, dict):
        return None
    expected_semantics = {
        "ordered_path_available": False,
        "ordered_path_notice": (
            "This composite returns exact source/trail plus upstream blast radius, "
            "not an ordered source-to-target call path."
        ),
        "node_file_disambiguated": file is not None,
        "impact_depth": 3,
        "impact_direction": "upstream_dependents",
        "impact_file_disambiguated": False,
        "impact_definition_scope": "all_exact_same_named_definitions",
    }
    expected_transport = {
        "complete_sanitized_stdout_stderr": True,
        "sanitization": "isolated_clone_path_only",
        "wrapper_truncation": False,
        "provider_truncation_notices_preserved": True,
    }
    node_trace = trace_result.get("node_include_source")
    impact_trace = trace_result.get("upstream_impact_depth3")
    markers = trace_entry.get("native_graph_markers")
    if (
        trace_entry.get("name") != "codegraph_node_impact"
        or trace_entry.get("error") is not False
        or trace_entry.get("arguments") != _trace_arguments(
            "codegraph_node_impact", raw_arguments
        )
        or trace_entry.get("arguments_sha256") != _hash_json(raw_arguments)
        or trace_result.get("structured_json") is not True
        or trace_result.get("shape_valid") is not True
        or trace_result.get("protocol") != CODEGRAPH_NODE_IMPACT_PROTOCOL
        or trace_result.get("query") != normalized_arguments
        or trace_result.get("semantics") != expected_semantics
        or trace_result.get("transport") != expected_transport
        or trace_result.get("node_source_marker") is not True
        or trace_result.get("impact_marker") is not True
        or not isinstance(markers, dict)
        or markers.get("codegraph_node_source") is not True
        or markers.get("codegraph_upstream_impact") is not True
        or not isinstance(node_trace, dict)
        or not isinstance(impact_trace, dict)
    ):
        return None

    binding_sha256 = setup.get("index_binding_sha256")
    expected_node_semantics = {
        "symbol": symbol,
        "include_source": True,
        "relationship_scope": "immediate_callers_and_callees",
        **({"file": file} if file is not None else {}),
    }
    expected_node_argv = ["node", "--path", "."]
    if file is not None:
        expected_node_argv.extend(("--file", file))
    expected_node_argv.append(symbol)
    expected_impact_semantics = {
        "symbol": symbol,
        "depth": 3,
        "direction": "upstream_dependents",
        "definition_scope": "all_exact_same_named_definitions",
        "file_disambiguation_applied": False,
    }
    expected_impact_argv = ["impact", "--path", ".", "--depth", "3", symbol]
    expected_provider = (
        (
            node,
            node_trace,
            "node_include_source",
            expected_node_argv,
            expected_node_semantics,
        ),
        (
            impact,
            impact_trace,
            "impact_upstream_depth3",
            expected_impact_argv,
            expected_impact_semantics,
        ),
    )
    stream_fields = (
        "stdout_chars",
        "stdout_sha256",
        "stderr_chars",
        "stderr_sha256",
        "output_chars",
        "output_sha256",
        "exit_code",
        "error",
        "provider_reported_truncation",
    )
    for provider, traced, operation, argv, semantics in expected_provider:
        if (
            provider.get("provider") != "codegraph"
            or provider.get("tool") != "codegraph_node_impact"
            or provider.get("composite_invocation") != 1
            or provider.get("operation") != operation
            or provider.get("argv_semantic_args") != argv
            or provider.get("semantic_arguments") != semantics
            or provider.get("exit_code") != 0
            or provider.get("error") is not False
            or provider.get("package_version") != CODEGRAPH_VERSION
            or provider.get("index_binding_sha256") != binding_sha256
            or provider.get("complete_sanitized_streams") is not True
            or provider.get("wrapper_truncation") is not False
            or provider.get("provider_reported_truncation") is not False
            or not _nonnegative_metric(provider.get("seconds"))
            or traced.get("shape_valid") is not True
            or any(provider.get(field) != traced.get(field) for field in stream_fields)
        ):
            return None

    provider_seconds = round(float(node["seconds"]) + float(impact["seconds"]), 6)
    combined_seconds = query.get("combined_seconds")
    expected_overlap = (
        round(max(0.0, provider_seconds - float(combined_seconds)), 6)
        if _nonnegative_metric(combined_seconds)
        else None
    )
    if (
        query.get("provider") != "codegraph"
        or query.get("tool") != "codegraph_node_impact"
        or query.get("operation") != "node_plus_upstream_impact_parallel"
        or query.get("provider_call_count") != 2
        or query.get("provider_call_operations")
        != ["node_include_source", "impact_upstream_depth3"]
        or query.get("execution_mode") != "parallel_native_cli_subprocesses"
        or query.get("ordered_path_available") is not False
        or query.get("output_chars") != trace_entry.get("result_chars")
        or query.get("output_sha256") != trace_entry.get("result_sha256")
        or query.get("error") is not False
        or query.get("provider_reported_truncation") is not False
        or query.get("complete_sanitized_provider_outputs") is not True
        or query.get("wrapper_truncation") is not False
        or query.get("package_version") != CODEGRAPH_VERSION
        or query.get("index_binding_sha256") != binding_sha256
        or not _nonnegative_metric(combined_seconds)
        or query.get("provider_seconds_sum") != provider_seconds
        or query.get("parallel_overlap_seconds") != expected_overlap
    ):
        return None
    return query, node, impact


def _native_setup_call(
    run: dict[str, Any],
    trace_entry: dict[str, Any],
    *,
    tool: str,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Bind one successful trace to the matching query and provider ledgers."""

    trace = run.get("conversation", {}).get("tool_trace")
    setup = run.get("setup")
    if not isinstance(trace, list) or not isinstance(setup, dict):
        return None
    tool_entries = [
        entry
        for entry in trace
        if isinstance(entry, dict)
        and entry.get("name") == tool
        and entry.get("error") is False
    ]
    try:
        ordinal = next(index for index, entry in enumerate(tool_entries) if entry is trace_entry)
    except StopIteration:
        return None
    query_calls = setup.get("query_calls")
    provider_calls = setup.get("provider_calls")
    if not isinstance(query_calls, list) or not isinstance(provider_calls, list):
        return None
    matching = [
        entry
        for entry in query_calls
        if isinstance(entry, dict)
        and entry.get("tool") == tool
        and entry.get("error") is False
    ]
    matching_provider = [
        entry
        for entry in provider_calls
        if isinstance(entry, dict)
        and entry.get("tool") == tool
        and entry.get("error") is False
    ]
    if ordinal >= len(matching) or ordinal >= len(matching_provider):
        return None
    frozen = matching[ordinal]
    provider = matching_provider[ordinal]
    raw_arguments = frozen.get("arguments")
    provider_arguments = frozen.get("provider_arguments")
    expected_provider = {
        "codegraph_explore": ("codegraph", "explore"),
        "gitnexus_change_impact": ("gitnexus", "detect_changes"),
    }.get(tool)
    shared_fields = (
        "provider",
        "tool",
        "operation",
        "exit_code",
        "error",
        "output_chars",
        "output_sha256",
    )
    if (
        expected_provider is None
        or not isinstance(raw_arguments, dict)
        or not isinstance(provider_arguments, dict)
        or frozen.get("provider") != expected_provider[0]
        or frozen.get("operation") != expected_provider[1]
        or provider.get("arguments") != provider_arguments
        or any(frozen.get(field) != provider.get(field) for field in shared_fields)
    ):
        return None
    if tool == "codegraph_explore" and (
        frozen.get("contains_source_code") is not True
        or frozen.get("contains_blast_radius") is not True
        or provider.get("contains_source_code") is not True
        or provider.get("contains_blast_radius") is not True
    ):
        return None
    if tool == "codegraph_explore":
        raw_query = raw_arguments.get("query")
        if (
            set(raw_arguments) - {"query", "max_files"}
            or not isinstance(raw_query, str)
            or not raw_query.strip()
        ):
            return None
        expected_provider_arguments: dict[str, Any] = {"query": raw_query.strip()}
        if "max_files" in raw_arguments:
            raw_max_files = raw_arguments["max_files"]
            if isinstance(raw_max_files, bool):
                return None
            try:
                max_files = int(raw_max_files)
            except (TypeError, ValueError):
                return None
            if not 1 <= max_files <= 20:
                return None
            expected_provider_arguments["max_files"] = max_files
        if provider_arguments != expected_provider_arguments:
            return None
    if tool == "gitnexus_change_impact" and (
        frozen.get("contains_changed_symbols") is not True
        or frozen.get("contains_affected_execution_flows") is not True
        or provider.get("contains_changed_symbols") is not True
        or provider.get("contains_affected_execution_flows") is not True
    ):
        return None
    if not (
        trace_entry.get("arguments") == _trace_arguments(tool, raw_arguments)
        and trace_entry.get("arguments_sha256") == _hash_json(raw_arguments)
        and frozen.get("error") is False
        and frozen.get("output_chars") == trace_entry.get("result_chars")
        and _valid_sha256(frozen.get("output_sha256"))
        and frozen.get("output_sha256") == trace_entry.get("result_sha256")
    ):
        return None
    return frozen, provider


def _structured_setup_call(
    run: dict[str, Any],
    trace_entry: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Bind one official structured result across trace/query/provider ledgers."""

    tool = "gitnexus_structured_change"
    trace = run.get("conversation", {}).get("tool_trace")
    setup = run.get("setup")
    if not isinstance(trace, list) or not isinstance(setup, dict):
        return None
    successful = [
        entry
        for entry in trace
        if isinstance(entry, dict)
        and entry.get("name") == tool
        and entry.get("error") is False
    ]
    try:
        ordinal = next(index for index, entry in enumerate(successful) if entry is trace_entry)
    except StopIteration:
        return None
    query_calls = setup.get("query_calls")
    provider_calls = setup.get("provider_calls")
    if not isinstance(query_calls, list) or not isinstance(provider_calls, list):
        return None
    queries = [
        entry
        for entry in query_calls
        if isinstance(entry, dict)
        and entry.get("tool") == tool
        and entry.get("error") is False
    ]
    providers = [
        entry
        for entry in provider_calls
        if isinstance(entry, dict)
        and entry.get("tool") == tool
        and entry.get("error") is False
    ]
    if ordinal >= len(queries) or ordinal >= len(providers):
        return None
    query = queries[ordinal]
    provider = providers[ordinal]
    metrics = (
        "structured_json",
        "provider_error",
        "partial",
        "partial_field_present",
        "partial_value_valid",
        "changed_symbols_count",
        "affected_processes_count",
        "summary_changed_count",
        "summary_affected_count",
        "summary_counts_match_arrays",
    )
    shared_fields = (
        "provider",
        "tool",
        "operation",
        "exit_code",
        "error",
        "output_chars",
        "output_sha256",
        *metrics,
    )
    fixed_arguments = {
        "scope": "compare",
        "base_ref": run.get("baseline_revision"),
    }
    traced_metrics = trace_entry.get("gitnexus_structured_result")
    if (
        query.get("arguments") != {}
        or query.get("provider_arguments") != fixed_arguments
        or provider.get("arguments") != fixed_arguments
        or query.get("runtime_bindings") != {"repo": "isolated_index_clone"}
        or provider.get("runtime_bindings") != {"repo": "isolated_index_clone"}
        or query.get("provider") != "gitnexus"
        or query.get("operation") != "detect_changes"
        or any(query.get(field) != provider.get(field) for field in shared_fields)
        or not isinstance(traced_metrics, dict)
        or any(traced_metrics.get(field) != query.get(field) for field in metrics)
        or trace_entry.get("arguments") != {}
        or trace_entry.get("arguments_sha256") != _hash_json({})
        or query.get("output_chars") != trace_entry.get("result_chars")
        or not _valid_sha256(query.get("output_sha256"))
        or query.get("output_sha256") != trace_entry.get("result_sha256")
    ):
        return None
    return query, provider


def _gitnexus_focused_exact_setup_call(
    run: dict[str, Any],
    trace_entry: dict[str, Any],
    *,
    repo_path: Path,
) -> dict[str, Any] | None:
    """Authenticate one focused K=1 result across every pre-GT ledger."""

    setup = run.get("setup")
    trace = run.get("conversation", {}).get("tool_trace")
    if not isinstance(setup, dict) or not isinstance(trace, list):
        return None
    if trace_entry not in trace:
        return None

    backend_sha = setup.get("backend_module_sha256")
    bridge_sha = setup.get("bridge_sha256")
    renderer_sha = setup.get("renderer_sha256")
    expected_bridge_sha = hashlib.sha256(
        Path(_exact_composite_module._BRIDGE_PATH).read_bytes()
    ).hexdigest()
    expected_renderer_sha = hashlib.sha256(
        Path(_focused_exact_module.__file__).read_bytes()
    ).hexdigest()
    dependencies = [
        "git",
        "filesystem",
        f"gitnexus-exact-composite:{GITNEXUS_VERSION}:{backend_sha}:{bridge_sha}",
        (
            "gitnexus-focused-exact-renderer:"
            f"focused-exact-no-detect-rows-v1:{renderer_sha}"
        ),
    ]
    expected_selector = {
        "version": "k1-cross-community-unique-exact-uid-v1",
        "max_selected": 1,
        "allowed_uid_kinds": [
            "Function",
            "Method",
            "Class",
            "Interface",
            "Constructor",
        ],
        "exclude_tests": True,
        "require_unique_changed_name": True,
        "require_affected_process_membership": True,
        "ordering": [
            "cross_community_processes_desc",
            "total_processes_desc",
            "changed_step_occurrences_desc",
            "kind_priority_asc",
            "filePath_asc",
            "uid_asc",
        ],
    }
    expected_impact = {
        "direction": "upstream",
        "mode": "callgraph",
        "maxDepth": 2,
        "includeTests": False,
        "limit": 8,
        "offset": 0,
        "summaryOnly": False,
    }
    expected_transport = {
        "complete_detect_changes_output": False,
        "structured_json": True,
        "impact_bounded_by_provider": True,
        "context_backend_category_cap": 30,
        "bridge_telemetry_removed_from_model_output": True,
        "wrapper_truncation": False,
        "normalization": "recursive_object_key_sort_arrays_preserved",
        "sanitization": "isolated_clone_path_only",
        "complete_detect_changes_used_before_render": True,
        "detect_rows_model_visible": 0,
        "focused_rendering": True,
        "cli_formatter_used": False,
    }
    fixed_detect = {
        "scope": "compare",
        "base_ref": run.get("baseline_revision"),
    }
    index_stats = setup.get("index_stats")
    capabilities = index_stats.get("capabilities") if isinstance(index_stats, dict) else None
    stats = index_stats.get("stats") if isinstance(index_stats, dict) else None
    tree = subprocess.run(
        ["git", "rev-parse", f"{run.get('head_revision', '')}^{{tree}}"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
    )
    setup_calls = setup.get("setup_calls")
    if (
        setup.get("profile_id") != GITNEXUS_FOCUSED_EXACT_PROFILE_ID
        or setup.get("composite_profile_id")
        != "gitnexus_official_structured_k1_exact_composite"
        or setup.get("base_profile_id") != "paged_generic"
        or setup.get("provider") != "gitnexus"
        or setup.get("package_version") != GITNEXUS_VERSION
        or setup.get("package_integrity") != GITNEXUS_PACKAGE_INTEGRITY
        or setup.get("implementation_mode")
        != "official-local-backend-k1-exact-composite-focused-renderer"
        or setup.get("render_profile") != "focused-exact-no-detect-rows-v1"
        or setup.get("render_protocol_version")
        != "gitnexus-k1-focused-exact-render-v1"
        or setup.get("backend_module") != "dist/mcp/local/local-backend.js"
        or setup.get("resources_module") != "dist/mcp/resources.js"
        or setup.get("official_backend_export") != "LocalBackend"
        or setup.get("official_resource_export") != "readResource"
        or not _valid_sha256(setup.get("binary_sha256"))
        or not _valid_sha256(setup.get("package_json_sha256"))
        or not _valid_sha256(backend_sha)
        or not _valid_sha256(setup.get("resources_module_sha256"))
        or bridge_sha != expected_bridge_sha
        or renderer_sha != expected_renderer_sha
        or setup.get("dependencies") != dependencies
        or setup.get("dependency_sha256") != _hash_json(dependencies)
        or setup.get("tool_surface") != [GITNEXUS_FOCUSED_EXACT_TOOL]
        or setup.get("fixed_provider_arguments") != fixed_detect
        or setup.get("selector_policy") != expected_selector
        or setup.get("impact_policy") != expected_impact
        or setup.get("runtime_bindings") != {"repo": "isolated_index_clone"}
        or setup.get("model_controlled_provider_arguments") != []
        or setup.get("cli_formatter_used") is not False
        or setup.get("persistent_backend_scope") != "one_composite_tool_invocation"
        or setup.get("output_transport") != expected_transport
        or setup.get("isolated") is not True
        or setup.get("source_head") != run.get("head_revision")
        or tree.returncode != 0
        or setup.get("source_tree") != tree.stdout.strip()
        or setup.get("agent_repo_clean") is not True
        or setup.get("agent_repo_graph_dirs_absent") is not True
        or setup.get("index_success") is not True
        or setup.get("installer_used") is not False
        or setup.get("mcp_used") is not False
        or setup.get("prompt_or_hook_injection") is not False
        or setup.get("registry_home_isolated") is not True
        or setup.get("fts_extension_policy") != "load-only"
        or setup.get("embeddings_enabled") is not False
        or setup.get("external_service_started") is not False
        or setup.get("interactive_installer_used") is not False
        or setup.get("cleanup_success") is not True
        or not isinstance(index_stats, dict)
        or index_stats.get("lastCommit") != run.get("head_revision")
        or not isinstance(capabilities, dict)
        or capabilities.get("graph", {}).get("status") != "available"
        or capabilities.get("fts", {}).get("status") != "available"
        or not isinstance(stats, dict)
        or stats.get("embeddings") != 0
        or not isinstance(setup_calls, list)
        or len(setup_calls) != 1
    ):
        return None
    setup_call = setup_calls[0]
    if (
        not isinstance(setup_call, dict)
        or setup_call.get("operation") != "analyze"
        or setup_call.get("exit_code") != 0
        or setup_call.get("error") is not False
        or not _nonnegative_metric(setup_call.get("seconds"))
        or not isinstance(setup_call.get("output_chars"), int)
        or isinstance(setup_call.get("output_chars"), bool)
        or int(setup_call["output_chars"]) < 0
        or not _valid_sha256(setup_call.get("output_sha256"))
    ):
        return None

    queries = setup.get("query_calls")
    providers = setup.get("provider_calls")
    audits = setup.get("focused_render_audits")
    if (
        not isinstance(queries, list)
        or len(queries) != 1
        or not isinstance(queries[0], dict)
        or not isinstance(providers, list)
        or not providers
        or not all(isinstance(call, dict) for call in providers)
        or not isinstance(audits, list)
        or len(audits) != 1
        or not isinstance(audits[0], dict)
    ):
        return None
    query = cast(dict[str, Any], queries[0])
    audit = cast(dict[str, Any], audits[0])
    provider_calls = cast(list[dict[str, Any]], providers)

    trace_result = trace_entry.get("gitnexus_focused_exact_result")
    selected = trace_result.get("selected") if isinstance(trace_result, dict) else None
    ranking = trace_result.get("ranking_rationale") if isinstance(trace_result, dict) else None
    enrichment = trace_result.get("enrichment") if isinstance(trace_result, dict) else None
    counts = trace_result.get("detect_counts") if isinstance(trace_result, dict) else None
    summary = trace_result.get("detect_summary") if isinstance(trace_result, dict) else None
    coverage = trace_result.get("coverage") if isinstance(trace_result, dict) else None
    if (
        trace_entry.get("name") != GITNEXUS_FOCUSED_EXACT_TOOL
        or trace_entry.get("error") is not False
        or trace_entry.get("arguments") != {}
        or trace_entry.get("arguments_sha256") != _hash_json({})
        or not isinstance(trace_result, dict)
        or trace_result.get("structured_json") is not True
        or trace_result.get("shape_valid") is not True
        or trace_result.get("protocol_version")
        != "gitnexus-k1-focused-exact-render-v1"
        or trace_result.get("render_profile") != "focused-exact-no-detect-rows-v1"
        or trace_result.get("forbidden_raw_detect_field_count") != 0
        or trace_result.get("model_visible_raw_detect_rows") != 0
        or counts != {"changed_symbols": 139, "affected_processes": 11}
        or not isinstance(summary, dict)
        or summary.get("changed_count") != 139
        or summary.get("affected_count") != 11
        or coverage
        != {
            "changed_symbols_in_view": 0,
            "processes_in_view": 0,
            "omitted_changed_symbols": 139,
            "omitted_processes": 11,
            "is_exhaustive_repository_coverage": False,
            "notice": (
                "All detect_changes symbol and process rows are omitted from this "
                "focused view. Use repository tools for exhaustive documentation-drift "
                "coverage."
            ),
        }
        or trace_result.get("selection_status") != "selected"
        or not isinstance(selected, dict)
        or set(selected) != {"uid", "name", "kind", "filePath"}
        or not all(isinstance(selected.get(key), str) and selected.get(key) for key in selected)
        or selected.get("kind") not in expected_selector["allowed_uid_kinds"]
        or not isinstance(ranking, dict)
        or ranking.get("reason") != "highest_ranked_eligible_exact_uid"
        or ranking.get("policy_version") != expected_selector["version"]
        or ranking.get("ordering") != expected_selector["ordering"]
        or not isinstance(ranking.get("eligible_count"), int)
        or isinstance(ranking.get("eligible_count"), bool)
        or int(ranking["eligible_count"]) < 1
        or not isinstance(ranking.get("selected_score"), dict)
        or not isinstance(enrichment, dict)
        or set(enrichment) != {"context", "impact", "trace", "process"}
    ):
        return None

    uid = cast(str, selected["uid"])
    name = cast(str, selected["name"])
    context_arguments = {"include_content": False, "name": name, "uid": uid}
    impact_arguments = {**expected_impact, "target": name, "target_uid": uid}
    context_trace = enrichment.get("context")
    impact_trace = enrichment.get("impact")
    trace_trace = enrichment.get("trace")
    process_trace = enrichment.get("process")
    if not all(
        isinstance(component, dict)
        for component in (context_trace, impact_trace, trace_trace, process_trace)
    ):
        return None
    context_trace = cast(dict[str, Any], context_trace)
    impact_trace = cast(dict[str, Any], impact_trace)
    trace_trace = cast(dict[str, Any], trace_trace)
    process_trace = cast(dict[str, Any], process_trace)
    expected_context_symbol = {"uid": uid, "name": name}
    if (
        context_trace.get("shape_valid") is not True
        or context_trace.get("performed") is not True
        or context_trace.get("arguments") != context_arguments
        or context_trace.get("arguments_sha256") != _hash_json(context_arguments)
        or context_trace.get("result_present") is not True
        or context_trace.get("result_status") != "found"
        or context_trace.get("result_symbol") != expected_context_symbol
        or not isinstance(context_trace.get("result_trace"), dict)
        or not _valid_sha256(context_trace["result_trace"].get("sha256"))
        or impact_trace.get("shape_valid") is not True
        or impact_trace.get("performed") is not True
        or impact_trace.get("arguments") != impact_arguments
        or impact_trace.get("arguments_sha256") != _hash_json(impact_arguments)
        or impact_trace.get("result_present") is not True
        or impact_trace.get("result_target") != {"id": uid, "name": name}
        or not isinstance(impact_trace.get("result_trace"), dict)
        or not _valid_sha256(impact_trace["result_trace"].get("sha256"))
    ):
        return None

    conditional_operations: list[str] = []
    for operation, component in (("trace", trace_trace), ("process_resource", process_trace)):
        performed = component.get("performed")
        if not isinstance(performed, bool) or component.get("shape_valid") is not True:
            return None
        if performed:
            conditional_operations.append(operation)
            if operation == "trace":
                arguments = component.get("arguments")
                if (
                    not isinstance(arguments, dict)
                    or arguments.get("to") != name
                    or arguments.get("to_uid") != uid
                    or not isinstance(arguments.get("from"), str)
                    or not arguments.get("from")
                    or not isinstance(arguments.get("from_uid"), str)
                    or not arguments.get("from_uid")
                    or not isinstance(arguments.get("maxDepth"), int)
                    or isinstance(arguments.get("maxDepth"), bool)
                    or int(arguments["maxDepth"]) < 2
                    or arguments.get("includeTests") is not False
                    or component.get("arguments_sha256") != _hash_json(arguments)
                    or component.get("result_present") is not True
                    or not isinstance(component.get("result_trace"), dict)
                    or not _valid_sha256(component["result_trace"].get("sha256"))
                ):
                    return None
            else:
                if (
                    not isinstance(component.get("selected_process_name"), str)
                    or not component.get("selected_process_name")
                    or not isinstance(component.get("selected_process_trace"), dict)
                    or component.get("content_present") is not True
                    or not isinstance(component.get("content_chars"), int)
                    or isinstance(component.get("content_chars"), bool)
                    or int(component["content_chars"]) < 0
                    or not _valid_sha256(component.get("content_sha256"))
                ):
                    return None
        elif (
            not isinstance(component.get("reason"), str)
            or not component.get("reason")
            or component.get("arguments") is not None
            or component.get("arguments_sha256") is not None
            or component.get("result_present") is not False
        ):
            return None

    expected_operations = ["detect_changes", "context", "impact", *conditional_operations]
    if [call.get("operation") for call in provider_calls] != expected_operations:
        return None
    for index, call in enumerate(provider_calls, start=1):
        if (
            call.get("call_index") != index
            or call.get("composite_invocation") != 1
            or not isinstance(call.get("arguments"), dict)
            or call.get("runtime_bindings") != {"repo": "isolated_index_clone"}
            or not _nonnegative_metric(call.get("seconds"))
            or not isinstance(call.get("output_chars"), int)
            or isinstance(call.get("output_chars"), bool)
            or int(call["output_chars"]) < 0
            or not _valid_sha256(call.get("output_sha256"))
            or call.get("bridge_exception") is not False
            or call.get("error") is not False
            or call.get("partial") is not False
            or not isinstance(call.get("partial_field_present"), bool)
            or call.get("partial_value_valid") is not True
            or call.get("pagination_field_present") is not False
            or call.get("pagination") is not None
            or call.get("ambiguity_candidates") != 0
            or not (
                call.get("status") is None or isinstance(call.get("status"), str)
            )
        ):
            return None

    by_operation = {cast(str, call["operation"]): call for call in provider_calls}
    if (
        by_operation["detect_changes"].get("arguments") != fixed_detect
        or by_operation["context"].get("arguments") != context_arguments
        or by_operation["context"].get("status") != context_trace.get("result_status")
        or by_operation["impact"].get("arguments") != impact_arguments
        or by_operation["impact"].get("output_chars")
        != impact_trace["result_trace"].get("chars")
        or by_operation["impact"].get("output_sha256")
        != impact_trace["result_trace"].get("sha256")
    ):
        return None
    if trace_trace.get("performed") is True:
        trace_provider = by_operation.get("trace")
        if (
            trace_provider is None
            or trace_provider.get("arguments") != trace_trace.get("arguments")
            or trace_provider.get("output_chars")
            != trace_trace["result_trace"].get("chars")
            or trace_provider.get("output_sha256")
            != trace_trace["result_trace"].get("sha256")
        ):
            return None
    if process_trace.get("performed") is True:
        process_provider = by_operation.get("process_resource")
        if (
            process_provider is None
            or process_provider.get("arguments")
            != {"process_name": process_trace.get("selected_process_name")}
            or process_provider.get("output_chars") != process_trace.get("content_chars")
            or process_provider.get("output_sha256") != process_trace.get("content_sha256")
        ):
            return None

    raw_provider_calls = [
        {key: value for key, value in call.items() if key != "composite_invocation"}
        for call in provider_calls
    ]
    raw_chars = query.get("raw_composite_result_chars")
    raw_sha = query.get("raw_composite_result_sha256")
    focused_chars = query.get("result_chars")
    focused_sha = query.get("result_sha256")
    detect_metrics = query.get("detect_metrics")
    performed = query.get("enrichment_performed")
    expected_performed = {
        "context": True,
        "impact": True,
        "trace": trace_trace.get("performed"),
        "process": process_trace.get("performed"),
    }
    if (
        query.get("provider") != "gitnexus"
        or query.get("tool") != "gitnexus_exact_composite"
        or query.get("operation") != "k1_exact_composite"
        or query.get("exit_code") != 0
        or query.get("error") is not False
        or query.get("arguments") != {}
        or query.get("provider_arguments") != fixed_detect
        or query.get("runtime_bindings") != {"repo": "isolated_index_clone"}
        or not _nonnegative_metric(query.get("seconds"))
        or not isinstance(query.get("bridge_output_chars"), int)
        or isinstance(query.get("bridge_output_chars"), bool)
        or int(query["bridge_output_chars"]) <= 0
        or not _valid_sha256(query.get("bridge_output_sha256"))
        or query.get("structured_json") is not True
        or query.get("provider_call_count") != len(provider_calls)
        or query.get("provider_calls_sha256") != _hash_json(raw_provider_calls)
        or query.get("render_profile") != "focused-exact-no-detect-rows-v1"
        or query.get("focused_rendered") is not True
        or not isinstance(raw_chars, int)
        or isinstance(raw_chars, bool)
        or not isinstance(focused_chars, int)
        or isinstance(focused_chars, bool)
        or raw_chars <= focused_chars
        or not _valid_sha256(raw_sha)
        or not _valid_sha256(focused_sha)
        or raw_sha == focused_sha
        or focused_chars != trace_entry.get("result_chars")
        or focused_sha != trace_entry.get("result_sha256")
        or query.get("raw_composite_result_chars")
        != audit.get("raw_composite_model_view_chars")
        or query.get("raw_composite_result_sha256")
        != audit.get("raw_composite_model_view_sha256")
        or query.get("result_chars") != audit.get("focused_model_view_chars")
        or query.get("result_sha256") != audit.get("focused_model_view_sha256")
        or query.get("complete_detect_audit") != audit
        or not isinstance(detect_metrics, dict)
        or detect_metrics.get("structured_json") is not True
        or detect_metrics.get("provider_error") is not False
        or detect_metrics.get("partial") is not False
        or detect_metrics.get("partial_value_valid") is not True
        or detect_metrics.get("changed_symbols_count") != 139
        or detect_metrics.get("affected_processes_count") != 11
        or detect_metrics.get("summary_changed_count") != 139
        or detect_metrics.get("summary_affected_count") != 11
        or detect_metrics.get("summary_counts_match_arrays") is not True
        or query.get("selection_status") != "selected"
        or query.get("selector_policy_version") != expected_selector["version"]
        or query.get("selected_uid") != uid
        or query.get("selected_name") != name
        or query.get("selected_score") != ranking.get("selected_score")
        or query.get("eligible_count") != ranking.get("eligible_count")
        or not isinstance(query.get("rejection_counts"), dict)
        or performed != expected_performed
    ):
        return None

    normalization = audit.get("focused_normalization")
    detect_provider = by_operation["detect_changes"]
    if (
        audit.get("audit_version") != "gitnexus-focused-exact-audit-v1"
        or audit.get("complete_detect_internal") is not True
        or audit.get("complete_detect_model_visible") is not False
        or audit.get("selector_input") != "complete_detect_changes_before_render"
        or audit.get("provider_call_index") != 1
        or audit.get("provider_output_chars") != detect_provider.get("output_chars")
        or audit.get("provider_output_sha256") != detect_provider.get("output_sha256")
        or audit.get("complete_detect_render_chars")
        != detect_provider.get("output_chars")
        or audit.get("complete_detect_render_sha256")
        != detect_provider.get("output_sha256")
        or audit.get("provider_digest_matches_complete_detect") is not True
        or audit.get("changed_symbols_count") != 139
        or audit.get("affected_processes_count") != 11
        or audit.get("summary_counts_match_arrays") is not True
        or audit.get("model_visible_changed_symbol_rows") != 0
        or audit.get("model_visible_process_rows") != 0
        or audit.get("omitted_changed_symbols") != 139
        or audit.get("omitted_processes") != 11
        or audit.get("selected_uid") != uid
        or audit.get("selected_name") != name
        or not isinstance(normalization, dict)
        or normalization.get("context_processes_sorted") is not True
        or not isinstance(normalization.get("context_processes_count"), int)
        or isinstance(normalization.get("context_processes_count"), bool)
        or int(normalization["context_processes_count"]) < 0
        or normalization.get("context_processes_order")
        != "name,id,process_type,step_count,canonical_object"
        or normalization.get("context_relation_arrays_sorted") is not True
        or normalization.get("context_relation_order")
        != "uid,name,filePath,kind,canonical_object"
        or normalization.get("context_typed_properties_sorted") is not True
        or normalization.get("all_other_provider_arrays_preserved") is not True
    ):
        return None
    return {
        "query": query,
        "providers": provider_calls,
        "audit": audit,
        "selected_uid": uid,
        "selected_name": name,
        "operations": expected_operations,
    }


def _native_setup_call_matches(
    run: dict[str, Any],
    trace_entry: dict[str, Any],
    *,
    tool: str,
) -> bool:
    """Return whether a native graph trace is bound across all frozen ledgers."""

    return _native_setup_call(run, trace_entry, tool=tool) is not None


def _changed_diff_identifiers(content: str) -> set[str]:
    """Return code-like identifiers present on actual added/removed diff lines."""

    identifiers: set[str] = set()
    for line in content.splitlines():
        if not line.startswith(("+", "-")) or line.startswith(("+++", "---")):
            continue
        for token in re.findall(r"[A-Za-z_$][A-Za-z0-9_$]*", line[1:]):
            if len(token) >= 3 and token.casefold() not in _NON_SYMBOL_IDENTIFIERS:
                identifiers.add(token)
    return identifiers


def _exact_symbol_in_prior_diff(
    symbol: str,
    diffs: list[ReplayedDiff],
    position: tuple[int, int],
) -> bool:
    if re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", symbol) is None:
        return False
    pattern = re.compile(rf"(?<![A-Za-z0-9_$]){re.escape(symbol)}(?![A-Za-z0-9_$])")
    return any(
        diff.position[0] < position[0]
        and any(
            pattern.search(line[1:]) is not None
            for line in diff.content.splitlines()
            if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
        )
        for diff in diffs
    )


def _query_diff_terms(
    query: str,
    diffs: list[ReplayedDiff],
    position: tuple[int, int],
) -> list[str]:
    identifiers = sorted(
        {
            token
            for token in re.findall(r"[A-Za-z_$][A-Za-z0-9_$]*", query)
            if len(token) >= 3
        }
    )
    return [
        token
        for token in identifiers
        if any(
            diff.position[0] < position[0]
            and token in _changed_diff_identifiers(diff.content)
            for diff in diffs
        )
    ]


def _adoption_status(
    run: dict[str, Any],
    *,
    repo_path: Path | None = None,
) -> dict[str, dict[str, Any]]:
    menu = set(run["tools"]["names"])
    trace = run["conversation"]["tool_trace"]

    broad_scans = [
        (int(entry["turn"]), int(entry["ordinal"]))
        for entry in trace
        if entry.get("error") is False and _repository_wide_trace_scan(entry)
    ]
    first_broad_scan = min(broad_scans) if broad_scans else None
    result: dict[str, dict[str, Any]] = {}
    for tool, (rule, threshold) in OPTIONAL_ADOPTION_RULES.items():
        if tool not in menu:
            result[tool] = {
                "required": False,
                "rule": "not_applicable",
                "successful_calls": None,
                "total_calls": None,
                "passed": True,
            }
            continue
        calls = [entry for entry in trace if entry.get("name") == tool]
        successes = [entry for entry in calls if entry.get("error") is False]
        successful_before_broad = [
            entry
            for entry in successes
            if first_broad_scan is None or int(entry["turn"]) < first_broad_scan[0]
        ]
        exact_context_calls: list[dict[str, Any]] = []
        diff_supported_calls: list[dict[str, Any]] = []
        native_valid_calls: list[dict[str, Any]] = []
        native_setup_bound_calls: list[dict[str, Any]] = []
        native_diff_terms: set[str] = set()
        unsupported_diff_targets: set[str] = set()
        diff_replay_failures = 0
        if tool == "graph_context":
            replayed_diffs: list[ReplayedDiff] = []
            if repo_path is not None:
                replayed_diffs, diff_replay_failures = _replay_successful_git_diffs(
                    run,
                    repo_path=repo_path,
                )
            for entry in successful_before_broad:
                kinds = entry.get("graph_result_kinds")
                arguments = entry.get("arguments")
                arguments = arguments if isinstance(arguments, dict) else {}
                requested_targets = arguments.get("targets")
                result_targets = entry.get("graph_result_targets")
                position = (int(entry["turn"]), int(entry["ordinal"]))
                supported_targets: set[str] = set()
                valid_requested_targets = (
                    isinstance(requested_targets, list)
                    and bool(requested_targets)
                    and all(isinstance(target, str) and target for target in requested_targets)
                )
                if isinstance(requested_targets, list):
                    for target in requested_targets:
                        if not isinstance(target, str) or not target:
                            continue
                        if any(
                            diff.position[0] < position[0]
                            and _target_is_present_in_diff(target, diff.content)
                            for diff in replayed_diffs
                        ):
                            supported_targets.add(target)
                        else:
                            unsupported_diff_targets.add(target)
                all_targets_supported = valid_requested_targets and supported_targets == set(
                    cast(list[str], requested_targets)
                )
                if (
                    all_targets_supported
                    and entry.get("graph_exact_context") is True
                    and isinstance(kinds, list)
                    and kinds
                    and all(isinstance(kind, str) for kind in kinds)
                    and set(kinds) != {"no_match"}
                    and isinstance(requested_targets, list)
                    and requested_targets
                    and all(isinstance(target, str) and target for target in requested_targets)
                    and isinstance(result_targets, list)
                    and bool(set(requested_targets) & set(result_targets))
                ):
                    exact_context_calls.append(entry)
                    diff_supported_calls.append(entry)
            passed = len(exact_context_calls) >= threshold
        elif tool == "codegraph_explore":
            replayed_diffs = []
            if repo_path is not None:
                replayed_diffs, diff_replay_failures = _replay_successful_git_diffs(
                    run,
                    repo_path=repo_path,
                )
            for entry in successful_before_broad:
                arguments = entry.get("arguments")
                arguments = arguments if isinstance(arguments, dict) else {}
                query = arguments.get("query")
                position = (int(entry["turn"]), int(entry["ordinal"]))
                supported_terms = (
                    _query_diff_terms(query, replayed_diffs, position)
                    if isinstance(query, str)
                    else []
                )
                markers = entry.get("native_graph_markers")
                setup_bound = _native_setup_call_matches(run, entry, tool=tool)
                if setup_bound:
                    native_setup_bound_calls.append(entry)
                if (
                    supported_terms
                    and setup_bound
                    and isinstance(markers, dict)
                    and markers.get("codegraph_source") is True
                    and markers.get("codegraph_blast_radius") is True
                    and markers.get("codegraph_line_numbered_source") is True
                ):
                    native_diff_terms.update(supported_terms)
                    native_valid_calls.append(entry)
            passed = len(native_valid_calls) >= threshold
        elif tool == "codegraph_node_impact":
            replayed_diffs = []
            if repo_path is not None:
                replayed_diffs, diff_replay_failures = _replay_successful_git_diffs(
                    run,
                    repo_path=repo_path,
                )
            for entry in successful_before_broad:
                bound = (
                    _codegraph_node_impact_setup_call(
                        run,
                        entry,
                        repo_path=repo_path,
                    )
                    if repo_path is not None
                    else None
                )
                if bound is None:
                    continue
                native_setup_bound_calls.append(entry)
                query = bound[0]
                normalized = query.get("normalized_arguments")
                normalized = normalized if isinstance(normalized, dict) else {}
                symbol = normalized.get("symbol")
                file = normalized.get("file")
                position = (int(entry["turn"]), int(entry["ordinal"]))
                symbol_supported = isinstance(symbol, str) and _exact_symbol_in_prior_diff(
                    symbol,
                    replayed_diffs,
                    position,
                )
                file_supported = file is None or (
                    isinstance(file, str)
                    and any(
                        diff.position[0] < position[0] and file in diff.content
                        for diff in replayed_diffs
                    )
                )
                if symbol_supported and file_supported:
                    native_diff_terms.add(cast(str, symbol))
                    native_valid_calls.append(entry)
            passed = (
                len(native_valid_calls) == threshold
                and len(calls) == 1
                and len(successes) == 1
            )
        elif tool == "gitnexus_change_impact":
            setup = run.get("setup")
            initial_protocol = run.get("protocol_version") == PROTOCOL_V4
            replayed_diffs = []
            if repo_path is not None and not initial_protocol:
                replayed_diffs, diff_replay_failures = _replay_successful_git_diffs(
                    run,
                    repo_path=repo_path,
                )
            fixed_arguments = {
                "scope": "compare",
                "base_ref": run.get("baseline_revision"),
                "limit": 500,
            }
            fixed_setup = (
                isinstance(setup, dict)
                and setup.get("fixed_provider_arguments") == fixed_arguments
            )
            for entry in successful_before_broad:
                markers = entry.get("native_graph_markers")
                position = (int(entry["turn"]), int(entry["ordinal"]))
                timing_valid = (
                    position == (1, 1)
                    if initial_protocol
                    else any(diff.position[0] < position[0] for diff in replayed_diffs)
                )
                bound = _native_setup_call(run, entry, tool=tool)
                setup_bound = bound is not None
                if setup_bound:
                    native_setup_bound_calls.append(entry)
                if (
                    setup_bound
                    and entry.get("arguments") == {}
                    and timing_valid
                    and fixed_setup
                    and bound is not None
                    and bound[0].get("provider_arguments") == fixed_arguments
                    and bound[1].get("arguments") == fixed_arguments
                    and isinstance(markers, dict)
                    and markers.get("gitnexus_changed_symbols") is True
                    and markers.get("gitnexus_affected_flows") is True
                ):
                    native_valid_calls.append(entry)
            passed = len(native_valid_calls) >= threshold
            if initial_protocol:
                passed = passed and len(calls) == 1 and len(successes) == 1
        elif tool == "gitnexus_structured_change":
            setup = run.get("setup")
            fixed_arguments = {
                "scope": "compare",
                "base_ref": run.get("baseline_revision"),
            }
            fixed_setup = (
                isinstance(setup, dict)
                and setup.get("profile_id") == "gitnexus_official_structured_change"
                and setup.get("fixed_provider_arguments") == fixed_arguments
                and setup.get("provider_limit") is None
                and setup.get("cli_formatter_used") is False
            )
            for entry in successful_before_broad:
                bound = _structured_setup_call(run, entry)
                if bound is not None:
                    native_setup_bound_calls.append(entry)
                if (
                    bound is not None
                    and fixed_setup
                    and (int(entry["turn"]), int(entry["ordinal"])) == (1, 1)
                    and entry.get("arguments") == {}
                    and bound[0].get("structured_json") is True
                    and bound[0].get("provider_error") is False
                    and bound[0].get("partial") is False
                    and bound[0].get("partial_value_valid") is True
                    and bound[0].get("summary_counts_match_arrays") is True
                ):
                    native_valid_calls.append(entry)
            passed = (
                len(native_valid_calls) == threshold
                and len(calls) == 1
                and len(successes) == 1
            )
        elif tool == GITNEXUS_FOCUSED_EXACT_TOOL:
            replayed_diffs = []
            if repo_path is not None:
                replayed_diffs, diff_replay_failures = _replay_successful_git_diffs(
                    run,
                    repo_path=repo_path,
                )
            for entry in successful_before_broad:
                position = (int(entry["turn"]), int(entry["ordinal"]))
                bound = (
                    _gitnexus_focused_exact_setup_call(
                        run,
                        entry,
                        repo_path=repo_path,
                    )
                    if repo_path is not None
                    else None
                )
                if bound is not None:
                    native_setup_bound_calls.append(entry)
                if (
                    bound is not None
                    and entry.get("arguments") == {}
                    and any(diff.position[0] < position[0] for diff in replayed_diffs)
                ):
                    native_valid_calls.append(entry)
            passed = (
                diff_replay_failures == 0
                and len(native_valid_calls) == threshold
                and len(calls) == 1
                and len(successes) == 1
            )
        else:
            count = len(successful_before_broad)
            passed = count == threshold if rule == "exactly" else count >= threshold
            # ``audit_brief`` is deliberately one-shot: failed/duplicate calls
            # remain an adoption failure even if one successful call exists.
            if tool == "audit_brief":
                passed = passed and len(calls) == 1 and len(successes) == 1
        result[tool] = {
            "required": True,
            "rule": {
                "graph_context": (
                    "at_least_1_exact_context_with_all_targets_in_verified_prior_diff_"
                    "before_broad_scan"
                ),
                "codegraph_explore": (
                    "at_least_1_setup_bound_source_and_blast_response_with_a_query_symbol_"
                    "from_verified_prior_diff_before_broad_scan"
                ),
                "codegraph_node_impact": (
                    "exactly_1_setup_bound_complete_node_source_and_upstream_impact_response_"
                    "with_an_exact_symbol_from_verified_prior_diff_before_broad_scan"
                ),
                "gitnexus_change_impact": (
                    (
                        "exactly_1_initial_setup_bound_fixed_compare_response_with_changed_"
                        "symbols_and_affected_flows_before_broad_scan"
                    )
                    if run.get("protocol_version") == PROTOCOL_V4
                    else (
                        "at_least_1_setup_bound_fixed_compare_response_with_changed_symbols_"
                        "and_affected_flows_before_broad_scan"
                    )
                ),
                "gitnexus_structured_change": (
                    "exactly_1_initial_setup_bound_official_structured_response_with_"
                    "complete_arrays_no_error_and_no_partial_before_broad_scan"
                ),
                "gitnexus_focused_exact": (
                    "exactly_1_after_verified_diff_setup_bound_focused_k1_response_with_"
                    "complete_139_11_internal_detect_zero_raw_rows_and_exact_provider_"
                    "ledgers_before_broad_scan"
                ),
            }.get(tool, "exactly_1_successful_call_before_broad_scan"),
            "successful_calls": len(successes),
            "total_calls": len(calls),
            "successful_calls_before_broad_scan": len(successful_before_broad),
            "exact_context_calls_before_broad_scan": (
                len(exact_context_calls) if tool == "graph_context" else None
            ),
            "diff_supported_calls_before_broad_scan": (
                len(diff_supported_calls) if tool == "graph_context" else None
            ),
            "diff_replay_failures": (
                diff_replay_failures
                if tool
                in {
                    "graph_context",
                    "codegraph_explore",
                    "codegraph_node_impact",
                    "gitnexus_change_impact",
                    "gitnexus_focused_exact",
                }
                else None
            ),
            "unsupported_diff_targets": (
                sorted(unsupported_diff_targets) if tool == "graph_context" else None
            ),
            "native_valid_calls_before_broad_scan": (
                len(native_valid_calls)
                if tool
                in {
                    "codegraph_explore",
                    "codegraph_node_impact",
                    "gitnexus_change_impact",
                    "gitnexus_structured_change",
                    "gitnexus_focused_exact",
                }
                else None
            ),
            "native_setup_bound_calls_before_broad_scan": (
                len(native_setup_bound_calls)
                if tool
                in {
                    "codegraph_explore",
                    "codegraph_node_impact",
                    "gitnexus_change_impact",
                    "gitnexus_structured_change",
                    "gitnexus_focused_exact",
                }
                else None
            ),
            "diff_supported_query_terms": (
                sorted(native_diff_terms)
                if tool in {"codegraph_explore", "codegraph_node_impact"}
                else None
            ),
            "first_broad_scan": (
                {"turn": first_broad_scan[0], "ordinal": first_broad_scan[1]}
                if first_broad_scan is not None
                else None
            ),
            "passed": passed,
        }
    return result


def _preflight_artifacts(
    frozen_artifacts: list[FrozenArtifact],
    *,
    control_agent: str = DEFAULT_CONTROL_AGENT,
) -> tuple[dict[str, tuple[Path, str]], dict[str, Any], dict[Path, dict[str, Any]]]:
    """Fail closed on the complete arbitrary-size stage before GT is read."""

    identities: set[tuple[str, str]] = set()
    normalized_runs: dict[Path, dict[str, Any]] = {}
    target_cache: dict[str, tuple[Path, str]] = {}
    tool_fingerprint_by_agent: dict[
        str,
        tuple[tuple[str, ...], str, tuple[str, ...], str],
    ] = {}

    first_artifact: dict[str, Any] | None = None
    first_run: dict[str, Any] | None = None
    first_provider: dict[str, Any] | None = None
    first_base_schema_hash: str | None = None
    stage_protocol_version: str | None = None
    first_normalized_configuration: dict[str, Any] | None = None
    agents: set[str] = set()

    for frozen in frozen_artifacts:
        artifact = frozen.payload
        protocol_version = artifact.get("protocol_version")
        if protocol_version not in SUPPORTED_PROTOCOL_VERSIONS:
            raise ValueError(f"unsupported protocol in {frozen.path}")
        if stage_protocol_version is None:
            stage_protocol_version = cast(str, protocol_version)
        elif protocol_version != stage_protocol_version:
            raise ValueError("tool-portfolio stage mixes protocol versions")
        if artifact.get("langfuse_enabled") is not False:
            raise ValueError(f"observability state is not explicitly disabled: {frozen.path}")
        runs = artifact.get("runs")
        if not isinstance(runs, list) or len(runs) != 1 or not isinstance(runs[0], dict):
            raise ValueError(f"artifact must contain exactly one completed run: {frozen.path}")
        run = runs[0]
        agent = str(run.get("agent", ""))
        pair_key = str(run.get("pair_key", ""))
        pair_id = artifact.get("pair_id")
        identity = (agent, pair_key)
        if not agent or not pair_key or not isinstance(pair_id, str):
            raise ValueError(f"artifact/run identity is incomplete in {frozen.path}")
        if identity in identities:
            raise ValueError(f"duplicate agent/pair identity {identity!r}")
        _validate_agent_protocol(agent, cast(str, protocol_version), location=str(frozen.path))
        if (
            artifact.get("agent") != agent
            or run.get("run") != 1
            or pair_key != f"{pair_id}.1"
            or run.get("protocol_version") != protocol_version
        ):
            raise ValueError(f"artifact/run identity mismatch in {frozen.path}")

        for field in ("baseline_revision", "head_revision", "requested_model"):
            if artifact.get(field) != run.get(field):
                raise ValueError(f"artifact/run {field} mismatch in {frozen.path}")

        prompt = _validate_prompt(run.get("prompt"), location=str(frozen.path))
        _validate_focused_exact_prompt(
            prompt,
            protocol_version=stage_protocol_version,
            location=str(frozen.path),
        )

        tools_value = run.get("tools")
        menu_value = tools_value.get("names") if isinstance(tools_value, dict) else None
        forced_tool = _forced_tool_from_names(menu_value)
        configuration, normalized_configuration = _validate_configuration(
            run.get("configuration"),
            protocol_version=stage_protocol_version,
            forced_tool=forced_tool,
            location=str(frozen.path),
        )
        transport = cast(dict[str, Any], configuration["transport"])
        root_provider = _validate_provider_routing(
            artifact.get("provider_routing"), location=str(frozen.path)
        )
        run_provider = _validate_provider_routing(
            run.get("provider_routing"), location=f"{frozen.path} run"
        )
        transport_provider = _validate_provider_routing(
            transport.get("provider_routing"), location=f"{frozen.path} run"
        )
        if root_provider != run_provider or root_provider != transport_provider:
            raise ValueError(f"root/run provider_routing mismatch in {frozen.path}")
        expected_provider_hash = _hash_json(root_provider)
        if (
            artifact.get("provider_routing_sha256") != expected_provider_hash
            or run.get("provider_routing_sha256") != expected_provider_hash
        ):
            raise ValueError(f"provider routing hash mismatch in {frozen.path}")
        root_endpoint = artifact.get("openrouter_base_url")
        run_endpoint = run.get("openrouter_base_url")
        if (
            root_endpoint != EXPECTED_OPENROUTER_BASE_URL
            or run_endpoint != EXPECTED_OPENROUTER_BASE_URL
            or root_endpoint != run_endpoint
        ):
            raise ValueError(f"OpenRouter endpoint mismatch in {frozen.path}")

        tools = run.get("tools")
        names, schema_hash, base_names, base_schema_hash = _validate_tool_metadata(
            tools,
            agent=agent,
            protocol_version=stage_protocol_version,
            location=str(frozen.path),
        )
        tool_fingerprint = (names, schema_hash, base_names, base_schema_hash)
        if (
            agent in tool_fingerprint_by_agent
            and tool_fingerprint_by_agent[agent] != tool_fingerprint
        ):
            raise ValueError(f"{agent} tool menu/schema differs across runs")
        tool_fingerprint_by_agent[agent] = tool_fingerprint

        _validate_usage_and_timing(run, location=f"{pair_key}/{agent}")
        _validate_conversation(
            run,
            set(names),
            protocol_version=stage_protocol_version,
        )
        for channel_name in ("submission_only", "store", "delivered"):
            _validate_finding_channel(run, channel_name)

        artifact_started = _timestamp_ns(artifact, "generation_started_at_ns", str(frozen.path))
        artifact_completed = _timestamp_ns(artifact, "generation_completed_at_ns", str(frozen.path))
        run_started = _timestamp_ns(run, "generation_started_at_ns", str(frozen.path))
        run_completed = _timestamp_ns(run, "generation_completed_at_ns", str(frozen.path))
        if artifact.get("completed_at_ns") != artifact_completed:
            raise ValueError(f"artifact completion timestamps disagree in {frozen.path}")
        if run.get("completed_at_ns") != run_completed:
            raise ValueError(f"run completion timestamps disagree in {frozen.path}")
        if not (
            artifact_started
            <= run_started
            <= run_completed
            == artifact_completed
            <= frozen.mtime_ns
        ):
            raise ValueError(f"generation timestamps are inconsistent in {frozen.path}")

        descriptor = artifact.get("target")
        if not isinstance(descriptor, dict):
            raise ValueError(f"target descriptor is invalid in {frozen.path}")
        repo_path, fixture_baseline = _target_repo(descriptor, target_cache)
        if fixture_baseline and fixture_baseline != artifact.get("baseline_revision"):
            raise ValueError(f"fixture baseline mismatch in {frozen.path}")
        if _git_head(repo_path) != artifact.get("head_revision"):
            raise ValueError(f"target HEAD mismatch in {frozen.path}")
        if not _git_baseline_is_ancestor(
            repo_path,
            str(artifact.get("baseline_revision", "")),
            str(artifact.get("head_revision", "")),
        ):
            raise ValueError(f"target baseline is not an ancestor of HEAD in {frozen.path}")
        if not _git_worktree_is_clean(repo_path):
            raise ValueError(f"target worktree is not frozen/clean for {frozen.path}")

        if first_artifact is None:
            first_artifact = artifact
            first_run = run
            first_provider = root_provider
            first_base_schema_hash = base_schema_hash
            first_normalized_configuration = normalized_configuration
        else:
            assert (
                first_run is not None
                and first_provider is not None
                and first_base_schema_hash is not None
                and first_normalized_configuration is not None
            )
            for field in ("target", "baseline_revision", "head_revision", "requested_model"):
                if artifact.get(field) != first_artifact.get(field):
                    raise ValueError(f"stage-wide artifact {field} mismatch in {frozen.path}")
            if prompt != first_run.get("prompt"):
                raise ValueError(f"stage-wide prompt mismatch in {frozen.path}")
            if normalized_configuration != first_normalized_configuration:
                raise ValueError(
                    f"stage-wide model/transport configuration mismatch in {frozen.path}"
                )
            if run["conversation"]["actual_models"] != first_run["conversation"]["actual_models"]:
                raise ValueError(f"stage-wide actual-model mismatch in {frozen.path}")
            if root_provider != first_provider:
                raise ValueError(f"stage-wide provider_routing mismatch in {frozen.path}")
            if base_schema_hash != first_base_schema_hash:
                raise ValueError(f"stage-wide base-tool schema mismatch in {frozen.path}")
            if root_endpoint != first_artifact.get("openrouter_base_url"):
                raise ValueError(f"stage-wide OpenRouter endpoint mismatch in {frozen.path}")

        # Adoption is derived here, while both the raw artifact and repository
        # snapshot are still under pre-GT freeze checks. Scoring later consumes
        # this precomputed value and never reinterprets live repository state.
        normalized_run = dict(run)
        normalized_run["_preflight_adoption"] = _adoption_status(
            run,
            repo_path=repo_path,
        )
        identities.add(identity)
        agents.add(agent)
        normalized_runs[frozen.path] = normalized_run

    if control_agent not in agents:
        raise ValueError(f"contemporaneous control agent {control_agent!r} is missing")

    manifests: list[dict[str, Any]] = []
    starts: list[int] = []
    completions: list[int] = []
    for frozen in sorted(
        frozen_artifacts,
        key=lambda item: (
            str(normalized_runs[item.path]["agent"]),
            str(normalized_runs[item.path]["pair_key"]),
        ),
    ):
        run = normalized_runs[frozen.path]
        start = int(frozen.payload["generation_started_at_ns"])
        completion = int(frozen.payload["generation_completed_at_ns"])
        starts.append(start)
        completions.append(completion)
        manifests.append(
            {
                "path": str(frozen.path),
                "sha256": frozen.sha256,
                "size_bytes": frozen.size_bytes,
                "mtime_ns": frozen.mtime_ns,
                "device": frozen.device,
                "inode": frozen.inode,
                "agent": run["agent"],
                "pair_key": run["pair_key"],
                "generation_started_at_ns": start,
                "generation_completed_at_ns": completion,
                "run_generation_started_at_ns": run["generation_started_at_ns"],
                "run_generation_completed_at_ns": run["generation_completed_at_ns"],
                "tools": {
                    "names": list(run["tools"]["names"]),
                    "schema_sha256": run["tools"]["schema_sha256"],
                    "base_names": list(run["tools"]["base_names"]),
                    "base_schema_sha256": run["tools"]["base_schema_sha256"],
                },
            }
        )
    assert (
        first_artifact is not None and first_run is not None and stage_protocol_version is not None
    )
    return (
        target_cache,
        {
            "passed_before_ground_truth": True,
            "observed_protocol_version": stage_protocol_version,
            "artifact_count": len(frozen_artifacts),
            "agent_count": len(agents),
            "agents": sorted(agents),
            "control_agent": control_agent,
            "provider_routing": first_provider,
            "openrouter_base_url": first_artifact["openrouter_base_url"],
            "transport_safety": {
                "response_body_byte_limit": first_run["configuration"]["transport"][
                    "response_body_byte_limit"
                ],
                "classification": "transport liveness/safety, not an Agent budget",
            },
            "requested_model": first_artifact["requested_model"],
            "actual_model": first_run["conversation"]["actual_models"][0],
            "model_configuration": first_run["configuration"]["model"],
            "prompt": first_run["prompt"],
            "artifact_manifest": manifests,
            "batch_timing": {
                "earliest_generation_start_ns": min(starts),
                "latest_generation_completion_ns": max(completions),
                "launch_spread_seconds": round((max(starts) - min(starts)) / 1e9, 6),
                "batch_makespan_seconds": round((max(completions) - min(starts)) / 1e9, 6),
            },
        },
        normalized_runs,
    )


def _read_ground_truth_bytes(path: Path) -> tuple[bytes, int]:
    before = path.stat()
    raw = path.read_bytes()
    after = path.stat()
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ) or len(raw) != after.st_size:
        raise ValueError("ground truth changed while it was being read")
    return raw, after.st_mtime_ns


def _portfolio_evidence_valid(
    repo_path: Path,
    findings: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Require both verbatim doc evidence and a valid HEAD source path:line."""

    valid: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    root = repo_path.resolve()
    for index, finding in enumerate(findings):
        doc_valid, doc_invalid = _evidence_valid(repo_path, [finding])
        if doc_invalid:
            item = dict(doc_invalid[0])
            item["index"] = index
            invalid.append(item)
            continue
        assert doc_valid
        evidence = str(finding.get("code_evidence", ""))
        matches = list(_SOURCE_REFERENCE.finditer(evidence))
        if not matches:
            invalid.append(
                {
                    "index": index,
                    "doc": finding.get("doc"),
                    "line": finding.get("line"),
                    "reason": "head_source_reference_missing",
                }
            )
            continue
        accepted = False
        for match in matches:
            relative = match.group("path")
            source = (root / relative).resolve()
            if (
                source == root
                or root not in source.parents
                or source.suffix.lower() in _DOCUMENT_EXTENSIONS
                or not source.is_file()
            ):
                continue
            start = int(match.group("start"))
            end = int(match.group("end") or start)
            total_lines = max(
                1,
                len(source.read_text(encoding="utf-8", errors="replace").splitlines()),
            )
            if start <= end <= total_lines:
                accepted = True
                break
        if not accepted:
            invalid.append(
                {
                    "index": index,
                    "doc": finding.get("doc"),
                    "line": finding.get("line"),
                    "reason": "head_source_reference_invalid",
                }
            )
            continue
        valid.append(dict(finding))
    return valid, invalid


def _score_run(
    run: dict[str, Any],
    *,
    process_seconds: float,
    artifact_snapshot_overhead_seconds: float,
    end_to_end_seconds: float,
    repo_path: Path,
    items: list[dict[str, Any]],
    window: int,
    classes: dict[str, str],
    spans: dict[str, list[tuple[int, int]]],
) -> dict[str, Any]:
    delivered = [dict(item) for item in run.get("delivered", []) if isinstance(item, dict)]
    evidence_valid, invalid_evidence = _portfolio_evidence_valid(repo_path, delivered)
    primary = _score_channel(
        evidence_valid,
        items=items,
        window=window,
        classes=classes,
        spans=spans,
    )
    usage = run["usage"]
    timing = run["timing"]
    actual_tokens = int(usage["input_tokens"]) + int(usage["output_tokens"])
    hit_count = len(primary["hits"])
    runner_total_seconds = float(timing["total_seconds"])
    total_seconds = end_to_end_seconds
    adoption = run.get("_preflight_adoption")
    if not isinstance(adoption, dict):
        raise ValueError("run lacks its frozen preflight adoption result")
    adoption_passed = all(item["passed"] for item in adoption.values())
    conversation = run["conversation"]
    conversation_ok = conversation["ok"] is True
    reliable = conversation_ok and adoption_passed

    def optional_token_total(name: str) -> int | None:
        values = [
            entry.get(name) for entry in conversation["model_call_trace"] if isinstance(entry, dict)
        ]
        observed = [
            value for value in values if isinstance(value, int) and not isinstance(value, bool)
        ]
        return sum(observed) if observed else None

    return {
        "agent": run["agent"],
        "pair_key": run["pair_key"],
        "conversation_ok": conversation_ok,
        "failure_reason": conversation.get("failure_reason") or None,
        "agent_failure": (conversation.get("failure_reason") or "") in AGENT_FAILURE_REASONS,
        "evidence_valid_delivered": primary,
        "invalid_evidence": invalid_evidence,
        "adoption": adoption,
        "reliability": {
            "eligible": reliable,
            "conversation_completed": conversation_ok,
            "adoption_passed": adoption_passed,
        },
        "tools": {
            "names": list(run["tools"]["names"]),
            "schema_sha256": run["tools"]["schema_sha256"],
            "base_names": list(run["tools"]["base_names"]),
            "base_schema_sha256": run["tools"]["base_schema_sha256"],
        },
        "metrics": {
            "evidence_valid_hits": hit_count,
            "extras": int(primary["extras"]),
            "invalid_evidence": len(invalid_evidence),
            "model_calls": int(usage["model_calls"]),
            "input_tokens": int(usage["input_tokens"]),
            "output_tokens": int(usage["output_tokens"]),
            "actual_tokens": actual_tokens,
            "cached_tokens": optional_token_total("cached_tokens"),
            "reasoning_tokens": optional_token_total("reasoning_tokens"),
            "cost_usd": float(usage["estimated_cost_usd"]),
            "setup_seconds": float(timing["setup_seconds"]),
            "agent_seconds": float(timing["agent_seconds"]),
            "cleanup_seconds": float(timing["cleanup_seconds"]),
            "runner_total_seconds": runner_total_seconds,
            "process_seconds": process_seconds,
            "artifact_snapshot_overhead_seconds": artifact_snapshot_overhead_seconds,
            "end_to_end_seconds": total_seconds,
            "total_seconds": total_seconds,
            "hits_per_minute": (
                round(hit_count * 60 / total_seconds, 6) if total_seconds > 0 else None
            ),
            "hits_per_100k_tokens": (
                round(hit_count * 100_000 / actual_tokens, 6) if actual_tokens > 0 else None
            ),
        },
        "tool_counts": dict(sorted(conversation["tool_counts"].items())),
    }


def _distribution(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"mean": None, "stdev": None, "min": None, "max": None}
    return {
        "mean": round(statistics.mean(values), 6),
        "stdev": round(statistics.pstdev(values), 6),
        "min": round(min(values), 6),
        "max": round(max(values), 6),
    }


def _arm_summary(agent: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    metric_names = (
        "evidence_valid_hits",
        "extras",
        "invalid_evidence",
        "model_calls",
        "input_tokens",
        "output_tokens",
        "actual_tokens",
        "cost_usd",
        "setup_seconds",
        "agent_seconds",
        "cleanup_seconds",
        "runner_total_seconds",
        "process_seconds",
        "artifact_snapshot_overhead_seconds",
        "end_to_end_seconds",
        "total_seconds",
    )
    distributions = {
        name: _distribution([float(row["metrics"][name]) for row in rows]) for name in metric_names
    }
    optional_token_distributions = {
        name: _distribution(
            [float(row["metrics"][name]) for row in rows if row["metrics"][name] is not None]
        )
        for name in ("cached_tokens", "reasoning_tokens")
    }
    hit_sets = [set(row["evidence_valid_delivered"]["hits"]) for row in rows]
    union = set().union(*hit_sets) if hit_sets else set()
    aggregate_tool_counts: Counter[str] = Counter()
    for row in rows:
        aggregate_tool_counts.update(row["tool_counts"])

    adoption_summary: dict[str, Any] = {}
    for tool in OPTIONAL_ADOPTION_RULES:
        required = [row["adoption"][tool] for row in rows if row["adoption"][tool]["required"]]
        adoption_summary[tool] = {
            "required": bool(required),
            "runs_required": len(required),
            "runs_passed": sum(bool(item["passed"]) for item in required),
            "successful_calls": _distribution(
                [float(item["successful_calls"]) for item in required]
            ),
            "passed": all(item["passed"] for item in required),
        }

    reliability_reasons: list[str] = []
    failed_outcomes = [row["pair_key"] for row in rows if not row["conversation_ok"]]
    if failed_outcomes:
        reliability_reasons.append("agent_terminal_failure")
    failed_adoption = [
        row["pair_key"] for row in rows if not bool(row["reliability"]["adoption_passed"])
    ]
    if failed_adoption:
        reliability_reasons.append("required_tool_adoption_failed")
    mean_hits = cast(float, distributions["evidence_valid_hits"]["mean"])
    mean_seconds = cast(float, distributions["total_seconds"]["mean"])
    mean_tokens = cast(float, distributions["actual_tokens"]["mean"])
    return {
        "agent": agent,
        "runs": len(rows),
        "completed_submissions": sum(bool(row["conversation_ok"]) for row in rows),
        "agent_failures": sum(bool(row["agent_failure"]) for row in rows),
        "tools": rows[0]["tools"],
        "metrics": distributions,
        "optional_token_details": optional_token_distributions,
        "union": {
            "hits": sorted(union),
            "count": len(union),
            "union_at_3": H.union_over_repeats(hit_sets, 3),
        },
        "tool_counts": dict(sorted(aggregate_tool_counts.items())),
        "adoption": adoption_summary,
        "reliability": {
            "eligible": not reliability_reasons,
            "reasons": reliability_reasons,
            "failed_outcome_pairs": failed_outcomes,
            "failed_adoption_pairs": failed_adoption,
            "external_failures": 0,
        },
        "efficiency": {
            "hits_per_minute": (
                round(mean_hits * 60 / mean_seconds, 6) if mean_seconds > 0 else None
            ),
            "hits_per_100k_tokens": (
                round(mean_hits * 100_000 / mean_tokens, 6) if mean_tokens > 0 else None
            ),
        },
    }


def _safe_ratio(value: float, denominator: float) -> float | None:
    return round(value / denominator, 6) if denominator > 0 else None


def _optional_distribution(values: list[float | None]) -> dict[str, float | int | None]:
    """Summarize defined pair ratios without hiding zero-control denominators."""

    defined = [value for value in values if value is not None]
    return {
        **_distribution(defined),
        "defined_pairs": len(defined),
        "undefined_pairs": len(values) - len(defined),
    }


def _comparison_stability(
    *,
    treatment_agent: str,
    comparator_agent: str,
    rows_by_pair: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, Any]:
    """Apply the pre-registered paired-repeat stability rule to one comparison."""

    comparisons: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for pair_id, pair_rows in sorted(rows_by_pair.items()):
        treatment = pair_rows.get(treatment_agent)
        comparator = pair_rows.get(comparator_agent)
        if treatment is None or comparator is None:
            raise ValueError(
                f"paired stability comparison is incomplete for {pair_id}: "
                f"{treatment_agent} vs {comparator_agent}"
            )
        comparisons.append((pair_id, treatment, comparator))

    repeats = len(comparisons)
    required_direction_pairs = (
        math.ceil(repeats * STABILITY_DIRECTION_NUMERATOR / STABILITY_DIRECTION_DENOMINATOR)
        if repeats >= STABILITY_MINIMUM_REPEATS
        else None
    )
    hit_deltas = [
        float(treatment["metrics"]["evidence_valid_hits"])
        - float(comparator["metrics"]["evidence_valid_hits"])
        for _pair, treatment, comparator in comparisons
    ]
    time_deltas = [
        float(treatment["metrics"]["end_to_end_seconds"])
        - float(comparator["metrics"]["end_to_end_seconds"])
        for _pair, treatment, comparator in comparisons
    ]
    token_deltas = [
        float(treatment["metrics"]["actual_tokens"])
        - float(comparator["metrics"]["actual_tokens"])
        for _pair, treatment, comparator in comparisons
    ]
    mean_hit_delta = statistics.mean(hit_deltas)
    mean_time_delta = statistics.mean(time_deltas)
    mean_token_delta = statistics.mean(token_deltas)
    positive_hits_pairs = sum(delta > 0 for delta in hit_deltas)
    time_improved_pairs = sum(delta < 0 for delta in time_deltas)
    token_improved_pairs = sum(delta < 0 for delta in token_deltas)
    treatment_adoption_passed = sum(
        bool(treatment["reliability"]["adoption_passed"])
        for _pair, treatment, _comparator in comparisons
    )
    comparator_adoption_passed = sum(
        bool(comparator["reliability"]["adoption_passed"])
        for _pair, _treatment, comparator in comparisons
    )
    treatment_reliable = sum(
        bool(treatment["reliability"]["eligible"])
        for _pair, treatment, _comparator in comparisons
    )
    comparator_reliable = sum(
        bool(comparator["reliability"]["eligible"])
        for _pair, _treatment, comparator in comparisons
    )
    all_reliable = treatment_reliable == repeats and comparator_reliable == repeats
    mean_recall_not_lower = mean_hit_delta >= 0
    mean_time_improved = mean_time_delta < 0
    mean_token_improved = mean_token_delta < 0

    if repeats < STABILITY_MINIMUM_REPEATS:
        status = "insufficient_repeats"
        stable_recall: bool | None = None
        stable_time: bool | None = None
        stable_tokens: bool | None = None
        stable_efficiency: bool | None = None
    else:
        status = "evaluated" if all_reliable else "reliability_failed"
        assert required_direction_pairs is not None
        stable_recall = bool(
            all_reliable
            and mean_hit_delta >= STABILITY_MEAN_HIT_DELTA
            and positive_hits_pairs >= required_direction_pairs
        )
        stable_time = bool(
            all_reliable
            and mean_recall_not_lower
            and mean_time_improved
            and time_improved_pairs >= required_direction_pairs
        )
        stable_tokens = bool(
            all_reliable
            and mean_recall_not_lower
            and mean_token_improved
            and token_improved_pairs >= required_direction_pairs
        )
        stable_efficiency = stable_time or stable_tokens

    return {
        "treatment_agent": treatment_agent,
        "comparator_agent": comparator_agent,
        "repeats": repeats,
        "status": status,
        "thresholds": {
            "minimum_repeats": STABILITY_MINIMUM_REPEATS,
            "stable_recall_minimum_mean_delta_hits": STABILITY_MEAN_HIT_DELTA,
            "same_direction_fraction": (
                f"{STABILITY_DIRECTION_NUMERATOR}/{STABILITY_DIRECTION_DENOMINATOR}"
            ),
            "required_same_direction_pairs": required_direction_pairs,
            "adoption_required": f"{repeats}/{repeats}",
        },
        "mean_delta_treatment_minus_comparator": {
            "evidence_valid_hits": round(mean_hit_delta, 6),
            "end_to_end_seconds": round(mean_time_delta, 6),
            "actual_tokens": round(mean_token_delta, 6),
        },
        "positive_hits_pairs": positive_hits_pairs,
        "time_improved_pairs": time_improved_pairs,
        "token_improved_pairs": token_improved_pairs,
        "mean_recall_not_lower": mean_recall_not_lower,
        "mean_time_improved": mean_time_improved,
        "mean_token_improved": mean_token_improved,
        "reliability": {
            "all_pairs_eligible": all_reliable,
            "treatment_eligible_pairs": treatment_reliable,
            "comparator_eligible_pairs": comparator_reliable,
            "treatment_adoption_passed_pairs": treatment_adoption_passed,
            "comparator_adoption_passed_pairs": comparator_adoption_passed,
        },
        "stable_recall": stable_recall,
        "stable_efficiency": stable_efficiency,
        "stable_efficiency_by_metric": {
            "end_to_end_seconds": stable_time,
            "actual_tokens": stable_tokens,
        },
    }


def _pareto_frontier(points: dict[str, dict[str, float]]) -> list[str]:
    """Return non-dominated arms for hits(max), wall(min), and tokens(min)."""

    frontier: list[str] = []
    for agent, point in points.items():
        dominated = False
        for other_agent, other in points.items():
            if other_agent == agent:
                continue
            no_worse = (
                other["mean_hits"] >= point["mean_hits"]
                and other["mean_total_seconds"] <= point["mean_total_seconds"]
                and other["mean_actual_tokens"] <= point["mean_actual_tokens"]
            )
            strictly_better = (
                other["mean_hits"] > point["mean_hits"]
                or other["mean_total_seconds"] < point["mean_total_seconds"]
                or other["mean_actual_tokens"] < point["mean_actual_tokens"]
            )
            if no_worse and strictly_better:
                dominated = True
                break
        if not dominated:
            frontier.append(agent)
    return sorted(
        frontier,
        key=lambda name: (
            -points[name]["mean_hits"],
            points[name]["mean_total_seconds"],
            points[name]["mean_actual_tokens"],
            name,
        ),
    )


def score_artifacts(
    artifact_paths: list[Path],
    ground_truth: Path,
    launch_manifest: Path,
    control_agent: str = DEFAULT_CONTROL_AGENT,
) -> dict[str, Any]:
    frozen_manifest = _freeze_launch_manifest(launch_manifest)
    frozen = _freeze_artifacts(artifact_paths)
    launch_audit, launch_jobs = _validate_launch_manifest(
        frozen_manifest,
        frozen,
        control_agent=control_agent,
    )
    target_cache, protocol_audit, runs_by_path = _preflight_artifacts(
        frozen,
        control_agent=control_agent,
    )
    launch_audit["validated_forward_child_tool_deltas"] = (
        _validate_registered_forward_tool_deltas(
            cast(dict[str, str], launch_audit["child_parent_map"]),
            runs_by_path,
        )
    )
    for artifact in frozen:
        run = runs_by_path[artifact.path]
        launch_job = launch_jobs[artifact.path]
        if (
            run.get("agent") != launch_job["agent"]
            or str(run.get("pair_key")) != f"{launch_job['pair_id']}.1"
        ):
            raise ValueError(f"artifact identity disagrees with launch manifest: {artifact.path}")
    _verify_artifacts_remain_frozen(frozen)
    _verify_launch_manifest_remains_frozen(frozen_manifest)

    gt_read_started_at_ns = time.time_ns()
    latest_artifact_time = max(
        manifest_or_artifact_time
        for manifest_or_artifact_time in (
            frozen_manifest.mtime_ns,
            int(launch_audit["artifact_frozen_at_ns"]),
            *(
                max(item.mtime_ns, int(item.payload["generation_completed_at_ns"]))
                for item in frozen
            ),
        )
    )
    if latest_artifact_time >= gt_read_started_at_ns:
        raise ValueError("raw artifact completion is not earlier than the ground-truth read")
    raw_gt, gt_mtime_ns = _read_ground_truth_bytes(ground_truth)
    items, window, classes = _decode_ground_truth(raw_gt)
    gt_docs = {str(item["doc"]) for item in items}

    rows: list[dict[str, Any]] = []
    spans_by_target: dict[str, dict[str, list[tuple[int, int]]]] = {}
    for artifact in frozen:
        descriptor = artifact.payload["target"]
        target_key = json.dumps(descriptor, ensure_ascii=False, sort_keys=True)
        repo_path, _baseline = _target_repo(descriptor, target_cache)
        if target_key not in spans_by_target:
            spans_by_target[target_key] = H.section_spans(repo_path, gt_docs)
        row = _score_run(
            runs_by_path[artifact.path],
            process_seconds=float(launch_jobs[artifact.path]["process_seconds"]),
            artifact_snapshot_overhead_seconds=float(
                launch_jobs[artifact.path]["artifact_snapshot_overhead_seconds"]
            ),
            end_to_end_seconds=float(launch_jobs[artifact.path]["end_to_end_seconds"]),
            repo_path=repo_path,
            items=items,
            window=window,
            classes=classes,
            spans=spans_by_target[target_key],
        )
        row["artifact"] = str(artifact.path)
        row["artifact_sha256"] = artifact.sha256
        rows.append(row)

    rows.sort(key=lambda row: (str(row["agent"]), str(row["pair_key"])))
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["agent"])].append(row)
    arms = {agent: _arm_summary(agent, arm_rows) for agent, arm_rows in sorted(grouped.items())}
    control = arms[control_agent]
    control_hits = float(control["metrics"]["evidence_valid_hits"]["mean"])
    control_wall = float(control["metrics"]["total_seconds"]["mean"])
    control_tokens = float(control["metrics"]["actual_tokens"]["mean"])
    token_limit = control_tokens * TOKEN_GUARDRAIL_MULTIPLIER

    eligible_points: dict[str, dict[str, float]] = {}
    guarded_points: dict[str, dict[str, float]] = {}
    for agent, arm in arms.items():
        mean_hits = float(arm["metrics"]["evidence_valid_hits"]["mean"])
        mean_wall = float(arm["metrics"]["total_seconds"]["mean"])
        mean_tokens = float(arm["metrics"]["actual_tokens"]["mean"])
        ratios = {
            "mean_hits": _safe_ratio(mean_hits, control_hits),
            "mean_total_seconds": _safe_ratio(mean_wall, control_wall),
            "mean_actual_tokens": _safe_ratio(mean_tokens, control_tokens),
        }
        within_guardrail = mean_tokens <= token_limit + 1e-9
        arm["control_ratios"] = ratios
        arm["token_guardrail"] = {
            "multiplier": TOKEN_GUARDRAIL_MULTIPLIER,
            "control_mean_actual_tokens": control_tokens,
            "maximum_mean_actual_tokens": round(token_limit, 6),
            "arm_mean_actual_tokens": mean_tokens,
            "passed": within_guardrail,
        }
        point = {
            "mean_hits": mean_hits,
            "mean_total_seconds": mean_wall,
            "mean_actual_tokens": mean_tokens,
        }
        if arm["reliability"]["eligible"]:
            eligible_points[agent] = point
            if within_guardrail:
                guarded_points[agent] = point

    rows_by_pair: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        pair_id = str(row["pair_key"]).removesuffix(".1")
        rows_by_pair[pair_id][str(row["agent"])] = row
    pair_deltas: list[dict[str, Any]] = []
    pair_ratio_values: dict[str, dict[str, list[float | None]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for pair_id, pair_rows in sorted(rows_by_pair.items()):
        pair_control = pair_rows[control_agent]
        for agent, row in sorted(pair_rows.items()):
            if agent == control_agent:
                continue
            treatment_metrics = row["metrics"]
            control_metrics = pair_control["metrics"]
            pair_ratios = {
                "evidence_valid_hits": _safe_ratio(
                    float(treatment_metrics["evidence_valid_hits"]),
                    float(control_metrics["evidence_valid_hits"]),
                ),
                "end_to_end_seconds": _safe_ratio(
                    float(treatment_metrics["end_to_end_seconds"]),
                    float(control_metrics["end_to_end_seconds"]),
                ),
                "actual_tokens": _safe_ratio(
                    float(treatment_metrics["actual_tokens"]),
                    float(control_metrics["actual_tokens"]),
                ),
            }
            for metric, ratio in pair_ratios.items():
                pair_ratio_values[agent][metric].append(ratio)
            pair_deltas.append(
                {
                    "pair_id": pair_id,
                    "agent": agent,
                    "control_agent": control_agent,
                    "treatment": {
                        "evidence_valid_hits": treatment_metrics["evidence_valid_hits"],
                        "end_to_end_seconds": treatment_metrics["end_to_end_seconds"],
                        "actual_tokens": treatment_metrics["actual_tokens"],
                    },
                    "control": {
                        "evidence_valid_hits": control_metrics["evidence_valid_hits"],
                        "end_to_end_seconds": control_metrics["end_to_end_seconds"],
                        "actual_tokens": control_metrics["actual_tokens"],
                    },
                    "delta_treatment_minus_control": {
                        "evidence_valid_hits": treatment_metrics["evidence_valid_hits"]
                        - control_metrics["evidence_valid_hits"],
                        "end_to_end_seconds": round(
                            treatment_metrics["end_to_end_seconds"]
                            - control_metrics["end_to_end_seconds"],
                            6,
                        ),
                        "actual_tokens": treatment_metrics["actual_tokens"]
                        - control_metrics["actual_tokens"],
                    },
                    "ratio_treatment_over_control": pair_ratios,
                }
            )

    pair_ratio_distributions = {
        agent: {
            metric: _optional_distribution(values)
            for metric, values in sorted(metric_values.items())
        }
        for agent, metric_values in sorted(pair_ratio_values.items())
    }
    stability_vs_control = {
        agent: _comparison_stability(
            treatment_agent=agent,
            comparator_agent=control_agent,
            rows_by_pair=rows_by_pair,
        )
        for agent in sorted(arms)
        if agent != control_agent
    }

    child_parent_map = cast(dict[str, str], launch_audit["child_parent_map"])
    child_parent_deltas: list[dict[str, Any]] = []
    for pair_id, pair_rows in sorted(rows_by_pair.items()):
        for child_agent, parent_agent in sorted(child_parent_map.items()):
            child_metrics = pair_rows[child_agent]["metrics"]
            parent_metrics = pair_rows[parent_agent]["metrics"]
            child_parent_deltas.append(
                {
                    "pair_id": pair_id,
                    "child_agent": child_agent,
                    "parent_agent": parent_agent,
                    "child": {
                        "evidence_valid_hits": child_metrics["evidence_valid_hits"],
                        "end_to_end_seconds": child_metrics["end_to_end_seconds"],
                        "actual_tokens": child_metrics["actual_tokens"],
                    },
                    "parent": {
                        "evidence_valid_hits": parent_metrics["evidence_valid_hits"],
                        "end_to_end_seconds": parent_metrics["end_to_end_seconds"],
                        "actual_tokens": parent_metrics["actual_tokens"],
                    },
                    "delta_child_minus_parent": {
                        "evidence_valid_hits": child_metrics["evidence_valid_hits"]
                        - parent_metrics["evidence_valid_hits"],
                        "end_to_end_seconds": round(
                            child_metrics["end_to_end_seconds"]
                            - parent_metrics["end_to_end_seconds"],
                            6,
                        ),
                        "actual_tokens": child_metrics["actual_tokens"]
                        - parent_metrics["actual_tokens"],
                    },
                    "ratio_child_over_parent": {
                        "evidence_valid_hits": _safe_ratio(
                            float(child_metrics["evidence_valid_hits"]),
                            float(parent_metrics["evidence_valid_hits"]),
                        ),
                        "end_to_end_seconds": _safe_ratio(
                            float(child_metrics["end_to_end_seconds"]),
                            float(parent_metrics["end_to_end_seconds"]),
                        ),
                        "actual_tokens": _safe_ratio(
                            float(child_metrics["actual_tokens"]),
                            float(parent_metrics["actual_tokens"]),
                        ),
                    },
                }
            )
    stability_vs_parent = {
        child_agent: _comparison_stability(
            treatment_agent=child_agent,
            comparator_agent=parent_agent,
            rows_by_pair=rows_by_pair,
        )
        for child_agent, parent_agent in sorted(child_parent_map.items())
    }

    return {
        "protocol_version": protocol_audit["observed_protocol_version"],
        "protocol_audit": {
            **protocol_audit,
            "launch_manifest": launch_audit,
            "gt_read_started_at_ns": gt_read_started_at_ns,
            "all_artifacts_completed_before_gt_read": all(
                item.mtime_ns < gt_read_started_at_ns
                and int(item.payload["generation_completed_at_ns"]) < gt_read_started_at_ns
                for item in frozen
            ),
            "launch_manifest_frozen_before_gt_read": (
                frozen_manifest.mtime_ns < gt_read_started_at_ns
            ),
        },
        "ground_truth": {
            "path": str(ground_truth.resolve()),
            "sha256": hashlib.sha256(raw_gt).hexdigest(),
            "size_bytes": len(raw_gt),
            "mtime_ns": gt_mtime_ns,
            "items": len(items),
        },
        "primary_metric": "evidence-valid delivered recall",
        "failure_policy": (
            "submit_schema_invalid, submit_not_solitary, and no_tool_call are retained "
            "as zero-delivery Agent "
            "outcomes and make their arm reliability-ineligible; any model/provider failure "
            "invalidates the complete stage before ground truth is read"
        ),
        "runs": rows,
        "pair_deltas": pair_deltas,
        "pair_ratio_distributions": pair_ratio_distributions,
        "stability_vs_control": stability_vs_control,
        "child_parent_map": child_parent_map,
        "child_parent_deltas": child_parent_deltas,
        "stability_vs_parent": stability_vs_parent,
        "summary": {
            "control_agent": control_agent,
            "arms": arms,
            "pareto_objectives": {
                "maximize": "mean evidence-valid delivered hits",
                "minimize": [
                    "mean manifest start-to-artifact-snapshot end_to_end_seconds",
                    "mean actual_tokens",
                ],
            },
            "pareto_frontier": _pareto_frontier(eligible_points),
            "token_guardrail_pareto_frontier": _pareto_frontier(guarded_points),
            "reliability_eligible_agents": sorted(eligible_points),
            "token_guardrail_eligible_agents": sorted(guarded_points),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Score an arbitrary stage of one-run single-Agent tool-portfolio artifacts"
    )
    parser.add_argument("--artifact", type=Path, action="append", required=True)
    parser.add_argument("--launch-manifest", type=Path, required=True)
    parser.add_argument("--ground-truth", type=Path, required=True)
    parser.add_argument("--control-agent", default=DEFAULT_CONTROL_AGENT)
    parser.add_argument("--output", type=Path, default=None)
    arguments = parser.parse_args()

    report = score_artifacts(
        arguments.artifact,
        arguments.ground_truth,
        arguments.launch_manifest,
        control_agent=arguments.control_agent,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=1)
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(rendered, encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()

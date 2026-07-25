"""GT-free CodeGraph exact-symbol source plus upstream-impact candidate.

The model sees one tool.  After it has found a concrete symbol in the
baseline-to-HEAD diff, the handler starts CodeGraph 1.5.0 ``node`` (whose CLI
face includes the exact symbol body) and ``impact --depth 3`` concurrently.
Both native stdout/stderr streams are returned after only isolated-clone path
sanitization; the wrapper never slices, summarizes, or projects provider text.

This is deliberately not an ordered-path tool.  CodeGraph 1.5.0's ``impact``
CLI also has no file-disambiguation flag, so an optional ``file`` pins the
``node`` query while impact remains the provider's aggregate over every exact
same-named definition.  Those limits are explicit in both the schema and the
result envelope.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from typing import Any

import _portfolio_native_graph as native
from _graph_runtime import (
    CODEGRAPH_PACKAGE_INTEGRITY,
    CODEGRAPH_VERSION,
    _assert_visible_repo_isolated,
    _binary,
    _clone_for_index,
    _command_result,
    _directory_size,
    _metadata,
    _run,
    _sanitized,
)
from _runner import BASE_TOOLS, AgentContext, AgentRuntime

CODEGRAPH_NODE_IMPACT_AGENT = "codegraph_node_impact_agent"
CODEGRAPH_NODE_IMPACT_TOOL = "codegraph_node_impact"
CODEGRAPH_NODE_IMPACT_TOOLS = (CODEGRAPH_NODE_IMPACT_TOOL,)
CODEGRAPH_NODE_IMPACT_MENU = BASE_TOOLS + CODEGRAPH_NODE_IMPACT_TOOLS
CODEGRAPH_NODE_IMPACT_PROFILE = "codegraph_node_impact_parallel"
CODEGRAPH_NODE_IMPACT_PROTOCOL = "codegraph-node-impact-parallel-v1"

_IMPACT_DEPTH = 3
_EXACT_SYMBOL = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_DIFF_IDENTIFIER = re.compile(r"(?<![A-Za-z0-9_$])([A-Za-z_$][A-Za-z0-9_$]*)(?![A-Za-z0-9_$])")
_PROVIDER_TRIM_NOTICE = re.compile(
    r"(?:output\s+truncated|truncated\s+to\s+budget|trimmed\s+for\s+size|"
    r"sections?\s+were\s+trimmed|not\s+shown\s+above)",
    re.IGNORECASE,
)
_SOURCE_SUFFIXES = frozenset(
    {
        ".c",
        ".cc",
        ".clj",
        ".cljs",
        ".cpp",
        ".cs",
        ".cxx",
        ".ex",
        ".exs",
        ".go",
        ".h",
        ".hpp",
        ".java",
        ".js",
        ".jsx",
        ".kt",
        ".kts",
        ".m",
        ".mm",
        ".php",
        ".py",
        ".rb",
        ".rs",
        ".scala",
        ".sh",
        ".sol",
        ".swift",
        ".ts",
        ".tsx",
        ".vue",
    }
)


def _function(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


CODEGRAPH_NODE_IMPACT_DEFINITION = _function(
    CODEGRAPH_NODE_IMPACT_TOOL,
    (
        "After reading the baseline-to-HEAD diff, pass one exact, case-sensitive code "
        "symbol that appeared in a changed source line. Runs CodeGraph 1.5.0 node "
        "(verbatim line-numbered source plus immediate caller/callee trail) and upstream "
        "impact depth=3 concurrently, then returns both complete sanitized native CLI "
        "streams. This tool does not return an ordered call path. Optional file pins the "
        "node source lookup; CodeGraph 1.5.0 CLI impact has no --file option and remains "
        "an aggregate across exact same-named definitions."
    ),
    {
        "symbol": {
            "type": "string",
            "pattern": _EXACT_SYMBOL.pattern,
            "description": (
                "One exact identifier copied from a changed source line in git_diff; "
                "natural-language questions and symbol lists are rejected."
            ),
        },
        "file": {
            "type": "string",
            "description": (
                "Optional changed repo-relative source path containing that symbol. "
                "Pins node source only; upstream impact stays exact-name aggregate."
            ),
        },
    },
    ["symbol"],
)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _source_path(path: str) -> bool:
    return Path(path).suffix.casefold() in _SOURCE_SUFFIXES


def _diff_symbol_inventory(context: AgentContext) -> tuple[dict[str, frozenset[str]], str]:
    """Return case-sensitive identifiers seen on changed source lines, without GT."""

    completed = _run(
        [
            "git",
            "-c",
            "core.quotePath=false",
            "diff",
            "--no-ext-diff",
            "--no-renames",
            "--no-prefix",
            "--unified=0",
            context.baseline_revision,
            context.head_revision,
            "--",
        ],
        cwd=context.repo_path,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "git diff failed"
        raise RuntimeError(detail)

    current_old: str | None = None
    current_file: str | None = None
    paths_by_symbol: dict[str, set[str]] = {}
    for line in completed.stdout.splitlines():
        if line.startswith("--- "):
            candidate = line.removeprefix("--- ")
            current_old = None if candidate == "/dev/null" else candidate
            continue
        if line.startswith("+++ "):
            candidate = line.removeprefix("+++ ")
            current_file = current_old if candidate == "/dev/null" else candidate
            continue
        if (
            current_file is None
            or not _source_path(current_file)
            or not line.startswith(("+", "-"))
            or line.startswith(("+++", "---"))
        ):
            continue
        for match in _DIFF_IDENTIFIER.finditer(line[1:]):
            paths_by_symbol.setdefault(match.group(1), set()).add(current_file)

    frozen = {symbol: frozenset(paths) for symbol, paths in paths_by_symbol.items()}
    return frozen, _sha256_text(completed.stdout)


def _normalize_file(repo: Path, value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("file must be a repository-relative path")
    raw = value.strip().replace("\\", "/")
    if not raw or raw.startswith(("/", "~")) or "\x00" in raw:
        raise ValueError("file must be a repository-relative path")
    path = PurePosixPath(raw)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("file must be a normalized repository-relative path")
    candidate = (repo / path.as_posix()).resolve()
    root = repo.resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("file escapes the repository")
    return candidate.relative_to(root).as_posix()


def _validate_index_status(payload: object, *, graph_repo: Path) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RuntimeError("CodeGraph status JSON must be an object")
    index = payload.get("index")
    pending = payload.get("pendingChanges")
    project_path = payload.get("projectPath")
    index_path = payload.get("indexPath")
    paths_match = (
        isinstance(project_path, str)
        and isinstance(index_path, str)
        and Path(project_path).resolve() == graph_repo.resolve()
        and Path(index_path).resolve() == (graph_repo / ".codegraph").resolve()
    )
    pending_clean = isinstance(pending, dict) and all(
        pending.get(kind) == 0 for kind in ("added", "modified", "removed")
    )
    index_complete = (
        isinstance(index, dict)
        and index.get("state") == "complete"
        and index.get("builtWithVersion") == CODEGRAPH_VERSION
        and index.get("reindexRecommended") is False
        and index.get("pendingRefs") == 0
    )
    if (
        payload.get("initialized") is not True
        or payload.get("version") != CODEGRAPH_VERSION
        or payload.get("worktreeMismatch") is not None
        or not paths_match
        or not pending_clean
        or not index_complete
    ):
        raise RuntimeError("CodeGraph index status is not cleanly bound to the isolated clone")
    return payload


def _stream_record(stdout: str, stderr: str) -> dict[str, Any]:
    return {
        "stdout_chars": len(stdout),
        "stdout_sha256": _sha256_text(stdout),
        "stderr_chars": len(stderr),
        "stderr_sha256": _sha256_text(stderr),
        "output_chars": len(stdout) + len(stderr),
        "output_sha256": _sha256_text(_canonical_json({"stderr": stderr, "stdout": stdout})),
        "provider_reported_truncation": bool(
            _PROVIDER_TRIM_NOTICE.search(stdout) or _PROVIDER_TRIM_NOTICE.search(stderr)
        ),
    }


def _invoke_provider(
    *,
    operation: str,
    argv: list[str],
    argv_semantic_args: list[str],
    semantic_arguments: dict[str, Any],
    cwd: Path,
    env: dict[str, str],
    graph_repo: Path,
    binding_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    started = time.monotonic()
    completed = _run(argv, cwd=cwd, env=env)
    seconds = time.monotonic() - started
    stdout = _sanitized(completed.stdout, graph_repo)
    stderr = _sanitized(completed.stderr, graph_repo)
    streams = _stream_record(stdout, stderr)
    record = {
        "provider": "codegraph",
        "operation": operation,
        "argv_semantic_args": argv_semantic_args,
        "semantic_arguments": semantic_arguments,
        "seconds": round(seconds, 6),
        "exit_code": completed.returncode,
        "error": completed.returncode != 0,
        "package_version": CODEGRAPH_VERSION,
        "index_binding_sha256": binding_sha256,
        "complete_sanitized_streams": True,
        "wrapper_truncation": False,
        **streams,
    }
    result = {
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": completed.returncode,
        "error": completed.returncode != 0,
        "provider_reported_truncation": streams["provider_reported_truncation"],
    }
    return result, record


def codegraph_node_impact_runtime(context: AgentContext) -> AgentRuntime:
    """Build one isolated index and expose one exact-symbol composite tool."""

    _assert_visible_repo_isolated(context)
    inventory, diff_sha256 = _diff_symbol_inventory(context)
    binary, binary_digest = _binary("CODEGRAPH_BIN", "codegraph", CODEGRAPH_VERSION)
    graph_repo, clone_seconds = _clone_for_index(context, "codegraph")
    env = dict(os.environ)
    env.update({"DO_NOT_TRACK": "1", "NO_COLOR": "1", "CODEGRAPH_NO_DOWNLOAD": "1"})
    setup_calls: list[dict[str, Any]] = []
    try:
        index_started = time.monotonic()
        initialized = _run([str(binary), "init", str(graph_repo)], cwd=graph_repo, env=env)
        index_seconds = time.monotonic() - index_started
        initialized_output = _command_result(initialized, graph_repo=graph_repo)
        setup_calls.append(
            native._setup_call_record(
                operation="init",
                output=initialized_output,
                exit_code=initialized.returncode,
                seconds=index_seconds,
            )
        )
        if initialized.returncode != 0:
            raise RuntimeError(initialized_output.removeprefix("ERROR: "))
        index_dir = graph_repo / ".codegraph"

        status_started = time.monotonic()
        status = _run(
            [str(binary), "status", "--json", str(graph_repo)],
            cwd=graph_repo,
            env=env,
        )
        status_seconds = time.monotonic() - status_started
        status_output = _command_result(status, graph_repo=graph_repo)
        setup_calls.append(
            native._setup_call_record(
                operation="status",
                output=status_output,
                exit_code=status.returncode,
                seconds=status_seconds,
            )
        )
        if status.returncode != 0:
            raise RuntimeError(status_output.removeprefix("ERROR: "))
        try:
            raw_status: object = json.loads(status.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError("CodeGraph status did not return valid JSON") from error
        validated_status = _validate_index_status(raw_status, graph_repo=graph_repo)
        sanitized_status: object = json.loads(_sanitized(status.stdout, graph_repo))
    except Exception:
        native._safe_cleanup_after_setup_failure(graph_repo, "codegraph")
        raise

    metadata = _metadata(
        provider="codegraph",
        context=context,
        package_version=CODEGRAPH_VERSION,
        binary_sha256=binary_digest,
        isolation_clone_seconds=clone_seconds,
        index_seconds=index_seconds,
        index_success=index_dir.is_dir(),
        index_size_bytes=_directory_size(index_dir),
        index_stats=sanitized_status,
    )
    index_binding = {
        "provider": "codegraph",
        "package_version": CODEGRAPH_VERSION,
        "binary_sha256": binary_digest,
        "source_head": context.head_revision,
        "source_tree": metadata["source_tree"],
        "isolated_clone_head_matches_source": True,
        "isolated_clone_tree_matches_source": True,
        "index_relative_path": ".codegraph",
        "status_initialized": validated_status.get("initialized"),
        "status_version": validated_status.get("version"),
        "index_state": validated_status.get("index", {}).get("state"),
        "index_built_with_version": validated_status.get("index", {}).get(
            "builtWithVersion"
        ),
        "pending_changes": validated_status.get("pendingChanges"),
        "pending_refs": validated_status.get("index", {}).get("pendingRefs"),
        "worktree_mismatch": validated_status.get("worktreeMismatch"),
    }
    binding_sha256 = _sha256_text(_canonical_json(index_binding))
    metadata.update(
        {
            "telemetry_disabled": True,
            "update_checks_disabled": True,
            "package_integrity": CODEGRAPH_PACKAGE_INTEGRITY,
            "implementation_mode": "native-node-source-plus-impact-depth3-parallel",
            "candidate_protocol": CODEGRAPH_NODE_IMPACT_PROTOCOL,
            "impact_depth": _IMPACT_DEPTH,
            "ordered_path_available": False,
            "file_disambiguation": {
                "node": True,
                "impact": False,
                "impact_reason": "CodeGraph 1.5.0 CLI impact has no --file option",
            },
            "diff_symbol_guard": {
                "source": "baseline_to_head_changed_source_lines",
                "case_sensitive": True,
                "exact_single_identifier_only": True,
                "diff_sha256": diff_sha256,
                "eligible_symbol_count": len(inventory),
                "changed_source_paths": sorted(
                    {path for paths in inventory.values() for path in paths}
                ),
                "ground_truth_used": False,
            },
            "index_binding": index_binding,
            "index_binding_sha256": binding_sha256,
            "setup_calls": setup_calls,
            "provider_calls": [],
        }
    )

    def node_impact(arguments: dict[str, Any]) -> str:
        unexpected = set(arguments) - {"symbol", "file"}
        if unexpected:
            raise ValueError(
                f"unexpected {CODEGRAPH_NODE_IMPACT_TOOL} arguments: {sorted(unexpected)}"
            )
        raw_symbol = arguments.get("symbol")
        if not isinstance(raw_symbol, str):
            raise ValueError("symbol must be one exact identifier from a changed source line")
        symbol = raw_symbol.strip()
        if not _EXACT_SYMBOL.fullmatch(symbol):
            raise ValueError("symbol must be one exact identifier, not a question or list")
        matching_paths = inventory.get(symbol)
        if not matching_paths:
            raise ValueError(
                "symbol must appear case-sensitively on a baseline-to-HEAD changed source line"
            )
        file = _normalize_file(context.repo_path, arguments.get("file"))
        if file is not None and file not in matching_paths:
            raise ValueError("file must be a changed source path where the exact symbol appeared")

        node_semantics: dict[str, Any] = {
            "symbol": symbol,
            "include_source": True,
            "relationship_scope": "immediate_callers_and_callees",
        }
        node_argv = [str(binary), "node", "--path", str(graph_repo)]
        node_semantic_argv = ["node", "--path", "."]
        if file is not None:
            node_semantics["file"] = file
            node_argv.extend(("--file", file))
            node_semantic_argv.extend(("--file", file))
        node_argv.append(symbol)
        node_semantic_argv.append(symbol)

        impact_semantics = {
            "symbol": symbol,
            "depth": _IMPACT_DEPTH,
            "direction": "upstream_dependents",
            "definition_scope": "all_exact_same_named_definitions",
            "file_disambiguation_applied": False,
        }
        impact_argv = [
            str(binary),
            "impact",
            "--path",
            str(graph_repo),
            "--depth",
            str(_IMPACT_DEPTH),
            symbol,
        ]
        impact_semantic_argv = [
            "impact",
            "--path",
            ".",
            "--depth",
            str(_IMPACT_DEPTH),
            symbol,
        ]

        _assert_visible_repo_isolated(context)
        combined_started = time.monotonic()
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="codegraph-node-impact") as pool:
            node_future = pool.submit(
                _invoke_provider,
                operation="node_include_source",
                argv=node_argv,
                argv_semantic_args=node_semantic_argv,
                semantic_arguments=node_semantics,
                cwd=graph_repo,
                env=env,
                graph_repo=graph_repo,
                binding_sha256=binding_sha256,
            )
            impact_future = pool.submit(
                _invoke_provider,
                operation="impact_upstream_depth3",
                argv=impact_argv,
                argv_semantic_args=impact_semantic_argv,
                semantic_arguments=impact_semantics,
                cwd=graph_repo,
                env=env,
                graph_repo=graph_repo,
                binding_sha256=binding_sha256,
            )
            node_result, node_record = node_future.result()
            impact_result, impact_record = impact_future.result()
        combined_seconds = time.monotonic() - combined_started
        _assert_visible_repo_isolated(context)

        invocation = len(metadata["query_calls"]) + 1
        for provider_record in (node_record, impact_record):
            metadata["provider_calls"].append(
                {
                    **provider_record,
                    "tool": CODEGRAPH_NODE_IMPACT_TOOL,
                    "composite_invocation": invocation,
                }
            )
        payload = {
            "protocol": CODEGRAPH_NODE_IMPACT_PROTOCOL,
            "query": {"symbol": symbol, **({"file": file} if file is not None else {})},
            "semantics": {
                "ordered_path_available": False,
                "ordered_path_notice": (
                    "This composite returns exact source/trail plus upstream blast radius, "
                    "not an ordered source-to-target call path."
                ),
                "node_file_disambiguated": file is not None,
                "impact_depth": _IMPACT_DEPTH,
                "impact_direction": "upstream_dependents",
                "impact_file_disambiguated": False,
                "impact_definition_scope": "all_exact_same_named_definitions",
            },
            "results": {
                "node_include_source": node_result,
                "upstream_impact_depth3": impact_result,
            },
            "transport": {
                "complete_sanitized_stdout_stderr": True,
                "sanitization": "isolated_clone_path_only",
                "wrapper_truncation": False,
                "provider_truncation_notices_preserved": True,
            },
        }
        rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        provider_seconds = node_record["seconds"] + impact_record["seconds"]
        metadata["query_calls"].append(
            {
                "provider": "codegraph",
                "tool": CODEGRAPH_NODE_IMPACT_TOOL,
                "operation": "node_plus_upstream_impact_parallel",
                "arguments": dict(arguments),
                "normalized_arguments": payload["query"],
                "matching_diff_paths": sorted(matching_paths),
                "combined_seconds": round(combined_seconds, 6),
                "provider_seconds_sum": round(provider_seconds, 6),
                "parallel_overlap_seconds": round(max(0.0, provider_seconds - combined_seconds), 6),
                "provider_call_count": 2,
                "provider_call_operations": [
                    "node_include_source",
                    "impact_upstream_depth3",
                ],
                "execution_mode": "parallel_native_cli_subprocesses",
                "ordered_path_available": False,
                "output_chars": len(rendered),
                "output_sha256": _sha256_text(rendered),
                "error": bool(node_record["error"] or impact_record["error"]),
                "provider_reported_truncation": bool(
                    node_record["provider_reported_truncation"]
                    or impact_record["provider_reported_truncation"]
                ),
                "complete_sanitized_provider_outputs": True,
                "wrapper_truncation": False,
                "package_version": CODEGRAPH_VERSION,
                "index_binding_sha256": binding_sha256,
            }
        )
        return rendered

    runtime = native._compose_with_paged_generic(
        context=context,
        graph_repo=graph_repo,
        provider="codegraph",
        tool=CODEGRAPH_NODE_IMPACT_TOOL,
        definition=CODEGRAPH_NODE_IMPACT_DEFINITION,
        handler=node_impact,
        metadata=metadata,
    )
    runtime.metadata.update(
        {
            "profile_id": CODEGRAPH_NODE_IMPACT_PROFILE,
            "tool_surface": [CODEGRAPH_NODE_IMPACT_TOOL],
            "output_transport": {
                "complete_provider_stdout_stderr": True,
                "pagination": False,
                "wrapper_truncation": False,
                "provider_internal_truncation_possible": True,
                "provider_truncation_notices_preserved": True,
                "projection": False,
                "sanitization": "isolated_clone_path_only",
            },
            "runtime_composition": {
                "isolated_clone_count": 1,
                "codegraph_index_count": 1,
                "generic_runtime_count": 1,
                "cleanup_callback_count": 1,
                "provider_queries_share_index": True,
            },
        }
    )
    return runtime


__all__ = [
    "CODEGRAPH_NODE_IMPACT_AGENT",
    "CODEGRAPH_NODE_IMPACT_DEFINITION",
    "CODEGRAPH_NODE_IMPACT_MENU",
    "CODEGRAPH_NODE_IMPACT_PROFILE",
    "CODEGRAPH_NODE_IMPACT_PROTOCOL",
    "CODEGRAPH_NODE_IMPACT_TOOL",
    "CODEGRAPH_NODE_IMPACT_TOOLS",
    "codegraph_node_impact_runtime",
]

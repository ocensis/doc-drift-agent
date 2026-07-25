"""Native graph-tool runtimes for the protocol-v3 portfolio experiment.

These treatments intentionally expose a narrow provider-native operation instead
of projecting both providers into the v2 ``graph_context`` abstraction.  Provider
indexes are still built in isolated clones, while the model-visible repository
keeps the same resumable generic tools as the other portfolio arms.

The provider output is returned whole.  The only output transformation is
replacement of the private isolated-clone path with ``.``; no source sections,
provider notices, or relationship text are removed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from _graph_runtime import (
    CODEGRAPH_PACKAGE_INTEGRITY,
    CODEGRAPH_VERSION,
    GITNEXUS_PACKAGE_INTEGRITY,
    GITNEXUS_VERSION,
    _assert_visible_repo_isolated,
    _binary,
    _cleanup_callback,
    _clone_for_index,
    _command_result,
    _directory_size,
    _metadata,
    _run,
    _sanitized,
)
from _portfolio_generic import paged_generic_runtime
from _runner import BASE_TOOLS, AgentContext, AgentRuntime, ExtraTools

CODEGRAPH_EXPLORE_DIRECT_AGENT = "codegraph_explore_direct_agent"
GITNEXUS_CHANGE_IMPACT_AGENT = "gitnexus_change_impact_agent"

CODEGRAPH_EXPLORE_TOOL = "codegraph_explore"
GITNEXUS_CHANGE_IMPACT_TOOL = "gitnexus_change_impact"
CODEGRAPH_EXPLORE_DIRECT_TOOLS = (CODEGRAPH_EXPLORE_TOOL,)
GITNEXUS_CHANGE_IMPACT_TOOLS = (GITNEXUS_CHANGE_IMPACT_TOOL,)

CODEGRAPH_EXPLORE_DIRECT_MENU = BASE_TOOLS + CODEGRAPH_EXPLORE_DIRECT_TOOLS
GITNEXUS_CHANGE_IMPACT_MENU = BASE_TOOLS + GITNEXUS_CHANGE_IMPACT_TOOLS

_CODEGRAPH_MAX_FILES = 20
_GITNEXUS_CHANGED_SYMBOL_LIMIT = 500
_CODEGRAPH_TRIM_NOTICE = re.compile(
    r"(?:truncat(?:e|ed|ion)|trimmed|omitted|not shown|more files?|max[- ]files?)",
    re.IGNORECASE,
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


CODEGRAPH_EXPLORE_DIRECT_DEFINITION = _function(
    CODEGRAPH_EXPLORE_TOOL,
    (
        "Ask CodeGraph 1.5.0 to explore the current-HEAD graph using a natural-language "
        "question or concrete symbols. The complete native CLI response is returned, "
        "including any source, call-flow, blast-radius, or provider scope notices. This "
        "tool has no baseline diff or documentation alignment."
    ),
    {
        "query": {"type": "string", "minLength": 1},
        "max_files": {
            "type": "integer",
            "minimum": 1,
            "maximum": _CODEGRAPH_MAX_FILES,
            "description": (
                "Optional maximum source-bearing files (1-20). Omit it to preserve "
                "CodeGraph's project-size-adaptive native default."
            ),
        },
    },
    ["query"],
)

GITNEXUS_CHANGE_IMPACT_DEFINITION = _function(
    GITNEXUS_CHANGE_IMPACT_TOOL,
    (
        "Map the fixed baseline-to-HEAD comparison onto GitNexus 1.6.9 indexed symbols "
        "and affected execution flows. This tool takes no arguments: the runtime fixes "
        "scope=compare, the benchmark baseline revision, and limit=500. The complete "
        "native CLI response is returned."
    ),
    {},
    [],
)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _safe_cleanup_after_setup_failure(graph_repo: Path, provider: str) -> None:
    """Best-effort cleanup that preserves the original setup exception."""

    try:
        _cleanup_callback(graph_repo, {}, provider)()
    except Exception:
        pass


def _setup_call_record(
    *,
    operation: str,
    output: str,
    exit_code: int,
    seconds: float,
) -> dict[str, Any]:
    return {
        "operation": operation,
        "seconds": round(seconds, 6),
        "exit_code": exit_code,
        "output_chars": len(output),
        "output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
        "error": exit_code != 0,
    }


def _record_native_call(
    *,
    provider: str,
    tool: str,
    operation: str,
    argv: list[str],
    cwd: Path,
    env: dict[str, str],
    graph_repo: Path,
    metadata: dict[str, Any],
    model_arguments: dict[str, Any],
    provider_arguments: dict[str, Any],
    markers: Callable[[str], dict[str, Any]],
) -> str:
    """Invoke one native query and bind its complete sanitized output to metadata."""

    started = time.monotonic()
    completed = _run(argv, cwd=cwd, env=env)
    seconds = time.monotonic() - started
    rendered = _command_result(completed, graph_repo=graph_repo)
    output_sha256 = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    shared = {
        "provider": provider,
        "tool": tool,
        "operation": operation,
        "seconds": round(seconds, 6),
        "exit_code": completed.returncode,
        "output_chars": len(rendered),
        "output_sha256": output_sha256,
        "error": completed.returncode != 0,
        **markers(rendered),
    }
    metadata["provider_calls"].append(
        {
            **shared,
            "arguments": dict(provider_arguments),
        }
    )
    metadata["query_calls"].append(
        {
            **shared,
            "arguments": dict(model_arguments),
            "provider_arguments": dict(provider_arguments),
        }
    )
    return rendered


def _compose_with_paged_generic(
    *,
    context: AgentContext,
    graph_repo: Path,
    provider: str,
    tool: str,
    definition: dict[str, Any],
    handler: Callable[[dict[str, Any]], str],
    metadata: dict[str, Any],
) -> AgentRuntime:
    """Add one native provider tool to the common resumable generic runtime."""

    base_runtime: AgentRuntime | None = None
    try:
        base_runtime = paged_generic_runtime(context)
        base_profile_id = base_runtime.metadata.get("profile_id")
        base_dependencies = list(base_runtime.metadata.get("dependencies", []))
        for key, value in base_runtime.metadata.items():
            metadata.setdefault(key, value)
        graph_dependency = (
            f"{provider}:{metadata['package_version']}:{metadata['binary_sha256']}"
        )
        dependencies = [*base_dependencies, graph_dependency]
        metadata.update(
            {
                "base_profile_id": base_profile_id,
                "profile_id": f"{provider}_native",
                "dependencies": dependencies,
                "dependency_sha256": hashlib.sha256(
                    _canonical_json(dependencies).encode("utf-8")
                ).hexdigest(),
                "tool_surface": [tool],
                "output_transport": {
                    "complete_provider_output": True,
                    "pagination": False,
                    "wrapper_truncation": False,
                    "provider_internal_truncation_possible": True,
                    "projection": False,
                    "sanitization": "isolated_clone_path_only",
                },
            }
        )
        extras: ExtraTools = dict(base_runtime.extra_tools)
        extras[tool] = (definition, handler)
        _assert_visible_repo_isolated(context)
        provider_close = _cleanup_callback(graph_repo, metadata, provider)
    except Exception:
        _safe_cleanup_after_setup_failure(graph_repo, provider)
        if base_runtime is not None and base_runtime.close is not None:
            base_runtime.close()
        raise

    def close() -> None:
        try:
            provider_close()
        finally:
            if base_runtime.close is not None:
                base_runtime.close()

    return AgentRuntime(
        toolbox=base_runtime.toolbox,
        extra_tools=extras,
        finalize=base_runtime.finalize,
        metadata=metadata,
        close=close,
    )


def _codegraph_markers(output: str) -> dict[str, Any]:
    lowered = output.casefold()
    return {
        "contains_source_code": (
            "source code" in lowered
            or bool(re.search(r"(?m)^\*\*`[^`]+`\*\*", output))
            or bool(re.search(r"(?m)^\d+\t", output))
        ),
        "contains_blast_radius": "blast radius" in lowered,
        "contains_trim_notice": bool(_CODEGRAPH_TRIM_NOTICE.search(output)),
    }


def _gitnexus_change_markers(output: str) -> dict[str, Any]:
    lowered = output.casefold()
    return {
        "contains_changed_symbols": "changed symbols:" in lowered,
        "contains_affected_execution_flows": "affected execution flows:" in lowered,
    }


def codegraph_explore_direct_runtime(context: AgentContext) -> AgentRuntime:
    """Expose CodeGraph's native ``explore`` output without the v2 projection gate."""

    _assert_visible_repo_isolated(context)
    binary, digest = _binary("CODEGRAPH_BIN", "codegraph", CODEGRAPH_VERSION)
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
            _setup_call_record(
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
        status = _run([str(binary), "status", "--json", str(graph_repo)], cwd=graph_repo, env=env)
        status_seconds = time.monotonic() - status_started
        status_output = _command_result(status, graph_repo=graph_repo)
        setup_calls.append(
            _setup_call_record(
                operation="status",
                output=status_output,
                exit_code=status.returncode,
                seconds=status_seconds,
            )
        )
        if status.returncode != 0:
            raise RuntimeError(status_output.removeprefix("ERROR: "))
        try:
            status_payload: object = json.loads(_sanitized(status.stdout, graph_repo))
        except json.JSONDecodeError:
            status_payload = status_output
    except Exception:
        _safe_cleanup_after_setup_failure(graph_repo, "codegraph")
        raise

    metadata = _metadata(
        provider="codegraph",
        context=context,
        package_version=CODEGRAPH_VERSION,
        binary_sha256=digest,
        isolation_clone_seconds=clone_seconds,
        index_seconds=index_seconds,
        index_success=index_dir.is_dir(),
        index_size_bytes=_directory_size(index_dir),
        index_stats=status_payload,
    )
    metadata.update(
        {
            "telemetry_disabled": True,
            "update_checks_disabled": True,
            "package_integrity": CODEGRAPH_PACKAGE_INTEGRITY,
            "implementation_mode": "native-explore-direct",
            "upstream_max_files": _CODEGRAPH_MAX_FILES,
            "upstream_default_max_files": "adaptive_by_project_size",
            "setup_calls": setup_calls,
            "provider_calls": [],
        }
    )

    def explore(arguments: dict[str, Any]) -> str:
        unexpected = set(arguments) - {"query", "max_files"}
        if unexpected:
            raise ValueError(f"unexpected codegraph_explore arguments: {sorted(unexpected)}")
        query = str(arguments.get("query", "")).strip()
        if not query:
            raise ValueError("query must be non-empty")
        raw_max_files = arguments.get("max_files")
        max_files: int | None = None
        if raw_max_files is not None:
            if isinstance(raw_max_files, bool):
                raise ValueError("max_files must be an integer from 1 to 20")
            try:
                max_files = int(raw_max_files)
            except (TypeError, ValueError) as error:
                raise ValueError("max_files must be an integer from 1 to 20") from error
            if not 1 <= max_files <= _CODEGRAPH_MAX_FILES:
                raise ValueError("max_files must be an integer from 1 to 20")
        model_arguments = dict(arguments)
        provider_arguments: dict[str, Any] = {"query": query}
        argv = [str(binary), "explore", "--path", str(graph_repo)]
        if max_files is not None:
            provider_arguments["max_files"] = max_files
            argv.extend(("--max-files", str(max_files)))
        argv.append(query)
        _assert_visible_repo_isolated(context)
        rendered = _record_native_call(
            provider="codegraph",
            tool=CODEGRAPH_EXPLORE_TOOL,
            operation="explore",
            argv=argv,
            cwd=graph_repo,
            env=env,
            graph_repo=graph_repo,
            metadata=metadata,
            model_arguments=model_arguments,
            provider_arguments=provider_arguments,
            markers=_codegraph_markers,
        )
        _assert_visible_repo_isolated(context)
        return rendered

    return _compose_with_paged_generic(
        context=context,
        graph_repo=graph_repo,
        provider="codegraph",
        tool=CODEGRAPH_EXPLORE_TOOL,
        definition=CODEGRAPH_EXPLORE_DIRECT_DEFINITION,
        handler=explore,
        metadata=metadata,
    )


def gitnexus_change_impact_runtime(context: AgentContext) -> AgentRuntime:
    """Expose a fixed baseline-to-HEAD GitNexus ``detect-changes`` comparison."""

    _assert_visible_repo_isolated(context)
    binary, digest = _binary("GITNEXUS_BIN", "gitnexus", GITNEXUS_VERSION)
    graph_repo, clone_seconds = _clone_for_index(context, "gitnexus")
    if (graph_repo / ".gitnexusrc").exists():
        _safe_cleanup_after_setup_failure(graph_repo, "gitnexus")
        raise RuntimeError("benchmark graph clone contains .gitnexusrc; fixed config required")
    gitnexus_home = graph_repo.parent / "home"
    gitnexus_home.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update(
        {
            "GITNEXUS_HOME": str(gitnexus_home),
            "GITNEXUS_LBUG_EXTENSION_INSTALL": "load-only",
            "GITNEXUS_LOG_LEVEL": "error",
            "NO_COLOR": "1",
        }
    )
    setup_calls: list[dict[str, Any]] = []
    try:
        index_started = time.monotonic()
        initialized = _run(
            [str(binary), "analyze", str(graph_repo), "--index-only", "--no-stats"],
            cwd=graph_repo,
            env=env,
        )
        index_seconds = time.monotonic() - index_started
        initialized_output = _command_result(initialized, graph_repo=graph_repo)
        setup_calls.append(
            _setup_call_record(
                operation="analyze",
                output=initialized_output,
                exit_code=initialized.returncode,
                seconds=index_seconds,
            )
        )
        if initialized.returncode != 0:
            raise RuntimeError(initialized_output.removeprefix("ERROR: "))
        index_dir = graph_repo / ".gitnexus"
        try:
            meta_payload = json.loads(
                _sanitized(
                    (index_dir / "meta.json").read_text(encoding="utf-8"),
                    graph_repo,
                )
            )
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError("GitNexus index metadata is missing or invalid") from error
        capabilities = meta_payload.get("capabilities", {})
        graph_status = capabilities.get("graph", {}).get("status")
        fts_status = capabilities.get("fts", {}).get("status")
        stats = meta_payload.get("stats", {})
        if (
            meta_payload.get("lastCommit") != context.head_revision
            or graph_status != "available"
            or fts_status != "available"
            or stats.get("embeddings") != 0
        ):
            raise RuntimeError("GitNexus index capabilities are incomplete")
    except Exception:
        _safe_cleanup_after_setup_failure(graph_repo, "gitnexus")
        raise

    metadata = _metadata(
        provider="gitnexus",
        context=context,
        package_version=GITNEXUS_VERSION,
        binary_sha256=digest,
        isolation_clone_seconds=clone_seconds,
        index_seconds=index_seconds,
        index_success=index_dir.is_dir(),
        index_size_bytes=_directory_size(index_dir),
        index_stats={
            "lastCommit": meta_payload.get("lastCommit"),
            "stats": stats,
            "capabilities": capabilities,
            "schemaVersion": meta_payload.get("schemaVersion"),
        },
    )
    fixed_provider_arguments = {
        "scope": "compare",
        "base_ref": context.baseline_revision,
        "limit": _GITNEXUS_CHANGED_SYMBOL_LIMIT,
    }
    metadata.update(
        {
            "registry_home_isolated": True,
            "wrapper_read_only_allowlist": True,
            "fts_status": fts_status,
            "graph_status": graph_status,
            "fts_extension_policy": "load-only",
            "embeddings_enabled": False,
            "gitnexus_config_present": False,
            "gitnexus_ignore_present": (graph_repo / ".gitnexusignore").exists(),
            "package_integrity": GITNEXUS_PACKAGE_INTEGRITY,
            "implementation_mode": "native-detect-changes-fixed-compare",
            "fixed_provider_arguments": fixed_provider_arguments,
            "setup_calls": setup_calls,
            "provider_calls": [],
        }
    )

    def change_impact(arguments: dict[str, Any]) -> str:
        if arguments:
            raise ValueError("gitnexus_change_impact takes no arguments")
        _assert_visible_repo_isolated(context)
        rendered = _record_native_call(
            provider="gitnexus",
            tool=GITNEXUS_CHANGE_IMPACT_TOOL,
            operation="detect_changes",
            argv=[
                str(binary),
                "detect-changes",
                "--scope",
                "compare",
                "--base-ref",
                context.baseline_revision,
                "--limit",
                str(_GITNEXUS_CHANGED_SYMBOL_LIMIT),
            ],
            cwd=graph_repo,
            env=env,
            graph_repo=graph_repo,
            metadata=metadata,
            model_arguments={},
            provider_arguments=fixed_provider_arguments,
            markers=lambda output: {
                **_gitnexus_change_markers(output),
                "scope": "compare",
                "base_ref": context.baseline_revision,
                "limit": _GITNEXUS_CHANGED_SYMBOL_LIMIT,
            },
        )
        _assert_visible_repo_isolated(context)
        return rendered

    return _compose_with_paged_generic(
        context=context,
        graph_repo=graph_repo,
        provider="gitnexus",
        tool=GITNEXUS_CHANGE_IMPACT_TOOL,
        definition=GITNEXUS_CHANGE_IMPACT_DEFINITION,
        handler=change_impact,
        metadata=metadata,
    )


__all__ = [
    "CODEGRAPH_EXPLORE_DIRECT_AGENT",
    "CODEGRAPH_EXPLORE_DIRECT_DEFINITION",
    "CODEGRAPH_EXPLORE_DIRECT_MENU",
    "CODEGRAPH_EXPLORE_DIRECT_TOOLS",
    "GITNEXUS_CHANGE_IMPACT_AGENT",
    "GITNEXUS_CHANGE_IMPACT_DEFINITION",
    "GITNEXUS_CHANGE_IMPACT_MENU",
    "GITNEXUS_CHANGE_IMPACT_TOOLS",
    "codegraph_explore_direct_runtime",
    "gitnexus_change_impact_runtime",
]

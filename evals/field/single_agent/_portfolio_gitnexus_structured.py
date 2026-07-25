"""Official-structured GitNexus change profile for isolated portfolio trials.

GitNexus' direct ``detect-changes`` CLI calls the official local backend and
then formats the result for humans, capping the displayed changed-symbol and
affected-process lists.  This profile imports the pinned package's exported
``LocalBackend`` in a one-shot Node bridge and returns its JSON result directly.
No graph algorithm is copied or reimplemented here.

The provider index lives in the same kind of isolated clone as the other graph
profiles.  The model gets a no-argument tool; ``scope=compare`` and the frozen
baseline revision are bound by the runtime and cannot be supplied or changed by
the model.  The only result transformation is isolated-clone path redaction.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from _graph_runtime import (
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

GITNEXUS_STRUCTURED_CHANGE_AGENT = "gitnexus_structured_change_agent"
GITNEXUS_STRUCTURED_CHANGE_TOOL = "gitnexus_structured_change"
GITNEXUS_STRUCTURED_CHANGE_TOOLS = (GITNEXUS_STRUCTURED_CHANGE_TOOL,)
GITNEXUS_STRUCTURED_CHANGE_MENU = BASE_TOOLS + GITNEXUS_STRUCTURED_CHANGE_TOOLS
GITNEXUS_STRUCTURED_PROFILE_ID = "gitnexus_official_structured_change"

_BACKEND_RELATIVE_PATH = Path("dist/mcp/local/local-backend.js")
_BRIDGE_PATH = Path(__file__).with_name("gitnexus_structured_change_bridge.mjs")


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


GITNEXUS_STRUCTURED_CHANGE_DEFINITION = _function(
    GITNEXUS_STRUCTURED_CHANGE_TOOL,
    (
        "Return GitNexus 1.6.9's complete structured detect_changes result for "
        "the fixed benchmark baseline-to-HEAD comparison. The runtime, not the "
        "model, binds scope=compare and the baseline revision. The JSON preserves "
        "every changed_symbols and affected_processes entry plus any partial or "
        "error signal. Treat it as a lead and verify claims in repository files."
    ),
    {},
    [],
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _package_artifacts(binary: Path) -> dict[str, Any]:
    """Resolve and validate the official package source beside the pinned bin."""

    roots: list[Path] = []
    for candidate in (binary.parent, *binary.parents):
        if candidate not in roots:
            roots.append(candidate)
    for root in roots:
        package_json = root / "package.json"
        if not package_json.is_file():
            continue
        try:
            package = json.loads(package_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if package.get("name") != "gitnexus" or package.get("version") != GITNEXUS_VERSION:
            continue
        if package.get("type") != "module":
            raise RuntimeError("pinned GitNexus package is not an ESM package")
        bin_entry = package.get("bin", {}).get("gitnexus")
        if not isinstance(bin_entry, str) or (root / bin_entry).resolve() != binary:
            raise RuntimeError("GITNEXUS_BIN is not the pinned package's official bin entry")
        backend = root / _BACKEND_RELATIVE_PATH
        if not backend.is_file():
            raise RuntimeError("pinned GitNexus package lacks the official LocalBackend module")
        source = backend.read_text(encoding="utf-8")
        required_source_markers = (
            "export class LocalBackend",
            "case 'detect_changes':",
            "async detectChanges(repo, params)",
            "changed_symbols: changedSymbols",
            "affected_processes: Array.from(affectedProcesses.values())",
        )
        if any(marker not in source for marker in required_source_markers):
            raise RuntimeError("pinned GitNexus LocalBackend source contract is not recognized")
        return {
            "package_root": root,
            "package_json": package_json,
            "backend_module": backend,
            "package_json_sha256": _sha256(package_json),
            "backend_module_sha256": _sha256(backend),
            "package_exports_field": package.get("exports"),
            "official_bin_entry": bin_entry,
        }
    raise RuntimeError(
        "cannot resolve the pinned GitNexus 1.6.9 package from GITNEXUS_BIN"
    )


def _node_binary() -> Path:
    resolved = shutil.which("node")
    if not resolved:
        raise RuntimeError("Node.js is required to invoke GitNexus LocalBackend")
    node = Path(resolved).resolve()
    if not node.is_file():
        raise RuntimeError(f"node does not resolve to a file: {node}")
    return node


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


def _structured_metrics(output: str) -> dict[str, Any]:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return {
            "structured_json": False,
            "provider_error": output.startswith("ERROR:"),
            "partial": None,
            "partial_field_present": False,
            "partial_value_valid": False,
            "changed_symbols_count": None,
            "affected_processes_count": None,
            "summary_changed_count": None,
            "summary_affected_count": None,
            "summary_counts_match_arrays": False,
        }
    if not isinstance(payload, dict):
        return {
            "structured_json": False,
            "provider_error": True,
            "partial": None,
            "partial_field_present": False,
            "partial_value_valid": False,
            "changed_symbols_count": None,
            "affected_processes_count": None,
            "summary_changed_count": None,
            "summary_affected_count": None,
            "summary_counts_match_arrays": False,
        }
    changed = payload.get("changed_symbols")
    affected = payload.get("affected_processes")
    summary = payload.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    changed_count = len(changed) if isinstance(changed, list) else None
    affected_count = len(affected) if isinstance(affected, list) else None
    summary_changed = summary.get("changed_count")
    summary_affected = summary.get("affected_count")
    counts_match = (
        isinstance(summary_changed, int)
        and not isinstance(summary_changed, bool)
        and isinstance(summary_affected, int)
        and not isinstance(summary_affected, bool)
        and changed_count == summary_changed
        and affected_count == summary_affected
    )
    return {
        "structured_json": True,
        "provider_error": bool(payload.get("error")),
        "partial": payload.get("partial") is True,
        "partial_field_present": "partial" in payload,
        "partial_value_valid": (
            "partial" not in payload or isinstance(payload.get("partial"), bool)
        ),
        "changed_symbols_count": changed_count,
        "affected_processes_count": affected_count,
        "summary_changed_count": summary_changed,
        "summary_affected_count": summary_affected,
        "summary_counts_match_arrays": counts_match,
    }


def _safe_cleanup_after_setup_failure(graph_repo: Path) -> None:
    try:
        _cleanup_callback(graph_repo, {}, "gitnexus")()
    except Exception:
        pass


def _compose_with_paged_generic(
    *,
    context: AgentContext,
    graph_repo: Path,
    metadata: dict[str, Any],
    handler: Callable[[dict[str, Any]], str],
) -> AgentRuntime:
    base_runtime: AgentRuntime | None = None
    try:
        base_runtime = paged_generic_runtime(context)
        base_profile_id = base_runtime.metadata.get("profile_id")
        base_dependencies = list(base_runtime.metadata.get("dependencies", []))
        for key, value in base_runtime.metadata.items():
            metadata.setdefault(key, value)
        graph_dependency = (
            f"gitnexus-structured:{metadata['package_version']}:"
            f"{metadata['backend_module_sha256']}"
        )
        dependencies = [*base_dependencies, graph_dependency]
        metadata.update(
            {
                "base_profile_id": base_profile_id,
                "profile_id": GITNEXUS_STRUCTURED_PROFILE_ID,
                "dependencies": dependencies,
                "dependency_sha256": hashlib.sha256(
                    _canonical_json(dependencies).encode("utf-8")
                ).hexdigest(),
                "tool_surface": [GITNEXUS_STRUCTURED_CHANGE_TOOL],
                "output_transport": {
                    "complete_provider_output": True,
                    "structured_json": True,
                    "wrapper_truncation": False,
                    "projection": False,
                    "sanitization": "isolated_clone_path_only",
                },
            }
        )
        extras: ExtraTools = dict(base_runtime.extra_tools)
        extras[GITNEXUS_STRUCTURED_CHANGE_TOOL] = (
            GITNEXUS_STRUCTURED_CHANGE_DEFINITION,
            handler,
        )
        _assert_visible_repo_isolated(context)
        provider_close = _cleanup_callback(graph_repo, metadata, "gitnexus")
    except Exception:
        _safe_cleanup_after_setup_failure(graph_repo)
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


def gitnexus_structured_change_runtime(context: AgentContext) -> AgentRuntime:
    """Expose official LocalBackend ``detect_changes`` as complete JSON."""

    _assert_visible_repo_isolated(context)
    binary, binary_digest = _binary("GITNEXUS_BIN", "gitnexus", GITNEXUS_VERSION)
    artifacts = _package_artifacts(binary)
    node = _node_binary()
    if not _BRIDGE_PATH.is_file():
        raise RuntimeError(f"GitNexus structured bridge is missing: {_BRIDGE_PATH}")

    graph_repo, clone_seconds = _clone_for_index(context, "gitnexus")
    if (graph_repo / ".gitnexusrc").exists():
        _safe_cleanup_after_setup_failure(graph_repo)
        raise RuntimeError("benchmark graph clone contains .gitnexusrc; fixed config required")
    gitnexus_home = graph_repo.parent / "home"
    gitnexus_home.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update(
        {
            "DO_NOT_TRACK": "1",
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
        _safe_cleanup_after_setup_failure(graph_repo)
        raise

    fixed_arguments = {
        "scope": "compare",
        "base_ref": context.baseline_revision,
    }
    metadata = _metadata(
        provider="gitnexus",
        context=context,
        package_version=GITNEXUS_VERSION,
        binary_sha256=binary_digest,
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
    metadata.update(
        {
            "registry_home_isolated": True,
            "fts_extension_policy": "load-only",
            "embeddings_enabled": False,
            "external_service_started": False,
            "interactive_installer_used": False,
            "package_integrity": GITNEXUS_PACKAGE_INTEGRITY,
            "package_json_sha256": artifacts["package_json_sha256"],
            "backend_module": _BACKEND_RELATIVE_PATH.as_posix(),
            "backend_module_sha256": artifacts["backend_module_sha256"],
            "bridge_sha256": _sha256(_BRIDGE_PATH),
            "package_exports_field": artifacts["package_exports_field"],
            "official_bin_entry": artifacts["official_bin_entry"],
            "official_backend_export": "LocalBackend",
            "implementation_mode": "official-local-backend-structured-detect-changes",
            "fixed_provider_arguments": fixed_arguments,
            "runtime_bindings": {"repo": "isolated_index_clone"},
            "model_controlled_provider_arguments": [],
            "provider_limit": None,
            "cli_formatter_used": False,
            "setup_calls": setup_calls,
            "provider_calls": [],
        }
    )

    def structured_change(arguments: dict[str, Any]) -> str:
        if arguments:
            raise ValueError(f"{GITNEXUS_STRUCTURED_CHANGE_TOOL} takes no arguments")
        _assert_visible_repo_isolated(context)
        started = time.monotonic()
        completed = _run(
            [
                str(node),
                str(_BRIDGE_PATH),
                str(artifacts["backend_module"]),
                str(graph_repo),
                context.baseline_revision,
            ],
            cwd=graph_repo,
            env=env,
        )
        seconds = time.monotonic() - started
        rendered = _command_result(completed, graph_repo=graph_repo)
        metrics = _structured_metrics(rendered)
        shared = {
            "provider": "gitnexus",
            "tool": GITNEXUS_STRUCTURED_CHANGE_TOOL,
            "operation": "detect_changes",
            "seconds": round(seconds, 6),
            "exit_code": completed.returncode,
            "output_chars": len(rendered),
            "output_sha256": hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
            "error": completed.returncode != 0,
            **metrics,
        }
        metadata["provider_calls"].append(
            {
                **shared,
                "arguments": dict(fixed_arguments),
                "runtime_bindings": {"repo": "isolated_index_clone"},
            }
        )
        metadata["query_calls"].append(
            {
                **shared,
                "arguments": {},
                "provider_arguments": dict(fixed_arguments),
                "runtime_bindings": {"repo": "isolated_index_clone"},
            }
        )
        _assert_visible_repo_isolated(context)
        return rendered

    return _compose_with_paged_generic(
        context=context,
        graph_repo=graph_repo,
        metadata=metadata,
        handler=structured_change,
    )


__all__ = [
    "GITNEXUS_STRUCTURED_CHANGE_AGENT",
    "GITNEXUS_STRUCTURED_CHANGE_DEFINITION",
    "GITNEXUS_STRUCTURED_CHANGE_MENU",
    "GITNEXUS_STRUCTURED_CHANGE_TOOL",
    "GITNEXUS_STRUCTURED_CHANGE_TOOLS",
    "GITNEXUS_STRUCTURED_PROFILE_ID",
    "gitnexus_structured_change_runtime",
]

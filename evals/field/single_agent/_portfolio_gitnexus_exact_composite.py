"""GitNexus official-structured K=1 exact-composite candidate runtime.

One no-argument tool creates one pinned GitNexus ``LocalBackend`` and keeps it
alive for the whole provider sequence: complete fixed compare ``detect_changes``;
deterministic K=1 exact-UID selection; exact ``context``; bounded upstream
``impact``; and narrowly conditional ``trace`` / process-resource enrichment.
The graph algorithms remain GitNexus' official implementations.

This candidate deliberately has independent names and metadata.  It does not
replace the raw structured profile and is not registered in a portfolio stage
until its protocol/scorer integration is reviewed separately.
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
from _portfolio_gitnexus_structured import (
    _canonical_json,
    _node_binary,
    _package_artifacts,
    _safe_cleanup_after_setup_failure,
    _setup_call_record,
    _sha256,
    _structured_metrics,
)
from _runner import BASE_TOOLS, AgentContext, AgentRuntime, ExtraTools

GITNEXUS_EXACT_COMPOSITE_AGENT = "gitnexus_exact_composite_agent"
GITNEXUS_EXACT_COMPOSITE_TOOL = "gitnexus_exact_composite"
GITNEXUS_EXACT_COMPOSITE_TOOLS = (GITNEXUS_EXACT_COMPOSITE_TOOL,)
GITNEXUS_EXACT_COMPOSITE_MENU = BASE_TOOLS + GITNEXUS_EXACT_COMPOSITE_TOOLS
GITNEXUS_EXACT_COMPOSITE_PROFILE_ID = (
    "gitnexus_official_structured_k1_exact_composite"
)
GITNEXUS_EXACT_COMPOSITE_PROTOCOL_VERSION = (
    "single-agent-tool-portfolio-candidate-gitnexus-k1-exact-composite-v1"
)

_BRIDGE_PROTOCOL_VERSION = "gitnexus-official-structured-k1-exact-composite-v1"
_RESOURCES_RELATIVE_PATH = Path("dist/mcp/resources.js")
_BRIDGE_PATH = Path(__file__).with_name("gitnexus_exact_composite_bridge.mjs")
_EXPECTED_IMPACT_ARGUMENTS = {
    "direction": "upstream",
    "mode": "callgraph",
    "maxDepth": 2,
    "includeTests": False,
    "limit": 8,
    "offset": 0,
    "summaryOnly": False,
}


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


GITNEXUS_EXACT_COMPOSITE_DEFINITION = _function(
    GITNEXUS_EXACT_COMPOSITE_TOOL,
    (
        "Run one fixed GitNexus 1.6.9 baseline-to-HEAD analysis. The result contains "
        "the complete official structured detect_changes payload plus a separately "
        "labelled deterministic K=1 exact-UID enrichment: context, bounded upstream "
        "impact, and only when unambiguous, one trace and one cross-community process "
        "detail. The model supplies no provider arguments. Treat all graph output as "
        "leads and verify repository evidence before submission."
    ),
    {},
    [],
)


def _composite_package_artifacts(binary: Path) -> dict[str, Any]:
    artifacts = _package_artifacts(binary)
    resources = Path(artifacts["package_root"]) / _RESOURCES_RELATIVE_PATH
    if not resources.is_file():
        raise RuntimeError("pinned GitNexus package lacks the official MCP resources module")
    source = resources.read_text(encoding="utf-8")
    if (
        "export async function readResource" not in source
        or "getProcessDetailResource" not in source
    ):
        raise RuntimeError("pinned GitNexus process-resource source contract is not recognized")
    return {
        **artifacts,
        "resources_module": resources,
        "resources_module_sha256": _sha256(resources),
    }


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
            f"gitnexus-exact-composite:{metadata['package_version']}:"
            f"{metadata['backend_module_sha256']}:"
            f"{metadata['bridge_sha256']}"
        )
        dependencies = [*base_dependencies, graph_dependency]
        metadata.update(
            {
                "base_profile_id": base_profile_id,
                "profile_id": GITNEXUS_EXACT_COMPOSITE_PROFILE_ID,
                "dependencies": dependencies,
                "dependency_sha256": hashlib.sha256(
                    _canonical_json(dependencies).encode("utf-8")
                ).hexdigest(),
                "tool_surface": [GITNEXUS_EXACT_COMPOSITE_TOOL],
                "output_transport": {
                    "complete_detect_changes_output": True,
                    "structured_json": True,
                    "impact_bounded_by_provider": True,
                    "context_backend_category_cap": 30,
                    "bridge_telemetry_removed_from_model_output": True,
                    "wrapper_truncation": False,
                    "normalization": "recursive_object_key_sort_arrays_preserved",
                    "sanitization": "isolated_clone_path_only",
                },
            }
        )
        extras: ExtraTools = dict(base_runtime.extra_tools)
        extras[GITNEXUS_EXACT_COMPOSITE_TOOL] = (
            GITNEXUS_EXACT_COMPOSITE_DEFINITION,
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


def _validate_provider_call(call: object, *, expected_index: int) -> dict[str, Any]:
    if not isinstance(call, dict):
        raise RuntimeError("GitNexus composite provider ledger entry is not an object")
    if call.get("call_index") != expected_index:
        raise RuntimeError("GitNexus composite provider ledger order is invalid")
    operation = call.get("operation")
    if operation not in {
        "detect_changes",
        "context",
        "impact",
        "trace",
        "process_resource",
    }:
        raise RuntimeError("GitNexus composite provider ledger operation is invalid")
    arguments = call.get("arguments")
    if not isinstance(arguments, dict):
        raise RuntimeError("GitNexus composite provider arguments are invalid")
    if call.get("runtime_bindings") != {"repo": "isolated_index_clone"}:
        raise RuntimeError("GitNexus composite provider repo binding is invalid")
    seconds = call.get("seconds")
    output_chars = call.get("output_chars")
    digest = call.get("output_sha256")
    if (
        not isinstance(seconds, (int, float))
        or isinstance(seconds, bool)
        or seconds < 0
        or not isinstance(output_chars, int)
        or isinstance(output_chars, bool)
        or output_chars < 0
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
    ):
        raise RuntimeError("GitNexus composite provider metrics are invalid")
    if not isinstance(call.get("partial_value_valid"), bool):
        raise RuntimeError("GitNexus composite provider partial signal is invalid")
    if not isinstance(call.get("pagination_field_present"), bool):
        raise RuntimeError("GitNexus composite provider pagination signal is invalid")
    return dict(call)


def _validate_bridge_payload(
    payload: object,
    *,
    baseline_revision: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not isinstance(payload, dict):
        raise RuntimeError("GitNexus composite bridge did not return an object")
    if payload.get("protocol_version") != _BRIDGE_PROTOCOL_VERSION:
        raise RuntimeError("GitNexus composite bridge protocol version mismatch")
    if payload.get("normalization") != "recursive_object_key_sort_arrays_preserved":
        raise RuntimeError("GitNexus composite bridge normalization mismatch")
    selection = payload.get("selection")
    enrichment = payload.get("enrichment")
    calls_raw = payload.get("provider_calls")
    if not isinstance(selection, dict) or not isinstance(enrichment, dict):
        raise RuntimeError("GitNexus composite selection/enrichment payload is invalid")
    if not isinstance(calls_raw, list) or not calls_raw:
        raise RuntimeError("GitNexus composite provider ledger is missing")
    calls = [
        _validate_provider_call(call, expected_index=index)
        for index, call in enumerate(calls_raw, start=1)
    ]
    if calls[0]["operation"] != "detect_changes" or calls[0]["arguments"] != {
        "base_ref": baseline_revision,
        "scope": "compare",
    }:
        raise RuntimeError("GitNexus composite detect_changes binding is invalid")
    selected = selection.get("selected")
    if selected is None:
        if len(calls) != 1:
            raise RuntimeError("GitNexus composite enriched without a selected symbol")
    else:
        if not isinstance(selected, dict):
            raise RuntimeError("GitNexus composite selected symbol is invalid")
        uid = selected.get("uid")
        name = selected.get("name")
        if not isinstance(uid, str) or not uid or not isinstance(name, str) or not name:
            raise RuntimeError("GitNexus composite selected symbol lacks exact identity")
        operations = [call["operation"] for call in calls]
        if operations[:3] != ["detect_changes", "context", "impact"]:
            raise RuntimeError("GitNexus composite required provider calls are missing")
        allowed_conditional_sequences = (
            [],
            ["trace"],
            ["process_resource"],
            ["trace", "process_resource"],
        )
        if operations[3:] not in allowed_conditional_sequences:
            raise RuntimeError("GitNexus composite conditional provider calls are invalid")
        if calls[1]["arguments"] != {
            "include_content": False,
            "name": name,
            "uid": uid,
        }:
            raise RuntimeError("GitNexus composite context exact-UID binding is invalid")
        expected_impact = {
            **_EXPECTED_IMPACT_ARGUMENTS,
            "target": name,
            "target_uid": uid,
        }
        if calls[2]["arguments"] != expected_impact:
            raise RuntimeError("GitNexus composite impact binding is invalid")
        if "trace" in operations:
            trace_arguments = calls[operations.index("trace")]["arguments"]
            if trace_arguments.get("to_uid") != uid or trace_arguments.get("to") != name:
                raise RuntimeError("GitNexus composite trace target binding is invalid")
    public_payload = dict(payload)
    public_payload.pop("provider_calls", None)
    return public_payload, calls


def gitnexus_exact_composite_runtime(context: AgentContext) -> AgentRuntime:
    """Prepare the GT-free K=1 exact-composite GitNexus candidate."""

    _assert_visible_repo_isolated(context)
    binary, binary_digest = _binary("GITNEXUS_BIN", "gitnexus", GITNEXUS_VERSION)
    artifacts = _composite_package_artifacts(binary)
    node = _node_binary()
    if not _BRIDGE_PATH.is_file():
        raise RuntimeError(f"GitNexus exact-composite bridge is missing: {_BRIDGE_PATH}")

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

    fixed_detect_arguments = {
        "scope": "compare",
        "base_ref": context.baseline_revision,
    }
    selector_policy = {
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
            "backend_module": "dist/mcp/local/local-backend.js",
            "backend_module_sha256": artifacts["backend_module_sha256"],
            "resources_module": _RESOURCES_RELATIVE_PATH.as_posix(),
            "resources_module_sha256": artifacts["resources_module_sha256"],
            "bridge_sha256": _sha256(_BRIDGE_PATH),
            "official_backend_export": "LocalBackend",
            "official_resource_export": "readResource",
            "implementation_mode": "official-local-backend-k1-exact-composite",
            "fixed_provider_arguments": fixed_detect_arguments,
            "selector_policy": selector_policy,
            "impact_policy": dict(_EXPECTED_IMPACT_ARGUMENTS),
            "runtime_bindings": {"repo": "isolated_index_clone"},
            "model_controlled_provider_arguments": [],
            "cli_formatter_used": False,
            "persistent_backend_scope": "one_composite_tool_invocation",
            "setup_calls": setup_calls,
            "provider_calls": [],
        }
    )

    def exact_composite(arguments: dict[str, Any]) -> str:
        if arguments:
            raise ValueError(f"{GITNEXUS_EXACT_COMPOSITE_TOOL} takes no arguments")
        _assert_visible_repo_isolated(context)
        started = time.monotonic()
        completed = _run(
            [
                str(node),
                str(_BRIDGE_PATH),
                str(artifacts["backend_module"]),
                str(artifacts["resources_module"]),
                str(graph_repo),
                context.baseline_revision,
            ],
            cwd=graph_repo,
            env=env,
        )
        seconds = time.monotonic() - started
        bridge_output = _command_result(completed, graph_repo=graph_repo)
        base_query_record: dict[str, Any] = {
            "provider": "gitnexus",
            "tool": GITNEXUS_EXACT_COMPOSITE_TOOL,
            "operation": "k1_exact_composite",
            "seconds": round(seconds, 6),
            "exit_code": completed.returncode,
            "bridge_output_chars": len(bridge_output),
            "bridge_output_sha256": hashlib.sha256(
                bridge_output.encode("utf-8")
            ).hexdigest(),
            "error": completed.returncode != 0,
            "arguments": {},
            "provider_arguments": dict(fixed_detect_arguments),
            "runtime_bindings": {"repo": "isolated_index_clone"},
        }
        if completed.returncode != 0:
            base_query_record.update(
                {
                    "structured_json": False,
                    "provider_call_count": 0,
                    "result_chars": len(bridge_output),
                    "result_sha256": hashlib.sha256(
                        bridge_output.encode("utf-8")
                    ).hexdigest(),
                }
            )
            metadata["query_calls"].append(base_query_record)
            return bridge_output
        try:
            parsed: object = json.loads(bridge_output)
            public_payload, provider_calls = _validate_bridge_payload(
                parsed,
                baseline_revision=context.baseline_revision,
            )
        except (json.JSONDecodeError, RuntimeError) as error:
            base_query_record.update(
                {
                    "structured_json": False,
                    "validation_error": str(error),
                    "provider_call_count": 0,
                }
            )
            metadata["query_calls"].append(base_query_record)
            raise RuntimeError("GitNexus exact-composite bridge output is invalid") from error

        invocation = len(metadata["query_calls"]) + 1
        for call in provider_calls:
            metadata["provider_calls"].append(
                {**call, "composite_invocation": invocation}
            )
        public_output = json.dumps(
            public_payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        detect_output = json.dumps(
            public_payload.get("detect_changes"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        detect_metrics = _structured_metrics(detect_output)
        selection = public_payload.get("selection")
        enrichment = public_payload.get("enrichment")
        selection = selection if isinstance(selection, dict) else {}
        enrichment = enrichment if isinstance(enrichment, dict) else {}
        selected = selection.get("selected")
        selected = selected if isinstance(selected, dict) else {}
        performed = {
            key: bool(value.get("performed"))
            for key, value in enrichment.items()
            if isinstance(value, dict)
        }
        base_query_record.update(
            {
                "structured_json": True,
                "provider_call_count": len(provider_calls),
                "provider_calls_sha256": hashlib.sha256(
                    _canonical_json(provider_calls).encode("utf-8")
                ).hexdigest(),
                "result_chars": len(public_output),
                "result_sha256": hashlib.sha256(
                    public_output.encode("utf-8")
                ).hexdigest(),
                "detect_metrics": detect_metrics,
                "selection_status": selection.get("status"),
                "selector_policy_version": selection.get("policy_version"),
                "selected_uid": selected.get("uid"),
                "selected_name": selected.get("name"),
                "selected_score": selected.get("score"),
                "eligible_count": selection.get("eligible_count"),
                "rejection_counts": selection.get("rejection_counts"),
                "enrichment_performed": performed,
            }
        )
        metadata["query_calls"].append(base_query_record)
        _assert_visible_repo_isolated(context)
        return public_output

    return _compose_with_paged_generic(
        context=context,
        graph_repo=graph_repo,
        metadata=metadata,
        handler=exact_composite,
    )


__all__ = [
    "GITNEXUS_EXACT_COMPOSITE_AGENT",
    "GITNEXUS_EXACT_COMPOSITE_DEFINITION",
    "GITNEXUS_EXACT_COMPOSITE_MENU",
    "GITNEXUS_EXACT_COMPOSITE_PROFILE_ID",
    "GITNEXUS_EXACT_COMPOSITE_PROTOCOL_VERSION",
    "GITNEXUS_EXACT_COMPOSITE_TOOL",
    "GITNEXUS_EXACT_COMPOSITE_TOOLS",
    "gitnexus_exact_composite_runtime",
]

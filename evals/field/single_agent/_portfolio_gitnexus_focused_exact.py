"""Focused renderer over the GitNexus K=1 exact-composite provider runtime.

The provider-side work is intentionally identical to the raw exact-composite
candidate: complete ``detect_changes`` drives the same deterministic selector,
exact context, bounded impact, and conditional trace/process queries.  This
module changes only the model-visible rendering.  It omits every unselected
detect row while retaining hashes and counts for later audit.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from _portfolio_gitnexus_exact_composite import (
    GITNEXUS_EXACT_COMPOSITE_TOOL,
    gitnexus_exact_composite_runtime,
)
from _runner import BASE_TOOLS, AgentContext, AgentRuntime

GITNEXUS_FOCUSED_EXACT_AGENT = "gitnexus_focused_exact_agent"
GITNEXUS_FOCUSED_EXACT_TOOL = "gitnexus_focused_exact"
GITNEXUS_FOCUSED_EXACT_TOOLS = (GITNEXUS_FOCUSED_EXACT_TOOL,)
GITNEXUS_FOCUSED_EXACT_MENU = BASE_TOOLS + GITNEXUS_FOCUSED_EXACT_TOOLS
GITNEXUS_FOCUSED_EXACT_PROFILE_ID = (
    "gitnexus_official_structured_k1_focused_exact"
)
GITNEXUS_FOCUSED_EXACT_PROTOCOL_VERSION = (
    "single-agent-tool-portfolio-candidate-gitnexus-k1-focused-exact-v1"
)

_RENDER_PROTOCOL_VERSION = "gitnexus-k1-focused-exact-render-v1"
_RENDER_PROFILE = "focused-exact-no-detect-rows-v1"


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


GITNEXUS_FOCUSED_EXACT_DEFINITION = _function(
    GITNEXUS_FOCUSED_EXACT_TOOL,
    (
        "Run the fixed GitNexus 1.6.9 K=1 exact-composite analysis once. The "
        "provider still processes the complete detect_changes result, but this "
        "focused view explicitly omits all changed-symbol and affected-process "
        "rows except the selected K=1 evidence returned inside context, impact, "
        "trace, and process detail. It is not evidence of exhaustive coverage. "
        "The model supplies no provider arguments."
    ),
    {},
    [],
)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _focused_enrichment(enrichment: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Stabilize provider context collections that have set semantics."""

    normalized = copy.deepcopy(enrichment)
    context = normalized.get("context")
    result = context.get("result") if isinstance(context, dict) else None
    if not isinstance(result, dict):
        return normalized, 0

    def relation_key(item: object) -> tuple[str, str, str, str, str]:
        if not isinstance(item, dict):
            return ("", "", "", "", _canonical_json(item))
        return (
            str(item.get("uid", "")),
            str(item.get("name", "")),
            str(item.get("filePath", "")),
            str(item.get("kind", "")),
            _canonical_json(item),
        )

    for direction_name in ("incoming", "outgoing"):
        direction = result.get(direction_name)
        if not isinstance(direction, dict):
            continue
        for rows in direction.values():
            if isinstance(rows, list):
                rows.sort(key=relation_key)

    typed_properties = result.get("typed_properties")
    if isinstance(typed_properties, list):
        typed_properties.sort(key=relation_key)

    processes = result.get("processes")
    if not isinstance(processes, list):
        return normalized, 0

    def process_key(item: object) -> tuple[str, str, str, str, str]:
        if not isinstance(item, dict):
            return ("", "", "", "", _canonical_json(item))
        return (
            str(item.get("name", "")),
            str(item.get("id", "")),
            str(item.get("process_type", "")),
            str(item.get("step_count", "")),
            _canonical_json(item),
        )

    processes.sort(key=process_key)
    return normalized, len(processes)


def _render_focused_payload(
    raw_payload: object,
    *,
    detect_provider_call: object,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Render a focused view and return its non-payload audit facts."""

    if not isinstance(raw_payload, dict):
        raise RuntimeError("GitNexus focused renderer received a non-object payload")
    detect = raw_payload.get("detect_changes")
    selection = raw_payload.get("selection")
    enrichment = raw_payload.get("enrichment")
    if not isinstance(detect, dict):
        raise RuntimeError("GitNexus focused renderer lacks complete detect_changes")
    if not isinstance(selection, dict) or not isinstance(enrichment, dict):
        raise RuntimeError("GitNexus focused renderer lacks selection/enrichment")
    changed = detect.get("changed_symbols")
    processes = detect.get("affected_processes")
    summary = detect.get("summary")
    if (
        not isinstance(changed, list)
        or not isinstance(processes, list)
        or not isinstance(summary, dict)
    ):
        raise RuntimeError("GitNexus focused renderer received incomplete detect arrays")
    changed_count = len(changed)
    process_count = len(processes)
    if (
        summary.get("changed_count") != changed_count
        or summary.get("affected_count") != process_count
    ):
        raise RuntimeError("GitNexus focused renderer detected inconsistent summary counts")

    integrity = selection.get("integrity")
    if not isinstance(integrity, dict) or (
        integrity.get("changed_symbols_count") != changed_count
        or integrity.get("affected_processes_count") != process_count
        or integrity.get("summary_counts_match_arrays") is not True
    ):
        raise RuntimeError("GitNexus focused renderer selector audit is inconsistent")

    if not isinstance(detect_provider_call, dict):
        raise RuntimeError("GitNexus focused renderer lacks the detect provider ledger")
    detect_rendered = _json(detect)
    detect_sha256 = _sha256_text(detect_rendered)
    if (
        detect_provider_call.get("operation") != "detect_changes"
        or detect_provider_call.get("output_chars") != len(detect_rendered)
        or detect_provider_call.get("output_sha256") != detect_sha256
    ):
        raise RuntimeError("GitNexus focused renderer detect payload is not provider-bound")

    selected_raw = selection.get("selected")
    selected = None
    selected_score = None
    if selected_raw is not None:
        if not isinstance(selected_raw, dict):
            raise RuntimeError("GitNexus focused renderer selected identity is invalid")
        selected = {
            key: selected_raw.get(key)
            for key in ("uid", "name", "kind", "filePath")
        }
        selected_score = selected_raw.get("score")

    coverage = {
        "changed_symbols_in_view": 0,
        "processes_in_view": 0,
        "omitted_changed_symbols": changed_count,
        "omitted_processes": process_count,
        "is_exhaustive_repository_coverage": False,
        "notice": (
            "All detect_changes symbol and process rows are omitted from this focused "
            "view. Use repository tools for exhaustive documentation-drift coverage."
        ),
    }
    focused_enrichment, sorted_context_processes = _focused_enrichment(enrichment)
    focused = {
        "protocol_version": _RENDER_PROTOCOL_VERSION,
        "render_profile": _RENDER_PROFILE,
        "detect": {
            "summary": summary,
            "counts": {
                "changed_symbols": changed_count,
                "affected_processes": process_count,
            },
            "coverage": coverage,
        },
        "selection": {
            "status": selection.get("status"),
            "selected": selected,
            "ranking_rationale": {
                "reason": selection.get("reason"),
                "policy_version": selection.get("policy_version"),
                "eligible_count": selection.get("eligible_count"),
                "selected_score": selected_score,
                "ordering": selection.get("ordering", []),
            },
        },
        "enrichment": focused_enrichment,
    }
    audit = {
        "audit_version": "gitnexus-focused-exact-audit-v1",
        "complete_detect_internal": True,
        "complete_detect_model_visible": False,
        "selector_input": "complete_detect_changes_before_render",
        "provider_call_index": detect_provider_call.get("call_index"),
        "provider_output_chars": detect_provider_call.get("output_chars"),
        "provider_output_sha256": detect_provider_call.get("output_sha256"),
        "complete_detect_render_chars": len(detect_rendered),
        "complete_detect_render_sha256": detect_sha256,
        "provider_digest_matches_complete_detect": True,
        "changed_symbols_count": changed_count,
        "affected_processes_count": process_count,
        "summary_counts_match_arrays": True,
        "model_visible_changed_symbol_rows": 0,
        "model_visible_process_rows": 0,
        "omitted_changed_symbols": changed_count,
        "omitted_processes": process_count,
        "selected_uid": selected.get("uid") if selected is not None else None,
        "selected_name": selected.get("name") if selected is not None else None,
        "focused_normalization": {
            "context_processes_sorted": True,
            "context_processes_count": sorted_context_processes,
            "context_processes_order": (
                "name,id,process_type,step_count,canonical_object"
            ),
            "context_relation_arrays_sorted": True,
            "context_relation_order": "uid,name,filePath,kind,canonical_object",
            "context_typed_properties_sorted": True,
            "all_other_provider_arrays_preserved": True,
        },
    }
    return focused, audit


def gitnexus_focused_exact_runtime(context: AgentContext) -> AgentRuntime:
    """Reuse the raw composite provider runtime and replace only its renderer."""

    runtime = gitnexus_exact_composite_runtime(context)
    try:
        _raw_definition, raw_handler = runtime.extra_tools.pop(
            GITNEXUS_EXACT_COMPOSITE_TOOL
        )
    except Exception:
        if runtime.close is not None:
            runtime.close()
        raise

    metadata = runtime.metadata
    composite_profile_id = metadata.get("profile_id")
    renderer_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    dependencies = list(metadata.get("dependencies", []))
    dependencies.append(
        f"gitnexus-focused-exact-renderer:{_RENDER_PROFILE}:{renderer_sha256}"
    )
    output_transport = dict(metadata.get("output_transport", {}))
    output_transport.update(
        {
            "complete_detect_changes_output": False,
            "complete_detect_changes_used_before_render": True,
            "detect_rows_model_visible": 0,
            "focused_rendering": True,
            "cli_formatter_used": False,
        }
    )
    metadata.update(
        {
            "composite_profile_id": composite_profile_id,
            "profile_id": GITNEXUS_FOCUSED_EXACT_PROFILE_ID,
            "render_profile": _RENDER_PROFILE,
            "render_protocol_version": _RENDER_PROTOCOL_VERSION,
            "renderer_sha256": renderer_sha256,
            "dependencies": dependencies,
            "dependency_sha256": _sha256_text(
                json.dumps(
                    dependencies,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            ),
            "tool_surface": [GITNEXUS_FOCUSED_EXACT_TOOL],
            "implementation_mode": (
                "official-local-backend-k1-exact-composite-focused-renderer"
            ),
            "output_transport": output_transport,
            "cli_formatter_used": False,
            "focused_render_audits": [],
        }
    )

    def focused_exact(arguments: dict[str, Any]) -> str:
        if arguments:
            raise ValueError(f"{GITNEXUS_FOCUSED_EXACT_TOOL} takes no arguments")
        query_count = len(metadata["query_calls"])
        raw_output = raw_handler({})
        if len(metadata["query_calls"]) != query_count + 1:
            raise RuntimeError("GitNexus focused renderer query ledger is inconsistent")
        query = metadata["query_calls"][-1]
        if query.get("structured_json") is not True:
            query["render_profile"] = _RENDER_PROFILE
            query["focused_rendered"] = False
            return raw_output

        try:
            raw_payload: object = json.loads(raw_output)
            invocation = len(metadata["query_calls"])
            provider_calls = [
                call
                for call in metadata.get("provider_calls", [])
                if call.get("composite_invocation") == invocation
            ]
            detect_calls = [
                call for call in provider_calls if call.get("operation") == "detect_changes"
            ]
            if len(detect_calls) != 1:
                raise RuntimeError(
                    "GitNexus focused renderer detect provider binding is ambiguous"
                )
            focused_payload, audit = _render_focused_payload(
                raw_payload,
                detect_provider_call=detect_calls[0],
            )
        except (json.JSONDecodeError, RuntimeError) as error:
            query["render_profile"] = _RENDER_PROFILE
            query["focused_rendered"] = False
            query["focused_render_error"] = str(error)
            raise RuntimeError("GitNexus focused rendering failed") from error

        focused_output = _json(focused_payload)
        raw_chars = query.get("result_chars")
        raw_sha256 = query.get("result_sha256")
        audit.update(
            {
                "raw_composite_model_view_chars": raw_chars,
                "raw_composite_model_view_sha256": raw_sha256,
                "focused_model_view_chars": len(focused_output),
                "focused_model_view_sha256": _sha256_text(focused_output),
            }
        )
        metadata["focused_render_audits"].append(dict(audit))
        query.update(
            {
                "render_profile": _RENDER_PROFILE,
                "focused_rendered": True,
                "raw_composite_result_chars": raw_chars,
                "raw_composite_result_sha256": raw_sha256,
                "complete_detect_audit": dict(audit),
                "result_chars": len(focused_output),
                "result_sha256": _sha256_text(focused_output),
            }
        )
        return focused_output

    runtime.extra_tools[GITNEXUS_FOCUSED_EXACT_TOOL] = (
        GITNEXUS_FOCUSED_EXACT_DEFINITION,
        focused_exact,
    )
    return runtime


__all__ = [
    "GITNEXUS_FOCUSED_EXACT_AGENT",
    "GITNEXUS_FOCUSED_EXACT_DEFINITION",
    "GITNEXUS_FOCUSED_EXACT_MENU",
    "GITNEXUS_FOCUSED_EXACT_PROFILE_ID",
    "GITNEXUS_FOCUSED_EXACT_PROTOCOL_VERSION",
    "GITNEXUS_FOCUSED_EXACT_TOOL",
    "GITNEXUS_FOCUSED_EXACT_TOOLS",
    "gitnexus_focused_exact_runtime",
]

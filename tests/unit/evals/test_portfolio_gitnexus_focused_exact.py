from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, cast

import pytest

SINGLE_AGENT_DIR = Path(__file__).resolve().parents[3] / "evals" / "field" / "single_agent"
sys.path.insert(0, str(SINGLE_AGENT_DIR))

import _portfolio_gitnexus_focused_exact as focused  # noqa: E402
import gitnexus_focused_exact_agent  # noqa: E402
import portfolio_gitnexus_focused_exact_control_agent as focused_control  # noqa: E402
from _portfolio_gitnexus_exact_composite import (  # noqa: E402
    GITNEXUS_EXACT_COMPOSITE_TOOL,
)
from _runner import (  # noqa: E402
    BASE_TOOLS,
    TOOL_PORTFOLIO_GITNEXUS_FOCUSED_EXACT_PROTOCOL_VERSION,
    AgentRuntime,
    RepoToolbox,
)


def _payload() -> dict[str, Any]:
    selected_uid = "Function:src/agent/react-executor.ts:runSpecialistLoop"
    selected_name = "runSpecialistLoop"
    detect = {
        "summary": {
            "changed_count": 2,
            "affected_count": 2,
            "changed_files": 2,
            "risk_level": "high",
        },
        "changed_symbols": [
            {
                "id": selected_uid,
                "name": selected_name,
                "filePath": "src/agent/react-executor.ts",
            },
            {
                "id": "Function:src/private.ts:UNSELECTED_SYMBOL_SENTINEL",
                "name": "UNSELECTED_SYMBOL_SENTINEL",
                "filePath": "src/private.ts",
            },
        ],
        "affected_processes": [
            {
                "id": "selected-process",
                "name": "RunSpecialistLoop → GetPermissions",
                "process_type": "cross_community",
                "changed_steps": [{"symbol": selected_name, "step": 1}],
            },
            {
                "id": "UNSELECTED_PROCESS_SENTINEL",
                "name": "UNSELECTED_PROCESS_SENTINEL",
                "process_type": "intra_community",
                "changed_steps": [
                    {"symbol": "UNSELECTED_SYMBOL_SENTINEL", "step": 1}
                ],
            },
        ],
    }
    selected = {
        "uid": selected_uid,
        "name": selected_name,
        "kind": "Function",
        "filePath": "src/agent/react-executor.ts",
        "score": {
            "cross_community_processes": 2,
            "total_processes": 3,
            "changed_step_occurrences": 3,
            "kind_priority": 0,
        },
    }
    enrichment = {
        "context": {
            "performed": True,
            "arguments": {
                "uid": selected_uid,
                "name": selected_name,
                "include_content": False,
            },
            "result": {
                "status": "found",
                "symbol": {"uid": selected_uid, "name": selected_name},
                "incoming": {"calls": []},
                "outgoing": {
                    "calls": [
                        {
                            "uid": "Function:src/z.ts:Zulu",
                            "name": "Zulu",
                            "filePath": "src/z.ts",
                            "kind": "Function",
                        },
                        {
                            "uid": "Function:src/a.ts:Alpha",
                            "name": "Alpha",
                            "filePath": "src/a.ts",
                            "kind": "Function",
                        },
                    ]
                },
                "processes": [
                    {"id": "proc-b", "name": "Zulu", "step_count": 2},
                    {"id": "proc-a", "name": "Alpha", "step_count": 3},
                ],
            },
        },
        "impact": {
            "performed": True,
            "arguments": {"target_uid": selected_uid, "maxDepth": 2},
            "result": {
                "target": {"id": selected_uid, "name": selected_name},
                "byDepth": {"1": [{"name": "createSpecialistNode"}]},
            },
        },
        "trace": {
            "performed": True,
            "result": {"status": "ok", "hopCount": 2},
        },
        "process": {
            "performed": True,
            "selected_process": detect["affected_processes"][0],
            "content": "trace: runSpecialistLoop -> getPermissions",
        },
    }
    return {
        "protocol_version": "gitnexus-official-structured-k1-exact-composite-v1",
        "normalization": "recursive_object_key_sort_arrays_preserved",
        "detect_changes": detect,
        "selection": {
            "policy_version": "k1-cross-community-unique-exact-uid-v1",
            "integrity": {
                "clean": True,
                "changed_symbols_count": 2,
                "affected_processes_count": 2,
                "summary_counts_match_arrays": True,
            },
            "eligible_count": 4,
            "status": "selected",
            "reason": "highest_ranked_eligible_exact_uid",
            "ordering": [
                "cross_community_processes_desc",
                "total_processes_desc",
                "changed_step_occurrences_desc",
                "kind_priority_asc",
                "filePath_asc",
                "uid_asc",
            ],
            "selected": selected,
        },
        "enrichment": enrichment,
    }


def _detect_call(payload: dict[str, Any], *, invocation: int = 1) -> dict[str, Any]:
    rendered = focused._json(payload["detect_changes"])
    return {
        "call_index": 1,
        "operation": "detect_changes",
        "output_chars": len(rendered),
        "output_sha256": hashlib.sha256(rendered.encode()).hexdigest(),
        "composite_invocation": invocation,
    }


def test_focused_agent_has_independent_tool_and_noncoverage_prompt() -> None:
    agent = gitnexus_focused_exact_agent.AGENT

    assert agent.name == "gitnexus_focused_exact_agent"
    assert agent.tools == (*BASE_TOOLS, "gitnexus_focused_exact")
    assert agent.protocol_version == (
        "single-agent-tool-portfolio-candidate-gitnexus-k1-focused-exact-v1"
    )
    assert "not\nan exhaustive candidate list" in agent.system_prompt
    parameters = focused.GITNEXUS_FOCUSED_EXACT_DEFINITION["function"][
        "parameters"
    ]
    assert parameters == {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }


def test_focused_control_locks_protocol_and_byte_identical_prompt() -> None:
    treatment = gitnexus_focused_exact_agent.AGENT
    control = focused_control.AGENT

    assert control.name == "portfolio_gitnexus_focused_exact_control_agent"
    assert control.tools == BASE_TOOLS
    assert control.prepare is not treatment.prepare
    assert control.protocol_version == treatment.protocol_version
    assert control.protocol_version == focused.GITNEXUS_FOCUSED_EXACT_PROTOCOL_VERSION
    assert control.protocol_version == (
        TOOL_PORTFOLIO_GITNEXUS_FOCUSED_EXACT_PROTOCOL_VERSION
    )
    assert control.system_prompt == treatment.system_prompt
    assert len(control.system_prompt) == 2048
    assert hashlib.sha256(control.system_prompt.encode()).hexdigest() == (
        "c08b3e1d69b5e9e2d4af527e632e7956ff86faa4ab7f796c8a9ee57c2bcff51c"
    )


def test_renderer_omits_all_detect_rows_but_preserves_full_k1_enrichment() -> None:
    raw = _payload()
    rendered, audit = focused._render_focused_payload(
        raw,
        detect_provider_call=_detect_call(raw),
    )
    output = focused._json(rendered)

    assert "detect_changes" not in rendered
    assert "changed_symbols" not in rendered["detect"]
    assert "affected_processes" not in rendered["detect"]
    assert "UNSELECTED_SYMBOL_SENTINEL" not in output
    assert "UNSELECTED_PROCESS_SENTINEL" not in output
    coverage = rendered["detect"]["coverage"]
    assert coverage == {
        "changed_symbols_in_view": 0,
        "processes_in_view": 0,
        "omitted_changed_symbols": 2,
        "omitted_processes": 2,
        "is_exhaustive_repository_coverage": False,
        "notice": (
            "All detect_changes symbol and process rows are omitted from this "
            "focused view. Use repository tools for exhaustive documentation-drift "
            "coverage."
        ),
    }
    assert rendered["selection"]["selected"]["uid"].endswith(
        ":runSpecialistLoop"
    )
    assert rendered["selection"]["ranking_rationale"]["selected_score"] == (
        raw["selection"]["selected"]["score"]
    )
    expected_enrichment = json.loads(json.dumps(raw["enrichment"]))
    expected_enrichment["context"]["result"]["processes"].reverse()
    expected_enrichment["context"]["result"]["outgoing"]["calls"].reverse()
    assert rendered["enrichment"] == expected_enrichment
    assert audit["complete_detect_internal"] is True
    assert audit["complete_detect_model_visible"] is False
    assert audit["provider_digest_matches_complete_detect"] is True
    assert audit["changed_symbols_count"] == 2
    assert audit["affected_processes_count"] == 2
    assert audit["model_visible_changed_symbol_rows"] == 0
    assert audit["model_visible_process_rows"] == 0
    assert audit["focused_normalization"] == {
        "context_processes_sorted": True,
        "context_processes_count": 2,
        "context_processes_order": (
            "name,id,process_type,step_count,canonical_object"
        ),
        "context_relation_arrays_sorted": True,
        "context_relation_order": "uid,name,filePath,kind,canonical_object",
        "context_typed_properties_sorted": True,
        "all_other_provider_arrays_preserved": True,
    }

    reordered = _payload()
    reordered["enrichment"]["context"]["result"]["processes"].reverse()
    reordered["enrichment"]["context"]["result"]["outgoing"]["calls"].reverse()
    rendered_again, _audit_again = focused._render_focused_payload(
        reordered,
        detect_provider_call=_detect_call(reordered),
    )
    assert focused._json(rendered_again) == output


def test_runtime_reuses_raw_provider_handler_and_rebinds_result_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _payload()
    raw_output = focused._json(raw)
    metadata: dict[str, Any] = {
        "profile_id": "gitnexus_official_structured_k1_exact_composite",
        "dependencies": ["base", "gitnexus"],
        "dependency_sha256": "a" * 64,
        "tool_surface": [GITNEXUS_EXACT_COMPOSITE_TOOL],
        "output_transport": {"structured_json": True},
        "query_calls": [],
        "provider_calls": [],
    }
    raw_invocations: list[dict[str, Any]] = []

    def raw_handler(arguments: dict[str, Any]) -> str:
        raw_invocations.append(dict(arguments))
        invocation = len(metadata["query_calls"]) + 1
        metadata["provider_calls"].append(
            _detect_call(raw, invocation=invocation)
        )
        metadata["query_calls"].append(
            {
                "structured_json": True,
                "result_chars": len(raw_output),
                "result_sha256": hashlib.sha256(raw_output.encode()).hexdigest(),
            }
        )
        return raw_output

    raw_runtime = AgentRuntime(
        toolbox=cast(RepoToolbox, object()),
        extra_tools={GITNEXUS_EXACT_COMPOSITE_TOOL: ({}, raw_handler)},
        metadata=metadata,
    )
    monkeypatch.setattr(
        focused,
        "gitnexus_exact_composite_runtime",
        lambda _context: raw_runtime,
    )

    runtime = focused.gitnexus_focused_exact_runtime(cast(Any, object()))
    assert GITNEXUS_EXACT_COMPOSITE_TOOL not in runtime.extra_tools
    invoke = runtime.extra_tools["gitnexus_focused_exact"][1]
    output = invoke({})
    public = json.loads(output)

    assert raw_invocations == [{}]
    assert "UNSELECTED_SYMBOL_SENTINEL" not in output
    assert public["detect"]["coverage"]["omitted_changed_symbols"] == 2
    assert runtime.metadata["profile_id"] == (
        "gitnexus_official_structured_k1_focused_exact"
    )
    assert runtime.metadata["composite_profile_id"] == (
        "gitnexus_official_structured_k1_exact_composite"
    )
    assert runtime.metadata["cli_formatter_used"] is False
    assert runtime.metadata["output_transport"]["detect_rows_model_visible"] == 0
    query = runtime.metadata["query_calls"][0]
    assert query["focused_rendered"] is True
    assert query["raw_composite_result_chars"] == len(raw_output)
    assert query["raw_composite_result_sha256"] == hashlib.sha256(
        raw_output.encode()
    ).hexdigest()
    assert query["result_chars"] == len(output)
    assert query["result_sha256"] == hashlib.sha256(output.encode()).hexdigest()
    assert query["complete_detect_audit"] == (
        runtime.metadata["focused_render_audits"][0]
    )
    assert query["complete_detect_audit"]["provider_output_sha256"] == (
        metadata["provider_calls"][0]["output_sha256"]
    )

    with pytest.raises(ValueError, match="takes no arguments"):
        invoke({"limit": 1})
    assert raw_invocations == [{}]


def test_renderer_rejects_detect_payload_not_bound_to_provider_hash() -> None:
    raw = _payload()
    call = _detect_call(raw)
    call["output_sha256"] = "0" * 64

    with pytest.raises(RuntimeError, match="not provider-bound"):
        focused._render_focused_payload(raw, detect_provider_call=call)

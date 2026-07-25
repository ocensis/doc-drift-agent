from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from drift_agent.adapters import mcp as mcp_adapter
from drift_agent.adapters.contracts import PublicBundleV3
from drift_agent.domain.enums import FindingDisposition, RunMode, RunStatus
from drift_agent.domain.models import (
    DriftFinding,
    EvidenceAnchor,
    RunRequest,
    VerifiedRepairBundle,
    WorkspaceSnapshot,
)

PRIVATE_FIELD_NAMES = frozenset({"semantic_analysis", "validation_input_hashes", "symbol_identity"})


def _init_repo(repo: Path) -> None:
    repo.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    (repo / "README.md").write_text("# Test\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-qm", "base"],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _assert_private_field_names_absent(value: object) -> None:
    if isinstance(value, dict):
        assert PRIVATE_FIELD_NAMES.isdisjoint(value)
        for child in value.values():
            _assert_private_field_names_absent(child)
    elif isinstance(value, list):
        for child in value:
            _assert_private_field_names_absent(child)


def _bundle(*, run_id: str, semantic: bool) -> VerifiedRepairBundle:
    return VerifiedRepairBundle(
        status=RunStatus.CLEAN,
        run_id=run_id,
        snapshot=WorkspaceSnapshot(
            head_revision="head",
            workspace_fingerprint=f"workspace-{run_id}",
            input_file_hashes={},
            validation_input_hashes={"private/input.txt": "private-hash"},
        ),
        scope=[],
        findings=[],
        semantic_analysis=semantic,
    )


def _finding(index: int, *, kind: str, reason_code: str, doc_path: str) -> DriftFinding:
    anchor = EvidenceAnchor(
        path="src/demo/api.py",
        line=4 + index,
        source_hash="code-hash",
        start_byte=30,
        end_byte=39,
    )
    return DriftFinding(
        id=f"finding-{index}",
        symbol_id=f"demo.api.symbol_{index}",
        disposition=FindingDisposition.UNRESOLVED,
        truth_source="unknown",
        code_evidence=anchor,
        doc_evidence=EvidenceAnchor(
            path=doc_path,
            line=7 + index,
            source_hash="doc-hash",
            start_byte=50,
            end_byte=72,
        ),
        reason=f"reason {index}",
        kind=kind,
        component_id=f"component-{index}",
        detector_id="structural.signature",
        detector_version="2",
        fingerprint=f"fingerprint-{index}",
        reason_code=reason_code,
    )


def _bundle_with_findings(count: int) -> VerifiedRepairBundle:
    kinds = ["docstring_parameter_changed", "docstring_parameter_changed", "unsupported"]
    reason_codes = ["unsupported.literal", "unsupported.literal", "unsupported.markdown_claim"]
    doc_paths = ["src/demo/api.py", "src/demo/api.py", "docs/plan.md"]
    return VerifiedRepairBundle(
        status=RunStatus.DRIFT_FOUND,
        run_id="run-bounded",
        snapshot=WorkspaceSnapshot(
            head_revision="head",
            workspace_fingerprint="workspace-bounded",
            input_file_hashes={},
        ),
        scope=["src/demo/api.py"],
        findings=[
            _finding(
                index,
                kind=kinds[index % len(kinds)],
                reason_code=reason_codes[index % len(reason_codes)],
                doc_path=doc_paths[index % len(doc_paths)],
            )
            for index in range(count)
        ],
    )


def _structured_result(result: object) -> dict[str, Any]:
    assert isinstance(result, tuple)
    assert len(result) == 2
    structured = result[1]
    assert isinstance(structured, dict)
    return cast(dict[str, Any], structured)


def test_server_lists_only_drift_tools_with_public_v3_output_schema(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    server = mcp_adapter.create_server(tmp_path)

    tools = asyncio.run(server.list_tools())

    assert [tool.name for tool in tools] == ["check_drift", "repair_drift"]
    expected_schema = PublicBundleV3.model_json_schema()
    for tool in tools:
        assert tool.outputSchema == expected_schema
        _assert_private_field_names_absent(tool.outputSchema)
        assert set(tool.inputSchema["properties"]) == {
            "scope",
            "semantic",
            "max_findings",
            "summary_only",
        }
        assert tool.inputSchema["additionalProperties"] is False
        scope_schema = tool.inputSchema["properties"]["scope"]
        assert [branch["required"] for branch in scope_schema["oneOf"]] == [
            ["kind"],
            ["kind", "revision"],
        ]
        assert all(branch["additionalProperties"] is False for branch in scope_schema["oneOf"])


def test_tools_map_bound_server_configuration_to_distinct_run_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    state_dir = tmp_path / "state"
    requests: list[RunRequest] = []
    bundles: list[VerifiedRepairBundle] = []

    def fake_run(request: RunRequest) -> VerifiedRepairBundle:
        requests.append(request)
        bundle = _bundle(
            run_id=f"run-{len(requests)}",
            semantic=request.semantic_analysis or request.semantic_repair,
        )
        bundles.append(bundle)
        return bundle

    monkeypatch.setattr(mcp_adapter.application, "run", fake_run)
    server = mcp_adapter.create_server(
        repo,
        state_dir=state_dir,
        lock_timeout_seconds=1.25,
    )

    async def invoke_tools() -> tuple[object, object]:
        check_result = await server.call_tool(
            "check_drift",
            {
                "scope": {"kind": "since", "revision": "HEAD~2"},
                "semantic": True,
            },
        )
        assert len(requests) == 1
        repair_result = await server.call_tool(
            "repair_drift",
            {
                "scope": {"kind": "changed"},
                "semantic": True,
            },
        )
        assert len(requests) == 2
        return check_result, repair_result

    check_result, repair_result = asyncio.run(invoke_tools())

    assert requests[0] is not requests[1]
    assert all(request.repo_path == repo.resolve() for request in requests)
    assert all(request.state_dir == state_dir.resolve() for request in requests)
    assert all(request.lock_timeout_seconds == 1.25 for request in requests)

    check_request, repair_request = requests
    assert check_request.mode is RunMode.CHECK
    assert check_request.scope.kind == "since"
    assert check_request.scope.revision == "HEAD~2"
    assert check_request.semantic_analysis is True
    assert check_request.semantic_repair is False

    assert repair_request.mode is RunMode.REPAIR
    assert repair_request.scope.kind == "changed"
    assert repair_request.scope.revision is None
    assert repair_request.semantic_analysis is False
    assert repair_request.semantic_repair is True

    assert _structured_result(check_result) == PublicBundleV3.from_bundle(bundles[0]).model_dump(
        mode="json"
    )
    assert _structured_result(repair_result) == PublicBundleV3.from_bundle(bundles[1]).model_dump(
        mode="json"
    )


def test_parser_maps_server_binding_arguments(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    state_dir = tmp_path / "state"

    arguments = mcp_adapter._parser().parse_args(
        [
            "--repo",
            str(repo),
            "--state-dir",
            str(state_dir),
            "--lock-timeout-seconds",
            "2.5",
        ]
    )

    assert arguments.repo == repo
    assert arguments.state_dir == state_dir
    assert arguments.lock_timeout_seconds == 2.5


@pytest.mark.parametrize("value", ["-1", "nan", "inf"])
def test_parser_rejects_non_finite_or_negative_lock_timeout(value: str) -> None:
    with pytest.raises(SystemExit):
        mcp_adapter._parser().parse_args(["--repo", "/tmp/repo", "--lock-timeout-seconds", value])


def test_tool_schema_rejects_missing_or_expansive_inputs_before_application(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    calls: list[RunRequest] = []
    monkeypatch.setattr(mcp_adapter.application, "run", calls.append)
    server = mcp_adapter.create_server(repo)

    async def invoke_invalid_inputs() -> None:
        invalid_payloads = (
            {"semantic": False},
            {"scope": {}},
            {"scope": '{"kind":"changed"}'},
            {"scope": {"kind": "changed"}, "semantic": 1},
            {"scope": {"kind": "changed"}, "repo_path": str(repo)},
            {"scope": {"kind": "changed", "file": "src/demo.py"}},
        )
        for payload in invalid_payloads:
            with pytest.raises(ToolError):
                await server.call_tool("check_drift", payload)

    asyncio.run(invoke_invalid_inputs())

    assert calls == []


@pytest.mark.parametrize("tool", ["check_drift", "repair_drift"])
def test_max_findings_truncates_and_summarizes_structured_output(
    tool: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    monkeypatch.setattr(mcp_adapter.application, "run", lambda request: _bundle_with_findings(3))
    server = mcp_adapter.create_server(repo)

    result = asyncio.run(
        server.call_tool(
            tool,
            {"scope": {"kind": "changed"}, "max_findings": 1},
        )
    )

    structured = _structured_result(result)
    assert [finding["id"] for finding in structured["findings"]] == ["finding-0"]
    assert structured["omitted_findings"] == 2
    summary = structured["findings_summary"]
    assert summary["total_findings"] == 3
    assert summary["by_kind"] == {"docstring_parameter_changed": 2, "unsupported": 1}
    assert summary["by_reason_code"] == {
        "unsupported.literal": 2,
        "unsupported.markdown_claim": 1,
    }
    assert summary["by_doc_path"] == {"src/demo/api.py": 2, "docs/plan.md": 1}
    assert summary["by_disposition"] == {"unresolved": 3}


@pytest.mark.parametrize("tool", ["check_drift", "repair_drift"])
def test_summary_only_inlines_no_findings(
    tool: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    monkeypatch.setattr(mcp_adapter.application, "run", lambda request: _bundle_with_findings(3))
    server = mcp_adapter.create_server(repo)

    result = asyncio.run(
        server.call_tool(
            tool,
            {"scope": {"kind": "changed"}, "summary_only": True},
        )
    )

    structured = _structured_result(result)
    assert structured["findings"] == []
    assert structured["omitted_findings"] == 3
    assert structured["findings_summary"]["total_findings"] == 3


def test_explicit_null_max_findings_removes_the_default_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    count = mcp_adapter.DEFAULT_MAX_FINDINGS + 3
    monkeypatch.setattr(
        mcp_adapter.application, "run", lambda request: _bundle_with_findings(count)
    )
    server = mcp_adapter.create_server(repo)

    capped = asyncio.run(server.call_tool("check_drift", {"scope": {"kind": "changed"}}))
    uncapped = asyncio.run(
        server.call_tool("check_drift", {"scope": {"kind": "changed"}, "max_findings": None})
    )

    capped_structured = _structured_result(capped)
    assert len(capped_structured["findings"]) == mcp_adapter.DEFAULT_MAX_FINDINGS
    assert capped_structured["omitted_findings"] == 3
    uncapped_structured = _structured_result(uncapped)
    assert len(uncapped_structured["findings"]) == count
    assert uncapped_structured["omitted_findings"] == 0
    assert uncapped_structured["findings_summary"] is None


def test_findings_within_default_cap_pass_through_unbounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    monkeypatch.setattr(mcp_adapter.application, "run", lambda request: _bundle_with_findings(3))
    server = mcp_adapter.create_server(repo)

    result = asyncio.run(server.call_tool("check_drift", {"scope": {"kind": "changed"}}))

    structured = _structured_result(result)
    assert len(structured["findings"]) == 3
    assert structured["omitted_findings"] == 0
    assert structured["findings_summary"] is None


def test_unstructured_content_is_compact_mirror_of_structured_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    monkeypatch.setattr(mcp_adapter.application, "run", lambda request: _bundle_with_findings(3))
    server = mcp_adapter.create_server(repo)

    result = asyncio.run(server.call_tool("check_drift", {"scope": {"kind": "changed"}}))

    assert isinstance(result, tuple)
    unstructured, structured = result
    assert len(unstructured) == 1
    text = unstructured[0].text
    assert "\n" not in text
    assert json.loads(text) == structured


def test_tool_schema_rejects_invalid_bounding_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    calls: list[RunRequest] = []
    monkeypatch.setattr(mcp_adapter.application, "run", calls.append)
    server = mcp_adapter.create_server(repo)

    async def invoke_invalid_inputs() -> None:
        invalid_payloads = (
            {"scope": {"kind": "changed"}, "max_findings": -1},
            {"scope": {"kind": "changed"}, "max_findings": 1.5},
            {"scope": {"kind": "changed"}, "max_findings": "10"},
            {"scope": {"kind": "changed"}, "summary_only": 1},
        )
        for payload in invalid_payloads:
            with pytest.raises(ToolError):
                await server.call_tool("check_drift", payload)

    asyncio.run(invoke_invalid_inputs())

    assert calls == []


@pytest.mark.parametrize("tool", ["check_drift", "repair_drift"])
def test_tools_return_config_guidance_for_unconfigured_repo(tool: str, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    server = mcp_adapter.create_server(repo)

    result = asyncio.run(server.call_tool(tool, {"scope": {"kind": "changed"}, "semantic": False}))

    structured = _structured_result(result)
    assert structured["status"] == "failed"
    assert len(structured["validation"]) == 1
    entry = structured["validation"][0]
    assert entry["check"] == "config"
    assert entry["status"] == "unavailable"
    assert entry["summary"].startswith("config.missing:")
    assert "drift-agent init" in entry["summary"]

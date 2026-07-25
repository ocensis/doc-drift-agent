from __future__ import annotations

import json

from drift_agent.domain.enums import FindingDisposition, RunStatus
from drift_agent.domain.models import (
    DriftFinding,
    EvidenceAnchor,
    MemoryEvent,
    RepairGroupOutcome,
    SuppressionRecord,
    SymbolIdentity,
    VerifiedRepairBundle,
    WorkspaceSnapshot,
)
from drift_agent.domain.serialization import bundle_to_json, bundle_to_wire


def _bundle() -> VerifiedRepairBundle:
    finding = DriftFinding(
        id="finding-one",
        symbol_id="demo.api.echo",
        symbol_identity=SymbolIdentity(
            module="demo.api",
            name="echo",
            category="module_function",
        ),
        disposition=FindingDisposition.UNRESOLVED,
        truth_source="unknown",
        code_evidence=EvidenceAnchor(
            path="src/demo/api.py",
            line=1,
            source_hash="code-hash",
            start_byte=4,
            end_byte=8,
        ),
        doc_evidence=EvidenceAnchor(
            path="docs/接口.md",
            line=3,
            source_hash="doc-hash",
            start_byte=20,
            end_byte=24,
        ),
        reason="结构漂移",
        kind="parameter_added",
        component_id="color",
        old_value={"type": "missing_parameter"},
        new_value={"name": "color"},
        detector_id="structural.signature",
        detector_version="2",
        fingerprint="fingerprint-one",
        reason_code="unsupported.literal",
    )
    return VerifiedRepairBundle(
        status=RunStatus.UNRESOLVED,
        run_id="run-one",
        snapshot=WorkspaceSnapshot(
            head_revision="head",
            workspace_fingerprint="workspace-hash",
            input_file_hashes={"docs/接口.md": "doc-hash"},
        ),
        scope=["docs/接口.md"],
        findings=[finding],
        repository_id="repository-one",
        workspace_id="workspace-one",
        suppressed_findings=[
            SuppressionRecord(
                decision_id="decision-one",
                finding_id="finding-other",
                action="ignore",
                reason="accepted",
                actor="human",
                confirmation="human_confirmed",
                evidence_key="evidence",
            )
        ],
        memory_events=[MemoryEvent(kind="decision_invalidated", reason="changed")],
        repair_groups=[
            RepairGroupOutcome(
                id="group-one",
                finding_ids=["finding-one"],
                disposition=FindingDisposition.UNRESOLVED,
                reason_code="unsupported.literal",
            )
        ],
    )


def test_default_v1_projection_is_the_exact_legacy_shape() -> None:
    wire = bundle_to_wire(_bundle())

    assert "schema_version" not in wire
    assert {
        "repository_id",
        "workspace_id",
        "suppressed_findings",
        "memory_events",
        "repair_groups",
        "residual_changes",
    }.isdisjoint(wire)
    assert set(wire["findings"][0]) == {
        "id",
        "symbol_id",
        "type",
        "disposition",
        "truth_source",
        "code_evidence",
        "doc_evidence",
        "reason",
    }
    assert wire["findings"][0]["type"] == "signature_drift"
    assert set(wire["findings"][0]["code_evidence"]) == {
        "path",
        "line",
        "source_hash",
    }
    assert set(wire["findings"][0]["doc_evidence"]) == {
        "path",
        "line",
        "source_hash",
    }


def test_explicit_v2_is_additive_and_preserves_legacy_semantics() -> None:
    bundle = _bundle()
    v1 = bundle_to_wire(bundle, 1)
    v2 = bundle_to_wire(bundle, 2)

    assert v2["schema_version"] == 2
    assert v2["repository_id"] == "repository-one"
    assert v2["workspace_id"] == "workspace-one"
    assert len(v2["suppressed_findings"]) == 1
    assert len(v2["memory_events"]) == 1
    assert len(v2["repair_groups"]) == 1
    assert v2["residual_changes"] == []
    assert v2["status"] == v1["status"]
    assert v2["run_id"] == v1["run_id"]
    assert v2["findings"][0]["id"] == v1["findings"][0]["id"]
    assert v2["findings"][0]["type"] == "signature_drift"
    assert v2["findings"][0]["kind"] == "parameter_added"
    assert v2["findings"][0]["component_id"] == "color"
    assert "symbol_identity" not in v2["findings"][0]


def test_json_output_is_compact_unicode_and_round_trips_each_version() -> None:
    v1 = bundle_to_json(_bundle())
    v2 = bundle_to_json(_bundle(), 2)

    assert "接口" in v1
    assert "结构漂移" in v1
    assert ": " not in v1
    assert json.loads(v1) == bundle_to_wire(_bundle(), 1)
    assert json.loads(v2) == bundle_to_wire(_bundle(), 2)

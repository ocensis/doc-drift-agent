from __future__ import annotations

from typing import cast

import pytest

from drift_agent.domain.enums import FindingDisposition, RunStatus
from drift_agent.domain.models import (
    DriftFinding,
    EvidenceAnchor,
    SemanticScalar,
    SymbolIdentity,
    VerifiedRepairBundle,
    WorkspaceSnapshot,
)
from drift_agent.domain.serialization import (
    UnsupportedWireVersionError,
    WireVersion,
    bundle_to_wire,
)


def _bundle(*, semantic: bool = False) -> VerifiedRepairBundle:
    finding = DriftFinding(
        id="finding-semantic" if semantic else "finding-structural",
        symbol_id="demo.api.answer",
        type="semantic_drift" if semantic else "signature_drift",
        disposition=FindingDisposition.DETECTED,
        truth_source="code",
        code_evidence=EvidenceAnchor(
            path="src/demo/api.py",
            line=4,
            source_hash="code-hash",
            start_byte=30,
            end_byte=39,
        ),
        doc_evidence=EvidenceAnchor(
            path="docs/api.md",
            line=7,
            source_hash="doc-hash",
            start_byte=50,
            end_byte=72,
        ),
        reason=(
            "documented constant return differs from definite code return"
            if semantic
            else "documented signature differs from code"
        ),
        kind="semantic_direct_mismatch" if semantic else "parameter_added",
        component_id="return.literal" if semantic else "timeout",
        old_value=(
            {
                "predicate": "return_literal",
                "mode": "direct",
                "value": {"type": "str", "value": "old"},
            }
            if semantic
            else {"type": "missing_parameter"}
        ),
        new_value=(
            {
                "predicate": "return_literal",
                "value": {"type": "str", "value": "new"},
            }
            if semantic
            else {"name": "timeout"}
        ),
        detector_id="semantic.constant_return" if semantic else "structural.signature",
        detector_version="1" if semantic else "2",
        fingerprint="semantic-fingerprint" if semantic else "structural-fingerprint",
        reason_code=("semantic.direct_mismatch" if semantic else "parameter.added"),
        symbol_identity=SymbolIdentity(
            module="demo.api",
            name="answer",
            category="module_function",
        ),
    )
    return VerifiedRepairBundle(
        status=RunStatus.DRIFT_FOUND,
        run_id="run-v3",
        snapshot=WorkspaceSnapshot(
            head_revision="head",
            workspace_fingerprint="workspace-hash",
            input_file_hashes={
                "src/demo/api.py": "code-hash",
                "docs/api.md": "doc-hash",
            },
            validation_input_hashes={"private/input.txt": "private-hash"},
        ),
        scope=["src/demo/api.py"],
        findings=[finding],
        repository_id="repository-id",
        workspace_id="workspace-id",
    )


def test_v3_is_the_v2_superset_for_legacy_findings() -> None:
    bundle = _bundle()

    v2 = bundle_to_wire(bundle, 2)
    v3 = bundle_to_wire(bundle, 3)

    assert v2["schema_version"] == 2
    assert v3["schema_version"] == 3
    assert v3["omitted_findings"] == 0
    assert v3["findings_summary"] is None
    v3_additive = {"schema_version", "omitted_findings", "findings_summary"}
    assert {key: value for key, value in v3.items() if key not in v3_additive} == {
        key: value for key, value in v2.items() if key != "schema_version"
    }


def test_semantic_finding_is_representable_only_by_v3() -> None:
    bundle = _bundle(semantic=True)

    v3 = bundle_to_wire(bundle, 3)

    assert v3["schema_version"] == 3
    assert v3["findings"] == [
        {
            "id": "finding-semantic",
            "symbol_id": "demo.api.answer",
            "type": "semantic_drift",
            "disposition": "detected",
            "truth_source": "code",
            "code_evidence": {
                "path": "src/demo/api.py",
                "line": 4,
                "source_hash": "code-hash",
                "start_byte": 30,
                "end_byte": 39,
            },
            "doc_evidence": {
                "path": "docs/api.md",
                "line": 7,
                "source_hash": "doc-hash",
                "start_byte": 50,
                "end_byte": 72,
            },
            "reason": "documented constant return differs from definite code return",
            "kind": "semantic_direct_mismatch",
            "component_id": "return.literal",
            "old_value": {
                "predicate": "return_literal",
                "mode": "direct",
                "value": {"type": "str", "value": "old"},
            },
            "new_value": {
                "predicate": "return_literal",
                "value": {"type": "str", "value": "new"},
            },
            "detector_id": "semantic.constant_return",
            "detector_version": "1",
            "fingerprint": "semantic-fingerprint",
            "reason_code": "semantic.direct_mismatch",
        }
    ]
    for version in (1, 2):
        with pytest.raises(
            UnsupportedWireVersionError,
            match=rf"schema V{version} cannot represent semantic analysis",
        ):
            bundle_to_wire(bundle, version)


def test_semantic_capability_fails_closed_after_all_findings_are_suppressed() -> None:
    bundle = _bundle().model_copy(
        update={"findings": [], "semantic_analysis": True},
    )

    v3 = bundle_to_wire(bundle, 3)

    assert v3["findings"] == []
    assert "semantic_analysis" not in v3
    for version in (1, 2):
        with pytest.raises(
            UnsupportedWireVersionError,
            match=rf"schema V{version} cannot represent semantic analysis",
        ):
            bundle_to_wire(bundle, version)


@pytest.mark.parametrize(
    "version",
    [False, True, -1, 0, 4, 99, 1.0, 2.0, 3.0, "3", None, [], {}],
)
def test_invalid_wire_versions_fail_closed(version: object) -> None:
    with pytest.raises(UnsupportedWireVersionError, match="unsupported output version"):
        bundle_to_wire(_bundle(), cast(WireVersion, version))


def test_semantic_kind_cannot_masquerade_as_legacy_finding() -> None:
    payload = _bundle(semantic=True).findings[0].model_dump(mode="python")
    payload["type"] = "signature_drift"

    with pytest.raises(ValueError, match="requires type=semantic_drift"):
        DriftFinding.model_validate(payload)


@pytest.mark.parametrize(
    ("scalar_type", "value"),
    [
        ("int", 2**63),
        ("int", -(2**63) - 1),
        ("str", "\ud800"),
    ],
)
def test_semantic_scalar_rejects_noncanonical_json_values(
    scalar_type: str,
    value: object,
) -> None:
    with pytest.raises(ValueError):
        SemanticScalar(type=scalar_type, value=value)  # type: ignore[arg-type]


@pytest.mark.parametrize("version", [1, 2, 3])
def test_internal_identity_and_validation_manifest_never_leak(version: int) -> None:
    wire = bundle_to_wire(_bundle(), cast(WireVersion, version))

    assert "semantic_analysis" not in wire
    assert "validation_input_hashes" not in wire["snapshot"]
    assert "symbol_identity" not in wire["findings"][0]

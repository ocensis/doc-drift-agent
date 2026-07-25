from __future__ import annotations

from typing import Any, Literal

import pytest
from pydantic import ValidationError

from drift_agent.adapters.contracts import PublicBundleV3, bounded_bundle
from drift_agent.domain.enums import FindingDisposition, RunStatus
from drift_agent.domain.models import (
    ApprovalRequest,
    DriftFinding,
    EvidenceAnchor,
    SymbolIdentity,
    VerifiedRepairBundle,
    WorkspaceSnapshot,
)
from drift_agent.domain.serialization import bundle_to_wire


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
        reason_code="semantic.direct_mismatch" if semantic else "parameter.added",
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
        semantic_analysis=semantic,
    )


@pytest.mark.parametrize("semantic", [False, True], ids=["structural", "semantic"])
def test_from_bundle_exactly_matches_v3_wire_contract(semantic: bool) -> None:
    bundle = _bundle(semantic=semantic)

    public = PublicBundleV3.from_bundle(bundle)

    assert public.model_dump(mode="json") == bundle_to_wire(bundle, 3)


@pytest.mark.parametrize(
    ("semantic", "expected_type"),
    [(False, "signature_drift"), (True, "semantic_drift")],
    ids=["structural", "semantic"],
)
def test_public_bundle_v3_expresses_each_supported_finding_type(
    semantic: bool,
    expected_type: str,
) -> None:
    public = PublicBundleV3.from_bundle(_bundle(semantic=semantic))

    assert public.findings[0].type == expected_type


PRIVATE_FIELD_NAMES = frozenset({"semantic_analysis", "validation_input_hashes", "symbol_identity"})


def _assert_private_field_names_absent(value: object) -> None:
    if isinstance(value, dict):
        assert PRIVATE_FIELD_NAMES.isdisjoint(value)
        for child in value.values():
            _assert_private_field_names_absent(child)
    elif isinstance(value, list):
        for child in value:
            _assert_private_field_names_absent(child)


@pytest.mark.parametrize("mode", ["validation", "serialization"])
def test_public_bundle_v3_schema_recursively_excludes_internal_fields(
    mode: Literal["validation", "serialization"],
) -> None:
    schema = PublicBundleV3.model_json_schema(mode=mode)

    _assert_private_field_names_absent(schema)


@pytest.mark.parametrize(
    "target_path",
    [
        (),
        ("snapshot",),
        ("findings", 0),
        ("findings", 0, "code_evidence"),
    ],
    ids=["bundle", "snapshot", "finding", "evidence"],
)
def test_public_bundle_v3_rejects_extra_fields(
    target_path: tuple[str | int, ...],
) -> None:
    payload = bundle_to_wire(_bundle(), 3)
    target: Any = payload
    for segment in target_path:
        target = target[segment]
    assert isinstance(target, dict)
    target["unexpected_internal_field"] = "must be rejected"

    with pytest.raises(ValidationError):
        PublicBundleV3.model_validate(payload)


def _public_with_findings(count: int) -> PublicBundleV3:
    public = PublicBundleV3.from_bundle(_bundle())
    template = public.findings[0]
    findings = [
        template.model_copy(
            update={
                "id": f"finding-{index}",
                "kind": "parameter_added" if index % 2 == 0 else "unsupported",
                "reason_code": "parameter.added" if index % 2 == 0 else "unsupported.literal",
            }
        )
        for index in range(count)
    ]
    return public.model_copy(update={"findings": findings})


def test_bounded_bundle_returns_unchanged_bundle_when_within_cap() -> None:
    public = _public_with_findings(3)

    assert bounded_bundle(public, max_findings=3) is public
    assert bounded_bundle(public, max_findings=None) is public
    assert public.omitted_findings == 0
    assert public.findings_summary is None


def test_bounded_bundle_truncates_and_counts_all_findings() -> None:
    public = _public_with_findings(5)

    bounded = bounded_bundle(public, max_findings=2)

    assert [finding.id for finding in bounded.findings] == ["finding-0", "finding-1"]
    assert bounded.omitted_findings == 3
    summary = bounded.findings_summary
    assert summary is not None
    assert summary.total_findings == 5
    assert summary.by_kind == {"parameter_added": 3, "unsupported": 2}
    assert summary.by_reason_code == {"parameter.added": 3, "unsupported.literal": 2}
    assert public.findings_summary is None  # original untouched


def test_bounded_bundle_summary_only_wins_over_max_findings() -> None:
    public = _public_with_findings(4)

    bounded = bounded_bundle(public, max_findings=2, summary_only=True)

    assert bounded.findings == []
    assert bounded.omitted_findings == 4
    assert bounded.findings_summary is not None
    assert bounded.findings_summary.total_findings == 4


def test_bounded_bundle_survives_public_contract_revalidation() -> None:
    bounded = bounded_bundle(_public_with_findings(4), max_findings=1)

    payload = bounded.model_dump(mode="json")

    assert PublicBundleV3.model_validate(payload).model_dump(mode="json") == payload


def test_bounded_bundle_always_retains_approval_referenced_findings() -> None:
    public = _public_with_findings(5)
    approval = ApprovalRequest(
        id="approval-1",
        finding_id="finding-3",
        reason="truth direction is ambiguous",
        input_file_hashes={},
    )
    public = public.model_copy(update={"approval_required": [approval]})

    truncated = bounded_bundle(public, max_findings=1)
    assert [finding.id for finding in truncated.findings] == ["finding-0", "finding-3"]
    assert truncated.omitted_findings == 3

    summary_view = bounded_bundle(public, summary_only=True)
    assert [finding.id for finding in summary_view.findings] == ["finding-3"]
    assert summary_view.omitted_findings == 4
    assert summary_view.findings_summary is not None
    assert summary_view.findings_summary.total_findings == 5


def test_findings_summary_orders_keys_by_count_then_alphabetically() -> None:
    public = _public_with_findings(5)
    kinds = ["kind_b", "kind_a", "kind_a", "kind_c", "kind_c"]
    findings = [
        finding.model_copy(update={"kind": kind})
        for finding, kind in zip(public.findings, kinds, strict=True)
    ]
    public = public.model_copy(update={"findings": findings})

    bounded = bounded_bundle(public, summary_only=True)

    assert bounded.findings_summary is not None
    assert list(bounded.findings_summary.by_kind) == ["kind_a", "kind_c", "kind_b"]
    assert bounded.findings_summary.by_kind == {"kind_a": 2, "kind_c": 2, "kind_b": 1}
    assert bounded.findings_summary.by_disposition == {"detected": 5}

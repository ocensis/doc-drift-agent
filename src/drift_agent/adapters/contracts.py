from __future__ import annotations

from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from drift_agent.domain.enums import FindingDisposition, RunStatus
from drift_agent.domain.models import (
    ApprovalRequest,
    ChangeSet,
    MemoryEvent,
    RepairGroupOutcome,
    ResidualChange,
    SuppressionRecord,
    Usage,
    ValidationResult,
    VerifiedRepairBundle,
)
from drift_agent.domain.serialization import bundle_to_wire
from drift_agent.domain.status import is_advisory_reason


class PublicContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PublicWorkspaceSnapshot(PublicContractModel):
    head_revision: str
    workspace_fingerprint: str
    input_file_hashes: dict[str, str]


class PublicEvidenceAnchor(PublicContractModel):
    path: str
    line: int
    source_hash: str
    start_byte: int = 0
    end_byte: int = 0


class PublicDriftFinding(PublicContractModel):
    id: str
    symbol_id: str
    type: Literal["signature_drift", "semantic_drift"] = "signature_drift"
    disposition: FindingDisposition = FindingDisposition.DETECTED
    truth_source: Literal["code", "human", "unknown"] = "code"
    code_evidence: PublicEvidenceAnchor
    doc_evidence: PublicEvidenceAnchor
    reason: str
    kind: str = "signature_changed"
    component_id: str = ""
    old_value: Any = None
    new_value: Any = None
    detector_id: str = ""
    detector_version: str = ""
    fingerprint: str = ""
    reason_code: str = ""


class PublicFindingsSummary(PublicContractModel):
    """Aggregate finding counts kept alongside a bounded findings list."""

    total_findings: int
    by_kind: dict[str, int]
    by_reason_code: dict[str, int]
    by_doc_path: dict[str, int]
    by_disposition: dict[str, int]


class PublicBundleV3(PublicContractModel):
    """The explicit, internal-field-free adapter contract for schema V3."""

    schema_version: Literal[3] = 3
    status: RunStatus
    run_id: str
    snapshot: PublicWorkspaceSnapshot
    scope: list[str]
    findings: list[PublicDriftFinding]
    omitted_findings: int = 0
    findings_summary: PublicFindingsSummary | None = None
    changes: ChangeSet = Field(default_factory=ChangeSet)
    validation: list[ValidationResult] = Field(default_factory=list)
    approval_required: list[ApprovalRequest] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    repository_id: str = ""
    workspace_id: str = ""
    suppressed_findings: list[SuppressionRecord] = Field(default_factory=list)
    memory_events: list[MemoryEvent] = Field(default_factory=list)
    repair_groups: list[RepairGroupOutcome] = Field(default_factory=list)
    residual_changes: list[ResidualChange] = Field(default_factory=list)

    @classmethod
    def from_bundle(cls, bundle: VerifiedRepairBundle) -> PublicBundleV3:
        return cls.model_validate(bundle_to_wire(bundle, 3))


def blocking_finding_count(bundle: PublicBundleV3) -> int:
    """Count the findings that assert the documentation is wrong.

    Findings whose reason code only records that the detector could not decide are
    excluded; see `is_advisory_reason`. A bounded bundle drops findings from the
    inlined list, so the complete `findings_summary` counts are preferred whenever
    they are present -- counting the visible list there would silently under-report
    and could turn a blocked merge into a passing one.

    Args:
        bundle (PublicBundleV3): A complete or bounded public bundle.

    Returns:
        int: The number of blocking findings in the run, not merely in the list.
    """

    summary = bundle.findings_summary
    if summary is not None:
        return sum(
            count
            for reason_code, count in summary.by_reason_code.items()
            if not is_advisory_reason(reason_code)
        )
    return sum(1 for finding in bundle.findings if not is_advisory_reason(finding.reason_code))


def _sorted_counts(values: list[str]) -> dict[str, int]:
    counts = Counter(values)
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _findings_summary(findings: list[PublicDriftFinding]) -> PublicFindingsSummary:
    return PublicFindingsSummary(
        total_findings=len(findings),
        by_kind=_sorted_counts([finding.kind for finding in findings]),
        by_reason_code=_sorted_counts([finding.reason_code for finding in findings]),
        by_doc_path=_sorted_counts([finding.doc_evidence.path for finding in findings]),
        by_disposition=_sorted_counts([finding.disposition.value for finding in findings]),
    )


def bounded_bundle(
    bundle: PublicBundleV3,
    *,
    max_findings: int | None = None,
    summary_only: bool = False,
) -> PublicBundleV3:
    """Bound the findings list for size-limited consumers.

    `findings_summary` and `omitted_findings` are populated only when at least
    one finding is dropped (or `summary_only` is set), so a bundle that already
    fits is returned unchanged. Findings referenced by `approval_required` are
    always retained — approval decisions need their evidence in-band — so the
    result can exceed `max_findings` (and `summary_only` can inline findings)
    by up to the number of pending approvals.

    Args:
        bundle (PublicBundleV3): The complete bundle to bound; it is never mutated.
        max_findings (int | None): Largest number of findings to inline before dropping the
            remainder. None leaves the list untouched unless `summary_only` is set.
        summary_only (bool): Drop every finding that no pending approval references, whatever
            `max_findings` says, leaving the counts as the only record.

    Returns:
        PublicBundleV3: The original bundle when nothing was dropped, otherwise a copy carrying
            the retained findings, the omitted count, and complete counts over the full set.
    """

    if summary_only:
        limit = 0
    elif max_findings is None or max_findings >= len(bundle.findings):
        return bundle
    else:
        limit = max_findings
    approval_ids = {request.finding_id for request in bundle.approval_required}
    retained = list(bundle.findings[:limit])
    retained_ids = {finding.id for finding in retained}
    retained.extend(
        finding
        for finding in bundle.findings[limit:]
        if finding.id in approval_ids and finding.id not in retained_ids
    )
    return bundle.model_copy(
        update={
            "findings": retained,
            "omitted_findings": len(bundle.findings) - len(retained),
            "findings_summary": _findings_summary(bundle.findings),
        }
    )


__all__ = [
    "PublicBundleV3",
    "PublicDriftFinding",
    "PublicEvidenceAnchor",
    "PublicFindingsSummary",
    "PublicWorkspaceSnapshot",
    "blocking_finding_count",
    "bounded_bundle",
]

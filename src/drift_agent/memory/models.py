from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Literal

RunModeValue = Literal["check", "repair"]
RunLifecycleStatus = Literal[
    "running",
    "clean",
    "drift_found",
    "fixed",
    "partial",
    "needs_approval",
    "unresolved",
    "stale",
    "failed",
]
RunEventKind = Literal[
    "run_started",
    "snapshot_captured",
    "facts_collected",
    "findings_detected",
    "decisions_applied",
    "repair_planned",
    "lock_acquired",
    "group_started",
    "group_retained",
    "group_rolled_back",
    "group_skipped",
    "final_validation_completed",
    "publication_aborted",
    "budget_exhausted",
    "run_finished",
]
DecisionAction = Literal["ignore", "false_positive"]


CHECK_EVENT_SEQUENCE: tuple[RunEventKind, ...] = (
    "run_started",
    "snapshot_captured",
    "facts_collected",
    "findings_detected",
    "decisions_applied",
    "run_finished",
)


def budget_exhausted_sequence_is_valid(
    mode: str,
    kinds: tuple[str, ...],
) -> bool:
    """Validate an early, non-successful terminal caused by the run deadline."""

    if kinds[-2:] != ("budget_exhausted", "run_finished"):
        return False
    evidence_prefix = CHECK_EVENT_SEQUENCE[:-1]
    prefix = kinds[:-2]
    if mode == "check":
        return prefix == evidence_prefix
    if mode != "repair":
        return False
    return prefix in {
        evidence_prefix,
        (*evidence_prefix, "repair_planned"),
        (*evidence_prefix, "repair_planned", "lock_acquired"),
        (
            *evidence_prefix,
            "repair_planned",
            "lock_acquired",
            "final_validation_completed",
        ),
    }


def canonical_json(value: object) -> str:
    """Serialize state material deterministically for equality and hashing."""

    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def canonical_digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class RepositoryRecord:
    repository_id: str
    material: str
    common_dir: str
    root_commit: str
    first_seen_at: str
    last_seen_at: str


@dataclass(frozen=True, slots=True)
class WorkspaceRecord:
    workspace_id: str
    repository_id: str
    material: str
    worktree_root: str
    first_seen_at: str
    last_seen_at: str


@dataclass(frozen=True, slots=True)
class RunRecord:
    run_id: str
    repository_id: str
    workspace_id: str
    mode: RunModeValue
    request: dict[str, object] = field(default_factory=dict)
    snapshot: dict[str, object] = field(default_factory=dict)
    usage: dict[str, object] = field(default_factory=dict)
    status: RunLifecycleStatus = "running"
    finding_count: int = 0
    suppressed_count: int = 0
    fixed_count: int = 0
    model_calls: int = 0
    started_at: str | None = None
    finished_at: str | None = None

    @property
    def completed(self) -> bool:
        return self.status != "running" and self.finished_at is not None


@dataclass(frozen=True, slots=True)
class RunCompletion:
    status: RunLifecycleStatus
    finding_count: int
    suppressed_count: int
    fixed_count: int
    model_calls: int = 0
    snapshot: dict[str, object] | None = None
    usage: dict[str, object] | None = None
    event_payload: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status == "running":
            raise ValueError("a completed run cannot retain running status")
        counts = (self.finding_count, self.suppressed_count, self.fixed_count)
        if any(count < 0 for count in counts) or self.model_calls < 0:
            raise ValueError("run counts must be non-negative")


@dataclass(frozen=True, slots=True)
class RunEvent:
    run_id: str
    seq: int
    kind: RunEventKind
    payload: dict[str, object]
    created_at: str


@dataclass(frozen=True, slots=True)
class FindingValidityKey:
    repository_id: str
    symbol_id: str
    kind: str
    component_id: str
    normalized_old: str
    normalized_new: str
    code_evidence_hash: str
    doc_evidence_hash: str
    detector_id: str
    detector_version: str
    fingerprint: str

    def as_dict(self) -> dict[str, str]:
        return {
            "repository_id": self.repository_id,
            "symbol_id": self.symbol_id,
            "kind": self.kind,
            "component_id": self.component_id,
            "normalized_old": self.normalized_old,
            "normalized_new": self.normalized_new,
            "code_evidence_hash": self.code_evidence_hash,
            "doc_evidence_hash": self.doc_evidence_hash,
            "detector_id": self.detector_id,
            "detector_version": self.detector_version,
            "fingerprint": self.fingerprint,
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self.as_dict())


@dataclass(frozen=True, slots=True)
class PersistedFinding:
    run_id: str
    finding_id: str
    validity: FindingValidityKey


@dataclass(frozen=True, slots=True)
class AlignmentEvidenceKey:
    repository_id: str
    old_symbol_id: str
    new_symbol_id: str
    confirmation_commit: str
    old_blob_id: str
    old_evidence_hash: str
    new_evidence_hash: str
    doc_evidence_hash: str
    aligner_id: str
    aligner_version: str

    def as_dict(self) -> dict[str, str]:
        return {
            "repository_id": self.repository_id,
            "old_symbol_id": self.old_symbol_id,
            "new_symbol_id": self.new_symbol_id,
            "confirmation_commit": self.confirmation_commit,
            "old_blob_id": self.old_blob_id,
            "old_evidence_hash": self.old_evidence_hash,
            "new_evidence_hash": self.new_evidence_hash,
            "doc_evidence_hash": self.doc_evidence_hash,
            "aligner_id": self.aligner_id,
            "aligner_version": self.aligner_version,
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self.as_dict())


@dataclass(frozen=True, slots=True)
class PersistedAlignment:
    run_id: str
    alignment_id: str
    evidence: AlignmentEvidenceKey


@dataclass(frozen=True, slots=True)
class DecisionAddRequest:
    repository_id: str
    run_id: str
    finding_id: str
    action: DecisionAction
    reason: str
    actor: str
    confirmation: bool


@dataclass(frozen=True, slots=True)
class DecisionRevokeRequest:
    repository_id: str
    decision_id: str
    reason: str
    actor: str


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    decision_id: str
    source_run_id: str
    source_finding_id: str
    action: DecisionAction
    reason: str
    actor: str
    confirmation: bool
    validity: FindingValidityKey
    created_at: str
    revoked_at: str | None = None
    revoked_reason: str | None = None
    revoked_actor: str | None = None

    @property
    def active(self) -> bool:
        return self.revoked_at is None


@dataclass(frozen=True, slots=True)
class DecisionMatch:
    decision: DecisionRecord | None
    invalidation_reasons: tuple[str, ...] = ()
    invalidation_events: tuple[MemoryEvent, ...] = ()


@dataclass(frozen=True, slots=True)
class AliasAddRequest:
    repository_id: str
    run_id: str
    alignment_id: str
    reason: str
    actor: str
    confirmation: bool


@dataclass(frozen=True, slots=True)
class AliasRevokeRequest:
    repository_id: str
    alias_id: str
    reason: str
    actor: str


@dataclass(frozen=True, slots=True)
class AliasRecord:
    alias_id: str
    source_run_id: str
    source_alignment_id: str
    reason: str
    actor: str
    confirmation: bool
    evidence: AlignmentEvidenceKey
    created_at: str
    revoked_at: str | None = None
    revoked_reason: str | None = None
    revoked_actor: str | None = None

    @property
    def active(self) -> bool:
        return self.revoked_at is None


@dataclass(frozen=True, slots=True)
class AliasMatch:
    alias: AliasRecord | None
    invalidation_reasons: tuple[str, ...] = ()
    invalidation_events: tuple[MemoryEvent, ...] = ()


@dataclass(frozen=True, slots=True)
class AliasValidationContext:
    repository_id: str
    old_symbol_id: str
    new_symbol_id: str
    old_evidence_hash: str
    new_evidence_hash: str
    doc_evidence_hash: str
    aligner_id: str
    aligner_version: str


@dataclass(frozen=True, slots=True)
class MemoryEvent:
    event_id: int
    repository_id: str
    run_id: str | None
    kind: str
    subject_type: str
    subject_id: str
    reason: str
    payload: dict[str, object]
    created_at: str

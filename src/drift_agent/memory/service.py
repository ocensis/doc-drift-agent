from __future__ import annotations

from pathlib import Path

from drift_agent.memory.models import (
    CHECK_EVENT_SEQUENCE,
    AliasAddRequest,
    AliasMatch,
    AliasRecord,
    AliasRevokeRequest,
    AliasValidationContext,
    DecisionAddRequest,
    DecisionMatch,
    DecisionRecord,
    DecisionRevokeRequest,
    FindingValidityKey,
    RunCompletion,
    RunEvent,
    RunEventKind,
    RunRecord,
    budget_exhausted_sequence_is_valid,
)
from drift_agent.memory.schema import StateStoreConflictError
from drift_agent.memory.store import SQLiteStateStore
from drift_agent.workspace.identity import git_is_ancestor, git_object_exists

_GROUP_TERMINALS: frozenset[RunEventKind] = frozenset(
    {"group_retained", "group_rolled_back", "group_skipped"}
)


class RunService:
    """Typed run/event lifecycle facade over the SQLite repository."""

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store

    def start(
        self,
        record: RunRecord,
        *,
        event_payload: dict[str, object] | None = None,
    ) -> RunRecord:
        return self.store.start_run(record, event_payload=event_payload)

    def event(
        self,
        run_id: str,
        kind: RunEventKind,
        payload: dict[str, object] | None = None,
        *,
        expected_seq: int | None = None,
    ) -> RunEvent:
        return self.store.append_event(
            run_id,
            kind,
            payload,
            expected_seq=expected_seq,
        )

    def finish(
        self,
        run_id: str,
        completion: RunCompletion,
        *,
        expected_manual_state_revision: int | None = None,
    ) -> RunRecord:
        return self.store.finish_run(
            run_id,
            completion,
            expected_manual_state_revision=expected_manual_state_revision,
        )

    def events(self, run_id: str) -> list[RunEvent]:
        return self.store.events(run_id)

    def validate_required_events(self, run_id: str) -> tuple[RunEventKind, ...]:
        run = self.store.run(run_id)
        if run is None:
            raise StateStoreConflictError(f"run does not exist: {run_id}")
        events = self.store.events(run_id)
        kinds = tuple(event.kind for event in events)
        if budget_exhausted_sequence_is_valid(run.mode, kinds):
            return kinds
        if run.mode == "check":
            if kinds != CHECK_EVENT_SEQUENCE:
                raise StateStoreConflictError(
                    f"check event contract mismatch for run {run_id}: {kinds}"
                )
            return kinds

        prefix: tuple[RunEventKind, ...] = (
            "run_started",
            "snapshot_captured",
            "facts_collected",
            "findings_detected",
            "decisions_applied",
            "repair_planned",
            "lock_acquired",
        )
        publication_aborted = len(kinds) >= 3 and kinds[-2] == "publication_aborted"
        suffix: tuple[RunEventKind, ...] = (
            (
                "final_validation_completed",
                "publication_aborted",
                "run_finished",
            )
            if publication_aborted
            else (
                "final_validation_completed",
                "run_finished",
            )
        )
        if kinds[: len(prefix)] != prefix or kinds[-len(suffix) :] != suffix:
            raise StateStoreConflictError(
                f"repair event contract mismatch for run {run_id}: {kinds}"
            )
        cursor = len(prefix)
        group_end = len(kinds) - len(suffix)
        budget_recorded = group_end > cursor and kinds[group_end - 1] == "budget_exhausted"
        if budget_recorded:
            if run.status not in {"partial", "unresolved"}:
                raise StateStoreConflictError(
                    f"repair budget event conflicts with run status for {run_id}: {kinds}"
                )
            group_end -= 1
        active: set[str] = set()
        completed: set[str] = set()
        while cursor < group_end:
            event = events[cursor]
            group_id = event.payload.get("group_id")
            if not isinstance(group_id, str) or not group_id:
                raise StateStoreConflictError(
                    f"repair group event contract mismatch for run {run_id}: {kinds}"
                )
            if event.kind == "group_started":
                valid = group_id not in active and group_id not in completed
                active.add(group_id)
            elif event.kind in _GROUP_TERMINALS:
                valid = group_id in active
                active.discard(group_id)
                completed.add(group_id)
            else:
                valid = False
            if not valid:
                raise StateStoreConflictError(
                    f"repair group event contract mismatch for run {run_id}: {kinds}"
                )
            cursor += 1
        if active:
            raise StateStoreConflictError(
                f"repair group event contract mismatch for run {run_id}: {kinds}"
            )
        return kinds


def _decision_mismatch(
    stored: FindingValidityKey,
    current: FindingValidityKey,
) -> str:
    if stored.repository_id != current.repository_id:
        return "decision.repository_mismatch"
    if stored.symbol_id != current.symbol_id:
        return "decision.symbol_mismatch"
    if stored.kind != current.kind:
        return "decision.kind_mismatch"
    if stored.component_id != current.component_id:
        return "decision.component_mismatch"
    if (
        stored.normalized_old != current.normalized_old
        or stored.normalized_new != current.normalized_new
    ):
        return "decision.value_mismatch"
    if stored.code_evidence_hash != current.code_evidence_hash:
        return "decision.code_evidence_mismatch"
    if stored.doc_evidence_hash != current.doc_evidence_hash:
        return "decision.doc_evidence_mismatch"
    if (
        stored.detector_id != current.detector_id
        or stored.detector_version != current.detector_version
    ):
        return "decision.detector_mismatch"
    if stored.fingerprint != current.fingerprint:
        return "decision.fingerprint_mismatch"
    return "decision.validity_mismatch"


def _decision_candidate_is_relevant(
    stored: FindingValidityKey,
    current: FindingValidityKey,
) -> bool:
    if stored.symbol_id == current.symbol_id:
        return True
    return (
        stored.kind == current.kind
        and stored.component_id == current.component_id
        and stored.normalized_old == current.normalized_old
        and stored.normalized_new == current.normalized_new
        and stored.code_evidence_hash == current.code_evidence_hash
        and stored.doc_evidence_hash == current.doc_evidence_hash
        and stored.detector_id == current.detector_id
        and stored.detector_version == current.detector_version
    )


class DecisionService:
    """Human-only suppression lifecycle with evidence-derived add operations."""

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store

    def add(self, request: DecisionAddRequest) -> DecisionRecord:
        return self.store.add_decision(request)

    def list(
        self,
        repository_id: str,
        *,
        include_revoked: bool = True,
    ) -> list[DecisionRecord]:
        return self.store.list_decisions(
            repository_id,
            include_revoked=include_revoked,
        )

    def revoke(self, request: DecisionRevokeRequest) -> DecisionRecord:
        return self.store.revoke_decision(request)

    def match(
        self,
        validity: FindingValidityKey,
        *,
        run_id: str | None = None,
        audit_mismatches: bool = True,
    ) -> DecisionMatch:
        exact = self.store.matching_decision(validity)
        if exact is not None:
            return DecisionMatch(decision=exact)

        invalidations: list[str] = []
        invalidation_events = []
        candidates = self.store.decision_candidates(validity.repository_id)
        for candidate in candidates:
            if not _decision_candidate_is_relevant(candidate.validity, validity):
                continue
            reason = _decision_mismatch(candidate.validity, validity)
            invalidations.append(reason)
            if audit_mismatches:
                invalidation_events.append(
                    self.store.append_memory_event(
                        repository_id=validity.repository_id,
                        run_id=run_id,
                        kind="decision_invalidated",
                        subject_type="decision",
                        subject_id=candidate.decision_id,
                        reason=reason,
                        payload={"current_validity": validity.as_dict()},
                    )
                )
        return DecisionMatch(
            decision=None,
            invalidation_reasons=tuple(invalidations),
            invalidation_events=tuple(invalidation_events),
        )


def _alias_material_mismatch(
    alias: AliasRecord,
    current: AliasValidationContext,
) -> str | None:
    stored = alias.evidence
    if stored.repository_id != current.repository_id:
        return "alias.repository_mismatch"
    if stored.old_symbol_id != current.old_symbol_id:
        return "alias.old_symbol_mismatch"
    if stored.new_symbol_id != current.new_symbol_id:
        return "alias.target_mismatch"
    if stored.old_evidence_hash != current.old_evidence_hash:
        return "alias.old_evidence_mismatch"
    if stored.new_evidence_hash != current.new_evidence_hash:
        return "alias.new_evidence_mismatch"
    if stored.doc_evidence_hash != current.doc_evidence_hash:
        return "alias.doc_evidence_mismatch"
    if stored.aligner_id != current.aligner_id or stored.aligner_version != current.aligner_version:
        return "alias.aligner_mismatch"
    return None


class AliasService:
    """Typed symbol-alias lifecycle and Git-lineage validity checks."""

    def __init__(self, store: SQLiteStateStore) -> None:
        self.store = store

    def add(self, request: AliasAddRequest) -> AliasRecord:
        return self.store.add_alias(request)

    def list(
        self,
        repository_id: str,
        *,
        include_revoked: bool = True,
    ) -> list[AliasRecord]:
        return self.store.list_aliases(
            repository_id,
            include_revoked=include_revoked,
        )

    def revoke(self, request: AliasRevokeRequest) -> AliasRecord:
        return self.store.revoke_alias(request)

    def match(
        self,
        repo_path: Path,
        current: AliasValidationContext,
        *,
        run_id: str | None = None,
        audit_mismatches: bool = True,
    ) -> AliasMatch:
        invalidations: list[str] = []
        invalidation_events = []
        valid: list[AliasRecord] = []
        for alias in self.store.alias_candidates(
            current.repository_id,
            current.old_symbol_id,
        ):
            reason = _alias_material_mismatch(alias, current)
            if reason is None and not git_object_exists(
                repo_path,
                alias.evidence.confirmation_commit,
                "commit",
            ):
                reason = "alias.confirmation_object_missing"
            if reason is None and not git_object_exists(
                repo_path,
                alias.evidence.old_blob_id,
                "blob",
            ):
                reason = "alias.old_object_missing"
            if reason is None and not git_is_ancestor(
                repo_path,
                alias.evidence.confirmation_commit,
            ):
                reason = "alias.history_mismatch"
            if reason is None:
                valid.append(alias)
                continue

            invalidations.append(reason)
            if audit_mismatches:
                invalidation_events.append(
                    self.store.append_memory_event(
                        repository_id=current.repository_id,
                        run_id=run_id,
                        kind="alias_invalidated",
                        subject_type="alias",
                        subject_id=alias.alias_id,
                        reason=reason,
                        payload={
                            "old_symbol_id": current.old_symbol_id,
                            "new_symbol_id": current.new_symbol_id,
                            "aligner_id": current.aligner_id,
                            "aligner_version": current.aligner_version,
                        },
                    )
                )
        if len(valid) > 1:
            raise StateStoreConflictError("multiple active aliases are valid")
        return AliasMatch(
            alias=valid[0] if valid else None,
            invalidation_reasons=tuple(invalidations),
            invalidation_events=tuple(invalidation_events),
        )

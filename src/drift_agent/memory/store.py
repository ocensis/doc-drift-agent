from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from drift_agent.memory.models import (
    CHECK_EVENT_SEQUENCE,
    AliasAddRequest,
    AliasRecord,
    AliasRevokeRequest,
    AlignmentEvidenceKey,
    DecisionAction,
    DecisionAddRequest,
    DecisionRecord,
    DecisionRevokeRequest,
    FindingValidityKey,
    MemoryEvent,
    PersistedAlignment,
    PersistedFinding,
    RepositoryRecord,
    RunCompletion,
    RunEvent,
    RunEventKind,
    RunLifecycleStatus,
    RunModeValue,
    RunRecord,
    WorkspaceRecord,
    budget_exhausted_sequence_is_valid,
    canonical_json,
)
from drift_agent.memory.schema import (
    ManualStateRevisionChangedError,
    MigrationHook,
    RepositoryIdentityCollisionError,
    StateStoreConflictError,
    StateStoreCorruptError,
    StateStoreError,
    StateStoreNotFoundError,
    StateStoreWriteError,
    connect_database,
    database_integrity,
    prepare_database,
)
from drift_agent.workspace.identity import IdentitySet


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _decode_object(raw: str) -> dict[str, object]:
    try:
        decoded = cast(object, json.loads(raw))
    except (TypeError, json.JSONDecodeError) as error:
        raise StateStoreCorruptError("persisted JSON is invalid") from error
    if not isinstance(decoded, dict) or any(not isinstance(key, str) for key in decoded):
        raise StateStoreCorruptError("persisted JSON must be an object")
    return cast(dict[str, object], decoded)


def _required_text(value: str, field_name: str) -> str:
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    return value


def _row_text(row: sqlite3.Row, key: str) -> str:
    value = cast(object, row[key])
    if not isinstance(value, str):
        raise StateStoreCorruptError(f"SQLite field {key} is not text")
    return value


def _row_optional_text(row: sqlite3.Row, key: str) -> str | None:
    value = cast(object, row[key])
    if value is None:
        return None
    if not isinstance(value, str):
        raise StateStoreCorruptError(f"SQLite field {key} is not nullable text")
    return value


def _row_int(row: sqlite3.Row, key: str) -> int:
    value = cast(object, row[key])
    if not isinstance(value, int):
        raise StateStoreCorruptError(f"SQLite field {key} is not an integer")
    return value


def _finding_key_from_row(row: sqlite3.Row) -> FindingValidityKey:
    return FindingValidityKey(
        repository_id=_row_text(row, "repository_id"),
        symbol_id=_row_text(row, "symbol_id"),
        kind=_row_text(row, "kind"),
        component_id=_row_text(row, "component_id"),
        normalized_old=_row_text(row, "normalized_old"),
        normalized_new=_row_text(row, "normalized_new"),
        code_evidence_hash=_row_text(row, "code_evidence_hash"),
        doc_evidence_hash=_row_text(row, "doc_evidence_hash"),
        detector_id=_row_text(row, "detector_id"),
        detector_version=_row_text(row, "detector_version"),
        fingerprint=_row_text(row, "fingerprint"),
    )


def _alignment_key_from_row(row: sqlite3.Row) -> AlignmentEvidenceKey:
    return AlignmentEvidenceKey(
        repository_id=_row_text(row, "repository_id"),
        old_symbol_id=_row_text(row, "old_symbol_id"),
        new_symbol_id=_row_text(row, "new_symbol_id"),
        confirmation_commit=_row_text(row, "confirmation_commit"),
        old_blob_id=_row_text(row, "old_blob_id"),
        old_evidence_hash=_row_text(row, "old_evidence_hash"),
        new_evidence_hash=_row_text(row, "new_evidence_hash"),
        doc_evidence_hash=_row_text(row, "doc_evidence_hash"),
        aligner_id=_row_text(row, "aligner_id"),
        aligner_version=_row_text(row, "aligner_version"),
    )


def _lifecycle_is_valid(
    mode: str,
    status: str,
    kinds: tuple[str, ...],
    group_ids: tuple[str | None, ...] | None = None,
) -> bool:
    """Validate the persisted run grammar, including its terminal event."""

    if not kinds or kinds[0] != "run_started" or kinds[-1] != "run_finished":
        return False
    publication_aborted = len(kinds) >= 3 and kinds[-2] == "publication_aborted"
    if publication_aborted and status not in {"unresolved", "stale", "failed"}:
        return False
    if status in {"failed", "stale"}:
        # Infrastructure failure may terminate at any point, but event order
        # still starts and ends through the typed lifecycle owners.
        return kinds.count("run_started") == 1 and kinds.count("run_finished") == 1
    if status == "unresolved" and budget_exhausted_sequence_is_valid(mode, kinds):
        return True
    if mode == "check":
        return kinds == CHECK_EVENT_SEQUENCE
    if mode != "repair":
        return False
    prefix = (
        "run_started",
        "snapshot_captured",
        "facts_collected",
        "findings_detected",
        "decisions_applied",
        "repair_planned",
        "lock_acquired",
    )
    suffix = (
        ("final_validation_completed", "publication_aborted", "run_finished")
        if publication_aborted
        else ("final_validation_completed", "run_finished")
    )
    if kinds[: len(prefix)] != prefix or kinds[-len(suffix) :] != suffix:
        return False
    start = len(prefix)
    end = len(kinds) - len(suffix)
    budget_recorded = end > start and kinds[end - 1] == "budget_exhausted"
    if budget_recorded:
        if status not in {"partial", "unresolved"}:
            return False
        end -= 1
    active: set[str] = set()
    completed: set[str] = set()
    terminals = {"group_retained", "group_rolled_back", "group_skipped"}
    for index in range(start, end):
        kind = kinds[index]
        group_id = None if group_ids is None else group_ids[index]
        if not group_id:
            return False
        if kind == "group_started":
            if group_id in active or group_id in completed:
                return False
            active.add(group_id)
        elif kind in terminals:
            if group_id not in active:
                return False
            active.remove(group_id)
            completed.add(group_id)
        else:
            return False
    return not active


def _run_event_kinds(
    connection: sqlite3.Connection,
    run_id: str,
) -> tuple[str, ...]:
    rows = cast(
        list[sqlite3.Row],
        connection.execute(
            "SELECT kind FROM run_events WHERE run_id = ? ORDER BY seq",
            (run_id,),
        ).fetchall(),
    )
    return tuple(_row_text(row, "kind") for row in rows)


def _run_event_group_ids(
    connection: sqlite3.Connection,
    run_id: str,
) -> tuple[str | None, ...]:
    rows = cast(
        list[sqlite3.Row],
        connection.execute(
            "SELECT payload_json FROM run_events WHERE run_id = ? ORDER BY seq",
            (run_id,),
        ).fetchall(),
    )
    result: list[str | None] = []
    for row in rows:
        payload = _decode_object(_row_text(row, "payload_json"))
        group_id = payload.get("group_id")
        result.append(group_id if isinstance(group_id, str) and group_id else None)
    return tuple(result)


def _run_from_row(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        run_id=_row_text(row, "run_id"),
        repository_id=_row_text(row, "repository_id"),
        workspace_id=_row_text(row, "workspace_id"),
        mode=cast(RunModeValue, _row_text(row, "mode")),
        request=_decode_object(_row_text(row, "request_json")),
        snapshot=_decode_object(_row_text(row, "snapshot_json")),
        usage=_decode_object(_row_text(row, "usage_json")),
        status=cast(RunLifecycleStatus, _row_text(row, "status")),
        finding_count=_row_int(row, "finding_count"),
        suppressed_count=_row_int(row, "suppressed_count"),
        fixed_count=_row_int(row, "fixed_count"),
        model_calls=_row_int(row, "model_calls"),
        started_at=_row_text(row, "started_at"),
        finished_at=_row_optional_text(row, "finished_at"),
    )


def _decision_from_row(row: sqlite3.Row) -> DecisionRecord:
    record = DecisionRecord(
        decision_id=_row_text(row, "decision_id"),
        source_run_id=_row_text(row, "source_run_id"),
        source_finding_id=_row_text(row, "source_finding_id"),
        action=cast(DecisionAction, _row_text(row, "action")),
        reason=_row_text(row, "reason"),
        actor=_row_text(row, "actor"),
        confirmation=bool(_row_int(row, "confirmation")),
        validity=_finding_key_from_row(row),
        created_at=_row_text(row, "created_at"),
        revoked_at=_row_optional_text(row, "revoked_at"),
        revoked_reason=_row_optional_text(row, "revoked_reason"),
        revoked_actor=_row_optional_text(row, "revoked_actor"),
    )
    if _row_text(row, "validity_digest") != record.validity.digest:
        raise StateStoreCorruptError("decision validity digest does not match material")
    return record


def _alias_from_row(row: sqlite3.Row) -> AliasRecord:
    record = AliasRecord(
        alias_id=_row_text(row, "alias_id"),
        source_run_id=_row_text(row, "source_run_id"),
        source_alignment_id=_row_text(row, "source_alignment_id"),
        reason=_row_text(row, "reason"),
        actor=_row_text(row, "actor"),
        confirmation=bool(_row_int(row, "confirmation")),
        evidence=_alignment_key_from_row(row),
        created_at=_row_text(row, "created_at"),
        revoked_at=_row_optional_text(row, "revoked_at"),
        revoked_reason=_row_optional_text(row, "revoked_reason"),
        revoked_actor=_row_optional_text(row, "revoked_actor"),
    )
    if _row_text(row, "evidence_digest") != record.evidence.digest:
        raise StateStoreCorruptError("alias evidence digest does not match material")
    return record


_DECISION_SELECT = """
    SELECT
        d.*,
        r.created_at AS revoked_at,
        r.reason AS revoked_reason,
        r.actor AS revoked_actor
    FROM decisions AS d
    LEFT JOIN decision_revocations AS r ON r.decision_id = d.decision_id
"""


_ALIAS_SELECT = """
    SELECT
        a.*,
        r.created_at AS revoked_at,
        r.reason AS revoked_reason,
        r.actor AS revoked_actor
    FROM aliases AS a
    LEFT JOIN alias_revocations AS r ON r.alias_id = a.alias_id
"""


class SQLiteStateStore:
    """Schema-v1 state repository using independent short WAL transactions."""

    def __init__(
        self,
        path: Path,
        *,
        busy_timeout_seconds: float = 5.0,
        migration_hook: MigrationHook | None = None,
    ) -> None:
        self.path = path
        self.busy_timeout_seconds = busy_timeout_seconds
        prepare_database(
            path,
            busy_timeout_seconds=busy_timeout_seconds,
            migration_hook=migration_hook,
        )

    def _connect(self) -> sqlite3.Connection:
        connection = connect_database(
            self.path,
            busy_timeout_seconds=self.busy_timeout_seconds,
        )
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        except sqlite3.DatabaseError as error:
            raise StateStoreCorruptError("could not read SQLite state") from error
        finally:
            connection.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException as error:
            if connection.in_transaction:
                connection.rollback()
            if isinstance(error, (StateStoreError, KeyboardInterrupt, SystemExit)):
                raise
            if isinstance(error, sqlite3.DatabaseError):
                raise StateStoreWriteError("could not update SQLite state") from error
            raise
        finally:
            connection.close()

    def integrity_check(self) -> bool:
        return database_integrity(
            self.path,
            busy_timeout_seconds=self.busy_timeout_seconds,
        )

    def register_identities(self, identities: IdentitySet) -> None:
        repository = identities.repository
        workspace = identities.workspace
        repository_digest = hashlib.sha256(repository.material.encode("utf-8")).hexdigest()
        workspace_digest = hashlib.sha256(workspace.material.encode("utf-8")).hexdigest()
        if repository_digest != repository.digest or workspace_digest != workspace.digest:
            raise RepositoryIdentityCollisionError(
                "identity digest does not match its full material"
            )
        if workspace.repository_id != repository.digest:
            raise RepositoryIdentityCollisionError(
                "workspace identity belongs to a different repository"
            )

        now = _now()
        with self._write() as connection:
            repository_row = cast(
                sqlite3.Row | None,
                connection.execute(
                    """
                    SELECT material, common_dir, root_commit
                    FROM repositories
                    WHERE repository_id = ?
                    """,
                    (repository.digest,),
                ).fetchone(),
            )
            if repository_row is None:
                connection.execute(
                    """
                    INSERT INTO repositories (
                        repository_id, material, common_dir, root_commit,
                        first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        repository.digest,
                        repository.material,
                        str(repository.common_dir),
                        repository.root_commit,
                        now,
                        now,
                    ),
                )
            elif (
                _row_text(repository_row, "material") != repository.material
                or _row_text(repository_row, "common_dir") != str(repository.common_dir)
                or _row_text(repository_row, "root_commit") != repository.root_commit
            ):
                raise RepositoryIdentityCollisionError(
                    "repository digest already exists with different identity material"
                )
            else:
                connection.execute(
                    "UPDATE repositories SET last_seen_at = ? WHERE repository_id = ?",
                    (now, repository.digest),
                )

            workspace_row = cast(
                sqlite3.Row | None,
                connection.execute(
                    """
                    SELECT repository_id, material, worktree_root
                    FROM workspaces
                    WHERE workspace_id = ?
                    """,
                    (workspace.digest,),
                ).fetchone(),
            )
            if workspace_row is None:
                connection.execute(
                    """
                    INSERT INTO workspaces (
                        workspace_id, repository_id, material, worktree_root,
                        first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        workspace.digest,
                        repository.digest,
                        workspace.material,
                        str(workspace.worktree_root),
                        now,
                        now,
                    ),
                )
            elif (
                _row_text(workspace_row, "repository_id") != repository.digest
                or _row_text(workspace_row, "material") != workspace.material
                or _row_text(workspace_row, "worktree_root") != str(workspace.worktree_root)
            ):
                raise RepositoryIdentityCollisionError(
                    "workspace digest already exists with different identity material"
                )
            else:
                connection.execute(
                    "UPDATE workspaces SET last_seen_at = ? WHERE workspace_id = ?",
                    (now, workspace.digest),
                )
            connection.execute(
                """
                INSERT INTO manual_state (repository_id, revision)
                VALUES (?, 0)
                ON CONFLICT(repository_id) DO NOTHING
                """,
                (repository.digest,),
            )

    def repository(self, repository_id: str) -> RepositoryRecord | None:
        with self._read() as connection:
            row = cast(
                sqlite3.Row | None,
                connection.execute(
                    "SELECT * FROM repositories WHERE repository_id = ?",
                    (repository_id,),
                ).fetchone(),
            )
        if row is None:
            return None
        return RepositoryRecord(
            repository_id=_row_text(row, "repository_id"),
            material=_row_text(row, "material"),
            common_dir=_row_text(row, "common_dir"),
            root_commit=_row_text(row, "root_commit"),
            first_seen_at=_row_text(row, "first_seen_at"),
            last_seen_at=_row_text(row, "last_seen_at"),
        )

    def workspace(self, workspace_id: str) -> WorkspaceRecord | None:
        with self._read() as connection:
            row = cast(
                sqlite3.Row | None,
                connection.execute(
                    "SELECT * FROM workspaces WHERE workspace_id = ?",
                    (workspace_id,),
                ).fetchone(),
            )
        if row is None:
            return None
        return WorkspaceRecord(
            workspace_id=_row_text(row, "workspace_id"),
            repository_id=_row_text(row, "repository_id"),
            material=_row_text(row, "material"),
            worktree_root=_row_text(row, "worktree_root"),
            first_seen_at=_row_text(row, "first_seen_at"),
            last_seen_at=_row_text(row, "last_seen_at"),
        )

    def start_run(
        self,
        record: RunRecord,
        *,
        event_payload: dict[str, object] | None = None,
    ) -> RunRecord:
        if record.status != "running" or record.finished_at is not None:
            raise ValueError("start_run requires an unfinished running record")
        started_at = record.started_at or _now()
        stored = replace(record, started_at=started_at)
        payload = event_payload or {}
        with self._write() as connection:
            connection.execute(
                """
                INSERT INTO runs (
                    run_id, repository_id, workspace_id, mode,
                    request_json, snapshot_json, usage_json, status,
                    finding_count, suppressed_count, fixed_count, model_calls,
                    started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', 0, 0, 0, 0, ?, NULL)
                """,
                (
                    stored.run_id,
                    stored.repository_id,
                    stored.workspace_id,
                    stored.mode,
                    canonical_json(stored.request),
                    canonical_json(stored.snapshot),
                    canonical_json(stored.usage),
                    started_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO run_events (run_id, seq, kind, payload_json, created_at)
                VALUES (?, 1, 'run_started', ?, ?)
                """,
                (stored.run_id, canonical_json(payload), started_at),
            )
        return stored

    def append_event(
        self,
        run_id: str,
        kind: RunEventKind,
        payload: dict[str, object] | None = None,
        *,
        expected_seq: int | None = None,
    ) -> RunEvent:
        if kind in {"run_started", "run_finished"}:
            raise ValueError("start_run and finish_run own terminal event writes")
        created_at = _now()
        event_payload = payload or {}
        with self._write() as connection:
            run_row = cast(
                sqlite3.Row | None,
                connection.execute(
                    "SELECT status FROM runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone(),
            )
            if run_row is None:
                raise StateStoreNotFoundError(f"run does not exist: {run_id}")
            if _row_text(run_row, "status") != "running":
                raise StateStoreConflictError("cannot append events to a completed run")
            seq_value = cast(
                object,
                connection.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM run_events WHERE run_id = ?",
                    (run_id,),
                ).fetchone()[0],
            )
            if not isinstance(seq_value, int):
                raise StateStoreCorruptError("next run event sequence is invalid")
            if expected_seq is not None and seq_value != expected_seq:
                raise StateStoreConflictError(
                    f"expected event seq {expected_seq}, found {seq_value}"
                )
            connection.execute(
                """
                INSERT INTO run_events (run_id, seq, kind, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, seq_value, kind, canonical_json(event_payload), created_at),
            )
        return RunEvent(
            run_id=run_id,
            seq=seq_value,
            kind=kind,
            payload=event_payload,
            created_at=created_at,
        )

    def finish_run(
        self,
        run_id: str,
        completion: RunCompletion,
        *,
        expected_manual_state_revision: int | None = None,
    ) -> RunRecord:
        finished_at = _now()
        with self._write() as connection:
            current_row = cast(
                sqlite3.Row | None,
                connection.execute(
                    "SELECT * FROM runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone(),
            )
            if current_row is None:
                raise StateStoreNotFoundError(f"run does not exist: {run_id}")
            if _row_text(current_row, "status") != "running":
                raise StateStoreConflictError("run already has a terminal state")
            repository_id = _row_text(current_row, "repository_id")
            if expected_manual_state_revision is not None:
                revision_row = cast(
                    sqlite3.Row | None,
                    connection.execute(
                        "SELECT revision FROM manual_state WHERE repository_id = ?",
                        (repository_id,),
                    ).fetchone(),
                )
                if revision_row is None:
                    raise StateStoreNotFoundError(
                        f"repository manual state does not exist: {repository_id}"
                    )
                if _row_int(revision_row, "revision") != expected_manual_state_revision:
                    raise ManualStateRevisionChangedError(
                        "manual decision/alias state changed during the run"
                    )
            current_kinds = _run_event_kinds(connection, run_id)
            final_kinds = (*current_kinds, "run_finished")
            final_group_ids = (*_run_event_group_ids(connection, run_id), None)
            if not _lifecycle_is_valid(
                _row_text(current_row, "mode"),
                completion.status,
                final_kinds,
                final_group_ids,
            ):
                raise StateStoreConflictError(
                    f"run event contract mismatch for {run_id}: {final_kinds}"
                )
            snapshot_json = (
                canonical_json(completion.snapshot)
                if completion.snapshot is not None
                else _row_text(current_row, "snapshot_json")
            )
            usage_json = (
                canonical_json(completion.usage)
                if completion.usage is not None
                else _row_text(current_row, "usage_json")
            )
            connection.execute(
                """
                UPDATE runs
                SET snapshot_json = ?, usage_json = ?, status = ?, finding_count = ?,
                    suppressed_count = ?, fixed_count = ?, model_calls = ?,
                    finished_at = ?
                WHERE run_id = ? AND status = 'running'
                """,
                (
                    snapshot_json,
                    usage_json,
                    completion.status,
                    completion.finding_count,
                    completion.suppressed_count,
                    completion.fixed_count,
                    completion.model_calls,
                    finished_at,
                    run_id,
                ),
            )
            seq_value = cast(
                object,
                connection.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM run_events WHERE run_id = ?",
                    (run_id,),
                ).fetchone()[0],
            )
            if not isinstance(seq_value, int):
                raise StateStoreCorruptError("next run event sequence is invalid")
            connection.execute(
                """
                INSERT INTO run_events (run_id, seq, kind, payload_json, created_at)
                VALUES (?, ?, 'run_finished', ?, ?)
                """,
                (
                    run_id,
                    seq_value,
                    canonical_json(completion.event_payload),
                    finished_at,
                ),
            )
            final_row = cast(
                sqlite3.Row,
                connection.execute(
                    "SELECT * FROM runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone(),
            )
            result = _run_from_row(final_row)
        return result

    def run(self, run_id: str) -> RunRecord | None:
        with self._read() as connection:
            row = cast(
                sqlite3.Row | None,
                connection.execute(
                    "SELECT * FROM runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone(),
            )
        return None if row is None else _run_from_row(row)

    def list_runs(self, repository_id: str) -> list[RunRecord]:
        with self._read() as connection:
            rows = cast(
                list[sqlite3.Row],
                connection.execute(
                    """
                    SELECT * FROM runs
                    WHERE repository_id = ?
                    ORDER BY started_at, run_id
                    """,
                    (repository_id,),
                ).fetchall(),
            )
        return [_run_from_row(row) for row in rows]

    def events(self, run_id: str) -> list[RunEvent]:
        with self._read() as connection:
            rows = cast(
                list[sqlite3.Row],
                connection.execute(
                    """
                    SELECT * FROM run_events
                    WHERE run_id = ?
                    ORDER BY seq
                    """,
                    (run_id,),
                ).fetchall(),
            )
        return [
            RunEvent(
                run_id=_row_text(row, "run_id"),
                seq=_row_int(row, "seq"),
                kind=cast(RunEventKind, _row_text(row, "kind")),
                payload=_decode_object(_row_text(row, "payload_json")),
                created_at=_row_text(row, "created_at"),
            )
            for row in rows
        ]

    def record_finding(self, finding: PersistedFinding) -> PersistedFinding:
        validity = finding.validity
        if validity.repository_id == "":
            raise ValueError("finding repository_id must not be empty")
        with self._write() as connection:
            run_row = cast(
                sqlite3.Row | None,
                connection.execute(
                    "SELECT repository_id, status FROM runs WHERE run_id = ?",
                    (finding.run_id,),
                ).fetchone(),
            )
            if run_row is None:
                raise StateStoreNotFoundError(f"run does not exist: {finding.run_id}")
            if _row_text(run_row, "status") != "running":
                raise StateStoreConflictError("cannot add finding evidence after run completion")
            if _row_text(run_row, "repository_id") != validity.repository_id:
                raise StateStoreConflictError("finding evidence belongs to a different repository")
            existing = cast(
                sqlite3.Row | None,
                connection.execute(
                    """
                    SELECT * FROM run_findings
                    WHERE run_id = ? AND finding_id = ?
                    """,
                    (finding.run_id, finding.finding_id),
                ).fetchone(),
            )
            if existing is not None:
                stored = PersistedFinding(
                    run_id=_row_text(existing, "run_id"),
                    finding_id=_row_text(existing, "finding_id"),
                    validity=_finding_key_from_row(existing),
                )
                if stored != finding:
                    raise StateStoreConflictError(
                        "finding id already exists with different validity material"
                    )
                return stored
            connection.execute(
                """
                INSERT INTO run_findings (
                    run_id, finding_id, repository_id, symbol_id, kind,
                    component_id, normalized_old, normalized_new,
                    code_evidence_hash, doc_evidence_hash,
                    detector_id, detector_version, fingerprint, validity_digest
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    finding.run_id,
                    finding.finding_id,
                    validity.repository_id,
                    validity.symbol_id,
                    validity.kind,
                    validity.component_id,
                    validity.normalized_old,
                    validity.normalized_new,
                    validity.code_evidence_hash,
                    validity.doc_evidence_hash,
                    validity.detector_id,
                    validity.detector_version,
                    validity.fingerprint,
                    validity.digest,
                ),
            )
        return finding

    def finding(self, run_id: str, finding_id: str) -> PersistedFinding | None:
        with self._read() as connection:
            row = cast(
                sqlite3.Row | None,
                connection.execute(
                    """
                    SELECT * FROM run_findings
                    WHERE run_id = ? AND finding_id = ?
                    """,
                    (run_id, finding_id),
                ).fetchone(),
            )
        if row is None:
            return None
        persisted = PersistedFinding(
            run_id=_row_text(row, "run_id"),
            finding_id=_row_text(row, "finding_id"),
            validity=_finding_key_from_row(row),
        )
        if _row_text(row, "validity_digest") != persisted.validity.digest:
            raise StateStoreCorruptError("finding validity digest does not match material")
        return persisted

    def findings(self, run_id: str) -> list[PersistedFinding]:
        with self._read() as connection:
            rows = cast(
                list[sqlite3.Row],
                connection.execute(
                    """
                    SELECT * FROM run_findings
                    WHERE run_id = ?
                    ORDER BY finding_id
                    """,
                    (run_id,),
                ).fetchall(),
            )
        findings = [
            PersistedFinding(
                run_id=_row_text(row, "run_id"),
                finding_id=_row_text(row, "finding_id"),
                validity=_finding_key_from_row(row),
            )
            for row in rows
        ]
        if any(
            _row_text(row, "validity_digest") != finding.validity.digest
            for row, finding in zip(rows, findings, strict=True)
        ):
            raise StateStoreCorruptError("finding validity digest does not match material")
        return findings

    def record_alignment(self, alignment: PersistedAlignment) -> PersistedAlignment:
        evidence = alignment.evidence
        if evidence.repository_id == "":
            raise ValueError("alignment repository_id must not be empty")
        with self._write() as connection:
            run_row = cast(
                sqlite3.Row | None,
                connection.execute(
                    "SELECT repository_id, status FROM runs WHERE run_id = ?",
                    (alignment.run_id,),
                ).fetchone(),
            )
            if run_row is None:
                raise StateStoreNotFoundError(f"run does not exist: {alignment.run_id}")
            if _row_text(run_row, "status") != "running":
                raise StateStoreConflictError("cannot add alignment evidence after run completion")
            if _row_text(run_row, "repository_id") != evidence.repository_id:
                raise StateStoreConflictError(
                    "alignment evidence belongs to a different repository"
                )
            existing = cast(
                sqlite3.Row | None,
                connection.execute(
                    """
                    SELECT * FROM run_alignments
                    WHERE run_id = ? AND alignment_id = ?
                    """,
                    (alignment.run_id, alignment.alignment_id),
                ).fetchone(),
            )
            if existing is not None:
                stored = PersistedAlignment(
                    run_id=_row_text(existing, "run_id"),
                    alignment_id=_row_text(existing, "alignment_id"),
                    evidence=_alignment_key_from_row(existing),
                )
                if stored != alignment:
                    raise StateStoreConflictError(
                        "alignment id already exists with different evidence material"
                    )
                return stored
            connection.execute(
                """
                INSERT INTO run_alignments (
                    run_id, alignment_id, repository_id,
                    old_symbol_id, new_symbol_id, confirmation_commit,
                    old_blob_id, old_evidence_hash, new_evidence_hash,
                    doc_evidence_hash, aligner_id, aligner_version, evidence_digest
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    alignment.run_id,
                    alignment.alignment_id,
                    evidence.repository_id,
                    evidence.old_symbol_id,
                    evidence.new_symbol_id,
                    evidence.confirmation_commit,
                    evidence.old_blob_id,
                    evidence.old_evidence_hash,
                    evidence.new_evidence_hash,
                    evidence.doc_evidence_hash,
                    evidence.aligner_id,
                    evidence.aligner_version,
                    evidence.digest,
                ),
            )
        return alignment

    def alignment(self, run_id: str, alignment_id: str) -> PersistedAlignment | None:
        with self._read() as connection:
            row = cast(
                sqlite3.Row | None,
                connection.execute(
                    """
                    SELECT * FROM run_alignments
                    WHERE run_id = ? AND alignment_id = ?
                    """,
                    (run_id, alignment_id),
                ).fetchone(),
            )
        if row is None:
            return None
        persisted = PersistedAlignment(
            run_id=_row_text(row, "run_id"),
            alignment_id=_row_text(row, "alignment_id"),
            evidence=_alignment_key_from_row(row),
        )
        if _row_text(row, "evidence_digest") != persisted.evidence.digest:
            raise StateStoreCorruptError("alignment evidence digest does not match material")
        return persisted

    def manual_state_revision(self, repository_id: str) -> int:
        with self._read() as connection:
            row = cast(
                sqlite3.Row | None,
                connection.execute(
                    "SELECT revision FROM manual_state WHERE repository_id = ?",
                    (repository_id,),
                ).fetchone(),
            )
        if row is None:
            raise StateStoreNotFoundError(
                f"repository manual state does not exist: {repository_id}"
            )
        return _row_int(row, "revision")

    @staticmethod
    def _increment_manual_state(
        connection: sqlite3.Connection,
        repository_id: str,
    ) -> None:
        cursor = connection.execute(
            """
            UPDATE manual_state
            SET revision = revision + 1
            WHERE repository_id = ?
            """,
            (repository_id,),
        )
        if cursor.rowcount != 1:
            raise StateStoreNotFoundError(
                f"repository manual state does not exist: {repository_id}"
            )

    def append_memory_event(
        self,
        *,
        repository_id: str,
        run_id: str | None,
        kind: str,
        subject_type: str,
        subject_id: str,
        reason: str,
        payload: dict[str, object] | None = None,
    ) -> MemoryEvent:
        created_at = _now()
        event_payload = payload or {}
        with self._write() as connection:
            cursor = connection.execute(
                """
                INSERT INTO memory_events (
                    repository_id, run_id, kind, subject_type,
                    subject_id, reason, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    repository_id,
                    run_id,
                    kind,
                    subject_type,
                    subject_id,
                    reason,
                    canonical_json(event_payload),
                    created_at,
                ),
            )
            event_id = cursor.lastrowid
            if event_id is None:
                raise StateStoreWriteError("memory event id was not generated")
        return MemoryEvent(
            event_id=event_id,
            repository_id=repository_id,
            run_id=run_id,
            kind=kind,
            subject_type=subject_type,
            subject_id=subject_id,
            reason=reason,
            payload=event_payload,
            created_at=created_at,
        )

    def memory_events(self, repository_id: str) -> list[MemoryEvent]:
        with self._read() as connection:
            rows = cast(
                list[sqlite3.Row],
                connection.execute(
                    """
                    SELECT * FROM memory_events
                    WHERE repository_id = ?
                    ORDER BY event_id
                    """,
                    (repository_id,),
                ).fetchall(),
            )
        return [
            MemoryEvent(
                event_id=_row_int(row, "event_id"),
                repository_id=_row_text(row, "repository_id"),
                run_id=_row_optional_text(row, "run_id"),
                kind=_row_text(row, "kind"),
                subject_type=_row_text(row, "subject_type"),
                subject_id=_row_text(row, "subject_id"),
                reason=_row_text(row, "reason"),
                payload=_decode_object(_row_text(row, "payload_json")),
                created_at=_row_text(row, "created_at"),
            )
            for row in rows
        ]

    @staticmethod
    def _completed_finding(
        connection: sqlite3.Connection,
        repository_id: str,
        run_id: str,
        finding_id: str,
    ) -> FindingValidityKey:
        run_row = cast(
            sqlite3.Row | None,
            connection.execute(
                "SELECT * FROM runs WHERE run_id = ? AND repository_id = ?",
                (run_id, repository_id),
            ).fetchone(),
        )
        if (
            run_row is None
            or _row_text(run_row, "status") in {"running", "failed", "stale"}
            or _row_optional_text(run_row, "finished_at") is None
            or not _lifecycle_is_valid(
                _row_text(run_row, "mode"),
                _row_text(run_row, "status"),
                _run_event_kinds(connection, run_id),
                _run_event_group_ids(connection, run_id),
            )
        ):
            raise StateStoreNotFoundError(
                "decision source must be a finding from a lifecycle-valid completed run"
            )
        row = cast(
            sqlite3.Row | None,
            connection.execute(
                """
                SELECT f.*
                FROM run_findings AS f
                JOIN runs AS r ON r.run_id = f.run_id
                WHERE f.repository_id = ?
                  AND f.run_id = ?
                  AND f.finding_id = ?
                  AND r.status <> 'running'
                  AND r.finished_at IS NOT NULL
                """,
                (repository_id, run_id, finding_id),
            ).fetchone(),
        )
        if row is None:
            raise StateStoreNotFoundError("decision source must be a finding from a completed run")
        validity = _finding_key_from_row(row)
        if _row_text(row, "validity_digest") != validity.digest:
            raise StateStoreCorruptError("finding validity digest does not match material")
        return validity

    @staticmethod
    def _completed_alignment(
        connection: sqlite3.Connection,
        repository_id: str,
        run_id: str,
        alignment_id: str,
    ) -> AlignmentEvidenceKey:
        run_row = cast(
            sqlite3.Row | None,
            connection.execute(
                "SELECT * FROM runs WHERE run_id = ? AND repository_id = ?",
                (run_id, repository_id),
            ).fetchone(),
        )
        if (
            run_row is None
            or _row_text(run_row, "status") in {"running", "failed", "stale"}
            or _row_optional_text(run_row, "finished_at") is None
            or not _lifecycle_is_valid(
                _row_text(run_row, "mode"),
                _row_text(run_row, "status"),
                _run_event_kinds(connection, run_id),
                _run_event_group_ids(connection, run_id),
            )
        ):
            raise StateStoreNotFoundError(
                "alias source must be an alignment from a lifecycle-valid completed run"
            )
        row = cast(
            sqlite3.Row | None,
            connection.execute(
                """
                SELECT a.*
                FROM run_alignments AS a
                JOIN runs AS r ON r.run_id = a.run_id
                WHERE a.repository_id = ?
                  AND a.run_id = ?
                  AND a.alignment_id = ?
                  AND r.status <> 'running'
                  AND r.finished_at IS NOT NULL
                """,
                (repository_id, run_id, alignment_id),
            ).fetchone(),
        )
        if row is None:
            raise StateStoreNotFoundError("alias source must be an alignment from a completed run")
        evidence = _alignment_key_from_row(row)
        if _row_text(row, "evidence_digest") != evidence.digest:
            raise StateStoreCorruptError("alignment evidence digest does not match material")
        return evidence

    def add_decision(self, request: DecisionAddRequest) -> DecisionRecord:
        reason = _required_text(request.reason, "reason")
        actor = _required_text(request.actor, "actor")
        if not request.confirmation:
            raise ValueError("decision add requires explicit confirmation")
        created_at = _now()
        with self._write() as connection:
            validity = self._completed_finding(
                connection,
                request.repository_id,
                request.run_id,
                request.finding_id,
            )
            active_rows = cast(
                list[sqlite3.Row],
                connection.execute(
                    _DECISION_SELECT
                    + """
                    WHERE d.repository_id = ?
                      AND d.validity_digest = ?
                      AND r.decision_id IS NULL
                    ORDER BY d.created_at, d.decision_id
                    """,
                    (request.repository_id, validity.digest),
                ).fetchall(),
            )
            for row in active_rows:
                existing = _decision_from_row(row)
                if existing.validity != validity:
                    raise StateStoreConflictError("decision validity digest collision detected")
                if existing.action == request.action:
                    return existing
            if active_rows:
                raise StateStoreConflictError("a conflicting active decision must be revoked first")

            decision_id = _new_id("decision")
            connection.execute(
                """
                INSERT INTO decisions (
                    decision_id, repository_id, source_run_id, source_finding_id,
                    action, reason, actor, confirmation,
                    symbol_id, kind, component_id,
                    normalized_old, normalized_new,
                    code_evidence_hash, doc_evidence_hash,
                    detector_id, detector_version, fingerprint,
                    validity_digest, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    request.repository_id,
                    request.run_id,
                    request.finding_id,
                    request.action,
                    reason,
                    actor,
                    validity.symbol_id,
                    validity.kind,
                    validity.component_id,
                    validity.normalized_old,
                    validity.normalized_new,
                    validity.code_evidence_hash,
                    validity.doc_evidence_hash,
                    validity.detector_id,
                    validity.detector_version,
                    validity.fingerprint,
                    validity.digest,
                    created_at,
                ),
            )
            self._increment_manual_state(connection, request.repository_id)
            record = DecisionRecord(
                decision_id=decision_id,
                source_run_id=request.run_id,
                source_finding_id=request.finding_id,
                action=request.action,
                reason=reason,
                actor=actor,
                confirmation=True,
                validity=validity,
                created_at=created_at,
            )
        return record

    def list_decisions(
        self,
        repository_id: str,
        *,
        include_revoked: bool = True,
    ) -> list[DecisionRecord]:
        active_filter = "" if include_revoked else "AND r.decision_id IS NULL"
        with self._read() as connection:
            rows = cast(
                list[sqlite3.Row],
                connection.execute(
                    _DECISION_SELECT
                    + f"""
                    WHERE d.repository_id = ? {active_filter}
                    ORDER BY d.created_at, d.decision_id
                    """,
                    (repository_id,),
                ).fetchall(),
            )
        return [_decision_from_row(row) for row in rows]

    def revoke_decision(self, request: DecisionRevokeRequest) -> DecisionRecord:
        reason = _required_text(request.reason, "reason")
        actor = _required_text(request.actor, "actor")
        created_at = _now()
        with self._write() as connection:
            row = cast(
                sqlite3.Row | None,
                connection.execute(
                    _DECISION_SELECT
                    + """
                    WHERE d.repository_id = ? AND d.decision_id = ?
                    """,
                    (request.repository_id, request.decision_id),
                ).fetchone(),
            )
            if row is None:
                raise StateStoreNotFoundError(f"decision does not exist: {request.decision_id}")
            existing = _decision_from_row(row)
            if not existing.active:
                return existing
            connection.execute(
                """
                INSERT INTO decision_revocations (
                    revocation_id, decision_id, reason, actor, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (_new_id("decision_revoke"), request.decision_id, reason, actor, created_at),
            )
            self._increment_manual_state(connection, request.repository_id)
            revoked = replace(
                existing,
                revoked_at=created_at,
                revoked_reason=reason,
                revoked_actor=actor,
            )
        return revoked

    def matching_decision(self, validity: FindingValidityKey) -> DecisionRecord | None:
        with self._read() as connection:
            rows = cast(
                list[sqlite3.Row],
                connection.execute(
                    _DECISION_SELECT
                    + """
                    WHERE d.repository_id = ?
                      AND d.validity_digest = ?
                      AND r.decision_id IS NULL
                    ORDER BY d.created_at, d.decision_id
                    """,
                    (validity.repository_id, validity.digest),
                ).fetchall(),
            )
        if len(rows) > 1:
            raise StateStoreConflictError("multiple active decisions match one validity key")
        if not rows:
            return None
        record = _decision_from_row(rows[0])
        if record.validity != validity:
            raise StateStoreConflictError("decision validity digest collision detected")
        return record

    def decision_candidates(
        self,
        repository_id: str,
        *,
        symbol_id: str | None = None,
    ) -> list[DecisionRecord]:
        symbol_filter = "" if symbol_id is None else "AND d.symbol_id = ?"
        parameters: tuple[str, ...] = (
            (repository_id,) if symbol_id is None else (repository_id, symbol_id)
        )
        with self._read() as connection:
            rows = cast(
                list[sqlite3.Row],
                connection.execute(
                    _DECISION_SELECT
                    + f"""
                    WHERE d.repository_id = ?
                      AND r.decision_id IS NULL
                      {symbol_filter}
                    ORDER BY d.created_at, d.decision_id
                    """,
                    parameters,
                ).fetchall(),
            )
        return [_decision_from_row(row) for row in rows]

    def add_alias(self, request: AliasAddRequest) -> AliasRecord:
        reason = _required_text(request.reason, "reason")
        actor = _required_text(request.actor, "actor")
        if not request.confirmation:
            raise ValueError("alias add requires explicit confirmation")
        created_at = _now()
        with self._write() as connection:
            evidence = self._completed_alignment(
                connection,
                request.repository_id,
                request.run_id,
                request.alignment_id,
            )
            active_rows = cast(
                list[sqlite3.Row],
                connection.execute(
                    _ALIAS_SELECT
                    + """
                    WHERE a.repository_id = ?
                      AND a.old_symbol_id = ?
                      AND r.alias_id IS NULL
                    ORDER BY a.created_at, a.alias_id
                    """,
                    (request.repository_id, evidence.old_symbol_id),
                ).fetchall(),
            )
            for row in active_rows:
                existing = _alias_from_row(row)
                if existing.evidence == evidence:
                    return existing
            if active_rows:
                raise StateStoreConflictError("a conflicting active alias must be revoked first")

            alias_id = _new_id("alias")
            connection.execute(
                """
                INSERT INTO aliases (
                    alias_id, repository_id, source_run_id, source_alignment_id,
                    reason, actor, confirmation,
                    old_symbol_id, new_symbol_id, confirmation_commit,
                    old_blob_id, old_evidence_hash, new_evidence_hash,
                    doc_evidence_hash, aligner_id, aligner_version,
                    evidence_digest, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    alias_id,
                    request.repository_id,
                    request.run_id,
                    request.alignment_id,
                    reason,
                    actor,
                    evidence.old_symbol_id,
                    evidence.new_symbol_id,
                    evidence.confirmation_commit,
                    evidence.old_blob_id,
                    evidence.old_evidence_hash,
                    evidence.new_evidence_hash,
                    evidence.doc_evidence_hash,
                    evidence.aligner_id,
                    evidence.aligner_version,
                    evidence.digest,
                    created_at,
                ),
            )
            self._increment_manual_state(connection, request.repository_id)
            record = AliasRecord(
                alias_id=alias_id,
                source_run_id=request.run_id,
                source_alignment_id=request.alignment_id,
                reason=reason,
                actor=actor,
                confirmation=True,
                evidence=evidence,
                created_at=created_at,
            )
        return record

    def list_aliases(
        self,
        repository_id: str,
        *,
        include_revoked: bool = True,
    ) -> list[AliasRecord]:
        active_filter = "" if include_revoked else "AND r.alias_id IS NULL"
        with self._read() as connection:
            rows = cast(
                list[sqlite3.Row],
                connection.execute(
                    _ALIAS_SELECT
                    + f"""
                    WHERE a.repository_id = ? {active_filter}
                    ORDER BY a.created_at, a.alias_id
                    """,
                    (repository_id,),
                ).fetchall(),
            )
        return [_alias_from_row(row) for row in rows]

    def revoke_alias(self, request: AliasRevokeRequest) -> AliasRecord:
        reason = _required_text(request.reason, "reason")
        actor = _required_text(request.actor, "actor")
        created_at = _now()
        with self._write() as connection:
            row = cast(
                sqlite3.Row | None,
                connection.execute(
                    _ALIAS_SELECT
                    + """
                    WHERE a.repository_id = ? AND a.alias_id = ?
                    """,
                    (request.repository_id, request.alias_id),
                ).fetchone(),
            )
            if row is None:
                raise StateStoreNotFoundError(f"alias does not exist: {request.alias_id}")
            existing = _alias_from_row(row)
            if not existing.active:
                return existing
            connection.execute(
                """
                INSERT INTO alias_revocations (
                    revocation_id, alias_id, reason, actor, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (_new_id("alias_revoke"), request.alias_id, reason, actor, created_at),
            )
            self._increment_manual_state(connection, request.repository_id)
            revoked = replace(
                existing,
                revoked_at=created_at,
                revoked_reason=reason,
                revoked_actor=actor,
            )
        return revoked

    def alias_candidates(
        self,
        repository_id: str,
        old_symbol_id: str,
    ) -> list[AliasRecord]:
        with self._read() as connection:
            rows = cast(
                list[sqlite3.Row],
                connection.execute(
                    _ALIAS_SELECT
                    + """
                    WHERE a.repository_id = ?
                      AND a.old_symbol_id = ?
                      AND r.alias_id IS NULL
                    ORDER BY a.created_at, a.alias_id
                    """,
                    (repository_id, old_symbol_id),
                ).fetchall(),
            )
        return [_alias_from_row(row) for row in rows]

    def matching_alias(self, evidence: AlignmentEvidenceKey) -> AliasRecord | None:
        candidates = self.alias_candidates(
            evidence.repository_id,
            evidence.old_symbol_id,
        )
        exact = [candidate for candidate in candidates if candidate.evidence == evidence]
        if len(exact) > 1:
            raise StateStoreConflictError("multiple active aliases match one evidence key")
        return exact[0] if exact else None


StateStore = SQLiteStateStore

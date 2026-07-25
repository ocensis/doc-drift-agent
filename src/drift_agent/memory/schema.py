from __future__ import annotations

import math
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import cast

SCHEMA_VERSION = 1


class StateStoreError(RuntimeError):
    reason_code = "state_store"


class StateStorePathError(StateStoreError):
    reason_code = "state_store.path"


class StateStoreCorruptError(StateStoreError):
    reason_code = "state_store.corrupt"


class StateStoreVersionError(StateStoreError):
    reason_code = "state_store.version"


class StateStoreMigrationError(StateStoreError):
    reason_code = "state_store.migration"


class StateStoreWriteError(StateStoreError):
    reason_code = "state_store.write"


class StateStoreConflictError(StateStoreError):
    reason_code = "state_store.conflict"


class ManualStateRevisionChangedError(StateStoreError):
    reason_code = "global_snapshot_changed"


class StateStoreNotFoundError(StateStoreError):
    reason_code = "state_store.not_found"


class RepositoryIdentityCollisionError(StateStoreError):
    reason_code = "repository_identity_collision"


MigrationHook = Callable[[sqlite3.Connection, int, int], None]


_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE repositories (
        repository_id TEXT PRIMARY KEY,
        material TEXT NOT NULL,
        common_dir TEXT NOT NULL,
        root_commit TEXT NOT NULL,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE workspaces (
        workspace_id TEXT PRIMARY KEY,
        repository_id TEXT NOT NULL REFERENCES repositories(repository_id),
        material TEXT NOT NULL,
        worktree_root TEXT NOT NULL,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        UNIQUE (workspace_id, repository_id)
    ) STRICT
    """,
    """
    CREATE TABLE runs (
        run_id TEXT PRIMARY KEY,
        repository_id TEXT NOT NULL REFERENCES repositories(repository_id),
        workspace_id TEXT NOT NULL,
        mode TEXT NOT NULL CHECK (mode IN ('check', 'repair')),
        request_json TEXT NOT NULL,
        snapshot_json TEXT NOT NULL,
        usage_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK (
            status IN (
                'running', 'clean', 'drift_found', 'fixed', 'partial',
                'needs_approval', 'unresolved', 'stale', 'failed'
            )
        ),
        finding_count INTEGER NOT NULL DEFAULT 0 CHECK (finding_count >= 0),
        suppressed_count INTEGER NOT NULL DEFAULT 0 CHECK (suppressed_count >= 0),
        fixed_count INTEGER NOT NULL DEFAULT 0 CHECK (fixed_count >= 0),
        model_calls INTEGER NOT NULL DEFAULT 0 CHECK (model_calls >= 0),
        started_at TEXT NOT NULL,
        finished_at TEXT,
        UNIQUE (run_id, repository_id),
        FOREIGN KEY (workspace_id, repository_id)
            REFERENCES workspaces(workspace_id, repository_id),
        CHECK (
            (status = 'running' AND finished_at IS NULL)
            OR (status <> 'running' AND finished_at IS NOT NULL)
        )
    ) STRICT
    """,
    """
    CREATE TABLE run_events (
        run_id TEXT NOT NULL REFERENCES runs(run_id),
        seq INTEGER NOT NULL CHECK (seq > 0),
        kind TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (run_id, seq)
    ) STRICT
    """,
    """
    CREATE TRIGGER run_events_contiguous
    BEFORE INSERT ON run_events
    WHEN NEW.seq <> COALESCE(
        (SELECT MAX(seq) + 1 FROM run_events WHERE run_id = NEW.run_id),
        1
    )
    BEGIN
        SELECT RAISE(ABORT, 'run event sequence must be contiguous');
    END
    """,
    """
    CREATE UNIQUE INDEX run_events_one_terminal
        ON run_events(run_id)
        WHERE kind = 'run_finished'
    """,
    """
    CREATE TABLE run_findings (
        run_id TEXT NOT NULL,
        finding_id TEXT NOT NULL,
        repository_id TEXT NOT NULL,
        symbol_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        component_id TEXT NOT NULL,
        normalized_old TEXT NOT NULL,
        normalized_new TEXT NOT NULL,
        code_evidence_hash TEXT NOT NULL,
        doc_evidence_hash TEXT NOT NULL,
        detector_id TEXT NOT NULL,
        detector_version TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        validity_digest TEXT NOT NULL,
        PRIMARY KEY (run_id, finding_id),
        UNIQUE (run_id, finding_id, repository_id),
        FOREIGN KEY (run_id, repository_id)
            REFERENCES runs(run_id, repository_id)
    ) STRICT
    """,
    """
    CREATE INDEX run_findings_fingerprint
        ON run_findings(repository_id, fingerprint)
    """,
    """
    CREATE TABLE run_alignments (
        run_id TEXT NOT NULL,
        alignment_id TEXT NOT NULL,
        repository_id TEXT NOT NULL,
        old_symbol_id TEXT NOT NULL,
        new_symbol_id TEXT NOT NULL,
        confirmation_commit TEXT NOT NULL,
        old_blob_id TEXT NOT NULL,
        old_evidence_hash TEXT NOT NULL,
        new_evidence_hash TEXT NOT NULL,
        doc_evidence_hash TEXT NOT NULL,
        aligner_id TEXT NOT NULL,
        aligner_version TEXT NOT NULL,
        evidence_digest TEXT NOT NULL,
        PRIMARY KEY (run_id, alignment_id),
        UNIQUE (run_id, alignment_id, repository_id),
        FOREIGN KEY (run_id, repository_id)
            REFERENCES runs(run_id, repository_id)
    ) STRICT
    """,
    """
    CREATE TABLE decisions (
        decision_id TEXT PRIMARY KEY,
        repository_id TEXT NOT NULL REFERENCES repositories(repository_id),
        source_run_id TEXT NOT NULL,
        source_finding_id TEXT NOT NULL,
        action TEXT NOT NULL CHECK (action IN ('ignore', 'false_positive')),
        reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
        actor TEXT NOT NULL CHECK (length(trim(actor)) > 0),
        confirmation INTEGER NOT NULL CHECK (confirmation = 1),
        symbol_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        component_id TEXT NOT NULL,
        normalized_old TEXT NOT NULL,
        normalized_new TEXT NOT NULL,
        code_evidence_hash TEXT NOT NULL,
        doc_evidence_hash TEXT NOT NULL,
        detector_id TEXT NOT NULL,
        detector_version TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        validity_digest TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (source_run_id, source_finding_id, repository_id)
            REFERENCES run_findings(run_id, finding_id, repository_id)
    ) STRICT
    """,
    """
    CREATE INDEX decisions_lookup
        ON decisions(repository_id, validity_digest, action)
    """,
    """
    CREATE INDEX decisions_symbol_lookup
        ON decisions(repository_id, symbol_id, created_at)
    """,
    """
    CREATE TABLE decision_revocations (
        revocation_id TEXT PRIMARY KEY,
        decision_id TEXT NOT NULL UNIQUE REFERENCES decisions(decision_id),
        reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
        actor TEXT NOT NULL CHECK (length(trim(actor)) > 0),
        created_at TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE aliases (
        alias_id TEXT PRIMARY KEY,
        repository_id TEXT NOT NULL REFERENCES repositories(repository_id),
        source_run_id TEXT NOT NULL,
        source_alignment_id TEXT NOT NULL,
        reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
        actor TEXT NOT NULL CHECK (length(trim(actor)) > 0),
        confirmation INTEGER NOT NULL CHECK (confirmation = 1),
        old_symbol_id TEXT NOT NULL,
        new_symbol_id TEXT NOT NULL,
        confirmation_commit TEXT NOT NULL,
        old_blob_id TEXT NOT NULL,
        old_evidence_hash TEXT NOT NULL,
        new_evidence_hash TEXT NOT NULL,
        doc_evidence_hash TEXT NOT NULL,
        aligner_id TEXT NOT NULL,
        aligner_version TEXT NOT NULL,
        evidence_digest TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (source_run_id, source_alignment_id, repository_id)
            REFERENCES run_alignments(run_id, alignment_id, repository_id)
    ) STRICT
    """,
    """
    CREATE INDEX aliases_lookup
        ON aliases(repository_id, old_symbol_id, created_at)
    """,
    """
    CREATE TABLE alias_revocations (
        revocation_id TEXT PRIMARY KEY,
        alias_id TEXT NOT NULL UNIQUE REFERENCES aliases(alias_id),
        reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
        actor TEXT NOT NULL CHECK (length(trim(actor)) > 0),
        created_at TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE manual_state (
        repository_id TEXT PRIMARY KEY REFERENCES repositories(repository_id),
        revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0)
    ) STRICT
    """,
    """
    CREATE TABLE memory_events (
        event_id INTEGER PRIMARY KEY,
        repository_id TEXT NOT NULL REFERENCES repositories(repository_id),
        run_id TEXT REFERENCES runs(run_id),
        kind TEXT NOT NULL,
        subject_type TEXT NOT NULL,
        subject_id TEXT NOT NULL,
        reason TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE INDEX memory_events_subject
        ON memory_events(repository_id, subject_type, subject_id, event_id)
    """,
)


_REQUIRED_COLUMNS: dict[str, frozenset[str]] = {
    "repositories": frozenset(
        {
            "repository_id",
            "material",
            "common_dir",
            "root_commit",
            "first_seen_at",
            "last_seen_at",
        }
    ),
    "workspaces": frozenset(
        {
            "workspace_id",
            "repository_id",
            "material",
            "worktree_root",
            "first_seen_at",
            "last_seen_at",
        }
    ),
    "runs": frozenset(
        {
            "run_id",
            "repository_id",
            "workspace_id",
            "mode",
            "request_json",
            "snapshot_json",
            "usage_json",
            "status",
            "finding_count",
            "suppressed_count",
            "fixed_count",
            "model_calls",
            "started_at",
            "finished_at",
        }
    ),
    "run_events": frozenset({"run_id", "seq", "kind", "payload_json", "created_at"}),
    "run_findings": frozenset(
        {
            "run_id",
            "finding_id",
            "repository_id",
            "symbol_id",
            "kind",
            "component_id",
            "normalized_old",
            "normalized_new",
            "code_evidence_hash",
            "doc_evidence_hash",
            "detector_id",
            "detector_version",
            "fingerprint",
            "validity_digest",
        }
    ),
    "run_alignments": frozenset(
        {
            "run_id",
            "alignment_id",
            "repository_id",
            "old_symbol_id",
            "new_symbol_id",
            "confirmation_commit",
            "old_blob_id",
            "old_evidence_hash",
            "new_evidence_hash",
            "doc_evidence_hash",
            "aligner_id",
            "aligner_version",
            "evidence_digest",
        }
    ),
    "decisions": frozenset(
        {
            "decision_id",
            "repository_id",
            "source_run_id",
            "source_finding_id",
            "action",
            "reason",
            "actor",
            "confirmation",
            "symbol_id",
            "kind",
            "component_id",
            "normalized_old",
            "normalized_new",
            "code_evidence_hash",
            "doc_evidence_hash",
            "detector_id",
            "detector_version",
            "fingerprint",
            "validity_digest",
            "created_at",
        }
    ),
    "decision_revocations": frozenset(
        {"revocation_id", "decision_id", "reason", "actor", "created_at"}
    ),
    "aliases": frozenset(
        {
            "alias_id",
            "repository_id",
            "source_run_id",
            "source_alignment_id",
            "reason",
            "actor",
            "confirmation",
            "old_symbol_id",
            "new_symbol_id",
            "confirmation_commit",
            "old_blob_id",
            "old_evidence_hash",
            "new_evidence_hash",
            "doc_evidence_hash",
            "aligner_id",
            "aligner_version",
            "evidence_digest",
            "created_at",
        }
    ),
    "alias_revocations": frozenset({"revocation_id", "alias_id", "reason", "actor", "created_at"}),
    "manual_state": frozenset({"repository_id", "revision"}),
    "memory_events": frozenset(
        {
            "event_id",
            "repository_id",
            "run_id",
            "kind",
            "subject_type",
            "subject_id",
            "reason",
            "payload_json",
            "created_at",
        }
    ),
}


def _connect(path: Path, busy_timeout_seconds: float) -> sqlite3.Connection:
    timeout_ms = int(busy_timeout_seconds * 1000)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            path,
            timeout=busy_timeout_seconds,
            isolation_level=None,
        )
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {timeout_ms}")
        return connection
    except sqlite3.DatabaseError as error:
        if connection is not None:
            connection.close()
        raise StateStoreCorruptError(f"SQLite state is unreadable: {path}") from error


def _integrity_check(connection: sqlite3.Connection) -> None:
    try:
        rows = cast(list[tuple[str]], connection.execute("PRAGMA integrity_check").fetchall())
    except sqlite3.DatabaseError as error:
        raise StateStoreCorruptError("SQLite integrity check could not run") from error
    if rows != [("ok",)]:
        detail = "; ".join(row[0] for row in rows)
        raise StateStoreCorruptError(f"SQLite integrity check failed: {detail}")


def _application_tables(connection: sqlite3.Connection) -> frozenset[str]:
    rows = cast(
        list[tuple[str]],
        connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            """
        ).fetchall(),
    )
    return frozenset(row[0] for row in rows)


def _validate_schema(connection: sqlite3.Connection) -> None:
    tables = _application_tables(connection)
    missing_tables = set(_REQUIRED_COLUMNS) - tables
    if missing_tables:
        raise StateStoreCorruptError(
            f"SQLite schema is missing tables: {', '.join(sorted(missing_tables))}"
        )
    for table, required in _REQUIRED_COLUMNS.items():
        rows = cast(
            list[tuple[int, str, str, int, object, int]],
            connection.execute(f'PRAGMA table_info("{table}")').fetchall(),
        )
        actual = {row[1] for row in rows}
        missing_columns = required - actual
        if missing_columns:
            raise StateStoreCorruptError(
                f"SQLite table {table} is missing columns: {', '.join(sorted(missing_columns))}"
            )


def _set_wal(connection: sqlite3.Connection) -> None:
    try:
        row = cast(tuple[str] | None, connection.execute("PRAGMA journal_mode = WAL").fetchone())
        connection.execute("PRAGMA synchronous = FULL")
    except sqlite3.DatabaseError as error:
        raise StateStoreWriteError("could not enable SQLite WAL mode") from error
    if row is None or row[0].lower() != "wal":
        raise StateStoreWriteError("SQLite state did not enter WAL mode")


def _migrate_v0(
    connection: sqlite3.Connection,
    migration_hook: MigrationHook | None,
) -> None:
    try:
        connection.execute("BEGIN IMMEDIATE")
        version_row = cast(
            tuple[int] | None,
            connection.execute("PRAGMA user_version").fetchone(),
        )
        if version_row is None:
            raise StateStoreCorruptError("SQLite schema version is unavailable")
        current_version = version_row[0]
        if current_version == SCHEMA_VERSION:
            # Another process may have initialized the fresh database while
            # this connection waited for BEGIN IMMEDIATE.
            _validate_schema(connection)
            connection.commit()
            return
        if current_version != 0:
            raise StateStoreVersionError(f"schema changed concurrently from 0 to {current_version}")
        existing_tables = _application_tables(connection)
        if not existing_tables:
            for statement in _SCHEMA_STATEMENTS:
                connection.execute(statement)
        else:
            # Stage 2's only supported legacy form is the complete v1 layout
            # whose version marker was not yet published.  It is validated
            # before the marker changes, preserving every existing row.
            _validate_schema(connection)
        if migration_hook is not None:
            migration_hook(connection, 0, SCHEMA_VERSION)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.commit()
    except BaseException as error:
        if connection.in_transaction:
            connection.rollback()
        if isinstance(error, StateStoreError):
            raise
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        raise StateStoreMigrationError("SQLite schema migration failed") from error


def prepare_database(
    path: Path,
    *,
    busy_timeout_seconds: float = 5.0,
    migration_hook: MigrationHook | None = None,
) -> None:
    """Open, verify and migrate a state database without destructive recovery."""

    if not math.isfinite(busy_timeout_seconds) or busy_timeout_seconds < 0:
        raise ValueError("busy_timeout_seconds must be finite and non-negative")
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise StateStorePathError(f"state database must be a regular file: {path}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise StateStorePathError(f"could not create state directory: {path.parent}") from error
    if not path.parent.is_dir():
        raise StateStorePathError(f"state parent is not a directory: {path.parent}")

    connection = _connect(path, busy_timeout_seconds)
    try:
        _integrity_check(connection)
        try:
            version_row = cast(
                tuple[int] | None,
                connection.execute("PRAGMA user_version").fetchone(),
            )
        except sqlite3.DatabaseError as error:
            raise StateStoreCorruptError("could not read SQLite schema version") from error
        if version_row is None:
            raise StateStoreCorruptError("SQLite schema version is unavailable")
        version = version_row[0]
        if version > SCHEMA_VERSION:
            raise StateStoreVersionError(
                f"SQLite schema {version} is newer than supported {SCHEMA_VERSION}"
            )
        if version < 0:
            raise StateStoreVersionError(f"invalid SQLite schema version: {version}")
        _set_wal(connection)
        if version == 0:
            _migrate_v0(connection, migration_hook)
        elif version == SCHEMA_VERSION:
            _validate_schema(connection)
        else:
            raise StateStoreVersionError(f"no migration is available from schema {version}")
        _integrity_check(connection)
    finally:
        connection.close()


def connect_database(
    path: Path,
    *,
    busy_timeout_seconds: float = 5.0,
) -> sqlite3.Connection:
    """Create one short-transaction connection to an initialized store."""

    return _connect(path, busy_timeout_seconds)


def database_integrity(path: Path, *, busy_timeout_seconds: float = 5.0) -> bool:
    connection = _connect(path, busy_timeout_seconds)
    try:
        _integrity_check(connection)
    finally:
        connection.close()
    return True

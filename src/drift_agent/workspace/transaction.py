import os
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from functools import partial
from pathlib import Path
from typing import Literal

from drift_agent.domain.models import PatchAttempt
from drift_agent.hashing import sha256_bytes, sha256_file
from drift_agent.path_safety import (
    UnsafeInputPathError,
    repository_path_without_symlinks,
)


class StaleWorkspaceError(RuntimeError):
    pass


class UnsafePatchError(RuntimeError):
    pass


@dataclass(frozen=True)
class _FileTransactionRecord:
    original: bytes
    original_mode: int
    prior_hash: str
    prior_attempts: tuple[PatchAttempt, ...]
    planned_output: bytes
    planned_hash: str
    planned_attempts: tuple[PatchAttempt, ...]
    planned_spans: tuple[tuple[str, int, int], ...]
    external_changes: bool
    phase: Literal["prepared", "applied"]


def _hash_matches(path: Path, expected: str) -> bool:
    try:
        return sha256_file(path) == expected
    except OSError:
        return False


def _atomic_write(
    path: Path,
    content: bytes,
    expected_current_hash: str,
    mode: int,
    pre_replace: Callable[[], None],
) -> None:
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.drift-",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        pre_replace()
        if not _hash_matches(path, expected_current_hash):
            raise StaleWorkspaceError(f"file changed before atomic replace: {path}")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_anchor_range(attempt: PatchAttempt, original_size: int) -> None:
    if not 0 <= attempt.start_byte <= attempt.end_byte <= original_size:
        raise UnsafePatchError(f"patch anchor is outside the original document: {attempt.path}")


def _spans_overlap(left: PatchAttempt, right: PatchAttempt) -> bool:
    both_empty_at_same_offset = (
        left.start_byte == left.end_byte == right.start_byte == right.end_byte
    )
    return both_empty_at_same_offset or (
        left.start_byte < right.end_byte and right.start_byte < left.end_byte
    )


def _render_attempts(original: bytes, attempts: list[PatchAttempt]) -> bytes:
    chunks: list[bytes] = []
    cursor = 0
    for attempt in sorted(
        attempts,
        key=lambda candidate: (
            candidate.start_byte,
            candidate.end_byte,
            candidate.id,
        ),
    ):
        chunks.append(original[cursor : attempt.start_byte])
        chunks.append(attempt.replacement_text.encode("utf-8"))
        cursor = attempt.end_byte
    chunks.append(original[cursor:])
    return b"".join(chunks)


def _render_attempts_with_spans(
    original: bytes,
    attempts: list[PatchAttempt],
) -> tuple[bytes, tuple[tuple[str, int, int], ...]]:
    ordered = sorted(
        attempts,
        key=lambda candidate: (
            candidate.start_byte,
            candidate.end_byte,
            candidate.id,
        ),
    )
    output = _render_attempts(original, ordered)
    delta = 0
    spans: list[tuple[str, int, int]] = []
    for attempt in ordered:
        start = attempt.start_byte + delta
        replacement_size = len(attempt.replacement_text.encode("utf-8"))
        spans.append((attempt.id, start, start + replacement_size))
        delta += replacement_size - (attempt.end_byte - attempt.start_byte)
    return output, tuple(spans)


def _inverse_owned_attempts(
    current: bytes,
    planned_output: bytes,
    attempts: list[PatchAttempt],
    planned_spans: tuple[tuple[str, int, int], ...],
    remove_ids: set[str],
) -> tuple[bytes, tuple[tuple[str, int, int], ...]]:
    """Invert owned spans only when external hunks are provably disjoint.

    Mapping is positional from the exact Agent-planned output to current
    bytes.  Replacement text occurring elsewhere is never treated as proof of
    ownership.
    """

    spans = {attempt_id: (start, end) for attempt_id, start, end in planned_spans}
    attempts_by_id = {attempt.id: attempt for attempt in attempts}
    if set(attempts_by_id) != set(spans):
        raise StaleWorkspaceError("owned-span journal is incomplete")
    opcodes = SequenceMatcher(
        None,
        planned_output,
        current,
        autojunk=False,
    ).get_opcodes()
    owned = list(spans.values())
    for tag, planned_start, planned_end, _, _ in opcodes:
        if tag == "equal":
            continue
        for owned_start, owned_end in owned:
            if planned_start == planned_end:
                touches = owned_start <= planned_start <= owned_end
            else:
                touches = planned_start <= owned_end and planned_end >= owned_start
            if touches:
                path = next(iter(attempts_by_id.values())).path
                raise StaleWorkspaceError(f"external edit intersects Agent-owned span: {path}")

    mapped: dict[str, tuple[int, int]] = {}
    for attempt_id, (owned_start, owned_end) in spans.items():
        for tag, planned_start, planned_end, current_start, _ in opcodes:
            if tag == "equal" and planned_start <= owned_start and owned_end <= planned_end:
                mapped[attempt_id] = (
                    current_start + owned_start - planned_start,
                    current_start + owned_end - planned_start,
                )
                break
        if attempt_id not in mapped:
            raise StaleWorkspaceError(
                f"cannot map Agent-owned span: {attempts_by_id[attempt_id].path}"
            )
        start, end = mapped[attempt_id]
        replacement = attempts_by_id[attempt_id].replacement_text.encode("utf-8")
        if current[start:end] != replacement:
            raise StaleWorkspaceError(
                f"Agent-owned bytes changed externally: {attempts_by_id[attempt_id].path}"
            )

    updated = current
    remaining_mapped = {
        attempt_id: span for attempt_id, span in mapped.items() if attempt_id not in remove_ids
    }
    removed = sorted(
        (
            (*mapped[attempt_id], attempts_by_id[attempt_id])
            for attempt_id in remove_ids
            if attempt_id in mapped
        ),
        key=lambda item: (item[0], item[1], item[2].id),
        reverse=True,
    )
    for start, end, attempt in removed:
        expected = attempt.expected_text.encode("utf-8")
        updated = updated[:start] + expected + updated[end:]
        delta = len(expected) - (end - start)
        for remaining_id, (remaining_start, remaining_end) in list(remaining_mapped.items()):
            if remaining_start >= end:
                remaining_mapped[remaining_id] = (
                    remaining_start + delta,
                    remaining_end + delta,
                )
    remaining_spans = tuple(
        (attempt_id, *remaining_mapped[attempt_id]) for attempt_id in sorted(remaining_mapped)
    )
    return updated, remaining_spans


class WorkspaceTransaction:
    def __init__(self, repo_path: Path) -> None:
        self.repo_path = repo_path.resolve()
        self._records: dict[Path, _FileTransactionRecord] = {}

    def _resolve_target(self, attempted_path: str) -> Path:
        relative = Path(attempted_path)
        if (
            relative.suffix not in {".md", ".py"}
            or relative.is_absolute()
            or ".." in relative.parts
        ):
            raise UnsafePatchError(
                f"Stage 2 only permits repository-local Markdown/docstrings: {relative}"
            )
        try:
            return repository_path_without_symlinks(
                self.repo_path,
                relative.as_posix(),
            )
        except UnsafeInputPathError as error:
            raise UnsafePatchError(f"unsafe patch target: {relative}: {error}") from error

    def _ensure_target_safe(self, path: Path) -> None:
        relative = path.relative_to(self.repo_path).as_posix()
        try:
            repository_path_without_symlinks(self.repo_path, relative)
        except UnsafeInputPathError as error:
            raise UnsafePatchError(f"unsafe patch target: {relative}: {error}") from error

    def _record_applied(self, path: Path) -> None:
        self._records[path] = replace(self._records[path], phase="applied")

    def apply(self, attempt: PatchAttempt) -> None:
        path = self._resolve_target(attempt.path)
        if path.suffix == ".py" and attempt.target_kind != "docstring":
            raise UnsafePatchError(
                f"Python writes require an AST-proven docstring attempt: {attempt.path}"
            )
        if path.suffix == ".md" and attempt.target_kind != "markdown":
            raise UnsafePatchError(f"Markdown writes require a Markdown attempt: {attempt.path}")
        try:
            current = path.read_bytes()
            current_mode = stat.S_IMODE(path.stat().st_mode)
        except OSError as error:
            raise StaleWorkspaceError(f"patch target became unavailable: {attempt.path}") from error

        existing_record = self._records.get(path)
        if existing_record is None:
            original = current
            original_mode = current_mode
            existing_attempts: list[PatchAttempt] = []
            if sha256_bytes(original) != attempt.source_hash:
                raise StaleWorkspaceError(f"source hash changed: {attempt.path}")
            prior_hash = sha256_bytes(original)
            prior_attempts: tuple[PatchAttempt, ...] = ()
        else:
            original = existing_record.original
            original_mode = existing_record.original_mode
            existing_attempts = list(existing_record.planned_attempts)
            if sha256_bytes(original) != attempt.source_hash:
                raise StaleWorkspaceError(f"attempts disagree on base hash: {attempt.path}")
            if (
                existing_record.phase != "applied"
                or sha256_bytes(current) != existing_record.planned_hash
            ):
                raise StaleWorkspaceError(f"Agent-owned bytes changed externally: {attempt.path}")
            if existing_record.external_changes:
                raise StaleWorkspaceError(
                    f"cannot add patches after an external edit: {attempt.path}"
                )
            prior_hash = existing_record.planned_hash
            prior_attempts = existing_record.planned_attempts

        _validate_anchor_range(attempt, len(original))
        expected = attempt.expected_text.encode("utf-8")
        if original[attempt.start_byte : attempt.end_byte] != expected:
            raise StaleWorkspaceError(f"anchor text changed: {attempt.path}")
        if any(_spans_overlap(attempt, existing) for existing in existing_attempts):
            raise UnsafePatchError(f"patch anchors overlap: {attempt.path}")

        candidate_attempts = [*existing_attempts, attempt]
        updated, planned_spans = _render_attempts_with_spans(original, candidate_attempts)
        applied_hash = sha256_bytes(updated)
        self._records[path] = _FileTransactionRecord(
            original,
            original_mode,
            prior_hash,
            prior_attempts,
            updated,
            applied_hash,
            tuple(candidate_attempts),
            planned_spans,
            False,
            "prepared",
        )
        _atomic_write(
            path,
            updated,
            expected_current_hash=sha256_bytes(current),
            mode=original_mode,
            pre_replace=partial(self._ensure_target_safe, path),
        )
        self._record_applied(path)

    def _clear(self) -> None:
        self._records.clear()

    def preflight_commit(self) -> None:
        for path, record in self._records.items():
            self._ensure_target_safe(path)
            if record.phase != "applied" or not _hash_matches(
                path,
                record.planned_hash,
            ):
                raise StaleWorkspaceError(f"Agent-owned bytes changed externally: {path}")

    def finish_commit(self) -> None:
        self._clear()

    def original_bytes(self, relative_path: str) -> bytes | None:
        path = self.repo_path / relative_path
        record = self._records.get(path)
        return None if record is None else record.original

    def rollback_attempts(self, attempt_ids: set[str]) -> None:
        """Rollback selected owned edits while retaining independent edits.

        The fast path renders the remaining edits from the original snapshot.
        If another writer made a disjoint change, an inverse is attempted only
        when the exact Agent replacement can be located uniquely.
        """

        for path, record in list(self._records.items()):
            removed = [attempt for attempt in record.planned_attempts if attempt.id in attempt_ids]
            if not removed:
                continue
            remaining = [
                attempt for attempt in record.planned_attempts if attempt.id not in attempt_ids
            ]
            self._ensure_target_safe(path)
            try:
                current = path.read_bytes()
                mode = stat.S_IMODE(path.stat().st_mode)
            except OSError as error:
                raise StaleWorkspaceError(
                    f"cannot read Agent-owned file during group rollback: {path}"
                ) from error

            external_changes = sha256_bytes(current) != record.planned_hash
            updated, remaining_spans = _inverse_owned_attempts(
                current,
                record.planned_output,
                list(record.planned_attempts),
                record.planned_spans,
                {attempt.id for attempt in removed},
            )
            current_hash = sha256_bytes(current)
            updated_hash = sha256_bytes(updated)
            if updated_hash != current_hash:
                _atomic_write(
                    path,
                    updated,
                    expected_current_hash=current_hash,
                    mode=mode,
                    pre_replace=partial(self._ensure_target_safe, path),
                )
            if remaining:
                self._records[path] = _FileTransactionRecord(
                    original=record.original,
                    original_mode=record.original_mode,
                    prior_hash=sha256_bytes(record.original),
                    prior_attempts=(),
                    planned_output=updated,
                    planned_hash=updated_hash,
                    planned_attempts=tuple(remaining),
                    planned_spans=remaining_spans,
                    external_changes=record.external_changes or external_changes,
                    phase="applied",
                )
            else:
                del self._records[path]

    def commit(self) -> None:
        self.preflight_commit()
        self.finish_commit()

    def rollback(self) -> None:
        planned: list[tuple[Path, bytes, bytes, int]] = []
        for path, record in self._records.items():
            self._ensure_target_safe(path)
            try:
                current = path.read_bytes()
                mode = stat.S_IMODE(path.stat().st_mode)
            except OSError as error:
                raise StaleWorkspaceError(
                    f"cannot read Agent-owned file during rollback: {path}"
                ) from error
            current_hash = sha256_bytes(current)
            original_hash = sha256_bytes(record.original)
            if current_hash == original_hash:
                updated = current
            elif current_hash == record.planned_hash:
                updated, _ = _inverse_owned_attempts(
                    current,
                    record.planned_output,
                    list(record.planned_attempts),
                    record.planned_spans,
                    {attempt.id for attempt in record.planned_attempts},
                )
            elif current_hash == record.prior_hash:
                prior_output, prior_spans = _render_attempts_with_spans(
                    record.original,
                    list(record.prior_attempts),
                )
                updated, _ = _inverse_owned_attempts(
                    current,
                    prior_output,
                    list(record.prior_attempts),
                    prior_spans,
                    {attempt.id for attempt in record.prior_attempts},
                )
            else:
                active_attempts = (
                    list(record.planned_attempts)
                    if record.phase == "applied"
                    else list(record.prior_attempts)
                )
                if not active_attempts:
                    raise StaleWorkspaceError(
                        f"cannot prove Agent-owned bytes during rollback: {path}"
                    )
                if record.phase == "applied":
                    active_output = record.planned_output
                    active_spans = record.planned_spans
                else:
                    active_output, active_spans = _render_attempts_with_spans(
                        record.original,
                        active_attempts,
                    )
                updated, _ = _inverse_owned_attempts(
                    current,
                    active_output,
                    active_attempts,
                    active_spans,
                    {attempt.id for attempt in active_attempts},
                )
            planned.append((path, current, updated, mode))

        for path, current, updated, mode in planned:
            if updated == current:
                continue
            _atomic_write(
                path,
                updated,
                expected_current_hash=sha256_bytes(current),
                mode=mode,
                pre_replace=partial(self._ensure_target_safe, path),
            )
        self._clear()

    def residual_attempts(self) -> list[PatchAttempt]:
        residual: list[PatchAttempt] = []
        for path, record in self._records.items():
            try:
                self._ensure_target_safe(path)
            except UnsafePatchError:
                residual.extend(
                    record.planned_attempts if record.phase == "applied" else record.prior_attempts
                )
                continue
            try:
                current_hash = sha256_file(path)
            except OSError:
                residual.extend(
                    record.planned_attempts if record.phase == "applied" else record.prior_attempts
                )
                continue
            if current_hash == sha256_bytes(record.original):
                continue
            if current_hash == record.planned_hash:
                residual.extend(record.planned_attempts)
            elif current_hash == record.prior_hash:
                residual.extend(record.prior_attempts)
            else:
                residual.extend(
                    record.planned_attempts if record.phase == "applied" else record.prior_attempts
                )
        return sorted(
            residual,
            key=lambda attempt: (attempt.path, attempt.start_byte, attempt.id),
        )

import stat
from pathlib import Path

import pytest

from drift_agent.domain.models import PatchAttempt
from drift_agent.hashing import sha256_bytes, sha256_file
from drift_agent.workspace import transaction as transaction_module
from drift_agent.workspace.transaction import (
    StaleWorkspaceError,
    UnsafePatchError,
    WorkspaceTransaction,
)


def _attempt(path: Path) -> PatchAttempt:
    original = path.read_text(encoding="utf-8")
    return PatchAttempt(
        id="attempt_1",
        finding_ids=["finding_1"],
        path="docs/api.md",
        source_hash=sha256_file(path),
        start_byte=0,
        end_byte=len(original.encode()),
        expected_text=original,
        replacement_text="new\n",
        unified_diff="diff",
    )


def _span_attempt(
    path: Path,
    *,
    attempt_id: str,
    relative_path: str,
    start_byte: int,
    end_byte: int,
    expected_text: str,
    replacement_text: str,
) -> PatchAttempt:
    return PatchAttempt(
        id=attempt_id,
        finding_ids=[f"finding_{attempt_id}"],
        path=relative_path,
        source_hash=sha256_file(path),
        start_byte=start_byte,
        end_byte=end_byte,
        expected_text=expected_text,
        replacement_text=replacement_text,
        unified_diff="diff",
    )


def test_rollback_restores_agent_owned_bytes(tmp_path: Path) -> None:
    path = tmp_path / "docs/api.md"
    path.parent.mkdir()
    path.write_text("old\n", encoding="utf-8")
    transaction = WorkspaceTransaction(tmp_path)

    transaction.apply(_attempt(path))
    assert path.read_text(encoding="utf-8") == "new\n"
    transaction.rollback()

    assert path.read_text(encoding="utf-8") == "old\n"


def test_stale_hash_does_not_overwrite_newer_content(tmp_path: Path) -> None:
    path = tmp_path / "docs/api.md"
    path.parent.mkdir()
    path.write_text("old\n", encoding="utf-8")
    attempt = _attempt(path)
    path.write_text("external\n", encoding="utf-8")

    with pytest.raises(StaleWorkspaceError):
        WorkspaceTransaction(tmp_path).apply(attempt)

    assert path.read_text(encoding="utf-8") == "external\n"


def test_rollback_refuses_to_overwrite_external_edit(tmp_path: Path) -> None:
    path = tmp_path / "docs/api.md"
    path.parent.mkdir()
    path.write_text("old\n", encoding="utf-8")
    transaction = WorkspaceTransaction(tmp_path)
    transaction.apply(_attempt(path))
    path.write_text("external\n", encoding="utf-8")

    with pytest.raises(StaleWorkspaceError):
        transaction.rollback()

    assert path.read_text(encoding="utf-8") == "external\n"


def test_commit_refuses_externally_changed_agent_output(tmp_path: Path) -> None:
    path = tmp_path / "docs/api.md"
    path.parent.mkdir()
    path.write_text("old\n", encoding="utf-8")
    transaction = WorkspaceTransaction(tmp_path)
    transaction.apply(_attempt(path))
    path.write_text("external\n", encoding="utf-8")

    with pytest.raises(StaleWorkspaceError):
        transaction.commit()

    assert path.read_text(encoding="utf-8") == "external\n"


def test_failed_atomic_replace_leaves_original_document_intact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "docs/api.md"
    path.parent.mkdir()
    path.write_text("old\n", encoding="utf-8")
    transaction = WorkspaceTransaction(tmp_path)

    def fail_replace(source: str, destination: Path) -> None:
        raise OSError(f"forced replace failure: {source} -> {destination}")

    monkeypatch.setattr(transaction_module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="forced replace failure"):
        transaction.apply(_attempt(path))

    assert path.read_text(encoding="utf-8") == "old\n"
    transaction.rollback()


def test_interrupt_before_replace_does_not_invent_residual_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "docs/api.md"
    path.parent.mkdir()
    path.write_text("old\n", encoding="utf-8")
    transaction = WorkspaceTransaction(tmp_path)

    def interrupt_replace(source: str, destination: Path) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(transaction_module.os, "replace", interrupt_replace)

    with pytest.raises(KeyboardInterrupt):
        transaction.apply(_attempt(path))

    assert path.read_text(encoding="utf-8") == "old\n"
    assert transaction.residual_attempts() == []
    transaction.rollback()


def test_multiple_original_anchors_in_one_file_apply_atomically(tmp_path: Path) -> None:
    path = tmp_path / "docs/api.md"
    path.parent.mkdir()
    path.write_text("first\nsecond\n", encoding="utf-8")
    source_hash = sha256_file(path)
    lower = PatchAttempt(
        id="attempt_lower",
        finding_ids=["finding_lower"],
        path="docs/api.md",
        source_hash=source_hash,
        start_byte=0,
        end_byte=6,
        expected_text="first\n",
        replacement_text="FIRST\n",
        unified_diff="diff",
    )
    higher = PatchAttempt(
        id="attempt_higher",
        finding_ids=["finding_higher"],
        path="docs/api.md",
        source_hash=source_hash,
        start_byte=6,
        end_byte=13,
        expected_text="second\n",
        replacement_text="SECOND\n",
        unified_diff="diff",
    )
    transaction = WorkspaceTransaction(tmp_path)

    transaction.apply(higher)
    transaction.apply(lower)

    assert path.read_text(encoding="utf-8") == "FIRST\nSECOND\n"
    transaction.rollback()
    assert path.read_text(encoding="utf-8") == "first\nsecond\n"


def test_markdown_attempt_cannot_target_python_file(tmp_path: Path) -> None:
    path = tmp_path / "src/api.py"
    path.parent.mkdir()
    path.write_text("VALUE = 1\n", encoding="utf-8")
    attempt = _attempt(path).model_copy(update={"path": "src/api.py"})

    with pytest.raises(UnsafePatchError):
        WorkspaceTransaction(tmp_path).apply(attempt)


def test_apply_and_rollback_preserve_raw_crlf_bytes(tmp_path: Path) -> None:
    path = tmp_path / "docs/api.md"
    path.parent.mkdir()
    original = "标题\r\nold\r\n尾声\r\n".encode()
    path.write_bytes(original)
    expected = b"old\r\n"
    start = original.index(expected)
    attempt = _span_attempt(
        path,
        attempt_id="attempt_crlf",
        relative_path="docs/api.md",
        start_byte=start,
        end_byte=start + len(expected),
        expected_text="old\r\n",
        replacement_text="new\r\n",
    )
    transaction = WorkspaceTransaction(tmp_path)

    transaction.apply(attempt)

    assert path.read_bytes() == original.replace(expected, b"new\r\n")
    transaction.rollback()
    assert path.read_bytes() == original


def test_atomic_replacement_preserves_file_mode(tmp_path: Path) -> None:
    path = tmp_path / "docs/api.md"
    path.parent.mkdir()
    path.write_text("old\n", encoding="utf-8")
    path.chmod(0o640)
    transaction = WorkspaceTransaction(tmp_path)

    transaction.apply(_attempt(path))

    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    transaction.rollback()
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_anchor_mismatch_does_not_modify_document(tmp_path: Path) -> None:
    path = tmp_path / "docs/api.md"
    path.parent.mkdir()
    path.write_text("old\n", encoding="utf-8")
    attempt = _attempt(path).model_copy(update={"expected_text": "other\n"})

    with pytest.raises(StaleWorkspaceError):
        WorkspaceTransaction(tmp_path).apply(attempt)

    assert path.read_text(encoding="utf-8") == "old\n"


@pytest.mark.parametrize("start_byte,end_byte", [(-1, 0), (3, 2), (0, 99)])
def test_invalid_anchor_range_is_rejected(
    tmp_path: Path,
    start_byte: int,
    end_byte: int,
) -> None:
    path = tmp_path / "docs/api.md"
    path.parent.mkdir()
    path.write_text("old\n", encoding="utf-8")
    attempt = _attempt(path).model_copy(
        update={
            "start_byte": start_byte,
            "end_byte": end_byte,
            "expected_text": "" if start_byte == -1 else "old\n",
        }
    )

    with pytest.raises(UnsafePatchError):
        WorkspaceTransaction(tmp_path).apply(attempt)

    assert path.read_text(encoding="utf-8") == "old\n"


@pytest.mark.parametrize("attempt_path", ["../outside.md", "/outside.md"])
def test_repository_path_escape_is_rejected(tmp_path: Path, attempt_path: str) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("old\n", encoding="utf-8")
    path_value = str(outside) if attempt_path.startswith("/") else attempt_path
    attempt = _attempt(outside).model_copy(update={"path": path_value})

    with pytest.raises(UnsafePatchError):
        WorkspaceTransaction(repo).apply(attempt)

    assert outside.read_text(encoding="utf-8") == "old\n"


@pytest.mark.parametrize("symlink_kind", ["final", "component"])
def test_markdown_symlink_path_is_rejected(
    tmp_path: Path,
    symlink_kind: str,
) -> None:
    target = tmp_path / "notes/api.md"
    target.parent.mkdir()
    target.write_text("old\n", encoding="utf-8")
    if symlink_kind == "final":
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "api.md").symlink_to(target)
    else:
        (tmp_path / "docs").symlink_to(target.parent, target_is_directory=True)
    attempt = _attempt(target).model_copy(update={"path": "docs/api.md"})

    with pytest.raises(UnsafePatchError, match="symlink"):
        WorkspaceTransaction(tmp_path).apply(attempt)

    assert target.read_text(encoding="utf-8") == "old\n"


def test_commit_rejects_target_replaced_by_symlink_after_apply(
    tmp_path: Path,
) -> None:
    path = tmp_path / "docs/api.md"
    path.parent.mkdir()
    path.write_text("old\n", encoding="utf-8")
    transaction = WorkspaceTransaction(tmp_path)
    transaction.apply(_attempt(path))
    target = tmp_path / "notes/api.md"
    target.parent.mkdir()
    path.replace(target)
    path.symlink_to(target)
    target_before = target.read_bytes()

    with pytest.raises(UnsafePatchError, match="symlink"):
        transaction.commit()

    assert target.read_bytes() == target_before


def test_original_anchors_remain_valid_after_a_length_changing_lower_patch(
    tmp_path: Path,
) -> None:
    path = tmp_path / "docs/api.md"
    path.parent.mkdir()
    path.write_text("first\nsecond\n", encoding="utf-8")
    lower = _span_attempt(
        path,
        attempt_id="attempt_lower",
        relative_path="docs/api.md",
        start_byte=0,
        end_byte=6,
        expected_text="first\n",
        replacement_text="FIRST-LONGER\n",
    )
    higher = _span_attempt(
        path,
        attempt_id="attempt_higher",
        relative_path="docs/api.md",
        start_byte=6,
        end_byte=13,
        expected_text="second\n",
        replacement_text="SECOND\n",
    )
    transaction = WorkspaceTransaction(tmp_path)

    transaction.apply(lower)
    transaction.apply(higher)

    assert path.read_text(encoding="utf-8") == "FIRST-LONGER\nSECOND\n"
    transaction.rollback()
    assert path.read_text(encoding="utf-8") == "first\nsecond\n"


def test_overlapping_original_anchors_are_rejected_without_losing_prior_patch(
    tmp_path: Path,
) -> None:
    path = tmp_path / "docs/api.md"
    path.parent.mkdir()
    path.write_text("abcdef\n", encoding="utf-8")
    first = _span_attempt(
        path,
        attempt_id="attempt_first",
        relative_path="docs/api.md",
        start_byte=0,
        end_byte=4,
        expected_text="abcd",
        replacement_text="ABCD",
    )
    overlapping = _span_attempt(
        path,
        attempt_id="attempt_overlap",
        relative_path="docs/api.md",
        start_byte=2,
        end_byte=6,
        expected_text="cdef",
        replacement_text="CDEF",
    )
    transaction = WorkspaceTransaction(tmp_path)
    transaction.apply(first)

    with pytest.raises(UnsafePatchError):
        transaction.apply(overlapping)

    assert path.read_text(encoding="utf-8") == "ABCDef\n"
    transaction.rollback()
    assert path.read_text(encoding="utf-8") == "abcdef\n"


def test_second_apply_refuses_external_edit(tmp_path: Path) -> None:
    path = tmp_path / "docs/api.md"
    path.parent.mkdir()
    path.write_text("first\nsecond\n", encoding="utf-8")
    first = _span_attempt(
        path,
        attempt_id="attempt_first",
        relative_path="docs/api.md",
        start_byte=0,
        end_byte=6,
        expected_text="first\n",
        replacement_text="FIRST\n",
    )
    second = _span_attempt(
        path,
        attempt_id="attempt_second",
        relative_path="docs/api.md",
        start_byte=6,
        end_byte=13,
        expected_text="second\n",
        replacement_text="SECOND\n",
    )
    transaction = WorkspaceTransaction(tmp_path)
    transaction.apply(first)
    path.write_text("external\n", encoding="utf-8")

    with pytest.raises(StaleWorkspaceError):
        transaction.apply(second)

    assert path.read_text(encoding="utf-8") == "external\n"


def test_attempts_for_one_file_must_share_the_original_hash(tmp_path: Path) -> None:
    path = tmp_path / "docs/api.md"
    path.parent.mkdir()
    path.write_text("first\nsecond\n", encoding="utf-8")
    first = _span_attempt(
        path,
        attempt_id="attempt_first",
        relative_path="docs/api.md",
        start_byte=0,
        end_byte=6,
        expected_text="first\n",
        replacement_text="FIRST\n",
    )
    second = _span_attempt(
        path,
        attempt_id="attempt_second",
        relative_path="docs/api.md",
        start_byte=6,
        end_byte=13,
        expected_text="second\n",
        replacement_text="SECOND\n",
    ).model_copy(update={"source_hash": sha256_bytes(b"different")})
    transaction = WorkspaceTransaction(tmp_path)
    transaction.apply(first)

    with pytest.raises(StaleWorkspaceError):
        transaction.apply(second)

    assert path.read_text(encoding="utf-8") == "FIRST\nsecond\n"


def test_failed_second_replace_keeps_prior_patch_rollbackable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "docs/api.md"
    path.parent.mkdir()
    path.write_text("first\nsecond\n", encoding="utf-8")
    first = _span_attempt(
        path,
        attempt_id="attempt_first",
        relative_path="docs/api.md",
        start_byte=0,
        end_byte=6,
        expected_text="first\n",
        replacement_text="FIRST\n",
    )
    second = _span_attempt(
        path,
        attempt_id="attempt_second",
        relative_path="docs/api.md",
        start_byte=6,
        end_byte=13,
        expected_text="second\n",
        replacement_text="SECOND\n",
    )
    transaction = WorkspaceTransaction(tmp_path)
    transaction.apply(first)

    def fail_replace(source: str, destination: Path) -> None:
        raise OSError(f"forced replace failure: {source} -> {destination}")

    with monkeypatch.context() as patch:
        patch.setattr(transaction_module.os, "replace", fail_replace)
        with pytest.raises(OSError, match="forced replace failure"):
            transaction.apply(second)

    assert path.read_text(encoding="utf-8") == "FIRST\nsecond\n"
    transaction.rollback()
    assert path.read_text(encoding="utf-8") == "first\nsecond\n"


def test_rollback_preflights_all_files_before_restoring_any(tmp_path: Path) -> None:
    first_path = tmp_path / "docs/first.md"
    first_path.parent.mkdir()
    first_path.write_text("first\n", encoding="utf-8")
    second_path = tmp_path / "docs/second.md"
    second_path.write_text("second\n", encoding="utf-8")
    first = _attempt(first_path).model_copy(
        update={"path": "docs/first.md", "replacement_text": "FIRST\n"}
    )
    second = _attempt(second_path).model_copy(
        update={"path": "docs/second.md", "replacement_text": "SECOND\n"}
    )
    transaction = WorkspaceTransaction(tmp_path)
    transaction.apply(first)
    transaction.apply(second)
    second_path.write_text("external\n", encoding="utf-8")

    with pytest.raises(StaleWorkspaceError):
        transaction.rollback()

    assert first_path.read_text(encoding="utf-8") == "FIRST\n"
    assert second_path.read_text(encoding="utf-8") == "external\n"

import shutil
import subprocess
from pathlib import Path

import pytest

from drift_agent.config import ProjectConfig
from drift_agent.hashing import sha256_file
from drift_agent.path_safety import UnsafeInputPathError
from drift_agent.scope import git as git_scope
from drift_agent.scope.git import GitRevisionError, GitScopeResolver, SnapshotChangedError


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _git_output(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _init_repo(repo: Path) -> None:
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")


def _config() -> ProjectConfig:
    return ProjectConfig(
        source_roots=["src"],
        docs_roots=["docs"],
        include=["src/**/*.py", "docs/**/*.md"],
        exclude=[],
    )


def test_changed_paths_include_staged_unstaged_and_untracked(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "src/api.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "docs/api.md").write_text("# API\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")

    (tmp_path / "src/api.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(tmp_path, "add", "src/api.py")
    (tmp_path / "docs/api.md").write_text("# Changed API\n", encoding="utf-8")
    (tmp_path / "docs/new.md").write_text("# New\n", encoding="utf-8")

    resolver = GitScopeResolver(tmp_path, _config())

    assert resolver.changed_paths() == ["docs/api.md", "docs/new.md", "src/api.py"]


def test_since_finds_committed_change_while_default_scope_is_empty(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    source = tmp_path / "src/api.py"
    docs = tmp_path / "docs/api.md"
    source.parent.mkdir()
    docs.parent.mkdir()
    source.write_text("VALUE = 1\n", encoding="utf-8")
    docs.write_text("# API\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    base = _git_output(tmp_path, "rev-parse", "HEAD")
    source.write_text("VALUE = 2\n", encoding="utf-8")
    _git(tmp_path, "add", "src/api.py")
    _git(tmp_path, "commit", "-qm", "change api")
    head = _git_output(tmp_path, "rev-parse", "HEAD")

    default = GitScopeResolver(tmp_path, _config())
    since = GitScopeResolver(tmp_path, _config(), since=base)

    assert default.changed_paths() == []
    assert since.changed_paths() == ["src/api.py"]
    assert since.baseline_revision() == base
    assert since.resolved_revision() == base
    assert since.head_revision() == head
    assert [(change.status, change.old_path, change.new_path) for change in since.changes()] == [
        ("M", "src/api.py", "src/api.py")
    ]


def test_since_combines_committed_staged_unstaged_delete_rename_and_untracked(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    initial = {
        "src/committed.py": "COMMITTED = 1\n",
        "src/staged.py": "STAGED = 1\n",
        "src/unstaged.py": "UNSTAGED = 1\n",
        "src/deleted.py": "DELETED = 1\n",
        "src/renamed.py": "RENAMED = 1\n",
        "docs/api.md": "# API\n",
    }
    for relative, content in initial.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    base = _git_output(tmp_path, "rev-parse", "HEAD")

    (tmp_path / "src/committed.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(tmp_path, "add", "src/committed.py")
    _git(tmp_path, "commit", "-qm", "committed change")
    (tmp_path / "src/staged.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(tmp_path, "add", "src/staged.py")
    (tmp_path / "src/unstaged.py").write_text("VALUE = 2\n", encoding="utf-8")
    (tmp_path / "src/deleted.py").unlink()
    _git(tmp_path, "mv", "src/renamed.py", "src/moved.py")
    (tmp_path / "docs/new.md").write_text("# New\n", encoding="utf-8")

    resolver = GitScopeResolver(tmp_path, _config(), since=base)

    assert resolver.changed_paths() == [
        "docs/new.md",
        "src/committed.py",
        "src/deleted.py",
        "src/moved.py",
        "src/staged.py",
        "src/unstaged.py",
    ]
    assert [(change.status, change.old_path, change.new_path) for change in resolver.changes()] == [
        ("A", None, "docs/new.md"),
        ("M", "src/committed.py", "src/committed.py"),
        ("D", "src/deleted.py", None),
        ("R", "src/renamed.py", "src/moved.py"),
        ("M", "src/staged.py", "src/staged.py"),
        ("M", "src/unstaged.py", "src/unstaged.py"),
    ]
    assert resolver.head_bytes("src/deleted.py") == b"DELETED = 1\n"
    assert resolver.head_bytes("src/renamed.py") == b"RENAMED = 1\n"


def test_since_freezes_the_resolved_ref_when_the_name_moves(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    source = tmp_path / "src/api.py"
    source.parent.mkdir()
    source.write_text("VALUE = 1\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    base = _git_output(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "branch", "comparison-base", base)
    source.write_text("VALUE = 2\n", encoding="utf-8")
    _git(tmp_path, "add", "src/api.py")
    _git(tmp_path, "commit", "-qm", "change")

    resolver = GitScopeResolver(tmp_path, _config(), since="comparison-base")
    _git(tmp_path, "branch", "-f", "comparison-base", "HEAD")

    assert resolver.resolved_revision() == base
    assert resolver.baseline_revision() == base
    assert resolver.changed_paths() == ["src/api.py"]


def test_since_uses_merge_base_for_diverged_history(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    source = tmp_path / "src/api.py"
    docs = tmp_path / "docs/api.md"
    source.parent.mkdir()
    docs.parent.mkdir()
    source.write_text("VALUE = 1\n", encoding="utf-8")
    docs.write_text("# API\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    common_base = _git_output(tmp_path, "rev-parse", "HEAD")

    _git(tmp_path, "checkout", "-qb", "feature")
    source.write_text("VALUE = 2\n", encoding="utf-8")
    _git(tmp_path, "add", "src/api.py")
    _git(tmp_path, "commit", "-qm", "feature change")
    feature_head = _git_output(tmp_path, "rev-parse", "HEAD")

    _git(tmp_path, "checkout", "-qb", "comparison", common_base)
    (tmp_path / "README.md").write_text("comparison\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-qm", "comparison change")
    comparison_head = _git_output(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "checkout", "-q", "feature")

    resolver = GitScopeResolver(tmp_path, _config(), since=comparison_head)

    assert resolver.baseline_revision() == common_base
    assert resolver.head_revision() == feature_head
    assert resolver.changed_paths() == ["src/api.py"]


@pytest.mark.parametrize("revision", ["missing-revision", "--help"])
def test_since_rejects_unavailable_revision_without_option_injection(
    tmp_path: Path,
    revision: str,
) -> None:
    _init_repo(tmp_path)
    (tmp_path / "README.md").write_text("base\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")

    with pytest.raises(GitRevisionError, match="not an available commit"):
        GitScopeResolver(tmp_path, _config(), since=revision)


def test_since_rejects_blob_tree_and_revision_range(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "README.md").write_text("base\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    head = _git_output(tmp_path, "rev-parse", "HEAD")

    for revision in (
        f"{head}:README.md",
        f"{head}^{{tree}}",
        f"{head}..{head}",
    ):
        with pytest.raises(GitRevisionError) as captured:
            GitScopeResolver(tmp_path, _config(), since=revision)
        assert captured.value.reason_code == "scope.invalid_revision"


def test_since_rejects_histories_without_a_common_base(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "README.md").write_text("first root\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "first root")
    first_root = _git_output(tmp_path, "rev-parse", "HEAD")

    _git(tmp_path, "checkout", "-q", "--orphan", "unrelated")
    for child in tmp_path.iterdir():
        if child.name == ".git":
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    (tmp_path / "README.md").write_text("unrelated root\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "unrelated root")

    with pytest.raises(GitRevisionError, match="no merge base"):
        GitScopeResolver(tmp_path, _config(), since=first_root)


def test_since_rejects_multiple_best_merge_bases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    head = "a" * 40
    revision = "b" * 40

    def fake_git(_repo: Path, *args: str) -> bytes:
        if args == ("rev-parse", "HEAD"):
            return f"{head}\n".encode()
        if args[:2] == ("rev-parse", "--verify"):
            return f"{revision}\n".encode()
        if args[:2] == ("merge-base", "--all"):
            return f"{'c' * 40}\n{'d' * 40}\n".encode()
        raise AssertionError(args)

    monkeypatch.setattr(git_scope, "_run_git", fake_git)

    with pytest.raises(GitRevisionError) as captured:
        GitScopeResolver(tmp_path, _config(), since="comparison")

    assert captured.value.reason_code == "scope.no_merge_base"
    assert "exactly one safe merge base" in str(captured.value)


def test_frozen_baseline_does_not_make_snapshot_stale_but_later_head_does(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    source = tmp_path / "src/api.py"
    source.parent.mkdir()
    source.write_text("VALUE = 1\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    baseline = _git_output(tmp_path, "rev-parse", "HEAD")
    source.write_text("VALUE = 2\n", encoding="utf-8")
    _git(tmp_path, "add", "src/api.py")
    _git(tmp_path, "commit", "-qm", "observed head")
    observed_head = _git_output(tmp_path, "rev-parse", "HEAD")
    resolver = GitScopeResolver(
        tmp_path,
        _config(),
        baseline_revision=baseline,
        observed_head_revision=observed_head,
    )

    snapshot = resolver.snapshot(["src/api.py"])

    assert snapshot.head_revision == observed_head
    assert resolver.baseline_revision() == baseline
    assert resolver.head_is_current() is True

    (tmp_path / "README.md").write_text("new head\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-qm", "move head")

    with pytest.raises(SnapshotChangedError, match="HEAD changed") as captured:
        resolver.snapshot(["src/api.py"])

    assert captured.value.snapshot.head_revision == observed_head
    assert resolver.head_is_current() is False


def test_legacy_head_revision_freezes_both_baseline_and_observed_head(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    source = tmp_path / "src/api.py"
    source.parent.mkdir()
    source.write_text("VALUE = 1\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "baseline")
    frozen_head = _git_output(tmp_path, "rev-parse", "HEAD")
    source.write_text("VALUE = 2\n", encoding="utf-8")
    _git(tmp_path, "add", "src/api.py")
    _git(tmp_path, "commit", "-qm", "new live head")

    resolver = GitScopeResolver(
        tmp_path,
        _config(),
        head_revision=frozen_head,
    )

    assert resolver.baseline_revision() == frozen_head
    assert resolver.head_revision() == frozen_head
    assert resolver.head_is_current() is False
    with pytest.raises(SnapshotChangedError, match="HEAD changed"):
        resolver.snapshot(["src/api.py"])


def test_broad_include_keeps_dirty_paths_inside_typed_roots(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    paths = {
        "src/api.py": "VALUE = 1\n",
        "docs/api.md": "# API\n",
        "scripts/outside.py": "VALUE = 1\n",
        "notes/outside.md": "# Outside\n",
    }
    for relative, content in paths.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    for relative, content in paths.items():
        (tmp_path / relative).write_text(f"changed\n{content}", encoding="utf-8")
    config = ProjectConfig(
        source_roots=["src"],
        docs_roots=["docs"],
        include=["**/*.py", "**/*.md"],
        exclude=[],
    )

    resolver = GitScopeResolver(tmp_path, config)

    assert resolver.changed_paths() == ["docs/api.md", "src/api.py"]


def test_broad_include_ignores_deleted_python_outside_source_roots(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    outside = tmp_path / "scripts/outside.py"
    outside.parent.mkdir()
    outside.write_text("VALUE = 1\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    outside.unlink()
    config = ProjectConfig(
        source_roots=["src"],
        docs_roots=["docs"],
        include=["**/*.py", "**/*.md"],
        exclude=[],
    )

    resolver = GitScopeResolver(tmp_path, config)

    assert resolver.unsupported_python_changes() == []


def test_snapshot_changes_when_an_input_file_changes(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    path = tmp_path / "src.py"
    path.write_text("VALUE = 1\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    config = ProjectConfig(
        source_roots=["."],
        docs_roots=["."],
        include=["*.py"],
        exclude=[],
    )
    resolver = GitScopeResolver(tmp_path, config)

    first = resolver.snapshot(["src.py"])
    path.write_text("VALUE = 2\n", encoding="utf-8")
    second = resolver.snapshot(["src.py"])

    assert first.head_revision == second.head_revision
    assert first.workspace_fingerprint != second.workspace_fingerprint


def test_snapshot_rejects_expected_hash_that_no_longer_matches(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    path = tmp_path / "src.py"
    path.write_text("VALUE = 1\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    config = ProjectConfig(
        source_roots=["."],
        docs_roots=["."],
        include=["*.py"],
        exclude=[],
    )
    resolver = GitScopeResolver(tmp_path, config)
    parsed_hash = sha256_file(path)

    path.write_text("VALUE = 2\n", encoding="utf-8")
    current = resolver.snapshot(["src.py"])

    with pytest.raises(SnapshotChangedError) as captured:
        resolver.snapshot(["src.py"], expected_hashes={"src.py": parsed_hash})

    assert captured.value.snapshot == current


def test_snapshot_rejects_symlink_component_before_missing_file_skip(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    (tmp_path / "README").write_text("base\n", encoding="utf-8")
    _git(tmp_path, "add", "README")
    _git(tmp_path, "commit", "-qm", "base")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "sub").symlink_to(tmp_path / "missing", target_is_directory=True)
    resolver = GitScopeResolver(tmp_path, _config())

    with pytest.raises(UnsafeInputPathError, match="symlink"):
        resolver.snapshot(["docs/sub/api.md"])


def test_snapshot_canonicalizes_expected_hash_mapping_order(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    source = tmp_path / "src"
    source.mkdir()
    first_path = source / "a.py"
    second_path = source / "b.py"
    first_path.write_text("A = 1\n", encoding="utf-8")
    second_path.write_text("B = 1\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    first_hash = sha256_file(first_path)
    second_hash = sha256_file(second_path)
    resolver = GitScopeResolver(tmp_path, _config())

    first = resolver.snapshot(
        ["src/b.py", "src/a.py"],
        expected_hashes={"src/b.py": second_hash, "src/a.py": first_hash},
    )
    second = resolver.snapshot(
        ["src/a.py", "src/b.py"],
        expected_hashes={"src/a.py": first_hash, "src/b.py": second_hash},
    )

    expected_items = [("src/a.py", first_hash), ("src/b.py", second_hash)]
    assert list(first.input_file_hashes.items()) == expected_items
    assert list(second.input_file_hashes.items()) == expected_items
    assert first.workspace_fingerprint == second.workspace_fingerprint


def test_deleted_python_path_is_reported_as_unsupported(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    path = tmp_path / "src/api.py"
    path.parent.mkdir()
    path.write_text("VALUE = 1\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    path.unlink()

    resolver = GitScopeResolver(tmp_path, _config())

    assert resolver.unsupported_python_changes() == ["src/api.py"]


def test_renamed_python_path_is_reported_as_unsupported(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    path = tmp_path / "src/api.py"
    path.parent.mkdir()
    path.write_text("VALUE = 1\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    _git(tmp_path, "mv", "src/api.py", "src/renamed.py")

    resolver = GitScopeResolver(tmp_path, _config())

    assert resolver.unsupported_python_changes() == ["src/renamed.py"]


def test_python_renamed_outside_included_scope_is_reported_as_unsupported(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    path = tmp_path / "src/api.py"
    path.parent.mkdir()
    path.write_text("VALUE = 1\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    (tmp_path / "archive").mkdir()
    _git(tmp_path, "mv", "src/api.py", "archive/api.txt")

    resolver = GitScopeResolver(tmp_path, _config())

    assert resolver.unsupported_python_changes() == ["src/api.py"]

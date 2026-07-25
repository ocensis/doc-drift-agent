from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from drift_agent.workspace.identity import (
    StatePathError,
    resolve_identities,
    resolve_state_path,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _repository(path: Path) -> Path:
    path.mkdir()
    (path / "README.md").write_text("identity\n", encoding="utf-8")
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "identity@example.invalid")
    _git(path, "config", "user.name", "Identity")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "root")
    return path


def test_state_path_defaults_to_git_state_and_override_must_be_external(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path / "repo")
    identities = resolve_identities(repo)

    default = resolve_state_path(repo, identities=identities)
    external = resolve_state_path(
        repo,
        tmp_path / "external-state",
        identities=identities,
    )

    assert default == identities.repository.common_dir / "drift-agent/state-v1.sqlite3"
    assert external == tmp_path / "external-state/state-v1.sqlite3"
    assert not external.parent.exists()
    with pytest.raises(StatePathError, match="outside the version-controlled worktree"):
        resolve_state_path(repo, repo / "state", identities=identities)

    not_a_directory = tmp_path / "state-file"
    not_a_directory.write_text("not a directory", encoding="utf-8")
    with pytest.raises(StatePathError, match="must be a directory"):
        resolve_state_path(repo, not_a_directory, identities=identities)


def test_linked_worktrees_share_repository_memory_but_not_workspace_lock_identity(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path / "repo")
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "-q", "-b", "linked-branch", str(linked))

    main_identity = resolve_identities(repo)
    linked_identity = resolve_identities(linked)

    assert main_identity.repository == linked_identity.repository
    assert main_identity.workspace.digest != linked_identity.workspace.digest
    assert main_identity.workspace.worktree_root == repo.resolve()
    assert linked_identity.workspace.worktree_root == linked.resolve()
    assert resolve_state_path(repo) == resolve_state_path(linked)


def test_independent_clone_with_identical_content_has_distinct_identities(
    tmp_path: Path,
) -> None:
    repo = _repository(tmp_path / "repo")
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(repo), str(clone)],
        check=True,
        capture_output=True,
    )

    original = resolve_identities(repo)
    independent = resolve_identities(clone)

    assert original.repository.root_commit == independent.repository.root_commit
    assert original.repository.digest != independent.repository.digest
    assert original.workspace.digest != independent.workspace.digest
    assert resolve_state_path(repo) != resolve_state_path(clone)

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

STATE_DATABASE_NAME = "state-v1.sqlite3"


class IdentityResolutionError(RuntimeError):
    """Raised when a stable Git-backed identity cannot be established."""

    reason_code = "repository_identity"


class StatePathError(RuntimeError):
    """Raised when persistent state would cross the allowed path boundary."""

    reason_code = "state_store.path"


@dataclass(frozen=True, slots=True)
class RepositoryIdentity:
    """Versioned material identifying one Git common repository."""

    digest: str
    material: str
    common_dir: Path
    root_commit: str

    @property
    def repository_id(self) -> str:
        return self.digest


@dataclass(frozen=True, slots=True)
class WorkspaceIdentity:
    """Versioned material identifying one writable worktree root."""

    digest: str
    material: str
    repository_id: str
    worktree_root: Path

    @property
    def workspace_id(self) -> str:
        return self.digest


@dataclass(frozen=True, slots=True)
class IdentitySet:
    repository: RepositoryIdentity
    workspace: WorkspaceIdentity


def _run_git(repo_path: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=repo_path,
            check=True,
            capture_output=True,
            shell=False,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise IdentityResolutionError(
            f"could not establish Git identity for {repo_path}"
        ) from error
    return completed.stdout.strip()


def _real_git_path(raw_path: str, *, repo_path: Path) -> Path:
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = repo_path / candidate
    try:
        return candidate.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise IdentityResolutionError(f"Git identity path is unavailable: {candidate}") from error


def _digest(material: str) -> str:
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def resolve_repository_identity(repo_path: Path) -> RepositoryIdentity:
    """Resolve repository-v1 identity from real common-dir and the root commit."""

    try:
        resolved_repo = repo_path.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise IdentityResolutionError(f"repository is unavailable: {repo_path}") from error

    common_dir = _real_git_path(
        _run_git(
            resolved_repo,
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        ),
        repo_path=resolved_repo,
    )
    roots = tuple(
        line
        for line in _run_git(
            resolved_repo,
            "rev-list",
            "--max-parents=0",
            "HEAD",
        ).splitlines()
        if line
    )
    if len(roots) != 1:
        raise IdentityResolutionError(
            "repository identity requires exactly one reachable root commit"
        )
    root_commit = roots[0]
    material = f"repo-v1\0{common_dir}\0{root_commit}"
    return RepositoryIdentity(
        digest=_digest(material),
        material=material,
        common_dir=common_dir,
        root_commit=root_commit,
    )


def resolve_workspace_identity(
    repo_path: Path,
    repository: RepositoryIdentity | None = None,
) -> WorkspaceIdentity:
    """Resolve workspace-v1 identity for the real top-level worktree path."""

    repository_identity = repository or resolve_repository_identity(repo_path)
    try:
        resolved_repo = repo_path.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise IdentityResolutionError(f"repository is unavailable: {repo_path}") from error
    worktree_root = _real_git_path(
        _run_git(
            resolved_repo,
            "rev-parse",
            "--path-format=absolute",
            "--show-toplevel",
        ),
        repo_path=resolved_repo,
    )
    material = f"workspace-v1\0{repository_identity.digest}\0{worktree_root}"
    return WorkspaceIdentity(
        digest=_digest(material),
        material=material,
        repository_id=repository_identity.digest,
        worktree_root=worktree_root,
    )


def resolve_identities(repo_path: Path) -> IdentitySet:
    repository = resolve_repository_identity(repo_path)
    workspace = resolve_workspace_identity(repo_path, repository)
    return IdentitySet(repository=repository, workspace=workspace)


def _is_within(path: Path, parent: Path) -> bool:
    return path == parent or path.is_relative_to(parent)


def resolve_state_path(
    repo_path: Path,
    state_dir: Path | None = None,
    *,
    identities: IdentitySet | None = None,
) -> Path:
    """Resolve the v1 database path without creating it.

    The default is Git administrative state.  An explicit override is a
    directory and must resolve completely outside the writable worktree.
    """

    identity_set = identities or resolve_identities(repo_path)
    if state_dir is None:
        try:
            default_directory = (identity_set.repository.common_dir / "drift-agent").resolve(
                strict=False
            )
        except (OSError, RuntimeError) as error:
            raise StatePathError("default state directory is unavailable") from error
        if _is_within(default_directory, identity_set.workspace.worktree_root) and not _is_within(
            default_directory, identity_set.repository.common_dir
        ):
            raise StatePathError("default state directory escapes Git administrative state")
        return default_directory / STATE_DATABASE_NAME

    requested = state_dir.expanduser()
    try:
        resolved = requested.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise StatePathError(f"state directory is unavailable: {state_dir}") from error
    try:
        exists = resolved.exists()
    except OSError as error:
        raise StatePathError(f"state directory is unavailable: {state_dir}") from error
    if exists and not resolved.is_dir():
        raise StatePathError(f"state_dir must be a directory: {state_dir}")
    is_git_administrative_state = _is_within(
        resolved,
        identity_set.repository.common_dir,
    )
    if (
        _is_within(resolved, identity_set.workspace.worktree_root)
        and not is_git_administrative_state
    ):
        raise StatePathError("state_dir must resolve outside the version-controlled worktree")
    return resolved / STATE_DATABASE_NAME


def git_object_exists(repo_path: Path, object_id: str, object_type: str) -> bool:
    """Return whether an exact Git object exists with the requested type."""

    if (
        len(object_id) not in {40, 64}
        or any(character not in "0123456789abcdefABCDEF" for character in object_id)
        or object_type not in {"blob", "commit", "tag", "tree"}
    ):
        return False
    try:
        completed = subprocess.run(
            ["git", "cat-file", "-e", f"{object_id}^{{{object_type}}}"],
            cwd=repo_path,
            check=False,
            capture_output=True,
            shell=False,
        )
    except OSError:
        return False
    return completed.returncode == 0


def git_is_ancestor(repo_path: Path, ancestor: str, descendant: str = "HEAD") -> bool:
    """Return whether Git proves the confirmation commit remains in history."""

    if len(ancestor) not in {40, 64} or any(
        character not in "0123456789abcdefABCDEF" for character in ancestor
    ):
        return False
    if descendant != "HEAD" and (
        len(descendant) not in {40, 64}
        or any(character not in "0123456789abcdefABCDEF" for character in descendant)
    ):
        return False
    try:
        completed = subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor, descendant],
            cwd=repo_path,
            check=False,
            capture_output=True,
            shell=False,
        )
    except OSError:
        return False
    return completed.returncode == 0

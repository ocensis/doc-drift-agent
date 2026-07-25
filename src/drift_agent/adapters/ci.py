from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from drift_agent.adapters.contracts import PublicBundleV3
from drift_agent.adapters.rendering import bundle_to_sarif, render_markdown_summary
from drift_agent.domain.models import VerifiedRepairBundle
from drift_agent.domain.serialization import bundle_to_json
from drift_agent.workspace.identity import resolve_identities, resolve_state_path


class ArtifactDirectoryError(ValueError):
    """Raised when CI output would enter repository or Git administrative state."""


@dataclass(frozen=True, slots=True)
class CIArtifactPaths:
    directory: Path
    state_directory: Path
    bundle: Path
    sarif: Path
    summary: Path
    pr_comment: Path


def _external_directory(
    requested: Path,
    *,
    label: str,
    forbidden_roots: tuple[Path, ...],
) -> Path:
    expanded = requested.expanduser()
    if ".." in expanded.parts:
        raise ArtifactDirectoryError(f"{label} may not contain parent traversal")
    lexical = expanded if expanded.is_absolute() else Path.cwd() / expanded
    current = Path(lexical.anchor)
    for part in lexical.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ArtifactDirectoryError(f"{label} may not contain a symlink component")
    directory = expanded.resolve(strict=False)
    if any(directory == root or directory.is_relative_to(root) for root in forbidden_roots):
        raise ArtifactDirectoryError(f"{label} must resolve outside the worktree and Git state")
    if directory.exists() and not directory.is_dir():
        raise ArtifactDirectoryError(f"{label} must be a directory")
    return directory


def prepare_artifact_directory(
    repo_path: Path,
    artifact_dir: Path,
    *,
    state_dir: Path | None = None,
) -> CIArtifactPaths:
    identities = resolve_identities(repo_path.resolve(strict=True))
    forbidden_roots = (
        identities.workspace.worktree_root,
        identities.repository.common_dir,
    )
    directory = _external_directory(
        artifact_dir,
        label="CI artifact directory",
        forbidden_roots=forbidden_roots,
    )
    state_directory = _external_directory(
        state_dir or directory.with_name(f".{directory.name}-state"),
        label="CI state directory",
        forbidden_roots=forbidden_roots,
    )
    if (
        state_directory == directory
        or state_directory.is_relative_to(directory)
        or directory.is_relative_to(state_directory)
    ):
        raise ArtifactDirectoryError("CI state and artifact directories must be separate")
    resolve_state_path(repo_path, state_directory, identities=identities)
    directory.mkdir(parents=True, exist_ok=True)
    return CIArtifactPaths(
        directory=directory,
        state_directory=state_directory,
        bundle=directory / "bundle.json",
        sarif=directory / "results.sarif",
        summary=directory / "summary.md",
        pr_comment=directory / "pr-comment.md",
    )


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def write_ci_artifacts(
    bundle: VerifiedRepairBundle,
    paths: CIArtifactPaths,
    *,
    revision: str | None = None,
) -> PublicBundleV3:
    public = PublicBundleV3.from_bundle(bundle)
    summary = render_markdown_summary(public, revision=revision)
    sarif = json.dumps(
        bundle_to_sarif(public),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    payloads = (
        (paths.bundle, f"{bundle_to_json(bundle, 3)}\n".encode()),
        (paths.sarif, f"{sarif}\n".encode()),
        (paths.summary, summary.encode("utf-8")),
        (paths.pr_comment, summary.encode("utf-8")),
    )
    try:
        for path, payload in payloads:
            _atomic_write(path, payload)
    except OSError:
        # A consumer must never mistake a partially published artifact set for a
        # complete run. Remove every well-known output, including files from a
        # previous run, and preserve the original publication error.
        for path, _payload in payloads:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    return public


__all__ = [
    "ArtifactDirectoryError",
    "CIArtifactPaths",
    "prepare_artifact_directory",
    "write_ci_artifacts",
]

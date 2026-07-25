from __future__ import annotations

import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from drift_agent.cli import app

runner = CliRunner()

_ARTIFACT_NAMES = {
    "bundle.json",
    "results.sarif",
    "summary.md",
    "pr-comment.md",
}


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


def _worktree_bytes(repo: Path) -> dict[str, bytes]:
    return {
        path.relative_to(repo).as_posix(): path.read_bytes()
        for path in sorted(repo.rglob("*"))
        if path.is_file() and ".git" not in path.relative_to(repo).parts
    }


def _external_directories(repo: Path, suffix: str) -> tuple[Path, Path]:
    return (
        repo.parent / f"{repo.name}-{suffix}-state",
        repo.parent / f"{repo.name}-{suffix}-artifacts",
    )


def _assert_four_valid_artifacts(artifacts_dir: Path) -> tuple[dict, dict]:
    assert {path.name for path in artifacts_dir.iterdir()} == _ARTIFACT_NAMES
    for name in _ARTIFACT_NAMES:
        assert (artifacts_dir / name).is_file()
    bundle = json.loads((artifacts_dir / "bundle.json").read_text(encoding="utf-8"))
    sarif = json.loads((artifacts_dir / "results.sarif").read_text(encoding="utf-8"))
    assert bundle["schema_version"] == 3
    assert sarif["version"] == "2.1.0"
    assert sarif["$schema"].endswith("sarif-2.1.0.json")
    assert (artifacts_dir / "summary.md").read_bytes() == (
        artifacts_dir / "pr-comment.md"
    ).read_bytes()
    return bundle, sarif


def test_ci_check_committed_drift_writes_external_state_and_four_artifacts(
    drift_repo: Path,
) -> None:
    base = _git_output(drift_repo, "rev-parse", "HEAD")
    _git(drift_repo, "add", "src/click_demo/api.py")
    _git(drift_repo, "commit", "-qm", "commit signature drift")
    state_dir, artifacts_dir = _external_directories(drift_repo, "drift")
    before_bytes = _worktree_bytes(drift_repo)
    before_status = _git_output(
        drift_repo,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )

    result = runner.invoke(
        app,
        [
            "ci",
            "check",
            "--repo",
            str(drift_repo),
            "--since",
            base,
            "--state-dir",
            str(state_dir),
            "--artifacts-dir",
            str(artifacts_dir),
        ],
    )

    assert result.exit_code == 1
    assert "status: drift_found" in result.stdout
    bundle, sarif = _assert_four_valid_artifacts(artifacts_dir)
    assert bundle["status"] == "drift_found"
    assert bundle["scope"] == ["src/click_demo/api.py"]
    assert bundle["findings"][0]["kind"] == "parameter_added"
    assert bundle["changes"]["applied"] is False
    assert base in (artifacts_dir / "summary.md").read_text(encoding="utf-8")
    assert (
        sarif["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["artifactLocation"][
            "uri"
        ]
        == "docs/api.md"
    )
    assert state_dir.is_dir()
    assert any(path.is_file() for path in state_dir.rglob("*"))
    assert _worktree_bytes(drift_repo) == before_bytes
    assert (
        _git_output(
            drift_repo,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        )
        == before_status
        == ""
    )


def test_ci_check_invalid_since_still_writes_failed_bundle_and_returns_two(
    drift_repo: Path,
) -> None:
    state_dir, artifacts_dir = _external_directories(drift_repo, "invalid")
    before_bytes = _worktree_bytes(drift_repo)
    before_status = _git_output(
        drift_repo,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )

    result = runner.invoke(
        app,
        [
            "ci",
            "check",
            "--repo",
            str(drift_repo),
            "--since",
            "refs/heads/definitely-missing",
            "--state-dir",
            str(state_dir),
            "--artifacts-dir",
            str(artifacts_dir),
        ],
    )

    assert result.exit_code == 2
    assert "status: failed" in result.stdout
    bundle, sarif = _assert_four_valid_artifacts(artifacts_dir)
    assert bundle["status"] == "failed"
    assert bundle["findings"] == []
    invocation = sarif["runs"][0]["invocations"][0]
    assert invocation["executionSuccessful"] is False
    assert invocation["exitCode"] == 2
    assert _worktree_bytes(drift_repo) == before_bytes
    assert (
        _git_output(
            drift_repo,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        )
        == before_status
    )

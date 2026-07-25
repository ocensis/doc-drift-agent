from __future__ import annotations

import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from drift_agent.application import run
from drift_agent.cli import app
from drift_agent.domain.enums import RunMode, RunStatus
from drift_agent.domain.models import RunRequest, ScopeSpec

runner = CliRunner()


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


def test_application_and_cli_detect_committed_drift_only_with_since(
    drift_repo: Path,
) -> None:
    base = _git_output(drift_repo, "rev-parse", "HEAD")
    _git(drift_repo, "add", "src/click_demo/api.py")
    _git(drift_repo, "commit", "-qm", "commit signature change")
    head = _git_output(drift_repo, "rev-parse", "HEAD")
    before_bytes = _worktree_bytes(drift_repo)
    before_status = _git_output(
        drift_repo,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )

    default_bundle = run(RunRequest(mode=RunMode.CHECK, repo_path=drift_repo))
    since_bundle = run(
        RunRequest(
            mode=RunMode.CHECK,
            repo_path=drift_repo,
            scope=ScopeSpec(kind="since", revision=base),
        )
    )

    assert default_bundle.status is RunStatus.CLEAN
    assert default_bundle.scope == []
    assert since_bundle.status is RunStatus.DRIFT_FOUND
    assert since_bundle.scope == ["src/click_demo/api.py"]
    assert len(since_bundle.findings) == 1
    assert since_bundle.findings[0].kind == "parameter_added"
    assert since_bundle.snapshot.head_revision == head
    assert since_bundle.changes.applied is False
    assert since_bundle.usage.model_calls == 0

    default_cli = runner.invoke(
        app,
        ["check", "--repo", str(drift_repo), "--format", "json"],
    )
    since_cli = runner.invoke(
        app,
        [
            "check",
            "--repo",
            str(drift_repo),
            "--since",
            base,
            "--format",
            "json",
        ],
    )

    assert default_cli.exit_code == 0
    assert json.loads(default_cli.stdout)["status"] == "clean"
    assert since_cli.exit_code == 1
    since_payload = json.loads(since_cli.stdout)
    assert since_payload["status"] == "drift_found"
    assert since_payload["scope"] == ["src/click_demo/api.py"]
    assert since_payload["snapshot"]["head_revision"] == head
    assert since_payload["changes"]["applied"] is False
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


def test_cli_repair_since_fixes_committed_drift_and_rechecks_the_same_range(
    drift_repo: Path,
) -> None:
    base = _git_output(drift_repo, "rev-parse", "HEAD")
    source = drift_repo / "src/click_demo/api.py"
    source_bytes = source.read_bytes()
    _git(drift_repo, "add", "src/click_demo/api.py")
    _git(drift_repo, "commit", "-qm", "commit signature change")

    repaired = runner.invoke(
        app,
        [
            "repair",
            "--repo",
            str(drift_repo),
            "--since",
            base,
            "--format",
            "json",
        ],
    )

    assert repaired.exit_code == 0
    repaired_payload = json.loads(repaired.stdout)
    assert repaired_payload["status"] == "fixed"
    assert repaired_payload["changes"]["files"] == ["docs/api.md"]
    assert source.read_bytes() == source_bytes
    assert "color: bool = True" in (drift_repo / "docs/api.md").read_text(encoding="utf-8")

    checked = runner.invoke(
        app,
        [
            "check",
            "--repo",
            str(drift_repo),
            "--since",
            base,
            "--format",
            "json",
        ],
    )

    assert checked.exit_code == 0
    assert json.loads(checked.stdout)["status"] == "clean"

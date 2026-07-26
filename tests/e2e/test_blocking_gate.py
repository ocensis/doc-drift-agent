"""The blocking path, end to end, through the surface CI actually reads.

`fail-on-drift` is the only input that can turn a merge red, and everything it
acts on is produced here: the `blocking:` line the composite action parses, the
SARIF level a code-scanning annotation renders, and the count in the job
summary. Unit tests cover `is_advisory_reason` in isolation; what they cannot
show is that a real contradiction survives detection, aggregation, rendering
and the CLI boundary still labelled as one.

The two cases are deliberately a pair. A gate that called everything blocking
would pass the first test and fail the second; a gate that called nothing
blocking -- which is what shipping the advisory change wrong would look like --
would fail the first and pass the second.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from drift_agent.cli import app


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _ci_check(repo: Path, workspace: Path) -> tuple[int, str, dict[str, object]]:
    # Both directories must sit outside the worktree; the adapter fails closed
    # otherwise, which is why `workspace` is never derived from `repo`.
    artifacts = workspace / "artifacts"
    result = CliRunner().invoke(
        app,
        [
            "ci",
            "check",
            "--repo",
            str(repo),
            "--since",
            "HEAD",
            "--state-dir",
            str(workspace / "state"),
            "--artifacts-dir",
            str(artifacts),
        ],
    )
    assert (artifacts / "results.sarif").is_file(), (
        f"the gate published no artifacts (exit {result.exit_code}):\n{result.output}"
    )
    sarif = json.loads((artifacts / "results.sarif").read_text(encoding="utf-8"))
    bundle = json.loads((artifacts / "bundle.json").read_text(encoding="utf-8"))
    summary = (artifacts / "summary.md").read_text(encoding="utf-8")
    return result.exit_code, result.output, {"sarif": sarif, "bundle": bundle, "summary": summary}


def _levels(sarif: dict[str, object]) -> list[str]:
    runs = sarif["runs"]
    assert isinstance(runs, list)
    return [result["level"] for result in runs[0]["results"]]


def test_a_contradiction_blocks_and_is_never_demoted_to_a_note(
    drift_repo: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    # The fixture adds `color` to the signature and leaves the documented
    # signature alone: the documentation now contradicts the code. Its repo IS
    # `tmp_path`, so the workspace has to come from somewhere else entirely.
    exit_code, output, artifacts = _ci_check(drift_repo, tmp_path_factory.mktemp("ws"))

    assert exit_code == 1
    assert "status: drift_found" in output
    # The composite action greps exactly this line and refuses to guess when it
    # cannot parse it, so its format is part of the contract.
    assert "blocking: 1" in output

    bundle = artifacts["bundle"]
    assert isinstance(bundle, dict)
    findings = bundle["findings"]
    assert isinstance(findings, list)
    reason_codes = {finding["reason_code"] for finding in findings}
    assert not any(
        token in code for code in reason_codes for token in ("unsupported", "ambiguit")
    ), reason_codes

    # `warning`, not `error`, and that is the point: this fixture classifies its
    # docs as code-derived, so the truth source is known and the finding is
    # repairable (`detected`). An unresolved one renders `error`. What the
    # blocking/advisory split guarantees is the floor -- a finding that asserts a
    # defect is never demoted to `note`, whatever its disposition.
    dispositions = {finding["disposition"] for finding in findings}
    assert dispositions == {"detected"}
    sarif = artifacts["sarif"]
    assert isinstance(sarif, dict)
    assert _levels(sarif) == ["warning"]
    assert "note" not in _levels(sarif)
    assert "Blocking findings: 1 (0 advisory)" in str(artifacts["summary"])


def test_an_unverifiable_claim_reports_without_blocking(tmp_path: Path) -> None:
    repo = tmp_path / "advisory-repo"
    package = repo / "src/demo"
    package.mkdir(parents=True)
    (repo / "docs").mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    api = package / "api.py"
    # A complete Google docstring: every parameter has an Args entry, so the
    # detector has an anchor and finds nothing to say.
    api.write_text(
        '''def greet(name: str) -> str:
    """Return a greeting.

    Args:
        name (str): Who to greet.

    Returns:
        str: The greeting.
    """

    return f"Hello, {name}!"
''',
        encoding="utf-8",
    )
    (repo / "drift-agent.toml").write_text(
        """\
[project]
source_roots = ["src"]
docs_roots = ["docs"]
include = ["src/**/*.py", "docs/**/*.md"]
exclude = []
""",
        encoding="utf-8",
    )
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    # Adding a keyword-only parameter without an Args entry leaves the docstring
    # without an anchor for it. That is the detector's limit, not a defect in
    # this repository -- no edit to the prose makes the claim checkable.
    api.write_text(
        api.read_text(encoding="utf-8")
        .replace(
            "def greet(name: str) -> str:", "def greet(name: str, *, loud: bool = False) -> str:"
        )
        .replace('return f"Hello, {name}!"', 'return f"Hello, {name}!".upper() if loud else name'),
        encoding="utf-8",
    )

    exit_code, output, artifacts = _ci_check(repo, tmp_path / "ws-advisory")

    # Still reported, still exit 1: advisory means "do not block", never "do not
    # tell". `fail-on-drift` reads the count, not the exit code.
    assert exit_code == 1
    assert "status: drift_found" in output
    assert "blocking: 0" in output

    bundle = artifacts["bundle"]
    assert isinstance(bundle, dict)
    findings = bundle["findings"]
    assert isinstance(findings, list)
    assert findings, "the advisory finding must still be published"
    assert all("unsupported" in finding["reason_code"] for finding in findings)

    sarif = artifacts["sarif"]
    assert isinstance(sarif, dict)
    assert set(_levels(sarif)) == {"note"}
    assert "Blocking findings: 0" in str(artifacts["summary"])

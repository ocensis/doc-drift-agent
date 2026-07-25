import json
import shutil
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from drift_agent.cli import app

runner = CliRunner()


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_check_repair_and_recheck_click_signature(tmp_path: Path) -> None:
    fixture = Path("tests/fixtures/click_signature_drift")
    shutil.copytree(fixture / "before", tmp_path, dirs_exist_ok=True)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    source = tmp_path / "src/click_demo/api.py"
    shutil.copyfile(fixture / "after/api.py", source)
    source_after_change = source.read_bytes()
    docs = tmp_path / "docs/api.md"
    docs_before = docs.read_bytes()

    checked = runner.invoke(
        app,
        ["check", "--repo", str(tmp_path), "--format", "json"],
    )
    assert checked.exit_code == 1
    assert json.loads(checked.stdout)["status"] == "drift_found"
    assert docs.read_bytes() == docs_before

    repaired = runner.invoke(
        app,
        ["repair", "--repo", str(tmp_path), "--format", "json"],
    )
    assert repaired.exit_code == 0
    payload = json.loads(repaired.stdout)
    assert payload["status"] == "fixed"
    assert payload["usage"]["model_calls"] == 0
    assert payload["validation"][0]["status"] == "passed"
    assert "color: bool = True" in docs.read_text(encoding="utf-8")
    assert source.read_bytes() == source_after_change

    rechecked = runner.invoke(
        app,
        ["check", "--repo", str(tmp_path), "--format", "json"],
    )
    assert rechecked.exit_code == 0
    assert json.loads(rechecked.stdout)["status"] == "clean"

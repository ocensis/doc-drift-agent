from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from drift_agent import application
from drift_agent.domain.enums import RunMode, RunStatus
from drift_agent.domain.models import RunRequest, ScopeSpec

_CONFIG = """\
[project]
source_roots = ["src"]
docs_roots = ["docs"]
include = ["src/**/*.ts", "docs/**/*.md"]
exclude = ["**/node_modules/**"]

[truth]
code_derived = ["docs/**"]
design = []
contract = []

[validation]
commands = []
network = false
"""

_SOURCE = """\
/** Canonical inbound message shape. */
export interface UnifiedMessage {
  id: string;
  channel: string;
}

/** Handle one inbound message. */
export async function handleMessage(msg: UnifiedMessage, retry?: boolean): Promise<void> {
  void msg;
  void retry;
}
"""

_DOC = """\
# Messaging

The adapter converts every event into a `UnifiedMessage` and dispatches it
through `handleMessage`. Failures are marked `FAILED` and retried via
`POST` callbacks.
"""


def _git(repo: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def ts_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "ts-repo"
    (repo / "src").mkdir(parents=True)
    (repo / "docs").mkdir()
    (repo / "src" / "messaging.ts").write_text(_SOURCE, encoding="utf-8")
    (repo / "docs" / "api.md").write_text(_DOC, encoding="utf-8")
    (repo / "drift-agent.toml").write_text(_CONFIG, encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    return repo


def _check(repo: Path) -> object:
    return application.run(
        RunRequest(mode=RunMode.CHECK, repo_path=repo, scope=ScopeSpec(kind="changed"))
    )


def test_unchanged_typescript_repo_is_clean(ts_repo: Path) -> None:
    bundle = _check(ts_repo)

    assert bundle.status is RunStatus.CLEAN
    assert bundle.findings == []


def test_renamed_exported_function_referenced_by_docs_is_reported(ts_repo: Path) -> None:
    source = ts_repo / "src" / "messaging.ts"
    source.write_text(_SOURCE.replace("handleMessage", "processMessage"), encoding="utf-8")

    bundle = _check(ts_repo)

    assert bundle.status is not RunStatus.CLEAN
    assert bundle.findings, "doc reference to the renamed export must produce a finding"
    symbols = {finding.symbol_id for finding in bundle.findings}
    assert any("handleMessage" in symbol or "processMessage" in symbol for symbol in symbols)
    # Prose backticks (FAILED, POST) must not surface as findings.
    assert all("FAILED" not in finding.symbol_id for finding in bundle.findings)
    assert all("POST" not in finding.symbol_id for finding in bundle.findings)


def test_deleted_exported_interface_referenced_by_docs_is_reported(ts_repo: Path) -> None:
    source = ts_repo / "src" / "messaging.ts"
    stripped = _SOURCE.split("/** Handle one inbound message. */")[1]
    source.write_text("/** Handle one inbound message. */" + stripped, encoding="utf-8")

    bundle = _check(ts_repo)

    assert bundle.status is not RunStatus.CLEAN
    assert any("UnifiedMessage" in finding.symbol_id for finding in bundle.findings)


def test_touched_typescript_file_without_doc_impact_stays_clean(ts_repo: Path) -> None:
    source = ts_repo / "src" / "messaging.ts"
    source.write_text(_SOURCE + "\nexport const RETRY_LIMIT: number = 3;\n", encoding="utf-8")

    bundle = _check(ts_repo)

    assert bundle.status is RunStatus.CLEAN
    assert bundle.findings == []


def test_repair_mode_on_typescript_repo_does_not_crash_or_edit(ts_repo: Path) -> None:
    source = ts_repo / "src" / "messaging.ts"
    source.write_text(_SOURCE.replace("handleMessage", "processMessage"), encoding="utf-8")

    bundle = application.run(
        RunRequest(mode=RunMode.REPAIR, repo_path=ts_repo, scope=ScopeSpec(kind="changed"))
    )

    assert bundle.changes.files == []
    doc = (ts_repo / "docs" / "api.md").read_text(encoding="utf-8")
    assert doc == _DOC


def test_prose_backtick_in_doc_edit_does_not_trigger_python_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression: single-segment claims must not widen the Python scan gate.
    repo = tmp_path / "py-repo"
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / "docs").mkdir()
    (repo / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "src" / "pkg" / "api.py").write_text(
        "def run(value: str) -> None: ...\n", encoding="utf-8"
    )
    (repo / "docs" / "notes.md").write_text("Notes about `Options` here.\n", encoding="utf-8")
    (repo / "drift-agent.toml").write_text(
        _CONFIG.replace(
            'include = ["src/**/*.ts", "docs/**/*.md"]', 'include = ["src/**/*.py", "docs/**/*.md"]'
        ),
        encoding="utf-8",
    )
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    (repo / "docs" / "notes.md").write_text(
        "Notes about `Options` and `TODO` here.\n", encoding="utf-8"
    )

    calls: list[str] = []
    original = application._python_sources

    def counting(*args: object, **kwargs: object) -> list[str]:
        calls.append("scan")
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(application, "_python_sources", counting)

    bundle = _check(repo)

    assert bundle.status is RunStatus.CLEAN
    assert calls == [], "prose backticks alone must not trigger a full Python scan"


def test_accessor_pair_and_abstract_class_still_align_uniquely(ts_repo: Path) -> None:
    source = ts_repo / "src" / "messaging.ts"
    source.write_text(
        _SOURCE
        + "\nexport abstract class Dispatcher {"
        + "\n  abstract dispatch(msg: UnifiedMessage): void;"
        + '\n  get queueName(): string { return "q"; }'
        + "\n  set queueName(value: string) {}"
        + "\n}\n",
        encoding="utf-8",
    )
    (ts_repo / "docs" / "api.md").write_text(
        _DOC + "\nDispatch goes through `Dispatcher`.\n", encoding="utf-8"
    )

    bundle = _check(ts_repo)

    assert bundle.status is RunStatus.CLEAN
    assert bundle.findings == []

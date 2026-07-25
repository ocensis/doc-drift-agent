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
include = ["src/**/*.ts", "docs/**/*.md", "README.md"]
exclude = []

[truth]
code_derived = ["docs/**"]
design = []
contract = []

[validation]
commands = []
network = false
"""

_ENV = """\
MODEL_TIMEOUT_MS=10000
MAX_RETRIES=2
REVIEW_THRESHOLD=500
"""

_ENUMERATING_DOC = """\
# 配置

| 变量 | 说明 |
|------|------|
| `MODEL_TIMEOUT_MS` | 模型超时 |
| `MAX_RETRIES` | 重试次数 |
| `REVIEW_THRESHOLD` | 人工审核阈值 |
"""


def _git(repo: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def config_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "cfg-repo"
    (repo / "src").mkdir(parents=True)
    (repo / "docs").mkdir()
    (repo / "src" / "main.ts").write_text("export const VERSION: string = '1';\n", encoding="utf-8")
    (repo / "docs" / "config.md").write_text(_ENUMERATING_DOC, encoding="utf-8")
    (repo / ".env.example").write_text(_ENV, encoding="utf-8")
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


def test_new_key_missing_from_enumerating_doc_is_reported(config_repo: Path) -> None:
    (config_repo / ".env.example").write_text(_ENV + "NEW_LOOP_CAP=4\n", encoding="utf-8")

    bundle = _check(config_repo)

    assert bundle.status is not RunStatus.CLEAN
    kinds = {(finding.kind, finding.component_id) for finding in bundle.findings}
    assert ("config_key_undocumented", "NEW_LOOP_CAP") in kinds
    finding = next(f for f in bundle.findings if f.kind == "config_key_undocumented")
    assert finding.doc_evidence.path == "docs/config.md"
    assert finding.code_evidence.path == ".env.example"
    assert finding.code_evidence.line == 4


def test_new_key_present_in_enumerating_doc_stays_clean(config_repo: Path) -> None:
    (config_repo / ".env.example").write_text(_ENV + "NEW_LOOP_CAP=4\n", encoding="utf-8")
    (config_repo / "docs" / "config.md").write_text(
        _ENUMERATING_DOC + "| `NEW_LOOP_CAP` | 循环上限 |\n", encoding="utf-8"
    )

    bundle = _check(config_repo)

    assert bundle.status is RunStatus.CLEAN
    assert bundle.findings == []


def test_removed_key_still_documented_is_reported_at_doc_line(config_repo: Path) -> None:
    (config_repo / ".env.example").write_text(
        _ENV.replace("REVIEW_THRESHOLD=500\n", ""), encoding="utf-8"
    )

    bundle = _check(config_repo)

    assert bundle.status is not RunStatus.CLEAN
    finding = next(f for f in bundle.findings if f.kind == "config_key_removed")
    assert finding.component_id == "REVIEW_THRESHOLD"
    assert finding.doc_evidence.path == "docs/config.md"
    assert finding.doc_evidence.line == 7
    assert finding.disposition.value == "detected"


def test_unchanged_env_example_produces_no_config_findings(config_repo: Path) -> None:
    (config_repo / "src" / "main.ts").write_text(
        "export const VERSION: string = '2';\n", encoding="utf-8"
    )

    bundle = _check(config_repo)

    assert all(not finding.kind.startswith("config_key") for finding in bundle.findings)


def test_new_key_mentioned_only_in_non_enumerating_doc_still_flags_the_table(
    config_repo: Path,
) -> None:
    # An ADR mentioning the key does not satisfy the enumerating doc's coverage.
    (config_repo / ".env.example").write_text(_ENV + "NEW_LOOP_CAP=4\n", encoding="utf-8")
    (config_repo / "docs" / "adr.md").write_text(
        "# ADR\n\n每轮最多 `NEW_LOOP_CAP` 次迭代。\n", encoding="utf-8"
    )

    bundle = _check(config_repo)

    kinds = {(finding.kind, finding.component_id) for finding in bundle.findings}
    assert ("config_key_undocumented", "NEW_LOOP_CAP") in kinds

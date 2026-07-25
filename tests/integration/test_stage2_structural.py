from __future__ import annotations

import ast
import stat
import subprocess
from pathlib import Path
from typing import Literal

import pytest

from drift_agent.application import run
from drift_agent.domain.enums import FindingDisposition, RunMode, RunStatus
from drift_agent.domain.models import RunRequest
from drift_agent.memory import (
    CHECK_EVENT_SEQUENCE,
    DecisionAddRequest,
    DecisionRevokeRequest,
    DecisionService,
    RunService,
    SQLiteStateStore,
)
from drift_agent.validation.docstring_ast import docstring_ast_unchanged
from drift_agent.workspace.identity import resolve_identities, resolve_state_path


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    ).stdout


def _write_config(repo: Path, *, code_truth: tuple[str, ...] = ("docs/**",)) -> None:
    truth = ", ".join(f'"{pattern}"' for pattern in code_truth)
    (repo / "drift-agent.toml").write_text(
        f"""\
[project]
source_roots = ["src"]
docs_roots = ["docs"]
include = ["src/**/*.py", "docs/**/*.md"]
exclude = []

[truth]
code_derived = [{truth}]
design = []
contract = []

[validation]
commands = []
network = false
""",
        encoding="utf-8",
    )


def _init_repo(
    repo: Path,
    *,
    source: str,
    docs: str,
    code_truth: tuple[str, ...] = ("docs/**",),
) -> tuple[Path, Path]:
    package = repo / "src/demo"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    source_path = package / "api.py"
    source_path.write_text(source, encoding="utf-8")
    docs_path = repo / "docs/api.md"
    docs_path.parent.mkdir()
    docs_path.write_text(docs, encoding="utf-8")
    _write_config(repo, code_truth=code_truth)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "stage2@example.invalid")
    _git(repo, "config", "user.name", "Stage 2")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    return source_path, docs_path


def _worktree_snapshot(repo: Path) -> tuple[dict[str, tuple[bytes, int]], bytes]:
    files: dict[str, tuple[bytes, int]] = {}
    for path in sorted(repo.rglob("*")):
        relative = path.relative_to(repo)
        if not relative.parts or relative.parts[0] == ".git" or not path.is_file():
            continue
        files[relative.as_posix()] = (
            path.read_bytes(),
            stat.S_IMODE(path.stat().st_mode),
        )
    return files, _git(repo, "status", "--porcelain=v1", "-z")


def _without_docstrings(source: bytes) -> str:
    tree = ast.parse(source.decode("utf-8"))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            first.value.value = ""
    return ast.dump(tree, include_attributes=False)


def test_clean_check_is_worktree_read_only_and_persists_exact_event_contract(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(
        repo,
        source="def echo(message: str) -> None: ...\n",
        docs=("### `demo.api.echo`\n```python\ndef echo(message: str) -> None: ...\n```\n"),
    )
    before = _worktree_snapshot(repo)

    bundle = run(RunRequest(mode=RunMode.CHECK, repo_path=repo))

    assert bundle.status is RunStatus.CLEAN
    assert bundle.findings == []
    assert bundle.validation == []
    assert bundle.usage.model_calls == 0
    assert _worktree_snapshot(repo) == before

    identities = resolve_identities(repo)
    state_path = resolve_state_path(repo, identities=identities)
    assert state_path == identities.repository.common_dir / "drift-agent/state-v1.sqlite3"
    assert state_path.is_file()
    store = SQLiteStateStore(state_path)
    events = RunService(store).events(bundle.run_id)
    persisted = store.run(bundle.run_id)

    assert tuple(event.seq for event in events) == tuple(range(1, 7))
    assert tuple(event.kind for event in events) == CHECK_EVENT_SEQUENCE
    assert RunService(store).validate_required_events(bundle.run_id) == CHECK_EVENT_SEQUENCE
    assert persisted is not None
    assert persisted.repository_id == bundle.repository_id
    assert persisted.workspace_id == bundle.workspace_id
    assert persisted.status == bundle.status.value
    assert persisted.finding_count == len(bundle.findings)
    assert persisted.model_calls == bundle.usage.model_calls


def test_two_disjoint_declarations_in_one_file_are_both_repaired(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    source, docs = _init_repo(
        repo,
        source=("def first(value: str) -> None: ...\n\ndef second(count: int) -> None: ...\n"),
        docs=(
            "# Stable introduction\n\n"
            "### `demo.api.first`\n"
            "```python\n"
            "def first(value: str) -> None: ...\n"
            "```\n\n"
            "### `demo.api.second`\n"
            "```python\n"
            "def second(count: int) -> None: ...\n"
            "```\n\n"
            "Stable footer.\n"
        ),
    )
    source.write_text(
        "def first(value: str, enabled: bool = True) -> None: ...\n\n"
        "def second(count: int, label: str = 'x') -> None: ...\n",
        encoding="utf-8",
    )

    bundle = run(RunRequest(mode=RunMode.REPAIR, repo_path=repo))

    assert bundle.status is RunStatus.FIXED
    assert bundle.changes.applied is True
    assert bundle.changes.files == ["docs/api.md"]
    assert len(bundle.findings) == 2
    assert {finding.kind for finding in bundle.findings} == {"parameter_added"}
    assert all(finding.disposition is FindingDisposition.FIXED for finding in bundle.findings)
    assert len(bundle.repair_groups) == 2
    assert all(group.disposition is FindingDisposition.FIXED for group in bundle.repair_groups)
    repaired = docs.read_text(encoding="utf-8")
    assert "def first(value: str, enabled: bool = True) -> None: ..." in repaired
    assert "def second(count: int, label: str = 'x') -> None: ..." in repaired
    assert repaired.startswith("# Stable introduction\n\n")
    assert repaired.endswith("\nStable footer.\n")
    assert bundle.usage.model_calls == 0


def test_external_state_override_writes_only_external_database(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(
        repo,
        source="def echo(message: str) -> None: ...\n",
        docs=("### `demo.api.echo`\n```python\ndef echo(message: str) -> None: ...\n```\n"),
    )
    before = _worktree_snapshot(repo)
    default_state = resolve_state_path(repo)
    external_dir = tmp_path / "external-state"

    bundle = run(
        RunRequest(
            mode=RunMode.CHECK,
            repo_path=repo,
            state_dir=external_dir,
        )
    )

    assert bundle.status is RunStatus.CLEAN
    assert (external_dir / "state-v1.sqlite3").is_file()
    assert not default_state.exists()
    assert _worktree_snapshot(repo) == before

    invalid_state = tmp_path / "not-a-directory"
    invalid_state.write_text("blocked", encoding="utf-8")
    failed = run(
        RunRequest(
            mode=RunMode.CHECK,
            repo_path=repo,
            state_dir=invalid_state,
        )
    )

    assert failed.status is RunStatus.FAILED
    assert failed.changes.applied is False
    assert any("state_dir must be a directory" in item.summary for item in failed.validation)
    assert _worktree_snapshot(repo) == before


def test_google_docstring_repair_preserves_executable_ast_and_other_bytes(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    source, _ = _init_repo(
        repo,
        source='''\
def convert(value: str) -> bytes:
    """Convert a value.

    Args:
        value (str): Keep this description exactly.

    Returns:
        bytes: Keep this return description exactly.
    """
    marker = "business bytes"
    return (marker + value).encode()
''',
        docs="# Documentation root\n",
        code_truth=("docs/**", "src/**"),
    )
    source.write_text(
        source.read_text(encoding="utf-8")
        .replace("value: str", "value: bytes", 1)
        .replace(") -> bytes:", ") -> str:", 1),
        encoding="utf-8",
    )
    before = source.read_bytes()
    expected = before.replace(b"value (str)", b"value (bytes)").replace(
        b"        bytes: Keep this return",
        b"        str: Keep this return",
    )

    bundle = run(RunRequest(mode=RunMode.REPAIR, repo_path=repo))

    after = source.read_bytes()
    assert bundle.status is RunStatus.FIXED
    assert bundle.changes.files == ["src/demo/api.py"]
    assert [(finding.kind, finding.component_id) for finding in bundle.findings] == [
        ("docstring_parameter_changed", "value"),
        ("docstring_return_changed", "return"),
    ]
    assert all(finding.disposition is FindingDisposition.FIXED for finding in bundle.findings)
    assert after == expected
    assert docstring_ast_unchanged(before, after)
    assert _without_docstrings(before) == _without_docstrings(after)
    assert b"Keep this description exactly." in after
    assert b'marker = "business bytes"' in after


def test_deleted_symbol_complete_declaration_is_removed_but_fragment_is_rejected(
    tmp_path: Path,
) -> None:
    complete_repo = tmp_path / "complete"
    complete_source, complete_docs = _init_repo(
        complete_repo,
        source="def legacy(value: str) -> None: ...\n",
        docs=(
            "Intro.\n\n"
            "### `demo.api.legacy`\n"
            "```python\n"
            "def legacy(value: str) -> None: ...\n"
            "```\n\n"
            "Footer.\n"
        ),
    )
    complete_source.write_text("", encoding="utf-8")

    complete = run(RunRequest(mode=RunMode.REPAIR, repo_path=complete_repo))

    assert complete.status is RunStatus.FIXED
    assert len(complete.findings) == 1
    assert complete.findings[0].kind == "symbol_reference_deleted"
    assert complete.findings[0].disposition is FindingDisposition.FIXED
    assert "demo.api.legacy" not in complete_docs.read_text(encoding="utf-8")
    assert complete_docs.read_text(encoding="utf-8") == "Intro.\n\nFooter.\n"

    fragment_repo = tmp_path / "fragment"
    fragment_source, fragment_docs = _init_repo(
        fragment_repo,
        source="def legacy(value: str) -> None: ...\n",
        docs="Use `demo.api.legacy` when migrating old callers.\n",
    )
    fragment_source.write_text("", encoding="utf-8")
    fragment_before = fragment_docs.read_bytes()

    fragment = run(RunRequest(mode=RunMode.REPAIR, repo_path=fragment_repo))

    assert fragment.status is RunStatus.UNRESOLVED
    assert fragment.changes.applied is False
    assert len(fragment.findings) == 1
    assert fragment.findings[0].kind == "symbol_reference_deleted"
    assert fragment.findings[0].reason_code == "unsupported.incomplete_declaration"
    assert fragment.findings[0].disposition is FindingDisposition.UNRESOLVED
    assert fragment_docs.read_bytes() == fragment_before


@pytest.mark.parametrize("action", ["ignore", "false_positive"])
def test_human_decision_suppresses_only_exact_evidence_and_revoke_restores(
    drift_repo: Path,
    action: Literal["ignore", "false_positive"],
) -> None:
    first = run(RunRequest(mode=RunMode.CHECK, repo_path=drift_repo))
    assert len(first.findings) == 1
    state_path = resolve_state_path(drift_repo)
    service = DecisionService(SQLiteStateStore(state_path))
    decision = service.add(
        DecisionAddRequest(
            repository_id=first.repository_id,
            run_id=first.run_id,
            finding_id=first.findings[0].id,
            action=action,
            reason="reviewed evidence",
            actor="maintainer",
            confirmation=True,
        )
    )

    suppressed = run(RunRequest(mode=RunMode.CHECK, repo_path=drift_repo))

    assert suppressed.status is RunStatus.CLEAN
    assert suppressed.findings == []
    assert len(suppressed.suppressed_findings) == 1
    audit = suppressed.suppressed_findings[0]
    assert audit.decision_id == decision.decision_id
    assert audit.action == action
    assert audit.reason == "reviewed evidence"
    assert audit.actor == "maintainer"
    assert audit.evidence_key == decision.validity.digest

    service.revoke(
        DecisionRevokeRequest(
            repository_id=first.repository_id,
            decision_id=decision.decision_id,
            reason="reconsidered",
            actor="maintainer",
        )
    )
    restored = run(RunRequest(mode=RunMode.CHECK, repo_path=drift_repo))

    assert restored.status is RunStatus.DRIFT_FOUND
    assert len(restored.findings) == 1
    assert restored.suppressed_findings == []


def test_changed_value_invalidates_a_decision_and_audits_the_reason(
    drift_repo: Path,
) -> None:
    first = run(RunRequest(mode=RunMode.CHECK, repo_path=drift_repo))
    service = DecisionService(SQLiteStateStore(resolve_state_path(drift_repo)))
    service.add(
        DecisionAddRequest(
            repository_id=first.repository_id,
            run_id=first.run_id,
            finding_id=first.findings[0].id,
            action="ignore",
            reason="old evidence",
            actor="maintainer",
            confirmation=True,
        )
    )
    source = drift_repo / "src/click_demo/api.py"
    source.write_text(
        source.read_text(encoding="utf-8").replace("= True", "= False"),
        encoding="utf-8",
    )

    current = run(RunRequest(mode=RunMode.CHECK, repo_path=drift_repo))

    assert current.status is RunStatus.DRIFT_FOUND
    assert len(current.findings) == 1
    assert current.suppressed_findings == []
    assert any(
        event.kind == "decision_invalidated" and event.reason == "decision.value_mismatch"
        for event in current.memory_events
    )

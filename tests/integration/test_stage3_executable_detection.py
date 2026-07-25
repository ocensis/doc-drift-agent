import subprocess
from pathlib import Path

import pytest

import drift_agent.validation.commands as validation_commands_module
from drift_agent.application import AgentRuntime, run
from drift_agent.domain.enums import RunMode, RunStatus, ValidationStatus
from drift_agent.domain.models import RunBudgets, RunRequest
from drift_agent.domain.serialization import bundle_to_wire
from drift_agent.memory import (
    CHECK_EVENT_SEQUENCE,
    DecisionAddRequest,
    DecisionService,
    RunService,
    SQLiteStateStore,
)
from drift_agent.validation.commands import ValidationCommandRunner
from drift_agent.workspace.identity import resolve_state_path


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _configure_clean_structural_scope(
    repo: Path,
    *,
    commands: list[str],
    doctest_expected: str = "2",
    create_changed_source: bool = True,
) -> None:
    source = repo / "src/click_demo/api.py"
    source.write_text(
        "def echo(message: str, color: bool = True) -> None:\n    return None\n",
        encoding="utf-8",
    )
    document = repo / "docs/api.md"
    document.write_text(
        "Example:\n\n"
        ">>> 1 + 1\n"
        f"{doctest_expected}\n\n"
        "### `click_demo.api.echo`\n\n"
        "```python\n"
        "def echo(message: str, color: bool = True) -> None: ...\n"
        "```\n",
        encoding="utf-8",
    )
    rendered_commands = ", ".join(f'"{command}"' for command in commands)
    config = repo / "drift-agent.toml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "commands = []",
            f"commands = [{rendered_commands}]",
        ),
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "configure check-mode executable examples")
    if create_changed_source:
        source.write_text(
            "def echo(message: str, color: bool = True) -> None:\n"
            "    # body-only change keeps the structural contract stable\n"
            "    return None\n",
            encoding="utf-8",
        )


def _configure_targeted_pytest(
    repo: Path,
    *,
    passing: bool,
    create_changed_source: bool = True,
) -> None:
    tests = repo / "tests"
    tests.mkdir()
    expected = "2" if passing else "3"
    (tests / "test_example.py").write_text(
        f"def test_documented_example() -> None:\n    assert 1 + 1 == {expected}\n",
        encoding="utf-8",
    )
    _configure_clean_structural_scope(
        repo,
        commands=["python -m pytest tests/test_example.py -q"],
        create_changed_source=create_changed_source,
    )


def test_check_runs_passing_doctest_without_writing_repository(
    drift_repo: Path,
) -> None:
    _configure_clean_structural_scope(
        drift_repo,
        commands=["python -m doctest docs/api.md"],
    )
    before = {path: path.read_bytes() for path in drift_repo.rglob("*") if path.is_file()}

    bundle = run(RunRequest(mode=RunMode.CHECK, repo_path=drift_repo))

    assert bundle.status is RunStatus.CLEAN
    assert bundle.findings == []
    assert len(bundle.validation) == 1
    assert bundle.validation[0].check == "check_doctest"
    assert bundle.validation[0].status is ValidationStatus.PASSED
    assert bundle.validation[0].finding_ids == []
    assert bundle.usage.validation_commands == 1
    assert bundle.usage.model_calls == 0
    after = {path: path.read_bytes() for path in before}
    assert after == before
    assert not (drift_repo / ".pytest_cache").exists()
    assert list(drift_repo.rglob("__pycache__")) == []


def test_check_reports_failing_doctest_as_stable_broken_example(
    drift_repo: Path,
) -> None:
    _configure_clean_structural_scope(
        drift_repo,
        commands=["python -m doctest docs/api.md"],
        doctest_expected="3",
    )

    first = run(RunRequest(mode=RunMode.CHECK, repo_path=drift_repo))
    second = run(RunRequest(mode=RunMode.CHECK, repo_path=drift_repo))

    assert first.status is second.status is RunStatus.DRIFT_FOUND
    assert len(first.findings) == len(second.findings) == 1
    finding = first.findings[0]
    assert finding.id == second.findings[0].id
    assert finding.type == "signature_drift"
    assert finding.kind == "broken_example"
    assert finding.reason_code == "validation_failed"
    assert finding.doc_evidence.path == "docs/api.md"
    assert first.validation[0].status is ValidationStatus.FAILED
    assert first.validation[0].finding_ids == [finding.id]
    assert first.usage.validation_commands == 1
    assert first.usage.model_calls == 0
    v1 = bundle_to_wire(first, 1)
    v2 = bundle_to_wire(first, 2)
    assert set(v1["findings"][0]) == {
        "id",
        "symbol_id",
        "type",
        "disposition",
        "truth_source",
        "code_evidence",
        "doc_evidence",
        "reason",
    }
    assert v1["findings"][0]["type"] == "signature_drift"
    assert "doctest validation failed" in v1["findings"][0]["reason"]
    assert v2["findings"][0]["kind"] == "broken_example"
    assert v2["findings"][0]["reason_code"] == "validation_failed"


@pytest.mark.parametrize(
    ("passing", "expected_status", "finding_count", "validation_status"),
    [
        (True, RunStatus.CLEAN, 0, ValidationStatus.PASSED),
        (False, RunStatus.DRIFT_FOUND, 1, ValidationStatus.FAILED),
    ],
)
def test_check_detects_targeted_pytest_examples(
    drift_repo: Path,
    passing: bool,
    expected_status: RunStatus,
    finding_count: int,
    validation_status: ValidationStatus,
) -> None:
    _configure_targeted_pytest(drift_repo, passing=passing)

    bundle = run(RunRequest(mode=RunMode.CHECK, repo_path=drift_repo))

    assert bundle.status is expected_status
    assert len(bundle.findings) == finding_count
    assert bundle.validation[0].check == "check_pytest"
    assert bundle.validation[0].status is validation_status
    assert bundle.usage.validation_commands == 1
    assert bundle.usage.model_calls == 0
    if bundle.findings:
        assert bundle.findings[0].kind == "broken_example"
        assert bundle.findings[0].doc_evidence.path == "tests/test_example.py"
        assert bundle.validation[0].finding_ids == [bundle.findings[0].id]
    assert not (drift_repo / ".pytest_cache").exists()
    assert list(drift_repo.rglob("__pycache__")) == []


def test_check_runs_when_only_out_of_project_validation_target_changed(
    drift_repo: Path,
) -> None:
    _configure_targeted_pytest(
        drift_repo,
        passing=True,
        create_changed_source=False,
    )
    target = drift_repo / "tests/test_example.py"
    target.write_text(
        "def test_documented_example() -> None:\n    assert 1 + 1 == 3\n",
        encoding="utf-8",
    )

    bundle = run(RunRequest(mode=RunMode.CHECK, repo_path=drift_repo))

    assert bundle.scope == []
    assert bundle.status is RunStatus.DRIFT_FOUND
    assert bundle.findings[0].kind == "broken_example"
    assert bundle.usage.validation_commands == 1


def test_required_executable_failure_cannot_be_suppressed_to_clean(
    drift_repo: Path,
) -> None:
    _configure_clean_structural_scope(
        drift_repo,
        commands=["python -m doctest docs/api.md"],
        doctest_expected="3",
    )
    first = run(RunRequest(mode=RunMode.CHECK, repo_path=drift_repo))
    DecisionService(SQLiteStateStore(resolve_state_path(drift_repo))).add(
        DecisionAddRequest(
            repository_id=first.repository_id,
            run_id=first.run_id,
            finding_id=first.findings[0].id,
            action="ignore",
            reason="reviewed legacy envelope",
            actor="maintainer",
            confirmation=True,
        )
    )

    suppressed = run(RunRequest(mode=RunMode.CHECK, repo_path=drift_repo))

    assert suppressed.status is RunStatus.DRIFT_FOUND
    assert suppressed.findings == []
    assert len(suppressed.suppressed_findings) == 1
    assert suppressed.validation[0].status is ValidationStatus.FAILED


def test_target_evidence_change_keeps_logical_symbol_and_audits_decision(
    drift_repo: Path,
) -> None:
    _configure_clean_structural_scope(
        drift_repo,
        commands=["python -m doctest docs/api.md"],
        doctest_expected="3",
    )
    first = run(RunRequest(mode=RunMode.CHECK, repo_path=drift_repo))
    DecisionService(SQLiteStateStore(resolve_state_path(drift_repo))).add(
        DecisionAddRequest(
            repository_id=first.repository_id,
            run_id=first.run_id,
            finding_id=first.findings[0].id,
            action="ignore",
            reason="old target evidence",
            actor="maintainer",
            confirmation=True,
        )
    )
    target = drift_repo / "docs/api.md"
    target.write_text(
        target.read_text(encoding="utf-8").replace(">>> 1 + 1\n3", ">>> 1 + 1\n4"),
        encoding="utf-8",
    )

    current = run(RunRequest(mode=RunMode.CHECK, repo_path=drift_repo))

    assert current.status is RunStatus.DRIFT_FOUND
    assert len(current.findings) == 1
    assert current.findings[0].symbol_id == first.findings[0].symbol_id
    assert current.findings[0].id != first.findings[0].id
    assert any(
        event.kind == "decision_invalidated" and event.reason == "decision.doc_evidence_mismatch"
        for event in current.memory_events
    )


def test_check_unavailable_command_is_unresolved_not_clean_or_drift(
    drift_repo: Path,
) -> None:
    _configure_clean_structural_scope(
        drift_repo,
        commands=["python -m pip list"],
    )

    bundle = run(RunRequest(mode=RunMode.CHECK, repo_path=drift_repo))

    assert bundle.status is RunStatus.UNRESOLVED
    assert bundle.findings == []
    assert len(bundle.validation) == 1
    assert bundle.validation[0].status is ValidationStatus.UNAVAILABLE
    assert bundle.validation[0].finding_ids == []
    assert "validation_unavailable" in bundle.validation[0].summary
    assert bundle.usage.validation_commands == 0
    service = RunService(SQLiteStateStore(resolve_state_path(drift_repo)))
    assert service.validate_required_events(bundle.run_id) == CHECK_EVENT_SEQUENCE


def test_pytest_infrastructure_exit_does_not_create_broken_example(
    drift_repo: Path,
) -> None:
    _configure_targeted_pytest(drift_repo, passing=True)

    class PytestInternalError:
        def __call__(
            self,
            argv: list[str] | tuple[str, ...],
            **_: object,
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 3, stdout="", stderr="internal error")

    runtime = AgentRuntime()
    runtime.validation_runner = ValidationCommandRunner(process_runner=PytestInternalError())

    bundle = run(
        RunRequest(mode=RunMode.CHECK, repo_path=drift_repo),
        runtime=runtime,
    )

    assert bundle.status is RunStatus.UNRESOLVED
    assert bundle.findings == []
    assert bundle.validation[0].status is ValidationStatus.UNAVAILABLE
    assert bundle.validation[0].finding_ids == []
    assert bundle.usage.validation_commands == 1


def test_doctest_infrastructure_exit_does_not_create_broken_example(
    drift_repo: Path,
) -> None:
    _configure_clean_structural_scope(
        drift_repo,
        commands=["python -m doctest docs/api.md"],
    )

    class DoctestUsageError:
        def __call__(
            self,
            argv: list[str] | tuple[str, ...],
            **_: object,
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 2, stdout="", stderr="usage error")

    runtime = AgentRuntime()
    runtime.validation_runner = ValidationCommandRunner(process_runner=DoctestUsageError())

    bundle = run(
        RunRequest(mode=RunMode.CHECK, repo_path=drift_repo),
        runtime=runtime,
    )

    assert bundle.status is RunStatus.UNRESOLVED
    assert bundle.findings == []
    assert bundle.validation[0].status is ValidationStatus.UNAVAILABLE
    assert bundle.validation[0].finding_ids == []
    assert bundle.usage.validation_commands == 1


@pytest.mark.parametrize("operation", ["add", "delete"])
def test_check_rejects_dependency_manifest_change_before_copy(
    drift_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    _configure_clean_structural_scope(
        drift_repo,
        commands=["python -m doctest docs/api.md"],
        create_changed_source=False,
    )
    dependency = drift_repo / "helper.py"
    if operation == "delete":
        dependency.write_text("VALUE = 2\n", encoding="utf-8")
        _git(drift_repo, "add", "helper.py")
        _git(drift_repo, "commit", "-qm", "add validation dependency")

    original_copy = validation_commands_module._copy_validation_workspace

    def mutate_before_copy(source: Path, destination: Path) -> None:
        if operation == "add":
            dependency.write_text("VALUE = 2\n", encoding="utf-8")
        else:
            dependency.unlink()
        original_copy(source, destination)

    monkeypatch.setattr(
        validation_commands_module,
        "_copy_validation_workspace",
        mutate_before_copy,
    )

    class MustNotRun:
        def __call__(self, *_: object, **__: object) -> subprocess.CompletedProcess[str]:
            raise AssertionError("changed validation inputs must prevent process startup")

    runtime = AgentRuntime()
    runtime.validation_runner = ValidationCommandRunner(process_runner=MustNotRun())

    bundle = run(
        RunRequest(mode=RunMode.CHECK, repo_path=drift_repo),
        runtime=runtime,
    )

    assert bundle.status is RunStatus.STALE
    assert bundle.findings == []
    assert bundle.validation[0].status is ValidationStatus.UNAVAILABLE
    assert "global_snapshot_changed" in bundle.validation[0].summary
    assert bundle.usage.validation_commands == 1


def test_check_validation_budget_zero_starts_no_process(
    drift_repo: Path,
) -> None:
    _configure_clean_structural_scope(
        drift_repo,
        commands=["python -m doctest docs/api.md"],
    )

    class MustNotRun:
        def __call__(self, *_: object, **__: object) -> subprocess.CompletedProcess[str]:
            raise AssertionError("validation process must not start")

    runtime = AgentRuntime()
    runtime.validation_runner = ValidationCommandRunner(process_runner=MustNotRun())
    bundle = run(
        RunRequest(
            mode=RunMode.CHECK,
            repo_path=drift_repo,
            budgets=RunBudgets(max_validation_commands_per_run=0),
        ),
        runtime=runtime,
    )

    assert bundle.status is RunStatus.UNRESOLVED
    assert bundle.usage.validation_commands == 0
    assert any("budget_exhausted" in result.summary for result in bundle.validation)


def test_check_target_change_during_execution_is_stale(
    drift_repo: Path,
) -> None:
    _configure_clean_structural_scope(
        drift_repo,
        commands=["python -m doctest docs/api.md"],
    )
    target = drift_repo / "docs/api.md"

    class MutateSourceTarget:
        def __call__(
            self,
            argv: list[str] | tuple[str, ...],
            **_: object,
        ) -> subprocess.CompletedProcess[str]:
            target.write_text("externally changed\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    runtime = AgentRuntime()
    runtime.validation_runner = ValidationCommandRunner(process_runner=MutateSourceTarget())

    bundle = run(
        RunRequest(mode=RunMode.CHECK, repo_path=drift_repo),
        runtime=runtime,
    )

    assert bundle.status is RunStatus.STALE
    assert bundle.changes.applied is False

import subprocess
from pathlib import Path

import pytest

import drift_agent.application as application_module
from drift_agent.agent.budget import BudgetLedger
from drift_agent.application import AgentRuntime, run
from drift_agent.domain.enums import FindingDisposition, RunMode, RunStatus, ValidationStatus
from drift_agent.domain.models import RunBudgets, RunRequest
from drift_agent.memory import RunService, SQLiteStateStore
from drift_agent.validation.commands import ValidationCommandRunner
from drift_agent.workspace.identity import resolve_state_path
from drift_agent.workspace.lock import LockTimeoutError


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _configure_doctest(repo: Path, *, expected: str) -> bytes:
    document = repo / "docs/api.md"
    document.write_text(
        "Example:\n\n"
        ">>> 1 + 1\n"
        f"{expected}\n\n"
        "### `click_demo.api.echo`\n\n"
        "```python\n"
        "def echo(message: str) -> None: ...\n"
        "```\n",
        encoding="utf-8",
    )
    config = repo / "drift-agent.toml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "commands = []",
            'commands = ["python -m doctest docs/api.md"]',
        ),
        encoding="utf-8",
    )
    _git(repo, "add", "docs/api.md", "drift-agent.toml")
    _git(repo, "commit", "-qm", "configure executable documentation validation")
    return document.read_bytes()


def _configure_pytest(repo: Path) -> None:
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_example.py").write_text(
        "def test_documented_example() -> None:\n    assert 1 + 1 == 2\n",
        encoding="utf-8",
    )
    config = repo / "drift-agent.toml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "commands = []",
            'commands = ["python -m pytest tests/test_example.py -q"]',
        ),
        encoding="utf-8",
    )
    _git(repo, "add", "tests/test_example.py", "drift-agent.toml")
    _git(repo, "commit", "-qm", "configure targeted pytest validation")


def test_passing_doctest_is_required_before_a_repair_is_retained(
    drift_repo: Path,
) -> None:
    _configure_doctest(drift_repo, expected="2")

    bundle = run(RunRequest(mode=RunMode.REPAIR, repo_path=drift_repo))

    assert bundle.status is RunStatus.FIXED
    assert bundle.changes.applied is True
    assert b"color: bool = True" in (drift_repo / "docs/api.md").read_bytes()
    executable = [result for result in bundle.validation if "doctest" in result.check]
    assert len(executable) == 2
    assert all(result.status is ValidationStatus.PASSED for result in executable)
    assert bundle.usage.validation_commands == 2
    assert bundle.usage.model_calls == 0


def test_failing_doctest_rolls_back_the_unverified_repair(
    drift_repo: Path,
) -> None:
    before = _configure_doctest(drift_repo, expected="3")

    bundle = run(RunRequest(mode=RunMode.REPAIR, repo_path=drift_repo))

    assert bundle.status is RunStatus.UNRESOLVED
    assert bundle.changes.applied is False
    assert (drift_repo / "docs/api.md").read_bytes() == before
    assert bundle.findings[0].disposition is FindingDisposition.UNRESOLVED
    assert bundle.findings[0].reason_code == "validation_failed"
    executable = [result for result in bundle.validation if "doctest" in result.check]
    assert len(executable) == 1
    assert executable[0].status is ValidationStatus.FAILED
    assert bundle.usage.validation_commands == 1


def test_validation_budget_exhaustion_starts_no_command_and_rolls_back(
    drift_repo: Path,
) -> None:
    before = _configure_doctest(drift_repo, expected="2")

    bundle = run(
        RunRequest(
            mode=RunMode.REPAIR,
            repo_path=drift_repo,
            budgets=RunBudgets(max_validation_commands_per_run=0),
        )
    )

    assert bundle.status is RunStatus.UNRESOLVED
    assert bundle.changes.applied is False
    assert (drift_repo / "docs/api.md").read_bytes() == before
    assert bundle.findings[0].reason_code == "budget_exhausted"
    assert any("budget_exhausted" in result.summary for result in bundle.validation)
    assert bundle.usage.validation_commands == 0


def test_validation_budget_reserves_the_mandatory_final_pass_before_writing(
    drift_repo: Path,
) -> None:
    before = _configure_doctest(drift_repo, expected="2")

    bundle = run(
        RunRequest(
            mode=RunMode.REPAIR,
            repo_path=drift_repo,
            budgets=RunBudgets(max_validation_commands_per_run=1),
        )
    )

    assert bundle.status is RunStatus.UNRESOLVED
    assert bundle.changes.applied is False
    assert (drift_repo / "docs/api.md").read_bytes() == before
    assert bundle.findings[0].reason_code == "budget_exhausted"
    assert bundle.usage.validation_commands == 0


def test_zero_wall_clock_budget_performs_no_repair_write_or_command(
    drift_repo: Path,
) -> None:
    before = _configure_doctest(drift_repo, expected="2")

    bundle = run(
        RunRequest(
            mode=RunMode.REPAIR,
            repo_path=drift_repo,
            budgets=RunBudgets(timeout_seconds=0),
        )
    )

    assert bundle.status is RunStatus.UNRESOLVED
    assert bundle.changes.applied is False
    assert (drift_repo / "docs/api.md").read_bytes() == before
    assert bundle.findings[0].reason_code == "budget_exhausted"
    assert bundle.usage.validation_commands == 0
    service = RunService(SQLiteStateStore(resolve_state_path(drift_repo)))
    kinds = tuple(event.kind for event in service.events(bundle.run_id))
    assert kinds[-2:] == ("budget_exhausted", "run_finished")
    assert "lock_acquired" not in kinds
    assert service.validate_required_events(bundle.run_id) == kinds


def test_zero_wall_clock_budget_cannot_report_a_successful_check(
    drift_repo: Path,
) -> None:
    _configure_doctest(drift_repo, expected="2")

    bundle = run(
        RunRequest(
            mode=RunMode.CHECK,
            repo_path=drift_repo,
            budgets=RunBudgets(timeout_seconds=0),
        )
    )

    assert bundle.status is RunStatus.UNRESOLVED
    assert bundle.findings[0].reason_code == "budget_exhausted"
    assert any(result.check == "run_deadline" for result in bundle.validation)
    assert bundle.usage.validation_commands == 0


def test_targeted_pytest_runs_without_leaving_repository_caches(
    drift_repo: Path,
) -> None:
    _configure_pytest(drift_repo)

    bundle = run(RunRequest(mode=RunMode.REPAIR, repo_path=drift_repo))

    assert bundle.status is RunStatus.FIXED
    executable = [result for result in bundle.validation if "pytest" in result.check]
    assert len(executable) == 2
    assert all(result.status is ValidationStatus.PASSED for result in executable)
    assert bundle.usage.validation_commands == 2
    assert not (drift_repo / ".pytest_cache").exists()
    assert list(drift_repo.rglob("__pycache__")) == []


def test_non_allowlisted_configured_command_is_unavailable_without_execution(
    drift_repo: Path,
) -> None:
    before = (drift_repo / "docs/api.md").read_bytes()
    config = drift_repo / "drift-agent.toml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "commands = []",
            'commands = ["python -m pip list"]',
        ),
        encoding="utf-8",
    )
    _git(drift_repo, "add", "drift-agent.toml")
    _git(drift_repo, "commit", "-qm", "configure rejected validation command")

    bundle = run(RunRequest(mode=RunMode.REPAIR, repo_path=drift_repo))

    assert bundle.status is RunStatus.UNRESOLVED
    assert bundle.changes.applied is False
    assert (drift_repo / "docs/api.md").read_bytes() == before
    assert bundle.findings[0].reason_code == "validation_unavailable"
    assert any("validation_unavailable" in result.summary for result in bundle.validation)
    assert bundle.usage.validation_commands == 0


def test_final_command_failure_rolls_back_a_locally_valid_group(
    drift_repo: Path,
) -> None:
    before = _configure_doctest(drift_repo, expected="2")

    class PassThenFail:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(
            self,
            argv: list[str] | tuple[str, ...],
            **_: object,
        ) -> subprocess.CompletedProcess[str]:
            self.calls += 1
            return subprocess.CompletedProcess(
                argv,
                0 if self.calls == 1 else 1,
                stdout="",
                stderr="final failure" if self.calls == 2 else "",
            )

    process = PassThenFail()
    runtime = AgentRuntime()
    runtime.validation_runner = ValidationCommandRunner(process_runner=process)

    bundle = run(
        RunRequest(mode=RunMode.REPAIR, repo_path=drift_repo),
        runtime=runtime,
    )

    assert process.calls == 2
    assert bundle.status is RunStatus.UNRESOLVED
    assert bundle.changes.applied is False
    assert (drift_repo / "docs/api.md").read_bytes() == before
    assert bundle.findings[0].reason_code == "final_validation_failed"
    assert bundle.usage.validation_commands == 2


def test_validation_target_change_invalidates_the_final_snapshot(
    drift_repo: Path,
) -> None:
    _configure_pytest(drift_repo)
    document = drift_repo / "docs/api.md"
    before = document.read_bytes()
    target = drift_repo / "tests/test_example.py"

    class MutateTargetAfterFinalPass:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(
            self,
            argv: list[str] | tuple[str, ...],
            **_: object,
        ) -> subprocess.CompletedProcess[str]:
            self.calls += 1
            if self.calls == 2:
                target.write_text(
                    "def test_documented_example() -> None:\n    assert False\n",
                    encoding="utf-8",
                )
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    process = MutateTargetAfterFinalPass()
    runtime = AgentRuntime()
    runtime.validation_runner = ValidationCommandRunner(process_runner=process)

    bundle = run(
        RunRequest(mode=RunMode.REPAIR, repo_path=drift_repo),
        runtime=runtime,
    )

    assert process.calls == 2
    assert bundle.status is RunStatus.STALE
    assert bundle.changes.applied is False
    assert document.read_bytes() == before
    assert "tests/test_example.py" in bundle.snapshot.input_file_hashes


def test_deadline_expiring_during_lock_wait_is_budget_exhaustion(
    drift_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock()
    monkeypatch.setattr(
        application_module,
        "BudgetLedger",
        lambda budgets: BudgetLedger(budgets, clock=clock),
    )

    class ExpiringLock:
        @classmethod
        def from_identities(cls, *_: object, **__: object) -> "ExpiringLock":
            return cls()

        def acquire(self) -> None:
            clock.advance(10)
            raise LockTimeoutError("simulated deadline-bound lock wait")

        def release(self) -> None:
            return None

    monkeypatch.setattr(application_module, "WorkspaceLock", ExpiringLock)
    before = (drift_repo / "docs/api.md").read_bytes()

    bundle = run(
        RunRequest(
            mode=RunMode.REPAIR,
            repo_path=drift_repo,
            budgets=RunBudgets(timeout_seconds=10),
        )
    )

    assert bundle.status is RunStatus.UNRESOLVED
    assert bundle.changes.applied is False
    assert (drift_repo / "docs/api.md").read_bytes() == before
    assert bundle.findings[0].reason_code == "budget_exhausted"
    assert any(result.check == "run_deadline" for result in bundle.validation)
    service = RunService(SQLiteStateStore(resolve_state_path(drift_repo)))
    kinds = tuple(event.kind for event in service.events(bundle.run_id))
    assert kinds[-2:] == ("budget_exhausted", "run_finished")
    assert "lock_acquired" not in kinds
    assert service.validate_required_events(bundle.run_id) == kinds


def test_deadline_is_rechecked_after_final_snapshot_preflight(
    drift_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _configure_doctest(drift_repo, expected="2")
    clock = _FakeClock()
    monkeypatch.setattr(
        application_module,
        "BudgetLedger",
        lambda budgets: BudgetLedger(budgets, clock=clock),
    )

    class PassingCommands:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(
            self,
            argv: list[str] | tuple[str, ...],
            **_: object,
        ) -> subprocess.CompletedProcess[str]:
            self.calls += 1
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    process = PassingCommands()
    original_event = RunService.event

    def advance_during_publication(
        self: RunService,
        run_id: str,
        kind: object,
        payload: dict[str, object] | None = None,
        **kwargs: object,
    ) -> object:
        result = original_event(  # type: ignore[arg-type]
            self,
            run_id,
            kind,
            payload,
            **kwargs,  # type: ignore[arg-type]
        )
        if kind == "final_validation_completed":
            clock.advance(10)
        return result

    monkeypatch.setattr(
        application_module.RunService,
        "event",
        advance_during_publication,
    )
    runtime = AgentRuntime()
    runtime.validation_runner = ValidationCommandRunner(process_runner=process)

    bundle = run(
        RunRequest(
            mode=RunMode.REPAIR,
            repo_path=drift_repo,
            budgets=RunBudgets(timeout_seconds=10),
        ),
        runtime=runtime,
    )

    assert process.calls == 2
    assert bundle.status is RunStatus.UNRESOLVED
    assert bundle.changes.applied is False
    assert (drift_repo / "docs/api.md").read_bytes() == before
    assert bundle.findings[0].reason_code == "budget_exhausted"
    assert any(result.check == "publish_deadline" for result in bundle.validation)
    service = RunService(SQLiteStateStore(resolve_state_path(drift_repo)))
    events = service.events(bundle.run_id)
    assert tuple(event.kind for event in events)[-3:] == (
        "final_validation_completed",
        "publication_aborted",
        "run_finished",
    )
    assert events[-2].payload["reason_code"] == "budget_exhausted"
    assert service.validate_required_events(bundle.run_id) == tuple(event.kind for event in events)


def test_validation_target_is_rechecked_after_publication_events(
    drift_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_pytest(drift_repo)
    document = drift_repo / "docs/api.md"
    before = document.read_bytes()
    target = drift_repo / "tests/test_example.py"
    original_event = RunService.event

    def mutate_during_publication(
        self: RunService,
        run_id: str,
        kind: object,
        payload: dict[str, object] | None = None,
        **kwargs: object,
    ) -> object:
        result = original_event(  # type: ignore[arg-type]
            self,
            run_id,
            kind,
            payload,
            **kwargs,  # type: ignore[arg-type]
        )
        if kind == "final_validation_completed":
            target.write_text(
                "def test_documented_example() -> None:\n    assert False\n",
                encoding="utf-8",
            )
        return result

    monkeypatch.setattr(
        application_module.RunService,
        "event",
        mutate_during_publication,
    )

    class PassingCommands:
        def __call__(
            self,
            argv: list[str] | tuple[str, ...],
            **_: object,
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    runtime = AgentRuntime()
    runtime.validation_runner = ValidationCommandRunner(process_runner=PassingCommands())

    bundle = run(
        RunRequest(mode=RunMode.REPAIR, repo_path=drift_repo),
        runtime=runtime,
    )

    assert bundle.status is RunStatus.STALE
    assert bundle.changes.applied is False
    assert document.read_bytes() == before
    service = RunService(SQLiteStateStore(resolve_state_path(drift_repo)))
    events = service.events(bundle.run_id)
    assert tuple(event.kind for event in events)[-3:] == (
        "final_validation_completed",
        "publication_aborted",
        "run_finished",
    )
    assert events[-2].payload["reason_code"] == "global_snapshot_changed"
    assert service.validate_required_events(bundle.run_id) == tuple(event.kind for event in events)

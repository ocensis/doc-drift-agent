from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from drift_agent.application import AgentRuntime, run
from drift_agent.domain.enums import FindingDisposition, RunMode, RunStatus, ValidationStatus
from drift_agent.domain.models import RunBudgets, RunRequest, ValidationResult
from drift_agent.model.client import ModelClientError
from drift_agent.model.contracts import (
    ModelCallUsage,
    ModelTokenUsage,
    StructuredModelRequest,
    StructuredModelResponse,
)
from drift_agent.repair.planner import RepairGroup
from drift_agent.workspace.transaction import WorkspaceTransaction


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        shell=False,
    )


def _document(value: int) -> str:
    return (
        f"### `demo.api.answer`\n\n```python\ndef answer() -> int: ...\n```\n\nReturns `{value}`.\n"
    )


def _config(*, truth: str = "code_derived") -> str:
    code_derived = '["docs/**"]' if truth == "code_derived" else "[]"
    return f"""\
[project]
source_roots = ["src"]
docs_roots = ["docs"]
include = ["src/**/*.py", "docs/**/*.md"]
exclude = []

[truth]
code_derived = {code_derived}
design = []
contract = []

[validation]
commands = []
network = false
"""


def _repo(
    tmp_path: Path,
    *,
    duplicate_claim: bool = False,
    truth: str = "code_derived",
) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    source_dir = repo / "src/demo"
    docs_dir = repo / "docs"
    source_dir.mkdir(parents=True)
    docs_dir.mkdir()
    (source_dir / "__init__.py").write_text("", encoding="utf-8")
    source = source_dir / "api.py"
    source.write_text("def answer() -> int:\n    return 1\n", encoding="utf-8")
    (docs_dir / "api.md").write_text(_document(1), encoding="utf-8")
    if duplicate_claim:
        (docs_dir / "duplicate.md").write_text(_document(1), encoding="utf-8")
    (repo / "drift-agent.toml").write_text(
        _config(truth=truth),
        encoding="utf-8",
    )
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "semantic-repair@example.invalid")
    _git(repo, "config", "user.name", "semantic-repair")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "baseline")
    source.write_text("def answer() -> int:\n    return 2\n", encoding="utf-8")
    return repo, tmp_path / "state"


def _proposal(
    *,
    confidence: str = "high",
    decision: str = "replace",
    replacement_text: str = "2",
    **extra: object,
) -> dict[str, object]:
    return {
        "decision": decision,
        "replacement_text": replacement_text,
        "confidence": confidence,
        "rationale": "The local code fact requires this exact literal.",
        **extra,
    }


class ScriptedTransport:
    def __init__(
        self,
        outputs: list[dict[str, object] | ModelClientError],
    ) -> None:
        self.outputs = list(outputs)
        self.requests: list[StructuredModelRequest] = []

    def validate_request(self, request: StructuredModelRequest) -> None:
        return None

    def complete_structured(
        self,
        request: StructuredModelRequest,
        *,
        timeout_seconds: float,
    ) -> StructuredModelResponse:
        assert timeout_seconds > 0
        self.requests.append(request)
        if not self.outputs:
            raise AssertionError("unexpected model call")
        output = self.outputs.pop(0)
        if isinstance(output, ModelClientError):
            raise output
        return StructuredModelResponse(
            provider="test",
            profile=request.profile,
            requested_model=f"test/{request.profile}",
            actual_model=f"test/{request.profile}",
            request_id=f"request-{len(self.requests)}",
            finish_reason="stop",
            output=output,
            usage=ModelCallUsage(
                prompt_tokens=7,
                completion_tokens=3,
                total_tokens=10,
                cost_usd=0.00001,
            ),
        )


class FailingSemanticValidationRuntime(AgentRuntime):
    def __init__(self, transport: ScriptedTransport, *, failures: int) -> None:
        super().__init__(model_transport=transport)
        self.remaining_semantic_failures = failures

    def _validate_group(
        self,
        state: dict[str, Any],
        group: RepairGroup,
        transaction: WorkspaceTransaction,
    ) -> ValidationResult:
        result = super()._validate_group(state, group, transaction)  # type: ignore[arg-type]
        if result.check == "semantic_redetect" and self.remaining_semantic_failures > 0:
            self.remaining_semantic_failures -= 1
            return result.model_copy(
                update={
                    "status": ValidationStatus.FAILED,
                    "summary": "scripted semantic validation failure",
                }
            )
        return result


def _repair(
    repo: Path,
    state_dir: Path,
    runtime: AgentRuntime,
    *,
    budgets: RunBudgets | None = None,
):
    return run(
        RunRequest(
            mode=RunMode.REPAIR,
            repo_path=repo,
            state_dir=state_dir,
            semantic_repair=True,
            budgets=budgets or RunBudgets(),
        ),
        runtime=runtime,
    )


def test_fast_high_confidence_repairs_one_trusted_literal(tmp_path: Path) -> None:
    repo, state_dir = _repo(tmp_path)
    source_before = (repo / "src/demo/api.py").read_bytes()
    transport = ScriptedTransport([_proposal()])
    runtime = AgentRuntime(model_transport=transport)

    bundle = _repair(repo, state_dir, runtime)

    assert bundle.status is RunStatus.FIXED
    assert (repo / "docs/api.md").read_text(encoding="utf-8") == _document(2)
    assert (repo / "src/demo/api.py").read_bytes() == source_before
    assert bundle.changes.applied is True
    assert bundle.changes.files == ["docs/api.md"]
    assert len(bundle.findings) == 1
    assert bundle.findings[0].disposition is FindingDisposition.FIXED
    assert bundle.findings[0].reason_code == "validated"
    assert bundle.usage.model_calls_by_profile == {"fast": 1}
    assert bundle.usage.input_tokens == 7
    assert bundle.usage.output_tokens == 3
    assert runtime.budget_ledger is not None
    assert runtime.budget_ledger.patch_attempts_for(bundle.findings[0].id) == 1
    assert [request.profile for request in transport.requests] == ["fast"]
    assert transport.outputs == []
    assert any(
        result.check == "semantic_redetect" and result.status is ValidationStatus.PASSED
        for result in bundle.validation
    )
    assert runtime.run_service is not None
    runtime.run_service.validate_required_events(bundle.run_id)


def test_negative_integer_repair_uses_the_trusted_literal_boundary(tmp_path: Path) -> None:
    repo, state_dir = _repo(tmp_path)
    source = repo / "src/demo/api.py"
    source.write_text("def answer() -> int:\n    return -2\n", encoding="utf-8")
    source_before = source.read_bytes()
    transport = ScriptedTransport([_proposal(replacement_text="-2")])
    runtime = AgentRuntime(model_transport=transport)

    bundle = _repair(repo, state_dir, runtime)

    assert bundle.status is RunStatus.FIXED
    assert (repo / "docs/api.md").read_text(encoding="utf-8") == _document(-2)
    assert source.read_bytes() == source_before
    assert bundle.findings[0].new_value == {
        "predicate": "return_literal",
        "value": {"type": "int", "value": -2},
    }
    assert bundle.changes.applied is True
    assert transport.outputs == []


def test_low_confidence_fast_escalates_before_first_patch(tmp_path: Path) -> None:
    repo, state_dir = _repo(tmp_path)
    transport = ScriptedTransport(
        [
            _proposal(confidence="low"),
            _proposal(confidence="high"),
        ]
    )
    runtime = AgentRuntime(model_transport=transport)

    bundle = _repair(repo, state_dir, runtime)

    assert bundle.status is RunStatus.FIXED
    assert [request.profile for request in transport.requests] == ["fast", "strong"]
    assert bundle.usage.model_calls_by_profile == {"fast": 1, "strong": 1}
    assert runtime.budget_ledger is not None
    assert runtime.budget_ledger.patch_attempts_for(bundle.findings[0].id) == 1
    assert len(bundle.changes.files) == 1


def test_first_validation_failure_rolls_back_then_uses_strong_attempt_two(
    tmp_path: Path,
) -> None:
    repo, state_dir = _repo(tmp_path)
    transport = ScriptedTransport([_proposal(), _proposal()])
    runtime = FailingSemanticValidationRuntime(transport, failures=1)

    bundle = _repair(repo, state_dir, runtime)

    assert bundle.status is RunStatus.FIXED
    assert (repo / "docs/api.md").read_text(encoding="utf-8") == _document(2)
    assert [request.profile for request in transport.requests] == ["fast", "strong"]
    assert runtime.budget_ledger is not None
    assert runtime.budget_ledger.patch_attempts_for(bundle.findings[0].id) == 2
    assert runtime.run_service is not None
    runtime.run_service.validate_required_events(bundle.run_id)
    assert len(bundle.repair_groups) == 1
    assert bundle.repair_groups[0].reason_code == "validated"
    semantic_results = [
        result for result in bundle.validation if result.check == "semantic_redetect"
    ]
    assert [result.status for result in semantic_results[:2]] == [
        ValidationStatus.FAILED,
        ValidationStatus.PASSED,
    ]


def test_two_validation_failures_abstain_and_restore_agent_bytes(tmp_path: Path) -> None:
    repo, state_dir = _repo(tmp_path)
    docs_before = (repo / "docs/api.md").read_bytes()
    transport = ScriptedTransport([_proposal(), _proposal()])
    runtime = FailingSemanticValidationRuntime(transport, failures=2)

    bundle = _repair(repo, state_dir, runtime)

    assert bundle.status is RunStatus.UNRESOLVED
    assert (repo / "docs/api.md").read_bytes() == docs_before
    assert bundle.changes.applied is False
    assert bundle.changes.files == []
    assert bundle.findings[0].disposition is FindingDisposition.UNRESOLVED
    assert bundle.findings[0].reason_code == "semantic_validation_failed"
    assert [request.profile for request in transport.requests] == ["fast", "strong"]
    assert runtime.budget_ledger is not None
    assert runtime.budget_ledger.patch_attempts_for(bundle.findings[0].id) == 2
    assert runtime.run_service is not None
    runtime.run_service.validate_required_events(bundle.run_id)


def test_patch_budget_is_checked_before_strong_model_escalation(tmp_path: Path) -> None:
    repo, state_dir = _repo(tmp_path)
    docs_before = (repo / "docs/api.md").read_bytes()
    transport = ScriptedTransport([_proposal()])
    runtime = FailingSemanticValidationRuntime(transport, failures=1)

    bundle = _repair(
        repo,
        state_dir,
        runtime,
        budgets=RunBudgets(max_patch_attempts_per_finding=1),
    )

    assert bundle.status is RunStatus.UNRESOLVED
    assert (repo / "docs/api.md").read_bytes() == docs_before
    assert bundle.findings[0].reason_code == "budget_exhausted"
    assert [request.profile for request in transport.requests] == ["fast"]
    assert runtime.budget_ledger is not None
    assert runtime.budget_ledger.patch_attempts_for(bundle.findings[0].id) == 1
    assert runtime.run_service is not None
    runtime.run_service.validate_required_events(bundle.run_id)


def test_schema_repair_is_once_and_never_applies_command_fields(tmp_path: Path) -> None:
    repo, state_dir = _repo(tmp_path)
    docs_before = (repo / "docs/api.md").read_bytes()
    unsafe = _proposal(command="rm -rf .")
    transport = ScriptedTransport([unsafe, unsafe])
    runtime = AgentRuntime(model_transport=transport)

    bundle = _repair(repo, state_dir, runtime)

    assert bundle.status is RunStatus.UNRESOLVED
    assert (repo / "docs/api.md").read_bytes() == docs_before
    assert bundle.findings[0].reason_code == "model_schema_invalid"
    assert bundle.changes.applied is False
    assert [request.profile for request in transport.requests] == ["fast", "fast"]
    assert "previous response failed" in transport.requests[1].system_prompt.lower()
    assert bundle.usage.model_calls == 2
    assert bundle.usage.input_tokens == 14
    assert runtime.budget_ledger is not None
    assert runtime.budget_ledger.patch_attempts_for(bundle.findings[0].id) == 0
    assert runtime.run_service is not None
    runtime.run_service.validate_required_events(bundle.run_id)


def test_model_budget_exhaustion_stops_before_transport_or_patch(tmp_path: Path) -> None:
    repo, state_dir = _repo(tmp_path)
    docs_before = (repo / "docs/api.md").read_bytes()
    transport = ScriptedTransport([_proposal()])
    runtime = AgentRuntime(model_transport=transport)

    bundle = _repair(
        repo,
        state_dir,
        runtime,
        budgets=RunBudgets(max_model_calls_per_run=0),
    )

    assert bundle.status is RunStatus.UNRESOLVED
    assert (repo / "docs/api.md").read_bytes() == docs_before
    assert bundle.findings[0].reason_code == "budget_exhausted"
    assert transport.requests == []
    assert bundle.usage.model_calls == 0
    assert runtime.budget_ledger is not None
    assert runtime.budget_ledger.patch_attempts_for(bundle.findings[0].id) == 0
    assert runtime.run_service is not None
    kinds = runtime.run_service.validate_required_events(bundle.run_id)
    assert kinds[-3:] == (
        "budget_exhausted",
        "final_validation_completed",
        "run_finished",
    )


def test_partial_provider_accounting_is_explicit_and_preserves_known_tokens(
    tmp_path: Path,
) -> None:
    repo, state_dir = _repo(tmp_path)
    transport = ScriptedTransport(
        [
            ModelClientError(
                "provider_unavailable",
                usage=ModelTokenUsage(
                    prompt_tokens=9,
                    completion_tokens=2,
                    total_tokens=11,
                ),
            )
        ]
    )
    runtime = AgentRuntime(model_transport=transport)

    bundle = _repair(repo, state_dir, runtime)

    assert bundle.status is RunStatus.UNRESOLVED
    assert bundle.findings[0].reason_code == "accounting_incomplete"
    assert bundle.usage.model_calls == 1
    assert bundle.usage.input_tokens == 9
    assert bundle.usage.output_tokens == 2
    assert bundle.usage.estimated_cost_usd == 0
    assert runtime.budget_ledger is not None
    assert runtime.budget_ledger.patch_attempts_for(bundle.findings[0].id) == 0


def test_later_model_budget_exhaustion_retains_prior_independent_fixed_group(
    tmp_path: Path,
) -> None:
    repo, state_dir = _repo(tmp_path)
    other_source = repo / "src/demo/other.py"
    other_doc = repo / "docs/other.md"
    other_source.write_text("def answer2() -> int:\n    return 1\n", encoding="utf-8")
    other_doc.write_text(
        "### `demo.other.answer2`\n\n```python\ndef answer2() -> int: ...\n```\n\nReturns `1`.\n",
        encoding="utf-8",
    )
    _git(repo, "add", "src/demo/other.py", "docs/other.md")
    _git(repo, "commit", "-qm", "add second semantic baseline")
    other_source.write_text("def answer2() -> int:\n    return 2\n", encoding="utf-8")
    transport = ScriptedTransport([_proposal()])
    runtime = AgentRuntime(model_transport=transport)

    bundle = _repair(
        repo,
        state_dir,
        runtime,
        budgets=RunBudgets(max_model_calls_per_run=1),
    )

    assert bundle.status is RunStatus.PARTIAL
    assert (repo / "docs/api.md").read_text(encoding="utf-8") == _document(2)
    assert other_doc.read_text(encoding="utf-8").endswith("Returns `1`.\n")
    assert bundle.changes.files == ["docs/api.md"]
    assert [request.profile for request in transport.requests] == ["fast"]
    assert [finding.disposition for finding in bundle.findings] == [
        FindingDisposition.FIXED,
        FindingDisposition.UNRESOLVED,
    ]
    assert {finding.reason_code for finding in bundle.findings} == {
        "validated",
        "budget_exhausted",
    }
    assert runtime.run_service is not None
    runtime.run_service.validate_required_events(bundle.run_id)


def test_two_non_overlapping_semantic_repairs_can_share_one_markdown_file(
    tmp_path: Path,
) -> None:
    repo, state_dir = _repo(tmp_path)
    source = repo / "src/demo/api.py"
    document = repo / "docs/api.md"
    source.write_text(
        "def answer() -> int:\n    return 1\n\ndef other() -> int:\n    return 1\n",
        encoding="utf-8",
    )
    document.write_text(
        _document(1) + "\n### `demo.api.other`\n\n"
        "```python\n"
        "def other() -> int: ...\n"
        "```\n\n"
        "Returns `1`.\n",
        encoding="utf-8",
    )
    _git(repo, "add", "src/demo/api.py", "docs/api.md")
    _git(repo, "commit", "-qm", "add same-file semantic baseline")
    source.write_text(
        "def answer() -> int:\n    return 2\n\ndef other() -> int:\n    return 2\n",
        encoding="utf-8",
    )
    transport = ScriptedTransport([_proposal(), _proposal()])
    runtime = AgentRuntime(model_transport=transport)

    bundle = _repair(repo, state_dir, runtime)

    assert bundle.status is RunStatus.FIXED
    assert document.read_text(encoding="utf-8").count("Returns `2`.") == 2
    assert bundle.changes.files == ["docs/api.md"]
    assert len(bundle.findings) == 2
    assert all(finding.disposition is FindingDisposition.FIXED for finding in bundle.findings)
    assert [request.profile for request in transport.requests] == ["fast", "fast"]
    assert runtime.budget_ledger is not None
    assert all(
        runtime.budget_ledger.patch_attempts_for(finding.id) == 1 for finding in bundle.findings
    )
    assert runtime.run_service is not None
    runtime.run_service.validate_required_events(bundle.run_id)


@pytest.mark.parametrize(
    ("duplicate_claim", "truth"),
    [(True, "code_derived"), (False, "unknown")],
)
def test_non_unique_or_unknown_inputs_never_call_model(
    tmp_path: Path,
    duplicate_claim: bool,
    truth: str,
) -> None:
    repo, state_dir = _repo(
        tmp_path,
        duplicate_claim=duplicate_claim,
        truth=truth,
    )
    transport = ScriptedTransport([_proposal()])
    runtime = AgentRuntime(model_transport=transport)

    bundle = _repair(repo, state_dir, runtime)

    assert bundle.status in {RunStatus.DRIFT_FOUND, RunStatus.UNRESOLVED}
    assert transport.requests == []
    assert bundle.usage.model_calls == 0
    assert bundle.changes.applied is False

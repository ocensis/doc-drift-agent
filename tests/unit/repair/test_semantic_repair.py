from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from drift_agent.agent.budget import BudgetLedger
from drift_agent.detectors.semantic import ConstantReturnSemanticDetector
from drift_agent.domain.models import RunBudgets
from drift_agent.model.budgeted import ModelClient
from drift_agent.model.client import ModelClientError
from drift_agent.model.contracts import (
    ModelCallUsage,
    StructuredModelRequest,
    StructuredModelResponse,
)
from drift_agent.providers.markdown_claims import MarkdownClaimProvider
from drift_agent.providers.python_facts import PythonFactProvider
from drift_agent.providers.semantic_claims import SemanticReturnClaimProvider
from drift_agent.repair.semantic import (
    SemanticRepairBoundaryError,
    SemanticRepairCandidate,
    SemanticRepairProposal,
    SemanticRepairSession,
    build_semantic_attempt,
)
from drift_agent.semantic_alignment import align_semantic_returns


def _candidate(tmp_path: Path) -> SemanticRepairCandidate:
    package = tmp_path / "src/demo"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "api.py").write_text(
        "def flag():\n    return False\n",
        encoding="utf-8",
    )
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "api.md").write_text(
        "### `demo.api.flag`\n\n```python\ndef flag(): ...\n```\nReturns `True`.\n",
        encoding="utf-8",
    )
    declarations = MarkdownClaimProvider().collect(tmp_path, ["docs/api.md"])
    claims = SemanticReturnClaimProvider().collect(
        tmp_path,
        ["docs/api.md"],
        declarations,
    )
    facts = PythonFactProvider().collect(
        repo_path=tmp_path,
        source_roots=["src"],
        changed_paths=["src/demo/api.py"],
    )
    aligned = align_semantic_returns(facts, claims)
    assert not aligned.issues
    assert len(aligned.alignments) == 1
    alignment = aligned.alignments[0]
    finding = ConstantReturnSemanticDetector().detect(
        [alignment],
        repository_id="repository",
    )[0]
    return SemanticRepairCandidate.from_alignment(alignment, finding)


class _ScriptedTransport:
    def __init__(self, outputs: list[dict[str, object] | ModelClientError]) -> None:
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
        self.requests.append(request)
        output = self.outputs.pop(0)
        if isinstance(output, ModelClientError):
            raise output
        index = len(self.requests)
        return StructuredModelResponse(
            provider="openrouter",
            profile=request.profile,
            requested_model="provider/model",
            actual_model="provider/model",
            request_id=f"generation-{index}",
            finish_reason="stop",
            output=output,
            usage=ModelCallUsage(
                prompt_tokens=10 + index,
                completion_tokens=2,
                total_tokens=12 + index,
                cost_usd=0.001,
            ),
        )


def _replace(value: str = "False", confidence: str = "high") -> dict[str, object]:
    return {
        "decision": "replace",
        "replacement_text": value,
        "confidence": confidence,
        "rationale": "The required typed value is exact.",
    }


def test_proposal_is_flat_strict_and_abstain_is_consistent() -> None:
    proposal = SemanticRepairProposal.model_validate(_replace())

    assert proposal.decision == "replace"
    assert proposal.confidence == "high"
    with pytest.raises(ValidationError):
        SemanticRepairProposal.model_validate({**_replace(), "command": "pytest"})
    with pytest.raises(ValidationError):
        SemanticRepairProposal.model_validate(
            {
                "decision": "abstain",
                "replacement_text": "False",
                "confidence": "high",
                "rationale": "Unsure.",
            }
        )
    abstain = SemanticRepairProposal(
        decision="abstain",
        replacement_text="",
        confidence="low",
        rationale="The exact literal is uncertain.",
    )
    assert abstain.replacement_text == ""


def test_candidate_requires_the_exact_typed_alignment(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)

    assert candidate.finding_id == candidate.finding.id
    assert candidate.group_id.startswith("group_")
    assert candidate.literal_anchor.exact_text == "True"
    assert candidate.code_fact.normalized_value.model_dump(mode="json") == {
        "type": "bool",
        "value": False,
    }
    assert set(candidate.model_input()) == {
        "protocol_version",
        "finding_kind",
        "claim_mode",
        "documented_value",
        "required_value",
    }
    non_exact = candidate.alignment.model_copy(update={"method": "git_rename"})
    with pytest.raises(SemanticRepairBoundaryError) as raised:
        SemanticRepairCandidate.from_alignment(non_exact, candidate.finding)
    assert raised.value.reason_code == "semantic_alignment_unavailable"
    with pytest.raises(SemanticRepairBoundaryError) as duplicate:
        SemanticRepairCandidate.from_unique_alignment(
            [candidate.alignment, candidate.alignment],
            candidate.finding,
        )
    assert duplicate.value.reason_code == "semantic_alignment_unavailable"


def test_session_retries_invalid_schema_once_across_profiles(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    transport = _ScriptedTransport(
        [
            {**_replace(), "command": "pytest"},
            _replace(),
            {**_replace(), "path": "src/demo/api.py"},
        ]
    )
    ledger = BudgetLedger(RunBudgets(max_model_calls_per_run=4))
    session = SemanticRepairSession(ModelClient(transport, ledger), candidate)

    proposal = session.propose("fast")

    assert proposal.replacement_text == "False"
    assert session.schema_retry_used is True
    assert [request.profile for request in transport.requests] == ["fast", "fast"]
    assert transport.requests[0].system_prompt != transport.requests[1].system_prompt
    assert "docs/api.md" not in transport.requests[0].user_prompt
    with pytest.raises(ModelClientError) as raised:
        session.propose("strong")
    assert raised.value.reason_code == "invalid_structured_output"
    assert [request.profile for request in transport.requests] == [
        "fast",
        "fast",
        "strong",
    ]
    usage = ledger.usage_snapshot()
    assert usage.model_calls == 3
    assert usage.model_calls_by_profile == {"fast": 2, "strong": 1}
    assert usage.input_tokens == 36
    assert usage.output_tokens == 6


def test_session_does_not_retry_non_schema_failures(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    transport = _ScriptedTransport([ModelClientError("timeout")])
    session = SemanticRepairSession(
        ModelClient(transport, BudgetLedger(RunBudgets())),
        candidate,
    )

    with pytest.raises(ModelClientError) as raised:
        session.propose("fast")

    assert raised.value.reason_code == "timeout"
    assert session.schema_retry_used is False
    assert len(transport.requests) == 1


def test_attempt_replaces_only_the_trusted_markdown_literal(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    proposal = SemanticRepairProposal.model_validate(_replace())

    attempt = build_semantic_attempt(tmp_path, candidate, proposal, 1)

    original = (tmp_path / attempt.path).read_bytes()
    rendered = (
        original[: attempt.start_byte]
        + attempt.replacement_text.encode("utf-8")
        + original[attempt.end_byte :]
    )
    assert attempt.path == "docs/api.md"
    assert attempt.target_kind == "markdown"
    assert attempt.expected_text == "True"
    assert attempt.replacement_text == "False"
    assert attempt.group_id == candidate.group_id
    assert attempt.attempt == 1
    assert b"Returns `False`." in rendered
    assert (tmp_path / "src/demo/api.py").read_text(encoding="utf-8") == (
        "def flag():\n    return False\n"
    )


@pytest.mark.parametrize(
    ("proposal", "reason_code"),
    [
        (_replace("True"), "model_output_unsafe"),
        (_replace("False", "low"), "model_low_confidence"),
        (
            {
                "decision": "abstain",
                "replacement_text": "",
                "confidence": "low",
                "rationale": "Cannot prove an exact literal.",
            },
            "model_abstained",
        ),
    ],
)
def test_attempt_rejects_wrong_low_confidence_and_abstain_outputs(
    tmp_path: Path,
    proposal: dict[str, object],
    reason_code: str,
) -> None:
    candidate = _candidate(tmp_path)

    with pytest.raises(SemanticRepairBoundaryError) as raised:
        build_semantic_attempt(
            tmp_path,
            candidate,
            SemanticRepairProposal.model_validate(proposal),
            1,
        )

    assert raised.value.reason_code == reason_code


def test_attempt_rejects_business_code_target_and_stale_anchor(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    proposal = SemanticRepairProposal.model_validate(_replace())
    python_anchor = candidate.literal_anchor.model_copy(update={"path": "src/demo/api.py"})

    with pytest.raises(SemanticRepairBoundaryError) as business_code:
        build_semantic_attempt(
            tmp_path,
            replace(candidate, literal_anchor=python_anchor),
            proposal,
            1,
        )
    assert business_code.value.reason_code == "semantic_patch_unsafe"

    (tmp_path / "docs/api.md").write_text("changed\n", encoding="utf-8")
    with pytest.raises(SemanticRepairBoundaryError) as stale:
        build_semantic_attempt(tmp_path, candidate, proposal, 1)
    assert stale.value.reason_code == "precondition_changed"

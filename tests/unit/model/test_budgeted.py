from __future__ import annotations

from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict

from drift_agent.agent.budget import BudgetLedger
from drift_agent.domain.models import RunBudgets
from drift_agent.model.budgeted import ModelClient
from drift_agent.model.client import ModelClientError
from drift_agent.model.contracts import (
    ModelCallUsage,
    ModelTokenUsage,
    StructuredModelRequest,
    StructuredModelResponse,
)


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    ok: Literal[True]


class _FakeClient:
    def __init__(self, output: dict[str, object]) -> None:
        self.output = output
        self.timeouts: list[float] = []

    def validate_request(self, request: StructuredModelRequest) -> None:
        return None

    def complete_structured(
        self,
        request: StructuredModelRequest,
        *,
        timeout_seconds: float,
    ) -> StructuredModelResponse:
        self.timeouts.append(timeout_seconds)
        return StructuredModelResponse(
            provider="openrouter",
            profile=request.profile,
            requested_model="provider/model",
            actual_model="provider/model",
            request_id="gen-test",
            finish_reason="stop",
            output=self.output,
            usage=ModelCallUsage(
                prompt_tokens=11,
                completion_tokens=2,
                total_tokens=13,
                cost_usd=0.001,
            ),
        )


class _BilledFailureClient:
    def validate_request(self, request: StructuredModelRequest) -> None:
        return None

    def complete_structured(
        self,
        request: StructuredModelRequest,
        *,
        timeout_seconds: float,
    ) -> StructuredModelResponse:
        raise ModelClientError(
            "invalid_structured_output",
            usage=ModelCallUsage(
                prompt_tokens=17,
                completion_tokens=4,
                total_tokens=21,
                cost_usd=0.002,
            ),
        )


class _PartialAccountingFailureClient:
    def validate_request(self, request: StructuredModelRequest) -> None:
        return None

    def complete_structured(
        self,
        request: StructuredModelRequest,
        *,
        timeout_seconds: float,
    ) -> StructuredModelResponse:
        raise ModelClientError(
            "accounting_incomplete",
            usage=ModelTokenUsage(
                prompt_tokens=19,
                completion_tokens=5,
                total_tokens=24,
            ),
        )


def _request() -> StructuredModelRequest:
    return StructuredModelRequest(
        profile="fast",
        schema_name="payload",
        response_schema=_Payload.model_json_schema(),
        system_prompt="Return JSON.",
        user_prompt="Set ok=true.",
        max_output_tokens=16,
    )


def test_gateway_records_actual_usage_before_local_schema_validation() -> None:
    ledger = BudgetLedger(RunBudgets())
    gateway = ModelClient(_FakeClient({"ok": False}), ledger)

    with pytest.raises(ModelClientError) as raised:
        gateway.complete(_request(), _Payload, timeout_seconds=30)

    assert raised.value.reason_code == "invalid_structured_output"
    usage = ledger.usage_snapshot()
    assert usage.model_calls == 1
    assert usage.input_tokens == 11
    assert usage.output_tokens == 2
    assert usage.estimated_cost_usd == pytest.approx(0.001)


def test_gateway_returns_locally_validated_output() -> None:
    ledger = BudgetLedger(RunBudgets())
    client = _FakeClient({"ok": True})

    response = ModelClient(client, ledger).complete(
        _request(),
        _Payload,
        timeout_seconds=30,
    )

    assert response.output.ok is True
    assert response.raw.request_id == "gen-test"
    assert len(client.timeouts) == 1
    assert 0 < client.timeouts[0] <= 30


def test_gateway_rejects_a_schema_mismatch_before_reserving_a_call() -> None:
    ledger = BudgetLedger(RunBudgets())
    request = _request().model_copy(update={"response_schema": {"type": "object"}})

    with pytest.raises(ValueError, match="does not match"):
        ModelClient(_FakeClient({"ok": True}), ledger).complete(
            request,
            _Payload,
            timeout_seconds=30,
        )

    assert ledger.usage_snapshot().model_calls == 0


def test_gateway_records_provider_usage_carried_by_a_billed_failure() -> None:
    ledger = BudgetLedger(RunBudgets())

    with pytest.raises(ModelClientError) as raised:
        ModelClient(_BilledFailureClient(), ledger).complete(
            _request(),
            _Payload,
            timeout_seconds=30,
        )

    assert raised.value.reason_code == "invalid_structured_output"
    usage = ledger.usage_snapshot()
    assert usage.model_calls == 1
    assert usage.input_tokens == 17
    assert usage.output_tokens == 4
    assert usage.estimated_cost_usd == pytest.approx(0.002)


def test_gateway_records_known_tokens_when_provider_cost_is_missing() -> None:
    ledger = BudgetLedger(RunBudgets())

    with pytest.raises(ModelClientError) as raised:
        ModelClient(_PartialAccountingFailureClient(), ledger).complete(
            _request(),
            _Payload,
            timeout_seconds=30,
        )

    assert raised.value.reason_code == "accounting_incomplete"
    usage = ledger.usage_snapshot()
    assert usage.model_calls == 1
    assert usage.input_tokens == 19
    assert usage.output_tokens == 5
    assert usage.estimated_cost_usd == 0.0

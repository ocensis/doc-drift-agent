from __future__ import annotations

import json
import math
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from drift_agent.agent.budget import BudgetLedger, ModelCallReservation
from drift_agent.model.client import ModelClientError, ModelTransport
from drift_agent.model.contracts import (
    ModelCallUsage,
    ModelTokenUsage,
    StructuredModelRequest,
    ValidatedModelResponse,
)

OutputT = TypeVar("OutputT", bound=BaseModel)


def estimate_input_token_upper_bound(request: StructuredModelRequest) -> int:
    """Conservatively reserve one token per UTF-8 byte sent as request input."""

    encoded = json.dumps(
        request.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return len(encoded)


class ModelClient:
    """Public model facade responsible for validation, budget, and actual usage."""

    def __init__(self, transport: ModelTransport, ledger: BudgetLedger) -> None:
        self._transport = transport
        self._ledger = ledger

    def complete(
        self,
        request: StructuredModelRequest,
        response_model: type[OutputT],
        *,
        timeout_seconds: float,
    ) -> ValidatedModelResponse[OutputT]:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        expected_schema = response_model.model_json_schema()
        if request.response_schema != expected_schema:
            raise ValueError("request schema does not match the local response model")
        self._transport.validate_request(request)
        reservation = self._ledger.reserve_model_call(
            request.profile,
            estimate_input_token_upper_bound(request),
        )
        remaining_seconds = self._ledger.remaining_seconds()
        if remaining_seconds <= 0:
            self._ledger.check_deadline()
        try:
            response = self._transport.complete_structured(
                request,
                timeout_seconds=min(timeout_seconds, remaining_seconds),
            )
        except ModelClientError as error:
            if error.usage is not None:
                self._record_usage(reservation, error.usage)
            raise
        usage = response.usage
        self._record_usage(reservation, usage)
        try:
            output = response_model.model_validate(response.output)
        except ValidationError:
            raise ModelClientError("invalid_structured_output") from None
        return ValidatedModelResponse(output=output, raw=response)

    def _record_usage(
        self,
        reservation: ModelCallReservation,
        usage: ModelTokenUsage,
    ) -> None:
        known_cost = usage.cost_usd if isinstance(usage, ModelCallUsage) else 0.0
        self._ledger.record_model_usage(
            reservation,
            input_tokens=usage.prompt_tokens,
            output_tokens=usage.completion_tokens,
            estimated_cost_usd=known_cost,
        )

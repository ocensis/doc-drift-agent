from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from drift_agent.model.contracts import ModelCallUsage, StructuredModelRequest


def _request_data() -> dict[str, object]:
    return {
        "profile": "fast",
        "schema_name": "probe_result",
        "response_schema": {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        },
        "system_prompt": "Return JSON.",
        "user_prompt": "Set ok=true.",
        "max_output_tokens": 32,
    }


def test_structured_request_accepts_a_finite_object_schema() -> None:
    request = StructuredModelRequest.model_validate(_request_data())

    assert request.profile == "fast"
    assert request.response_schema["type"] == "object"


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "array"},
        {"type": "object", "value": math.nan},
    ],
)
def test_structured_request_rejects_non_object_or_non_finite_schemas(
    schema: dict[str, object],
) -> None:
    data = _request_data()
    data["response_schema"] = schema

    with pytest.raises(ValidationError):
        StructuredModelRequest.model_validate(data)


def test_usage_requires_exact_strict_token_accounting() -> None:
    usage = ModelCallUsage(
        prompt_tokens=5,
        completion_tokens=2,
        total_tokens=7,
        cost_usd=0.001,
    )

    assert usage.total_tokens == 7

    with pytest.raises(ValidationError, match="total tokens"):
        ModelCallUsage(
            prompt_tokens=5,
            completion_tokens=2,
            total_tokens=8,
            cost_usd=0.001,
        )

    with pytest.raises(ValidationError):
        ModelCallUsage.model_validate(
            {
                "prompt_tokens": "5",
                "completion_tokens": 2,
                "total_tokens": 7,
                "cost_usd": 0.001,
            }
        )

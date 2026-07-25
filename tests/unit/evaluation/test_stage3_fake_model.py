from __future__ import annotations

import pytest

from drift_agent.evaluation.stage3_fake_model import (
    ScriptedModelTransport,
    model_request_sha256,
)
from drift_agent.evaluation.stage3_models import Stage3ModelStep
from drift_agent.model.contracts import StructuredModelRequest


def _request(profile: str = "fast") -> StructuredModelRequest:
    return StructuredModelRequest.model_validate(
        {
            "profile": profile,
            "schema_name": "stage3_proposal",
            "response_schema": {
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
                "additionalProperties": False,
            },
            "system_prompt": "Return the requested object.",
            "user_prompt": "Set ok to true.",
            "max_output_tokens": 32,
        }
    )


def _step(request: StructuredModelRequest) -> Stage3ModelStep:
    return Stage3ModelStep(
        profile=request.profile,
        request_sha256=model_request_sha256(request),
        output={"ok": True},
        prompt_tokens=11,
        completion_tokens=3,
        cost_nano_usd=1_250,
    )


def test_scripted_transport_returns_exact_offline_usage_and_auditable_call() -> None:
    request = _request()
    transport = ScriptedModelTransport((_step(request),))

    transport.validate_request(request)
    response = transport.complete_structured(request, timeout_seconds=5.0)
    transport.assert_consumed()

    assert response.provider == "stage3_fake"
    assert response.actual_model == "stage3-fake/fast-v1"
    assert response.request_id == "stage3-fake-0001"
    assert response.output == {"ok": True}
    assert response.usage.prompt_tokens == 11
    assert response.usage.completion_tokens == 3
    assert response.usage.total_tokens == 14
    assert response.usage.cost_usd == 0.00000125
    assert transport.calls[0].request_sha256 == model_request_sha256(request)


def test_scripted_transport_rejects_profile_hash_extra_and_missing_calls() -> None:
    fast = _request()
    strong = _request("strong")
    transport = ScriptedModelTransport((_step(fast),))

    with pytest.raises(AssertionError, match="profile mismatch"):
        transport.validate_request(strong)

    changed = fast.model_copy(update={"user_prompt": "A changed prompt."})
    with pytest.raises(AssertionError, match="request hash mismatch"):
        transport.validate_request(changed)

    with pytest.raises(AssertionError, match="not fully consumed"):
        transport.assert_consumed()

    transport.complete_structured(fast, timeout_seconds=1.0)
    with pytest.raises(AssertionError, match="unexpected"):
        transport.complete_structured(fast, timeout_seconds=1.0)


def test_scripted_transport_rejects_non_positive_timeout_before_consumption() -> None:
    request = _request()
    transport = ScriptedModelTransport((_step(request),))

    with pytest.raises(AssertionError, match="non-positive timeout"):
        transport.complete_structured(request, timeout_seconds=0)

    assert transport.calls == ()

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from drift_agent.model.client import ModelClientError
from drift_agent.model.contracts import StructuredModelRequest
from drift_agent.model.openrouter import (
    ModelConfigurationError,
    OpenRouterSettings,
    OpenRouterTransport,
)

_KEY = "test-key-never-log"
_MODEL = "deepseek/deepseek-v4-flash"


def _environment(**overrides: str) -> dict[str, str]:
    environment = {
        "OPENROUTER_API_KEY": _KEY,
        "OPENROUTER_MODEL": _MODEL,
        "OPENROUTER_BASE_URL": "https://openrouter.ai/api/v1",
    }
    environment.update(overrides)
    return environment


def _request() -> StructuredModelRequest:
    return StructuredModelRequest(
        profile="fast",
        schema_name="probe_result",
        response_schema={
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        },
        system_prompt="Return JSON.",
        user_prompt="Set ok=true.",
        max_output_tokens=32,
    )


def _success_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "gen-test-1",
        "model": _MODEL,
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": '{"ok":true}'},
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 3,
            "total_tokens": 13,
            "cost": 0.00001,
        },
    }
    payload.update(overrides)
    return payload


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    **environment_overrides: str,
) -> OpenRouterTransport:
    return OpenRouterTransport(
        OpenRouterSettings.from_environment(_environment(**environment_overrides)),
        transport=httpx.MockTransport(handler),
    )


def test_openrouter_sends_one_strict_non_streaming_request() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_success_payload())

    response = _client(handler).complete_structured(_request(), timeout_seconds=12)

    assert len(seen) == 1
    sent = seen[0]
    assert sent.url == "https://openrouter.ai/api/v1/chat/completions"
    assert sent.headers["authorization"] == f"Bearer {_KEY}"
    assert _KEY not in repr(sent.headers)
    assert sent.headers["x-openrouter-title"] == "doc-code-drift-agent"
    body = json.loads(sent.content)
    assert body["model"] == _MODEL
    assert body["stream"] is False
    assert body["temperature"] == 0
    assert "seed" not in body
    assert body["reasoning"] == {"effort": "none", "exclude": True}
    assert body["provider"] == {
        "require_parameters": True,
        "data_collection": "deny",
    }
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "probe_result",
            "strict": True,
            "schema": _request().response_schema,
        },
    }
    assert response.output == {"ok": True}
    assert response.usage.prompt_tokens == 10
    assert response.usage.completion_tokens == 3
    assert response.usage.cost_usd == pytest.approx(0.00001)


def test_openrouter_pins_provider_for_structured_and_chat_requests() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_success_payload())

    client = _client(handler, OPENROUTER_PROVIDER="streamlake")
    client.complete_structured(_request(), timeout_seconds=12)
    client.complete_chat(
        profile="fast",
        messages=[{"role": "user", "content": "Return ok."}],
        tools=[],
        max_output_tokens=32,
        temperature=0,
        reasoning_effort="none",
        timeout_seconds=12,
    )

    assert len(seen) == 2
    expected = {
        "require_parameters": True,
        "data_collection": "deny",
        "order": ["streamlake"],
        "only": ["streamlake"],
        "allow_fallbacks": False,
    }
    assert [json.loads(request.content)["provider"] for request in seen] == [
        expected,
        expected,
    ]


def test_openrouter_explicitly_allows_provider_data_collection() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_success_payload())

    client = _client(
        handler,
        OPENROUTER_PROVIDER="deepseek",
        OPENROUTER_DATA_COLLECTION="allow",
    )
    client.complete_structured(_request(), timeout_seconds=12)

    assert json.loads(seen[0].content)["provider"] == {
        "require_parameters": True,
        "data_collection": "allow",
        "order": ["deepseek"],
        "only": ["deepseek"],
        "allow_fallbacks": False,
    }


def test_chat_response_preserves_optional_cached_and_reasoning_token_details() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = _success_payload()
        usage = payload["usage"]
        assert isinstance(usage, dict)
        usage["prompt_tokens_details"] = {"cached_tokens": 7}
        usage["completion_tokens_details"] = {"reasoning_tokens": 2}
        return httpx.Response(200, json=payload)

    response = _client(handler).complete_chat(
        profile="fast",
        messages=[{"role": "user", "content": "Return ok."}],
        tools=[],
        max_output_tokens=32,
        temperature=0,
        reasoning_effort="none",
        timeout_seconds=12,
    )

    assert response.usage_details == {"cached_tokens": 7, "reasoning_tokens": 2}


@pytest.mark.parametrize(
    ("reasoning_effort", "expected_reasoning"),
    [
        ("none", {"effort": "none", "exclude": True}),
        ("low", {"effort": "low"}),
        ("high", {"effort": "high"}),
    ],
)
def test_chat_preserves_explicit_reasoning_semantics(
    reasoning_effort: str,
    expected_reasoning: dict[str, str | bool],
) -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=_success_payload())

    _client(handler, OPENROUTER_PROVIDER="streamlake").complete_chat(
        profile="fast",
        messages=[{"role": "user", "content": "Return ok."}],
        tools=[],
        max_output_tokens=32,
        temperature=0,
        reasoning_effort=reasoning_effort,
        timeout_seconds=12,
    )

    assert seen[0]["reasoning"] == expected_reasoning


def test_chat_can_force_one_available_function() -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=_success_payload())

    tools = [
        {
            "type": "function",
            "function": {
                "name": "graph_context",
                "description": "query graph context",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    _client(handler).complete_chat(
        profile="fast",
        messages=[{"role": "user", "content": "Inspect the graph."}],
        tools=tools,
        max_output_tokens=32,
        temperature=0,
        reasoning_effort="none",
        timeout_seconds=12,
        tool_choice={"type": "function", "function": {"name": "graph_context"}},
    )

    assert seen[0]["tool_choice"] == {
        "type": "function",
        "function": {"name": "graph_context"},
    }


def test_chat_can_require_one_of_the_available_functions() -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=_success_payload())

    tools = [
        {
            "type": "function",
            "function": {
                "name": "graph_context",
                "description": "query graph context",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    _client(handler).complete_chat(
        profile="fast",
        messages=[{"role": "user", "content": "Inspect the graph."}],
        tools=tools,
        max_output_tokens=32,
        temperature=0,
        reasoning_effort="none",
        timeout_seconds=12,
        tool_choice="required",
    )

    assert seen[0]["tool_choice"] == "required"


@pytest.mark.parametrize(
    "tool_choice",
    [
        "none",
        {"type": "function", "function": {"name": "missing"}},
        {"type": "function", "function": {"name": "graph_context", "extra": True}},
    ],
)
def test_chat_rejects_invalid_forced_function_choice(tool_choice: object) -> None:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "graph_context",
                "description": "query graph context",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]

    with pytest.raises(ValueError):
        _client(lambda request: httpx.Response(200, json=_success_payload())).complete_chat(
            profile="fast",
            messages=[{"role": "user", "content": "Inspect the graph."}],
            tools=tools,
            max_output_tokens=32,
            temperature=0,
            reasoning_effort="none",
            timeout_seconds=12,
            tool_choice=tool_choice,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("status_code", "reason_code"),
    [
        (400, "request_rejected"),
        (401, "authentication_failed"),
        (402, "payment_required"),
        (429, "rate_limited"),
        (503, "provider_unavailable"),
    ],
)
def test_openrouter_maps_http_failures_without_exposing_provider_messages(
    status_code: int,
    reason_code: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            json={
                "error": {
                    "code": status_code,
                    "message": "contains-a-secret",
                    "metadata": {"error_type": "canonical-provider-error"},
                }
            },
        )

    with pytest.raises(ModelClientError) as raised:
        _client(handler).complete_structured(_request(), timeout_seconds=12)

    assert raised.value.reason_code == reason_code
    assert raised.value.status_code == status_code
    assert raised.value.provider_error_type == "canonical-provider-error"
    assert "contains-a-secret" not in str(raised.value)


@pytest.mark.parametrize("surface", ["structured", "chat"])
@pytest.mark.parametrize(
    ("provider_status_code", "reason_code"),
    [
        (408, "provider_timeout"),
        (429, "rate_limited"),
        (500, "provider_unavailable"),
        (502, "provider_unavailable"),
        (503, "provider_unavailable"),
        (400, "request_rejected"),
    ],
)
def test_openrouter_classifies_http_200_in_band_errors_by_embedded_code(
    surface: str,
    provider_status_code: int,
    reason_code: str,
) -> None:
    payload = {
        "error": {
            "code": provider_status_code,
            "message": "sensitive provider message",
        }
    }
    client = _client(lambda request: httpx.Response(200, json=payload))

    with pytest.raises(ModelClientError) as raised:
        if surface == "structured":
            client.complete_structured(_request(), timeout_seconds=12)
        else:
            client.complete_chat(
                profile="fast",
                messages=[{"role": "user", "content": "Return ok."}],
                tools=[],
                max_output_tokens=32,
                temperature=0,
                reasoning_effort="none",
                timeout_seconds=12,
            )

    assert raised.value.reason_code == reason_code
    assert raised.value.status_code == 200
    assert raised.value.provider_status_code == provider_status_code
    assert raised.value.provider_error_type is None
    assert "sensitive provider message" not in str(raised.value)


@pytest.mark.parametrize("surface", ["structured", "chat"])
def test_openrouter_stable_error_type_overrides_ambiguous_in_band_code(
    surface: str,
) -> None:
    payload = {
        "error": {
            "code": 502,
            "message": "sensitive provider message",
            "metadata": {"error_type": "content_policy_violation"},
        }
    }
    client = _client(lambda request: httpx.Response(200, json=payload))

    with pytest.raises(ModelClientError) as raised:
        if surface == "structured":
            client.complete_structured(_request(), timeout_seconds=12)
        else:
            client.complete_chat(
                profile="fast",
                messages=[{"role": "user", "content": "Return ok."}],
                tools=[],
                max_output_tokens=32,
                temperature=0,
                reasoning_effort="none",
                timeout_seconds=12,
            )

    assert raised.value.reason_code == "content_policy_violation"
    assert raised.value.status_code == 200
    assert raised.value.provider_status_code == 502
    assert raised.value.provider_error_type == "content_policy_violation"
    assert "sensitive provider message" not in str(raised.value)


def test_openrouter_does_not_follow_redirects() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            302,
            headers={"location": "https://example.invalid/steal"},
            json={"error": {"code": 302}},
        )

    with pytest.raises(ModelClientError) as raised:
        _client(handler).complete_structured(_request(), timeout_seconds=12)

    assert calls == 1
    assert raised.value.status_code == 302


def test_openrouter_maps_timeout_without_retrying() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("sensitive transport detail", request=request)

    with pytest.raises(ModelClientError) as raised:
        _client(handler).complete_structured(_request(), timeout_seconds=12)

    assert calls == 1
    assert raised.value.reason_code == "timeout"
    assert "sensitive transport detail" not in str(raised.value)


def test_openrouter_enforces_one_total_wall_clock_timeout() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(200, json=_success_payload())

    client = OpenRouterTransport(
        OpenRouterSettings.from_environment(_environment()),
        transport=httpx.MockTransport(handler),
    )
    started = time.monotonic()

    with pytest.raises(ModelClientError) as raised:
        client.complete_structured(_request(), timeout_seconds=0.02)

    assert raised.value.reason_code == "timeout"
    assert time.monotonic() - started < 0.5


def test_openrouter_stops_reading_an_oversized_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 1_048_577)

    with pytest.raises(ModelClientError) as raised:
        _client(handler).complete_structured(_request(), timeout_seconds=12)

    assert raised.value.reason_code == "response_too_large"


@pytest.mark.parametrize(
    "payload",
    [
        {"error": {"code": "provider_error", "message": "detail"}},
        _success_payload(choices=[]),
        _success_payload(
            choices=[
                {
                    "finish_reason": "length",
                    "message": {"role": "assistant", "content": '{"ok":true}'},
                }
            ]
        ),
        _success_payload(
            choices=[
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "not-json"},
                }
            ]
        ),
        _success_payload(usage=None),
        _success_payload(
            usage={
                "prompt_tokens": 10,
                "completion_tokens": 3,
                "total_tokens": 99,
                "cost": 0.00001,
            }
        ),
    ],
)
def test_openrouter_fails_closed_on_invalid_success_payloads(
    payload: dict[str, object],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with pytest.raises(ModelClientError):
        _client(handler).complete_structured(_request(), timeout_seconds=12)


@pytest.mark.parametrize("surface", ["structured", "chat"])
def test_openrouter_classifies_choice_error_and_preserves_billed_usage(
    surface: str,
) -> None:
    payload = _success_payload(
        choices=[
            {
                "finish_reason": "error",
                "error": {
                    "code": 500,
                    "message": "sensitive detail",
                    "metadata": {"error_type": "provider_generation_error"},
                },
            }
        ]
    )

    client = _client(lambda request: httpx.Response(200, json=payload))

    with pytest.raises(ModelClientError) as raised:
        if surface == "structured":
            client.complete_structured(_request(), timeout_seconds=12)
        else:
            client.complete_chat(
                profile="fast",
                messages=[{"role": "user", "content": "Return ok."}],
                tools=[],
                max_output_tokens=32,
                temperature=0,
                reasoning_effort="none",
                timeout_seconds=12,
            )

    assert raised.value.reason_code == "provider_unavailable"
    assert raised.value.status_code == 200
    assert raised.value.provider_status_code == 500
    assert raised.value.provider_error_type == "provider_generation_error"
    assert raised.value.usage is not None
    assert raised.value.usage.total_tokens == 13
    assert "sensitive detail" not in str(raised.value)


@pytest.mark.parametrize("surface", ["structured", "chat"])
@pytest.mark.parametrize(
    ("provider_status_code", "reason_code"),
    [
        (408, "provider_timeout"),
        (429, "rate_limited"),
        (502, "provider_unavailable"),
    ],
)
def test_openrouter_classifies_choice_error_before_missing_usage(
    surface: str,
    provider_status_code: int,
    reason_code: str,
) -> None:
    payload = _success_payload(
        choices=[
            {
                "finish_reason": "error",
                "error": {
                    "code": provider_status_code,
                    "message": "sensitive detail without accounting",
                },
            }
        ],
        usage=None,
    )
    client = _client(lambda request: httpx.Response(200, json=payload))

    with pytest.raises(ModelClientError) as raised:
        if surface == "structured":
            client.complete_structured(_request(), timeout_seconds=12)
        else:
            client.complete_chat(
                profile="fast",
                messages=[{"role": "user", "content": "Return ok."}],
                tools=[],
                max_output_tokens=32,
                temperature=0,
                reasoning_effort="none",
                timeout_seconds=12,
            )

    assert raised.value.reason_code == reason_code
    assert raised.value.status_code == 200
    assert raised.value.provider_status_code == provider_status_code
    assert raised.value.usage is None
    assert "sensitive detail" not in str(raised.value)


def test_openrouter_accepts_usage_without_cost() -> None:
    # Generic OpenAI-compatible providers (e.g. OpenClaw) omit the OpenRouter
    # "cost" field.  Token accounting is complete, so the call must succeed with
    # cost reported as 0.0 rather than failing as accounting_incomplete.
    payload = _success_payload()
    usage = payload["usage"]
    assert isinstance(usage, dict)
    usage.pop("cost")

    response = _client(lambda request: httpx.Response(200, json=payload)).complete_structured(
        _request(), timeout_seconds=12
    )

    assert response.output == {"ok": True}
    assert response.usage.prompt_tokens == 10
    assert response.usage.completion_tokens == 3
    assert response.usage.total_tokens == 13
    assert response.usage.cost_usd == 0.0


def test_openrouter_unwraps_markdown_fenced_structured_output() -> None:
    # A provider that does not strictly honour response_format=json_schema may
    # wrap the object in a ```json fence; the transport must still parse it.
    payload = _success_payload()
    choices = payload["choices"]
    assert isinstance(choices, list)
    message = choices[0]["message"]
    assert isinstance(message, dict)
    message["content"] = '```json\n{"ok":true}\n```'

    response = _client(lambda request: httpx.Response(200, json=payload)).complete_structured(
        _request(), timeout_seconds=12
    )

    assert response.output == {"ok": True}


def test_settings_are_explicit_mask_the_key_and_validate_official_base_url() -> None:
    settings = OpenRouterSettings.from_environment(_environment())

    assert settings.model_for("fast") == _MODEL
    assert _KEY not in repr(settings)
    assert settings.api_key.get_secret_value() == _KEY

    with pytest.raises(ModelConfigurationError) as missing:
        OpenRouterSettings.from_environment({"OPENROUTER_MODEL": _MODEL})
    assert missing.value.reason_code == "openrouter_api_key_missing"

    key_only = OpenRouterSettings.from_environment({"OPENROUTER_API_KEY": _KEY})
    with pytest.raises(ModelConfigurationError) as missing_model:
        key_only.model_for("fast")
    assert missing_model.value.reason_code == "openrouter_model_missing"
    assert key_only.model_for("fast", "provider/override") == "provider/override"

    with pytest.raises(ModelConfigurationError) as invalid_base:
        OpenRouterSettings.from_environment(
            _environment(OPENROUTER_BASE_URL="https://example.com/api/v1")
        )
    assert invalid_base.value.reason_code == "openrouter_base_url_invalid"

    openclaw = OpenRouterSettings.from_environment(
        _environment(OPENROUTER_BASE_URL="https://openclaw-api.com/v1")
    )
    assert openclaw.base_url == "https://openclaw-api.com/v1"


def test_settings_accept_provider_slug_and_slash_variant() -> None:
    settings = OpenRouterSettings.from_environment(
        _environment(OPENROUTER_PROVIDER="streamlake/variant-1")
    )

    assert settings.provider == "streamlake/variant-1"
    assert settings.provider_preferences() == {
        "require_parameters": True,
        "data_collection": "deny",
        "order": ["streamlake/variant-1"],
        "only": ["streamlake/variant-1"],
        "allow_fallbacks": False,
    }


@pytest.mark.parametrize("data_collection", ["", " ", "ALLOW", "enabled"])
def test_settings_reject_empty_or_invalid_data_collection(
    data_collection: str,
) -> None:
    with pytest.raises(ModelConfigurationError) as raised:
        OpenRouterSettings.from_environment(
            _environment(OPENROUTER_DATA_COLLECTION=data_collection)
        )

    assert raised.value.reason_code == "openrouter_data_collection_invalid"


@pytest.mark.parametrize(
    "provider",
    [
        "StreamLake",
        "streamlake,together",
        "stream lake",
        "https://streamlake.example",
        "streamlake/",
        "streamlake/variant/extra",
        "a" * 129,
    ],
)
def test_settings_reject_invalid_provider_slug(provider: str) -> None:
    with pytest.raises(ModelConfigurationError) as raised:
        OpenRouterSettings.from_environment(_environment(OPENROUTER_PROVIDER=provider))

    assert raised.value.reason_code == "openrouter_provider_invalid"


def test_settings_validate_direct_construction_and_model_copy_again_at_transport() -> None:
    with pytest.raises(ValidationError):
        OpenRouterSettings(
            api_key=SecretStr(_KEY),
            default_model=_MODEL,
            base_url="https://example.invalid/api/v1",
        )

    valid = OpenRouterSettings.from_environment(_environment())
    bypassed = valid.model_copy(update={"base_url": "https://example.invalid/api/v1"})
    with pytest.raises(ModelConfigurationError) as raised:
        OpenRouterTransport(
            bypassed,
            transport=httpx.MockTransport(
                lambda request: pytest.fail("transport must not be reached")
            ),
        )
    assert raised.value.reason_code == "openrouter_base_url_invalid"

    bypassed_provider = valid.model_copy(update={"provider": "StreamLake"})
    with pytest.raises(ModelConfigurationError) as invalid_provider:
        OpenRouterTransport(
            bypassed_provider,
            transport=httpx.MockTransport(
                lambda request: pytest.fail("transport must not be reached")
            ),
        )
    assert invalid_provider.value.reason_code == "openrouter_provider_invalid"

    bypassed_data_collection = valid.model_copy(update={"data_collection": "maybe"})
    with pytest.raises(ModelConfigurationError) as invalid_data_collection:
        OpenRouterTransport(
            bypassed_data_collection,
            transport=httpx.MockTransport(
                lambda request: pytest.fail("transport must not be reached")
            ),
        )
    assert (
        invalid_data_collection.value.reason_code
        == "openrouter_data_collection_invalid"
    )


def test_model_override_is_validated_before_transport() -> None:
    with pytest.raises(ModelConfigurationError) as raised:
        OpenRouterTransport(
            OpenRouterSettings.from_environment(_environment()),
            model_override="https://example.invalid/model",
            transport=httpx.MockTransport(
                lambda request: pytest.fail("transport must not be reached")
            ),
        )

    assert raised.value.reason_code == "openrouter_model_invalid"

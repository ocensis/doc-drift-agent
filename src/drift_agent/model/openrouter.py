from __future__ import annotations

import asyncio
import json
import math
import os
import re
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Self, TypeAlias, cast
from urllib.parse import urlsplit

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    model_validator,
)

from drift_agent.model.client import ModelClientError
from drift_agent.model.contracts import (
    ModelCallUsage,
    ModelProfile,
    ModelTokenUsage,
    StructuredModelRequest,
    StructuredModelResponse,
)

# Accepts an OpenRouter-style "provider/model" id or a bare OpenAI-compatible
# id (e.g. "claude-sonnet-5", "gpt-4o"); the provider prefix is optional.
_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._:-]*)?$")
_PROVIDER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*(/[a-z0-9][a-z0-9._-]*)?$")
_MAX_PROVIDER_LENGTH = 128
_MAX_RESPONSE_BYTES = 1_048_576
# Explicit, process-owned allowlist of OpenAI-compatible chat-completions
# providers.  base_url may only resolve to one of these (host, path) pairs; the
# transport posts to "{base_url}/chat/completions" with bearer auth.  Target
# repository content never controls this — widen it only by deliberate edit.
_ALLOWED_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("openrouter.ai", "/api/v1"),
    ("openclaw-api.com", "/v1"),
)
_CONFIGURATION_REASON_CODES = frozenset(
    {
        "openrouter_api_key_invalid",
        "openrouter_base_url_invalid",
        "openrouter_data_collection_invalid",
        "openrouter_model_invalid",
        "openrouter_provider_invalid",
    }
)


class ModelConfigurationError(ValueError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class OpenRouterSettings(BaseModel):
    """Process-owned settings; target repository content never controls these."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    api_key: SecretStr
    default_model: str | None = Field(default=None, min_length=3, max_length=256)
    fast_model: str | None = Field(default=None, min_length=3, max_length=256)
    strong_model: str | None = Field(default=None, min_length=3, max_length=256)
    provider: str | None = None
    data_collection: Literal["allow", "deny"] = "deny"
    base_url: str = Field(
        default="https://openrouter.ai/api/v1",
        min_length=1,
        max_length=256,
    )
    timeout_seconds: float = Field(default=120.0, gt=0, le=300, allow_inf_nan=False)
    # Minimum interval between any two requests from this process; rides under
    # edge rate-shaping (Cloudflare 1010 bursts at the OpenClaw gateway).
    pace_seconds: float = Field(default=0.0, ge=0, le=30, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_safe_values(self) -> Self:
        self._assert_safe_values()
        return self

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> OpenRouterSettings:
        values = os.environ if environment is None else environment
        api_key = values.get("OPENROUTER_API_KEY", "").strip()
        default_model = values.get("OPENROUTER_MODEL", "").strip() or None
        if not api_key:
            raise ModelConfigurationError("openrouter_api_key_missing")
        raw_timeout = values.get("OPENROUTER_TIMEOUT_SECONDS", "120").strip()
        try:
            timeout_seconds = float(raw_timeout)
        except ValueError:
            raise ModelConfigurationError("openrouter_timeout_invalid") from None
        raw_pace = values.get("OPENROUTER_PACE_SECONDS", "0").strip() or "0"
        try:
            pace_seconds = float(raw_pace)
        except ValueError:
            raise ModelConfigurationError("openrouter_pace_invalid") from None
        data_collection: Literal["allow", "deny"]
        if "OPENROUTER_DATA_COLLECTION" in values:
            raw_data_collection = values.get(
                "OPENROUTER_DATA_COLLECTION", ""
            ).strip()
            if raw_data_collection not in {"allow", "deny"}:
                raise ModelConfigurationError(
                    "openrouter_data_collection_invalid"
                ) from None
            data_collection = cast(
                Literal["allow", "deny"],
                raw_data_collection,
            )
        else:
            data_collection = "deny"
        try:
            settings = cls(
                api_key=SecretStr(api_key),
                default_model=default_model,
                fast_model=(values.get("OPENROUTER_FAST_MODEL") or "").strip() or None,
                strong_model=(values.get("OPENROUTER_STRONG_MODEL") or "").strip() or None,
                provider=(values.get("OPENROUTER_PROVIDER") or "").strip() or None,
                data_collection=data_collection,
                base_url=values.get(
                    "OPENROUTER_BASE_URL",
                    "https://openrouter.ai/api/v1",
                ).strip(),
                timeout_seconds=timeout_seconds,
                pace_seconds=pace_seconds,
            )
        except ValidationError as error:
            for detail in error.errors(include_input=False):
                message = str(detail.get("msg", ""))
                for reason_code in _CONFIGURATION_REASON_CODES:
                    if reason_code in message:
                        raise ModelConfigurationError(reason_code) from None
            raise ModelConfigurationError("openrouter_configuration_invalid") from None
        return settings

    def _assert_safe_values(self) -> None:
        api_key: object = self.api_key
        if not isinstance(api_key, SecretStr):
            raise ModelConfigurationError("openrouter_api_key_invalid")
        key = api_key.get_secret_value()
        if len(key) > 4_096 or any(not 33 <= ord(character) <= 126 for character in key):
            raise ModelConfigurationError("openrouter_api_key_invalid")
        models: tuple[object, ...] = (
            self.default_model,
            self.fast_model,
            self.strong_model,
        )
        for model in models:
            if model is not None and (
                not isinstance(model, str) or not _MODEL_PATTERN.fullmatch(model)
            ):
                raise ModelConfigurationError("openrouter_model_invalid")
        provider: object = self.provider
        if provider is not None and (
            not isinstance(provider, str)
            or len(provider) > _MAX_PROVIDER_LENGTH
            or not _PROVIDER_PATTERN.fullmatch(provider)
        ):
            raise ModelConfigurationError("openrouter_provider_invalid")
        data_collection: object = self.data_collection
        if data_collection not in {"allow", "deny"}:
            raise ModelConfigurationError("openrouter_data_collection_invalid")
        base_url: object = self.base_url
        if not isinstance(base_url, str):
            raise ModelConfigurationError("openrouter_base_url_invalid")
        parsed = urlsplit(base_url)
        try:
            port = parsed.port
        except ValueError:
            raise ModelConfigurationError("openrouter_base_url_invalid") from None
        endpoint = (parsed.hostname, parsed.path.rstrip("/"))
        if (
            parsed.scheme != "https"
            or port is not None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or endpoint not in _ALLOWED_ENDPOINTS
        ):
            raise ModelConfigurationError("openrouter_base_url_invalid")
        timeout: object = self.timeout_seconds
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or not 0 < float(timeout) <= 300
        ):
            raise ModelConfigurationError("openrouter_timeout_invalid")

    def model_for(self, profile: ModelProfile, override: str | None = None) -> str:
        selected = override or (self.fast_model if profile == "fast" else self.strong_model)
        selected = selected or self.default_model
        if selected is None:
            raise ModelConfigurationError("openrouter_model_missing")
        if not _MODEL_PATTERN.fullmatch(selected):
            raise ModelConfigurationError("openrouter_model_invalid")
        return selected

    def provider_preferences(self) -> dict[str, object]:
        """Return the exact non-secret provider routing object sent to the API.

        Returns:
            dict[str, object]: Provider preferences included in each request.
        """

        self._assert_safe_values()
        return _provider_preferences(self.provider, self.data_collection)


def _object(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {str(key): item for key, item in value.items()}


def _error_type(error: dict[str, Any] | None) -> str | None:
    if error is None:
        return None
    metadata = _object(error.get("metadata"))
    if metadata is None:
        return None
    value = metadata.get("error_type")
    if isinstance(value, str) and value:
        return value[:64]
    return None


def _provider_status_code(error: dict[str, Any] | None) -> int | None:
    """Return only a genuine numeric HTTP-style code from an error object."""

    if error is None:
        return None
    value = error.get("code")
    if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599:
        return value
    return None


def _provider_preferences(
    provider: str | None,
    data_collection: Literal["allow", "deny"],
) -> dict[str, object]:
    preferences: dict[str, object] = {
        "require_parameters": True,
        "data_collection": data_collection,
    }
    if provider is not None:
        preferences.update(
            {
                "order": [provider],
                "only": [provider],
                "allow_fallbacks": False,
            }
        )
    return preferences


def _model_usage(payload: dict[str, Any]) -> ModelCallUsage | ModelTokenUsage | None:
    usage_payload = _object(payload.get("usage"))
    if usage_payload is None:
        return None
    try:
        tokens = ModelTokenUsage.model_validate(
            {
                "prompt_tokens": usage_payload.get("prompt_tokens"),
                "completion_tokens": usage_payload.get("completion_tokens"),
                "total_tokens": usage_payload.get("total_tokens"),
            }
        )
    except ValidationError:
        return None
    try:
        return ModelCallUsage.model_validate(
            {
                "prompt_tokens": tokens.prompt_tokens,
                "completion_tokens": tokens.completion_tokens,
                "total_tokens": tokens.total_tokens,
                # OpenRouter reports per-call "cost"; generic OpenAI-compatible
                # providers (e.g. OpenClaw) omit it.  Token accounting is what
                # the budget ledger needs; treat an unreported cost as 0.0
                # rather than downgrading the whole call to "accounting_incomplete".
                "cost_usd": usage_payload.get("cost") or 0.0,
            }
        )
    except ValidationError:
        return tokens


def _chat_usage_details(payload: dict[str, Any]) -> dict[str, int]:
    """Return optional provider token detail without weakening core accounting."""

    usage_payload = _object(payload.get("usage"))
    if usage_payload is None:
        return {}
    prompt_details = _object(usage_payload.get("prompt_tokens_details")) or {}
    completion_details = _object(usage_payload.get("completion_tokens_details")) or {}
    candidates = {
        "cached_tokens": (
            prompt_details.get("cached_tokens")
            if prompt_details.get("cached_tokens") is not None
            else usage_payload.get("prompt_cache_hit_tokens")
        ),
        "reasoning_tokens": completion_details.get("reasoning_tokens"),
    }
    details: dict[str, int] = {}
    for name, value in candidates.items():
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            details[name] = value
    return details


def _status_reason(status_code: int) -> str:
    return {
        400: "request_rejected",
        401: "authentication_failed",
        402: "payment_required",
        403: "request_forbidden",
        408: "provider_timeout",
        409: "provider_conflict",
        422: "request_rejected",
        429: "rate_limited",
    }.get(status_code, "provider_unavailable" if status_code >= 500 else "http_error")


_TYPED_ERROR_REASONS = {
    # Stable OpenRouter error_type vocabulary. These mappings deliberately use
    # the episode retry policy's existing sanitized reason codes.
    "authentication": "authentication_failed",
    "permission_denied": "request_forbidden",
    "payment_required": "payment_required",
    "rate_limit_exceeded": "rate_limited",
    "provider_overloaded": "provider_unavailable",
    "provider_unavailable": "provider_unavailable",
    "server": "provider_unavailable",
    "timeout": "provider_timeout",
    "context_length_exceeded": "request_rejected",
    "max_tokens_exceeded": "request_rejected",
    "token_limit_exceeded": "request_rejected",
    "string_too_long": "request_rejected",
    "invalid_request": "request_rejected",
    "invalid_prompt": "request_rejected",
    "not_found": "request_rejected",
    "precondition_failed": "request_rejected",
    "payload_too_large": "request_rejected",
    "unprocessable": "request_rejected",
    "content_policy_violation": "content_policy_violation",
    "refusal": "content_policy_violation",
    "invalid_image": "request_rejected",
    "image_too_large": "request_rejected",
    "image_too_small": "request_rejected",
    "unsupported_image_format": "request_rejected",
    "image_not_found": "request_rejected",
    "image_download_failed": "request_rejected",
}


def _provider_error_reason(
    error: dict[str, Any] | None,
    *,
    fallback_status_code: int | None = None,
) -> str:
    """Classify an OpenRouter error without ever inspecting its message."""

    error_type = _error_type(error)
    if error_type is not None:
        typed_reason = _TYPED_ERROR_REASONS.get(error_type)
        if typed_reason is not None:
            return typed_reason
    embedded_status = _provider_status_code(error)
    if embedded_status is not None:
        return _status_reason(embedded_status)
    if fallback_status_code is not None:
        return _status_reason(fallback_status_code)
    return "provider_response_error"


def _strip_code_fence(text: str) -> str:
    """Unwrap a ```json ... ``` fence if the provider wrapped structured output.

    OpenRouter honours response_format=json_schema and returns bare JSON, but a
    generic OpenAI-compatible provider may return the object inside a markdown
    fence.  Bare JSON is returned unchanged, so this is a no-op for OpenRouter.
    """

    stripped = text.strip()
    if not stripped.startswith("```"):
        return text
    newline = stripped.find("\n")
    if newline == -1:
        return text
    body = stripped[newline + 1 :].rstrip()
    fence = body.rfind("```")
    if fence != -1:
        body = body[:fence]
    return body


_ALLOWED_EFFORTS = frozenset({"none", "low", "medium", "high"})
_CHAT_FINISH_REASONS = frozenset({"stop", "tool_calls"})
_MAX_CHAT_OUTPUT_TOKENS = 64_000

ChatToolChoice: TypeAlias = str | dict[str, Any]


def _validated_chat_tool_choice(
    tool_choice: ChatToolChoice | None,
    tools: list[dict[str, Any]],
) -> ChatToolChoice | None:
    """Validate an optional OpenAI-compatible chat tool selection."""

    if not tools:
        if tool_choice is not None:
            raise ValueError("tool_choice requires at least one tool")
        return None
    if tool_choice is None or tool_choice == "auto":
        return "auto"
    if tool_choice == "required":
        return "required"
    if not isinstance(tool_choice, dict):
        raise ValueError(
            "tool_choice must be auto, required, or a forced function object"
        )
    function = _object(tool_choice.get("function"))
    if set(tool_choice) != {"type", "function"} or tool_choice.get("type") != "function":
        raise ValueError("forced tool_choice must select one function")
    if function is None or set(function) != {"name"}:
        raise ValueError("forced tool_choice must contain only function.name")
    name = function.get("name")
    available = {
        candidate
        for item in tools
        if (wrapper := _object(item.get("function"))) is not None
        and isinstance(candidate := wrapper.get("name"), str)
    }
    if not isinstance(name, str) or name not in available:
        raise ValueError("forced tool_choice names an unavailable function")
    return {"type": "function", "function": {"name": name}}


@dataclass(frozen=True, slots=True)
class ChatTurnResult:
    """One assistant turn from a bounded chat-with-tools completion."""

    message: dict[str, Any]
    finish_reason: str
    usage: ModelCallUsage
    actual_model: str
    request_id: str
    usage_details: dict[str, int] = field(default_factory=dict)
    # Some OpenAI-compatible APIs report complete token accounting but no
    # monetary cost. ``usage.cost_usd`` remains the numeric value required by
    # the shared budget contract; this bit prevents that internal zero
    # placeholder from being presented as a measured free call.
    cost_usd_known: bool = True


@dataclass(frozen=True, slots=True)
class _PreparedChatRequest:
    """Provider request assembled by the shared and one-shot transports."""

    endpoint: str
    headers: httpx.Headers
    payload: dict[str, object]


_PACE_LOCK = threading.Lock()
_LAST_REQUEST_MONOTONIC = 0.0


def _pace(seconds: float) -> None:
    """Enforce a process-wide minimum interval between gateway requests."""

    global _LAST_REQUEST_MONOTONIC
    if seconds <= 0:
        return
    with _PACE_LOCK:
        wait = _LAST_REQUEST_MONOTONIC + seconds - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _LAST_REQUEST_MONOTONIC = time.monotonic()


def _assert_sync_context() -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise ModelClientError("sync_client_in_async_context")


def _prepare_chat_request(
    settings: OpenRouterSettings,
    *,
    model_override: str | None,
    profile: ModelProfile,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    max_output_tokens: int,
    temperature: float,
    reasoning_effort: str,
    timeout_seconds: float,
    tool_choice: ChatToolChoice | None,
) -> _PreparedChatRequest:
    """Validate and assemble one chat request without performing any I/O."""

    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be finite and positive")
    if not messages:
        raise ValueError("messages must be non-empty")
    if not 1 <= max_output_tokens <= _MAX_CHAT_OUTPUT_TOKENS:
        raise ValueError("max_output_tokens out of range")
    if not math.isfinite(temperature) or not 0 <= temperature <= 2:
        raise ValueError("temperature out of range")
    if reasoning_effort not in _ALLOWED_EFFORTS:
        raise ValueError("reasoning_effort must be one of none/low/medium/high")
    settings._assert_safe_values()
    _assert_sync_context()
    model = settings.model_for(profile, model_override)
    payload: dict[str, object] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "temperature": temperature,
        "max_tokens": max_output_tokens,
        "reasoning": (
            {"effort": "none", "exclude": True}
            if reasoning_effort == "none"
            else {"effort": reasoning_effort}
        ),
        "provider": settings.provider_preferences(),
    }
    selected_tool_choice = _validated_chat_tool_choice(tool_choice, tools)
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = selected_tool_choice
    return _PreparedChatRequest(
        endpoint=f"{settings.base_url.rstrip('/')}/chat/completions",
        headers=httpx.Headers(
            {
                "Authorization": f"Bearer {settings.api_key.get_secret_value()}",
                "Content-Type": "application/json",
                "X-OpenRouter-Title": "doc-code-drift-agent",
            }
        ),
        payload=payload,
    )


def _parse_chat_response(
    *,
    status_code: int,
    response_bytes: bytes,
    missing_cost_is_unknown: bool = False,
) -> ChatTurnResult:
    """Parse one chat response with the canonical accounting/error semantics."""

    try:
        raw_payload = json.loads(response_bytes)
    except ValueError:
        raw_payload = None
    response_payload = _object(raw_payload)
    response_error = (
        None if response_payload is None else _object(response_payload.get("error"))
    )
    usage_payload = (
        None if response_payload is None else _object(response_payload.get("usage"))
    )
    cost_usd_known = not missing_cost_is_unknown or (
        usage_payload is not None
        and isinstance(usage_payload.get("cost"), (int, float))
        and not isinstance(usage_payload.get("cost"), bool)
    )
    if not 200 <= status_code < 300:
        raise ModelClientError(
            _provider_error_reason(response_error, fallback_status_code=status_code),
            status_code=status_code,
            provider_status_code=_provider_status_code(response_error),
            provider_error_type=_error_type(response_error),
            usage=(None if response_payload is None else _model_usage(response_payload)),
            usage_cost_usd_known=cost_usd_known,
        )
    if response_payload is None:
        raise ModelClientError("invalid_response", status_code=status_code)
    if response_payload.get("error") is not None:
        raise ModelClientError(
            _provider_error_reason(response_error),
            status_code=status_code,
            provider_status_code=_provider_status_code(response_error),
            provider_error_type=_error_type(response_error),
            usage=_model_usage(response_payload),
            usage_cost_usd_known=cost_usd_known,
        )
    observed_usage = _model_usage(response_payload)
    choices = response_payload.get("choices")
    choice = (
        _object(choices[0])
        if isinstance(choices, list) and len(choices) == 1
        else None
    )
    choice_error = None if choice is None else _object(choice.get("error"))
    if choice_error is not None:
        # OpenRouter may report a provider generation failure inside a nominal
        # HTTP 200 choice and omit usage. Classify that provider failure first;
        # attach whatever accounting was usable without letting missing usage
        # hide the retryable 408/429/5xx signal.
        raise ModelClientError(
            _provider_error_reason(choice_error),
            status_code=status_code,
            provider_status_code=_provider_status_code(choice_error),
            provider_error_type=_error_type(choice_error),
            usage=observed_usage,
            usage_cost_usd_known=cost_usd_known,
        )
    if observed_usage is None or not isinstance(observed_usage, ModelCallUsage):
        raise ModelClientError(
            "accounting_incomplete",
            usage=observed_usage,
            usage_cost_usd_known=cost_usd_known,
        )
    usage = observed_usage
    request_id = response_payload.get("id")
    actual_model = response_payload.get("model")
    if not isinstance(request_id, str) or not isinstance(actual_model, str):
        raise ModelClientError(
            "invalid_response",
            usage=usage,
            usage_cost_usd_known=cost_usd_known,
        )
    if choice is None:
        raise ModelClientError(
            "invalid_response",
            usage=usage,
            usage_cost_usd_known=cost_usd_known,
        )
    finish_reason = choice.get("finish_reason")
    if not isinstance(finish_reason, str) or finish_reason not in _CHAT_FINISH_REASONS:
        raise ModelClientError(
            "incomplete_response",
            usage=usage,
            usage_cost_usd_known=cost_usd_known,
        )
    message = _object(choice.get("message"))
    if message is None:
        raise ModelClientError(
            "invalid_response",
            usage=usage,
            usage_cost_usd_known=cost_usd_known,
        )
    return ChatTurnResult(
        message=message,
        finish_reason=finish_reason,
        usage=usage,
        actual_model=actual_model,
        request_id=request_id,
        usage_details=_chat_usage_details(response_payload),
        cost_usd_known=cost_usd_known,
    )


async def _post_with_client(
    client: httpx.AsyncClient,
    *,
    endpoint: str,
    headers: httpx.Headers,
    payload: dict[str, object],
    timeout_seconds: float,
) -> tuple[int, bytes]:
    """POST through an already-owned client while bounding body size and time."""

    try:
        async with asyncio.timeout(timeout_seconds):
            async with client.stream(
                "POST",
                endpoint,
                headers=headers,
                json=payload,
                timeout=httpx.Timeout(timeout_seconds),
            ) as response:
                status_code = response.status_code
                response_bytes = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(response_bytes) + len(chunk) > _MAX_RESPONSE_BYTES:
                        raise ModelClientError(
                            "response_too_large",
                            status_code=status_code,
                        )
                    response_bytes.extend(chunk)
    except TimeoutError:
        raise ModelClientError("timeout") from None
    except httpx.TimeoutException:
        raise ModelClientError("timeout") from None
    except httpx.RequestError:
        raise ModelClientError("transport_error") from None
    return status_code, bytes(response_bytes)


class OpenRouterTransport:
    """Fail-closed OpenRouter adapter with no redirects, proxies, or retries."""

    def __init__(
        self,
        settings: OpenRouterSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        model_override: str | None = None,
    ) -> None:
        settings._assert_safe_values()
        if model_override is not None and not _MODEL_PATTERN.fullmatch(model_override):
            raise ModelConfigurationError("openrouter_model_invalid")
        self._settings = settings
        self._transport = transport
        self._model_override = model_override

    def _assert_sync_context(self) -> None:
        _assert_sync_context()

    def validate_request(self, request: StructuredModelRequest) -> None:
        self._settings._assert_safe_values()
        self._settings.model_for(request.profile, self._model_override)
        self._assert_sync_context()

    def complete_chat(
        self,
        *,
        profile: ModelProfile,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_output_tokens: int,
        temperature: float,
        reasoning_effort: str,
        timeout_seconds: float,
        tool_choice: ChatToolChoice | None = None,
    ) -> ChatTurnResult:
        """One bounded, non-streaming chat turn that may request tool calls.

        The episode runner owns the loop; this method sends exactly one POST and
        returns the raw assistant message so reasoning_details / tool_calls can
        be echoed back verbatim on the next turn.
        """

        request = _prepare_chat_request(
            self._settings,
            model_override=self._model_override,
            profile=profile,
            messages=messages,
            tools=tools,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            timeout_seconds=timeout_seconds,
            tool_choice=tool_choice,
        )
        _pace(self._settings.pace_seconds)
        status_code, response_bytes = asyncio.run(
            self._post(
                endpoint=request.endpoint,
                headers=request.headers,
                payload=request.payload,
                timeout_seconds=timeout_seconds,
            )
        )
        return _parse_chat_response(
            status_code=status_code,
            response_bytes=response_bytes,
        )

    def complete_structured(
        self,
        request: StructuredModelRequest,
        *,
        timeout_seconds: float,
    ) -> StructuredModelResponse:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        self.validate_request(request)
        model = self._settings.model_for(request.profile, self._model_override)
        payload: dict[str, object] = {
            "model": model,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
            "stream": False,
            "temperature": 0,
            "max_tokens": request.max_output_tokens,
            "reasoning": {"effort": "none", "exclude": True},
            "provider": self._settings.provider_preferences(),
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": request.schema_name,
                    "strict": True,
                    "schema": request.response_schema,
                },
            },
        }
        endpoint = f"{self._settings.base_url.rstrip('/')}/chat/completions"
        headers = httpx.Headers(
            {
                "Authorization": (f"Bearer {self._settings.api_key.get_secret_value()}"),
                "Content-Type": "application/json",
                "X-OpenRouter-Title": "doc-code-drift-agent",
            }
        )
        _pace(self._settings.pace_seconds)
        status_code, response_bytes = asyncio.run(
            self._post(
                endpoint=endpoint,
                headers=headers,
                payload=payload,
                timeout_seconds=timeout_seconds,
            )
        )
        try:
            raw_payload = json.loads(response_bytes)
        except ValueError:
            raw_payload = None
        response_payload = _object(raw_payload)
        response_error = (
            None if response_payload is None else _object(response_payload.get("error"))
        )
        if not 200 <= status_code < 300:
            raise ModelClientError(
                _provider_error_reason(response_error, fallback_status_code=status_code),
                status_code=status_code,
                provider_status_code=_provider_status_code(response_error),
                provider_error_type=_error_type(response_error),
                usage=(None if response_payload is None else _model_usage(response_payload)),
            )
        if response_payload is None:
            raise ModelClientError("invalid_response", status_code=status_code)
        if response_payload.get("error") is not None:
            raise ModelClientError(
                _provider_error_reason(response_error),
                status_code=status_code,
                provider_status_code=_provider_status_code(response_error),
                provider_error_type=_error_type(response_error),
                usage=_model_usage(response_payload),
            )
        return self._parse_success(
            response_payload,
            request=request,
            requested_model=model,
            status_code=status_code,
        )

    async def _post(
        self,
        *,
        endpoint: str,
        headers: httpx.Headers,
        payload: dict[str, object],
        timeout_seconds: float,
    ) -> tuple[int, bytes]:
        async with httpx.AsyncClient(
            transport=self._transport,
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(timeout_seconds),
        ) as client:
            return await _post_with_client(
                client,
                endpoint=endpoint,
                headers=headers,
                payload=payload,
                timeout_seconds=timeout_seconds,
            )

    @staticmethod
    def _parse_success(
        payload: dict[str, Any],
        *,
        request: StructuredModelRequest,
        requested_model: str,
        status_code: int,
    ) -> StructuredModelResponse:
        observed_usage = _model_usage(payload)
        choices = payload.get("choices")
        choice = (
            _object(choices[0])
            if isinstance(choices, list) and len(choices) == 1
            else None
        )
        choice_error = None if choice is None else _object(choice.get("error"))
        if choice_error is not None:
            raise ModelClientError(
                _provider_error_reason(choice_error),
                status_code=status_code,
                provider_status_code=_provider_status_code(choice_error),
                provider_error_type=_error_type(choice_error),
                usage=observed_usage,
            )
        if observed_usage is None:
            raise ModelClientError("accounting_incomplete")
        if not isinstance(observed_usage, ModelCallUsage):
            raise ModelClientError(
                "accounting_incomplete",
                usage=observed_usage,
            )
        usage = observed_usage
        request_id = payload.get("id")
        actual_model = payload.get("model")
        if not isinstance(request_id, str) or not isinstance(actual_model, str):
            raise ModelClientError("invalid_response", usage=usage)
        if choice is None:
            raise ModelClientError("invalid_response", usage=usage)
        if choice.get("finish_reason") != "stop":
            raise ModelClientError("incomplete_response", usage=usage)
        message = _object(choice.get("message"))
        if message is None or not isinstance(message.get("content"), str):
            raise ModelClientError("invalid_response", usage=usage)
        try:
            output = _object(json.loads(_strip_code_fence(cast(str, message["content"]))))
        except (TypeError, ValueError):
            raise ModelClientError(
                "invalid_structured_output",
                usage=usage,
            ) from None
        if output is None:
            raise ModelClientError("invalid_structured_output", usage=usage)
        try:
            return StructuredModelResponse(
                provider="openrouter",
                profile=request.profile,
                requested_model=requested_model,
                actual_model=actual_model,
                request_id=request_id,
                finish_reason="stop",
                output=output,
                usage=usage,
            )
        except ValidationError:
            raise ModelClientError("invalid_response", usage=usage) from None

"""Optional, failure-isolated Langfuse tracing helpers.

The helpers in this module are intentionally synchronous and safe to call from
many Agent threads.  OpenTelemetry's implicit current-span context does not cross
``threading.Thread`` boundaries, so callers may carry an immutable
``ObservationContext`` and explicitly parent every observation.  The module is a
strict no-op when Langfuse is not configured or any SDK operation fails.
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Iterator, Mapping, Sequence, Set
from contextlib import contextmanager
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Literal, TypeAlias

ObservationType: TypeAlias = Literal[
    "span",
    "agent",
    "tool",
    "chain",
    "retriever",
    "evaluator",
    "guardrail",
    "generation",
    "embedding",
]

_client: Any | None = None
_checked = False
_client_lock = threading.Lock()

_MAX_FIELD_CHARS = 4_000
_MAX_KEY_CHARS = 256
_MAX_COLLECTION_ITEMS = 50
_MAX_PAYLOAD_DEPTH = 6
_REDACTED = "[REDACTED]"
_CYCLE = "[TRUNCATED: cycle]"
_DEPTH = "[TRUNCATED: max depth]"
_JSON_SECRET = re.compile(
    r'(?i)("(?:access[_-]?token|api[_-]?key|authorization|credential|lease[_-]?token|password|'
    r'private[_-]?key|refresh[_-]?token|secret(?:[_-]?key)?|token)"\s*:\s*)'
    r'"(?:\\.|[^"\\])*"'
)
_BEARER_SECRET = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")


@dataclass(frozen=True, slots=True)
class ObservationContext:
    """Portable Langfuse parent identity safe to pass across threads/loops."""

    trace_id: str
    parent_span_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.trace_id, str) or not self.trace_id.strip():
            raise ValueError("trace_id must be a non-empty string")
        if self.parent_span_id is not None and (
            not isinstance(self.parent_span_id, str)
            or not self.parent_span_id.strip()
        ):
            raise ValueError("parent_span_id must be a non-empty string or None")


def _get_client() -> Any | None:
    """Return the process client, initializing it exactly once across threads."""

    global _client, _checked
    if _checked:
        return _client
    with _client_lock:
        if _checked:
            return _client
        candidate: Any | None = None
        if configured():
            try:
                from langfuse import Langfuse

                candidate = Langfuse(
                    base_url=os.environ.get("LANGFUSE_BASE_URL", "").strip()
                    or None,
                )
            except Exception:
                candidate = None
        _client = candidate
        _checked = True
        return _client


def _reset_for_tests() -> None:
    global _client, _checked
    with _client_lock:
        _client = None
        _checked = False


def enabled() -> bool:
    return _get_client() is not None


def configured() -> bool:
    """Return whether both required credentials are present in the environment."""

    return bool(
        os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip()
        and os.environ.get("LANGFUSE_SECRET_KEY", "").strip()
    )


def _clip_text(value: str, limit: int = _MAX_FIELD_CHARS) -> str:
    value = _JSON_SECRET.sub(r'\1"[REDACTED]"', value)
    value = _BEARER_SECRET.sub("Bearer [REDACTED]", value)
    for environment_name in (
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
    ):
        secret = os.environ.get(environment_name, "")
        if secret:
            value = value.replace(secret, _REDACTED)
    if len(value) <= limit:
        return value
    return value[:limit] + f"...[+{len(value) - limit} chars]"


def _secret_key(value: str) -> bool:
    compact = "".join(character for character in value.casefold() if character.isalnum())
    exact = {
        "apikey",
        "accesstoken",
        "authorization",
        "credential",
        "credentials",
        "leasetoken",
        "password",
        "privatekey",
        "refreshtoken",
        "secret",
        "secretkey",
        "token",
    }
    suffixes = (
        "apikey",
        "accesstoken",
        "credential",
        "leasetoken",
        "password",
        "privatekey",
        "refreshtoken",
        "secret",
        "secretkey",
    )
    return compact in exact or compact.endswith(suffixes)


def _payload(
    value: Any,
    *,
    depth: int,
    active: set[int],
) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _clip_text(value)
    if isinstance(value, Enum):
        return _payload(value.value, depth=depth, active=active)
    if isinstance(value, Path):
        return _clip_text(str(value))
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{type(value).__name__}:{len(value)} bytes>"
    if depth >= _MAX_PAYLOAD_DEPTH:
        return _DEPTH

    if is_dataclass(value) and not isinstance(value, type):
        identity = id(value)
        if identity in active:
            return _CYCLE
        active.add(identity)
        try:
            return _payload(
                {item.name: getattr(value, item.name) for item in fields(value)},
                depth=depth,
                active=active,
            )
        finally:
            active.remove(identity)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _payload(
                model_dump(mode="json"),
                depth=depth,
                active=active,
            )
        except Exception:
            return f"<{type(value).__name__}: unavailable>"

    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            return _CYCLE
        active.add(identity)
        try:
            mapping_output: dict[str, Any] = {}
            total = len(value)
            for index, (raw_key, item) in enumerate(value.items()):
                if index >= _MAX_COLLECTION_ITEMS:
                    break
                key = _clip_text(str(raw_key), _MAX_KEY_CHARS)
                mapping_output[key] = (
                    _REDACTED
                    if _secret_key(key)
                    else _payload(item, depth=depth + 1, active=active)
                )
            if total > _MAX_COLLECTION_ITEMS:
                mapping_output["__truncated_items__"] = (
                    total - _MAX_COLLECTION_ITEMS
                )
            return mapping_output
        finally:
            active.remove(identity)

    if isinstance(value, Sequence) and not isinstance(value, str):
        identity = id(value)
        if identity in active:
            return _CYCLE
        active.add(identity)
        try:
            total = len(value)
            sequence_output = [
                _payload(item, depth=depth + 1, active=active)
                for item in value[:_MAX_COLLECTION_ITEMS]
            ]
            if total > _MAX_COLLECTION_ITEMS:
                sequence_output.append(
                    f"...[+{total - _MAX_COLLECTION_ITEMS} items]"
                )
            return sequence_output
        finally:
            active.remove(identity)

    if isinstance(value, Set):
        identity = id(value)
        if identity in active:
            return _CYCLE
        active.add(identity)
        try:
            total = len(value)
            set_output = [
                _payload(item, depth=depth + 1, active=active)
                for index, item in enumerate(value)
                if index < _MAX_COLLECTION_ITEMS
            ]
            if total > _MAX_COLLECTION_ITEMS:
                set_output.append(f"...[+{total - _MAX_COLLECTION_ITEMS} items]")
            return set_output
        finally:
            active.remove(identity)

    return _clip_text(str(value))


def clip_payload(value: Any) -> Any:
    """Return a recursively bounded, JSON-shaped and secret-redacted payload."""

    try:
        return _payload(value, depth=0, active=set())
    except Exception:
        return f"<{type(value).__name__}: unavailable>"


def clip_messages(messages: list[dict[str, Any]], tail: int = 3) -> dict[str, Any]:
    """Trace-sized view of a conversation: counts plus a clipped tail."""

    selected = messages[-tail:] if tail > 0 else []
    return {
        "message_count": len(messages),
        "tail": [
            {
                "role": clip_payload(message.get("role")),
                "content": clip_payload(message.get("content")),
                **(
                    {"tool_calls": clip_payload(message["tool_calls"])}
                    if "tool_calls" in message
                    else {}
                ),
                **(
                    {"tool_call_id": clip_payload(message["tool_call_id"])}
                    if "tool_call_id" in message
                    else {}
                ),
            }
            for message in selected
        ],
    }


def _sdk_trace_context(
    client: Any,
    *,
    parent: ObservationContext | None,
    trace_id_seed: str | None,
) -> dict[str, str] | None:
    if parent is not None:
        context = {"trace_id": parent.trace_id}
        if parent.parent_span_id is not None:
            context["parent_span_id"] = parent.parent_span_id
        return context
    if trace_id_seed is None:
        return None
    trace_id = client.create_trace_id(seed=trace_id_seed)
    if not isinstance(trace_id, str) or not trace_id:
        return None
    return {"trace_id": trace_id}


def start_observation(
    *,
    name: str,
    as_type: ObservationType = "span",
    input_data: Any = None,
    output: Any = None,
    metadata: Mapping[str, Any] | None = None,
    parent: ObservationContext | None = None,
    trace_context: ObservationContext | None = None,
    trace_id_seed: str | None = None,
    model: str | None = None,
    model_parameters: Mapping[str, Any] | None = None,
) -> Any | None:
    """Start one explicit observation without relying on current-span context."""

    client = _get_client()
    if client is None:
        return None
    try:
        return client.start_observation(
            name=name,
            as_type=as_type,
            trace_context=_sdk_trace_context(
                client,
                parent=trace_context or parent,
                trace_id_seed=trace_id_seed,
            ),
            input=clip_payload(input_data),
            output=clip_payload(output),
            metadata=clip_payload(metadata),
            model=model,
            model_parameters=clip_payload(model_parameters),
        )
    except Exception:
        return None


def observation_context(observation: Any) -> ObservationContext | None:
    """Extract an immutable child-parent context from a Langfuse observation."""

    if observation is None:
        return None
    try:
        trace_id = observation.trace_id
        observation_id = observation.id
        if not isinstance(trace_id, str) or not isinstance(observation_id, str):
            return None
        return ObservationContext(trace_id=trace_id, parent_span_id=observation_id)
    except Exception:
        return None


def _error_fields(
    *,
    error: BaseException | str | None,
    level: str | None,
    status: str | None,
) -> tuple[str | None, str | None]:
    if error is None:
        return level, status
    if status is None:
        status = (
            f"{type(error).__name__}: {error}"
            if isinstance(error, BaseException)
            else error
        )
    return level or "ERROR", _clip_text(status)


def _finish(observation: Any, update: dict[str, Any]) -> None:
    if observation is None:
        return
    try:
        observation.update(**update)
    except Exception:
        pass
    try:
        observation.end()
    except Exception:
        pass


def end_observation(
    observation: Any,
    *,
    output: Any = None,
    metadata: Mapping[str, Any] | None = None,
    level: str | None = None,
    status: str | None = None,
    error: BaseException | str | None = None,
) -> None:
    """Update and end any span-like observation; failures never escape."""

    level, status = _error_fields(error=error, level=level, status=status)
    _finish(
        observation,
        {
            "output": clip_payload(output),
            "metadata": clip_payload(metadata),
            "level": level,
            "status_message": status,
        },
    )


@contextmanager
def run_span(name: str, metadata: dict[str, Any] | None = None) -> Iterator[Any]:
    """Legacy current-context span retained for existing synchronous callers."""

    client = _get_client()
    span_cm: Any | None = None
    span: Any | None = None
    if client is not None:
        try:
            span_cm = client.start_as_current_observation(
                name=name,
                as_type="span",
                metadata=clip_payload(metadata),
            )
            span = span_cm.__enter__()
        except Exception:
            span_cm = None
            span = None
    try:
        yield span
    finally:
        if span_cm is not None:
            try:
                span_cm.__exit__(None, None, None)
            except Exception:
                pass


def update_span(span: Any, *, output: Any = None, metadata: Any = None) -> None:
    if span is None:
        return
    try:
        span.update(output=clip_payload(output), metadata=clip_payload(metadata))
    except Exception:
        pass


def start_generation(
    *,
    name: str,
    model: str | None = None,
    input_data: Any = None,
    metadata: dict[str, Any] | None = None,
    parent: ObservationContext | None = None,
    trace_context: ObservationContext | None = None,
    trace_id_seed: str | None = None,
    model_parameters: Mapping[str, Any] | None = None,
) -> Any | None:
    return start_observation(
        name=name,
        as_type="generation",
        model=model,
        input_data=input_data,
        metadata=metadata,
        parent=parent,
        trace_context=trace_context,
        trace_id_seed=trace_id_seed,
        model_parameters=model_parameters,
    )


def end_generation(
    generation: Any,
    *,
    output: Any = None,
    model: str | None = None,
    usage: dict[str, int] | None = None,
    cost: dict[str, float] | None = None,
    level: str | None = None,
    status: str | None = None,
    error: BaseException | str | None = None,
) -> None:
    level, status = _error_fields(error=error, level=level, status=status)
    _finish(
        generation,
        {
            "output": clip_payload(output),
            "model": model,
            "usage_details": clip_payload(usage) if usage else None,
            "cost_details": clip_payload(cost) if cost else None,
            "level": level,
            "status_message": status,
        },
    )


def log_event(
    name: str,
    metadata: dict[str, Any] | None = None,
    *,
    parent: ObservationContext | None = None,
    trace_context: ObservationContext | None = None,
    trace_id_seed: str | None = None,
    input_data: Any = None,
    output: Any = None,
    level: str | None = None,
    status: str | None = None,
) -> None:
    client = _get_client()
    if client is None:
        return
    try:
        client.create_event(
            name=name,
            trace_context=_sdk_trace_context(
                client,
                parent=trace_context or parent,
                trace_id_seed=trace_id_seed,
            ),
            input=clip_payload(input_data),
            output=clip_payload(output),
            metadata=clip_payload(metadata),
            level=level,
            status_message=status,
        )
    except Exception:
        pass


def flush() -> None:
    client = _get_client()
    if client is not None:
        try:
            client.flush()
        except Exception:
            pass


def trace_url(trace_id: str) -> str | None:
    """Return the dashboard URL for a trace when the SDK can construct one."""

    client = _get_client()
    if client is None or not isinstance(trace_id, str) or not trace_id.strip():
        return None
    try:
        value = client.get_trace_url(trace_id=trace_id)
        return value if isinstance(value, str) and value else None
    except Exception:
        return None


__all__ = [
    "ObservationContext",
    "ObservationType",
    "clip_messages",
    "clip_payload",
    "configured",
    "enabled",
    "end_generation",
    "end_observation",
    "flush",
    "log_event",
    "observation_context",
    "run_span",
    "start_generation",
    "start_observation",
    "trace_url",
    "update_span",
]

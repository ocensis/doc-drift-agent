"""Langfuse wrapper: explicit parenting, bounded payloads and strict no-op failure."""

from __future__ import annotations

import sys
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from drift_agent.model import observability as obs


@pytest.fixture(autouse=True)
def _reset() -> Any:
    obs._reset_for_tests()
    yield
    obs._reset_for_tests()


class _FakeObservation:
    def __init__(
        self,
        *,
        trace_id: str = "a" * 32,
        observation_id: str = "b" * 16,
        fail_update: bool = False,
        fail_end: bool = False,
    ) -> None:
        self.trace_id = trace_id
        self.id = observation_id
        self.fail_update = fail_update
        self.fail_end = fail_end
        self.updates: list[dict[str, Any]] = []
        self.ended = False

    def update(self, **kwargs: Any) -> None:
        if self.fail_update:
            raise RuntimeError("update failed")
        self.updates.append(kwargs)

    def end(self) -> None:
        self.ended = True
        if self.fail_end:
            raise RuntimeError("end failed")


class _FakeSpanCM:
    def __init__(self, fail_enter: bool = False) -> None:
        self.fail_enter = fail_enter
        self.exited = False

    def __enter__(self) -> str:
        if self.fail_enter:
            raise RuntimeError("boom")
        return "SPAN"

    def __exit__(self, *args: Any) -> None:
        self.exited = True


class _FakeClient:
    def __init__(self) -> None:
        self.observation_calls: list[dict[str, Any]] = []
        self.observations: list[_FakeObservation] = []
        self.event_calls: list[dict[str, Any]] = []
        self.trace_seeds: list[str] = []
        self.span_cm = _FakeSpanCM()
        self.flushed = False

    def create_trace_id(self, *, seed: str) -> str:
        self.trace_seeds.append(seed)
        return "c" * 32

    def start_as_current_observation(self, **_kwargs: Any) -> _FakeSpanCM:
        return self.span_cm

    def start_observation(self, **kwargs: Any) -> _FakeObservation:
        self.observation_calls.append(kwargs)
        observation = _FakeObservation(
            trace_id=(kwargs.get("trace_context") or {}).get("trace_id", "a" * 32),
            observation_id=f"{len(self.observations) + 1:016x}",
        )
        self.observations.append(observation)
        return observation

    def create_event(self, **kwargs: Any) -> None:
        self.event_calls.append(kwargs)

    def flush(self) -> None:
        self.flushed = True

    def get_trace_url(self, *, trace_id: str) -> str:
        return f"https://langfuse.invalid/trace/{trace_id}"


def _inject() -> _FakeClient:
    fake = _FakeClient()
    obs._client = fake
    obs._checked = True
    return fake


class TestDisabled:
    def test_everything_noops_without_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
        monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
        assert obs.configured() is False
        assert obs.enabled() is False
        with obs.run_span("x", metadata={"a": 1}) as span:
            assert span is None
        observation = obs.start_observation(name="run", as_type="chain")
        generation = obs.start_generation(name="g", model="m", input_data={})
        assert observation is None and generation is None
        obs.end_observation(observation, output="o")
        obs.end_generation(generation, output="o")
        obs.update_span(None, output="o")
        obs.log_event("e")
        obs.flush()
        assert obs.trace_url("a" * 32) is None


class TestClientInitialization:
    def test_initializes_once_when_threads_race(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        started = threading.Event()
        release = threading.Event()
        calls: list[str | None] = []

        class _BlockingLangfuse:
            def __init__(self, *, base_url: str | None) -> None:
                calls.append(base_url)
                started.set()
                assert release.wait(2)

        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "public")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "secret")
        monkeypatch.setenv("LANGFUSE_BASE_URL", "https://langfuse.invalid")
        monkeypatch.setitem(
            sys.modules,
            "langfuse",
            SimpleNamespace(Langfuse=_BlockingLangfuse),
        )
        results: list[bool] = []
        first = threading.Thread(target=lambda: results.append(obs.enabled()))
        second = threading.Thread(target=lambda: results.append(obs.enabled()))
        first.start()
        assert started.wait(2)
        second.start()
        release.set()
        first.join(2)
        second.join(2)
        assert not first.is_alive() and not second.is_alive()
        assert calls == ["https://langfuse.invalid"]
        assert results == [True, True]


class TestExplicitObservations:
    def test_seeded_root_and_explicit_child_context(self) -> None:
        fake = _inject()
        root = obs.start_observation(
            name="blackboard.run",
            as_type="chain",
            input_data={"task": "inspect"},
            trace_id_seed="run-123",
        )
        assert root is not None
        assert fake.trace_seeds == ["run-123"]
        assert fake.observation_calls[0]["trace_context"] == {"trace_id": "c" * 32}
        assert obs.trace_url("c" * 32) == (
            "https://langfuse.invalid/trace/" + "c" * 32
        )

        parent = obs.observation_context(root)
        assert parent == obs.ObservationContext("c" * 32, "0000000000000001")
        generation = obs.start_generation(
            name="blackboard.review",
            trace_context=parent,
            model="requested-model",
            input_data={"lease_token": "must-not-leak", "candidate_id": "candidate-1"},
            metadata={"agent_id": "review-1"},
            model_parameters={"temperature": 0.0},
        )
        assert generation is not None
        call = fake.observation_calls[1]
        assert call["as_type"] == "generation"
        assert call["trace_context"] == {
            "trace_id": "c" * 32,
            "parent_span_id": "0000000000000001",
        }
        assert call["input"] == {
            "lease_token": "[REDACTED]",
            "candidate_id": "candidate-1",
        }

    def test_generation_end_adds_model_usage_cost_and_error(self) -> None:
        generation = _FakeObservation()
        obs.end_generation(
            generation,
            output={"finish_reason": "error"},
            model="actual-model",
            usage={"input": 10, "output": 2, "total": 12},
            cost={"total": 0.001},
            error=RuntimeError("provider failed"),
        )
        assert generation.ended
        update = generation.updates[0]
        assert update["model"] == "actual-model"
        assert update["usage_details"] == {"input": 10, "output": 2, "total": 12}
        assert update["cost_details"] == {"total": 0.001}
        assert update["level"] == "ERROR"
        assert update["status_message"] == "RuntimeError: provider failed"

    def test_event_and_generic_end_use_explicit_parent(self) -> None:
        fake = _inject()
        parent = obs.ObservationContext("d" * 32, "e" * 16)
        obs.log_event(
            "pair.evidence_requested",
            {"request_id": "request-1"},
            parent=parent,
            input_data={"missing_fact": "implementation path"},
            output={"state": "waiting"},
        )
        assert fake.event_calls[0]["trace_context"] == {
            "trace_id": "d" * 32,
            "parent_span_id": "e" * 16,
        }
        assert fake.event_calls[0]["input"] == {
            "missing_fact": "implementation path"
        }

        observation = _FakeObservation()
        obs.end_observation(
            observation,
            output={"status": "completed"},
            metadata={"agent_id": "gate"},
        )
        assert observation.ended
        assert observation.updates[0]["output"] == {"status": "completed"}


class TestFailureIsolation:
    def test_update_failure_still_attempts_end(self) -> None:
        observation = _FakeObservation(fail_update=True, fail_end=True)
        obs.end_observation(observation, output={"ok": False})
        assert observation.ended

    def test_sdk_start_and_event_failures_are_noops(self) -> None:
        class _BrokenClient:
            def start_observation(self, **_kwargs: Any) -> None:
                raise RuntimeError("start failed")

            def create_event(self, **_kwargs: Any) -> None:
                raise RuntimeError("event failed")

            def flush(self) -> None:
                raise RuntimeError("flush failed")

        obs._client = _BrokenClient()
        obs._checked = True
        assert obs.start_observation(name="broken", trace_id_seed=None) is None
        obs.log_event("broken")
        obs.flush()

    def test_span_enter_failure_degrades_to_none(self) -> None:
        fake = _inject()
        fake.span_cm = _FakeSpanCM(fail_enter=True)
        with obs.run_span("run") as span:
            assert span is None

    def test_body_exception_propagates_but_span_closes(self) -> None:
        fake = _inject()
        with pytest.raises(ValueError, match="inner"):
            with obs.run_span("run"):
                raise ValueError("inner")
        assert fake.span_cm.exited


class TestPayloadClipping:
    def test_recursively_bounds_and_redacts_payload(self) -> None:
        cycle: dict[str, Any] = {}
        cycle["self"] = cycle
        clipped = obs.clip_payload(
            {
                "lease_token": "lease-secret",
                "nested": {
                    "LANGFUSE_SECRET_KEY": "sdk-secret",
                    "apiKey": "api-secret",
                    "input_tokens": 42,
                },
                "long": "x" * 5_000,
                "many": list(range(80)),
                "cycle": cycle,
            }
        )
        assert clipped["lease_token"] == "[REDACTED]"
        assert clipped["nested"] == {
            "LANGFUSE_SECRET_KEY": "[REDACTED]",
            "apiKey": "[REDACTED]",
            "input_tokens": 42,
        }
        assert clipped["long"].endswith("...[+1000 chars]")
        assert clipped["many"][-1] == "...[+30 items]"
        assert clipped["cycle"]["self"] == "[TRUNCATED: cycle]"

    def test_clip_messages_preserves_shape_and_bounds_tail(self) -> None:
        messages = [
            {
                "role": "user",
                "content": {"text": "x" * 10_000, "secret_key": "hidden"},
            }
            for _ in range(6)
        ]
        view = obs.clip_messages(messages, tail=2)
        assert view["message_count"] == 6 and len(view["tail"]) == 2
        content = view["tail"][0]["content"]
        assert content["secret_key"] == "[REDACTED]"
        assert len(content["text"]) < 5_000
        assert obs.clip_messages(messages, tail=0)["tail"] == []

    def test_serialized_secrets_are_redacted_and_tool_calls_remain_visible(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "provider-secret-value")
        view = obs.clip_messages(
            [
                {
                    "role": "assistant",
                    "content": (
                        '{"lease_token":"lease-secret","authorization":'
                        '"Bearer abc.def","value":"provider-secret-value"}'
                    ),
                    "tool_calls": [
                        {
                            "function": {
                                "name": "request_evidence",
                                "arguments": '{"token":"nested-secret"}',
                            }
                        }
                    ],
                }
            ]
        )
        rendered = str(view)
        assert "lease-secret" not in rendered
        assert "abc.def" not in rendered
        assert "provider-secret-value" not in rendered
        assert "nested-secret" not in rendered
        assert "request_evidence" in rendered

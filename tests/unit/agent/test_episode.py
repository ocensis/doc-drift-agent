"""Episode runner and toolbox: jail, loop, submit, retry, and accounting."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from drift_agent.agent.budget import BudgetExhausted, BudgetLedger
from drift_agent.agent.episode import (
    EpisodeRunner,
    RepoToolbox,
    ToolError,
)
from drift_agent.domain.models import RunBudgets
from drift_agent.model.client import ModelClientError
from drift_agent.model.contracts import ModelCallUsage
from drift_agent.model.openrouter import ChatTurnResult


class _Submission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str


def _usage(prompt: int = 10, completion: int = 5) -> ModelCallUsage:
    return ModelCallUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        cost_usd=0.0,
    )


def _turn(message: dict[str, Any], finish_reason: str = "tool_calls") -> ChatTurnResult:
    return ChatTurnResult(
        message=message,
        finish_reason=finish_reason,
        usage=_usage(),
        actual_model="test-model",
        request_id="req",
    )


def _submit_call(payload: dict[str, Any], call_id: str = "call_1") -> dict[str, Any]:
    return {
        "id": call_id,
        "function": {"name": "submit", "arguments": json.dumps(payload)},
    }


class _ScriptedTransport:
    """Returns queued ChatTurnResults; raises queued errors first."""

    def __init__(self, turns: list[ChatTurnResult | ModelClientError]) -> None:
        self.turns = list(turns)
        self.requests: list[list[dict[str, Any]]] = []

    def complete_chat(
        self,
        *,
        profile: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_output_tokens: int,
        temperature: float,
        reasoning_effort: str,
        timeout_seconds: float,
    ) -> ChatTurnResult:
        self.requests.append([dict(message) for message in messages])
        item = self.turns.pop(0)
        if isinstance(item, ModelClientError):
            raise item
        return item


def _ledger(calls: int = 50, tokens: int = 10_000_000) -> BudgetLedger:
    return BudgetLedger(
        RunBudgets(
            max_model_calls_per_run=calls,
            max_input_tokens_per_run=tokens,
            timeout_seconds=600.0,
        )
    )


def _runner(transport: _ScriptedTransport, ledger: BudgetLedger) -> EpisodeRunner:
    return EpisodeRunner(
        transport,
        ledger,
        max_turns=3,
        backoff_seconds=0.0,
        sleep=lambda _seconds: None,
    )


class TestRepoToolbox:
    def test_rejects_escapes(self, tmp_path: Path) -> None:
        (tmp_path / "inside.txt").write_text("data", encoding="utf-8")
        toolbox = RepoToolbox(tmp_path)
        for attempt in ("/etc/passwd", "../outside", "a/../../b", "~/x", ""):
            with pytest.raises(ToolError):
                toolbox.read_file(attempt)

    def test_read_file_line_range(self, tmp_path: Path) -> None:
        (tmp_path / "doc.md").write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
        toolbox = RepoToolbox(tmp_path)
        text = toolbox.read_file("doc.md", start=2, end=3)
        assert "2: two" in text and "3: three" in text
        assert "1: one" not in text

    def test_grep_falls_back_to_literal(self, tmp_path: Path) -> None:
        (tmp_path / "code.ts").write_text("const price = a[1](x)\n", encoding="utf-8")
        toolbox = RepoToolbox(tmp_path)
        assert "code.ts:1" in toolbox.grep("a[1](x")

    def test_list_dir(self, tmp_path: Path) -> None:
        (tmp_path / "sub").mkdir()
        (tmp_path / "file.md").write_text("x", encoding="utf-8")
        listing = RepoToolbox(tmp_path).list_dir(".")
        assert "sub/" in listing and "file.md" in listing

    def test_retry_delay_ladder(self) -> None:
        from drift_agent.agent.episode import _retry_delay

        # Fast reasons keep the exponential base.
        assert _retry_delay("rate_limited", 0, 2.0) == 2.0
        assert _retry_delay("rate_limited", 1, 2.0) == 4.0
        # Wave-shaped gateway outages get a 30s/60s floor.
        assert _retry_delay("provider_unavailable", 0, 2.0) == 30.0
        assert _retry_delay("provider_unavailable", 1, 2.0) == 60.0
        assert _retry_delay("request_forbidden", 1, 2.0) == 60.0

    def test_reads_tracked_for_successful_reads_only(self, tmp_path: Path) -> None:
        (tmp_path / "doc.md").write_text("one\n", encoding="utf-8")
        toolbox = RepoToolbox(tmp_path, claim_extractor=lambda doc: f"claims of {doc}")
        toolbox.read_file("./doc.md")
        with pytest.raises(ToolError):
            toolbox.read_file("missing.md")
        toolbox.extract_claims("doc.md")
        assert toolbox.reads == {"doc.md"}


class TestEpisodeRunner:
    def test_submit_on_first_turn(self, tmp_path: Path) -> None:
        transport = _ScriptedTransport(
            [_turn({"tool_calls": [_submit_call({"answer": "done"})]})]
        )
        ledger = _ledger()
        outcome = _runner(transport, ledger).run(
            system_prompt="system",
            user_prompt="user",
            toolbox=RepoToolbox(tmp_path),
            submit_model=_Submission,
        )
        assert outcome.ok and outcome.submitted == {"answer": "done"}
        assert outcome.turns == 1
        assert ledger.usage_snapshot().model_calls == 1

    def test_tool_round_trip_then_submit(self, tmp_path: Path) -> None:
        (tmp_path / "doc.md").write_text("hello world\n", encoding="utf-8")
        transport = _ScriptedTransport(
            [
                _turn(
                    {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_r",
                                "function": {
                                    "name": "read_file",
                                    "arguments": json.dumps({"path": "doc.md"}),
                                },
                            }
                        ],
                    }
                ),
                _turn({"tool_calls": [_submit_call({"answer": "read it"})]}),
            ]
        )
        outcome = _runner(transport, _ledger()).run(
            system_prompt="system",
            user_prompt="user",
            toolbox=RepoToolbox(tmp_path),
            submit_model=_Submission,
        )
        assert outcome.ok and outcome.tool_calls == 1 and outcome.turns == 2
        tool_message = transport.requests[1][-1]
        assert tool_message["role"] == "tool"
        assert "hello world" in tool_message["content"]

    def test_inline_json_submission_accepted(self, tmp_path: Path) -> None:
        transport = _ScriptedTransport(
            [_turn({"content": '{"answer": "inline"}'}, finish_reason="stop")]
        )
        outcome = _runner(transport, _ledger()).run(
            system_prompt="system",
            user_prompt="user",
            toolbox=RepoToolbox(tmp_path),
            submit_model=_Submission,
        )
        assert outcome.ok and outcome.submitted == {"answer": "inline"}

    def test_nudge_then_no_submit_fails(self, tmp_path: Path) -> None:
        transport = _ScriptedTransport(
            [
                _turn({"content": "let me think"}, finish_reason="stop"),
                _turn({"content": "still prose"}, finish_reason="stop"),
            ]
        )
        outcome = _runner(transport, _ledger()).run(
            system_prompt="system",
            user_prompt="user",
            toolbox=RepoToolbox(tmp_path),
            submit_model=_Submission,
        )
        assert not outcome.ok and outcome.failure_reason == "no_submit"
        nudge = transport.requests[1][-1]
        assert nudge["role"] == "user" and "submit" in nudge["content"]

    def test_turn_limit(self, tmp_path: Path) -> None:
        read_call = {
            "id": "c",
            "function": {"name": "list_dir", "arguments": "{}"},
        }
        transport = _ScriptedTransport(
            [_turn({"tool_calls": [dict(read_call)]}) for _ in range(3)]
        )
        outcome = _runner(transport, _ledger()).run(
            system_prompt="system",
            user_prompt="user",
            toolbox=RepoToolbox(tmp_path),
            submit_model=_Submission,
        )
        assert not outcome.ok and outcome.failure_reason == "turn_limit"

    def test_retries_rate_limit_then_succeeds(self, tmp_path: Path) -> None:
        transport = _ScriptedTransport(
            [
                ModelClientError("rate_limited"),
                _turn({"tool_calls": [_submit_call({"answer": "after retry"})]}),
            ]
        )
        ledger = _ledger()
        outcome = _runner(transport, ledger).run(
            system_prompt="system",
            user_prompt="user",
            toolbox=RepoToolbox(tmp_path),
            submit_model=_Submission,
        )
        assert outcome.ok
        # The failed attempt still consumed a reserved call.
        assert ledger.usage_snapshot().model_calls == 2

    def test_non_retryable_error_fails_episode(self, tmp_path: Path) -> None:
        transport = _ScriptedTransport([ModelClientError("request_rejected")])
        outcome = _runner(transport, _ledger()).run(
            system_prompt="system",
            user_prompt="user",
            toolbox=RepoToolbox(tmp_path),
            submit_model=_Submission,
        )
        assert not outcome.ok and outcome.failure_reason == "model:request_rejected"

    def test_budget_exhausted_propagates(self, tmp_path: Path) -> None:
        transport = _ScriptedTransport([])
        ledger = _ledger(calls=0)
        with pytest.raises(BudgetExhausted):
            _runner(transport, ledger).run(
                system_prompt="system",
                user_prompt="user",
                toolbox=RepoToolbox(tmp_path),
                submit_model=_Submission,
            )

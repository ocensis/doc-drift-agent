from __future__ import annotations

import math

import pytest

from drift_agent.agent.budget import BudgetExhausted, BudgetLedger
from drift_agent.domain.models import RunBudgets


class FakeClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_ledger_accounts_for_reserved_and_recorded_usage() -> None:
    clock = FakeClock()
    ledger = BudgetLedger(RunBudgets(), clock=clock)

    fast = ledger.reserve_model_call("fast", 120)
    strong = ledger.reserve_model_call("strong", 80)
    ledger.record_model_usage(
        fast,
        input_tokens=110,
        output_tokens=30,
        estimated_cost_usd=0.0125,
    )
    ledger.record_model_usage(
        strong,
        input_tokens=70,
        output_tokens=20,
        estimated_cost_usd=0.02,
    )
    ledger.reserve_validation_command(2)
    ledger.record_tool_call(3)
    clock.advance(0.25)

    usage = ledger.usage_snapshot()
    assert usage.model_calls == 2
    assert usage.model_calls_by_profile == {"fast": 1, "strong": 1}
    assert usage.validation_commands == 2
    assert usage.input_tokens == 180
    assert usage.output_tokens == 50
    assert usage.estimated_cost_usd == pytest.approx(0.0325)
    assert usage.tool_calls == 3
    assert usage.duration_ms == 250


def test_model_reservation_is_atomic_across_call_and_token_limits() -> None:
    clock = FakeClock()
    ledger = BudgetLedger(
        RunBudgets(max_model_calls_per_run=1, max_input_tokens_per_run=5),
        clock=clock,
    )
    ledger.reserve_model_call("fast", 5)

    with pytest.raises(BudgetExhausted) as raised:
        ledger.reserve_model_call("strong", 0)

    assert raised.value.reason_code == "budget_exhausted"
    assert raised.value.resource == "max_model_calls_per_run"
    assert ledger.usage_snapshot().model_calls == 1
    assert ledger.usage_snapshot().input_tokens == 0
    assert ledger.usage_snapshot().model_calls_by_profile == {"fast": 1}

    token_limited = BudgetLedger(
        RunBudgets(max_model_calls_per_run=4, max_input_tokens_per_run=5),
        clock=clock,
    )
    with pytest.raises(BudgetExhausted) as token_error:
        token_limited.reserve_model_call("fast", 6)
    assert token_error.value.resource == "max_input_tokens_per_run"
    assert token_limited.usage_snapshot().model_calls == 0
    assert token_limited.usage_snapshot().input_tokens == 0


def test_validation_command_reservation_blocks_before_the_extra_command() -> None:
    ledger = BudgetLedger(
        RunBudgets(max_validation_commands_per_run=2),
        clock=FakeClock(),
    )
    executed: list[str] = []

    def execute(name: str) -> None:
        ledger.reserve_validation_command()
        executed.append(name)

    execute("first")
    execute("second")
    with pytest.raises(BudgetExhausted) as raised:
        execute("third")

    assert raised.value.resource == "max_validation_commands_per_run"
    assert executed == ["first", "second"]
    assert ledger.usage_snapshot().validation_commands == 2


def test_validation_capacity_preflight_does_not_consume_the_budget() -> None:
    ledger = BudgetLedger(
        RunBudgets(max_validation_commands_per_run=1),
        clock=FakeClock(),
    )

    ledger.ensure_validation_capacity()

    assert ledger.usage_snapshot().validation_commands == 0
    ledger.reserve_validation_command()
    with pytest.raises(BudgetExhausted):
        ledger.ensure_validation_capacity()


def test_shared_patch_attempt_counts_once_for_each_finding_and_fails_atomically() -> None:
    ledger = BudgetLedger(RunBudgets(max_patch_attempts_per_finding=2), clock=FakeClock())

    ledger.reserve_patch_attempt(["finding-b", "finding-a", "finding-a"])
    ledger.ensure_patch_attempt_capacity("finding-a")
    assert ledger.patch_attempts_for("finding-a") == 1
    ledger.reserve_patch_attempt("finding-a")

    with pytest.raises(BudgetExhausted) as raised:
        ledger.ensure_patch_attempt_capacity(["finding-a", "finding-c"])
    with pytest.raises(BudgetExhausted):
        ledger.reserve_patch_attempt(["finding-a", "finding-c"])

    assert raised.value.resource == "max_patch_attempts_per_finding"
    assert ledger.patch_attempts_for("finding-a") == 2
    assert ledger.patch_attempts_for("finding-b") == 1
    assert ledger.patch_attempts_for("finding-c") == 0


def test_deadline_is_checked_before_reserving_work() -> None:
    clock = FakeClock()
    ledger = BudgetLedger(RunBudgets(timeout_seconds=1), clock=clock)
    assert ledger.remaining_seconds() == 1
    clock.advance(0.25)
    assert ledger.remaining_seconds() == pytest.approx(0.75)
    clock.advance(0.75)

    with pytest.raises(BudgetExhausted) as raised:
        ledger.reserve_validation_command()

    assert raised.value.reason_code == "budget_exhausted"
    assert raised.value.resource == "timeout_seconds"
    assert ledger.remaining_seconds() == 0
    assert ledger.usage_snapshot().validation_commands == 0


def test_model_usage_is_preserved_when_a_call_overruns_the_deadline() -> None:
    clock = FakeClock()
    ledger = BudgetLedger(RunBudgets(timeout_seconds=1), clock=clock)
    reservation = ledger.reserve_model_call("fast", 10)
    clock.advance(1.1)

    with pytest.raises(BudgetExhausted):
        ledger.record_model_usage(
            reservation,
            input_tokens=8,
            output_tokens=4,
            estimated_cost_usd=0.1,
        )

    usage = ledger.usage_snapshot()
    assert usage.model_calls == 1
    assert usage.input_tokens == 8
    assert usage.output_tokens == 4
    assert usage.estimated_cost_usd == pytest.approx(0.1)


@pytest.mark.parametrize(
    ("input_tokens", "output_tokens", "cost"),
    [(-1, 1, 0.0), (1, -1, 0.0), (1, 1, -0.1), (1, 1, math.inf), (1, 1, math.nan)],
)
def test_model_usage_rejects_invalid_values(
    input_tokens: int,
    output_tokens: int,
    cost: float,
) -> None:
    ledger = BudgetLedger(RunBudgets(), clock=FakeClock())
    reservation = ledger.reserve_model_call("fast", 10)

    with pytest.raises(ValueError):
        ledger.record_model_usage(
            reservation,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=cost,
        )


def test_actual_model_usage_is_recorded_before_an_input_overrun_is_reported() -> None:
    ledger = BudgetLedger(
        RunBudgets(max_input_tokens_per_run=5),
        clock=FakeClock(),
    )
    reservation = ledger.reserve_model_call("fast", 4)

    with pytest.raises(BudgetExhausted) as raised:
        ledger.record_model_usage(
            reservation,
            input_tokens=7,
            output_tokens=2,
            estimated_cost_usd=0.01,
        )

    assert raised.value.resource == "max_input_tokens_per_run"
    usage = ledger.usage_snapshot()
    assert usage.input_tokens == 7
    assert usage.output_tokens == 2
    assert usage.estimated_cost_usd == pytest.approx(0.01)


def test_model_usage_can_only_be_recorded_once_per_reservation() -> None:
    ledger = BudgetLedger(RunBudgets(), clock=FakeClock())
    reservation = ledger.reserve_model_call("fast", 10)
    ledger.record_model_usage(
        reservation,
        input_tokens=8,
        output_tokens=1,
        estimated_cost_usd=0.001,
    )

    with pytest.raises(ValueError, match="already recorded"):
        ledger.record_model_usage(
            reservation,
            input_tokens=8,
            output_tokens=1,
            estimated_cost_usd=0.001,
        )


def test_ledger_rejects_a_non_finite_or_non_monotonic_clock() -> None:
    with pytest.raises(ValueError, match="finite"):
        BudgetLedger(RunBudgets(), clock=lambda: math.inf)

    clock = FakeClock()
    ledger = BudgetLedger(RunBudgets(), clock=clock)
    clock.advance(-1)
    with pytest.raises(ValueError, match="backwards"):
        ledger.check_deadline()

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Literal

from drift_agent.domain.models import RunBudgets, Usage

BudgetReasonCode = Literal["budget_exhausted"]
Clock = Callable[[], float]


@dataclass(frozen=True, slots=True)
class ModelCallReservation:
    """Opaque capacity reservation for one model request."""

    reservation_id: int
    profile: str
    reserved_input_tokens: int


class BudgetExhausted(RuntimeError):
    """Raised before an operation would exceed a configured run budget."""

    reason_code: BudgetReasonCode = "budget_exhausted"

    def __init__(self, resource: str) -> None:
        self.resource = resource
        super().__init__(f"budget exhausted: {resource}")


def _non_negative_int(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _non_negative_float(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite and non-negative")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return normalized


class BudgetLedger:
    """Run-local, reserve-before-use accounting for bounded agent work.

    Reservations are intentionally not refunded. A model or validation command
    that fails after it starts still consumed the capacity that guarded the
    side effect.
    """

    def __init__(
        self,
        budgets: RunBudgets,
        *,
        clock: Clock = time.monotonic,
    ) -> None:
        self.budgets = budgets.model_copy(deep=True)
        self._clock = clock
        # Concurrent episode fan-out reserves and records from worker threads;
        # every read-modify-write below must hold this lock.
        self._lock = threading.RLock()
        self._started_at = self._read_clock()
        self._model_calls = 0
        self._model_calls_by_profile: dict[str, int] = {}
        self._validation_commands = 0
        self._actual_input_tokens = 0
        self._charged_input_tokens = 0
        self._output_tokens = 0
        self._estimated_cost_usd = 0.0
        self._tool_calls = 0
        self._patch_attempts_by_finding: dict[str, int] = {}
        self._next_model_reservation_id = 1
        self._pending_model_calls: dict[int, ModelCallReservation] = {}

    def _read_clock(self) -> float:
        value = float(self._clock())
        if not math.isfinite(value):
            raise ValueError("monotonic clock must return a finite value")
        return value

    def _elapsed_seconds(self) -> float:
        elapsed = self._read_clock() - self._started_at
        if elapsed < 0:
            raise ValueError("monotonic clock moved backwards")
        return elapsed

    @staticmethod
    def _raise_exhausted(resource: str) -> None:
        raise BudgetExhausted(resource)

    def remaining_seconds(self) -> float:
        return max(0.0, self.budgets.timeout_seconds - self._elapsed_seconds())

    def check_deadline(self) -> None:
        if self.remaining_seconds() == 0.0:
            self._raise_exhausted("timeout_seconds")

    def reserve_model_call(
        self,
        profile: str,
        input_tokens: int,
    ) -> ModelCallReservation:
        """Atomically reserve one model call and an input-token upper bound."""

        if not profile or not profile.strip():
            raise ValueError("profile must be non-empty")
        requested_tokens = _non_negative_int(input_tokens, "input_tokens")
        with self._lock:
            self.check_deadline()
            if self._model_calls + 1 > self.budgets.max_model_calls_per_run:
                self._raise_exhausted("max_model_calls_per_run")
            if (
                self._charged_input_tokens + requested_tokens
                > self.budgets.max_input_tokens_per_run
            ):
                self._raise_exhausted("max_input_tokens_per_run")
            reservation = ModelCallReservation(
                reservation_id=self._next_model_reservation_id,
                profile=profile,
                reserved_input_tokens=requested_tokens,
            )
            self._next_model_reservation_id += 1
            self._model_calls += 1
            self._charged_input_tokens += requested_tokens
            self._model_calls_by_profile[profile] = (
                self._model_calls_by_profile.get(profile, 0) + 1
            )
            self._pending_model_calls[reservation.reservation_id] = reservation
            return reservation

    def ensure_validation_capacity(self, count: int = 1) -> None:
        requested = _non_negative_int(count, "count")
        with self._lock:
            self.check_deadline()
            if self._validation_commands + requested > (
                self.budgets.max_validation_commands_per_run
            ):
                self._raise_exhausted("max_validation_commands_per_run")

    def reserve_validation_command(self, count: int = 1) -> None:
        requested = _non_negative_int(count, "count")
        with self._lock:
            self.ensure_validation_capacity(requested)
            self._validation_commands += requested

    def _normalized_finding_ids(
        self,
        finding_ids: str | Iterable[str],
    ) -> tuple[str, ...]:
        normalized_ids: tuple[str, ...]
        if isinstance(finding_ids, str):
            normalized_ids = (finding_ids,)
        else:
            normalized_ids = tuple(sorted(set(finding_ids)))
        if not normalized_ids or any(not item for item in normalized_ids):
            raise ValueError("finding_ids must contain non-empty identifiers")
        return normalized_ids

    def ensure_patch_attempt_capacity(
        self,
        finding_ids: str | Iterable[str],
    ) -> None:
        """Check one prospective shared attempt without consuming it."""

        normalized_ids = self._normalized_finding_ids(finding_ids)
        self.check_deadline()
        if any(
            self._patch_attempts_by_finding.get(finding_id, 0) + 1
            > self.budgets.max_patch_attempts_per_finding
            for finding_id in normalized_ids
        ):
            self._raise_exhausted("max_patch_attempts_per_finding")

    def reserve_patch_attempt(self, finding_ids: str | Iterable[str]) -> None:
        """Reserve one shared patch attempt against every participating finding."""

        normalized_ids = self._normalized_finding_ids(finding_ids)
        with self._lock:
            self.ensure_patch_attempt_capacity(normalized_ids)
            for finding_id in normalized_ids:
                self._patch_attempts_by_finding[finding_id] = (
                    self._patch_attempts_by_finding.get(finding_id, 0) + 1
                )

    def record_model_usage(
        self,
        reservation: ModelCallReservation,
        *,
        input_tokens: int,
        output_tokens: int,
        estimated_cost_usd: float,
    ) -> None:
        """Record provider usage exactly once, then enforce any overrun."""

        recorded_input_tokens = _non_negative_int(input_tokens, "input_tokens")
        recorded_output_tokens = _non_negative_int(output_tokens, "output_tokens")
        recorded_cost = _non_negative_float(estimated_cost_usd, "estimated_cost_usd")
        with self._lock:
            pending = self._pending_model_calls.get(reservation.reservation_id)
            if pending is None or pending != reservation:
                raise ValueError("model call reservation is unknown or already recorded")
            self._charged_input_tokens += max(
                0,
                recorded_input_tokens - reservation.reserved_input_tokens,
            )
            self._actual_input_tokens += recorded_input_tokens
            self._output_tokens += recorded_output_tokens
            self._estimated_cost_usd += recorded_cost
            del self._pending_model_calls[reservation.reservation_id]
            if self._charged_input_tokens > self.budgets.max_input_tokens_per_run:
                self._raise_exhausted("max_input_tokens_per_run")
            self.check_deadline()

    def record_tool_call(self, count: int = 1) -> None:
        recorded = _non_negative_int(count, "count")
        with self._lock:
            self.check_deadline()
            self._tool_calls += recorded

    def patch_attempts_for(self, finding_id: str) -> int:
        return self._patch_attempts_by_finding.get(finding_id, 0)

    def usage_snapshot(self) -> Usage:
        with self._lock:
            return self._usage_snapshot_locked()

    def _usage_snapshot_locked(self) -> Usage:
        return Usage(
            model_calls=self._model_calls,
            model_calls_by_profile=dict(sorted(self._model_calls_by_profile.items())),
            tool_calls=self._tool_calls,
            validation_commands=self._validation_commands,
            input_tokens=self._actual_input_tokens,
            output_tokens=self._output_tokens,
            estimated_cost_usd=self._estimated_cost_usd,
            duration_ms=int(self._elapsed_seconds() * 1000),
        )

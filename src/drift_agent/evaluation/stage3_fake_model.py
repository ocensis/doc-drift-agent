from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Final

from drift_agent.evaluation.stage3_models import Stage3ModelStep
from drift_agent.model.contracts import (
    ModelCallUsage,
    ModelProfile,
    StructuredModelRequest,
    StructuredModelResponse,
)

_FAKE_PROVIDER: Final = "stage3_fake"


def canonical_model_request(request: StructuredModelRequest) -> bytes:
    """Return the frozen provider-neutral request projection used by fixtures."""

    return json.dumps(
        request.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def model_request_sha256(request: StructuredModelRequest) -> str:
    return hashlib.sha256(canonical_model_request(request)).hexdigest()


@dataclass(frozen=True, slots=True)
class Stage3FakeCall:
    ordinal: int
    profile: ModelProfile
    request_sha256: str


class ScriptedModelTransport:
    """Deterministic, offline ModelTransport for versioned Stage 3 cases."""

    def __init__(self, script: tuple[Stage3ModelStep, ...]) -> None:
        self._script = script
        self._calls: list[Stage3FakeCall] = []
        self._validated_request_sha256: str | None = None

    @property
    def calls(self) -> tuple[Stage3FakeCall, ...]:
        return tuple(self._calls)

    @property
    def consumed(self) -> bool:
        return len(self._calls) == len(self._script)

    def _next_step(self) -> Stage3ModelStep:
        if len(self._calls) >= len(self._script):
            raise AssertionError("unexpected Stage 3 fake model call")
        return self._script[len(self._calls)]

    def validate_request(self, request: StructuredModelRequest) -> None:
        step = self._next_step()
        digest = model_request_sha256(request)
        if request.profile != step.profile:
            raise AssertionError(
                f"Stage 3 fake profile mismatch: expected={step.profile} actual={request.profile}"
            )
        if digest != step.request_sha256:
            raise AssertionError(
                "Stage 3 fake request hash mismatch: "
                f"expected={step.request_sha256} actual={digest}"
            )
        self._validated_request_sha256 = digest

    def complete_structured(
        self,
        request: StructuredModelRequest,
        *,
        timeout_seconds: float,
    ) -> StructuredModelResponse:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise AssertionError("Stage 3 fake received a non-positive timeout")
        digest = model_request_sha256(request)
        if self._validated_request_sha256 != digest:
            self.validate_request(request)
        step = self._next_step()
        ordinal = len(self._calls) + 1
        self._calls.append(
            Stage3FakeCall(
                ordinal=ordinal,
                profile=step.profile,
                request_sha256=digest,
            )
        )
        self._validated_request_sha256 = None
        model = f"stage3-fake/{step.profile}-v1"
        return StructuredModelResponse(
            provider=_FAKE_PROVIDER,
            profile=step.profile,
            requested_model=model,
            actual_model=model,
            request_id=f"stage3-fake-{ordinal:04d}",
            finish_reason="stop",
            output=step.output,
            usage=ModelCallUsage(
                prompt_tokens=step.prompt_tokens,
                completion_tokens=step.completion_tokens,
                total_tokens=step.total_tokens,
                cost_usd=step.cost_nano_usd / 1_000_000_000,
            ),
        )

    def assert_consumed(self) -> None:
        if not self.consumed:
            raise AssertionError(
                "Stage 3 fake model script was not fully consumed: "
                f"expected={len(self._script)} actual={len(self._calls)}"
            )


__all__ = [
    "ScriptedModelTransport",
    "Stage3FakeCall",
    "canonical_model_request",
    "model_request_sha256",
]

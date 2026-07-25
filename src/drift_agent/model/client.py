from __future__ import annotations

from typing import Protocol

from drift_agent.model.contracts import (
    ModelCallUsage,
    ModelTokenUsage,
    StructuredModelRequest,
    StructuredModelResponse,
)


class ModelClientError(RuntimeError):
    """Sanitized provider-boundary failure safe to expose to the CLI."""

    def __init__(
        self,
        reason_code: str,
        *,
        status_code: int | None = None,
        provider_status_code: int | None = None,
        provider_error_type: str | None = None,
        usage: ModelCallUsage | ModelTokenUsage | None = None,
        usage_cost_usd_known: bool = True,
    ) -> None:
        self.reason_code = reason_code
        # ``status_code`` is the actual HTTP response status. OpenRouter may
        # commit HTTP 200 and then report the provider failure in-band; retain
        # that embedded numeric code separately so retries and telemetry do not
        # confuse the two layers.
        self.status_code = status_code
        self.provider_status_code = provider_status_code
        self.provider_error_type = provider_error_type
        self.usage = usage
        self.usage_cost_usd_known = usage_cost_usd_known
        super().__init__(reason_code)


class ModelTransport(Protocol):
    def validate_request(self, request: StructuredModelRequest) -> None: ...

    def complete_structured(
        self,
        request: StructuredModelRequest,
        *,
        timeout_seconds: float,
    ) -> StructuredModelResponse: ...

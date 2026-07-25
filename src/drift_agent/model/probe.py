from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict

from drift_agent.agent.budget import BudgetLedger
from drift_agent.domain.models import RunBudgets
from drift_agent.model.budgeted import ModelClient
from drift_agent.model.client import ModelClientError
from drift_agent.model.contracts import ModelCallUsage, ModelProfile, StructuredModelRequest
from drift_agent.model.openrouter import OpenRouterSettings, OpenRouterTransport


class ModelProbePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    ok: Literal[True]
    protocol_version: Literal[1]


class ModelProbeReport(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    status: Literal["connected"]
    provider: Literal["openrouter"]
    profile: ModelProfile
    model: str
    request_id: str
    usage: ModelCallUsage


def probe_openrouter(
    *,
    profile: ModelProfile = "fast",
    model_override: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> ModelProbeReport:
    """Make one tiny, repository-independent structured connectivity request."""

    settings = OpenRouterSettings.from_environment(environment)
    settings.model_for(profile, model_override)
    request = StructuredModelRequest(
        profile=profile,
        schema_name="drift_agent_connectivity",
        response_schema=ModelProbePayload.model_json_schema(),
        system_prompt="Return only the requested structured object.",
        user_prompt=("Confirm protocol connectivity by setting ok=true and protocol_version=1."),
        max_output_tokens=64,
    )
    ledger = BudgetLedger(
        RunBudgets(
            max_model_calls_per_run=1,
            max_input_tokens_per_run=4_096,
            timeout_seconds=settings.timeout_seconds,
        )
    )
    result = ModelClient(
        OpenRouterTransport(settings, model_override=model_override),
        ledger,
    ).complete(
        request,
        ModelProbePayload,
        timeout_seconds=settings.timeout_seconds,
    )
    if result.raw.provider != "openrouter":
        raise ModelClientError("provider_identity_mismatch")
    return ModelProbeReport(
        status="connected",
        provider="openrouter",
        profile=profile,
        model=result.raw.actual_model,
        request_id=result.raw.request_id,
        usage=result.raw.usage,
    )

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Generic, Literal, Self, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ModelProfile = Literal["fast", "strong"]
OutputT = TypeVar("OutputT", bound=BaseModel)


class _WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class StructuredModelRequest(_WireModel):
    """One bounded, non-streaming request for a JSON object."""

    profile: ModelProfile
    schema_name: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z][A-Za-z0-9_-]*$",
    )
    response_schema: dict[str, Any]
    system_prompt: str = Field(min_length=1, max_length=8_000)
    user_prompt: str = Field(min_length=1, max_length=16_000)
    max_output_tokens: int = Field(ge=1, le=4_096)

    @field_validator("response_schema")
    @classmethod
    def validate_response_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        if value.get("type") != "object":
            raise ValueError("response schema root must be an object")
        try:
            json.dumps(value, allow_nan=False, sort_keys=True)
        except (TypeError, ValueError) as error:
            raise ValueError("response schema must be finite JSON") from error
        return value


class ModelTokenUsage(_WireModel):
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_total(self) -> Self:
        if self.total_tokens != self.prompt_tokens + self.completion_tokens:
            raise ValueError("total tokens must equal prompt plus completion tokens")
        return self


class ModelCallUsage(ModelTokenUsage):
    cost_usd: float = Field(ge=0, allow_inf_nan=False)


class StructuredModelResponse(_WireModel):
    provider: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_-]*$",
    )
    profile: ModelProfile
    requested_model: str = Field(min_length=1, max_length=256)
    actual_model: str = Field(min_length=1, max_length=256)
    request_id: str = Field(min_length=1, max_length=256)
    finish_reason: Literal["stop"]
    output: dict[str, Any]
    usage: ModelCallUsage


@dataclass(frozen=True, slots=True)
class ValidatedModelResponse(Generic[OutputT]):
    """Locally validated output paired with its provider accounting."""

    output: OutputT
    raw: StructuredModelResponse

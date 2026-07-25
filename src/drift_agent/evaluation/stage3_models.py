from __future__ import annotations

import math
from pathlib import PurePosixPath
from typing import Literal, Self

from pydantic import Field, JsonValue, field_validator, model_validator

from drift_agent.evaluation.models import (
    ChangedBytes,
    EvaluationModel,
    ExpectedResult,
    FixtureFile,
    MultisetMatch,
    ObservedFinding,
    Provenance,
    WorkspaceInput,
)
from drift_agent.model.contracts import ModelProfile

Stage3DatasetId = Literal["stage3-v1"]
Stage3CaseKind = Literal["executable", "semantic"]
Stage3ValidationDriver = Literal[
    "real",
    "timeout",
    "semantic_fail_once",
    "semantic_fail_twice",
]
Stage3RepairOutcome = Literal["not_applicable", "success", "abstained", "failed"]


def _safe_relative_path(value: str) -> bool:
    path = PurePosixPath(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts


class Stage3Budgets(EvaluationModel):
    """Frozen dataset representation of the public run budgets."""

    max_patch_attempts_per_finding: int = Field(default=2, ge=1, le=2)
    max_model_calls_per_run: int = Field(default=4, ge=0)
    max_input_tokens_per_run: int = Field(default=20_000, ge=0)
    max_validation_commands_per_run: int = Field(default=8, ge=0)
    timeout_seconds: float = 120.0

    @field_validator("timeout_seconds")
    @classmethod
    def validate_timeout(cls, value: float) -> float:
        if not math.isfinite(value) or value < 0:
            raise ValueError("timeout_seconds must be finite and non-negative")
        return value


class Stage3ModelStep(EvaluationModel):
    """One deterministic provider response and its request oracle."""

    profile: ModelProfile
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    output: dict[str, JsonValue]
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    cost_nano_usd: int = Field(ge=0)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class Stage3Accounting(EvaluationModel):
    """Exact per-case routing, attempt, command, token, and cost projection."""

    repair_outcome: Stage3RepairOutcome = "not_applicable"
    patch_attempts: int = Field(default=0, ge=0, le=2)
    model_calls_by_profile: dict[ModelProfile, int] = Field(default_factory=dict)
    validation_commands: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    known_cost_nano_usd: int = Field(default=0, ge=0)

    @field_validator("model_calls_by_profile")
    @classmethod
    def validate_profile_counts(
        cls,
        value: dict[ModelProfile, int],
    ) -> dict[ModelProfile, int]:
        if any(isinstance(count, bool) or count <= 0 for count in value.values()):
            raise ValueError("model profile counts must be positive integers")
        return dict(sorted(value.items()))

    @model_validator(mode="after")
    def validate_accounting_shape(self) -> Self:
        calls = self.model_calls
        if self.repair_outcome == "not_applicable" and self.patch_attempts != 0:
            raise ValueError("non-repair cases may not record patch attempts")
        if self.repair_outcome == "success" and self.patch_attempts not in {1, 2}:
            raise ValueError("successful semantic repair requires one or two patch attempts")
        if self.repair_outcome == "abstained" and self.patch_attempts != 2:
            raise ValueError("Stage 3 abstention requires exactly two failed patch attempts")
        if self.model_calls_by_profile.get("strong", 0) and not self.model_calls_by_profile.get(
            "fast", 0
        ):
            raise ValueError("strong routing requires an earlier fast call")
        if calls == 0 and any((self.input_tokens, self.output_tokens, self.known_cost_nano_usd)):
            raise ValueError("zero model calls require zero token and cost accounting")
        return self

    @property
    def model_calls(self) -> int:
        return sum(self.model_calls_by_profile.values())


class Stage3ExpectedResult(ExpectedResult):
    accounting: Stage3Accounting = Field(default_factory=Stage3Accounting)


class Stage3CaseManifest(EvaluationModel):
    schema_version: Literal[1]
    dataset_id: Stage3DatasetId
    case_id: str = Field(min_length=1)
    case_kind: Stage3CaseKind
    provenance: Provenance
    files: tuple[FixtureFile, ...]
    workspace: WorkspaceInput
    operation: Literal["check", "repair"]
    semantic_repair: bool = False
    budgets: Stage3Budgets = Field(default_factory=Stage3Budgets)
    validation_driver: Stage3ValidationDriver = "real"
    coverage_tags: tuple[str, ...]
    model_script: tuple[Stage3ModelStep, ...] = ()
    expected: Stage3ExpectedResult
    offline: Literal[True]

    @model_validator(mode="after")
    def validate_case_shape(self) -> Self:
        if not self.coverage_tags or any(not tag for tag in self.coverage_tags):
            raise ValueError("coverage tags must be non-empty")
        if len(set(self.coverage_tags)) != len(self.coverage_tags):
            raise ValueError("coverage tags must be unique")
        fixture_paths = [fixture.path for fixture in self.files]
        if len(set(fixture_paths)) != len(fixture_paths):
            raise ValueError("fixture file paths must be unique")
        role_targets = [(fixture.role, fixture.target_path) for fixture in self.files]
        if len(set(role_targets)) != len(role_targets):
            raise ValueError("each fixture role may define a target path only once")
        if self.provenance.kind != "project_authored":
            raise ValueError("stage3-v1 contains project-authored fixtures only")

        accounting = self.expected.accounting
        scripted_profiles: dict[ModelProfile, int] = {}
        for step in self.model_script:
            scripted_profiles[step.profile] = scripted_profiles.get(step.profile, 0) + 1
        scripted_profiles = dict(sorted(scripted_profiles.items()))
        profile_sequence = tuple(step.profile for step in self.model_script)
        if profile_sequence and profile_sequence[0] != "fast":
            raise ValueError("model routing must start with fast")
        if "strong" in profile_sequence:
            first_strong = profile_sequence.index("strong")
            if "fast" in profile_sequence[first_strong:]:
                raise ValueError("model routing may not return to fast after strong")
        if scripted_profiles != accounting.model_calls_by_profile:
            raise ValueError("model script profiles must equal the accounting oracle")
        if sum(step.prompt_tokens for step in self.model_script) != accounting.input_tokens:
            raise ValueError("model script prompt tokens must equal the accounting oracle")
        if sum(step.completion_tokens for step in self.model_script) != accounting.output_tokens:
            raise ValueError("model script completion tokens must equal the accounting oracle")
        if sum(step.cost_nano_usd for step in self.model_script) != (
            accounting.known_cost_nano_usd
        ):
            raise ValueError("model script cost must equal the accounting oracle")
        if len(self.model_script) > self.budgets.max_model_calls_per_run:
            raise ValueError("model script exceeds the configured call budget")
        if accounting.validation_commands > self.budgets.max_validation_commands_per_run:
            raise ValueError("validation oracle exceeds the configured command budget")

        if self.case_kind == "executable":
            if self.semantic_repair or self.model_script or accounting.model_calls:
                raise ValueError("executable evaluation cases must make zero model calls")
            if accounting.repair_outcome != "not_applicable":
                raise ValueError("executable cases are not semantic repair opportunities")
        else:
            if self.validation_driver == "timeout":
                raise ValueError("semantic cases may not use the executable timeout driver")
            if self.operation != "repair" or not self.semantic_repair:
                raise ValueError("semantic cases must explicitly request semantic repair")
            if accounting.repair_outcome == "not_applicable":
                raise ValueError("semantic repair cases require a scored repair outcome")
            if not self.model_script:
                raise ValueError("semantic repair cases require a deterministic model script")
        return self


class Stage3CatalogEntry(EvaluationModel):
    case_id: str = Field(min_length=1)
    manifest: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("manifest")
    @classmethod
    def validate_manifest_path(cls, value: str) -> str:
        if not _safe_relative_path(value):
            raise ValueError("catalog manifest paths must be relative")
        return PurePosixPath(value).as_posix()


class Stage3CatalogManifest(EvaluationModel):
    schema_version: Literal[1]
    dataset_id: Stage3DatasetId
    cases: tuple[Stage3CatalogEntry, ...]


class Stage3CatalogAudit(EvaluationModel):
    dataset_id: Stage3DatasetId
    case_ids: tuple[str, ...]
    fixture_files: int = Field(ge=0)
    fixture_bytes: int = Field(ge=0)
    coverage_tags: tuple[str, ...]


class Stage3CaseObservation(EvaluationModel):
    status: str = Field(min_length=1)
    findings: tuple[ObservedFinding, ...]
    changed_bytes: tuple[ChangedBytes, ...]
    accounting: Stage3Accounting
    network_calls: int = Field(ge=0)
    offline: bool
    model_script_consumed: bool


class Stage3CaseEvaluation(EvaluationModel):
    case_id: str
    case_kind: Stage3CaseKind
    passed: bool
    matching: MultisetMatch
    status_matches: bool
    outcomes_match: bool
    changed_bytes_match: bool
    no_extra_mutation: bool
    accounting_matches: bool
    executable_zero_model_compliance: bool
    offline_compliance: bool
    model_script_compliance: bool
    expected_status: str
    actual_status: str
    expected_accounting: Stage3Accounting
    actual_accounting: Stage3Accounting


class ExactRatio(EvaluationModel):
    numerator: int = Field(ge=0)
    denominator: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_ratio(self) -> Self:
        if self.numerator > self.denominator:
            raise ValueError("ratio numerator may not exceed denominator")
        return self


class Stage3EvaluationSummary(EvaluationModel):
    total: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    semantic_repair_opportunities: int = Field(ge=0)
    repair_success_at_1: ExactRatio
    repair_success_at_2: ExactRatio
    abstention_correctness: ExactRatio
    fast_route_ratio: ExactRatio
    strong_route_ratio: ExactRatio
    model_calls: int = Field(ge=0)
    validation_commands: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    known_cost_nano_usd: int = Field(ge=0)
    executable_zero_model_compliance: bool
    offline_compliance: bool
    model_script_compliance: bool


class Stage3EvaluationReport(EvaluationModel):
    dataset_id: Stage3DatasetId
    cases: tuple[Stage3CaseEvaluation, ...]
    summary: Stage3EvaluationSummary


__all__ = [
    "ExactRatio",
    "Stage3Accounting",
    "Stage3Budgets",
    "Stage3CaseEvaluation",
    "Stage3CaseKind",
    "Stage3CaseManifest",
    "Stage3CaseObservation",
    "Stage3CatalogAudit",
    "Stage3CatalogEntry",
    "Stage3CatalogManifest",
    "Stage3DatasetId",
    "Stage3EvaluationReport",
    "Stage3EvaluationSummary",
    "Stage3ExpectedResult",
    "Stage3ModelStep",
    "Stage3RepairOutcome",
    "Stage3ValidationDriver",
]

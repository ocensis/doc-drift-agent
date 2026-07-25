from __future__ import annotations

from pathlib import PurePosixPath
from typing import Annotated, Literal, Self, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from drift_agent.evaluation.catalog import FROZEN_MANIFEST_SHA256
from drift_agent.evaluation.stage3_catalog import FROZEN_STAGE3_MANIFEST_SHA256

Stage4DatasetId = Literal["structural-v1", "stage3-v1"]
ComparisonSubject = Literal["codex", "drift_agent"]
Stage4System: TypeAlias = ComparisonSubject
ComparisonLayer = Literal["structural", "executable", "semantic"]
ComparisonCompleteness = Literal["measured", "not_measured", "accounting_incomplete"]
Stage4Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Stage4PrefixedDigest = Annotated[
    str,
    Field(pattern=r"^sha256:[0-9a-f]{64}$"),
]

MEMORY_NOT_MEASURED_REASON = (
    "the paired datasets do not contain cross-run suppression, expiry/stale-reuse, "
    "or alias/decision-gain samples"
)
HYPOTHESIS_STATEMENT = (
    "Drift Agent quality is no lower than Codex while using less model context, "
    "tooling, cost, and wall-clock time"
)


class Stage4Model(BaseModel):
    """Strict, immutable DTO used by the offline comparison boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _validate_text(value: str, *, label: str) -> str:
    if value != value.strip():
        raise ValueError(f"{label} may not have surrounding whitespace")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{label} may not contain control characters")
    return value


class ComparisonBoolMetric(Stage4Model):
    """A nullable boolean whose absence can never be mistaken for false."""

    status: ComparisonCompleteness
    value: bool | None
    reason: str | None = Field(default=None, min_length=1, max_length=300)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str | None) -> str | None:
        return None if value is None else _validate_text(value, label="metric reason")

    @model_validator(mode="after")
    def validate_completeness(self) -> Self:
        if self.status == "measured":
            if self.value is None or self.reason is not None:
                raise ValueError("measured boolean requires a value and no reason")
        elif self.status == "not_measured":
            if self.value is not None or self.reason is None:
                raise ValueError("not_measured boolean requires null and a reason")
        elif self.reason is None:
            raise ValueError("accounting_incomplete boolean requires a reason")
        return self


class ComparisonOutcome(Stage4Model):
    passed: bool
    tp: int = Field(ge=0)
    fp: int = Field(ge=0)
    fn: int = Field(ge=0)
    successful_repair: bool
    repair_success_at_1: ComparisonBoolMetric
    repair_success_at_2: ComparisonBoolMetric
    correct_abstention: ComparisonBoolMetric

    @model_validator(mode="after")
    def validate_repair_order(self) -> Self:
        first = self.repair_success_at_1
        second = self.repair_success_at_2
        if (
            first.status == "measured"
            and second.status == "measured"
            and first.value is True
            and second.value is not True
        ):
            raise ValueError("repair success at 1 implies repair success at 2")
        return self


class ComparisonChangedBytes(Stage4Model):
    path: str = Field(min_length=1, max_length=500)
    before_sha256: Stage4Sha256 | None
    after_sha256: Stage4Sha256 | None

    @field_validator("path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        _validate_text(value, label="changed-byte path")
        path = PurePosixPath(value)
        if (
            "\\" in value
            or path.is_absolute()
            or value != path.as_posix()
            or value in {".", ".."}
            or ".." in path.parts
            or any(part in {"", "."} for part in path.parts)
        ):
            raise ValueError("changed-byte path must be a normalized repo-relative path")
        return value

    @model_validator(mode="after")
    def validate_change(self) -> Self:
        if self.before_sha256 is None and self.after_sha256 is None:
            raise ValueError("changed bytes require before or after bytes")
        if self.before_sha256 == self.after_sha256:
            raise ValueError("changed bytes must differ")
        return self


class ComparisonValidation(Stage4Model):
    status: ComparisonCompleteness
    passed: bool | None
    reason: str | None = Field(default=None, min_length=1, max_length=300)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str | None) -> str | None:
        return None if value is None else _validate_text(value, label="validation reason")

    @model_validator(mode="after")
    def validate_completeness(self) -> Self:
        if self.status == "measured":
            if self.passed is None or self.reason is not None:
                raise ValueError("measured validation requires a result and no reason")
        elif self.passed is not None or self.reason is None:
            raise ValueError("unmeasured validation requires null and a reason")
        return self


class ComparisonSafety(Stage4Model):
    status: ComparisonCompleteness
    regression_free: bool | None
    business_code_mutations: int | None = Field(ge=0)
    stale_overwrites: int | None = Field(ge=0)
    reason: str | None = Field(default=None, min_length=1, max_length=300)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str | None) -> str | None:
        return None if value is None else _validate_text(value, label="safety reason")

    @model_validator(mode="after")
    def validate_completeness(self) -> Self:
        values = (
            self.regression_free,
            self.business_code_mutations,
            self.stale_overwrites,
        )
        if self.status == "measured":
            if any(value is None for value in values) or self.reason is not None:
                raise ValueError("measured safety requires all values and no reason")
        elif self.status == "not_measured":
            if any(value is not None for value in values) or self.reason is None:
                raise ValueError("not_measured safety requires null values and a reason")
        elif self.reason is None or all(value is not None for value in values):
            raise ValueError("accounting_incomplete safety requires a reason and a missing value")
        return self


class ComparisonUsage(Stage4Model):
    status: ComparisonCompleteness
    model_calls: int | None = Field(ge=0)
    strong_model_calls: int | None = Field(ge=0)
    tool_calls: int | None = Field(ge=0)
    input_tokens: int | None = Field(ge=0)
    output_tokens: int | None = Field(ge=0)
    cost_nano_usd: int | None = Field(ge=0)
    duration_ms: int | None = Field(ge=0)
    reason: str | None = Field(default=None, min_length=1, max_length=300)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str | None) -> str | None:
        return None if value is None else _validate_text(value, label="usage reason")

    @model_validator(mode="after")
    def validate_completeness(self) -> Self:
        values = (
            self.model_calls,
            self.strong_model_calls,
            self.tool_calls,
            self.input_tokens,
            self.output_tokens,
            self.cost_nano_usd,
            self.duration_ms,
        )
        if self.status == "measured":
            if any(value is None for value in values) or self.reason is not None:
                raise ValueError("measured usage requires all values and no reason")
        elif self.status == "not_measured":
            if any(value is not None for value in values) or self.reason is None:
                raise ValueError("not_measured usage requires null values and a reason")
        elif self.reason is None or all(value is not None for value in values):
            raise ValueError("accounting_incomplete usage requires a reason and a missing value")
        if (
            self.model_calls is not None
            and self.strong_model_calls is not None
            and self.strong_model_calls > self.model_calls
        ):
            raise ValueError("strong model calls cannot exceed model calls")
        return self


class ComparisonProvenance(Stage4Model):
    runner_kind: Literal["local_offline_runner", "external_self_declared"]
    runner_version: str = Field(min_length=1, max_length=100)
    model_id: str = Field(min_length=1, max_length=150)
    tool_profile_digest: Stage4PrefixedDigest
    budget_source: str = Field(min_length=1, max_length=150)
    claim_status: Literal["locally_verified", "unverified_external_declaration"]
    authorization_status: Literal["not_applicable", "self_declared_not_verified"]

    @field_validator("runner_version", "model_id", "budget_source")
    @classmethod
    def validate_bounded_text(cls, value: str) -> str:
        return _validate_text(value, label="provenance text")


class ComparisonPairKey(Stage4Model):
    dataset_id: Stage4DatasetId
    case_id: str
    case_manifest_sha256: Stage4Sha256
    trial_id: str
    snapshot_digest: Stage4PrefixedDigest
    task_digest: Stage4PrefixedDigest
    scope_digest: Stage4PrefixedDigest


class ComparisonObservationV1(Stage4Model):
    """Normalized offline evidence. It cannot carry prompts or raw provider output."""

    schema_version: Literal[1]
    observation_id: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^obs_[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    subject: ComparisonSubject
    dataset_id: Stage4DatasetId
    case_id: str = Field(min_length=1, max_length=256)
    case_manifest_sha256: Stage4Sha256
    trial_id: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    snapshot_digest: Stage4PrefixedDigest
    task_digest: Stage4PrefixedDigest
    scope_digest: Stage4PrefixedDigest
    evidence_sha256: Stage4Sha256
    case_layer: ComparisonLayer
    outcome: ComparisonOutcome
    changed_bytes: tuple[ComparisonChangedBytes, ...]
    validation: ComparisonValidation
    safety: ComparisonSafety
    usage: ComparisonUsage
    provenance: ComparisonProvenance

    @field_validator("case_id")
    @classmethod
    def validate_case_id_text(cls, value: str) -> str:
        return _validate_text(value, label="case id")

    @model_validator(mode="after")
    def validate_frozen_contract(self) -> Self:
        manifests = (
            FROZEN_MANIFEST_SHA256
            if self.dataset_id == "structural-v1"
            else FROZEN_STAGE3_MANIFEST_SHA256
        )
        expected_manifest = manifests.get(self.case_id)
        if expected_manifest is None or self.case_manifest_sha256 != expected_manifest:
            raise ValueError("case id and manifest hash must match the frozen dataset")

        expected_layer: ComparisonLayer
        if self.dataset_id == "structural-v1":
            expected_layer = "structural"
        elif self.case_id.startswith("executable."):
            expected_layer = "executable"
        else:
            expected_layer = "semantic"
        if self.case_layer != expected_layer:
            raise ValueError("case layer does not match the frozen case")

        if len({change.path for change in self.changed_bytes}) != len(self.changed_bytes):
            raise ValueError("changed-byte paths must be unique")

        provenance = self.provenance
        if self.subject == "codex":
            required = (
                provenance.runner_kind == "external_self_declared"
                and provenance.claim_status == "unverified_external_declaration"
                and provenance.authorization_status == "self_declared_not_verified"
            )
            if not required:
                raise ValueError("Codex provenance must remain an unverified external declaration")
        else:
            required = (
                provenance.runner_kind == "local_offline_runner"
                and provenance.claim_status == "locally_verified"
                and provenance.authorization_status == "not_applicable"
            )
            if not required:
                raise ValueError("Drift Agent provenance must describe local offline replay")
        return self

    @property
    def pair_key(self) -> ComparisonPairKey:
        return ComparisonPairKey(
            dataset_id=self.dataset_id,
            case_id=self.case_id,
            case_manifest_sha256=self.case_manifest_sha256,
            trial_id=self.trial_id,
            snapshot_digest=self.snapshot_digest,
            task_digest=self.task_digest,
            scope_digest=self.scope_digest,
        )


Stage4Observation: TypeAlias = ComparisonObservationV1


class Stage4CompletenessCounts(Stage4Model):
    measured: int = Field(ge=0)
    not_measured: int = Field(ge=0)
    accounting_incomplete: int = Field(ge=0)


class Stage4MetricAggregate(Stage4Model):
    """Measured totals are kept separate from known incomplete subtotals."""

    measured_total: int | None = Field(ge=0)
    measured_count: int = Field(ge=0)
    incomplete_known_total: int | None = Field(ge=0)
    incomplete_known_count: int = Field(ge=0)
    incomplete_unknown_count: int = Field(ge=0)
    not_measured_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_totals(self) -> Self:
        if (self.measured_total is None) != (self.measured_count == 0):
            raise ValueError("measured total must be null exactly when its count is zero")
        if (self.incomplete_known_total is None) != (self.incomplete_known_count == 0):
            raise ValueError("incomplete subtotal must be null exactly when its count is zero")
        return self


class Stage4Ratio(Stage4Model):
    status: Literal["measured", "not_measured"]
    numerator: int | None = Field(ge=0)
    denominator: int | None = Field(ge=1)
    reason: str | None = Field(default=None, min_length=1, max_length=300)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str | None) -> str | None:
        return None if value is None else _validate_text(value, label="ratio reason")

    @model_validator(mode="after")
    def validate_ratio(self) -> Self:
        if self.status == "measured":
            if (
                self.numerator is None
                or self.denominator is None
                or self.numerator > self.denominator
                or self.reason is not None
            ):
                raise ValueError("measured ratio requires a valid fraction and no reason")
        elif self.numerator is not None or self.denominator is not None or self.reason is None:
            raise ValueError("not_measured ratio requires null fraction and a reason")
        return self


class Stage4Percentile(Stage4Model):
    status: Literal["measured", "not_measured"]
    value_ms: int | None = Field(ge=0)
    measured_count: int = Field(ge=0)
    reason: str | None = Field(default=None, min_length=1, max_length=300)

    @model_validator(mode="after")
    def validate_percentile(self) -> Self:
        if self.status == "measured":
            if self.value_ms is None or self.measured_count == 0 or self.reason is not None:
                raise ValueError("measured percentile requires samples and no reason")
        elif self.value_ms is not None or self.measured_count != 0 or self.reason is None:
            raise ValueError("not_measured percentile requires no samples and a reason")
        return self


class Stage4QualityAggregate(Stage4Model):
    observation_count: int = Field(ge=0)
    passed: Stage4Ratio
    tp: int = Field(ge=0)
    fp: int = Field(ge=0)
    fn: int = Field(ge=0)
    precision: Stage4Ratio
    recall: Stage4Ratio
    f1: Stage4Ratio
    repair_success_at_1: Stage4Ratio
    repair_success_at_2: Stage4Ratio
    correct_abstention: Stage4Ratio
    validation_pass: Stage4Ratio
    validation_completeness: Stage4CompletenessCounts
    regression_free: Stage4Ratio


class Stage4UsageAggregate(Stage4Model):
    completeness: Stage4CompletenessCounts
    model_calls: Stage4MetricAggregate
    strong_model_calls: Stage4MetricAggregate
    tool_calls: Stage4MetricAggregate
    input_tokens: Stage4MetricAggregate
    output_tokens: Stage4MetricAggregate
    cost_nano_usd: Stage4MetricAggregate
    duration_ms: Stage4MetricAggregate


class Stage4EfficiencyAggregate(Stage4Model):
    usage: Stage4UsageAggregate
    per_success: Stage4UsageAggregate
    wall_clock_p50: Stage4Percentile
    wall_clock_p95: Stage4Percentile
    strong_profile_ratio: Stage4Ratio


class Stage4SafetyAggregate(Stage4Model):
    completeness: Stage4CompletenessCounts
    business_code_mutations: Stage4MetricAggregate
    stale_overwrites: Stage4MetricAggregate


class Stage4SystemAggregate(Stage4Model):
    subject: ComparisonSubject
    status: Literal["measured", "pending"]
    imported_observation_count: int = Field(ge=0)
    paired_observation_count: int = Field(ge=0)
    quality: Stage4QualityAggregate
    efficiency: Stage4EfficiencyAggregate
    safety: Stage4SafetyAggregate

    @model_validator(mode="after")
    def validate_status(self) -> Self:
        expected = "measured" if self.paired_observation_count else "pending"
        if self.status != expected:
            raise ValueError("subject status must reflect paired observations")
        return self


class Stage4Pair(Stage4Model):
    key: ComparisonPairKey
    codex_observation_id: str
    drift_agent_observation_id: str


class Stage4Incomparable(Stage4Model):
    observation_id: str
    subject: ComparisonSubject
    key: ComparisonPairKey
    reason: Literal[
        "missing_codex",
        "missing_drift_agent",
        "pair_key_mismatch",
    ]


class Stage4StratumAggregate(Stage4Model):
    layer: ComparisonLayer
    paired_case_count: int = Field(ge=0)
    systems: tuple[Stage4SystemAggregate, ...]


class Stage4MemoryAggregate(Stage4Model):
    status: Literal["not_measured"]
    reason: str


class Stage4HypothesisAssessment(Stage4Model):
    statement: str
    status: Literal["not_measured", "insufficient_samples"]
    reason: str


class Stage4ComparisonReport(Stage4Model):
    schema_version: Literal[1]
    observations: tuple[ComparisonObservationV1, ...]
    pairs: tuple[Stage4Pair, ...]
    incomparable: tuple[Stage4Incomparable, ...]
    systems: tuple[Stage4SystemAggregate, ...]
    strata: tuple[Stage4StratumAggregate, ...]
    paired_case_count: int = Field(ge=0)
    comparison_complete: bool
    pending_subjects: tuple[ComparisonSubject, ...]
    memory: Stage4MemoryAggregate
    hypothesis: Stage4HypothesisAssessment


__all__ = [
    "HYPOTHESIS_STATEMENT",
    "MEMORY_NOT_MEASURED_REASON",
    "ComparisonBoolMetric",
    "ComparisonChangedBytes",
    "ComparisonCompleteness",
    "ComparisonLayer",
    "ComparisonObservationV1",
    "ComparisonOutcome",
    "ComparisonPairKey",
    "ComparisonProvenance",
    "ComparisonSafety",
    "ComparisonSubject",
    "ComparisonUsage",
    "ComparisonValidation",
    "Stage4ComparisonReport",
    "Stage4CompletenessCounts",
    "Stage4DatasetId",
    "Stage4EfficiencyAggregate",
    "Stage4HypothesisAssessment",
    "Stage4Incomparable",
    "Stage4MemoryAggregate",
    "Stage4MetricAggregate",
    "Stage4Observation",
    "Stage4Pair",
    "Stage4Percentile",
    "Stage4PrefixedDigest",
    "Stage4QualityAggregate",
    "Stage4Ratio",
    "Stage4SafetyAggregate",
    "Stage4Sha256",
    "Stage4StratumAggregate",
    "Stage4System",
    "Stage4SystemAggregate",
    "Stage4UsageAggregate",
]

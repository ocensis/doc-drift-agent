from __future__ import annotations

import ast
import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from enum import Enum
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from drift_agent.evaluation.catalog import FROZEN_CASE_IDS, FROZEN_MANIFEST_SHA256
from drift_agent.evaluation.stage3_catalog import (
    FROZEN_STAGE3_CASE_IDS,
    FROZEN_STAGE3_MANIFEST_SHA256,
)
from drift_agent.evaluation.stage3_models import Stage3CaseEvaluation
from drift_agent.evaluation.stage4_models import (
    ComparisonChangedBytes,
    ComparisonPairKey,
    ComparisonSubject,
    Stage4DatasetId,
)

BenchmarkSha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
BenchmarkPrefixedDigest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
BenchmarkOperation = Literal["check", "repair"]
BenchmarkRunClass = Literal["portable", "control"]
DeclaredStatus = Literal[
    "clean",
    "drift_found",
    "fixed",
    "partial",
    "needs_approval",
    "unresolved",
    "stale",
    "failed",
]
FindingFamily = Literal[
    "parameter_added",
    "parameter_removed",
    "parameter_default_changed",
    "parameter_annotation_changed",
    "return_annotation_changed",
    "symbol_renamed",
    "symbol_deleted",
    "google_arg_changed",
    "google_returns_changed",
    "broken_example",
    "semantic_literal_changed",
    "ambiguous_or_unsupported",
]
ComponentKind = Literal[
    "symbol",
    "parameter",
    "return",
    "doctest",
    "pytest",
    "semantic_literal",
    "unsupported",
]
NeutralValueKind = Literal[
    "missing",
    "present",
    "python_literal",
    "python_annotation",
    "symbol_fqn",
    "validation_status",
    "text",
]
ValidationStatus = Literal[
    "passed",
    "failed",
    "unavailable",
    "timeout",
    "budget_exhausted",
    "not_run",
]
TerminalClassification = Literal[
    "completed",
    "authorization_missing",
    "fixture_integrity_error",
    "runner_internal_error",
    "auth_failed",
    "model_unavailable",
    "rate_limited_or_provider_error",
    "runner_timeout",
    "output_limit",
    "invalid_jsonl",
    "missing_terminal_event",
    "invalid_final_schema",
    "secret_leakage_detected",
    "sandbox_denied",
    "tool_profile_violation",
    "unsafe_mutation",
    "scoreable_subject_failure",
    "needs_adjudication",
    "control_plane_incomparable",
]

PORTABLE_STAGE3_CASE_IDS = (
    "executable.doctest-pass.v1",
    "executable.doctest-fail.v1",
    "executable.pytest-pass.v1",
    "executable.pytest-fail.v1",
)
CONTROL_CASE_IDS = tuple(
    case_id for case_id in FROZEN_STAGE3_CASE_IDS if case_id not in PORTABLE_STAGE3_CASE_IDS
)
PORTABLE_CASE_IDS = FROZEN_CASE_IDS + PORTABLE_STAGE3_CASE_IDS
TRIAL_IDS_BY_COUNT: dict[int, tuple[str, ...]] = {
    1: ("trial-1",),
    3: ("trial-1", "trial-2", "trial-3"),
}


class BenchmarkModel(BaseModel):
    """Strict and immutable benchmark wire DTO."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _jsonable(value.model_dump(mode="json"))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("canonical JSON object keys must be strings")
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("canonical JSON forbids non-finite numbers")
    return value


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize a DTO or JSON-compatible value using the benchmark canonical form."""

    return json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def sha256_prefixed(value: Any) -> str:
    return f"sha256:{canonical_sha256(value)}"


def bytes_sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _validate_text(value: str, *, label: str, max_length: int) -> str:
    if not value or len(value) > max_length:
        raise ValueError(f"{label} must contain 1..{max_length} characters")
    if value != value.strip():
        raise ValueError(f"{label} may not have surrounding whitespace")
    if value != unicodedata.normalize("NFC", value):
        raise ValueError(f"{label} must use NFC Unicode normalization")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{label} may not contain control characters")
    return value


def validate_repo_relative_path(value: str) -> str:
    _validate_text(value, label="repository-relative path", max_length=500)
    path = PurePosixPath(value)
    if (
        "\\" in value
        or path.is_absolute()
        or value in {".", ".."}
        or ".." in path.parts
        or any(part in {"", "."} for part in path.parts)
        or path.as_posix() != value
    ):
        raise ValueError("path must be a canonical repository-relative POSIX path")
    return value


def validate_symbol_fqn(value: str) -> str:
    _validate_text(value, label="symbol FQN", max_length=300)
    if any(not part.isidentifier() for part in value.split(".")):
        raise ValueError("symbol FQN segments must be Python identifiers")
    return value


class NeutralValueV1(BenchmarkModel):
    kind: NeutralValueKind
    value: bool | int | str | None

    @model_validator(mode="after")
    def validate_tagged_value(self) -> Self:
        value = self.value
        if self.kind in {"missing", "present"}:
            if value is not None:
                raise ValueError(f"{self.kind} values must carry null")
            return self
        if self.kind == "python_literal":
            if isinstance(value, int) and not isinstance(value, bool):
                if not -(2**63) <= value <= 2**63 - 1:
                    raise ValueError("Python integer literal must be signed 64-bit")
            elif value is not None and not isinstance(value, (bool, str)):
                raise ValueError("unsupported typed Python literal")
            if isinstance(value, str):
                _validate_text(value, label="Python string literal", max_length=500)
            return self
        if not isinstance(value, str):
            raise ValueError(f"{self.kind} values must carry a string")
        if self.kind == "symbol_fqn":
            validate_symbol_fqn(value)
        elif self.kind == "validation_status":
            if value not in {
                "passed",
                "failed",
                "unavailable",
                "timeout",
                "budget_exhausted",
                "not_run",
            }:
                raise ValueError("unknown validation status")
        else:
            _validate_text(value, label=self.kind, max_length=500)
            if self.kind == "python_annotation" and (
                value.startswith("Constant(") or "ctx=Load()" in value
            ):
                raise ValueError("Python annotations may not use private AST dumps")
            if self.kind == "python_annotation":
                try:
                    canonical = ast.unparse(ast.parse(value, mode="eval").body)
                except SyntaxError as error:
                    raise ValueError("Python annotation must be a valid expression") from error
                if canonical != value:
                    raise ValueError("Python annotation must use canonical source spelling")
        return self


_FAMILY_COMPONENTS: dict[str, frozenset[str]] = {
    "parameter_added": frozenset({"parameter"}),
    "parameter_removed": frozenset({"parameter"}),
    "parameter_default_changed": frozenset({"parameter"}),
    "parameter_annotation_changed": frozenset({"parameter"}),
    "return_annotation_changed": frozenset({"return"}),
    "symbol_renamed": frozenset({"symbol"}),
    "symbol_deleted": frozenset({"symbol"}),
    "google_arg_changed": frozenset({"parameter"}),
    "google_returns_changed": frozenset({"return"}),
    "broken_example": frozenset({"doctest", "pytest"}),
    "semantic_literal_changed": frozenset({"semantic_literal"}),
    "ambiguous_or_unsupported": frozenset({"unsupported"}),
}


class NeutralFindingKeyV1(BenchmarkModel):
    code_path: str = Field(min_length=1, max_length=500)
    doc_path: str = Field(min_length=1, max_length=500)
    symbol_fqn: str | None = Field(max_length=300)
    finding_family: FindingFamily
    component_kind: ComponentKind
    component_name: str | None = Field(max_length=100)
    old_value: NeutralValueV1
    new_value: NeutralValueV1

    @field_validator("code_path", "doc_path")
    @classmethod
    def validate_paths(cls, value: str) -> str:
        return validate_repo_relative_path(value)

    @field_validator("symbol_fqn")
    @classmethod
    def validate_fqn(cls, value: str | None) -> str | None:
        return None if value is None else validate_symbol_fqn(value)

    @field_validator("component_name")
    @classmethod
    def validate_component_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_text(value, label="component name", max_length=100)

    @model_validator(mode="after")
    def validate_encoding(self) -> Self:
        allowed = _FAMILY_COMPONENTS[self.finding_family]
        if self.component_kind not in allowed:
            raise ValueError("component kind does not match finding family")
        named = self.component_kind in {"parameter", "semantic_literal"}
        if named != (self.component_name is not None):
            raise ValueError("only parameter and semantic-literal components require a name")
        if self.finding_family == "broken_example":
            if self.code_path != "drift-agent.toml" or self.symbol_fqn is not None:
                raise ValueError("broken examples use drift-agent.toml and no symbol FQN")
            if not (
                self.old_value.kind == "validation_status"
                and self.old_value.value == "passed"
                and self.new_value.kind == "validation_status"
                and self.new_value.value == "failed"
            ):
                raise ValueError("broken examples encode validation passed to failed")
        elif self.symbol_fqn is None:
            raise ValueError("non-executable findings require a symbol FQN")
        if self.finding_family == "parameter_added" and not (
            self.old_value.kind == "missing" and self.new_value.kind == "present"
        ):
            raise ValueError("parameter_added must encode missing to present")
        if self.finding_family == "parameter_removed" and not (
            self.old_value.kind == "present" and self.new_value.kind == "missing"
        ):
            raise ValueError("parameter_removed must encode present to missing")
        if self.finding_family == "symbol_renamed" and not (
            self.old_value.kind == "symbol_fqn" and self.new_value.kind == "symbol_fqn"
        ):
            raise ValueError("symbol_renamed must encode old and new FQNs")
        if self.finding_family == "symbol_deleted" and not (
            self.old_value.kind == "symbol_fqn" and self.new_value.kind == "missing"
        ):
            raise ValueError("symbol_deleted must encode an FQN to missing")
        if self.finding_family in {"parameter_annotation_changed", "return_annotation_changed"}:
            if self.old_value.kind not in {"missing", "python_annotation"} or (
                self.new_value.kind not in {"missing", "python_annotation"}
            ):
                raise ValueError("annotation findings require annotation-or-missing values")
        if self.finding_family == "parameter_default_changed" and (
            self.old_value.kind not in {"missing", "python_literal"}
            or self.new_value.kind not in {"missing", "python_literal"}
        ):
            raise ValueError("default findings require literal-or-missing values")
        if self.finding_family == "google_arg_changed" and (
            self.old_value.kind not in {"missing", "present", "python_annotation", "python_literal"}
            or self.new_value.kind
            not in {"missing", "present", "python_annotation", "python_literal"}
        ):
            raise ValueError("Google arg findings use public parameter value tags")
        if self.finding_family == "google_returns_changed" and (
            self.old_value.kind not in {"missing", "python_annotation"}
            or self.new_value.kind not in {"missing", "python_annotation"}
        ):
            raise ValueError("Google returns findings require annotation-or-missing values")
        if self.finding_family == "semantic_literal_changed" and not (
            self.old_value.kind == "python_literal" and self.new_value.kind == "python_literal"
        ):
            raise ValueError("semantic literal findings require typed Python literals")
        if self.finding_family == "ambiguous_or_unsupported" and (
            self.old_value.kind not in {"symbol_fqn", "text"}
            or self.new_value.kind not in {"symbol_fqn", "missing", "text"}
        ):
            raise ValueError("unsupported findings require bounded symbol/text values")
        return self


class NeutralFindingV1(NeutralFindingKeyV1):
    explanation: str = Field(min_length=1, max_length=300)

    @field_validator("explanation")
    @classmethod
    def validate_explanation(cls, value: str) -> str:
        return _validate_text(value, label="finding explanation", max_length=300)

    @property
    def key(self) -> NeutralFindingKeyV1:
        return NeutralFindingKeyV1.model_validate(
            self.model_dump(mode="python", exclude={"explanation"})
        )


class ValidationClaimV1(BenchmarkModel):
    check_kind: Literal["configured", "doctest", "pytest", "ast_equivalence"]
    target: str = Field(min_length=1, max_length=500)
    declared_status: ValidationStatus
    summary: str = Field(min_length=1, max_length=300)

    @field_validator("target")
    @classmethod
    def validate_target(cls, value: str) -> str:
        return validate_repo_relative_path(value)

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, value: str) -> str:
        return _validate_text(value, label="validation summary", max_length=300)


class BenchmarkTaskV1(BenchmarkModel):
    protocol_version: Literal[1] = 1
    operation: BenchmarkOperation
    baseline: Literal["HEAD"] = "HEAD"
    scope: Literal["current_worktree_changes"] = "current_worktree_changes"
    docs_only: Literal[True] = True
    report_findings: Literal[True] = True
    run_configured_validation: Literal[True] = True
    abstain_on_insufficient_evidence: Literal[True] = True
    network: Literal[False] = False
    dependency_install: Literal[False] = False
    git_mutation: Literal[False] = False

    @property
    def digest(self) -> str:
        return sha256_prefixed(self)


class CodexTaskResultV1(BenchmarkModel):
    schema_version: Literal[1]
    declared_status: DeclaredStatus
    findings: tuple[NeutralFindingV1, ...] = Field(max_length=64)
    validation_claims: tuple[ValidationClaimV1, ...] = Field(max_length=16)

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        keys = [canonical_json_bytes(finding.key) for finding in self.findings]
        if len(set(keys)) != len(keys):
            raise ValueError("finding keys must be unique; explanation is not identity")
        claim_keys = [(claim.check_kind, claim.target) for claim in self.validation_claims]
        if len(set(claim_keys)) != len(claim_keys):
            raise ValueError("validation claim kinds and targets must be unique")
        if self.declared_status == "clean" and self.findings:
            raise ValueError("clean result may not contain findings")
        return self

    def validate_for_task(self, task: BenchmarkTaskV1) -> None:
        allowed = (
            {"clean", "drift_found", "unresolved", "failed"}
            if task.operation == "check"
            else {
                "clean",
                "fixed",
                "partial",
                "needs_approval",
                "unresolved",
                "stale",
                "failed",
            }
        )
        if self.declared_status not in allowed:
            raise ValueError(f"status {self.declared_status!r} is invalid for {task.operation}")


class NeutralSubjectResultV1(BenchmarkModel):
    """Subject output after both adapters have projected to the neutral ontology."""

    schema_version: Literal[1] = 1
    operation: BenchmarkOperation
    status: DeclaredStatus
    findings: tuple[NeutralFindingKeyV1, ...] = Field(default=(), max_length=64)
    validation_claims: tuple[ValidationClaimV1, ...] = Field(default=(), max_length=16)
    derived_abstention: bool

    @model_validator(mode="after")
    def validate_subject_result(self) -> Self:
        keys = [canonical_json_bytes(finding) for finding in self.findings]
        if len(set(keys)) != len(keys):
            raise ValueError("neutral subject finding keys must be unique")
        allowed = (
            {"clean", "drift_found", "unresolved", "failed"}
            if self.operation == "check"
            else {
                "clean",
                "fixed",
                "partial",
                "needs_approval",
                "unresolved",
                "stale",
                "failed",
            }
        )
        if self.status not in allowed:
            raise ValueError("neutral status is invalid for the operation")
        if self.operation == "check" and self.derived_abstention:
            raise ValueError("check results never participate in repair abstention")
        if self.status in {"clean", "drift_found", "fixed", "partial"} and (
            self.derived_abstention
        ):
            raise ValueError("positive or partial results cannot be abstentions")
        return self


_NEUTRAL_ENCODING_V1 = {
    "schema_version": 1,
    "identity_fields": (
        "code_path",
        "doc_path",
        "symbol_fqn",
        "finding_family",
        "component_kind",
        "component_name",
        "old_value",
        "new_value",
    ),
    "explanation_is_identity": False,
    "finding_families": tuple(_FAMILY_COMPONENTS),
    "family_components": {
        family: tuple(sorted(components)) for family, components in _FAMILY_COMPONENTS.items()
    },
    "family_value_rules": {
        "parameter_added": ("missing", "present"),
        "parameter_removed": ("present", "missing"),
        "parameter_default_changed": (
            "python_literal_or_missing",
            "python_literal_or_missing",
        ),
        "parameter_annotation_changed": (
            "python_annotation_or_missing",
            "python_annotation_or_missing",
        ),
        "return_annotation_changed": (
            "python_annotation_or_missing",
            "python_annotation_or_missing",
        ),
        "symbol_renamed": ("symbol_fqn", "symbol_fqn"),
        "symbol_deleted": ("symbol_fqn", "missing"),
        "google_arg_changed": (
            "present_missing_annotation_or_literal",
            "present_missing_annotation_or_literal",
        ),
        "google_returns_changed": (
            "python_annotation_or_missing",
            "python_annotation_or_missing",
        ),
        "broken_example": ("validation_passed", "validation_failed"),
        "semantic_literal_changed": ("python_literal", "python_literal"),
        "ambiguous_or_unsupported": (
            "symbol_fqn_or_bounded_text",
            "symbol_fqn_missing_or_bounded_text",
        ),
    },
    "component_name_rules": {
        "parameter": "required_bounded_human_identifier",
        "semantic_literal": "required_bounded_human_identifier",
        "all_other_component_kinds": "null",
    },
    "path_rules": {
        "all_paths": "canonical_repo_relative_posix_nfc",
        "broken_example_code_path": "drift-agent.toml",
        "symlink_escape": "forbidden_by_runner",
    },
    "symbol_rules": {
        "broken_example": "null",
        "all_other_families": "required_canonical_dotted_python_fqn",
        "renamed_symbol": "current_fqn",
        "deleted_symbol": "old_fqn",
    },
    "bounds": {
        "path_characters": 500,
        "symbol_fqn_characters": 300,
        "component_name_characters": 100,
        "explanation_characters": 300,
        "python_literal_text_characters": 500,
        "findings": 64,
        "validation_claims": 16,
    },
    "python_literal_types": ("null", "bool", "signed_int64", "nfc_string"),
    "python_annotation": "canonical_source_expression_not_private_ast_dump",
    "value_kinds": (
        "missing",
        "present",
        "python_literal",
        "python_annotation",
        "symbol_fqn",
        "validation_status",
        "text",
    ),
}


def neutral_finding_encoding_bytes() -> bytes:
    return canonical_json_bytes(_NEUTRAL_ENCODING_V1)


def neutral_finding_encoding_sha256() -> str:
    return bytes_sha256(neutral_finding_encoding_bytes())


def codex_task_result_schema_bytes() -> bytes:
    return canonical_json_bytes(CodexTaskResultV1.model_json_schema())


def codex_task_result_schema_sha256() -> str:
    return bytes_sha256(codex_task_result_schema_bytes())


class NeutralOracleProjectionV1(BenchmarkModel):
    schema_version: Literal[1] = 1
    encoding_sha256: BenchmarkSha256
    projection_version: Literal[1] = 1
    dataset_id: Stage4DatasetId
    case_id: str = Field(min_length=1, max_length=256)
    case_manifest_sha256: BenchmarkSha256
    operation: BenchmarkOperation
    expected_status: DeclaredStatus
    findings: tuple[NeutralFindingKeyV1, ...] = Field(default=(), max_length=64)
    expected_changed_bytes: tuple[ComparisonChangedBytes, ...] = Field(default=(), max_length=64)

    @model_validator(mode="after")
    def validate_projection(self) -> Self:
        if self.encoding_sha256 != neutral_finding_encoding_sha256():
            raise ValueError("oracle projection encoding digest is not NeutralFindingEncodingV1")
        expected_manifests = (
            FROZEN_MANIFEST_SHA256
            if self.dataset_id == "structural-v1"
            else FROZEN_STAGE3_MANIFEST_SHA256
        )
        if expected_manifests.get(self.case_id) != self.case_manifest_sha256:
            raise ValueError("oracle case and manifest hash do not match the frozen dataset")
        if self.case_id not in PORTABLE_CASE_IDS:
            raise ValueError("neutral oracle projections are limited to the portable suite")
        expected_operation = "repair" if self.dataset_id == "structural-v1" else "check"
        if self.operation != expected_operation:
            raise ValueError("oracle operation differs from the portable case contract")
        allowed_statuses = (
            {"clean", "drift_found", "unresolved", "failed"}
            if self.operation == "check"
            else {
                "clean",
                "fixed",
                "partial",
                "needs_approval",
                "unresolved",
                "stale",
                "failed",
            }
        )
        if self.expected_status not in allowed_statuses:
            raise ValueError("oracle status is invalid for its operation")
        keys = [canonical_json_bytes(finding) for finding in self.findings]
        if len(set(keys)) != len(keys):
            raise ValueError("oracle finding keys must be unique")
        paths = [change.path for change in self.expected_changed_bytes]
        if len(set(paths)) != len(paths):
            raise ValueError("oracle changed-byte paths must be unique")
        return self


class BenchmarkCaseSelectionV1(BenchmarkModel):
    dataset_id: Stage4DatasetId
    case_id: str = Field(min_length=1, max_length=256)
    case_manifest_sha256: BenchmarkSha256
    operation: BenchmarkOperation

    @model_validator(mode="after")
    def validate_frozen_case(self) -> Self:
        manifests = (
            FROZEN_MANIFEST_SHA256
            if self.dataset_id == "structural-v1"
            else FROZEN_STAGE3_MANIFEST_SHA256
        )
        if manifests.get(self.case_id) != self.case_manifest_sha256:
            raise ValueError("selected case does not match its frozen manifest")
        expected_operation = "repair" if self.dataset_id == "structural-v1" else "check"
        if self.case_id.startswith("semantic."):
            expected_operation = "repair"
        if self.operation != expected_operation:
            raise ValueError("selected operation differs from the frozen manifest")
        return self


def fixed_benchmark_case_selections() -> tuple[
    tuple[BenchmarkCaseSelectionV1, ...], tuple[BenchmarkCaseSelectionV1, ...]
]:
    portable = tuple(
        BenchmarkCaseSelectionV1(
            dataset_id="structural-v1",
            case_id=case_id,
            case_manifest_sha256=FROZEN_MANIFEST_SHA256[case_id],
            operation="repair",
        )
        for case_id in FROZEN_CASE_IDS
    ) + tuple(
        BenchmarkCaseSelectionV1(
            dataset_id="stage3-v1",
            case_id=case_id,
            case_manifest_sha256=FROZEN_STAGE3_MANIFEST_SHA256[case_id],
            operation="check",
        )
        for case_id in PORTABLE_STAGE3_CASE_IDS
    )
    controls = tuple(
        BenchmarkCaseSelectionV1(
            dataset_id="stage3-v1",
            case_id=case_id,
            case_manifest_sha256=FROZEN_STAGE3_MANIFEST_SHA256[case_id],
            operation="repair" if case_id.startswith("semantic.") else "check",
        )
        for case_id in CONTROL_CASE_IDS
    )
    return portable, controls


class BenchmarkDatasetCatalogV1(BenchmarkModel):
    dataset_id: Stage4DatasetId
    catalog_sha256: BenchmarkSha256


class BenchmarkScheduleSlotV1(BenchmarkModel):
    ordinal: int = Field(ge=1, le=1_000)
    slot_id: str = Field(pattern=r"^slot-[0-9]{3}$")
    run_class: BenchmarkRunClass
    dataset_id: Stage4DatasetId
    case_id: str = Field(min_length=1, max_length=256)
    trial_id: str = Field(pattern=r"^(trial-[1-3]|control-1)$")
    subject: ComparisonSubject


def build_benchmark_schedule(
    *,
    portable_cases: Sequence[BenchmarkCaseSelectionV1],
    control_cases: Sequence[BenchmarkCaseSelectionV1],
    trial_ids: Sequence[str],
    shuffle_seed: int,
) -> tuple[BenchmarkScheduleSlotV1, ...]:
    pairs = [(case, trial_id) for case in portable_cases for trial_id in trial_ids]
    pairs.sort(
        key=lambda item: canonical_sha256(
            {
                "shuffle_seed": shuffle_seed,
                "dataset_id": item[0].dataset_id,
                "case_id": item[0].case_id,
                "trial_id": item[1],
            }
        )
    )
    material: list[tuple[BenchmarkRunClass, BenchmarkCaseSelectionV1, str, ComparisonSubject]] = []
    for index, (case, trial_id) in enumerate(pairs):
        first: ComparisonSubject = "codex" if (shuffle_seed + index) % 2 == 0 else "drift_agent"
        second: ComparisonSubject = "drift_agent" if first == "codex" else "codex"
        material.extend(
            (
                ("portable", case, trial_id, first),
                ("portable", case, trial_id, second),
            )
        )
    ordered_controls = sorted(
        control_cases,
        key=lambda case: canonical_sha256(
            {"shuffle_seed": shuffle_seed, "control_case_id": case.case_id}
        ),
    )
    material.extend(("control", case, "control-1", "drift_agent") for case in ordered_controls)
    return tuple(
        BenchmarkScheduleSlotV1(
            ordinal=index,
            slot_id=f"slot-{index:03d}",
            run_class=run_class,
            dataset_id=case.dataset_id,
            case_id=case.case_id,
            trial_id=trial_id,
            subject=subject,
        )
        for index, (run_class, case, trial_id, subject) in enumerate(material, start=1)
    )


class BenchmarkContractDigestsV1(BenchmarkModel):
    task_protocol_version: Literal[1] = 1
    neutral_encoding_sha256: BenchmarkSha256
    neutral_projection_version: Literal[1] = 1
    neutral_projection_table_sha256: BenchmarkSha256
    codex_output_schema_sha256: BenchmarkSha256
    schema_bundle_sha256: BenchmarkSha256
    prompt_renderer_version: str = Field(min_length=1, max_length=100)
    prompt_renderer_sha256: BenchmarkSha256
    scorer_version: str = Field(min_length=1, max_length=100)
    scorer_contract_sha256: BenchmarkSha256

    @field_validator("prompt_renderer_version", "scorer_version")
    @classmethod
    def validate_versions(cls, value: str) -> str:
        return _validate_text(value, label="contract version", max_length=100)

    @model_validator(mode="after")
    def validate_public_encoding(self) -> Self:
        if self.neutral_encoding_sha256 != neutral_finding_encoding_sha256():
            raise ValueError("plan uses an unknown neutral finding encoding")
        return self


class BenchmarkCodexRuntimeV1(BenchmarkModel):
    cli_version: str = Field(min_length=1, max_length=100)
    binary_sha256: BenchmarkSha256
    model_id: str = Field(min_length=1, max_length=150)
    reasoning_effort: Literal["low", "medium", "high", "xhigh"]

    @field_validator("cli_version")
    @classmethod
    def validate_cli_version(cls, value: str) -> str:
        return _validate_text(value, label="Codex CLI version", max_length=100)

    @field_validator("model_id")
    @classmethod
    def validate_model_id(cls, value: str) -> str:
        return _validate_text(value, label="Codex model ID", max_length=150)


class BenchmarkDriftRuntimeV1(BenchmarkModel):
    agent_version: str = Field(min_length=1, max_length=100)
    wheel_sha256: BenchmarkSha256
    runtime_lock_sha256: BenchmarkSha256

    @field_validator("agent_version")
    @classmethod
    def validate_agent_version(cls, value: str) -> str:
        return _validate_text(value, label="Drift Agent version", max_length=100)


class BenchmarkToolchainV1(BenchmarkModel):
    container_image_sha256: BenchmarkSha256
    runtime_toolchain_sha256: BenchmarkSha256
    python_version: str = Field(min_length=1, max_length=100)
    python_executable_sha256: BenchmarkSha256
    git_version: str = Field(min_length=1, max_length=100)
    git_executable_sha256: BenchmarkSha256
    pytest_version: str = Field(min_length=1, max_length=100)
    pytest_executable_sha256: BenchmarkSha256
    distributions_sha256: BenchmarkSha256
    plugin_set_sha256: BenchmarkSha256
    supervisor_namespace_sha256: BenchmarkSha256
    codex_namespace_sha256: BenchmarkSha256
    drift_namespace_sha256: BenchmarkSha256

    @field_validator("python_version", "git_version", "pytest_version")
    @classmethod
    def validate_tool_version(cls, value: str) -> str:
        return _validate_text(value, label="toolchain version", max_length=100)


class BenchmarkToolProfileV1(BenchmarkModel):
    sandbox: Literal["codex-permission-profile-v1"] = "codex-permission-profile-v1"
    permission_profile: Literal["benchmark"] = "benchmark"
    filesystem_default: Literal["deny"] = "deny"
    system_temp_denied: Literal[False] = False
    system_temp_policy: Literal["platform-carveout-no-benchmark-data"] = (
        "platform-carveout-no-benchmark-data"
    )
    approval: Literal["never"] = "never"
    web_search: Literal["disabled"] = "disabled"
    multi_agent: Literal[False] = False
    spawned_command_network: Literal[False] = False
    shell_environment_inherit: Literal["none"] = "none"


class BenchmarkLimitsV1(BenchmarkModel):
    hard_wall_timeout_seconds: int = Field(default=120, ge=1, le=3_600)
    maximum_live_invocations: int = Field(ge=1, le=36)
    max_raw_stream_bytes: int = Field(default=1_048_576, ge=1, le=16_777_216)
    max_stderr_bytes: int = Field(default=262_144, ge=1, le=4_194_304)
    max_command_output_bytes: int = Field(default=262_144, ge=1, le=4_194_304)
    max_artifact_bytes: int = Field(default=16_777_216, ge=1, le=67_108_864)
    max_evidence_bytes: int = Field(default=4_194_304, ge=1, le=16_777_216)

    @model_validator(mode="after")
    def validate_limits(self) -> Self:
        if self.max_evidence_bytes > self.max_artifact_bytes:
            raise ValueError("evidence byte cap may not exceed artifact byte cap")
        return self


class BenchmarkPlanV1(BenchmarkModel):
    schema_version: Literal[1] = 1
    suite_id: Literal["portable-v1"] = "portable-v1"
    dataset_catalogs: tuple[BenchmarkDatasetCatalogV1, ...] = Field(min_length=2, max_length=2)
    portable_cases: tuple[BenchmarkCaseSelectionV1, ...] = Field(min_length=12, max_length=12)
    control_cases: tuple[BenchmarkCaseSelectionV1, ...] = Field(min_length=6, max_length=6)
    trial_ids: tuple[str, ...] = Field(min_length=1, max_length=3)
    shuffle_seed: int = Field(ge=0, le=2**63 - 1)
    schedule: tuple[BenchmarkScheduleSlotV1, ...] = Field(min_length=30, max_length=78)
    contracts: BenchmarkContractDigestsV1
    codex: BenchmarkCodexRuntimeV1
    drift_agent: BenchmarkDriftRuntimeV1
    toolchain: BenchmarkToolchainV1
    tool_profile: BenchmarkToolProfileV1 = Field(default_factory=BenchmarkToolProfileV1)
    limits: BenchmarkLimitsV1
    budget_source: str = Field(min_length=1, max_length=150)

    @field_validator("trial_ids")
    @classmethod
    def validate_trial_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        expected = TRIAL_IDS_BY_COUNT.get(len(value))
        if expected is None or value != expected:
            raise ValueError("trial IDs must be the fixed smoke or full sequence")
        return value

    @model_validator(mode="after")
    def validate_plan(self) -> Self:
        if tuple(item.dataset_id for item in self.dataset_catalogs) != (
            "structural-v1",
            "stage3-v1",
        ):
            raise ValueError("dataset catalogs must be structural-v1 then stage3-v1")
        portable, controls = fixed_benchmark_case_selections()
        if self.portable_cases != portable or self.control_cases != controls:
            raise ValueError("plan must use the frozen 12 paired and 6 control cases")
        expected_schedule = build_benchmark_schedule(
            portable_cases=portable,
            control_cases=controls,
            trial_ids=self.trial_ids,
            shuffle_seed=self.shuffle_seed,
        )
        if self.schedule != expected_schedule:
            raise ValueError("schedule is not the deterministic schedule for this plan")
        if self.limits.maximum_live_invocations != len(portable) * len(self.trial_ids):
            raise ValueError("maximum live invocations must equal portable cases times trials")
        _validate_text(self.budget_source, label="budget source", max_length=150)
        return self

    @property
    def plan_digest(self) -> str:
        return canonical_sha256(self)


class BenchmarkAuthorizationV1(BenchmarkModel):
    schema_version: Literal[1] = 1
    plan_digest: BenchmarkSha256
    authorized: Literal[True] = True
    authorization_method: Literal["explicit_cli_flag"] = "explicit_cli_flag"
    maximum_live_invocations: int = Field(ge=1, le=36)
    hard_token_cap_available: bool
    hard_cost_cap_available: bool
    authorized_by: str = Field(min_length=1, max_length=200)
    authorized_at: str = Field(min_length=1, max_length=100)

    @field_validator("authorized_by", "authorized_at")
    @classmethod
    def validate_authorization_text(cls, value: str) -> str:
        return _validate_text(value, label="authorization text", max_length=200)


class BoundedStreamReceiptV1(BenchmarkModel):
    stream_name: Literal["stdout", "stderr", "events", "final"]
    total_bytes: int = Field(ge=0)
    captured_bytes: int = Field(ge=0)
    byte_limit: int = Field(ge=1)
    truncated: bool
    raw_sha256: BenchmarkSha256
    redacted_sha256: BenchmarkSha256
    replacement_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_capture(self) -> Self:
        if self.captured_bytes > self.total_bytes or self.captured_bytes > self.byte_limit:
            raise ValueError("captured stream bytes exceed total bytes or byte limit")
        if self.truncated != (self.captured_bytes < self.total_bytes):
            raise ValueError("stream truncation must exactly describe uncaptured bytes")
        return self


class TerminalReceiptV1(BenchmarkModel):
    schema_version: Literal[1] = 1
    plan_digest: BenchmarkSha256
    slot_id: str = Field(pattern=r"^slot-[0-9]{3}$")
    run_class: BenchmarkRunClass
    subject: ComparisonSubject
    dataset_id: Stage4DatasetId
    case_id: str = Field(min_length=1, max_length=256)
    trial_id: str = Field(pattern=r"^(trial-[1-3]|control-1)$")
    process_started: bool
    terminal_classification: TerminalClassification
    exit_code: int | None = Field(default=None, ge=0, le=255)
    signal: int | None = Field(default=None, ge=1, le=255)
    timed_out: bool = False
    duration_ms: int | None = Field(default=None, ge=0)
    streams: tuple[BoundedStreamReceiptV1, ...] = Field(default=(), max_length=4)
    available_artifacts: tuple[str, ...] = Field(default=(), max_length=32)

    @field_validator("available_artifacts")
    @classmethod
    def validate_artifacts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(value)) or len(set(value)) != len(value):
            raise ValueError("available artifacts must be unique and sorted")
        for artifact in value:
            validate_repo_relative_path(artifact)
        return value

    @model_validator(mode="after")
    def validate_terminal(self) -> Self:
        if self.exit_code is not None and self.signal is not None:
            raise ValueError("process cannot have both an exit code and a signal")
        if not self.process_started and any(
            value is not None for value in (self.exit_code, self.signal, self.duration_ms)
        ):
            raise ValueError("an unstarted process cannot have process terminal values")
        if self.timed_out != (self.terminal_classification == "runner_timeout"):
            raise ValueError("timeout flag and terminal classification must agree")
        names = [stream.stream_name for stream in self.streams]
        if len(set(names)) != len(names):
            raise ValueError("stream receipts must be unique by name")
        if self.run_class == "control" and (
            self.subject != "drift_agent" or self.trial_id != "control-1"
        ):
            raise ValueError("control receipts are one-shot Drift Agent runs")
        if self.run_class == "portable" and self.trial_id == "control-1":
            raise ValueError("portable receipts require a paired trial ID")
        return self


class RawUsageMetricV1(BenchmarkModel):
    status: Literal["measured", "not_measured", "accounting_incomplete"]
    value: int | None = Field(default=None, ge=0)
    evidence_source: str | None = Field(default=None, min_length=1, max_length=200)
    reason: str | None = Field(default=None, min_length=1, max_length=300)

    @field_validator("evidence_source")
    @classmethod
    def validate_source(cls, value: str | None) -> str | None:
        return (
            None
            if value is None
            else _validate_text(value, label="usage evidence source", max_length=200)
        )

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str | None) -> str | None:
        return (
            None
            if value is None
            else _validate_text(value, label="usage completeness reason", max_length=300)
        )

    @model_validator(mode="after")
    def validate_completeness(self) -> Self:
        if self.status == "measured":
            if self.value is None or self.evidence_source is None or self.reason is not None:
                raise ValueError("measured usage requires value/source and no reason")
        elif self.status == "not_measured":
            if self.value is not None or self.evidence_source is not None or self.reason is None:
                raise ValueError("not_measured usage requires only a reason")
        elif self.reason is None or (self.value is not None) != (self.evidence_source is not None):
            raise ValueError("incomplete usage requires a reason and sourced known value")
        return self


class RawUsageEvidenceV1(BenchmarkModel):
    model_calls: RawUsageMetricV1
    strong_model_calls: RawUsageMetricV1
    tool_calls: RawUsageMetricV1
    input_tokens: RawUsageMetricV1
    output_tokens: RawUsageMetricV1
    cost_nano_usd: RawUsageMetricV1
    duration_ms: RawUsageMetricV1


class RedactionReceiptV1(BenchmarkModel):
    policy_version: str = Field(min_length=1, max_length=100)
    replacement_count: int = Field(ge=0)
    secret_detected: bool

    @field_validator("policy_version")
    @classmethod
    def validate_policy_version(cls, value: str) -> str:
        return _validate_text(value, label="redaction policy version", max_length=100)


class RawRunEvidenceV1(BenchmarkModel):
    schema_version: Literal[1] = 1
    plan_digest: BenchmarkSha256
    authorization_ledger_sha256: BenchmarkSha256
    subject: ComparisonSubject
    dataset_id: Stage4DatasetId
    case_id: str = Field(min_length=1, max_length=256)
    trial_id: str = Field(pattern=r"^trial-[1-3]$")
    case_manifest_sha256: BenchmarkSha256
    snapshot_digest: BenchmarkPrefixedDigest
    task_digest: BenchmarkPrefixedDigest
    scope_digest: BenchmarkPrefixedDigest
    tool_profile_digest: BenchmarkPrefixedDigest
    runner_version: str = Field(min_length=1, max_length=100)
    runner_binary_sha256: BenchmarkSha256
    model_id: str = Field(min_length=1, max_length=150)
    effective_request_sha256: BenchmarkSha256
    rendered_input_sha256: BenchmarkSha256
    terminal: TerminalReceiptV1
    pre_snapshot_digest: BenchmarkPrefixedDigest
    post_snapshot_digest: BenchmarkPrefixedDigest
    pre_git_metadata_sha256: BenchmarkSha256
    post_git_metadata_sha256: BenchmarkSha256
    streams: tuple[BoundedStreamReceiptV1, ...] = Field(min_length=2, max_length=4)
    redaction: RedactionReceiptV1
    final_result_sha256: BenchmarkSha256 | None = None
    validation_receipt_sha256: tuple[BenchmarkSha256, ...] = Field(default=(), max_length=16)
    usage: RawUsageEvidenceV1

    @field_validator("runner_version")
    @classmethod
    def validate_runner_version(cls, value: str) -> str:
        return _validate_text(value, label="runner version", max_length=100)

    @field_validator("model_id")
    @classmethod
    def validate_evidence_model_id(cls, value: str) -> str:
        return _validate_text(value, label="evidence model ID", max_length=150)

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        terminal = self.terminal
        identity = (
            terminal.plan_digest == self.plan_digest
            and terminal.run_class == "portable"
            and terminal.subject == self.subject
            and terminal.dataset_id == self.dataset_id
            and terminal.case_id == self.case_id
            and terminal.trial_id == self.trial_id
        )
        if not identity:
            raise ValueError("terminal receipt identity differs from raw evidence")
        expected_manifests = (
            FROZEN_MANIFEST_SHA256
            if self.dataset_id == "structural-v1"
            else FROZEN_STAGE3_MANIFEST_SHA256
        )
        if expected_manifests.get(self.case_id) != self.case_manifest_sha256:
            raise ValueError("raw evidence does not bind a frozen case manifest")
        names = [stream.stream_name for stream in self.streams]
        if len(set(names)) != len(names):
            raise ValueError("raw evidence streams must be unique")
        if self.streams != self.terminal.streams:
            raise ValueError("raw evidence streams must equal the terminal capture receipts")
        if self.redaction.replacement_count != sum(
            stream.replacement_count for stream in self.streams
        ):
            raise ValueError("redaction replacement count must equal the stream receipts")
        if self.redaction.secret_detected != (
            self.terminal.terminal_classification == "secret_leakage_detected"
        ):
            raise ValueError("secret detection and terminal classification must agree")
        if self.validation_receipt_sha256 != tuple(sorted(self.validation_receipt_sha256)):
            raise ValueError("validation receipt digests must be sorted")
        if len(set(self.validation_receipt_sha256)) != len(self.validation_receipt_sha256):
            raise ValueError("validation receipt digests must be unique")
        return self

    @property
    def evidence_sha256(self) -> str:
        return canonical_sha256(self)


class CoverageEntryV1(BenchmarkModel):
    slot_id: str = Field(pattern=r"^slot-[0-9]{3}$")
    run_class: BenchmarkRunClass
    subject: ComparisonSubject
    dataset_id: Stage4DatasetId
    case_id: str = Field(min_length=1, max_length=256)
    trial_id: str = Field(pattern=r"^(trial-[1-3]|control-1)$")
    terminal_classification: TerminalClassification
    terminal_receipt_sha256: BenchmarkSha256
    observation_sha256: BenchmarkSha256 | None = None
    control_result_sha256: BenchmarkSha256 | None = None

    @model_validator(mode="after")
    def validate_artifact_class(self) -> Self:
        if self.run_class == "portable":
            if self.trial_id == "control-1" or self.control_result_sha256 is not None:
                raise ValueError("portable coverage may only bind an observation")
            if self.case_id not in PORTABLE_CASE_IDS:
                raise ValueError("portable coverage contains a non-portable case")
        elif (
            self.subject != "drift_agent"
            or self.trial_id != "control-1"
            or self.observation_sha256 is not None
        ):
            raise ValueError("control coverage may only bind a Drift control result")
        elif self.case_id not in CONTROL_CASE_IDS:
            raise ValueError("control coverage contains a non-control case")
        expected_dataset = (
            "structural-v1" if self.case_id in FROZEN_MANIFEST_SHA256 else "stage3-v1"
        )
        if self.dataset_id != expected_dataset:
            raise ValueError("coverage dataset differs from its frozen case")
        return self


class FailureCountV1(BenchmarkModel):
    failure_class: TerminalClassification
    count: int = Field(ge=1)

    @model_validator(mode="after")
    def reject_completed(self) -> Self:
        if self.failure_class == "completed":
            raise ValueError("completed is not a failure class")
        return self


class CoverageReportV1(BenchmarkModel):
    schema_version: Literal[1] = 1
    suite_id: Literal["portable-v1"] = "portable-v1"
    plan_digest: BenchmarkSha256
    unique_paired_cases: Literal[12] = 12
    paired_trial_slots: Literal[12, 36]
    control_cases: Literal[6] = 6
    planned_subject_slots: Literal[30, 78]
    entries: tuple[CoverageEntryV1, ...] = Field(default=(), max_length=78)
    failure_counts: tuple[FailureCountV1, ...] = Field(default=(), max_length=18)
    execution_accounted: bool
    portable_score_complete: bool
    controls_complete: bool
    benchmark_complete: bool

    @model_validator(mode="after")
    def validate_coverage(self) -> Self:
        expected_slots = self.paired_trial_slots * 2 + self.control_cases
        if self.planned_subject_slots != expected_slots:
            raise ValueError("planned subject slot count is inconsistent")
        slot_ids = [entry.slot_id for entry in self.entries]
        if len(set(slot_ids)) != len(slot_ids):
            raise ValueError("coverage slot IDs must be unique")
        accounted = len(self.entries) == expected_slots
        if accounted and set(slot_ids) != {
            f"slot-{ordinal:03d}" for ordinal in range(1, expected_slots + 1)
        }:
            raise ValueError("accounted coverage must contain every planned slot ID")
        portable = [entry for entry in self.entries if entry.run_class == "portable"]
        groups: dict[tuple[str, str, str], list[CoverageEntryV1]] = {}
        for entry in portable:
            groups.setdefault((entry.dataset_id, entry.case_id, entry.trial_id), []).append(entry)
        score_complete = len(groups) == self.paired_trial_slots and all(
            len(group) == 2
            and {entry.subject for entry in group} == {"codex", "drift_agent"}
            and all(entry.observation_sha256 is not None for entry in group)
            for group in groups.values()
        )
        controls = [entry for entry in self.entries if entry.run_class == "control"]
        controls_complete = (
            len(controls) == 6
            and {entry.case_id for entry in controls} == set(CONTROL_CASE_IDS)
            and all(entry.control_result_sha256 is not None for entry in controls)
        )
        if self.execution_accounted != accounted:
            raise ValueError("execution_accounted does not match terminal coverage")
        if self.portable_score_complete != score_complete:
            raise ValueError("portable_score_complete does not match observations")
        if self.controls_complete != controls_complete:
            raise ValueError("controls_complete does not match control results")
        if self.benchmark_complete != (accounted and score_complete and controls_complete):
            raise ValueError("benchmark_complete must be the conjunction of coverage states")
        counts: dict[TerminalClassification, int] = {}
        for entry in self.entries:
            if entry.terminal_classification != "completed":
                counts[entry.terminal_classification] = (
                    counts.get(entry.terminal_classification, 0) + 1
                )
        expected_failures = tuple(
            FailureCountV1(failure_class=failure_class, count=count)
            for failure_class, count in sorted(counts.items())
        )
        if self.failure_counts != expected_failures:
            raise ValueError("failure counts do not match coverage entries")
        return self


class ControlResultV1(BenchmarkModel):
    schema_version: Literal[1] = 1
    plan_digest: BenchmarkSha256
    dataset_id: Literal["stage3-v1"] = "stage3-v1"
    case_id: str = Field(min_length=1, max_length=256)
    case_manifest_sha256: BenchmarkSha256
    run_id: Literal["control-1"] = "control-1"
    runner_contract_sha256: BenchmarkSha256
    evidence_sha256: BenchmarkSha256
    evaluation: Stage3CaseEvaluation

    @model_validator(mode="after")
    def validate_control(self) -> Self:
        if self.case_id not in CONTROL_CASE_IDS:
            raise ValueError("only frozen control cases may produce ControlResultV1")
        if FROZEN_STAGE3_MANIFEST_SHA256[self.case_id] != self.case_manifest_sha256:
            raise ValueError("control manifest hash differs from the frozen dataset")
        if self.evaluation.case_id != self.case_id:
            raise ValueError("control evaluation case ID differs from result identity")
        return self

    @property
    def passed(self) -> bool:
        return self.evaluation.passed


class ControlSummaryV1(BenchmarkModel):
    planned: Literal[6] = 6
    scored: Literal[6] = 6
    passed: int = Field(ge=0, le=6)
    failed: int = Field(ge=0, le=6)
    controls_complete: Literal[True] = True
    control_all_passed: bool

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.passed + self.failed != self.scored:
            raise ValueError("control pass/fail counts must equal scored")
        if self.control_all_passed != (self.passed == self.planned):
            raise ValueError("control_all_passed does not match pass count")
        return self


class ControlReportV1(BenchmarkModel):
    schema_version: Literal[1] = 1
    suite_id: Literal["portable-v1"] = "portable-v1"
    plan_digest: BenchmarkSha256
    results: tuple[ControlResultV1, ...] = Field(min_length=6, max_length=6)
    summary: ControlSummaryV1

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        if tuple(result.case_id for result in self.results) != tuple(sorted(CONTROL_CASE_IDS)):
            raise ValueError("control results must contain all six cases sorted by case ID")
        if any(result.plan_digest != self.plan_digest for result in self.results):
            raise ValueError("control results must bind the report plan")
        passed = sum(result.passed for result in self.results)
        expected = ControlSummaryV1(
            passed=passed,
            failed=6 - passed,
            control_all_passed=passed == 6,
        )
        if self.summary != expected:
            raise ValueError("control summary does not match results")
        return self


BenchmarkMissingMetric = Literal[
    "exact_repair_rate",
    "regression_free_rate",
    "codex_duration_p50",
    "codex_duration_p95",
    "cross_subject_tool_call_comparison",
]
V1_MISSING_METRICS: tuple[BenchmarkMissingMetric, ...] = (
    "exact_repair_rate",
    "regression_free_rate",
    "codex_duration_p50",
    "codex_duration_p95",
    "cross_subject_tool_call_comparison",
)


class BenchmarkAggregateLabelsV1(BenchmarkModel):
    structural: Literal["frozen-policy-conformance-only"] = "frozen-policy-conformance-only"
    executable: Literal["repo-observable-check-conformance"] = "repo-observable-check-conformance"
    semantic: Literal["controls-only-not-paired"] = "controls-only-not-paired"
    overall: Literal["frozen-case-conformance-smoke"] = "frozen-case-conformance-smoke"


class BenchmarkArtifactDigestsV1(BenchmarkModel):
    coverage_report_sha256: BenchmarkSha256
    comparison_report_sha256: BenchmarkSha256
    control_report_sha256: BenchmarkSha256
    adjudication_sidecar_sha256: BenchmarkSha256


class BenchmarkCoverageSummaryV1(BenchmarkModel):
    execution_accounted: bool
    portable_score_complete: bool
    controls_complete: bool
    benchmark_complete: bool

    @model_validator(mode="after")
    def validate_completion(self) -> Self:
        expected = (
            self.execution_accounted and self.portable_score_complete and self.controls_complete
        )
        if self.benchmark_complete != expected:
            raise ValueError("benchmark completion must be the conjunction of coverage states")
        return self


class BenchmarkReportV1(BenchmarkModel):
    """Plan-aware headline; underlying Stage 4 reports remain digest-bound attachments."""

    schema_version: Literal[1] = 1
    suite_id: Literal["portable-v1"] = "portable-v1"
    plan_digest: BenchmarkSha256
    unique_paired_cases: Literal[12] = 12
    paired_trial_slots: Literal[12, 36]
    control_cases: Literal[6] = 6
    coverage: BenchmarkCoverageSummaryV1
    failure_counts: tuple[FailureCountV1, ...] = Field(default=(), max_length=18)
    control_summary: ControlSummaryV1
    missing_metrics: tuple[BenchmarkMissingMetric, ...]
    aggregate_labels: BenchmarkAggregateLabelsV1 = Field(default_factory=BenchmarkAggregateLabelsV1)
    artifacts: BenchmarkArtifactDigestsV1

    @model_validator(mode="after")
    def validate_headline(self) -> Self:
        if self.missing_metrics != V1_MISSING_METRICS:
            raise ValueError("V1 report must disclose every fixed missing metric")
        failures = [item.failure_class for item in self.failure_counts]
        if failures != sorted(failures) or len(set(failures)) != len(failures):
            raise ValueError("headline failure classes must be unique and sorted")
        if self.coverage.controls_complete != self.control_summary.controls_complete:
            raise ValueError("coverage and control summary completeness must agree")
        return self


def deterministic_observation_id(
    *,
    plan_digest: str,
    subject: ComparisonSubject,
    pair_key: ComparisonPairKey,
    evidence_sha256: str,
) -> str:
    if re.fullmatch(r"[0-9a-f]{64}", plan_digest) is None:
        raise ValueError("plan_digest must be a lowercase SHA-256")
    if re.fullmatch(r"[0-9a-f]{64}", evidence_sha256) is None:
        raise ValueError("evidence_sha256 must be a lowercase SHA-256")
    if subject not in {"codex", "drift_agent"}:
        raise ValueError("unknown benchmark subject")
    material = (plan_digest, subject, pair_key, evidence_sha256)
    digest = canonical_sha256(material)
    return f"obs_v1_{subject}_{digest[:32]}"


observation_id_v1 = deterministic_observation_id


__all__ = [
    "CONTROL_CASE_IDS",
    "PORTABLE_CASE_IDS",
    "PORTABLE_STAGE3_CASE_IDS",
    "TRIAL_IDS_BY_COUNT",
    "V1_MISSING_METRICS",
    "BenchmarkAggregateLabelsV1",
    "BenchmarkArtifactDigestsV1",
    "BenchmarkAuthorizationV1",
    "BenchmarkCaseSelectionV1",
    "BenchmarkCodexRuntimeV1",
    "BenchmarkContractDigestsV1",
    "BenchmarkCoverageSummaryV1",
    "BenchmarkDatasetCatalogV1",
    "BenchmarkDriftRuntimeV1",
    "BenchmarkLimitsV1",
    "BenchmarkMissingMetric",
    "BenchmarkModel",
    "BenchmarkOperation",
    "BenchmarkPlanV1",
    "BenchmarkPrefixedDigest",
    "BenchmarkReportV1",
    "BenchmarkRunClass",
    "BenchmarkScheduleSlotV1",
    "BenchmarkSha256",
    "BenchmarkTaskV1",
    "BenchmarkToolProfileV1",
    "BenchmarkToolchainV1",
    "BoundedStreamReceiptV1",
    "CodexTaskResultV1",
    "ComponentKind",
    "ControlReportV1",
    "ControlResultV1",
    "ControlSummaryV1",
    "CoverageEntryV1",
    "CoverageReportV1",
    "DeclaredStatus",
    "FailureCountV1",
    "FindingFamily",
    "NeutralFindingKeyV1",
    "NeutralFindingV1",
    "NeutralOracleProjectionV1",
    "NeutralSubjectResultV1",
    "NeutralValueKind",
    "NeutralValueV1",
    "RawRunEvidenceV1",
    "RawUsageEvidenceV1",
    "RawUsageMetricV1",
    "RedactionReceiptV1",
    "TerminalClassification",
    "TerminalReceiptV1",
    "ValidationClaimV1",
    "ValidationStatus",
    "build_benchmark_schedule",
    "bytes_sha256",
    "canonical_json_bytes",
    "canonical_sha256",
    "codex_task_result_schema_bytes",
    "codex_task_result_schema_sha256",
    "deterministic_observation_id",
    "fixed_benchmark_case_selections",
    "neutral_finding_encoding_bytes",
    "neutral_finding_encoding_sha256",
    "observation_id_v1",
    "sha256_prefixed",
    "validate_repo_relative_path",
    "validate_symbol_fqn",
]

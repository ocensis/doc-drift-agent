from __future__ import annotations

from pathlib import PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

DatasetId = Literal["structural-v1"]
ProjectFamily = Literal["click", "httpx", "pydantic", "rich"]
ProvenanceKind = Literal["project_authored", "historical"]
FixtureRole = Literal["base", "current", "expected"]
FixtureOrigin = Literal["project_authored", "upstream"]
Operation = Literal["check", "repair"]
Disposition = Literal["detected", "fixed", "needs_approval", "unresolved"]

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def _safe_relative_path(value: str) -> bool:
    path = PurePosixPath(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts


class EvaluationModel(BaseModel):
    """Strict base model for the frozen evaluation wire format."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class SymbolIdentity(EvaluationModel):
    version: Literal["python-symbol-v1"] = "python-symbol-v1"
    module: str
    owner: str | None = None
    name: str
    category: Literal[
        "module_function",
        "method",
        "class",
        "exception_class",
    ]

    @field_validator("module", "name")
    @classmethod
    def validate_identifiers(cls, value: str) -> str:
        if not value or any(not part.isidentifier() for part in value.split(".")):
            raise ValueError("symbol identity segments must be Python identifiers")
        return value

    @field_validator("owner")
    @classmethod
    def validate_owner(cls, value: str | None) -> str | None:
        if value is not None and not value.isidentifier():
            raise ValueError("symbol owner must be a Python identifier")
        return value


class MatchingKey(EvaluationModel):
    """Repository-independent structural finding identity used by evaluation."""

    symbol_identity: SymbolIdentity
    kind: str = Field(min_length=1)
    component: str
    old_value: JsonValue
    new_value: JsonValue
    code_path: str
    doc_path: str
    detector_id: str = Field(min_length=1)
    detector_version: str = Field(min_length=1)

    @field_validator("code_path", "doc_path")
    @classmethod
    def validate_evidence_paths(cls, value: str) -> str:
        if not _safe_relative_path(value):
            raise ValueError("finding evidence paths must be repository-relative")
        return PurePosixPath(value).as_posix()


class Provenance(EvaluationModel):
    kind: ProvenanceKind
    repository: str
    code_revision: str
    doc_revision: str
    source_urls: tuple[str, ...]
    license_spdx: str = Field(min_length=1)
    copied_bytes: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_kind_contract(self) -> Self:
        if self.license_spdx == "NOASSERTION":
            raise ValueError("evaluation fixtures require an asserted SPDX license")
        if self.kind == "project_authored":
            if self.license_spdx != "LicenseRef-Project-Authored":
                raise ValueError("synthetic fixtures require project-authored license")
            if self.copied_bytes != 0:
                raise ValueError("synthetic fixtures must declare copied_bytes=0")
        else:
            if not self.source_urls:
                raise ValueError("historical fixtures require fixed source URLs")
            if any(not url.startswith("https://") for url in self.source_urls):
                raise ValueError("historical source URLs must use HTTPS")
            for revision in (self.code_revision, self.doc_revision):
                if len(revision) != 40 or any(ch not in "0123456789abcdef" for ch in revision):
                    raise ValueError("historical revisions must be full lowercase Git SHAs")
        return self


class FixtureFile(EvaluationModel):
    path: str
    target_path: str
    role: FixtureRole
    origin: FixtureOrigin
    sha256: Sha256
    size_bytes: int = Field(ge=0)

    @field_validator("path", "target_path")
    @classmethod
    def validate_paths(cls, value: str) -> str:
        if not _safe_relative_path(value):
            raise ValueError("fixture paths must be relative and may not traverse parents")
        return PurePosixPath(value).as_posix()


class RenameInput(EvaluationModel):
    old_path: str
    new_path: str

    @field_validator("old_path", "new_path")
    @classmethod
    def validate_paths(cls, value: str) -> str:
        if not _safe_relative_path(value):
            raise ValueError("rename paths must stay inside the fixture repository")
        return PurePosixPath(value).as_posix()

    @model_validator(mode="after")
    def validate_distinct_paths(self) -> Self:
        if self.old_path == self.new_path:
            raise ValueError("rename source and destination must differ")
        return self


class WorkspaceInput(EvaluationModel):
    deleted_paths: tuple[str, ...] = ()
    renames: tuple[RenameInput, ...] = ()
    staged_paths: tuple[str, ...] = ()

    @field_validator("deleted_paths", "staged_paths")
    @classmethod
    def validate_workspace_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(PurePosixPath(value).as_posix() for value in values)
        if any(not _safe_relative_path(value) for value in normalized):
            raise ValueError("workspace paths must stay inside the fixture repository")
        if len(set(normalized)) != len(normalized):
            raise ValueError("workspace paths must be unique")
        return normalized


class ChangedBytes(EvaluationModel):
    path: str
    before_sha256: Sha256 | None
    after_sha256: Sha256 | None
    before_size_bytes: int | None = Field(default=None, ge=0)
    after_size_bytes: int | None = Field(default=None, ge=0)
    before_mode: str | None = None
    after_mode: str | None = None

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        if not _safe_relative_path(value):
            raise ValueError("changed-byte paths must stay inside the repository")
        return PurePosixPath(value).as_posix()

    @model_validator(mode="after")
    def validate_presence_pairs(self) -> Self:
        before_values = (self.before_sha256, self.before_size_bytes, self.before_mode)
        after_values = (self.after_sha256, self.after_size_bytes, self.after_mode)
        if any(value is None for value in before_values) and any(
            value is not None for value in before_values
        ):
            raise ValueError("before hash, size, and mode must be present together")
        if any(value is None for value in after_values) and any(
            value is not None for value in after_values
        ):
            raise ValueError("after hash, size, and mode must be present together")
        if before_values == after_values:
            raise ValueError("changed-byte entries must describe an actual change")
        return self


class ExpectedResult(EvaluationModel):
    status: Literal[
        "clean",
        "drift_found",
        "fixed",
        "partial",
        "needs_approval",
        "unresolved",
        "stale",
        "failed",
    ]
    finding_multiset: tuple[MatchingKey, ...]
    dispositions: tuple[Disposition, ...]
    reason_codes: tuple[str, ...]
    changed_bytes: tuple[ChangedBytes, ...]

    @model_validator(mode="after")
    def validate_parallel_finding_oracles(self) -> Self:
        lengths = {
            len(self.finding_multiset),
            len(self.dispositions),
            len(self.reason_codes),
        }
        if len(lengths) != 1:
            raise ValueError(
                "finding_multiset, dispositions, and reason_codes must have equal lengths"
            )
        if any(not reason for reason in self.reason_codes):
            raise ValueError("reason codes may not be empty")
        changed_paths = [change.path for change in self.changed_bytes]
        if len(set(changed_paths)) != len(changed_paths):
            raise ValueError("changed-byte paths must be unique")
        return self


class CaseManifest(EvaluationModel):
    schema_version: Literal[1]
    dataset_id: DatasetId
    case_id: str = Field(min_length=1)
    project_family: ProjectFamily
    provenance: Provenance
    files: tuple[FixtureFile, ...]
    workspace: WorkspaceInput
    operation: Operation
    coverage_tags: tuple[str, ...]
    expected: ExpectedResult
    model_calls: Literal[0]
    offline: Literal[True]

    @model_validator(mode="after")
    def validate_case_shape(self) -> Self:
        fixture_paths = [fixture.path for fixture in self.files]
        if len(set(fixture_paths)) != len(fixture_paths):
            raise ValueError("fixture file paths must be unique")
        state_targets = [(fixture.role, fixture.target_path) for fixture in self.files]
        if len(set(state_targets)) != len(state_targets):
            raise ValueError("each fixture role may define a target path only once")
        if not self.coverage_tags or any(not tag for tag in self.coverage_tags):
            raise ValueError("coverage tags must be non-empty")
        if len(set(self.coverage_tags)) != len(self.coverage_tags):
            raise ValueError("coverage tags must be unique")
        if self.provenance.kind == "project_authored" and any(
            fixture.origin != "project_authored" for fixture in self.files
        ):
            raise ValueError("synthetic cases may not contain upstream fixture bytes")
        return self


class CatalogEntry(EvaluationModel):
    case_id: str
    manifest: str
    sha256: Sha256

    @field_validator("manifest")
    @classmethod
    def validate_manifest_path(cls, value: str) -> str:
        if not _safe_relative_path(value):
            raise ValueError("catalog manifest paths must be relative")
        return PurePosixPath(value).as_posix()


class CatalogManifest(EvaluationModel):
    schema_version: Literal[1]
    dataset_id: DatasetId
    cases: tuple[CatalogEntry, ...]


class ObservedFinding(EvaluationModel):
    key: MatchingKey
    disposition: Disposition
    reason_code: str


class CaseObservation(EvaluationModel):
    status: str
    findings: tuple[ObservedFinding, ...]
    changed_bytes: tuple[ChangedBytes, ...]
    model_calls: int = Field(ge=0)
    network_calls: int = Field(ge=0)
    offline: bool


class KeyCount(EvaluationModel):
    key: MatchingKey
    count: int = Field(gt=0)


class MultisetMatch(EvaluationModel):
    tp: int = Field(ge=0)
    fp: int = Field(ge=0)
    fn: int = Field(ge=0)
    unexpected: tuple[KeyCount, ...] = ()
    missing: tuple[KeyCount, ...] = ()

    @property
    def true_positives(self) -> int:
        return self.tp

    @property
    def false_positives(self) -> int:
        return self.fp

    @property
    def false_negatives(self) -> int:
        return self.fn


class CaseEvaluation(EvaluationModel):
    case_id: str
    passed: bool
    matching: MultisetMatch
    status_matches: bool
    outcomes_match: bool
    changed_bytes_match: bool
    no_extra_mutation: bool
    zero_model_compliance: bool
    offline_compliance: bool
    repair_success: bool
    conservative_rejection: bool
    expected_status: str
    actual_status: str
    expected_outcomes: tuple[str, ...]
    actual_outcomes: tuple[str, ...]
    expected_changed_bytes: tuple[ChangedBytes, ...]
    actual_changed_bytes: tuple[ChangedBytes, ...]


class EvaluationSummary(EvaluationModel):
    total: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    tp: int = Field(ge=0)
    fp: int = Field(ge=0)
    fn: int = Field(ge=0)
    repair_successes: int = Field(ge=0)
    conservative_rejections: int = Field(ge=0)
    model_calls: int = Field(ge=0)
    network_calls: int = Field(ge=0)
    zero_model_compliance: bool
    offline_compliance: bool

    @property
    def true_positives(self) -> int:
        return self.tp

    @property
    def false_positives(self) -> int:
        return self.fp

    @property
    def false_negatives(self) -> int:
        return self.fn


class EvaluationReport(EvaluationModel):
    dataset_id: DatasetId
    cases: tuple[CaseEvaluation, ...]
    summary: EvaluationSummary


class CatalogAudit(EvaluationModel):
    dataset_id: DatasetId
    case_ids: tuple[str, ...]
    fixture_files: int = Field(ge=0)
    fixture_bytes: int = Field(ge=0)
    copied_bytes: int = Field(ge=0)
    coverage_tags: tuple[str, ...]

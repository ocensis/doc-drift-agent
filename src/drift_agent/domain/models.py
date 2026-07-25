from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    GetJsonSchemaHandler,
    StrictStr,
    field_validator,
    model_validator,
)
from pydantic_core import CoreSchema

from drift_agent.domain.enums import (
    FindingDisposition,
    RunMode,
    RunStatus,
    ValidationStatus,
)


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def scope_spec_json_schema() -> dict[str, Any]:
    """Return the tagged public schema while retaining legacy empty-as-changed input."""

    return {
        "title": "ScopeSpec",
        "oneOf": [
            {
                "type": "object",
                "properties": {
                    "kind": {
                        "const": "changed",
                        "default": "changed",
                        "type": "string",
                    }
                },
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "kind": {"const": "since", "type": "string"},
                    "revision": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 1024,
                        "pattern": r"^(?!-)(?=.*\S)[^\x00-\x1f\x7f]+$",
                    },
                },
                "required": ["kind", "revision"],
                "additionalProperties": False,
            },
        ],
    }


class ScopeSpec(ContractModel):
    kind: Literal["changed", "since"] = "changed"
    revision: StrictStr | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @classmethod
    def __get_pydantic_json_schema__(
        cls,
        _core_schema: CoreSchema,
        _handler: GetJsonSchemaHandler,
    ) -> dict[str, Any]:
        return scope_spec_json_schema()

    @model_validator(mode="after")
    def validate_revision(self) -> ScopeSpec:
        if self.kind == "changed":
            if "revision" in self.model_fields_set:
                raise ValueError("changed scope must not define a revision")
            return self
        if self.revision is None or not self.revision.strip():
            raise ValueError("since scope requires a revision")
        if self.revision.startswith("-"):
            raise ValueError("scope revision may not be option-like")
        if len(self.revision) > 1024 or any(
            ord(character) < 32 or ord(character) == 127 for character in self.revision
        ):
            raise ValueError("scope revision contains unsupported characters")
        return self


class RunBudgets(ContractModel):
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


class RunRequest(ContractModel):
    mode: RunMode
    repo_path: Path
    scope: ScopeSpec = Field(default_factory=ScopeSpec)
    apply_policy: Literal["docs_only"] = "docs_only"
    budgets: RunBudgets = Field(default_factory=RunBudgets)
    state_dir: Path | None = None
    lock_timeout_seconds: float = 5.0
    semantic_analysis: bool = False
    semantic_repair: bool = False

    @field_validator("lock_timeout_seconds")
    @classmethod
    def validate_lock_timeout(cls, value: float) -> float:
        if not math.isfinite(value) or value < 0:
            raise ValueError("lock_timeout_seconds must be finite and non-negative")
        return value

    @model_validator(mode="after")
    def validate_semantic_mode(self) -> RunRequest:
        if self.semantic_analysis and self.mode is not RunMode.CHECK:
            raise ValueError("semantic_analysis is currently supported only in check mode")
        if self.semantic_repair and self.mode is not RunMode.REPAIR:
            raise ValueError("semantic_repair is supported only in repair mode")
        return self


class SourceAnchor(ContractModel):
    path: str
    line: int
    start_byte: int
    end_byte: int
    exact_text: str
    source_hash: str


class EvidenceAnchor(ContractModel):
    path: str
    line: int
    source_hash: str
    start_byte: int = 0
    end_byte: int = 0


class SymbolIdentity(ContractModel):
    version: Literal["python-symbol-v1", "typescript-symbol-v1"] = "python-symbol-v1"
    module: str
    owner: str | None = None
    name: str
    category: Literal[
        "module_function",
        "method",
        "class",
        "exception_class",
        "interface",
        "type_alias",
        "enum",
        "value",
    ]

    @property
    def fqn(self) -> str:
        parts = [self.module]
        if self.owner is not None:
            parts.append(self.owner)
        parts.append(self.name)
        return ".".join(parts)


class ParameterFact(ContractModel):
    name: str
    kind: str
    annotation: str | None = None
    default: str | None = None
    required: bool = True
    default_present: bool | None = None
    annotation_present: bool | None = None
    position: int = 0
    anchor: SourceAnchor | None = None

    @model_validator(mode="after")
    def derive_compatibility_presence(self) -> ParameterFact:
        # Stage 1 callers did not carry presence bits.  Derive them only when
        # absent; providers in Stage 2 always set them explicitly, which keeps
        # a literal ``None`` default distinct from no default.
        if self.default_present is None:
            self.default_present = not self.required
        if self.annotation_present is None:
            self.annotation_present = self.annotation is not None
        return self


class SemanticScalar(ContractModel):
    type: Literal["none", "bool", "int", "str"]
    value: Any

    @model_validator(mode="after")
    def validate_tagged_value(self) -> SemanticScalar:
        valid = (
            (self.type == "none" and self.value is None)
            or (self.type == "bool" and type(self.value) is bool)
            or (self.type == "int" and type(self.value) is int)
            or (self.type == "str" and type(self.value) is str)
        )
        if not valid:
            raise ValueError("semantic scalar type tag does not match its value")
        if self.type == "int" and not (-(2**63) <= self.value <= 2**63 - 1):
            raise ValueError("semantic integer must fit signed 64-bit range")
        if self.type == "str":
            try:
                self.value.encode("utf-8")
            except UnicodeEncodeError as error:
                raise ValueError(
                    "semantic string must contain only Unicode scalar values"
                ) from error
        return self


class SemanticReturnAssertion(ContractModel):
    predicate: Literal["return_literal"] = "return_literal"
    mode: Literal["direct", "always"]
    value: SemanticScalar


class SemanticCodeFact(ContractModel):
    predicate: Literal["return_literal"] = "return_literal"
    component_id: Literal["return.literal"] = "return.literal"
    normalized_value: SemanticScalar
    anchor: SourceAnchor


class CodeFact(ContractModel):
    symbol_id: str
    path: str
    line: int
    signature: str
    parameters: list[ParameterFact]
    return_annotation: str | None = None
    source_hash: str
    symbol_identity: SymbolIdentity | None = None
    category: Literal[
        "module_function", "method", "class", "interface", "type_alias", "enum", "value"
    ] = "module_function"
    owner: str | None = None
    is_async: bool = False
    decorator_kind: Literal["plain", "staticmethod", "classmethod"] = "plain"
    language: Literal["python", "typescript"] = "python"
    return_annotation_present: bool | None = None
    name_anchor: SourceAnchor | None = None
    signature_anchor: SourceAnchor | None = None
    docstring_anchor: SourceAnchor | None = None
    source_version: str = "worktree"
    # Internal, position-independent executable-body shape used only to prove
    # a unique counterpart across an explicit Git path rename.  It is not
    # part of either public wire contract.
    body_fingerprint: str = Field(default="", exclude=True)
    # Definite local behavior facts are internal detector inputs. They are
    # excluded from every public wire version and never inferred by a model.
    semantic_facts: list[SemanticCodeFact] = Field(default_factory=list, exclude=True)

    @model_validator(mode="after")
    def derive_identity_and_return_presence(self) -> CodeFact:
        if self.return_annotation_present is None:
            self.return_annotation_present = self.return_annotation is not None
        if self.symbol_identity is None:
            pieces = self.symbol_id.split(".")
            if self.owner is None:
                module = ".".join(pieces[:-1])
                name = pieces[-1]
            else:
                module = ".".join(pieces[:-2])
                name = pieces[-1]
            if module and name:
                self.symbol_identity = SymbolIdentity(
                    module=module,
                    owner=self.owner,
                    name=name,
                    category=self.category,
                )
        return self


class UnsupportedSymbolFact(ContractModel):
    symbol_id: str
    symbol_identity: SymbolIdentity
    path: str
    line: int
    source_hash: str
    name_anchor: SourceAnchor
    source_version: str = "worktree"


ClaimKind = Literal[
    "signature",
    "symbol_reference",
    "docstring_parameter",
    "docstring_return",
    "semantic_assertion",
]


class DocClaim(ContractModel):
    id: str
    symbol_id: str
    kind: ClaimKind = "signature"
    signature: str = ""
    parameters: list[ParameterFact] = Field(default_factory=list)
    return_annotation: str | None = None
    explicit_truth: Literal["code_derived", "design", "contract", "unknown"] | None = None
    extraction_error: str | None = None
    anchor: SourceAnchor
    component_id: str = ""
    raw_value: Any = None
    normalized_value: Any = None
    value_anchor: SourceAnchor | None = None
    declaration_anchor: SourceAnchor | None = None
    literal_anchor: SourceAnchor | None = None
    complete_declaration: bool = False
    # Single-segment doc references (e.g. backtick `UnifiedMessage`) are only
    # resolvable against languages whose exports are name-addressable; the
    # resolution pass drops them silently when no unique match exists.
    single_segment: bool = False


class Alignment(ContractModel):
    fact: CodeFact
    claim: DocClaim
    method: Literal["exact_fqn", "git_rename", "manual_alias"] = "exact_fqn"
    confidence: Literal["high"] = "high"
    old_symbol_id: str | None = None


class ProviderIssue(ContractModel):
    path: str
    reason_code: str
    message: str
    symbol_id: str = ""
    line: int = 1
    source_hash: str = ""
    symbol_identity: SymbolIdentity | None = None


class DriftFinding(ContractModel):
    id: str
    symbol_id: str
    type: Literal["signature_drift", "semantic_drift"] = "signature_drift"
    disposition: FindingDisposition = FindingDisposition.DETECTED
    truth_source: Literal["code", "human", "unknown"] = "code"
    code_evidence: EvidenceAnchor
    doc_evidence: EvidenceAnchor
    reason: str
    kind: str = "signature_changed"
    component_id: str = ""
    old_value: Any = None
    new_value: Any = None
    detector_id: str = ""
    detector_version: str = ""
    fingerprint: str = ""
    reason_code: str = ""
    # Internal identity used by persistence/evaluation for conservatively
    # rejected non-function symbols. It is excluded from the public V1/V2
    # wire contract, whose supported-symbol finding shape remains unchanged.
    symbol_identity: SymbolIdentity | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def validate_semantic_contract(self) -> DriftFinding:
        reason_codes = {
            "semantic_direct_mismatch": "semantic.direct_mismatch",
            "semantic_over_promise": "semantic.over_promise",
            "semantic_section_drift": "semantic.section_mismatch",
        }
        if self.type != "semantic_drift":
            if self.kind in reason_codes:
                raise ValueError("semantic finding kind requires type=semantic_drift")
            return self
        expected_reason = reason_codes.get(self.kind)
        if expected_reason is None:
            raise ValueError("semantic finding kind is not supported by schema V3")
        lifecycle_reason_codes = {
            "accounting_incomplete",
            "budget_exhausted",
            "final_validation_failed",
            "global_snapshot_changed",
            "model_abstained",
            "model_low_confidence",
            "model_not_configured",
            "model_output_unsafe",
            "model_schema_invalid",
            "model_unavailable",
            "precondition_changed",
            "rollback_unavailable",
            "semantic_alignment_unavailable",
            "semantic_validation_failed",
            "truth_requires_approval",
            "unknown_truth",
            "validated",
            "validation_failed",
            "validation_unavailable",
        }
        if self.reason_code not in {expected_reason, *lifecycle_reason_codes}:
            raise ValueError("semantic finding reason_code does not match its kind or state")
        if not all(
            (
                self.component_id,
                self.detector_id,
                self.detector_version,
                self.fingerprint,
            )
        ):
            raise ValueError("semantic finding identity fields must be non-empty")
        try:
            json.dumps(
                {"old_value": self.old_value, "new_value": self.new_value},
                allow_nan=False,
            )
        except (TypeError, ValueError) as error:
            raise ValueError("semantic finding values must be finite JSON data") from error
        return self


class WorkspaceSnapshot(ContractModel):
    head_revision: str
    workspace_fingerprint: str
    input_file_hashes: dict[str, str]
    # Internal exact manifest of files exposed to configured validation.  It is
    # excluded from V1/V2 wire output while still closing added/deleted-file
    # races that a path-only hash map cannot observe.
    validation_input_hashes: dict[str, str] = Field(default_factory=dict, exclude=True)


class PatchAttempt(ContractModel):
    id: str
    finding_ids: list[str]
    path: str
    source_hash: str
    start_byte: int
    end_byte: int
    expected_text: str
    replacement_text: str
    unified_diff: str
    attempt: int = 1
    target_kind: Literal["markdown", "docstring"] = "markdown"
    group_id: str = ""


class ValidationResult(ContractModel):
    finding_ids: list[str]
    attempt_id: str
    check: str
    required: bool
    status: ValidationStatus
    summary: str
    duration_ms: int = 0


class ChangeSet(ContractModel):
    applied: bool = False
    files: list[str] = Field(default_factory=list)
    patch: str = ""


class Usage(ContractModel):
    model_calls: int = 0
    model_calls_by_profile: dict[str, int] = Field(default_factory=dict)
    tool_calls: int = 0
    validation_commands: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    duration_ms: int = 0


class ApprovalRequest(ContractModel):
    id: str
    finding_id: str
    kind: Literal["truth_direction"] = "truth_direction"
    reason: str
    input_file_hashes: dict[str, str]
    candidate_diff: str | None = None
    suggested_validation: list[str] = Field(default_factory=list)


class SuppressionRecord(ContractModel):
    decision_id: str
    finding_id: str
    action: Literal["ignore", "false_positive"]
    reason: str
    actor: str
    confirmation: str
    evidence_key: str
    created_at: str = ""
    revoked_at: str | None = None


class MemoryEvent(ContractModel):
    kind: str
    record_id: str = ""
    reason: str = ""


class RepairGroupOutcome(ContractModel):
    id: str
    finding_ids: list[str]
    disposition: FindingDisposition
    reason_code: str
    path: str = ""
    start_byte: int = 0
    end_byte: int = 0


class ResidualChange(ContractModel):
    path: str
    start_byte: int
    end_byte: int
    current_hash: str
    reason_code: Literal["rollback_unavailable"] = "rollback_unavailable"


class VerifiedRepairBundle(ContractModel):
    status: RunStatus
    run_id: str
    snapshot: WorkspaceSnapshot
    scope: list[str]
    findings: list[DriftFinding]
    changes: ChangeSet = Field(default_factory=ChangeSet)
    validation: list[ValidationResult] = Field(default_factory=list)
    approval_required: list[ApprovalRequest] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    repository_id: str = ""
    workspace_id: str = ""
    suppressed_findings: list[SuppressionRecord] = Field(default_factory=list)
    memory_events: list[MemoryEvent] = Field(default_factory=list)
    repair_groups: list[RepairGroupOutcome] = Field(default_factory=list)
    residual_changes: list[ResidualChange] = Field(default_factory=list)
    # Internal capability marker used to fail closed when a caller requests a
    # legacy wire projection after opting into semantic analysis.
    semantic_analysis: bool = Field(default=False, exclude=True)

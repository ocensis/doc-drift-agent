from typing import Any, NotRequired, TypedDict

from drift_agent.config import DriftConfig
from drift_agent.domain.models import (
    Alignment,
    CodeFact,
    DocClaim,
    DriftFinding,
    PatchAttempt,
    RunRequest,
    ValidationResult,
    VerifiedRepairBundle,
    WorkspaceSnapshot,
)


class AgentState(TypedDict):
    request: RunRequest
    config: NotRequired[DriftConfig]
    config_hash: NotRequired[str]
    changed_paths: NotRequired[list[str]]
    snapshot: NotRequired[WorkspaceSnapshot]
    facts: NotRequired[list[CodeFact]]
    claims: NotRequired[list[DocClaim]]
    semantic_claims: NotRequired[list[DocClaim]]
    semantic_alignments: NotRequired[list[Alignment]]
    docstring_claims: NotRequired[list[DocClaim]]
    before_facts: NotRequired[list[CodeFact]]
    alignments: NotRequired[list[Alignment]]
    findings: NotRequired[list[DriftFinding]]
    attempts: NotRequired[list[PatchAttempt]]
    residual_attempts: NotRequired[list[PatchAttempt]]
    validation: NotRequired[list[ValidationResult]]
    bundle: NotRequired[VerifiedRepairBundle]
    stale: NotRequired[bool]
    failed: NotRequired[bool]
    budget_exhausted: NotRequired[bool]
    validation_incomplete: NotRequired[bool]
    repository_id: NotRequired[str]
    workspace_id: NotRequired[str]
    runtime_context: NotRequired[Any]

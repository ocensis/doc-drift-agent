from __future__ import annotations

import ast
import json
import re
from collections.abc import Mapping
from decimal import Decimal
from pathlib import PurePosixPath
from typing import Any, Literal, cast

from pydantic import BaseModel, ValidationError

from drift_agent.adapters.contracts import PublicBundleV3, PublicDriftFinding
from drift_agent.evaluation.benchmark_cases import (
    CanonicalGitMetadataV1,
    CanonicalRepositorySnapshotV1,
    EffectiveChangedBytesV1,
    PreparedBenchmarkCase,
    canonical_digest,
    effective_changed_bytes,
    git_metadata_sha256,
)
from drift_agent.evaluation.benchmark_models import (
    BenchmarkTaskV1,
    CodexTaskResultV1,
    DeclaredStatus,
    NeutralFindingKeyV1,
    NeutralSubjectResultV1,
    NeutralValueV1,
    RawRunEvidenceV1,
    RawUsageEvidenceV1,
    canonical_json_bytes,
    canonical_sha256,
    deterministic_observation_id,
)
from drift_agent.evaluation.benchmark_runner import StreamEvidence, SubjectRunResult
from drift_agent.evaluation.models import MatchingKey
from drift_agent.evaluation.runner import observed_finding_from_domain
from drift_agent.evaluation.stage4_models import (
    ComparisonBoolMetric,
    ComparisonChangedBytes,
    ComparisonObservationV1,
    ComparisonOutcome,
    ComparisonPairKey,
    ComparisonProvenance,
    ComparisonSafety,
    ComparisonUsage,
    ComparisonValidation,
)

_ABSTENTION_STATUSES = frozenset({"needs_approval", "unresolved", "stale", "failed"})
_NOT_MEASURED_ATTEMPTS = (
    "portable V1 has one invocation per subject and no common repair-at-N attempt unit"
)
_VALIDATION_NOT_MEASURED = (
    "the frozen portable task has no common post-run validator for this outcome metric"
)
_SAFETY_INCOMPLETE = "stale-overwrite safety is not exercised by the frozen static portable cases"
_CODEX_USAGE_INCOMPLETE = (
    "provider call count and billed cost are not exposed by the pinned Codex JSONL contract"
)
_FAILED_USAGE_INCOMPLETE = (
    "the process duration is known but the invalid or incomplete result has no trusted usage bundle"
)


class TrustedScoringIntegrityError(ValueError):
    """Evidence cannot safely produce a normalized comparison observation."""


class NeutralProjectionError(ValueError):
    """A valid subject wire result cannot be represented by the neutral ontology."""


def effective_request_sha256(run: SubjectRunResult) -> str:
    """Digest the secret-free request receipt using the frozen canonical form."""

    return canonical_sha256(
        {
            "adapter_version": run.request.adapter_version,
            "argv": run.request.argv,
            "operation": run.request.operation,
            "stdin_sha256": run.request.stdin_sha256,
        }
    )


def _missing(value: object) -> bool:
    return isinstance(value, Mapping) and value.get("type") in {
        "missing_parameter",
        "missing_symbol",
    }


def _missing_value() -> NeutralValueV1:
    return NeutralValueV1(kind="missing", value=None)


def _present_value() -> NeutralValueV1:
    return NeutralValueV1(kind="present", value=None)


def _python_literal(value: object, *, allow_missing: bool = False) -> NeutralValueV1:
    if allow_missing and _missing(value):
        return _missing_value()
    if isinstance(value, str) and value.startswith("Constant(value=") and value.endswith(")"):
        try:
            value = ast.literal_eval(value[len("Constant(value=") : -1])
        except (SyntaxError, ValueError) as error:
            raise NeutralProjectionError("invalid private Python literal encoding") from error
    if value is None or isinstance(value, (bool, int, str)):
        try:
            return NeutralValueV1(kind="python_literal", value=value)
        except ValidationError as error:
            raise NeutralProjectionError(
                "Python literal is outside the neutral contract"
            ) from error
    raise NeutralProjectionError("unsupported Python literal encoding")


def _python_annotation(value: object) -> NeutralValueV1:
    if _missing(value) or value is None:
        return _missing_value()
    if not isinstance(value, str):
        raise NeutralProjectionError("annotation must be a string or missing")
    private_name = re.fullmatch(r"Name\(id=(['\"])([A-Za-z_]\w*)\1, ctx=Load\(\)\)", value)
    if private_name is not None:
        normalized = private_name.group(2)
    else:
        try:
            normalized = ast.unparse(ast.parse(value, mode="eval").body)
        except (SyntaxError, ValueError) as error:
            raise NeutralProjectionError("annotation cannot be canonicalized") from error
    try:
        return NeutralValueV1(kind="python_annotation", value=normalized)
    except ValidationError as error:
        raise NeutralProjectionError("annotation is outside the neutral contract") from error


def _symbol_value(value: object) -> NeutralValueV1:
    if not isinstance(value, str):
        raise NeutralProjectionError("symbol value must be a canonical FQN string")
    try:
        return NeutralValueV1(kind="symbol_fqn", value=value)
    except ValidationError as error:
        raise NeutralProjectionError("symbol value is outside the neutral contract") from error


def _text_value(value: object) -> NeutralValueV1:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return NeutralValueV1(kind="text", value=rendered)
    except (TypeError, ValueError, ValidationError) as error:
        raise NeutralProjectionError("unsupported finding value cannot be encoded") from error


def _symbol_fqn(key: MatchingKey) -> str:
    identity = key.symbol_identity
    parts = [identity.module]
    if identity.owner is not None:
        parts.append(identity.owner)
    parts.append(identity.name)
    return ".".join(parts)


def project_drift_finding(finding: PublicDriftFinding) -> NeutralFindingKeyV1:
    """Project one public Drift finding without consulting the hidden oracle."""

    observed = observed_finding_from_domain(finding).key
    common: dict[str, Any] = {
        "code_path": observed.code_path,
        "doc_path": observed.doc_path,
        "symbol_fqn": _symbol_fqn(observed),
    }
    kind = observed.kind
    old_value = observed.old_value
    new_value = observed.new_value

    try:
        if kind == "parameter_added":
            return NeutralFindingKeyV1(
                **common,
                finding_family="parameter_added",
                component_kind="parameter",
                component_name=observed.component,
                old_value=_missing_value(),
                new_value=_present_value(),
            )
        if kind == "parameter_removed":
            return NeutralFindingKeyV1(
                **common,
                finding_family="parameter_removed",
                component_kind="parameter",
                component_name=observed.component,
                old_value=_present_value(),
                new_value=_missing_value(),
            )
        if kind == "parameter_default_changed":
            return NeutralFindingKeyV1(
                **common,
                finding_family="parameter_default_changed",
                component_kind="parameter",
                component_name=observed.component,
                old_value=_python_literal(old_value, allow_missing=True),
                new_value=_python_literal(new_value, allow_missing=True),
            )
        if kind == "parameter_annotation_changed":
            return NeutralFindingKeyV1(
                **common,
                finding_family="parameter_annotation_changed",
                component_kind="parameter",
                component_name=observed.component,
                old_value=_python_annotation(old_value),
                new_value=_python_annotation(new_value),
            )
        if kind == "return_annotation_changed":
            return NeutralFindingKeyV1(
                **common,
                finding_family="return_annotation_changed",
                component_kind="return",
                component_name=None,
                old_value=_python_annotation(old_value),
                new_value=_python_annotation(new_value),
            )
        if kind == "docstring_parameter_changed":
            return NeutralFindingKeyV1(
                **common,
                finding_family="google_arg_changed",
                component_kind="parameter",
                component_name=observed.component,
                old_value=(_present_value() if old_value is None else _python_literal(old_value)),
                new_value=(_missing_value() if _missing(new_value) else _python_literal(new_value)),
            )
        if kind == "docstring_return_changed":
            return NeutralFindingKeyV1(
                **common,
                finding_family="google_returns_changed",
                component_kind="return",
                component_name=None,
                old_value=_python_annotation(old_value),
                new_value=_python_annotation(new_value),
            )
        if kind == "symbol_reference_renamed":
            return NeutralFindingKeyV1(
                **common,
                finding_family="symbol_renamed",
                component_kind="symbol",
                component_name=None,
                old_value=_symbol_value(old_value),
                new_value=_symbol_value(new_value),
            )
        if kind == "symbol_reference_deleted":
            return NeutralFindingKeyV1(
                **common,
                finding_family="symbol_deleted",
                component_kind="symbol",
                component_name=None,
                old_value=_symbol_value(old_value),
                new_value=_missing_value(),
            )
        if kind == "broken_example":
            component_kind = observed.component.split(":", 1)[0]
            if component_kind not in {"doctest", "pytest"}:
                raise NeutralProjectionError("unknown executable validation kind")
            return NeutralFindingKeyV1(
                code_path=observed.code_path,
                doc_path=observed.doc_path,
                symbol_fqn=None,
                finding_family="broken_example",
                component_kind=cast(Literal["doctest", "pytest"], component_kind),
                component_name=None,
                old_value=NeutralValueV1(kind="validation_status", value="passed"),
                new_value=NeutralValueV1(kind="validation_status", value="failed"),
            )
        if kind in {"semantic_direct_mismatch", "semantic_over_promise"}:
            return NeutralFindingKeyV1(
                **common,
                finding_family="semantic_literal_changed",
                component_kind="semantic_literal",
                component_name="return",
                old_value=_python_literal(old_value),
                new_value=_python_literal(new_value),
            )
        if kind == "unsupported":
            return NeutralFindingKeyV1(
                **common,
                finding_family="ambiguous_or_unsupported",
                component_kind="unsupported",
                component_name=None,
                old_value=_symbol_value(old_value),
                new_value=(_missing_value() if _missing(new_value) else _symbol_value(new_value)),
            )
        return NeutralFindingKeyV1(
            **common,
            finding_family="ambiguous_or_unsupported",
            component_kind="unsupported",
            component_name=None,
            old_value=_text_value(old_value),
            new_value=_text_value(new_value),
        )
    except ValidationError as error:
        raise NeutralProjectionError("finding violates NeutralFindingEncodingV1") from error


def _derived_abstention(
    operation: Literal["check", "repair"],
    status: DeclaredStatus,
    *,
    mutation_empty: bool,
) -> bool:
    return operation == "repair" and mutation_empty and status in _ABSTENTION_STATUSES


def project_codex_subject_result(
    result: object,
    *,
    operation: Literal["check", "repair"],
    mutation_empty: bool,
) -> NeutralSubjectResultV1 | None:
    """Return None when a scoreable Codex result is invalid for neutral scoring."""

    try:
        parsed = (
            result
            if isinstance(result, CodexTaskResultV1)
            else CodexTaskResultV1.model_validate(result, strict=False)
        )
        parsed.validate_for_task(BenchmarkTaskV1(operation=operation))
        status = parsed.declared_status
        return NeutralSubjectResultV1(
            operation=operation,
            status=status,
            findings=tuple(finding.key for finding in parsed.findings),
            validation_claims=parsed.validation_claims,
            derived_abstention=_derived_abstention(
                operation, status, mutation_empty=mutation_empty
            ),
        )
    except (ValidationError, ValueError, TypeError, NeutralProjectionError):
        return None


def project_drift_subject_result(
    result: object,
    *,
    operation: Literal["check", "repair"],
    mutation_empty: bool,
) -> NeutralSubjectResultV1 | None:
    """Return None for malformed, duplicate, or unprojectable Drift output."""

    try:
        bundle = (
            result if isinstance(result, PublicBundleV3) else PublicBundleV3.model_validate(result)
        )
        findings = tuple(project_drift_finding(finding) for finding in bundle.findings)
        identities = tuple(canonical_json_bytes(finding) for finding in findings)
        if len(set(identities)) != len(identities):
            raise NeutralProjectionError("duplicate neutral finding keys")
        raw_status = bundle.status.value
        if raw_status not in {
            "clean",
            "drift_found",
            "fixed",
            "partial",
            "needs_approval",
            "unresolved",
            "stale",
            "failed",
        }:
            raise NeutralProjectionError("unknown Drift status")
        status = cast(DeclaredStatus, raw_status)
        if status == "clean" and findings:
            raise NeutralProjectionError("clean status cannot carry findings")
        return NeutralSubjectResultV1(
            operation=operation,
            status=status,
            findings=findings,
            validation_claims=(),
            derived_abstention=_derived_abstention(
                operation, status, mutation_empty=mutation_empty
            ),
        )
    except (ValidationError, ValueError, TypeError, NeutralProjectionError):
        return None


def _stream_receipt_name(subject: str) -> str:
    return "events" if subject == "codex" else "stdout"


def _assert_stream_matches(
    stream: StreamEvidence,
    evidence: RawRunEvidenceV1,
    *,
    name: str,
) -> None:
    receipts = [receipt for receipt in evidence.streams if receipt.stream_name == name]
    if len(receipts) != 1:
        raise TrustedScoringIntegrityError(f"raw evidence must contain one {name} receipt")
    receipt = receipts[0]
    actual = (
        stream.bytes_read,
        stream.bytes_stored,
        stream.byte_limit,
        stream.truncated,
        stream.raw_sha256,
        stream.redacted_sha256,
        stream.explicit_secret_replacements + stream.generic_replacements,
    )
    expected = (
        receipt.total_bytes,
        receipt.captured_bytes,
        receipt.byte_limit,
        receipt.truncated,
        receipt.raw_sha256,
        receipt.redacted_sha256,
        receipt.replacement_count,
    )
    if actual != expected:
        raise TrustedScoringIntegrityError(f"{name} stream differs from sealed evidence")


def _known_usage(run: SubjectRunResult) -> dict[str, int | None]:
    known: dict[str, int | None] = {
        "model_calls": None,
        "strong_model_calls": None,
        "tool_calls": None,
        "input_tokens": None,
        "output_tokens": None,
        "cost_nano_usd": None,
        "duration_ms": run.terminal.duration_ms,
    }
    if run.subject == "codex":
        protocol = run.codex_protocol
        if protocol is not None:
            known.update(
                tool_calls=protocol.usage.tool_calls,
                input_tokens=protocol.usage.input_tokens,
                output_tokens=protocol.usage.output_tokens,
            )
        return known
    if not isinstance(run.parsed_result, PublicBundleV3):
        return known
    usage = run.parsed_result.usage
    nanos = Decimal(str(usage.estimated_cost_usd)) * Decimal(1_000_000_000)
    if nanos != nanos.to_integral_value():
        raise TrustedScoringIntegrityError("Drift cost cannot be represented in nano-USD")
    known.update(
        model_calls=usage.model_calls,
        strong_model_calls=usage.model_calls_by_profile.get("strong", 0),
        tool_calls=usage.tool_calls,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost_nano_usd=int(nanos),
    )
    return known


def _validate_raw_usage(
    raw: RawUsageEvidenceV1,
    known: Mapping[str, int | None],
) -> None:
    for name, value in known.items():
        metric = getattr(raw, name)
        if value is None:
            if metric.value is not None or metric.status == "measured":
                raise TrustedScoringIntegrityError(f"unknown {name} may not become measured usage")
        elif metric.status != "measured" or metric.value != value:
            raise TrustedScoringIntegrityError(
                f"known {name} must exactly match measured raw usage"
            )


def _comparison_usage(
    run: SubjectRunResult,
    raw: RawUsageEvidenceV1,
) -> ComparisonUsage:
    known = _known_usage(run)
    _validate_raw_usage(raw, known)
    all_known = all(value is not None for value in known.values())
    if run.subject == "drift_agent" and all_known:
        return ComparisonUsage(status="measured", reason=None, **known)
    reason = _CODEX_USAGE_INCOMPLETE if run.subject == "codex" else _FAILED_USAGE_INCOMPLETE
    return ComparisonUsage(status="accounting_incomplete", reason=reason, **known)


def _parsed_result_sha256(result: object) -> str:
    if isinstance(result, BaseModel):
        return canonical_sha256(result)
    return canonical_sha256(result)


def _validate_evidence(
    prepared: PreparedBenchmarkCase,
    run: SubjectRunResult,
    pre_snapshot: CanonicalRepositorySnapshotV1,
    post_snapshot: CanonicalRepositorySnapshotV1,
    pre_git_metadata: CanonicalGitMetadataV1,
    post_git_metadata: CanonicalGitMetadataV1,
    evidence: RawRunEvidenceV1,
) -> None:
    oracle = prepared.hidden_oracle
    identity = (
        run.subject == evidence.subject
        and evidence.dataset_id == prepared.dataset_id == oracle.dataset_id
        and evidence.case_id == prepared.case_id == oracle.case_id
        and evidence.case_manifest_sha256
        == prepared.case_manifest_sha256
        == oracle.case_manifest_sha256
        and evidence.snapshot_digest == prepared.snapshot_digest
        and evidence.task_digest == prepared.task_digest
        and evidence.scope_digest == prepared.scope_digest
        and evidence.pre_snapshot_digest == canonical_digest(pre_snapshot)
        and evidence.post_snapshot_digest == canonical_digest(post_snapshot)
        and evidence.pre_git_metadata_sha256 == git_metadata_sha256(pre_git_metadata)
        and evidence.post_git_metadata_sha256 == git_metadata_sha256(post_git_metadata)
        and run.request.operation == prepared.task.operation == oracle.operation
    )
    if not identity:
        raise TrustedScoringIntegrityError("case, task, snapshot, or evidence identity mismatch")
    if pre_snapshot != prepared.prepared_snapshot:
        raise TrustedScoringIntegrityError("pre-subject snapshot is not the prepared snapshot")
    if pre_git_metadata != prepared.prepared_git_metadata:
        raise TrustedScoringIntegrityError("pre-subject Git metadata is not the prepared state")
    terminal = evidence.terminal
    expected_exit = (
        run.terminal.returncode
        if run.terminal.returncode is not None and run.terminal.returncode >= 0
        else None
    )
    terminal_matches = (
        terminal.process_started == run.terminal.started
        and terminal.terminal_classification == run.terminal.classification
        and terminal.exit_code == expected_exit
        and terminal.signal == run.terminal.signal_number
        and terminal.timed_out == run.terminal.timed_out
        and terminal.duration_ms == run.terminal.duration_ms
    )
    if not terminal_matches:
        raise TrustedScoringIntegrityError("runner terminal receipt differs from raw evidence")
    if not run.terminal.started or not run.terminal.scoreable:
        raise TrustedScoringIntegrityError(
            "infrastructure or protocol failure may not generate an observation"
        )
    if evidence.rendered_input_sha256 != run.request.stdin_sha256:
        raise TrustedScoringIntegrityError("rendered input digest differs from the request")
    if evidence.effective_request_sha256 != effective_request_sha256(run):
        raise TrustedScoringIntegrityError("effective request receipt cannot be reproduced")
    _assert_stream_matches(
        run.stdout,
        evidence,
        name=_stream_receipt_name(run.subject),
    )
    _assert_stream_matches(run.stderr, evidence, name="stderr")
    replacement_count = sum(
        stream.explicit_secret_replacements + stream.generic_replacements
        for stream in (run.stdout, run.stderr)
    )
    if (
        evidence.redaction.policy_version != run.stdout.redaction_policy_version
        or run.stderr.redaction_policy_version != run.stdout.redaction_policy_version
        or evidence.redaction.replacement_count != replacement_count
        or evidence.redaction.secret_detected
    ):
        raise TrustedScoringIntegrityError("redaction receipt differs or contains a secret")
    if run.parsed_result is None:
        if evidence.final_result_sha256 is not None:
            raise TrustedScoringIntegrityError("missing final result cannot have a final digest")
    elif evidence.final_result_sha256 != _parsed_result_sha256(run.parsed_result):
        raise TrustedScoringIntegrityError("parsed final result differs from sealed evidence")


def _comparison_changes(
    changes: tuple[EffectiveChangedBytesV1, ...],
) -> tuple[ComparisonChangedBytes, ...]:
    result: list[ComparisonChangedBytes] = []
    for change in changes:
        before_hash = None if change.before is None else change.before.sha256
        after_hash = None if change.after is None else change.after.sha256
        # V1 ComparisonChangedBytes represents byte identity only. Mode/kind-only
        # mutations remain safety failures but cannot be serialized in this tuple.
        if before_hash == after_hash:
            continue
        result.append(
            ComparisonChangedBytes(
                path=change.path,
                before_sha256=before_hash,
                after_sha256=after_hash,
            )
        )
    return tuple(result)


def _change_key(change: ComparisonChangedBytes) -> tuple[str, str, str]:
    return (change.path, change.before_sha256 or "", change.after_sha256 or "")


def _is_test_or_config(path: str) -> bool:
    pure = PurePosixPath(path)
    parts = pure.parts
    name = pure.name
    return (
        "tests" in parts
        or name.startswith("test_")
        or name
        in {
            "drift-agent.toml",
            "pyproject.toml",
            "setup.py",
            "setup.cfg",
            "tox.ini",
            "uv.lock",
            "poetry.lock",
            "requirements.txt",
            ".gitignore",
            ".gitattributes",
        }
    )


def _safety(
    prepared: PreparedBenchmarkCase,
    run: SubjectRunResult,
    pre_snapshot: CanonicalRepositorySnapshotV1,
    post_snapshot: CanonicalRepositorySnapshotV1,
    pre_git_metadata: CanonicalGitMetadataV1,
    post_git_metadata: CanonicalGitMetadataV1,
    changes: tuple[EffectiveChangedBytesV1, ...],
) -> tuple[bool, ComparisonSafety]:
    expected_paths = {change.path for change in prepared.hidden_oracle.expected_changed_bytes}
    code_paths = {finding.code_path for finding in prepared.hidden_oracle.findings}
    business_paths: set[str] = set()
    violation = (
        pre_snapshot.head_tree != post_snapshot.head_tree
        or pre_snapshot.index_entries != post_snapshot.index_entries
        or pre_git_metadata != post_git_metadata
        or run.terminal.classification == "tool_profile_violation"
    )
    for change in changes:
        before = change.before
        after = change.after
        path = change.path
        kind_changed = before is not None and after is not None and before.kind != after.kind
        mode_changed = before is not None and after is not None and before.mode != after.mode
        symlink_touched = (before is not None and before.kind == "symlink") or (
            after is not None and after.kind == "symlink"
        )
        if kind_changed or mode_changed or symlink_touched:
            violation = True
        if _is_test_or_config(path):
            violation = True
        is_business = not _is_test_or_config(path) and (
            path in code_paths
            or (PurePosixPath(path).suffix == ".py" and "tests" not in PurePosixPath(path).parts)
        )
        if is_business and path not in expected_paths:
            business_paths.add(path)
            violation = True
    if run.subject == "drift_agent" and isinstance(run.parsed_result, PublicBundleV3):
        if run.parsed_result.usage.model_calls != 0:
            violation = True
    return (
        not violation,
        ComparisonSafety(
            status="accounting_incomplete",
            regression_free=not violation,
            business_code_mutations=len(business_paths),
            stale_overwrites=None,
            reason=_SAFETY_INCOMPLETE,
        ),
    )


def _finding_scores(
    expected: tuple[NeutralFindingKeyV1, ...],
    actual: tuple[NeutralFindingKeyV1, ...],
) -> tuple[int, int, int, bool]:
    expected_ids = {canonical_json_bytes(finding) for finding in expected}
    actual_ids = {canonical_json_bytes(finding) for finding in actual}
    tp = len(expected_ids & actual_ids)
    fp = len(actual_ids - expected_ids)
    fn = len(expected_ids - actual_ids)
    return tp, fp, fn, expected_ids == actual_ids


def score_subject_run(
    prepared: PreparedBenchmarkCase,
    run: SubjectRunResult,
    pre_snapshot: CanonicalRepositorySnapshotV1,
    post_snapshot: CanonicalRepositorySnapshotV1,
    pre_git_metadata: CanonicalGitMetadataV1,
    post_git_metadata: CanonicalGitMetadataV1,
    *,
    evidence: RawRunEvidenceV1,
    budget_source: str,
) -> ComparisonObservationV1:
    """Produce one strict V1 observation from trusted evidence and hidden oracle data."""

    _validate_evidence(
        prepared,
        run,
        pre_snapshot,
        post_snapshot,
        pre_git_metadata,
        post_git_metadata,
        evidence,
    )
    changes = effective_changed_bytes(pre_snapshot, post_snapshot)
    comparison_changes = _comparison_changes(changes)
    metadata_unchanged = (
        pre_snapshot.head_tree == post_snapshot.head_tree
        and pre_snapshot.index_entries == post_snapshot.index_entries
        and pre_git_metadata == post_git_metadata
    )
    mutation_empty = not changes and metadata_unchanged
    if run.parsed_result is None:
        neutral = None
    elif run.subject == "codex":
        neutral = project_codex_subject_result(
            run.parsed_result,
            operation=prepared.task.operation,
            mutation_empty=mutation_empty,
        )
    else:
        neutral = project_drift_subject_result(
            run.parsed_result,
            operation=prepared.task.operation,
            mutation_empty=mutation_empty,
        )
    actual_findings = () if neutral is None else neutral.findings
    tp, fp, fn, findings_exact = _finding_scores(
        prepared.hidden_oracle.findings,
        actual_findings,
    )
    status_matches = (
        neutral is not None and neutral.status == prepared.hidden_oracle.expected_status
    )
    expected_changes = tuple(sorted(prepared.hidden_oracle.expected_changed_bytes, key=_change_key))
    actual_changes = tuple(sorted(comparison_changes, key=_change_key))
    exact_changed_bytes = metadata_unchanged and actual_changes == expected_changes
    safe, safety = _safety(
        prepared,
        run,
        pre_snapshot,
        post_snapshot,
        pre_git_metadata,
        post_git_metadata,
        changes,
    )
    completed = run.terminal.classification == "completed"
    valid_final = neutral is not None
    repair_positive = prepared.task.operation == "repair" and bool(expected_changes)
    abstention_case = prepared.task.operation == "repair" and not expected_changes
    correct_abstention_value = (
        completed
        and valid_final
        and findings_exact
        and status_matches
        and neutral is not None
        and neutral.derived_abstention
        and mutation_empty
        and safe
    )
    if abstention_case:
        correct_abstention = ComparisonBoolMetric(
            status="measured",
            value=correct_abstention_value,
            reason=None,
        )
    else:
        correct_abstention = ComparisonBoolMetric(
            status="not_measured",
            value=None,
            reason="abstention is only applicable to repair cases with no expected mutation",
        )
    successful_repair = (
        repair_positive and completed and valid_final and exact_changed_bytes and safe
    )
    if prepared.task.operation == "check":
        passed = (
            completed
            and valid_final
            and findings_exact
            and status_matches
            and mutation_empty
            and safe
        )
    elif repair_positive:
        passed = (
            completed
            and valid_final
            and findings_exact
            and status_matches
            and exact_changed_bytes
            and safe
        )
    else:
        passed = correct_abstention_value

    pair_key = ComparisonPairKey(
        dataset_id=prepared.dataset_id,
        case_id=prepared.case_id,
        case_manifest_sha256=prepared.case_manifest_sha256,
        trial_id=evidence.trial_id,
        snapshot_digest=prepared.snapshot_digest,
        task_digest=prepared.task_digest,
        scope_digest=prepared.scope_digest,
    )
    provenance = ComparisonProvenance(
        runner_kind=(
            "external_self_declared" if run.subject == "codex" else "local_offline_runner"
        ),
        runner_version=evidence.runner_version,
        model_id=evidence.model_id,
        tool_profile_digest=evidence.tool_profile_digest,
        budget_source=budget_source,
        claim_status=(
            "unverified_external_declaration" if run.subject == "codex" else "locally_verified"
        ),
        authorization_status=(
            "self_declared_not_verified" if run.subject == "codex" else "not_applicable"
        ),
    )
    usage = _comparison_usage(run, evidence.usage)
    return ComparisonObservationV1(
        schema_version=1,
        observation_id=deterministic_observation_id(
            plan_digest=evidence.plan_digest,
            subject=run.subject,
            pair_key=pair_key,
            evidence_sha256=evidence.evidence_sha256,
        ),
        subject=run.subject,
        dataset_id=prepared.dataset_id,
        case_id=prepared.case_id,
        case_manifest_sha256=prepared.case_manifest_sha256,
        trial_id=evidence.trial_id,
        snapshot_digest=prepared.snapshot_digest,
        task_digest=prepared.task_digest,
        scope_digest=prepared.scope_digest,
        evidence_sha256=evidence.evidence_sha256,
        case_layer=prepared.case.layer,
        outcome=ComparisonOutcome(
            passed=passed,
            tp=tp,
            fp=fp,
            fn=fn,
            successful_repair=successful_repair,
            repair_success_at_1=ComparisonBoolMetric(
                status="not_measured",
                value=None,
                reason=_NOT_MEASURED_ATTEMPTS,
            ),
            repair_success_at_2=ComparisonBoolMetric(
                status="not_measured",
                value=None,
                reason=_NOT_MEASURED_ATTEMPTS,
            ),
            correct_abstention=correct_abstention,
        ),
        changed_bytes=actual_changes,
        validation=ComparisonValidation(
            status="not_measured",
            passed=None,
            reason=_VALIDATION_NOT_MEASURED,
        ),
        safety=safety,
        usage=usage,
        provenance=provenance,
    )


__all__ = [
    "NeutralProjectionError",
    "TrustedScoringIntegrityError",
    "effective_request_sha256",
    "project_codex_subject_result",
    "project_drift_finding",
    "project_drift_subject_result",
    "score_subject_run",
]

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from drift_agent.adapters.contracts import (
    PublicBundleV3,
    PublicDriftFinding,
    PublicEvidenceAnchor,
    PublicWorkspaceSnapshot,
)
from drift_agent.domain.enums import RunStatus
from drift_agent.domain.models import Usage
from drift_agent.evaluation.benchmark_cases import (
    PORTABLE_CASE_IDS,
    CanonicalGitMetadataV1,
    CanonicalRepositorySnapshotV1,
    PreparedBenchmarkCase,
    canonical_digest,
    capture_git_metadata,
    capture_repository_snapshot,
    git_metadata_sha256,
    prepare_benchmark_case,
)
from drift_agent.evaluation.benchmark_harness import (
    BenchmarkHarnessError,
    _validate_sealed_final,
    _validate_sealed_streams,
)
from drift_agent.evaluation.benchmark_models import (
    BoundedStreamReceiptV1,
    CodexTaskResultV1,
    NeutralFindingV1,
    RawRunEvidenceV1,
    RawUsageEvidenceV1,
    RawUsageMetricV1,
    RedactionReceiptV1,
    TerminalReceiptV1,
    canonical_json_bytes,
    canonical_sha256,
)
from drift_agent.evaluation.benchmark_runner import (
    CodexProtocolResult,
    CodexUsage,
    EffectiveRequestReceipt,
    StreamEvidence,
    SubjectRunResult,
    TerminalReceipt,
    seal_stream,
)
from drift_agent.evaluation.benchmark_scoring import (
    TrustedScoringIntegrityError,
    effective_request_sha256,
    project_drift_finding,
    project_drift_subject_result,
    score_subject_run,
)

_SHA_A = "a" * 64
_SHA_B = "b" * 64
_TOOL_PROFILE = f"sha256:{'c' * 64}"


def _prepared(tmp_path: Path, case_id: str) -> PreparedBenchmarkCase:
    return prepare_benchmark_case(case_id, tmp_path / "subjects", opaque_id="1" * 32)


def _codex_result(
    prepared: PreparedBenchmarkCase,
    *,
    include_findings: bool = True,
    status: str | None = None,
) -> CodexTaskResultV1:
    findings = (
        tuple(
            NeutralFindingV1(
                **key.model_dump(mode="python"),
                explanation="fixture-neutral finding",
            )
            for key in prepared.hidden_oracle.findings
        )
        if include_findings
        else ()
    )
    return CodexTaskResultV1(
        schema_version=1,
        declared_status=status or prepared.hidden_oracle.expected_status,
        findings=findings,
        validation_claims=(),
    )


def _receipt(name: str, stream: StreamEvidence) -> BoundedStreamReceiptV1:
    return BoundedStreamReceiptV1(
        stream_name=name,
        total_bytes=stream.bytes_read,
        captured_bytes=stream.bytes_stored,
        byte_limit=stream.byte_limit,
        truncated=stream.truncated,
        raw_sha256=stream.raw_sha256,
        redacted_sha256=stream.redacted_sha256,
        replacement_count=(stream.explicit_secret_replacements + stream.generic_replacements),
    )


def _measured(value: int, source: str = "supervisor") -> RawUsageMetricV1:
    return RawUsageMetricV1(status="measured", value=value, evidence_source=source)


def _unknown(reason: str = "not exposed") -> RawUsageMetricV1:
    return RawUsageMetricV1(status="accounting_incomplete", reason=reason)


def _raw_usage(
    subject: str,
    parsed_result: object | None,
    protocol: CodexProtocolResult | None,
    duration_ms: int,
) -> RawUsageEvidenceV1:
    if subject == "codex":
        usage = protocol.usage if protocol is not None else CodexUsage()
        return RawUsageEvidenceV1(
            model_calls=_unknown(),
            strong_model_calls=_unknown(),
            tool_calls=_measured(usage.tool_calls, "codex-jsonl-items"),
            input_tokens=(
                _unknown()
                if usage.input_tokens is None
                else _measured(usage.input_tokens, "codex-terminal-usage")
            ),
            output_tokens=(
                _unknown()
                if usage.output_tokens is None
                else _measured(usage.output_tokens, "codex-terminal-usage")
            ),
            cost_nano_usd=_unknown("billing receipt unavailable"),
            duration_ms=_measured(duration_ms, "supervisor-monotonic-clock"),
        )
    if isinstance(parsed_result, PublicBundleV3):
        bundle_usage = parsed_result.usage
        return RawUsageEvidenceV1(
            model_calls=_measured(bundle_usage.model_calls, "drift-public-v3"),
            strong_model_calls=_measured(
                bundle_usage.model_calls_by_profile.get("strong", 0),
                "drift-public-v3",
            ),
            tool_calls=_measured(bundle_usage.tool_calls, "drift-public-v3"),
            input_tokens=_measured(bundle_usage.input_tokens, "drift-public-v3"),
            output_tokens=_measured(bundle_usage.output_tokens, "drift-public-v3"),
            cost_nano_usd=_measured(0, "drift-public-v3"),
            duration_ms=_measured(duration_ms, "supervisor-monotonic-clock"),
        )
    return RawUsageEvidenceV1(
        model_calls=_unknown(),
        strong_model_calls=_unknown(),
        tool_calls=_unknown(),
        input_tokens=_unknown(),
        output_tokens=_unknown(),
        cost_nano_usd=_unknown(),
        duration_ms=_measured(duration_ms, "supervisor-monotonic-clock"),
    )


def _run_and_evidence(
    prepared: PreparedBenchmarkCase,
    post_snapshot: CanonicalRepositorySnapshotV1,
    post_git_metadata: CanonicalGitMetadataV1,
    *,
    subject: str = "codex",
    parsed_result: object | None,
    classification: str = "completed",
    scoreable: bool = True,
) -> tuple[SubjectRunResult, RawRunEvidenceV1]:
    stdout = seal_stream(b"sealed-result\n", total_bytes=14, byte_limit=4096)
    stderr = seal_stream(b"", total_bytes=0, byte_limit=4096)
    duration_ms = 17
    request = EffectiveRequestReceipt(
        operation=prepared.task.operation,
        adapter_version="test-adapter-v1",
        argv=("/opaque/subject", prepared.task.operation),
        stdin_sha256=_SHA_A,
    )
    protocol = (
        CodexProtocolResult(
            events=(),
            terminal_type=("turn.completed" if classification == "completed" else None),
            has_turn_activity=True,
            final_result=parsed_result,
            final_error=None if parsed_result is not None else "invalid final",
            usage=CodexUsage(input_tokens=11, output_tokens=7, tool_calls=2),
            tool_profile_violations=(),
            terminal_failure_class=None,
        )
        if subject == "codex"
        else None
    )
    terminal = TerminalReceipt(
        started=True,
        classification=classification,
        scoreable=scoreable,
        returncode=0,
        signal_number=None,
        duration_ms=duration_ms,
        timed_out=False,
        output_limited=False,
    )
    run = SubjectRunResult(
        subject=subject,
        request=request,
        terminal=terminal,
        stdout=stdout,
        stderr=stderr,
        parsed_result=parsed_result,
        codex_protocol=protocol,
    )
    stdout_name = "events" if subject == "codex" else "stdout"
    streams = (_receipt(stdout_name, stdout), _receipt("stderr", stderr))
    terminal_v1 = TerminalReceiptV1(
        plan_digest=_SHA_B,
        slot_id="slot-001",
        run_class="portable",
        subject=subject,
        dataset_id=prepared.dataset_id,
        case_id=prepared.case_id,
        trial_id="trial-1",
        process_started=True,
        terminal_classification=classification,
        exit_code=0,
        signal=None,
        timed_out=False,
        duration_ms=duration_ms,
        streams=streams,
        available_artifacts=(),
    )
    evidence = RawRunEvidenceV1(
        plan_digest=_SHA_B,
        authorization_ledger_sha256=_SHA_A,
        subject=subject,
        dataset_id=prepared.dataset_id,
        case_id=prepared.case_id,
        trial_id="trial-1",
        case_manifest_sha256=prepared.case_manifest_sha256,
        snapshot_digest=prepared.snapshot_digest,
        task_digest=prepared.task_digest,
        scope_digest=prepared.scope_digest,
        tool_profile_digest=_TOOL_PROFILE,
        runner_version="test-runner-v1",
        runner_binary_sha256=_SHA_A,
        model_id="test-codex" if subject == "codex" else "none",
        effective_request_sha256=effective_request_sha256(run),
        rendered_input_sha256=request.stdin_sha256,
        terminal=terminal_v1,
        pre_snapshot_digest=canonical_digest(prepared.prepared_snapshot),
        post_snapshot_digest=canonical_digest(post_snapshot),
        pre_git_metadata_sha256=git_metadata_sha256(prepared.prepared_git_metadata),
        post_git_metadata_sha256=git_metadata_sha256(post_git_metadata),
        streams=streams,
        redaction=RedactionReceiptV1(
            policy_version=stdout.redaction_policy_version,
            replacement_count=0,
            secret_detected=False,
        ),
        final_result_sha256=(None if parsed_result is None else canonical_sha256(parsed_result)),
        usage=_raw_usage(subject, parsed_result, protocol, duration_ms),
    )
    return run, evidence


def _score(
    prepared: PreparedBenchmarkCase,
    post_snapshot: CanonicalRepositorySnapshotV1,
    post_git_metadata: CanonicalGitMetadataV1,
    run: SubjectRunResult,
    evidence: RawRunEvidenceV1,
):
    return score_subject_run(
        prepared,
        run,
        prepared.prepared_snapshot,
        post_snapshot,
        prepared.prepared_git_metadata,
        post_git_metadata,
        evidence=evidence,
        budget_source="manual benchmark authorization",
    )


def _apply_expected_fixture(prepared: PreparedBenchmarkCase) -> None:
    expected = [fixture for fixture in prepared.case.manifest.files if fixture.role == "expected"]
    assert len(expected) == 1
    fixture = expected[0]
    shutil.copyfile(
        prepared.case.case_root / fixture.path,
        prepared.repo_path / fixture.target_path,
    )


def _public_finding(prepared: PreparedBenchmarkCase, index: int = 0) -> PublicDriftFinding:
    matching = prepared.case.manifest.expected.finding_multiset[index]
    identity = matching.symbol_identity
    symbol = ".".join(
        part for part in (identity.module, identity.owner, identity.name) if part is not None
    )
    return PublicDriftFinding(
        id=f"finding-{index}",
        symbol_id=symbol,
        code_evidence=PublicEvidenceAnchor(
            path=matching.code_path,
            line=1,
            source_hash=_SHA_A,
        ),
        doc_evidence=PublicEvidenceAnchor(
            path=matching.doc_path,
            line=1,
            source_hash=_SHA_B,
        ),
        reason="fixture finding",
        kind=matching.kind,
        component_id=matching.component,
        old_value=matching.old_value,
        new_value=matching.new_value,
        detector_id=matching.detector_id,
        detector_version=matching.detector_version,
    )


def _bundle(
    status: RunStatus,
    *,
    findings: list[PublicDriftFinding] | None = None,
) -> PublicBundleV3:
    return PublicBundleV3(
        status=status,
        run_id="run-test",
        snapshot=PublicWorkspaceSnapshot(
            head_revision="HEAD",
            workspace_fingerprint=_SHA_A,
            input_file_hashes={},
        ),
        scope=[],
        findings=findings or [],
        usage=Usage(),
    )


def test_clean_check_passes_with_incomplete_codex_usage(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path, "executable.doctest-pass.v1")
    result = _codex_result(prepared)
    run, evidence = _run_and_evidence(
        prepared,
        prepared.prepared_snapshot,
        prepared.prepared_git_metadata,
        parsed_result=result,
    )

    observation = _score(
        prepared,
        prepared.prepared_snapshot,
        prepared.prepared_git_metadata,
        run,
        evidence,
    )

    assert observation.outcome.passed is True
    assert (observation.outcome.tp, observation.outcome.fp, observation.outcome.fn) == (0, 0, 0)
    assert observation.changed_bytes == ()
    assert observation.validation.status == "not_measured"
    assert observation.safety.status == "accounting_incomplete"
    assert observation.safety.regression_free is True
    assert observation.usage.status == "accounting_incomplete"
    assert observation.usage.duration_ms == 17
    assert observation.usage.model_calls is None


def test_exact_repair_passes_and_is_successful(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path, "click.parameter-default.v1")
    _apply_expected_fixture(prepared)
    post = capture_repository_snapshot(prepared.repo_path)
    post_git = capture_git_metadata(prepared.repo_path)
    run, evidence = _run_and_evidence(
        prepared,
        post,
        post_git,
        parsed_result=_codex_result(prepared),
    )

    observation = _score(prepared, post, post_git, run, evidence)

    assert observation.outcome.passed is True
    assert observation.outcome.successful_repair is True
    assert (observation.outcome.tp, observation.outcome.fp, observation.outcome.fn) == (1, 0, 0)
    assert observation.changed_bytes == prepared.hidden_oracle.expected_changed_bytes


def test_exact_patch_can_succeed_while_missing_finding_fails_overall(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path, "click.parameter-default.v1")
    _apply_expected_fixture(prepared)
    post = capture_repository_snapshot(prepared.repo_path)
    post_git = capture_git_metadata(prepared.repo_path)
    run, evidence = _run_and_evidence(
        prepared,
        post,
        post_git,
        parsed_result=_codex_result(prepared, include_findings=False),
    )

    observation = _score(prepared, post, post_git, run, evidence)

    assert observation.outcome.successful_repair is True
    assert observation.outcome.passed is False
    assert (observation.outcome.tp, observation.outcome.fp, observation.outcome.fn) == (0, 0, 1)


def test_no_change_conflict_scores_correct_abstention(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path, "click.conflict.v1")
    run, evidence = _run_and_evidence(
        prepared,
        prepared.prepared_snapshot,
        prepared.prepared_git_metadata,
        parsed_result=_codex_result(prepared),
    )

    observation = _score(
        prepared,
        prepared.prepared_snapshot,
        prepared.prepared_git_metadata,
        run,
        evidence,
    )

    assert observation.outcome.passed is True
    assert observation.outcome.correct_abstention.status == "measured"
    assert observation.outcome.correct_abstention.value is True
    assert observation.outcome.successful_repair is False


def test_scoreable_invalid_final_stays_in_denominator_with_empty_findings(
    tmp_path: Path,
) -> None:
    prepared = _prepared(tmp_path, "click.parameter-default.v1")
    run, evidence = _run_and_evidence(
        prepared,
        prepared.prepared_snapshot,
        prepared.prepared_git_metadata,
        parsed_result=None,
        classification="invalid_final_schema",
    )

    observation = _score(
        prepared,
        prepared.prepared_snapshot,
        prepared.prepared_git_metadata,
        run,
        evidence,
    )

    assert observation.outcome.passed is False
    assert observation.outcome.successful_repair is False
    assert (observation.outcome.tp, observation.outcome.fp, observation.outcome.fn) == (0, 0, 1)
    assert observation.usage.status == "accounting_incomplete"


def test_non_scoreable_failure_cannot_generate_observation(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path, "executable.doctest-pass.v1")
    result = _codex_result(prepared)
    run, evidence = _run_and_evidence(
        prepared,
        prepared.prepared_snapshot,
        prepared.prepared_git_metadata,
        parsed_result=result,
        scoreable=False,
    )

    with pytest.raises(TrustedScoringIntegrityError, match="may not generate"):
        _score(
            prepared,
            prepared.prepared_snapshot,
            prepared.prepared_git_metadata,
            run,
            evidence,
        )


def test_business_code_mutation_is_unsafe_but_keeps_detection_score(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path, "click.parameter-default.v1")
    source = prepared.repo_path / "src/click_eval/api.py"
    source.write_text(source.read_text(encoding="utf-8") + "\n# unsafe\n", encoding="utf-8")
    post = capture_repository_snapshot(prepared.repo_path)
    post_git = capture_git_metadata(prepared.repo_path)
    run, evidence = _run_and_evidence(
        prepared,
        post,
        post_git,
        parsed_result=_codex_result(prepared),
    )

    observation = _score(prepared, post, post_git, run, evidence)

    assert (observation.outcome.tp, observation.outcome.fp, observation.outcome.fn) == (1, 0, 0)
    assert observation.outcome.passed is False
    assert observation.safety.regression_free is False
    assert observation.safety.business_code_mutations == 1


def test_git_config_mutation_is_unsafe_and_breaks_abstention(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path, "click.conflict.v1")
    subprocess.run(
        ["git", "config", "benchmark.mutated", "yes"],
        cwd=prepared.repo_path,
        check=True,
    )
    post = capture_repository_snapshot(prepared.repo_path)
    post_git = capture_git_metadata(prepared.repo_path)
    run, evidence = _run_and_evidence(
        prepared,
        post,
        post_git,
        parsed_result=_codex_result(prepared),
    )

    observation = _score(prepared, post, post_git, run, evidence)

    assert observation.changed_bytes == ()
    assert observation.safety.regression_free is False
    assert observation.outcome.correct_abstention.value is False
    assert observation.outcome.passed is False


def test_drift_finding_projects_to_same_neutral_oracle(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path, "click.parameter-default.v1")

    projected = project_drift_finding(_public_finding(prepared))

    assert projected == prepared.hidden_oracle.findings[0]


def test_all_portable_drift_findings_round_trip_through_neutral_ontology(
    tmp_path: Path,
) -> None:
    projected_count = 0
    for case_index, case_id in enumerate(PORTABLE_CASE_IDS):
        prepared = _prepared(tmp_path / f"case-{case_index}", case_id)
        for finding_index, expected in enumerate(prepared.hidden_oracle.findings):
            assert project_drift_finding(_public_finding(prepared, finding_index)) == expected
            projected_count += 1

    assert projected_count == 14


def test_duplicate_drift_keys_fail_the_entire_neutral_projection(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path, "click.parameter-default.v1")
    finding = _public_finding(prepared)
    bundle = _bundle(RunStatus.UNRESOLVED, findings=[finding, finding])

    projected = project_drift_subject_result(
        bundle,
        operation="repair",
        mutation_empty=True,
    )

    assert projected is None


def test_valid_drift_check_has_measured_zero_model_usage(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path, "executable.doctest-pass.v1")
    bundle = _bundle(RunStatus.CLEAN)
    run, evidence = _run_and_evidence(
        prepared,
        prepared.prepared_snapshot,
        prepared.prepared_git_metadata,
        subject="drift_agent",
        parsed_result=bundle,
    )

    observation = _score(
        prepared,
        prepared.prepared_snapshot,
        prepared.prepared_git_metadata,
        run,
        evidence,
    )

    assert observation.outcome.passed is True
    assert observation.usage.status == "measured"
    assert observation.usage.model_calls == 0
    assert observation.usage.cost_nano_usd == 0


def test_evidence_snapshot_mismatch_fails_closed(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path, "executable.doctest-pass.v1")
    run, evidence = _run_and_evidence(
        prepared,
        prepared.prepared_snapshot,
        prepared.prepared_git_metadata,
        parsed_result=_codex_result(prepared),
    )
    evidence = evidence.model_copy(update={"post_snapshot_digest": f"sha256:{'d' * 64}"})

    with pytest.raises(TrustedScoringIntegrityError, match="identity mismatch"):
        _score(
            prepared,
            prepared.prepared_snapshot,
            prepared.prepared_git_metadata,
            run,
            evidence,
        )


def test_observation_is_byte_deterministic_for_same_evidence(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path, "executable.pytest-pass.v1")
    run, evidence = _run_and_evidence(
        prepared,
        prepared.prepared_snapshot,
        prepared.prepared_git_metadata,
        parsed_result=_codex_result(prepared),
    )

    first = _score(
        prepared,
        prepared.prepared_snapshot,
        prepared.prepared_git_metadata,
        run,
        evidence,
    )
    second = _score(
        prepared,
        prepared.prepared_snapshot,
        prepared.prepared_git_metadata,
        run,
        evidence,
    )

    assert first == second
    assert first.observation_id.startswith("obs_v1_codex_")
    assert canonical_sha256(first) == canonical_sha256(second)


def _write_codex_replay_artifacts(
    directory: Path,
    run: SubjectRunResult,
    result: CodexTaskResultV1,
) -> None:
    directory.mkdir()
    (directory / "events.raw.jsonl").write_bytes(run.stdout.sealed_raw)
    (directory / "events.redacted.jsonl").write_bytes(run.stdout.redacted)
    (directory / "stderr.raw.bin").write_bytes(run.stderr.sealed_raw)
    (directory / "stderr.redacted.txt").write_bytes(run.stderr.redacted)
    (directory / "final-result.json").write_bytes(canonical_json_bytes(result) + b"\n")


def test_replay_rejects_sealed_raw_stream_tamper(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path / "case", "executable.pytest-pass.v1")
    result = _codex_result(prepared)
    run, evidence = _run_and_evidence(
        prepared,
        prepared.prepared_snapshot,
        prepared.prepared_git_metadata,
        parsed_result=result,
    )
    artifacts = tmp_path / "artifacts"
    _write_codex_replay_artifacts(artifacts, run, result)
    (artifacts / "events.raw.jsonl").write_bytes(b"tampered\n")

    with pytest.raises(BenchmarkHarnessError, match="sealed events stream"):
        _validate_sealed_streams(
            directory=artifacts,
            evidence=evidence,
            artifact_byte_limit=4096,
        )


def test_replay_rejects_redacted_stream_tamper(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path / "case", "executable.pytest-pass.v1")
    result = _codex_result(prepared)
    run, evidence = _run_and_evidence(
        prepared,
        prepared.prepared_snapshot,
        prepared.prepared_git_metadata,
        parsed_result=result,
    )
    artifacts = tmp_path / "artifacts"
    _write_codex_replay_artifacts(artifacts, run, result)
    (artifacts / "stderr.redacted.txt").write_bytes(b"tampered")

    with pytest.raises(BenchmarkHarnessError, match="sealed stderr stream"):
        _validate_sealed_streams(
            directory=artifacts,
            evidence=evidence,
            artifact_byte_limit=4096,
        )


def test_replay_rejects_final_artifact_tamper(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path / "case", "executable.pytest-pass.v1")
    result = _codex_result(prepared)
    run, evidence = _run_and_evidence(
        prepared,
        prepared.prepared_snapshot,
        prepared.prepared_git_metadata,
        parsed_result=result,
    )
    artifacts = tmp_path / "artifacts"
    _write_codex_replay_artifacts(artifacts, run, result)
    (artifacts / "final-result.json").write_bytes(canonical_json_bytes(result) + b" \n")

    with pytest.raises(BenchmarkHarnessError, match="invalid benchmark artifact"):
        _validate_sealed_final(directory=artifacts, evidence=evidence)

from __future__ import annotations

import json
import math

import pytest
from pydantic import ValidationError

from drift_agent.evaluation.benchmark_models import (
    CONTROL_CASE_IDS,
    PORTABLE_CASE_IDS,
    V1_MISSING_METRICS,
    BenchmarkAggregateLabelsV1,
    BenchmarkArtifactDigestsV1,
    BenchmarkCaseSelectionV1,
    BenchmarkCodexRuntimeV1,
    BenchmarkContractDigestsV1,
    BenchmarkCoverageSummaryV1,
    BenchmarkDatasetCatalogV1,
    BenchmarkDriftRuntimeV1,
    BenchmarkLimitsV1,
    BenchmarkPlanV1,
    BenchmarkReportV1,
    BenchmarkTaskV1,
    BenchmarkToolchainV1,
    BoundedStreamReceiptV1,
    CodexTaskResultV1,
    ControlReportV1,
    ControlResultV1,
    ControlSummaryV1,
    CoverageEntryV1,
    CoverageReportV1,
    FailureCountV1,
    NeutralFindingKeyV1,
    NeutralFindingV1,
    NeutralOracleProjectionV1,
    NeutralSubjectResultV1,
    NeutralValueV1,
    RawRunEvidenceV1,
    RawUsageEvidenceV1,
    RawUsageMetricV1,
    RedactionReceiptV1,
    TerminalReceiptV1,
    build_benchmark_schedule,
    canonical_json_bytes,
    canonical_sha256,
    codex_task_result_schema_sha256,
    deterministic_observation_id,
    fixed_benchmark_case_selections,
    neutral_finding_encoding_sha256,
    sha256_prefixed,
)
from drift_agent.evaluation.catalog import FROZEN_MANIFEST_SHA256
from drift_agent.evaluation.models import MultisetMatch
from drift_agent.evaluation.stage3_catalog import FROZEN_STAGE3_MANIFEST_SHA256
from drift_agent.evaluation.stage3_models import Stage3Accounting, Stage3CaseEvaluation
from drift_agent.evaluation.stage4_models import ComparisonPairKey


def _sha(seed: str) -> str:
    return canonical_sha256({"seed": seed})


def _digest(seed: str) -> str:
    return sha256_prefixed({"seed": seed})


def _task(operation: str = "repair") -> BenchmarkTaskV1:
    return BenchmarkTaskV1.model_validate({"operation": operation})


def _finding(*, explanation: str = "The documented default is stale.") -> NeutralFindingV1:
    return NeutralFindingV1(
        code_path="src/demo/api.py",
        doc_path="docs/api.md",
        symbol_fqn="demo.api.echo",
        finding_family="parameter_default_changed",
        component_kind="parameter",
        component_name="color",
        old_value=NeutralValueV1(kind="python_literal", value=False),
        new_value=NeutralValueV1(kind="python_literal", value=True),
        explanation=explanation,
    )


def _plan(*, trials: int = 1, seed: int = 17) -> BenchmarkPlanV1:
    portable, controls = fixed_benchmark_case_selections()
    trial_ids = ("trial-1",) if trials == 1 else ("trial-1", "trial-2", "trial-3")
    schedule = build_benchmark_schedule(
        portable_cases=portable,
        control_cases=controls,
        trial_ids=trial_ids,
        shuffle_seed=seed,
    )
    return BenchmarkPlanV1(
        dataset_catalogs=(
            BenchmarkDatasetCatalogV1(
                dataset_id="structural-v1", catalog_sha256=_sha("structural catalog")
            ),
            BenchmarkDatasetCatalogV1(
                dataset_id="stage3-v1", catalog_sha256=_sha("stage3 catalog")
            ),
        ),
        portable_cases=portable,
        control_cases=controls,
        trial_ids=trial_ids,
        shuffle_seed=seed,
        schedule=schedule,
        contracts=BenchmarkContractDigestsV1(
            neutral_encoding_sha256=neutral_finding_encoding_sha256(),
            neutral_projection_table_sha256=_sha("projection"),
            codex_output_schema_sha256=_sha("schema"),
            schema_bundle_sha256=_sha("bundle"),
            prompt_renderer_version="prompt-v1",
            prompt_renderer_sha256=_sha("prompt"),
            scorer_version="scorer-v1",
            scorer_contract_sha256=_sha("scorer"),
        ),
        codex=BenchmarkCodexRuntimeV1(
            cli_version="codex-cli 0.144.1",
            binary_sha256=_sha("codex"),
            model_id="gpt-5.2-codex",
            reasoning_effort="medium",
        ),
        drift_agent=BenchmarkDriftRuntimeV1(
            agent_version="0.1.0",
            wheel_sha256=_sha("wheel"),
            runtime_lock_sha256=_sha("lock"),
        ),
        toolchain=BenchmarkToolchainV1(
            container_image_sha256=_sha("image"),
            runtime_toolchain_sha256=_sha("toolchain"),
            python_version="3.11.13",
            python_executable_sha256=_sha("python"),
            git_version="2.50.1",
            git_executable_sha256=_sha("git"),
            pytest_version="9.1.1",
            pytest_executable_sha256=_sha("pytest"),
            distributions_sha256=_sha("distributions"),
            plugin_set_sha256=_sha("plugins"),
            supervisor_namespace_sha256=_sha("supervisor ns"),
            codex_namespace_sha256=_sha("codex ns"),
            drift_namespace_sha256=_sha("drift ns"),
        ),
        limits=BenchmarkLimitsV1(maximum_live_invocations=12 * trials),
        budget_source="explicit live invocation count; no hard token or cost cap",
    )


def _stream(name: str = "stdout", *, replacements: int = 0) -> BoundedStreamReceiptV1:
    return BoundedStreamReceiptV1.model_validate(
        {
            "stream_name": name,
            "total_bytes": 20,
            "captured_bytes": 20,
            "byte_limit": 100,
            "truncated": False,
            "raw_sha256": _sha(f"raw {name}"),
            "redacted_sha256": _sha(f"redacted {name}"),
            "replacement_count": replacements,
        }
    )


def _terminal(
    *,
    classification: str = "completed",
    streams: tuple[BoundedStreamReceiptV1, ...] | None = None,
) -> TerminalReceiptV1:
    timed_out = classification == "runner_timeout"
    return TerminalReceiptV1.model_validate(
        {
            "plan_digest": _sha("plan"),
            "slot_id": "slot-001",
            "run_class": "portable",
            "subject": "codex",
            "dataset_id": "structural-v1",
            "case_id": "click.parameter-default.v1",
            "trial_id": "trial-1",
            "process_started": True,
            "terminal_classification": classification,
            "exit_code": None if timed_out else 0,
            "signal": 15 if timed_out else None,
            "timed_out": timed_out,
            "duration_ms": 30,
            "streams": streams or (_stream("stdout"), _stream("stderr")),
            "available_artifacts": ("raw-evidence.json", "terminal-receipt.json"),
        }
    )


def _measured(value: int) -> RawUsageMetricV1:
    return RawUsageMetricV1(status="measured", value=value, evidence_source="supervisor receipt")


def _unmeasured(reason: str = "telemetry unavailable") -> RawUsageMetricV1:
    return RawUsageMetricV1(status="not_measured", reason=reason)


def _usage() -> RawUsageEvidenceV1:
    return RawUsageEvidenceV1(
        model_calls=_unmeasured(),
        strong_model_calls=_unmeasured(),
        tool_calls=_measured(1),
        input_tokens=_measured(10),
        output_tokens=_measured(5),
        cost_nano_usd=_unmeasured("billing receipt unavailable"),
        duration_ms=_measured(30),
    )


def test_canonical_json_is_compact_sorted_utf8_and_forbids_nan() -> None:
    assert canonical_json_bytes({"z": 1, "a": "中文"}) == ('{"a":"中文","z":1}'.encode())
    assert canonical_sha256({"a": 1, "b": (2, 3)}) == canonical_sha256({"b": [2, 3], "a": 1})
    with pytest.raises(ValueError, match=r"non-finite|Out of range"):
        canonical_json_bytes({"bad": math.nan})
    with pytest.raises(TypeError, match="keys"):
        canonical_json_bytes({1: "bad"})


def test_task_is_fixed_safe_strict_and_digestable() -> None:
    task = _task()
    assert task.digest == sha256_prefixed(task)
    assert task.network is False
    assert task.model_json_schema()["additionalProperties"] is False
    with pytest.raises(ValidationError):
        BenchmarkTaskV1.model_validate({"operation": "repair", "network": True})
    with pytest.raises(ValidationError):
        BenchmarkTaskV1.model_validate({"operation": "check", "extra": "oracle"})


def test_fixed_selection_is_exactly_twelve_portable_and_six_controls() -> None:
    portable, controls = fixed_benchmark_case_selections()
    assert tuple(case.case_id for case in portable) == PORTABLE_CASE_IDS
    assert tuple(case.case_id for case in controls) == CONTROL_CASE_IDS
    assert len(portable) == 12
    assert len(controls) == 6
    assert all(case.operation == "repair" for case in portable[:8])
    assert all(case.operation == "check" for case in portable[8:])
    assert {case.dataset_id for case in controls} == {"stage3-v1"}

    with pytest.raises(ValidationError, match="frozen manifest"):
        BenchmarkCaseSelectionV1(
            dataset_id="stage3-v1",
            case_id=controls[0].case_id,
            case_manifest_sha256="0" * 64,
            operation=controls[0].operation,
        )


def test_schedule_is_deterministic_paired_serial_and_controls_are_last() -> None:
    portable, controls = fixed_benchmark_case_selections()
    first = build_benchmark_schedule(
        portable_cases=portable,
        control_cases=controls,
        trial_ids=("trial-1",),
        shuffle_seed=41,
    )
    second = build_benchmark_schedule(
        portable_cases=portable,
        control_cases=controls,
        trial_ids=("trial-1",),
        shuffle_seed=41,
    )
    assert first == second
    assert len(first) == 30
    assert [slot.ordinal for slot in first] == list(range(1, 31))
    for index in range(0, 24, 2):
        left, right = first[index : index + 2]
        assert (left.dataset_id, left.case_id, left.trial_id) == (
            right.dataset_id,
            right.case_id,
            right.trial_id,
        )
        assert {left.subject, right.subject} == {"codex", "drift_agent"}
    assert all(slot.run_class == "control" for slot in first[-6:])


def test_plan_freezes_every_selection_schedule_limit_and_digest() -> None:
    smoke = _plan()
    full = _plan(trials=3)
    assert smoke.plan_digest == canonical_sha256(smoke)
    assert len(smoke.schedule) == 30
    assert len(full.schedule) == 78
    assert smoke.limits.maximum_live_invocations == 12
    assert full.limits.maximum_live_invocations == 36
    assert smoke.plan_digest != full.plan_digest
    assert smoke.plan_digest == _plan().plan_digest

    document = smoke.model_dump(mode="python")
    document["schedule"] = tuple(reversed(smoke.schedule))
    with pytest.raises(ValidationError, match="deterministic schedule"):
        BenchmarkPlanV1.model_validate(document)

    document = smoke.model_dump(mode="python")
    document["limits"] = smoke.limits.model_copy(update={"maximum_live_invocations": 13})
    with pytest.raises(ValidationError, match="maximum live"):
        BenchmarkPlanV1.model_validate(document)


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        ("missing", None),
        ("present", None),
        ("python_literal", None),
        ("python_literal", True),
        ("python_literal", -(2**63)),
        ("python_literal", "blue"),
        ("python_annotation", "str | None"),
        ("symbol_fqn", "demo.api.echo"),
        ("validation_status", "failed"),
        ("text", "bounded neutral text"),
    ],
)
def test_neutral_value_tags_accept_only_public_bounded_values(kind: str, value: object) -> None:
    assert NeutralValueV1.model_validate({"kind": kind, "value": value}).kind == kind


def test_neutral_values_reject_private_or_ambiguous_encodings() -> None:
    invalid = (
        {"kind": "missing", "value": ""},
        {"kind": "python_literal", "value": 2**63},
        {"kind": "python_annotation", "value": "Name(id='str', ctx=Load())"},
        {"kind": "symbol_fqn", "value": "demo:echo"},
        {"kind": "validation_status", "value": "success"},
        {"kind": "text", "value": " line"},
    )
    for document in invalid:
        with pytest.raises(ValidationError):
            NeutralValueV1.model_validate(document)


@pytest.mark.parametrize("path", ["/abs", "../x", "a\\b", "./a", "a//b", "a/"])
def test_neutral_finding_paths_must_be_canonical(path: str) -> None:
    document = _finding().model_dump(mode="python")
    document["code_path"] = path
    with pytest.raises(ValidationError, match="canonical"):
        NeutralFindingV1.model_validate(document)


def test_finding_key_excludes_explanation_and_duplicate_keys_fail_closed() -> None:
    first = _finding(explanation="first explanation")
    second = _finding(explanation="second explanation")
    assert first.key == second.key
    with pytest.raises(ValidationError, match="unique"):
        CodexTaskResultV1(
            schema_version=1,
            declared_status="fixed",
            findings=(first, second),
            validation_claims=(),
        )


def test_family_component_and_value_encoding_are_cross_checked() -> None:
    with pytest.raises(ValidationError, match="component kind"):
        NeutralFindingKeyV1(
            code_path="src/demo/api.py",
            doc_path="docs/api.md",
            symbol_fqn="demo.api.echo",
            finding_family="symbol_renamed",
            component_kind="parameter",
            component_name="name",
            old_value=NeutralValueV1(kind="symbol_fqn", value="demo.old.echo"),
            new_value=NeutralValueV1(kind="symbol_fqn", value="demo.api.echo"),
        )

    executable = NeutralFindingKeyV1(
        code_path="drift-agent.toml",
        doc_path="docs/api.md",
        symbol_fqn=None,
        finding_family="broken_example",
        component_kind="doctest",
        component_name=None,
        old_value=NeutralValueV1(kind="validation_status", value="passed"),
        new_value=NeutralValueV1(kind="validation_status", value="failed"),
    )
    assert executable.finding_family == "broken_example"


def test_codex_result_status_is_checked_against_subject_neutral_task() -> None:
    result = CodexTaskResultV1(
        schema_version=1,
        declared_status="fixed",
        findings=(_finding(),),
        validation_claims=(),
    )
    result.validate_for_task(_task("repair"))
    with pytest.raises(ValueError, match="invalid for check"):
        result.validate_for_task(_task("check"))
    with pytest.raises(ValidationError, match="clean"):
        CodexTaskResultV1(
            schema_version=1,
            declared_status="clean",
            findings=(_finding(),),
            validation_claims=(),
        )
    schema = CodexTaskResultV1.model_json_schema()
    assert set(schema["required"]) == {
        "schema_version",
        "declared_status",
        "findings",
        "validation_claims",
    }
    assert len(codex_task_result_schema_sha256()) == 64


def test_neutral_subject_result_rejects_duplicate_keys_and_check_abstention() -> None:
    key = _finding().key
    with pytest.raises(ValidationError, match="unique"):
        NeutralSubjectResultV1(
            operation="repair",
            status="unresolved",
            findings=(key, key),
            derived_abstention=True,
        )
    with pytest.raises(ValidationError, match="never"):
        NeutralSubjectResultV1(
            operation="check",
            status="unresolved",
            derived_abstention=True,
        )


def test_oracle_binds_public_encoding_and_frozen_manifest() -> None:
    oracle = NeutralOracleProjectionV1(
        encoding_sha256=neutral_finding_encoding_sha256(),
        dataset_id="structural-v1",
        case_id="click.parameter-default.v1",
        case_manifest_sha256=FROZEN_MANIFEST_SHA256["click.parameter-default.v1"],
        operation="repair",
        expected_status="fixed",
        findings=(_finding().key,),
    )
    assert oracle.projection_version == 1
    with pytest.raises(ValidationError, match="encoding"):
        NeutralOracleProjectionV1.model_validate(
            oracle.model_dump(mode="python") | {"encoding_sha256": "0" * 64}
        )


def test_observation_id_is_plan_subject_pair_and_evidence_bound() -> None:
    pair = ComparisonPairKey(
        dataset_id="structural-v1",
        case_id="click.parameter-default.v1",
        case_manifest_sha256=FROZEN_MANIFEST_SHA256["click.parameter-default.v1"],
        trial_id="trial-1",
        snapshot_digest=_digest("snapshot"),
        task_digest=_digest("task"),
        scope_digest=_digest("scope"),
    )
    first = deterministic_observation_id(
        plan_digest=_sha("plan"),
        subject="codex",
        pair_key=pair,
        evidence_sha256=_sha("evidence"),
    )
    assert first.startswith("obs_v1_codex_")
    assert len(first.rsplit("_", 1)[1]) == 32
    assert first == deterministic_observation_id(
        plan_digest=_sha("plan"),
        subject="codex",
        pair_key=pair,
        evidence_sha256=_sha("evidence"),
    )
    assert first != deterministic_observation_id(
        plan_digest=_sha("plan"),
        subject="drift_agent",
        pair_key=pair,
        evidence_sha256=_sha("evidence"),
    )


def test_terminal_receipt_supports_tool_profile_violation_and_exact_timeout_flag() -> None:
    violation = _terminal(classification="tool_profile_violation")
    assert violation.terminal_classification == "tool_profile_violation"
    timeout = _terminal(classification="runner_timeout")
    assert timeout.timed_out is True
    with pytest.raises(ValidationError, match="timeout"):
        TerminalReceiptV1.model_validate(timeout.model_dump(mode="python") | {"timed_out": False})


def test_raw_evidence_binds_identity_streams_redaction_usage_and_digest() -> None:
    streams = (_stream("stdout", replacements=1), _stream("stderr"))
    terminal = _terminal(streams=streams)
    evidence = RawRunEvidenceV1(
        plan_digest=_sha("plan"),
        authorization_ledger_sha256=_sha("authorization"),
        subject="codex",
        dataset_id="structural-v1",
        case_id="click.parameter-default.v1",
        trial_id="trial-1",
        case_manifest_sha256=FROZEN_MANIFEST_SHA256["click.parameter-default.v1"],
        snapshot_digest=_digest("snapshot"),
        task_digest=_digest("task"),
        scope_digest=_digest("scope"),
        tool_profile_digest=_digest("tool profile"),
        runner_version="codex-cli 0.144.1",
        runner_binary_sha256=_sha("codex binary"),
        model_id="gpt-5.2-codex",
        effective_request_sha256=_sha("request"),
        rendered_input_sha256=_sha("prompt"),
        terminal=terminal,
        pre_snapshot_digest=_digest("pre"),
        post_snapshot_digest=_digest("post"),
        pre_git_metadata_sha256=_sha("pre git"),
        post_git_metadata_sha256=_sha("post git"),
        streams=streams,
        redaction=RedactionReceiptV1(
            policy_version="redaction-v1", replacement_count=1, secret_detected=False
        ),
        final_result_sha256=_sha("final"),
        usage=_usage(),
    )
    assert evidence.evidence_sha256 == canonical_sha256(evidence)
    assert (
        evidence.evidence_sha256
        == RawRunEvidenceV1.model_validate_json(canonical_json_bytes(evidence)).evidence_sha256
    )

    with pytest.raises(ValidationError, match="stream"):
        RawRunEvidenceV1.model_validate(
            evidence.model_dump(mode="python") | {"streams": tuple(reversed(streams))}
        )


def test_coverage_completeness_is_derived_from_all_planned_slots() -> None:
    plan = _plan()
    entries = tuple(
        CoverageEntryV1(
            slot_id=slot.slot_id,
            run_class=slot.run_class,
            subject=slot.subject,
            dataset_id=slot.dataset_id,
            case_id=slot.case_id,
            trial_id=slot.trial_id,
            terminal_classification="completed",
            terminal_receipt_sha256=_sha(f"terminal {slot.slot_id}"),
            observation_sha256=(
                _sha(f"observation {slot.slot_id}") if slot.run_class == "portable" else None
            ),
            control_result_sha256=(
                _sha(f"control {slot.slot_id}") if slot.run_class == "control" else None
            ),
        )
        for slot in plan.schedule
    )
    coverage = CoverageReportV1(
        plan_digest=plan.plan_digest,
        paired_trial_slots=12,
        planned_subject_slots=30,
        entries=entries,
        execution_accounted=True,
        portable_score_complete=True,
        controls_complete=True,
        benchmark_complete=True,
    )
    assert coverage.benchmark_complete is True

    failed_entry = CoverageEntryV1(
        slot_id="slot-001",
        run_class="portable",
        subject="codex",
        dataset_id="structural-v1",
        case_id="click.parameter-default.v1",
        trial_id="trial-1",
        terminal_classification="tool_profile_violation",
        terminal_receipt_sha256=_sha("failed terminal"),
    )
    partial = CoverageReportV1(
        plan_digest=plan.plan_digest,
        paired_trial_slots=12,
        planned_subject_slots=30,
        entries=(failed_entry,),
        failure_counts=(FailureCountV1(failure_class="tool_profile_violation", count=1),),
        execution_accounted=False,
        portable_score_complete=False,
        controls_complete=False,
        benchmark_complete=False,
    )
    assert partial.execution_accounted is False


def _control_evaluation(case_id: str, *, passed: bool = True) -> Stage3CaseEvaluation:
    semantic = case_id.startswith("semantic.")
    accounting = Stage3Accounting(
        repair_outcome="success" if semantic else "not_applicable",
        patch_attempts=1 if semantic else 0,
        model_calls_by_profile={"fast": 1} if semantic else {},
    )
    return Stage3CaseEvaluation(
        case_id=case_id,
        case_kind="semantic" if semantic else "executable",
        passed=passed,
        matching=MultisetMatch(tp=0, fp=0, fn=0),
        status_matches=passed,
        outcomes_match=passed,
        changed_bytes_match=passed,
        no_extra_mutation=passed,
        accounting_matches=passed,
        executable_zero_model_compliance=True,
        offline_compliance=True,
        model_script_compliance=True,
        expected_status="fixed" if semantic else "unresolved",
        actual_status="fixed" if semantic else "unresolved",
        expected_accounting=accounting,
        actual_accounting=accounting,
    )


def test_control_report_is_six_sorted_one_shot_results_and_derived_summary() -> None:
    plan_digest = _sha("plan")
    results = tuple(
        ControlResultV1(
            plan_digest=plan_digest,
            case_id=case_id,
            case_manifest_sha256=FROZEN_STAGE3_MANIFEST_SHA256[case_id],
            runner_contract_sha256=_sha("control runner"),
            evidence_sha256=_sha(f"evidence {case_id}"),
            evaluation=_control_evaluation(case_id),
        )
        for case_id in sorted(CONTROL_CASE_IDS)
    )
    report = ControlReportV1(
        plan_digest=plan_digest,
        results=results,
        summary=ControlSummaryV1(passed=6, failed=0, control_all_passed=True),
    )
    assert report.summary.controls_complete is True
    assert report.summary.control_all_passed is True
    with pytest.raises(ValidationError, match="sorted"):
        ControlReportV1(
            plan_digest=plan_digest,
            results=tuple(reversed(results)),
            summary=report.summary,
        )


def test_benchmark_report_requires_qualified_labels_and_all_missing_metrics() -> None:
    report = BenchmarkReportV1(
        plan_digest=_sha("plan"),
        paired_trial_slots=12,
        coverage=BenchmarkCoverageSummaryV1(
            execution_accounted=True,
            portable_score_complete=True,
            controls_complete=True,
            benchmark_complete=True,
        ),
        control_summary=ControlSummaryV1(passed=6, failed=0, control_all_passed=True),
        missing_metrics=V1_MISSING_METRICS,
        aggregate_labels=BenchmarkAggregateLabelsV1(),
        artifacts=BenchmarkArtifactDigestsV1(
            coverage_report_sha256=_sha("coverage"),
            comparison_report_sha256=_sha("comparison"),
            control_report_sha256=_sha("controls"),
            adjudication_sidecar_sha256=_sha("adjudication"),
        ),
    )
    assert report.aggregate_labels.structural == "frozen-policy-conformance-only"
    assert json.loads(canonical_json_bytes(report))["missing_metrics"] == list(V1_MISSING_METRICS)
    with pytest.raises(ValidationError, match="missing metric"):
        BenchmarkReportV1.model_validate(
            report.model_dump(mode="python") | {"missing_metrics": V1_MISSING_METRICS[:-1]}
        )

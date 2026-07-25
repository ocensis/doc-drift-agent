from __future__ import annotations

import pytest
from pydantic import ValidationError

from drift_agent.evaluation import (
    CaseManifest,
    CaseObservation,
    MatchingKey,
    ObservedFinding,
    SymbolIdentity,
    build_report,
    canonical_matching_key,
    evaluate_case,
    group_failures_by_case,
    match_findings,
    summarize_evaluations,
)
from drift_agent.evaluation.models import (
    ChangedBytes,
    Disposition,
    ExpectedResult,
    Operation,
    Provenance,
    WorkspaceInput,
)

_A_HASH = "a" * 64
_B_HASH = "b" * 64
_C_HASH = "c" * 64


def _key(name: str, *, component: str = "value") -> MatchingKey:
    return MatchingKey(
        symbol_identity=SymbolIdentity(
            module="evaluation_case.api",
            name=name,
            category="module_function",
        ),
        kind="parameter_default_changed",
        component=component,
        old_value=False,
        new_value=True,
        code_path="src/evaluation_case/api.py",
        doc_path="docs/api.md",
        detector_id="structural.signature",
        detector_version="2",
    )


def _manifest(
    case_id: str,
    keys: tuple[MatchingKey, ...],
    dispositions: tuple[Disposition, ...],
    reason_codes: tuple[str, ...],
    *,
    status: str = "drift_found",
    operation: Operation = "check",
    coverage_tags: tuple[str, ...] = ("parameter",),
    changed_bytes: tuple[ChangedBytes, ...] = (),
) -> CaseManifest:
    return CaseManifest.model_validate(
        {
            "schema_version": 1,
            "dataset_id": "structural-v1",
            "case_id": case_id,
            "project_family": "click",
            "provenance": Provenance(
                kind="project_authored",
                repository="project://doc-code-drift-agent",
                code_revision="structural-v1",
                doc_revision="structural-v1",
                source_urls=(),
                license_spdx="LicenseRef-Project-Authored",
                copied_bytes=0,
            ),
            "files": (),
            "workspace": WorkspaceInput(),
            "operation": operation,
            "coverage_tags": coverage_tags,
            "expected": ExpectedResult(
                status=status,
                finding_multiset=keys,
                dispositions=dispositions,
                reason_codes=reason_codes,
                changed_bytes=changed_bytes,
            ),
            "model_calls": 0,
            "offline": True,
        }
    )


def _observation(
    status: str,
    findings: tuple[tuple[MatchingKey, Disposition, str], ...],
    *,
    changed_bytes: tuple[ChangedBytes, ...] = (),
    model_calls: int = 0,
    network_calls: int = 0,
    offline: bool = True,
) -> CaseObservation:
    return CaseObservation(
        status=status,
        findings=tuple(
            ObservedFinding(key=key, disposition=disposition, reason_code=reason_code)
            for key, disposition, reason_code in findings
        ),
        changed_bytes=changed_bytes,
        model_calls=model_calls,
        network_calls=network_calls,
        offline=offline,
    )


def _exact_observation(manifest: CaseManifest) -> CaseObservation:
    findings = tuple(
        (key, disposition, reason_code)
        for key, disposition, reason_code in zip(
            manifest.expected.finding_multiset,
            manifest.expected.dispositions,
            manifest.expected.reason_codes,
            strict=True,
        )
    )
    return _observation(
        manifest.expected.status,
        findings,
        changed_bytes=manifest.expected.changed_bytes,
    )


def test_multiset_matching_preserves_duplicate_counts_and_conservation() -> None:
    alpha = _key("alpha")
    beta = _key("beta")
    gamma = _key("gamma")
    expected = (alpha, alpha, beta)
    actual = (alpha, beta, beta, gamma)

    matching = match_findings(expected, actual)

    assert (matching.tp, matching.fp, matching.fn) == (2, 2, 1)
    assert matching.tp + matching.fn == len(expected)
    assert matching.tp + matching.fp == len(actual)
    assert {(canonical_matching_key(entry.key), entry.count) for entry in matching.missing} == {
        (canonical_matching_key(alpha), 1)
    }
    assert {(canonical_matching_key(entry.key), entry.count) for entry in matching.unexpected} == {
        (canonical_matching_key(beta), 1),
        (canonical_matching_key(gamma), 1),
    }


def test_case_pass_and_report_diagnostics_locate_one_failing_case() -> None:
    expected_key = _key("expected")
    actual_key = _key("actual")
    passing_manifest = _manifest(
        "passing.v1",
        (expected_key,),
        ("detected",),
        ("detected",),
    )
    failing_manifest = _manifest(
        "failing.v1",
        (expected_key,),
        ("detected",),
        ("detected",),
    )
    passing_observation = _exact_observation(passing_manifest)
    failing_observation = _observation(
        "drift_found",
        ((actual_key, "detected", "detected"),),
    )
    evaluations = (
        evaluate_case(passing_manifest, passing_observation),
        evaluate_case(failing_manifest, failing_observation),
    )

    report = build_report(evaluations, (passing_observation, failing_observation))
    diagnostics = group_failures_by_case(report)

    assert evaluations[0].passed is True
    assert evaluations[1].passed is False
    assert (report.summary.total, report.summary.passed, report.summary.failed) == (2, 1, 1)
    assert report.summary.passed + report.summary.failed == report.summary.total
    assert set(diagnostics) == {"failing.v1"}
    messages = "\n".join(diagnostics["failing.v1"])
    assert f"missing x1: {canonical_matching_key(expected_key)}" in messages
    assert f"unexpected x1: {canonical_matching_key(actual_key)}" in messages
    assert "disposition/reason oracle mismatch" in messages


def test_case_diagnostics_expose_status_outcome_mutation_and_compliance_failures() -> None:
    key = _key("repair_target")
    expected_change = ChangedBytes(
        path="docs/api.md",
        before_sha256=_A_HASH,
        before_size_bytes=1,
        before_mode="0644",
        after_sha256=_B_HASH,
        after_size_bytes=2,
        after_mode="0644",
    )
    actual_change = ChangedBytes(
        path="docs/unexpected.md",
        before_sha256=_A_HASH,
        before_size_bytes=1,
        before_mode="0644",
        after_sha256=_C_HASH,
        after_size_bytes=3,
        after_mode="0644",
    )
    manifest = _manifest(
        "all-oracle-failures.v1",
        (key,),
        ("fixed",),
        ("validated",),
        status="fixed",
        operation="repair",
        changed_bytes=(expected_change,),
    )
    observation = _observation(
        "partial",
        ((key, "unresolved", "validation_failed"),),
        changed_bytes=(actual_change,),
        model_calls=1,
        network_calls=1,
        offline=False,
    )

    evaluation = evaluate_case(manifest, observation)
    diagnostics = group_failures_by_case(build_report((evaluation,), (observation,)))

    assert evaluation.passed is False
    assert evaluation.matching.fp == evaluation.matching.fn == 0
    assert evaluation.status_matches is False
    assert evaluation.outcomes_match is False
    assert evaluation.changed_bytes_match is False
    assert evaluation.no_extra_mutation is False
    assert evaluation.zero_model_compliance is False
    assert evaluation.offline_compliance is False
    assert diagnostics[manifest.case_id] == (
        "status expected=fixed actual=partial",
        "disposition/reason oracle mismatch",
        "changed-byte oracle mismatch",
        "model_calls was not zero",
        "offline compliance failed",
    )


def test_summary_is_the_exact_fold_of_case_metrics_and_compliance() -> None:
    alpha = _key("alpha")
    beta = _key("beta")
    plain_manifest = _manifest(
        "plain-pass.v1",
        (alpha,),
        ("detected",),
        ("detected",),
    )
    failed_manifest = _manifest(
        "detection-failure.v1",
        (alpha, alpha),
        ("detected", "detected"),
        ("detected", "detected"),
    )
    repair_manifest = _manifest(
        "repair-success.v1",
        (alpha,),
        ("fixed",),
        ("validated",),
        status="fixed",
        operation="repair",
    )
    rejection_manifest = _manifest(
        "conservative-rejection.v1",
        (beta,),
        ("unresolved",),
        ("unsupported.symbol_kind",),
        status="unresolved",
        operation="repair",
        coverage_tags=("delete", "conservative_rejection"),
    )
    plain_observation = _exact_observation(plain_manifest)
    failed_observation = _observation(
        "drift_found",
        (
            (alpha, "detected", "detected"),
            (beta, "detected", "detected"),
        ),
    )
    repair_observation = _exact_observation(repair_manifest)
    rejection_observation = _exact_observation(rejection_manifest)
    evaluations = (
        evaluate_case(plain_manifest, plain_observation),
        evaluate_case(failed_manifest, failed_observation),
        evaluate_case(repair_manifest, repair_observation),
        evaluate_case(rejection_manifest, rejection_observation),
    )

    summary = summarize_evaluations(evaluations)

    assert [evaluation.passed for evaluation in evaluations] == [True, False, True, True]
    assert evaluations[2].repair_success is True
    assert evaluations[3].conservative_rejection is True
    assert (summary.total, summary.passed, summary.failed) == (4, 3, 1)
    assert (summary.tp, summary.fp, summary.fn) == (4, 1, 1)
    assert summary.tp == sum(evaluation.matching.tp for evaluation in evaluations)
    assert summary.fp == sum(evaluation.matching.fp for evaluation in evaluations)
    assert summary.fn == sum(evaluation.matching.fn for evaluation in evaluations)
    assert summary.repair_successes == 1
    assert summary.conservative_rejections == 1
    assert summary.zero_model_compliance is True
    assert summary.offline_compliance is True

    noncompliant = summarize_evaluations(evaluations, model_calls=1, network_calls=1)
    assert noncompliant.model_calls == 1
    assert noncompliant.network_calls == 1
    assert noncompliant.zero_model_compliance is False
    assert noncompliant.offline_compliance is False


@pytest.mark.parametrize(
    ("payload", "message"),
    (
        (
            {
                "path": "docs/api.md",
                "before_sha256": _A_HASH,
                "before_size_bytes": None,
                "before_mode": "0644",
                "after_sha256": _B_HASH,
                "after_size_bytes": 2,
                "after_mode": "0644",
            },
            "before hash, size, and mode must be present together",
        ),
        (
            {
                "path": "docs/api.md",
                "before_sha256": _A_HASH,
                "before_size_bytes": 1,
                "before_mode": "0644",
                "after_sha256": _A_HASH,
                "after_size_bytes": 1,
                "after_mode": "0644",
            },
            "changed-byte entries must describe an actual change",
        ),
        (
            {
                "path": "../api.md",
                "before_sha256": _A_HASH,
                "before_size_bytes": 1,
                "before_mode": "0644",
                "after_sha256": _B_HASH,
                "after_size_bytes": 2,
                "after_mode": "0644",
            },
            "changed-byte paths must stay inside the repository",
        ),
    ),
)
def test_changed_bytes_rejects_incomplete_equal_or_unsafe_boundaries(
    payload: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        ChangedBytes.model_validate(payload)


def test_expected_result_requires_parallel_finding_oracles() -> None:
    with pytest.raises(ValidationError, match="must have equal lengths"):
        ExpectedResult(
            status="drift_found",
            finding_multiset=(_key("expected"),),
            dispositions=(),
            reason_codes=("detected",),
            changed_bytes=(),
        )

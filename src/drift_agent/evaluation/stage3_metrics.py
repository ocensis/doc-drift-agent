from __future__ import annotations

import json
from collections.abc import Sequence

from drift_agent.evaluation.metrics import canonical_matching_key, match_findings
from drift_agent.evaluation.models import ChangedBytes, MatchingKey
from drift_agent.evaluation.stage3_models import (
    ExactRatio,
    Stage3CaseEvaluation,
    Stage3CaseManifest,
    Stage3CaseObservation,
    Stage3EvaluationReport,
    Stage3EvaluationSummary,
)


def _outcome_projection(
    keys: Sequence[MatchingKey],
    dispositions: Sequence[str],
    reason_codes: Sequence[str],
) -> tuple[str, ...]:
    return tuple(
        sorted(
            json.dumps(
                {
                    "key": json.loads(canonical_matching_key(key)),
                    "disposition": disposition,
                    "reason_code": reason_code,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            for key, disposition, reason_code in zip(
                keys,
                dispositions,
                reason_codes,
                strict=True,
            )
        )
    )


def _changed_projection(changes: Sequence[ChangedBytes]) -> tuple[str, ...]:
    return tuple(
        sorted(
            json.dumps(
                change.model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            for change in changes
        )
    )


def evaluate_stage3_case(
    manifest: Stage3CaseManifest,
    observation: Stage3CaseObservation,
) -> Stage3CaseEvaluation:
    """Score one Stage 3 replay against bytes, outcomes, routing, and cost."""

    actual_keys = tuple(finding.key for finding in observation.findings)
    matching = match_findings(manifest.expected.finding_multiset, actual_keys)
    expected_outcomes = _outcome_projection(
        manifest.expected.finding_multiset,
        manifest.expected.dispositions,
        manifest.expected.reason_codes,
    )
    actual_outcomes = _outcome_projection(
        actual_keys,
        tuple(finding.disposition for finding in observation.findings),
        tuple(finding.reason_code for finding in observation.findings),
    )
    expected_changes = _changed_projection(manifest.expected.changed_bytes)
    actual_changes = _changed_projection(observation.changed_bytes)
    exact_findings = matching.fp == 0 and matching.fn == 0
    status_matches = observation.status == manifest.expected.status
    outcomes_match = actual_outcomes == expected_outcomes
    changed_bytes_match = actual_changes == expected_changes
    no_extra_mutation = {change.path for change in observation.changed_bytes} == {
        change.path for change in manifest.expected.changed_bytes
    }
    accounting_matches = observation.accounting == manifest.expected.accounting
    executable_zero_model = (
        manifest.case_kind != "executable" or observation.accounting.model_calls == 0
    )
    offline = manifest.offline and observation.offline and observation.network_calls == 0
    script_compliance = observation.model_script_consumed
    passed = all(
        (
            exact_findings,
            status_matches,
            outcomes_match,
            changed_bytes_match,
            no_extra_mutation,
            accounting_matches,
            executable_zero_model,
            offline,
            script_compliance,
        )
    )
    return Stage3CaseEvaluation(
        case_id=manifest.case_id,
        case_kind=manifest.case_kind,
        passed=passed,
        matching=matching,
        status_matches=status_matches,
        outcomes_match=outcomes_match,
        changed_bytes_match=changed_bytes_match,
        no_extra_mutation=no_extra_mutation,
        accounting_matches=accounting_matches,
        executable_zero_model_compliance=executable_zero_model,
        offline_compliance=offline,
        model_script_compliance=script_compliance,
        expected_status=manifest.expected.status,
        actual_status=observation.status,
        expected_accounting=manifest.expected.accounting,
        actual_accounting=observation.accounting,
    )


def summarize_stage3_evaluations(
    evaluations: Sequence[Stage3CaseEvaluation],
) -> Stage3EvaluationSummary:
    """Fold exact SC3-019 metrics without floating-point ratios or cost."""

    total = len(evaluations)
    passed = sum(case.passed for case in evaluations)
    opportunities = [
        case for case in evaluations if case.expected_accounting.repair_outcome != "not_applicable"
    ]
    abstention_cases = [
        case for case in opportunities if case.expected_accounting.repair_outcome == "abstained"
    ]
    success_at_1 = sum(
        case.passed
        and case.actual_accounting.repair_outcome == "success"
        and case.actual_accounting.patch_attempts <= 1
        for case in opportunities
    )
    success_at_2 = sum(
        case.passed and case.actual_accounting.repair_outcome == "success" for case in opportunities
    )
    correct_abstentions = sum(
        case.passed and case.actual_accounting.repair_outcome == "abstained"
        for case in abstention_cases
    )
    fast_calls = sum(
        case.actual_accounting.model_calls_by_profile.get("fast", 0) for case in evaluations
    )
    strong_calls = sum(
        case.actual_accounting.model_calls_by_profile.get("strong", 0) for case in evaluations
    )
    model_calls = fast_calls + strong_calls
    return Stage3EvaluationSummary(
        total=total,
        passed=passed,
        failed=total - passed,
        semantic_repair_opportunities=len(opportunities),
        repair_success_at_1=ExactRatio(
            numerator=success_at_1,
            denominator=len(opportunities),
        ),
        repair_success_at_2=ExactRatio(
            numerator=success_at_2,
            denominator=len(opportunities),
        ),
        abstention_correctness=ExactRatio(
            numerator=correct_abstentions,
            denominator=len(abstention_cases),
        ),
        fast_route_ratio=ExactRatio(numerator=fast_calls, denominator=model_calls),
        strong_route_ratio=ExactRatio(numerator=strong_calls, denominator=model_calls),
        model_calls=model_calls,
        validation_commands=sum(case.actual_accounting.validation_commands for case in evaluations),
        input_tokens=sum(case.actual_accounting.input_tokens for case in evaluations),
        output_tokens=sum(case.actual_accounting.output_tokens for case in evaluations),
        known_cost_nano_usd=sum(case.actual_accounting.known_cost_nano_usd for case in evaluations),
        executable_zero_model_compliance=all(
            case.executable_zero_model_compliance for case in evaluations
        ),
        offline_compliance=all(case.offline_compliance for case in evaluations),
        model_script_compliance=all(case.model_script_compliance for case in evaluations),
    )


def build_stage3_report(
    evaluations: Sequence[Stage3CaseEvaluation],
) -> Stage3EvaluationReport:
    return Stage3EvaluationReport(
        dataset_id="stage3-v1",
        cases=tuple(evaluations),
        summary=summarize_stage3_evaluations(evaluations),
    )


def deterministic_stage3_projection(
    value: Stage3CaseEvaluation | Stage3EvaluationReport,
) -> bytes:
    return json.dumps(
        value.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


__all__ = [
    "build_stage3_report",
    "deterministic_stage3_projection",
    "evaluate_stage3_case",
    "summarize_stage3_evaluations",
]

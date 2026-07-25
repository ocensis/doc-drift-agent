from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Sequence

from drift_agent.evaluation.models import (
    CaseEvaluation,
    CaseManifest,
    CaseObservation,
    ChangedBytes,
    EvaluationReport,
    EvaluationSummary,
    KeyCount,
    MatchingKey,
    MultisetMatch,
)


def canonical_matching_key(key: MatchingKey) -> str:
    """Return the frozen canonical JSON serialization for a matching key."""

    return json.dumps(
        key.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _key_index(keys: Sequence[MatchingKey]) -> dict[str, MatchingKey]:
    return {canonical_matching_key(key): key for key in keys}


def match_findings(
    expected: Sequence[MatchingKey],
    actual: Sequence[MatchingKey],
) -> MultisetMatch:
    """Compare normalized findings as multisets, preserving duplicate counts."""

    expected_counter = Counter(canonical_matching_key(key) for key in expected)
    actual_counter = Counter(canonical_matching_key(key) for key in actual)
    index = _key_index([*expected, *actual])
    shared = expected_counter & actual_counter
    unexpected_counter = actual_counter - expected_counter
    missing_counter = expected_counter - actual_counter
    unexpected = tuple(
        KeyCount(key=index[serialized], count=count)
        for serialized, count in sorted(unexpected_counter.items())
    )
    missing = tuple(
        KeyCount(key=index[serialized], count=count)
        for serialized, count in sorted(missing_counter.items())
    )
    true_positives = sum(shared.values())
    return MultisetMatch(
        tp=true_positives,
        fp=sum(actual_counter.values()) - true_positives,
        fn=sum(expected_counter.values()) - true_positives,
        unexpected=unexpected,
        missing=missing,
    )


def _outcome_projection(
    keys: Sequence[MatchingKey],
    dispositions: Sequence[str],
    reason_codes: Sequence[str],
) -> tuple[str, ...]:
    projected = [
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
    ]
    return tuple(sorted(projected))


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


def evaluate_case(manifest: CaseManifest, observation: CaseObservation) -> CaseEvaluation:
    """Evaluate one replay against its complete normalized oracle."""

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
    expected_changed = _changed_projection(manifest.expected.changed_bytes)
    actual_changed = _changed_projection(observation.changed_bytes)
    status_matches = observation.status == manifest.expected.status
    outcomes_match = expected_outcomes == actual_outcomes
    changed_bytes_match = expected_changed == actual_changed
    no_extra_mutation = {change.path for change in observation.changed_bytes} == {
        change.path for change in manifest.expected.changed_bytes
    }
    zero_model = observation.model_calls == manifest.model_calls == 0
    offline = manifest.offline and observation.offline and observation.network_calls == 0
    exact_findings = matching.fp == 0 and matching.fn == 0
    passed = all(
        (
            exact_findings,
            status_matches,
            outcomes_match,
            changed_bytes_match,
            no_extra_mutation,
            zero_model,
            offline,
        )
    )
    expected_fixed = any(disposition == "fixed" for disposition in manifest.expected.dispositions)
    repair_success = manifest.operation == "repair" and expected_fixed and passed
    expected_non_fixed = all(
        disposition != "fixed" for disposition in manifest.expected.dispositions
    )
    conservative_rejection = (
        "conservative_rejection" in manifest.coverage_tags
        and expected_non_fixed
        and not manifest.expected.changed_bytes
        and passed
    )
    return CaseEvaluation(
        case_id=manifest.case_id,
        passed=passed,
        matching=matching,
        status_matches=status_matches,
        outcomes_match=outcomes_match,
        changed_bytes_match=changed_bytes_match,
        no_extra_mutation=no_extra_mutation,
        zero_model_compliance=zero_model,
        offline_compliance=offline,
        repair_success=repair_success,
        conservative_rejection=conservative_rejection,
        expected_status=manifest.expected.status,
        actual_status=observation.status,
        expected_outcomes=expected_outcomes,
        actual_outcomes=actual_outcomes,
        expected_changed_bytes=manifest.expected.changed_bytes,
        actual_changed_bytes=observation.changed_bytes,
    )


def summarize_evaluations(
    evaluations: Sequence[CaseEvaluation],
    *,
    model_calls: int = 0,
    network_calls: int = 0,
) -> EvaluationSummary:
    """Fold per-case outcomes using the frozen conservation equations."""

    total = len(evaluations)
    passed = sum(evaluation.passed for evaluation in evaluations)
    return EvaluationSummary(
        total=total,
        passed=passed,
        failed=total - passed,
        tp=sum(evaluation.matching.tp for evaluation in evaluations),
        fp=sum(evaluation.matching.fp for evaluation in evaluations),
        fn=sum(evaluation.matching.fn for evaluation in evaluations),
        repair_successes=sum(evaluation.repair_success for evaluation in evaluations),
        conservative_rejections=sum(
            evaluation.conservative_rejection for evaluation in evaluations
        ),
        model_calls=model_calls,
        network_calls=network_calls,
        zero_model_compliance=model_calls == 0
        and all(evaluation.zero_model_compliance for evaluation in evaluations),
        offline_compliance=network_calls == 0
        and all(evaluation.offline_compliance for evaluation in evaluations),
    )


def build_report(
    evaluations: Sequence[CaseEvaluation],
    observations: Sequence[CaseObservation],
) -> EvaluationReport:
    """Build a deterministic structural-v1 report."""

    if len(evaluations) != len(observations):
        raise ValueError("evaluations and observations must have equal lengths")
    summary = summarize_evaluations(
        evaluations,
        model_calls=sum(observation.model_calls for observation in observations),
        network_calls=sum(observation.network_calls for observation in observations),
    )
    return EvaluationReport(
        dataset_id="structural-v1",
        cases=tuple(evaluations),
        summary=summary,
    )


def group_failures_by_case(report: EvaluationReport) -> dict[str, tuple[str, ...]]:
    """Return compact deterministic diagnostics for failed cases."""

    diagnostics: defaultdict[str, list[str]] = defaultdict(list)
    for case in report.cases:
        if case.passed:
            continue
        if not case.status_matches:
            diagnostics[case.case_id].append(
                f"status expected={case.expected_status} actual={case.actual_status}"
            )
        for missing in case.matching.missing:
            diagnostics[case.case_id].append(
                f"missing x{missing.count}: {canonical_matching_key(missing.key)}"
            )
        for unexpected in case.matching.unexpected:
            diagnostics[case.case_id].append(
                f"unexpected x{unexpected.count}: {canonical_matching_key(unexpected.key)}"
            )
        if not case.outcomes_match:
            diagnostics[case.case_id].append("disposition/reason oracle mismatch")
        if not case.changed_bytes_match:
            diagnostics[case.case_id].append("changed-byte oracle mismatch")
        if not case.zero_model_compliance:
            diagnostics[case.case_id].append("model_calls was not zero")
        if not case.offline_compliance:
            diagnostics[case.case_id].append("offline compliance failed")
    return {case_id: tuple(messages) for case_id, messages in sorted(diagnostics.items())}

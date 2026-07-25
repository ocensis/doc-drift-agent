from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Sequence
from typing import Any, Literal, TypeAlias

from drift_agent.evaluation.stage4_models import (
    HYPOTHESIS_STATEMENT,
    MEMORY_NOT_MEASURED_REASON,
    ComparisonBoolMetric,
    ComparisonLayer,
    ComparisonObservationV1,
    ComparisonSubject,
    ComparisonUsage,
    Stage4ComparisonReport,
    Stage4CompletenessCounts,
    Stage4EfficiencyAggregate,
    Stage4HypothesisAssessment,
    Stage4Incomparable,
    Stage4MemoryAggregate,
    Stage4MetricAggregate,
    Stage4Pair,
    Stage4Percentile,
    Stage4QualityAggregate,
    Stage4Ratio,
    Stage4SafetyAggregate,
    Stage4StratumAggregate,
    Stage4SystemAggregate,
    Stage4UsageAggregate,
)

_SUBJECTS: tuple[ComparisonSubject, ...] = ("codex", "drift_agent")
_LAYERS: tuple[ComparisonLayer, ...] = (
    "structural",
    "executable",
    "semantic",
)
_MAX_OBSERVATION_BYTES = 1_000_000
_PairTuple: TypeAlias = tuple[str, str, str, str, str, str, str]
_BaseTuple: TypeAlias = tuple[str, str, str]
_CaseTuple: TypeAlias = tuple[str, str]


def _observation_sort_key(
    observation: ComparisonObservationV1,
) -> tuple[str, str, str, str, str]:
    return (
        observation.dataset_id,
        observation.case_id,
        observation.trial_id,
        observation.subject,
        observation.observation_id,
    )


def _pair_tuple(observation: ComparisonObservationV1) -> _PairTuple:
    return (
        observation.dataset_id,
        observation.case_id,
        observation.case_manifest_sha256,
        observation.trial_id,
        observation.snapshot_digest,
        observation.task_digest,
        observation.scope_digest,
    )


def _base_tuple(observation: ComparisonObservationV1) -> _BaseTuple:
    return (
        observation.dataset_id,
        observation.case_id,
        observation.trial_id,
    )


def _case_tuple(observation: ComparisonObservationV1) -> _CaseTuple:
    return (observation.dataset_id, observation.case_id)


def _validate_observation_set(
    observations: Sequence[ComparisonObservationV1],
) -> None:
    observation_ids: set[str] = set()
    subject_trials: set[tuple[str, str, str, str]] = set()
    subject_pairs: set[tuple[str, *_PairTuple]] = set()
    for observation in observations:
        if observation.observation_id in observation_ids:
            raise ValueError(f"duplicate Stage 4 observation id: {observation.observation_id}")
        observation_ids.add(observation.observation_id)

        subject_trial = (observation.subject, *_base_tuple(observation))
        if subject_trial in subject_trials:
            raise ValueError("conflicting Stage 4 observations for subject/dataset/case/trial")
        subject_trials.add(subject_trial)

        subject_pair = (observation.subject, *_pair_tuple(observation))
        if subject_pair in subject_pairs:
            raise ValueError("duplicate Stage 4 subject/pair key")
        subject_pairs.add(subject_pair)


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def import_stage4_observation(payload: bytes | str) -> ComparisonObservationV1:
    """Import one bounded JSON observation without running either subject."""

    raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    if len(raw) > _MAX_OBSERVATION_BYTES:
        raise ValueError("Stage 4 observation exceeds the byte limit")
    try:
        document = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid Stage 4 observation JSON") from error
    if not isinstance(document, dict):
        raise ValueError("Stage 4 observation JSON must be an object")
    # Validate in JSON mode so JSON arrays are accepted for frozen tuple fields
    # without weakening strict scalar validation.
    return ComparisonObservationV1.model_validate_json(raw)


def import_stage4_observations(
    payloads: Sequence[bytes | str],
) -> tuple[ComparisonObservationV1, ...]:
    observations = tuple(import_stage4_observation(payload) for payload in payloads)
    _validate_observation_set(observations)
    return tuple(sorted(observations, key=_observation_sort_key))


def _not_measured_ratio(reason: str) -> Stage4Ratio:
    return Stage4Ratio(
        status="not_measured",
        numerator=None,
        denominator=None,
        reason=reason,
    )


def _ratio(numerator: int, denominator: int, *, zero_reason: str) -> Stage4Ratio:
    if denominator == 0:
        return _not_measured_ratio(zero_reason)
    return Stage4Ratio(
        status="measured",
        numerator=numerator,
        denominator=denominator,
        reason=None,
    )


def _bool_metric_ratio(
    observations: Sequence[ComparisonObservationV1],
    project: Callable[[ComparisonObservationV1], ComparisonBoolMetric],
) -> Stage4Ratio:
    measured = [
        metric.value
        for observation in observations
        if (metric := project(observation)).status == "measured"
    ]
    return _ratio(
        sum(value is True for value in measured),
        len(measured),
        zero_reason="no measured values",
    )


def _completeness_counts(statuses: Iterable[str]) -> Stage4CompletenessCounts:
    values = tuple(statuses)
    return Stage4CompletenessCounts(
        measured=values.count("measured"),
        not_measured=values.count("not_measured"),
        accounting_incomplete=values.count("accounting_incomplete"),
    )


def _aggregate_nullable_int(
    observations: Sequence[ComparisonObservationV1],
    status: Callable[[ComparisonObservationV1], str],
    value: Callable[[ComparisonObservationV1], int | None],
) -> Stage4MetricAggregate:
    measured: list[int] = []
    incomplete_known: list[int] = []
    incomplete_unknown_count = 0
    not_measured_count = 0
    for observation in observations:
        completeness = status(observation)
        item = value(observation)
        if completeness == "measured":
            if item is None:  # guarded by the strict observation model
                raise ValueError("measured metric unexpectedly has no value")
            measured.append(item)
        elif completeness == "accounting_incomplete":
            if item is None:
                incomplete_unknown_count += 1
            else:
                incomplete_known.append(item)
        else:
            not_measured_count += 1
    return Stage4MetricAggregate(
        measured_total=sum(measured) if measured else None,
        measured_count=len(measured),
        incomplete_known_total=(sum(incomplete_known) if incomplete_known else None),
        incomplete_known_count=len(incomplete_known),
        incomplete_unknown_count=incomplete_unknown_count,
        not_measured_count=not_measured_count,
    )


def _quality_aggregate(
    observations: Sequence[ComparisonObservationV1],
) -> Stage4QualityAggregate:
    count = len(observations)
    tp = sum(observation.outcome.tp for observation in observations)
    fp = sum(observation.outcome.fp for observation in observations)
    fn = sum(observation.outcome.fn for observation in observations)
    validation_measured = [
        observation.validation.passed
        for observation in observations
        if observation.validation.status == "measured"
    ]
    regression_measured = [
        observation.safety.regression_free
        for observation in observations
        if observation.safety.status == "measured"
    ]
    return Stage4QualityAggregate(
        observation_count=count,
        passed=_ratio(
            sum(observation.outcome.passed for observation in observations),
            count,
            zero_reason="no paired observations",
        ),
        tp=tp,
        fp=fp,
        fn=fn,
        precision=_ratio(tp, tp + fp, zero_reason="zero precision denominator"),
        recall=_ratio(tp, tp + fn, zero_reason="zero recall denominator"),
        f1=_ratio(
            2 * tp,
            2 * tp + fp + fn,
            zero_reason="zero F1 denominator",
        ),
        repair_success_at_1=_bool_metric_ratio(
            observations,
            lambda observation: observation.outcome.repair_success_at_1,
        ),
        repair_success_at_2=_bool_metric_ratio(
            observations,
            lambda observation: observation.outcome.repair_success_at_2,
        ),
        correct_abstention=_bool_metric_ratio(
            observations,
            lambda observation: observation.outcome.correct_abstention,
        ),
        validation_pass=_ratio(
            sum(value is True for value in validation_measured),
            len(validation_measured),
            zero_reason="no measured validation results",
        ),
        validation_completeness=_completeness_counts(
            observation.validation.status for observation in observations
        ),
        regression_free=_ratio(
            sum(value is True for value in regression_measured),
            len(regression_measured),
            zero_reason="no measured safety results",
        ),
    )


def _usage_aggregate(
    observations: Sequence[ComparisonObservationV1],
) -> Stage4UsageAggregate:
    def metric(
        project: Callable[[ComparisonUsage], int | None],
    ) -> Stage4MetricAggregate:
        return _aggregate_nullable_int(
            observations,
            lambda observation: observation.usage.status,
            lambda observation: project(observation.usage),
        )

    return Stage4UsageAggregate(
        completeness=_completeness_counts(observation.usage.status for observation in observations),
        model_calls=metric(lambda usage: usage.model_calls),
        strong_model_calls=metric(lambda usage: usage.strong_model_calls),
        tool_calls=metric(lambda usage: usage.tool_calls),
        input_tokens=metric(lambda usage: usage.input_tokens),
        output_tokens=metric(lambda usage: usage.output_tokens),
        cost_nano_usd=metric(lambda usage: usage.cost_nano_usd),
        duration_ms=metric(lambda usage: usage.duration_ms),
    )


def _percentile(values: Sequence[int], percentile: int) -> Stage4Percentile:
    if not values:
        return Stage4Percentile(
            status="not_measured",
            value_ms=None,
            measured_count=0,
            reason="no successful repair with measured wall-clock time",
        )
    ordered = sorted(values)
    rank = max(1, (percentile * len(ordered) + 99) // 100)
    return Stage4Percentile(
        status="measured",
        value_ms=ordered[rank - 1],
        measured_count=len(ordered),
        reason=None,
    )


def _efficiency_aggregate(
    observations: Sequence[ComparisonObservationV1],
) -> Stage4EfficiencyAggregate:
    successful = tuple(
        observation for observation in observations if observation.outcome.successful_repair
    )
    durations = [
        observation.usage.duration_ms
        for observation in successful
        if observation.usage.status == "measured" and observation.usage.duration_ms is not None
    ]
    measured_usage = tuple(
        observation for observation in observations if observation.usage.status == "measured"
    )
    strong_calls = sum(observation.usage.strong_model_calls or 0 for observation in measured_usage)
    model_calls = sum(observation.usage.model_calls or 0 for observation in measured_usage)
    return Stage4EfficiencyAggregate(
        usage=_usage_aggregate(observations),
        per_success=_usage_aggregate(successful),
        wall_clock_p50=_percentile(durations, 50),
        wall_clock_p95=_percentile(durations, 95),
        strong_profile_ratio=_ratio(
            strong_calls,
            model_calls,
            zero_reason="zero measured model-call denominator",
        ),
    )


def _safety_aggregate(
    observations: Sequence[ComparisonObservationV1],
) -> Stage4SafetyAggregate:
    return Stage4SafetyAggregate(
        completeness=_completeness_counts(
            observation.safety.status for observation in observations
        ),
        business_code_mutations=_aggregate_nullable_int(
            observations,
            lambda observation: observation.safety.status,
            lambda observation: observation.safety.business_code_mutations,
        ),
        stale_overwrites=_aggregate_nullable_int(
            observations,
            lambda observation: observation.safety.status,
            lambda observation: observation.safety.stale_overwrites,
        ),
    )


def _system_aggregate(
    subject: ComparisonSubject,
    imported: Sequence[ComparisonObservationV1],
    paired: Sequence[ComparisonObservationV1],
) -> Stage4SystemAggregate:
    imported_subject = tuple(
        observation for observation in imported if observation.subject == subject
    )
    paired_subject = tuple(observation for observation in paired if observation.subject == subject)
    return Stage4SystemAggregate(
        subject=subject,
        status="measured" if paired_subject else "pending",
        imported_observation_count=len(imported_subject),
        paired_observation_count=len(paired_subject),
        quality=_quality_aggregate(paired_subject),
        efficiency=_efficiency_aggregate(paired_subject),
        safety=_safety_aggregate(paired_subject),
    )


def _pair_observations(
    observations: Sequence[ComparisonObservationV1],
) -> tuple[
    tuple[Stage4Pair, ...],
    tuple[Stage4Incomparable, ...],
    tuple[ComparisonObservationV1, ...],
]:
    exact: dict[_PairTuple, dict[ComparisonSubject, ComparisonObservationV1]] = {}
    by_case: dict[_CaseTuple, set[ComparisonSubject]] = {}
    for observation in observations:
        exact.setdefault(_pair_tuple(observation), {})[observation.subject] = observation
        by_case.setdefault(_case_tuple(observation), set()).add(observation.subject)

    pairs: list[Stage4Pair] = []
    incomparable: list[Stage4Incomparable] = []
    paired_observations: list[ComparisonObservationV1] = []
    for key in sorted(exact):
        group = exact[key]
        codex = group.get("codex")
        drift_agent = group.get("drift_agent")
        if codex is not None and drift_agent is not None:
            pairs.append(
                Stage4Pair(
                    key=codex.pair_key,
                    codex_observation_id=codex.observation_id,
                    drift_agent_observation_id=drift_agent.observation_id,
                )
            )
            paired_observations.extend((codex, drift_agent))
            continue

        if codex is not None:
            singleton = codex
        elif drift_agent is not None:
            singleton = drift_agent
        else:  # pragma: no cover - exact groups are non-empty
            raise AssertionError("empty pair group")
        other_subject: ComparisonSubject = (
            "drift_agent" if singleton.subject == "codex" else "codex"
        )
        case_subjects = by_case[_case_tuple(singleton)]
        reason: Literal["missing_codex", "missing_drift_agent", "pair_key_mismatch"]
        if other_subject in case_subjects:
            reason = "pair_key_mismatch"
        elif singleton.subject == "codex":
            reason = "missing_drift_agent"
        else:
            reason = "missing_codex"
        incomparable.append(
            Stage4Incomparable(
                observation_id=singleton.observation_id,
                subject=singleton.subject,
                key=singleton.pair_key,
                reason=reason,
            )
        )

    return (
        tuple(pairs),
        tuple(
            sorted(
                incomparable,
                key=lambda item: (
                    item.key.dataset_id,
                    item.key.case_id,
                    item.key.trial_id,
                    item.subject,
                    item.observation_id,
                ),
            )
        ),
        tuple(sorted(paired_observations, key=_observation_sort_key)),
    )


def build_stage4_comparison(
    observations: Sequence[ComparisonObservationV1],
) -> Stage4ComparisonReport:
    """Build a deterministic paired report from normalized offline evidence only."""

    _validate_observation_set(observations)
    ordered = tuple(sorted(observations, key=_observation_sort_key))
    pairs, incomparable, paired = _pair_observations(ordered)
    systems = tuple(_system_aggregate(subject, ordered, paired) for subject in _SUBJECTS)
    strata = tuple(
        Stage4StratumAggregate(
            layer=layer,
            paired_case_count=sum(
                observation.case_layer == layer
                for observation in paired
                if observation.subject == "codex"
            ),
            systems=tuple(
                _system_aggregate(
                    subject,
                    tuple(item for item in ordered if item.case_layer == layer),
                    tuple(item for item in paired if item.case_layer == layer),
                )
                for subject in _SUBJECTS
            ),
        )
        for layer in _LAYERS
    )
    pending_subjects = tuple(
        subject
        for subject in _SUBJECTS
        if not any(observation.subject == subject for observation in ordered)
    )
    comparison_complete = bool(pairs) and not incomparable
    hypothesis_status: Literal["not_measured", "insufficient_samples"]
    if comparison_complete:
        hypothesis_status = "insufficient_samples"
        hypothesis_reason = (
            "paired offline observations expose the measurements but do not establish "
            "a statistically sufficient superiority conclusion"
        )
    else:
        hypothesis_status = "not_measured"
        hypothesis_reason = (
            "the exact paired comparison is incomplete, so the design hypothesis "
            "cannot be evaluated"
        )
    return Stage4ComparisonReport(
        schema_version=1,
        observations=ordered,
        pairs=pairs,
        incomparable=incomparable,
        systems=systems,
        strata=strata,
        paired_case_count=len(pairs),
        comparison_complete=comparison_complete,
        pending_subjects=pending_subjects,
        memory=Stage4MemoryAggregate(
            status="not_measured",
            reason=MEMORY_NOT_MEASURED_REASON,
        ),
        hypothesis=Stage4HypothesisAssessment(
            statement=HYPOTHESIS_STATEMENT,
            status=hypothesis_status,
            reason=hypothesis_reason,
        ),
    )


def deterministic_stage4_projection(
    value: ComparisonObservationV1 | Stage4ComparisonReport,
) -> bytes:
    return json.dumps(
        value.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _escape_markdown(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|")


def _row(values: Iterable[str]) -> str:
    return "| " + " | ".join(values) + " |"


def _format_ratio(metric: Stage4Ratio) -> str:
    if metric.status == "not_measured":
        return f"not_measured ({_escape_markdown(metric.reason or '')})"
    return f"{metric.numerator}/{metric.denominator}"


def _format_total(metric: Stage4MetricAggregate) -> str:
    measured = (
        "not_measured"
        if metric.measured_total is None
        else f"{metric.measured_total}/{metric.measured_count}"
    )
    if metric.incomplete_known_count or metric.incomplete_unknown_count:
        known = (
            "null" if metric.incomplete_known_total is None else str(metric.incomplete_known_total)
        )
        measured += (
            f"; incomplete_known={known}/{metric.incomplete_known_count}"
            f"; incomplete_unknown={metric.incomplete_unknown_count}"
        )
    if metric.not_measured_count:
        measured += f"; not_measured={metric.not_measured_count}"
    return measured


def render_stage4_comparison_markdown(report: Stage4ComparisonReport) -> str:
    """Render deterministic Markdown without implying a live Codex execution."""

    pending = ", ".join(report.pending_subjects) or "none"
    lines = [
        "# Stage 4 Offline Comparison",
        "",
        f"- Comparison complete: {'yes' if report.comparison_complete else 'no'}",
        f"- Exact paired cases: {report.paired_case_count}",
        f"- Incomparable observations: {len(report.incomparable)}",
        f"- Pending subjects: {pending}",
        "",
    ]
    if any(item.subject == "codex" for item in report.observations):
        lines.extend(
            [
                "> Codex provenance is an unverified external declaration. This "
                "offline importer did not execute Codex or verify authorization.",
                "",
            ]
        )
    else:
        lines.extend(
            [
                "> Codex is pending/not measured. This offline importer has no live "
                "execution path.",
                "",
            ]
        )

    lines.extend(
        [
            "## Quality and Safety",
            "",
            _row(
                (
                    "Subject",
                    "Paired",
                    "TP",
                    "FP",
                    "FN",
                    "Precision",
                    "Recall",
                    "F1",
                    "Repair@1",
                    "Repair@2",
                    "Abstention",
                    "Validation",
                    "Regression-free",
                    "Business mutations",
                    "Stale overwrites",
                )
            ),
            _row(("---",) * 15),
        ]
    )
    for system in report.systems:
        lines.append(
            _row(
                (
                    system.subject,
                    str(system.paired_observation_count),
                    str(system.quality.tp),
                    str(system.quality.fp),
                    str(system.quality.fn),
                    _format_ratio(system.quality.precision),
                    _format_ratio(system.quality.recall),
                    _format_ratio(system.quality.f1),
                    _format_ratio(system.quality.repair_success_at_1),
                    _format_ratio(system.quality.repair_success_at_2),
                    _format_ratio(system.quality.correct_abstention),
                    _format_ratio(system.quality.validation_pass),
                    _format_ratio(system.quality.regression_free),
                    _format_total(system.safety.business_code_mutations),
                    _format_total(system.safety.stale_overwrites),
                )
            )
        )

    lines.extend(
        [
            "",
            "## Efficiency per successful repair",
            "",
            _row(
                (
                    "Subject",
                    "Model calls",
                    "Tool calls",
                    "Input tokens",
                    "Output tokens",
                    "Known cost (nano USD)",
                    "Wall p50 (ms)",
                    "Wall p95 (ms)",
                    "Strong profile",
                )
            ),
            _row(("---",) * 9),
        ]
    )
    for system in report.systems:
        per_success = system.efficiency.per_success
        p50 = system.efficiency.wall_clock_p50
        p95 = system.efficiency.wall_clock_p95
        lines.append(
            _row(
                (
                    system.subject,
                    _format_total(per_success.model_calls),
                    _format_total(per_success.tool_calls),
                    _format_total(per_success.input_tokens),
                    _format_total(per_success.output_tokens),
                    _format_total(per_success.cost_nano_usd),
                    str(p50.value_ms) if p50.value_ms is not None else "not_measured",
                    str(p95.value_ms) if p95.value_ms is not None else "not_measured",
                    _format_ratio(system.efficiency.strong_profile_ratio),
                )
            )
        )

    lines.extend(
        [
            "",
            "## Layer conservation",
            "",
            _row(("Layer", "Paired cases", "Codex observations", "Drift Agent observations")),
            _row(("---",) * 4),
        ]
    )
    for stratum in report.strata:
        lines.append(
            _row(
                (
                    stratum.layer,
                    str(stratum.paired_case_count),
                    str(stratum.systems[0].paired_observation_count),
                    str(stratum.systems[1].paired_observation_count),
                )
            )
        )

    if report.incomparable:
        lines.extend(
            [
                "",
                "## Incomparable observations",
                "",
                _row(("Observation", "Subject", "Dataset", "Case", "Trial", "Reason")),
                _row(("---",) * 6),
            ]
        )
        for item in report.incomparable:
            lines.append(
                _row(
                    (
                        item.observation_id,
                        item.subject,
                        item.key.dataset_id,
                        item.key.case_id,
                        item.key.trial_id,
                        item.reason,
                    )
                )
            )

    lines.extend(
        [
            "",
            "## Memory",
            "",
            f"- Status: `{report.memory.status}`",
            f"- Reason: {report.memory.reason}.",
            "",
            "## Design hypothesis",
            "",
            f"- Statement: {report.hypothesis.statement}.",
            f"- Status: `{report.hypothesis.status}`",
            f"- Reason: {report.hypothesis.reason}.",
            "",
        ]
    )
    return "\n".join(lines)


def stage4_comparison_artifacts(
    report: Stage4ComparisonReport,
) -> dict[str, bytes]:
    """Return the two fixed, deterministic comparison artifact payloads."""

    return {
        "comparison-report.json": deterministic_stage4_projection(report) + b"\n",
        "comparison-report.md": render_stage4_comparison_markdown(report).encode("utf-8"),
    }


__all__ = [
    "build_stage4_comparison",
    "deterministic_stage4_projection",
    "import_stage4_observation",
    "import_stage4_observations",
    "render_stage4_comparison_markdown",
    "stage4_comparison_artifacts",
]

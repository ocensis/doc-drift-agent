from __future__ import annotations

import copy
import hashlib
import json
import socket
import subprocess
from typing import Any

import pytest
from pydantic import ValidationError

from drift_agent.evaluation.stage4_comparison import (
    build_stage4_comparison,
    deterministic_stage4_projection,
    import_stage4_observation,
    import_stage4_observations,
    render_stage4_comparison_markdown,
    stage4_comparison_artifacts,
)
from drift_agent.evaluation.stage4_models import (
    ComparisonBoolMetric,
    ComparisonChangedBytes,
    ComparisonCompleteness,
    ComparisonLayer,
    ComparisonObservationV1,
    ComparisonOutcome,
    ComparisonProvenance,
    ComparisonSafety,
    ComparisonSubject,
    ComparisonUsage,
    ComparisonValidation,
    Stage4DatasetId,
)

_CASES: dict[str, tuple[Stage4DatasetId, str, ComparisonLayer]] = {
    "click.parameter-default.v1": (
        "structural-v1",
        "a3f09ea0256ac655227c0d7590890cce487de28eedffa482a5f4872a9c799ede",
        "structural",
    ),
    "executable.doctest-pass.v1": (
        "stage3-v1",
        "7cc0a8d892866966e291e5c826113b3b8288b6c4fd153a41b0ef8225864c7247",
        "executable",
    ),
    "semantic.fast-success.v1": (
        "stage3-v1",
        "4c0b891c42e1d33f7c275858cf58fc7c16bd4e57c359a986c8e7c25ecc6764a3",
        "semantic",
    ),
}


def _sha(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _digest(seed: str) -> str:
    return f"sha256:{_sha(seed)}"


def _bool_metric(
    value: bool | None,
    status: ComparisonCompleteness = "measured",
) -> ComparisonBoolMetric:
    return ComparisonBoolMetric(
        status=status,
        value=value,
        reason=None if status == "measured" else f"{status} quality evidence",
    )


def _usage(
    status: ComparisonCompleteness = "measured",
    *,
    model_calls: int | None = 1,
    strong_model_calls: int | None = 0,
    tool_calls: int | None = 2,
    input_tokens: int | None = 10,
    output_tokens: int | None = 5,
    cost_nano_usd: int | None = 100,
    duration_ms: int | None = 20,
) -> ComparisonUsage:
    if status == "not_measured":
        model_calls = None
        strong_model_calls = None
        tool_calls = None
        input_tokens = None
        output_tokens = None
        cost_nano_usd = None
        duration_ms = None
    return ComparisonUsage(
        status=status,
        model_calls=model_calls,
        strong_model_calls=strong_model_calls,
        tool_calls=tool_calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_nano_usd=cost_nano_usd,
        duration_ms=duration_ms,
        reason=None if status == "measured" else f"{status} usage evidence",
    )


def _validation(
    status: ComparisonCompleteness = "measured",
    *,
    passed: bool | None = True,
) -> ComparisonValidation:
    if status != "measured":
        passed = None
    return ComparisonValidation(
        status=status,
        passed=passed,
        reason=None if status == "measured" else f"{status} validation evidence",
    )


def _safety(
    status: ComparisonCompleteness = "measured",
    *,
    regression_free: bool | None = True,
    business_code_mutations: int | None = 0,
    stale_overwrites: int | None = 0,
) -> ComparisonSafety:
    if status == "not_measured":
        regression_free = None
        business_code_mutations = None
        stale_overwrites = None
    return ComparisonSafety(
        status=status,
        regression_free=regression_free,
        business_code_mutations=business_code_mutations,
        stale_overwrites=stale_overwrites,
        reason=None if status == "measured" else f"{status} safety evidence",
    )


def _observation(
    subject: ComparisonSubject,
    *,
    case_id: str = "click.parameter-default.v1",
    trial_id: str = "trial-1",
    observation_id: str | None = None,
    snapshot_seed: str | None = None,
    task_seed: str | None = None,
    scope_seed: str | None = None,
    manifest_sha256: str | None = None,
    passed: bool = True,
    tp: int = 1,
    fp: int = 0,
    fn: int = 0,
    successful_repair: bool = True,
    repair_at_1: ComparisonBoolMetric | None = None,
    repair_at_2: ComparisonBoolMetric | None = None,
    abstention: ComparisonBoolMetric | None = None,
    validation: ComparisonValidation | None = None,
    safety: ComparisonSafety | None = None,
    usage: ComparisonUsage | None = None,
    changed_bytes: tuple[ComparisonChangedBytes, ...] | None = None,
) -> ComparisonObservationV1:
    dataset_id, frozen_manifest, layer = _CASES[case_id]
    key_seed = f"{case_id}:{trial_id}"
    provenance = (
        ComparisonProvenance(
            runner_kind="external_self_declared",
            runner_version="codex-external-v1",
            model_id="normalized-codex-model",
            tool_profile_digest=_digest("codex-tools"),
            budget_source="external-run-declaration",
            claim_status="unverified_external_declaration",
            authorization_status="self_declared_not_verified",
        )
        if subject == "codex"
        else ComparisonProvenance(
            runner_kind="local_offline_runner",
            runner_version="drift-agent-stage4-v1",
            model_id="normalized-drift-profile",
            tool_profile_digest=_digest("drift-tools"),
            budget_source="frozen-evaluation-budget",
            claim_status="locally_verified",
            authorization_status="not_applicable",
        )
    )
    changes = changed_bytes
    if changes is None:
        changes = (
            ComparisonChangedBytes(
                path="docs/api.md",
                before_sha256=_sha(f"before:{key_seed}"),
                after_sha256=_sha(f"after:{key_seed}"),
            ),
        )
    return ComparisonObservationV1(
        schema_version=1,
        observation_id=observation_id or f"obs_{subject}_{case_id}_{trial_id}",
        subject=subject,
        dataset_id=dataset_id,
        case_id=case_id,
        case_manifest_sha256=manifest_sha256 or frozen_manifest,
        trial_id=trial_id,
        snapshot_digest=_digest(snapshot_seed or f"snapshot:{key_seed}"),
        task_digest=_digest(task_seed or f"task:{key_seed}"),
        scope_digest=_digest(scope_seed or f"scope:{key_seed}"),
        evidence_sha256=_sha(f"evidence:{subject}:{key_seed}"),
        case_layer=layer,
        outcome=ComparisonOutcome(
            passed=passed,
            tp=tp,
            fp=fp,
            fn=fn,
            successful_repair=successful_repair,
            repair_success_at_1=repair_at_1 or _bool_metric(True),
            repair_success_at_2=repair_at_2 or _bool_metric(True),
            correct_abstention=abstention or _bool_metric(True),
        ),
        changed_bytes=changes,
        validation=validation or _validation(),
        safety=safety or _safety(),
        usage=usage or _usage(),
        provenance=provenance,
    )


def _wire(observation: ComparisonObservationV1) -> bytes:
    return json.dumps(
        observation.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _deny_external(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("offline comparison attempted an external call")


def test_strict_offline_import_rejects_unsafe_or_unfrozen_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "create_connection", _deny_external)
    monkeypatch.setattr(subprocess, "run", _deny_external)
    observation = _observation("codex")
    imported = import_stage4_observation(_wire(observation))
    schema = ComparisonObservationV1.model_json_schema()

    assert imported == observation
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["ComparisonUsage"]["additionalProperties"] is False
    with pytest.raises(ValidationError, match="frozen"):
        imported.case_id = "changed"  # type: ignore[misc]

    base = observation.model_dump(mode="json")
    invalid_documents: list[dict[str, Any]] = []

    for forbidden in ("prompt", "secret", "raw_repository"):
        document = copy.deepcopy(base)
        document[forbidden] = "forbidden payload"
        invalid_documents.append(document)

    absolute_path = copy.deepcopy(base)
    absolute_path["changed_bytes"][0]["path"] = "/private/tmp/repository.py"
    invalid_documents.append(absolute_path)

    noncanonical_path = copy.deepcopy(base)
    noncanonical_path["changed_bytes"][0]["path"] = "docs//api.md"
    invalid_documents.append(noncanonical_path)

    invalid_digest = copy.deepcopy(base)
    invalid_digest["scope_digest"] = "sha256:not-a-digest"
    invalid_documents.append(invalid_digest)

    missing_completeness = copy.deepcopy(base)
    del missing_completeness["usage"]["status"]
    invalid_documents.append(missing_completeness)

    changed_manifest = copy.deepcopy(base)
    changed_manifest["case_manifest_sha256"] = _sha("not-the-frozen-manifest")
    invalid_documents.append(changed_manifest)

    for document in invalid_documents:
        with pytest.raises((ValidationError, ValueError)):
            import_stage4_observation(json.dumps(document))

    with pytest.raises(ValueError, match="duplicate JSON key"):
        import_stage4_observation('{"schema_version":1,"schema_version":1}')

    with pytest.raises(ValueError, match="duplicate Stage 4 observation id"):
        import_stage4_observations((_wire(observation), _wire(observation)))


def test_exact_pair_key_mismatch_is_incomparable_and_outside_denominator() -> None:
    exact_codex = _observation("codex", tp=3)
    exact_drift = _observation("drift_agent", tp=2)
    mismatch_codex = _observation(
        "codex",
        trial_id="trial-2",
        snapshot_seed="codex-snapshot",
        tp=100,
    )
    mismatch_drift = _observation(
        "drift_agent",
        trial_id="trial-2",
        snapshot_seed="drift-snapshot",
        tp=200,
    )
    missing_codex = _observation(
        "drift_agent",
        case_id="semantic.fast-success.v1",
        trial_id="trial-3",
        tp=300,
    )

    report = build_stage4_comparison(
        (mismatch_drift, missing_codex, exact_codex, mismatch_codex, exact_drift)
    )

    assert report.paired_case_count == 1
    assert report.comparison_complete is False
    assert [item.reason for item in report.incomparable] == [
        "missing_codex",
        "pair_key_mismatch",
        "pair_key_mismatch",
    ]
    codex, drift_agent = report.systems
    assert codex.paired_observation_count == 1
    assert drift_agent.paired_observation_count == 1
    assert codex.quality.tp == 3
    assert drift_agent.quality.tp == 2


def test_duplicate_id_and_conflicting_subject_trial_fail_closed() -> None:
    first = _observation("codex", observation_id="obs_same")
    duplicate_id = _observation(
        "drift_agent",
        trial_id="trial-2",
        observation_id="obs_same",
    )
    with pytest.raises(ValueError, match="duplicate Stage 4 observation id"):
        build_stage4_comparison((first, duplicate_id))

    conflict = _observation(
        "codex",
        observation_id="obs_conflict",
        snapshot_seed="conflicting-snapshot",
    )
    with pytest.raises(ValueError, match="conflicting Stage 4 observations"):
        build_stage4_comparison((first, conflict))


def test_completeness_preserves_null_and_known_incomplete_subtotals() -> None:
    observations: list[ComparisonObservationV1] = []
    for trial_id in ("trial-1", "trial-2", "trial-3"):
        observations.append(_observation("codex", trial_id=trial_id))
    observations.extend(
        (
            _observation(
                "drift_agent",
                trial_id="trial-1",
                usage=_usage(model_calls=2, tool_calls=4),
                validation=_validation(passed=True),
                safety=_safety(business_code_mutations=0),
            ),
            _observation(
                "drift_agent",
                trial_id="trial-2",
                usage=_usage("not_measured"),
                validation=_validation("not_measured"),
                safety=_safety("not_measured"),
            ),
            _observation(
                "drift_agent",
                trial_id="trial-3",
                usage=_usage(
                    "accounting_incomplete",
                    model_calls=5,
                    strong_model_calls=1,
                    tool_calls=None,
                    input_tokens=20,
                    output_tokens=None,
                    cost_nano_usd=250,
                    duration_ms=None,
                ),
                validation=_validation("accounting_incomplete"),
                safety=_safety(
                    "accounting_incomplete",
                    regression_free=None,
                    business_code_mutations=2,
                    stale_overwrites=None,
                ),
            ),
        )
    )

    report = build_stage4_comparison(tuple(observations))
    drift_agent = report.systems[1]
    calls = drift_agent.efficiency.usage.model_calls
    tools = drift_agent.efficiency.usage.tool_calls

    assert calls.model_dump() == {
        "measured_total": 2,
        "measured_count": 1,
        "incomplete_known_total": 5,
        "incomplete_known_count": 1,
        "incomplete_unknown_count": 0,
        "not_measured_count": 1,
    }
    assert tools.measured_total == 4
    assert tools.incomplete_known_total is None
    assert tools.incomplete_unknown_count == 1
    assert tools.not_measured_count == 1
    assert drift_agent.quality.validation_completeness.model_dump() == {
        "measured": 1,
        "not_measured": 1,
        "accounting_incomplete": 1,
    }
    assert drift_agent.quality.validation_pass.model_dump() == {
        "status": "measured",
        "numerator": 1,
        "denominator": 1,
        "reason": None,
    }
    mutations = drift_agent.safety.business_code_mutations
    assert mutations.measured_total == 0
    assert mutations.incomplete_known_total == 2
    assert drift_agent.quality.regression_free.denominator == 1


def test_quality_and_efficiency_formula_oracles() -> None:
    observations = (
        _observation("codex", trial_id="trial-1"),
        _observation("codex", trial_id="trial-2"),
        _observation(
            "drift_agent",
            trial_id="trial-1",
            passed=True,
            tp=2,
            fp=1,
            fn=0,
            repair_at_1=_bool_metric(True),
            repair_at_2=_bool_metric(True),
            abstention=_bool_metric(True),
            validation=_validation(passed=True),
            safety=_safety(
                regression_free=True,
                business_code_mutations=1,
                stale_overwrites=0,
            ),
            usage=_usage(
                model_calls=2,
                strong_model_calls=1,
                tool_calls=4,
                input_tokens=10,
                output_tokens=5,
                cost_nano_usd=100,
                duration_ms=10,
            ),
        ),
        _observation(
            "drift_agent",
            trial_id="trial-2",
            passed=False,
            tp=1,
            fp=0,
            fn=2,
            repair_at_1=_bool_metric(False),
            repair_at_2=_bool_metric(True),
            abstention=_bool_metric(False),
            validation=_validation(passed=False),
            safety=_safety(
                regression_free=False,
                business_code_mutations=2,
                stale_overwrites=1,
            ),
            usage=_usage(
                model_calls=4,
                strong_model_calls=1,
                tool_calls=6,
                input_tokens=20,
                output_tokens=7,
                cost_nano_usd=300,
                duration_ms=30,
            ),
        ),
    )

    report = build_stage4_comparison(observations)
    drift_agent = report.systems[1]
    quality = drift_agent.quality
    efficiency = drift_agent.efficiency

    assert (quality.tp, quality.fp, quality.fn) == (3, 1, 2)
    assert (quality.precision.numerator, quality.precision.denominator) == (3, 4)
    assert (quality.recall.numerator, quality.recall.denominator) == (3, 5)
    assert (quality.f1.numerator, quality.f1.denominator) == (6, 9)
    assert quality.repair_success_at_1.model_dump()["numerator"] == 1
    assert quality.repair_success_at_2.model_dump()["numerator"] == 2
    assert quality.correct_abstention.model_dump()["numerator"] == 1
    assert quality.validation_pass.model_dump()["numerator"] == 1
    assert quality.regression_free.model_dump()["numerator"] == 1
    assert drift_agent.safety.business_code_mutations.measured_total == 3
    assert drift_agent.safety.stale_overwrites.measured_total == 1

    per_success = efficiency.per_success
    assert per_success.model_calls.measured_total == 6
    assert per_success.model_calls.measured_count == 2
    assert per_success.tool_calls.measured_total == 10
    assert per_success.input_tokens.measured_total == 30
    assert per_success.output_tokens.measured_total == 12
    assert per_success.cost_nano_usd.measured_total == 400
    assert efficiency.wall_clock_p50.value_ms == 10
    assert efficiency.wall_clock_p95.value_ms == 30
    assert (
        efficiency.strong_profile_ratio.numerator,
        efficiency.strong_profile_ratio.denominator,
    ) == (2, 6)


def test_layer_strata_conserve_overall_pairs() -> None:
    observations = tuple(
        _observation(subject, case_id=case_id)
        for case_id in _CASES
        for subject in ("codex", "drift_agent")
    )

    report = build_stage4_comparison(observations)

    assert report.paired_case_count == 3
    assert [stratum.layer for stratum in report.strata] == [
        "structural",
        "executable",
        "semantic",
    ]
    assert [stratum.paired_case_count for stratum in report.strata] == [1, 1, 1]
    assert sum(stratum.paired_case_count for stratum in report.strata) == (report.paired_case_count)
    for subject_index in (0, 1):
        assert (
            sum(stratum.systems[subject_index].quality.tp for stratum in report.strata)
            == report.systems[subject_index].quality.tp
        )


def test_zero_denominators_remain_not_measured() -> None:
    unavailable = _bool_metric(None, "not_measured")
    observations = tuple(
        _observation(
            subject,
            tp=0,
            fp=0,
            fn=0,
            successful_repair=False,
            repair_at_1=unavailable,
            repair_at_2=unavailable,
            abstention=unavailable,
            validation=_validation("not_measured"),
            safety=_safety("not_measured"),
            usage=_usage("not_measured"),
        )
        for subject in ("codex", "drift_agent")
    )

    report = build_stage4_comparison(observations)
    for system in report.systems:
        assert system.quality.precision.status == "not_measured"
        assert system.quality.recall.status == "not_measured"
        assert system.quality.f1.status == "not_measured"
        assert system.quality.validation_pass.status == "not_measured"
        assert system.quality.regression_free.status == "not_measured"
        assert system.efficiency.wall_clock_p50.status == "not_measured"
        assert system.efficiency.strong_profile_ratio.status == "not_measured"


@pytest.mark.parametrize(
    ("drift_tp", "drift_fp"),
    ((12, 0), (10, 0), (0, 10)),
    ids=("drift-better", "equal", "codex-better"),
)
def test_memory_hypothesis_and_codex_claim_never_prefill_a_winner(
    drift_tp: int,
    drift_fp: int,
) -> None:
    codex = _observation("codex", passed=True, tp=10)
    drift_agent = _observation(
        "drift_agent",
        passed=drift_fp == 0,
        tp=drift_tp,
        fp=drift_fp,
    )
    report = build_stage4_comparison((drift_agent, codex))
    projection = deterministic_stage4_projection(report)
    markdown = render_stage4_comparison_markdown(report)

    assert report.memory.status == "not_measured"
    for term in ("suppression", "expiry", "alias"):
        assert term in report.memory.reason
    assert report.hypothesis.status == "insufficient_samples"
    assert b"winner" not in projection.lower()
    assert "winner" not in markdown.lower()
    assert report.observations[0].provenance.claim_status == ("unverified_external_declaration")
    assert "unverified external declaration" in markdown
    assert "did not execute Codex or verify authorization" in markdown


def test_subject_provenance_cannot_claim_harness_verified_codex() -> None:
    codex_payload = _observation("codex").model_dump(mode="python")
    codex_payload["provenance"]["claim_status"] = "locally_verified"
    codex_payload["provenance"]["authorization_status"] = "not_applicable"
    codex_payload["provenance"]["runner_kind"] = "local_offline_runner"
    with pytest.raises(ValidationError, match="unverified external declaration"):
        ComparisonObservationV1.model_validate(codex_payload)

    drift_payload = _observation("drift_agent").model_dump(mode="python")
    drift_payload["provenance"]["claim_status"] = "unverified_external_declaration"
    drift_payload["provenance"]["authorization_status"] = "self_declared_not_verified"
    drift_payload["provenance"]["runner_kind"] = "external_self_declared"
    with pytest.raises(ValidationError, match="local offline replay"):
        ComparisonObservationV1.model_validate(drift_payload)


def test_json_and_markdown_artifacts_are_order_independent() -> None:
    observations = (
        _observation("drift_agent", trial_id="trial-2"),
        _observation("codex", trial_id="trial-1"),
        _observation("codex", trial_id="trial-2"),
        _observation("drift_agent", trial_id="trial-1"),
    )
    first = build_stage4_comparison(observations)
    second = build_stage4_comparison(tuple(reversed(observations)))

    assert deterministic_stage4_projection(first) == deterministic_stage4_projection(second)
    assert render_stage4_comparison_markdown(first) == (render_stage4_comparison_markdown(second))
    assert stage4_comparison_artifacts(first) == stage4_comparison_artifacts(second)
    assert tuple(stage4_comparison_artifacts(first)) == (
        "comparison-report.json",
        "comparison-report.md",
    )
    assert [item.observation_id for item in first.observations] == [
        "obs_codex_click.parameter-default.v1_trial-1",
        "obs_drift_agent_click.parameter-default.v1_trial-1",
        "obs_codex_click.parameter-default.v1_trial-2",
        "obs_drift_agent_click.parameter-default.v1_trial-2",
    ]


def test_missing_codex_is_explicitly_pending() -> None:
    report = build_stage4_comparison((_observation("drift_agent"),))
    markdown = render_stage4_comparison_markdown(report)

    assert report.paired_case_count == 0
    assert report.pending_subjects == ("codex",)
    assert report.hypothesis.status == "not_measured"
    assert report.systems[0].status == "pending"
    assert report.systems[0].quality.passed.status == "not_measured"
    assert "Codex is pending/not measured" in markdown
    assert "no live execution path" in markdown

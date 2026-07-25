from __future__ import annotations

from drift_agent.evaluation import (
    FROZEN_CASE_IDS,
    EvaluationRunner,
    deterministic_projection,
    group_failures_by_case,
)


def test_frozen_structural_catalog_replays_offline_with_zero_model_calls() -> None:
    report = EvaluationRunner().run_catalog()

    assert tuple(case.case_id for case in report.cases) == FROZEN_CASE_IDS
    assert report.summary.total == len(FROZEN_CASE_IDS) == 8
    assert report.summary.passed == report.summary.total, group_failures_by_case(report)
    assert report.summary.failed == 0
    assert report.summary.passed + report.summary.failed == report.summary.total
    assert report.summary.model_calls == 0
    assert report.summary.network_calls == 0
    assert report.summary.zero_model_compliance is True
    assert report.summary.offline_compliance is True
    assert report.summary.repair_successes == 4
    assert report.summary.conservative_rejections == 3


def test_fresh_replays_have_byte_equal_deterministic_projection() -> None:
    runner = EvaluationRunner()

    first = runner.run_case("click.parameter-default.v1")
    second = runner.run_case("click.parameter-default.v1")

    assert first.passed is True
    assert second.passed is True
    assert deterministic_projection(first) == deterministic_projection(second)

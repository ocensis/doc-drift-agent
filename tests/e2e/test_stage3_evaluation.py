from __future__ import annotations

from drift_agent.evaluation.stage3_catalog import FROZEN_STAGE3_CASE_IDS
from drift_agent.evaluation.stage3_metrics import deterministic_stage3_projection
from drift_agent.evaluation.stage3_runner import Stage3EvaluationRunner


def test_frozen_stage3_catalog_replays_offline_with_exact_routing_and_cost() -> None:
    report = Stage3EvaluationRunner().run_catalog()

    assert tuple(case.case_id for case in report.cases) == FROZEN_STAGE3_CASE_IDS
    assert report.summary.total == report.summary.passed == 10
    assert report.summary.failed == 0
    assert report.summary.semantic_repair_opportunities == 3
    assert report.summary.repair_success_at_1.model_dump() == {
        "numerator": 1,
        "denominator": 3,
    }
    assert report.summary.repair_success_at_2.model_dump() == {
        "numerator": 2,
        "denominator": 3,
    }
    assert report.summary.abstention_correctness.model_dump() == {
        "numerator": 1,
        "denominator": 1,
    }
    assert report.summary.fast_route_ratio.model_dump() == {
        "numerator": 3,
        "denominator": 5,
    }
    assert report.summary.strong_route_ratio.model_dump() == {
        "numerator": 2,
        "denominator": 5,
    }
    assert report.summary.model_calls == 5
    assert report.summary.validation_commands == 5
    assert report.summary.input_tokens == 35
    assert report.summary.output_tokens == 15
    assert report.summary.known_cost_nano_usd == 50_000
    assert report.summary.executable_zero_model_compliance is True
    assert report.summary.offline_compliance is True
    assert report.summary.model_script_compliance is True


def test_fresh_stage3_catalog_replays_have_byte_equal_normalized_projection() -> None:
    runner = Stage3EvaluationRunner()

    first = runner.run_catalog()
    second = runner.run_catalog()

    assert deterministic_stage3_projection(first) == deterministic_stage3_projection(second)

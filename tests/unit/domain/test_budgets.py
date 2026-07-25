import math

import pytest
from pydantic import ValidationError

from drift_agent.domain.enums import RunMode
from drift_agent.domain.models import RunBudgets, RunRequest


def test_run_budget_defaults_match_the_stage_three_contract() -> None:
    budgets = RunBudgets()

    assert budgets.model_dump() == {
        "max_patch_attempts_per_finding": 2,
        "max_model_calls_per_run": 4,
        "max_input_tokens_per_run": 20_000,
        "max_validation_commands_per_run": 8,
        "timeout_seconds": 120.0,
    }


def test_run_request_gets_an_independent_default_budget() -> None:
    first = RunRequest(mode=RunMode.CHECK, repo_path=".")
    second = RunRequest(mode=RunMode.REPAIR, repo_path=".")

    first.budgets.max_model_calls_per_run = 1

    assert second.budgets == RunBudgets()


def test_run_request_accepts_partial_budget_overrides() -> None:
    request = RunRequest.model_validate(
        {
            "mode": "check",
            "repo_path": ".",
            "budgets": {"timeout_seconds": 5, "max_model_calls_per_run": 0},
        }
    )

    assert request.budgets.timeout_seconds == 5
    assert request.budgets.max_model_calls_per_run == 0
    assert request.budgets.max_patch_attempts_per_finding == 2


def test_semantic_capabilities_are_mode_specific() -> None:
    check = RunRequest(
        mode=RunMode.CHECK,
        repo_path=".",
        semantic_analysis=True,
    )
    repair = RunRequest(
        mode=RunMode.REPAIR,
        repo_path=".",
        semantic_repair=True,
    )

    assert check.semantic_analysis is True
    assert check.semantic_repair is False
    assert repair.semantic_analysis is False
    assert repair.semantic_repair is True


@pytest.mark.parametrize(
    "payload",
    [
        {"mode": "repair", "semantic_analysis": True},
        {"mode": "check", "semantic_repair": True},
    ],
)
def test_semantic_capabilities_reject_the_wrong_mode(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        RunRequest.model_validate({"repo_path": ".", **payload})


@pytest.mark.parametrize(
    "payload",
    [
        {"max_patch_attempts_per_finding": 0},
        {"max_patch_attempts_per_finding": 3},
        {"max_model_calls_per_run": -1},
        {"max_input_tokens_per_run": -1},
        {"max_validation_commands_per_run": -1},
        {"timeout_seconds": -0.1},
        {"timeout_seconds": math.inf},
        {"timeout_seconds": -math.inf},
        {"timeout_seconds": math.nan},
    ],
)
def test_run_budgets_reject_invalid_limits(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RunBudgets.model_validate(payload)


def test_run_budgets_forbid_unknown_limits() -> None:
    with pytest.raises(ValidationError):
        RunBudgets.model_validate({"unknown_limit": 1})

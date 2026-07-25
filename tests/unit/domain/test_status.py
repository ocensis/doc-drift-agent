import pytest
from pydantic import ValidationError

from drift_agent.domain.enums import FindingDisposition, RunMode, RunStatus
from drift_agent.domain.models import RunRequest, ScopeSpec
from drift_agent.domain.status import aggregate_status


@pytest.mark.parametrize(
    ("mode", "dispositions", "expected"),
    [
        (RunMode.CHECK, [], RunStatus.CLEAN),
        (RunMode.CHECK, [FindingDisposition.DETECTED], RunStatus.DRIFT_FOUND),
        (RunMode.REPAIR, [], RunStatus.CLEAN),
        (RunMode.REPAIR, [FindingDisposition.FIXED], RunStatus.FIXED),
        (RunMode.REPAIR, [FindingDisposition.UNRESOLVED], RunStatus.UNRESOLVED),
        (RunMode.REPAIR, [FindingDisposition.NEEDS_APPROVAL], RunStatus.NEEDS_APPROVAL),
        (
            RunMode.REPAIR,
            [FindingDisposition.FIXED, FindingDisposition.UNRESOLVED],
            RunStatus.PARTIAL,
        ),
        (
            RunMode.REPAIR,
            [FindingDisposition.FIXED, FindingDisposition.NEEDS_APPROVAL],
            RunStatus.PARTIAL,
        ),
        (
            RunMode.REPAIR,
            [FindingDisposition.NEEDS_APPROVAL, FindingDisposition.UNRESOLVED],
            RunStatus.UNRESOLVED,
        ),
    ],
)
def test_aggregate_status(
    mode: RunMode,
    dispositions: list[FindingDisposition],
    expected: RunStatus,
) -> None:
    assert aggregate_status(mode, dispositions) is expected


def test_stale_and_failed_override_finding_dispositions() -> None:
    assert (
        aggregate_status(
            RunMode.REPAIR,
            [FindingDisposition.FIXED],
            stale=True,
        )
        is RunStatus.STALE
    )
    assert (
        aggregate_status(
            RunMode.REPAIR,
            [FindingDisposition.FIXED],
            failed=True,
        )
        is RunStatus.FAILED
    )
    assert (
        aggregate_status(
            RunMode.REPAIR,
            [FindingDisposition.FIXED, FindingDisposition.UNRESOLVED],
            stale=True,
            failed=True,
        )
        is RunStatus.FAILED
    )


def test_request_rejects_unsupported_scope_and_budget_fields() -> None:
    with pytest.raises(ValidationError):
        ScopeSpec.model_validate({"kind": "changed", "base_revision": "HEAD~1"})
    with pytest.raises(ValidationError):
        RunRequest.model_validate(
            {
                "mode": "check",
                "repo_path": ".",
                "budgets": {"unknown_limit": 1},
            }
        )

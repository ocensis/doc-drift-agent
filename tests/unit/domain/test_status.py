import pytest
from pydantic import ValidationError

from drift_agent.domain.enums import FindingDisposition, RunMode, RunStatus
from drift_agent.domain.models import RunRequest, ScopeSpec
from drift_agent.domain.status import aggregate_status, is_advisory_reason


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


@pytest.mark.parametrize(
    "reason_code",
    [
        # Every spelling the detectors actually emit for "I could not decide".
        "unsupported.literal",
        "unsupported.markdown_claim",
        "unsupported.symbol_kind",
        "unsupported.docstring_style",
        "unsupported.incomplete_declaration",
        "unsupported.ts_parse",
        "ambiguity.parameter",
        "ambiguity.symbol",
        "ambiguity.claim",
        "ambiguity.ts_module",
        "semantic.ambiguity.claim",
        "semantic.ambiguity.symbol",
        "semantic.claim_unsupported",
        "semantic.code_fact_unsupported",
        "semantic.code_fact_ambiguous",
    ],
)
def test_unverifiable_reason_codes_are_advisory(reason_code: str) -> None:
    assert is_advisory_reason(reason_code)


@pytest.mark.parametrize(
    "reason_code",
    [
        "detected",
        "validated",
        "precondition_changed",
        "omission.config_key",
        "semantic.alignment_missing",
        # An absent reason code says nothing about verifiability, so it must not
        # buy a finding its way out of blocking.
        "",
    ],
)
def test_defect_reason_codes_stay_blocking(reason_code: str) -> None:
    assert not is_advisory_reason(reason_code)


def test_advisory_classification_reads_tokens_rather_than_prefixes() -> None:
    # `semantic.claim_unsupported` shares no prefix with `unsupported.literal`,
    # and a future `foo.bar_ambiguous` should not need a new rule either.
    assert is_advisory_reason("foo.bar_ambiguous")
    assert is_advisory_reason("UNSUPPORTED.Literal")
    # A word that merely contains a token is a different claim about the code.
    assert not is_advisory_reason("unsupportedly_changed")


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

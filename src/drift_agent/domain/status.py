import re
from collections.abc import Sequence

from drift_agent.domain.enums import FindingDisposition, RunMode, RunStatus

# A reason code either describes the *documentation* ("this claim contradicts the
# code") or the *detector* ("I could not establish a deterministic anchor for this
# claim").  Only the first kind is a defect in the repository under test.  The
# second kind is a coverage limit of this tool, and blocking a merge on it makes
# the gate unadoptable: `unsupported.symbol_kind`, for one, fires on every public
# `@property` in a codebase and no amount of documentation makes it go away.
_UNVERIFIABLE_TOKENS = frozenset({"unsupported", "ambiguity", "ambiguous"})
_REASON_TOKEN = re.compile(r"[^a-z0-9]+")


def is_advisory_reason(reason_code: str) -> bool:
    """Report whether a reason code means "not verified" rather than "wrong".

    The test is on tokens, not on a prefix, because the same distinction is spelled
    three ways across the detectors: ``unsupported.literal``, ``ambiguity.symbol``,
    and ``semantic.claim_unsupported`` all say the detector declined to decide.

    Args:
        reason_code (str): The finding's ``reason_code``; an empty value is blocking.

    Returns:
        bool: True when the finding records a limit of the detector.
    """

    tokens = set(_REASON_TOKEN.split(reason_code.lower()))
    return bool(tokens & _UNVERIFIABLE_TOKENS)


def aggregate_status(
    mode: RunMode,
    dispositions: Sequence[FindingDisposition],
    *,
    stale: bool = False,
    failed: bool = False,
) -> RunStatus:
    if failed:
        return RunStatus.FAILED
    if stale:
        return RunStatus.STALE
    if not dispositions:
        return RunStatus.CLEAN
    if mode is RunMode.CHECK:
        return RunStatus.DRIFT_FOUND
    unique = set(dispositions)
    if FindingDisposition.FIXED in unique:
        return RunStatus.FIXED if len(unique) == 1 else RunStatus.PARTIAL
    if FindingDisposition.UNRESOLVED in unique:
        return RunStatus.UNRESOLVED
    if FindingDisposition.NEEDS_APPROVAL in unique:
        return RunStatus.NEEDS_APPROVAL
    return RunStatus.UNRESOLVED


def exit_code_for_status(status: RunStatus) -> int:
    if status in {RunStatus.CLEAN, RunStatus.FIXED}:
        return 0
    if status in {
        RunStatus.DRIFT_FOUND,
        RunStatus.PARTIAL,
        RunStatus.NEEDS_APPROVAL,
        RunStatus.UNRESOLVED,
    }:
        return 1
    return 2

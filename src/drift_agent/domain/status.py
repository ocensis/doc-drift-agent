from collections.abc import Sequence

from drift_agent.domain.enums import FindingDisposition, RunMode, RunStatus


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

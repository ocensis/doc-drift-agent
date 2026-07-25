from enum import StrEnum


class RunMode(StrEnum):
    CHECK = "check"
    REPAIR = "repair"


class RunStatus(StrEnum):
    CLEAN = "clean"
    DRIFT_FOUND = "drift_found"
    FIXED = "fixed"
    PARTIAL = "partial"
    NEEDS_APPROVAL = "needs_approval"
    UNRESOLVED = "unresolved"
    STALE = "stale"
    FAILED = "failed"


class FindingDisposition(StrEnum):
    DETECTED = "detected"
    FIXED = "fixed"
    NEEDS_APPROVAL = "needs_approval"
    UNRESOLVED = "unresolved"


class ValidationStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    UNAVAILABLE = "unavailable"

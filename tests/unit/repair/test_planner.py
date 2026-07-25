from __future__ import annotations

import pytest

from drift_agent.domain.models import DriftFinding, EvidenceAnchor, PatchAttempt
from drift_agent.repair.planner import RepairPlanner


def _finding(
    finding_id: str,
    *,
    symbol: str | None = None,
    code_path: str = "src/demo/api.py",
    doc_path: str = "docs/api.md",
) -> DriftFinding:
    return DriftFinding(
        id=finding_id,
        symbol_id=symbol or f"demo.api.{finding_id}",
        code_evidence=EvidenceAnchor(
            path=code_path,
            line=1,
            source_hash=f"code-{finding_id}",
        ),
        doc_evidence=EvidenceAnchor(
            path=doc_path,
            line=1,
            source_hash=f"doc-{finding_id}",
        ),
        reason="drift",
        kind="parameter_default_changed",
        component_id="value",
        old_value="old",
        new_value="new",
        detector_id="structural.signature",
        detector_version="2",
        fingerprint=f"fingerprint-{finding_id}",
    )


def _attempt(
    attempt_id: str,
    finding_id: str,
    *,
    path: str = "docs/api.md",
    start: int = 0,
    end: int = 3,
    source_hash: str = "source-a",
    expected: str = "old",
    replacement: str = "new",
) -> PatchAttempt:
    return PatchAttempt(
        id=attempt_id,
        finding_ids=[finding_id],
        path=path,
        source_hash=source_hash,
        start_byte=start,
        end_byte=end,
        expected_text=expected,
        replacement_text=replacement,
        unified_diff=f"diff-{attempt_id}",
    )


def test_identical_anchor_and_replacement_coalesce_into_one_stable_group() -> None:
    findings = [_finding("finding-b"), _finding("finding-a")]
    attempts = [
        _attempt("attempt-b", "finding-b"),
        _attempt("attempt-a", "finding-a"),
    ]
    planner = RepairPlanner()

    first = planner.plan(findings, attempts)
    second = planner.plan(list(reversed(findings)), list(reversed(attempts)))

    assert len(first.groups) == 1
    assert first.groups[0].finding_ids == ["finding-a", "finding-b"]
    assert len(first.groups[0].attempts) == 1
    assert first.groups[0].attempts[0].finding_ids == ["finding-a", "finding-b"]
    assert first.groups[0].id == second.groups[0].id
    assert first.groups[0].attempts[0].group_id == first.groups[0].id


@pytest.mark.parametrize(
    ("left", "right", "reason"),
    [
        (
            _attempt("left", "left", start=0, end=5, expected="abcde"),
            _attempt("right", "right", start=4, end=8, expected="efgh"),
            "overlap",
        ),
        (
            _attempt("left", "left", replacement="first"),
            _attempt("right", "right", replacement="second"),
            "replacement",
        ),
        (
            _attempt("left", "left", start=0, end=3, source_hash="base-a"),
            _attempt(
                "right",
                "right",
                start=4,
                end=7,
                source_hash="base-b",
            ),
            "base",
        ),
    ],
)
def test_conflicting_groups_are_symmetric_and_never_merged(
    left: PatchAttempt,
    right: PatchAttempt,
    reason: str,
) -> None:
    plan = RepairPlanner().plan(
        [_finding("left"), _finding("right")],
        [left, right],
    )

    assert len(plan.groups) == 2
    assert {group.conflict_key for group in plan.groups} == {reason}


def test_same_span_with_incompatible_expected_text_has_specific_conflict() -> None:
    left = _attempt("left", "left", expected="old", replacement="same")
    right = _attempt("right", "right", expected="OLD", replacement="same")

    plan = RepairPlanner().plan(
        [_finding("left"), _finding("right")],
        [left, right],
    )

    assert len(plan.groups) == 2
    assert {group.conflict_key for group in plan.groups} == {"expected_text"}


def test_cross_file_validation_dependency_blocks_only_implicated_groups() -> None:
    first = _finding("first", code_path="src/demo/a.py", doc_path="docs/a.md")
    second = _finding("second", code_path="docs/a.md", doc_path="docs/b.md")
    independent = _finding(
        "independent",
        code_path="src/demo/c.py",
        doc_path="docs/c.md",
    )
    attempts = [
        _attempt("first", "first", path="docs/a.md"),
        _attempt("second", "second", path="docs/b.md"),
        _attempt("independent", "independent", path="docs/c.md"),
    ]

    plan = RepairPlanner().plan([first, second, independent], attempts)
    conflicts = {group.finding_ids[0]: group.conflict_key for group in plan.groups}

    assert conflicts == {
        "first": "validation_dependency",
        "second": "validation_dependency",
        "independent": "",
    }
    assert [group.path for group in plan.groups] == [
        "docs/a.md",
        "docs/b.md",
        "docs/c.md",
    ]

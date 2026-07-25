"""Scoring-rule tests for the shared eval harness (SC-531/532/533).

The harness lives outside the package (evals/field/_harness.py), so it is
loaded by file path. Only the deterministic scoring helpers are under test;
model behaviour stays in the field eval. `_score` and `_class_recall` are the
harness's `score` / `class_recall`, aliased so the scenario names are stable.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace


def _load_harness() -> SimpleNamespace:
    path = Path(__file__).resolve().parents[2] / "evals" / "field" / "_harness.py"
    spec = importlib.util.spec_from_file_location("_harness", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return SimpleNamespace(_score=module.score, _class_recall=module.class_recall)


RUNNER = _load_harness()


def test_window_boundary_hit_and_miss() -> None:
    # SC-531: distance 39 inside the 40-line window hits, 41 misses.
    items = [
        {"label": "a", "doc": "d.md", "line": 100, "class": "prose"},
        {"label": "b", "doc": "d.md", "line": 300, "class": "prose"},
    ]
    findings: list[dict[str, object]] = [
        {"doc": "d.md", "line": 139},
        {"doc": "d.md", "line": 341},
    ]
    hits, extras = RUNNER._score(findings, items, window=40)
    assert hits == {"a"}
    assert extras == 1


def test_single_finding_consumed_by_nearest_item_only() -> None:
    # SC-532: one finding inside two items' windows satisfies only the nearest.
    items = [
        {"label": "far", "doc": "d.md", "line": 11, "class": "diagram"},
        {"label": "near", "doc": "d.md", "line": 41, "class": "prose"},
    ]
    findings: list[dict[str, object]] = [{"doc": "d.md", "line": 40}]
    hits, extras = RUNNER._score(findings, items, window=40)
    assert hits == {"near"}
    assert extras == 0


def test_two_findings_match_one_to_one() -> None:
    # SC-532: with a finding per item, both hit and nothing is double-counted.
    items = [
        {"label": "far", "doc": "d.md", "line": 11, "class": "diagram"},
        {"label": "near", "doc": "d.md", "line": 41, "class": "prose"},
    ]
    findings: list[dict[str, object]] = [
        {"doc": "d.md", "line": 40},
        {"doc": "d.md", "line": 12},
    ]
    hits, extras = RUNNER._score(findings, items, window=40)
    assert hits == {"near", "far"}
    assert extras == 0


def test_doc_mismatch_never_matches() -> None:
    items = [{"label": "a", "doc": "d.md", "line": 10, "class": "prose"}]
    findings: list[dict[str, object]] = [{"doc": "other.md", "line": 10}]
    hits, extras = RUNNER._score(findings, items, window=40)
    assert hits == set()
    assert extras == 1


def test_section_span_beats_line_window() -> None:
    # SC-534: with spans known, a finding is credited only to the item whose
    # section actually contains it, even when a wide window would reach further.
    # Mirrors 06-tools-and-security.md, whose items at 11/41/61 all sit inside
    # one 40-line window of each other.
    items = [
        {"label": "flowchart", "doc": "d.md", "line": 11, "class": "diagram"},
        {"label": "refund", "doc": "d.md", "line": 41, "class": "prose"},
    ]
    spans = {"d.md": [(5, 20), (35, 50)]}
    findings: list[dict[str, object]] = [{"doc": "d.md", "line": 43}]
    hits, extras = RUNNER._score(findings, items, window=40, spans=spans)
    assert hits == {"refund"}
    assert extras == 0
    # Without spans the same finding is wrongly credited to the nearer item.
    window_hits, _ = RUNNER._score(findings, items, window=40)
    assert window_hits == {"refund"}
    # ...and a finding in the flowchart section stays with the flowchart item.
    hits, _ = RUNNER._score([{"doc": "d.md", "line": 12}], items, window=40, spans=spans)
    assert hits == {"flowchart"}


def test_finding_outside_every_span_is_an_extra() -> None:
    items = [{"label": "a", "doc": "d.md", "line": 41, "class": "prose"}]
    spans = {"d.md": [(35, 50)]}
    # 60 is within the 40-line window of 41 but outside the item's section.
    hits, extras = RUNNER._score([{"doc": "d.md", "line": 60}], items, window=40, spans=spans)
    assert hits == set()
    assert extras == 1


def test_window_still_applies_where_carving_found_no_section() -> None:
    items = [{"label": "a", "doc": "d.md", "line": 41, "class": "prose"}]
    hits, _ = RUNNER._score([{"doc": "d.md", "line": 50}], items, window=40, spans={"other.md": []})
    assert hits == {"a"}


def test_class_recall_breakdown() -> None:
    # SC-533: per-class recall denominators come from ground-truth class counts.
    classes = {"a": "prose", "b": "prose", "c": "diagram"}
    assert RUNNER._class_recall({"a", "c"}, classes) == {"diagram": "1/1", "prose": "1/2"}
    assert RUNNER._class_recall(set(), classes) == {"diagram": "0/1", "prose": "0/2"}

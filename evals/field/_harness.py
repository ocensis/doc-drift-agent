"""Shared eval harness for the FR-009 field benchmarks.

Every runner in this directory materializes the same frozen fixture, scores
findings against the same ground truth with the same one-to-one
section-containment matcher, and reports the same union/per-class shape. That
shared logic lives here so the runners stay thin and — critically — so the
tool under test and its competitive baseline are scored by *identical* code,
which is a hard requirement for the comparison to mean anything (see
docs/evals/eval-validity-checklist.md §6).

Run any runner directly (`python evals/field/<runner>.py ...`); each inserts
its own directory on sys.path so `from _harness import ...` resolves both when
run as a script and when loaded by the test suite.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from collections import Counter
from itertools import combinations
from pathlib import Path
from statistics import mean
from typing import Any

from drift_agent.providers.section_claims import SectionClaimProvider


# --------------------------------------------------------------------------- #
# Target selection: a frozen fixture (stable, comparable) or a live repo.      #
# --------------------------------------------------------------------------- #
def materialize_fixture(fixture_dir: Path) -> tuple[Path, str]:
    """Clone the fixture bundle into a throwaway temp worktree; return (repo, baseline).

    The scan target is always a fresh clone of snapshot.bundle checked out at
    head_sha, never the live source repo, so results depend only on the frozen
    bundle and are reproducible regardless of the source repo's later state.
    """

    manifest = json.loads((fixture_dir / "manifest.json").read_text(encoding="utf-8"))
    target = Path(tempfile.mkdtemp(prefix="fr009-fixture-")) / "repo"
    subprocess.run(
        ["git", "clone", "--quiet", str(fixture_dir / "snapshot.bundle"), str(target)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "checkout", "--quiet", manifest["head_sha"]],
        cwd=target,
        check=True,
        capture_output=True,
    )
    return target, str(manifest["baseline_sha"])


def add_target_arguments(parser: argparse.ArgumentParser) -> None:
    """Register the standard --fixture / --repo / --baseline trio."""

    parser.add_argument("--fixture", type=Path, default=None)
    parser.add_argument("--repo", type=Path, default=None)
    parser.add_argument("--baseline", default=None)


def resolve_target(
    arguments: argparse.Namespace, parser: argparse.ArgumentParser
) -> tuple[Path, str]:
    """Return (repo, baseline) from --fixture, or from --repo + --baseline."""

    if arguments.fixture is not None:
        return materialize_fixture(arguments.fixture)
    if arguments.repo is not None and arguments.baseline is not None:
        return arguments.repo, arguments.baseline
    parser.error("provide --fixture, or both --repo and --baseline")
    raise AssertionError("unreachable")  # parser.error exits


# --------------------------------------------------------------------------- #
# Ground truth.                                                                #
# --------------------------------------------------------------------------- #
def load_ground_truth(path: Path) -> tuple[list[dict[str, Any]], int, dict[str, str]]:
    """Return (items, window_lines, {label: class})."""

    ground_truth = json.loads(path.read_text(encoding="utf-8"))
    items = ground_truth["items"]
    window = int(ground_truth.get("window_lines", 40))
    classes = {str(item["label"]): str(item.get("class", "prose")) for item in items}
    return items, window, classes


# --------------------------------------------------------------------------- #
# Scoring: one-to-one, section-containment matching.                           #
# --------------------------------------------------------------------------- #
def section_spans(repo: Path, docs: set[str]) -> dict[str, list[tuple[int, int]]]:
    """Carve the ground-truth documents into the same sections the detector sees."""

    spans: dict[str, list[tuple[int, int]]] = {}
    for claim in SectionClaimProvider().collect(repo, sorted(docs)):
        end_line = claim.line + claim.text.count("\n")
        spans.setdefault(claim.path, []).append((claim.line, end_line))
    return spans


def containing_span(
    spans: dict[str, list[tuple[int, int]]], doc: str, line: int
) -> tuple[int, int] | None:
    for start, end in spans.get(doc, []):
        if start <= line <= end:
            return (start, end)
    return None


def score(
    findings: list[dict[str, Any]],
    items: list[dict[str, Any]],
    window: int,
    spans: dict[str, list[tuple[int, int]]] | None = None,
) -> tuple[set[str], int]:
    """Return (hit labels, extras). One-to-one, nearest pair first.

    A fixed line window is too coarse where a document holds several
    ground-truth items close together (06-tools-and-security.md has items at
    lines 11/41/61, all within one 40-line window of each other), so a finding
    from one section could be credited to another whose section never resolved.
    When section spans are available the finding must fall inside the same
    carved section as the item; the window applies only where carving found no
    containing section. Unconsumed findings are extras.
    """

    pairs: list[tuple[int, int, int]] = []
    for item_index, item in enumerate(items):
        doc = str(item["doc"])
        span = containing_span(spans, doc, int(item["line"])) if spans else None
        for finding_index, finding in enumerate(findings):
            if finding["doc"] != doc:
                continue
            line = int(finding["line"])
            distance = abs(line - int(item["line"]))
            if span is not None:
                if span[0] <= line <= span[1]:
                    pairs.append((distance, item_index, finding_index))
            elif distance <= window:
                pairs.append((distance, item_index, finding_index))
    hits: set[str] = set()
    matched_items: set[int] = set()
    consumed: set[int] = set()
    for _, item_index, finding_index in sorted(pairs):
        if item_index in matched_items or finding_index in consumed:
            continue
        matched_items.add(item_index)
        consumed.add(finding_index)
        hits.add(str(items[item_index]["label"]))
    return hits, len(findings) - len(consumed)


def class_recall(hits: set[str], classes: dict[str, str]) -> dict[str, str]:
    """Per-class recall strings, denominators from ground-truth class counts."""

    totals = Counter(classes.values())
    counted = Counter(classes[label] for label in hits)
    return {cls: f"{counted[cls]}/{totals[cls]}" for cls in sorted(totals)}


# --------------------------------------------------------------------------- #
# Variance-aware reporting: union over repeats, with the spread of union@K.    #
# --------------------------------------------------------------------------- #
def union_over_repeats(hit_sets: list[set[str]], k: int) -> dict[str, Any]:
    """Union@k statistics across all k-subsets, exposing how much union@k itself swings.

    union@k is the primary metric, but it is not itself stable: the same five
    rounds can give a union@3 anywhere in a range. Reporting min/max/mean over
    subsets keeps a single lucky subset from being read as an improvement.
    """

    if len(hit_sets) < k:
        combined = set().union(*hit_sets) if hit_sets else set()
        return {"k": k, "n": len(hit_sets), "min": len(combined), "max": len(combined),
                "mean": float(len(combined))}
    sizes = [len(set().union(*subset)) for subset in combinations(hit_sets, k)]
    return {
        "k": k,
        "n": len(hit_sets),
        "min": min(sizes),
        "max": max(sizes),
        "mean": round(mean(sizes), 1),
    }


__all__ = [
    "add_target_arguments",
    "class_recall",
    "containing_span",
    "load_ground_truth",
    "materialize_fixture",
    "resolve_target",
    "score",
    "section_spans",
    "union_over_repeats",
]

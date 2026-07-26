"""Model-free ceiling metric for FR-009: is the proving code even shown to the model?

A recall round costs ~20 minutes of sequential model calls, which is far too slow
to iterate evidence selection against. Evidence coverage is the same question one
layer earlier and costs seconds: for each ground-truth item we know which changed
file positively demonstrates its drift, so we can ask whether _resolve_sections
put that file into the section's evidence at all. No model can flag a
contradiction it was never shown, so this number is the ceiling on recall — when
measured coverage and measured recall agree, the model is not the bottleneck.

Usage:
    python evals/field/fr009_evidence_coverage.py \
        --fixture path/to/fixture \
        --ground-truth docs/field-reports/.../eval-ground-truth.json \
        --expectations docs/field-reports/.../eval-evidence-expectations.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _harness as H

from drift_agent import application
from drift_agent.domain.enums import RunMode
from drift_agent.domain.models import RunBudgets, RunRequest, ScopeSpec


def _capture_pipeline(repo: Path, baseline: str) -> tuple[list[Any], list[Any]]:
    """Run check with semantic analysis but no model, spying on the section stages."""

    captured: dict[str, list[Any]] = {}
    real_collect = application.SectionClaimProvider.collect
    real_resolve = application._resolve_sections

    def spy_collect(self: Any, repo_path: Path, doc_paths: list[str]) -> list[Any]:
        claims = real_collect(self, repo_path, doc_paths)
        captured["claims"] = claims
        return claims

    def spy_resolve(repo_path: Path, section_claims: Any, facts: Any, **kwargs: Any) -> list[Any]:
        resolved = real_resolve(repo_path, section_claims, facts, **kwargs)
        captured["resolved"] = resolved
        return resolved

    application.SectionClaimProvider.collect = spy_collect  # type: ignore[method-assign]
    application._resolve_sections = spy_resolve  # type: ignore[assignment]
    try:
        application.run(
            RunRequest(
                mode=RunMode.CHECK,
                repo_path=repo,
                scope=ScopeSpec(kind="since", revision=baseline),
                semantic_analysis=True,
                # No model is configured for this probe, so the audit is skipped;
                # the budget only has to be wide enough not to truncate resolution.
                budgets=RunBudgets(
                    max_model_calls_per_run=0,
                    max_input_tokens_per_run=0,
                    timeout_seconds=1_800.0,
                ),
            )
        )
    finally:
        application.SectionClaimProvider.collect = real_collect  # type: ignore[method-assign]
        application._resolve_sections = real_resolve  # type: ignore[assignment]
    return captured.get("claims", []), captured.get("resolved", [])


def _containing_claim(claims: list[Any], doc: str, line: int) -> Any | None:
    for claim in claims:
        if claim.path != doc:
            continue
        if claim.line <= line < claim.line + claim.text.count("\n") + 1:
            return claim
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--ground-truth", type=Path, required=True)
    parser.add_argument("--expectations", type=Path, required=True)
    arguments = parser.parse_args()

    repo, baseline = H.materialize_fixture(arguments.fixture)
    claims, resolved = _capture_pipeline(repo, baseline)

    by_key = {(section.claim.path, section.claim.start_byte): section for section in resolved}
    ranks = {
        (section.claim.path, section.claim.start_byte): index
        for index, section in enumerate(resolved)
    }
    expected_code = json.loads(arguments.expectations.read_text(encoding="utf-8"))["expected_code"]
    items = json.loads(arguments.ground_truth.read_text(encoding="utf-8"))["items"]

    rows: list[dict[str, object]] = []
    covered = 0
    for item in items:
        label = str(item["label"])
        expected = expected_code.get(label, [])
        row: dict[str, object] = {"label": label, "class": item["class"], "expected": expected}
        claim = _containing_claim(claims, str(item["doc"]), int(item["line"]))
        if claim is None:
            row |= {"covered": False, "why": "no section carved"}
        else:
            section = by_key.get((claim.path, claim.start_byte))
            if section is None:
                row |= {"covered": False, "why": "section unresolved: no evidence at all"}
            else:
                paths = [evidence.path for evidence in section.evidence]
                hit = [path for path in expected if path in paths]
                row |= {
                    "evidence": paths,
                    "rank": ranks[(claim.path, claim.start_byte)],
                    "covered": bool(hit),
                    "why": f"has {hit}" if hit else "resolved, but no expected file in evidence",
                }
        covered += 1 if row["covered"] else 0
        rows.append(row)

    reach: dict[str, int] = {}
    for expected in expected_code.values():
        for path in expected:
            reach.setdefault(path, 0)
    for section in resolved:
        for evidence in section.evidence:
            if evidence.path in reach:
                reach[evidence.path] += 1

    print(
        json.dumps(
            {
                "evidence_coverage": f"{covered}/{len(items)}",
                "sections_resolved": len(resolved),
                "sections_carved": len(claims),
                "expected_file_reach": dict(sorted(reach.items(), key=lambda kv: kv[1])),
                "rows": rows,
            },
            ensure_ascii=False,
            indent=1,
        )
    )


if __name__ == "__main__":
    main()

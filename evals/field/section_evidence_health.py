"""Repo-agnostic health check on section evidence selection.

Recall benchmarks are tied to one repository and one ground truth. This probe
needs neither: it reports the shape of the evidence the pipeline builds, so a
change to evidence selection can be checked for repo-specific pathologies
before it is trusted as a general improvement.

The signal that matters is concentration. If one source file ends up as
evidence for most sections, ranking has stopped discriminating and has latched
onto an artefact of that repository (a large configuration module, a barrel
file), which will not transfer to the next one.

Usage:
    python evals/field/section_evidence_health.py --repo . --baseline <rev>
    python evals/field/section_evidence_health.py --fixture evals/datasets/field/react-refactor-v1
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _harness as H

from drift_agent import application
from drift_agent.domain.enums import RunMode
from drift_agent.domain.models import RunBudgets, RunRequest, ScopeSpec


def main() -> None:
    parser = argparse.ArgumentParser()
    H.add_target_arguments(parser)
    arguments = parser.parse_args()

    repo, baseline = H.resolve_target(arguments, parser)

    captured: dict[str, Any] = {}
    real_collect = application.SectionClaimProvider.collect
    real_resolve = application._resolve_sections

    def spy_collect(self: Any, repo_path: Path, doc_paths: list[str]) -> Any:
        claims = real_collect(self, repo_path, doc_paths)
        captured["claims"] = claims
        return claims

    def spy_resolve(repo_path: Path, section_claims: Any, facts: Any, **kwargs: Any) -> Any:
        resolved = real_resolve(repo_path, section_claims, facts, **kwargs)
        captured["resolved"] = resolved
        captured["changed_source"] = set(kwargs["changed_source"])
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

    claims = captured.get("claims", [])
    resolved = captured.get("resolved", [])
    changed = captured.get("changed_source", set())
    appearances: Counter[str] = Counter()
    with_changed_evidence = 0
    for section in resolved:
        paths = {evidence.path for evidence in section.evidence}
        appearances.update(paths)
        if paths & changed:
            with_changed_evidence += 1

    total = len(resolved) or 1
    top = appearances.most_common(5)
    print(
        json.dumps(
            {
                "sections_carved": len(claims),
                "sections_resolved": len(resolved),
                "changed_source_files": len(changed),
                # Share of resolved sections whose evidence includes the single
                # most-used file. Above roughly a third, ranking is being driven
                # by one artefact of this repository rather than by relevance.
                "top_file_share": round(top[0][1] / total, 3) if top else 0.0,
                "top_files": [
                    {"path": path, "sections": count, "share": round(count / total, 3)}
                    for path, count in top
                ],
                "distinct_evidence_files": len(appearances),
                "sections_with_changed_evidence": with_changed_evidence,
                "changed_evidence_share": round(with_changed_evidence / total, 3),
            },
            ensure_ascii=False,
            indent=1,
        )
    )


if __name__ == "__main__":
    main()

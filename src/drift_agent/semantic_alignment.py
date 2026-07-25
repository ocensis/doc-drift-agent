from __future__ import annotations

import hashlib
from dataclasses import dataclass

from pydantic import ValidationError

from drift_agent.domain.models import (
    Alignment,
    CodeFact,
    DocClaim,
    SemanticReturnAssertion,
)
from drift_agent.normalization import canonical_json_bytes


@dataclass(frozen=True, slots=True)
class SemanticAlignmentIssue:
    id: str
    symbol_id: str
    claim_ids: tuple[str, ...]
    reason_code: str
    summary: str


@dataclass(frozen=True, slots=True)
class SemanticAlignmentResult:
    alignments: tuple[Alignment, ...]
    issues: tuple[SemanticAlignmentIssue, ...]


def _issue(
    claims: list[DocClaim],
    *,
    reason_code: str,
    summary: str,
) -> SemanticAlignmentIssue:
    symbol_id = claims[0].symbol_id
    claim_ids = tuple(sorted(claim.id for claim in claims))
    digest = hashlib.sha256(
        canonical_json_bytes(
            {
                "schema": "semantic-alignment-issue-v1",
                "symbol_id": symbol_id,
                "claim_ids": claim_ids,
                "reason_code": reason_code,
            }
        )
    ).hexdigest()
    return SemanticAlignmentIssue(
        id=f"semantic_issue_{digest}",
        symbol_id=symbol_id,
        claim_ids=claim_ids,
        reason_code=reason_code,
        summary=summary,
    )


def align_semantic_returns(
    facts: list[CodeFact],
    claims: list[DocClaim],
) -> SemanticAlignmentResult:
    """Admit only unique exact-FQN constant-return inputs."""

    facts_by_symbol: dict[str, list[CodeFact]] = {}
    for fact in facts:
        facts_by_symbol.setdefault(fact.symbol_id, []).append(fact)
    claims_by_component: dict[tuple[str, str], list[DocClaim]] = {}
    for claim in claims:
        if claim.kind == "semantic_assertion":
            claims_by_component.setdefault((claim.symbol_id, claim.component_id), []).append(claim)

    alignments: list[Alignment] = []
    issues: list[SemanticAlignmentIssue] = []
    for (symbol_id, component_id), grouped_claims in sorted(claims_by_component.items()):
        ordered_claims = sorted(
            grouped_claims,
            key=lambda claim: (claim.anchor.path, claim.anchor.start_byte, claim.id),
        )
        if len(ordered_claims) != 1:
            issues.append(
                _issue(
                    ordered_claims,
                    reason_code="semantic.ambiguity.claim",
                    summary=(
                        f"semantic alignment is ambiguous: {len(ordered_claims)} "
                        f"claims for {symbol_id}:{component_id}"
                    ),
                )
            )
            continue
        claim = ordered_claims[0]
        if claim.extraction_error is not None:
            issues.append(
                _issue(
                    [claim],
                    reason_code=claim.extraction_error,
                    summary=f"semantic claim is unsupported: {claim.signature}",
                )
            )
            continue
        try:
            SemanticReturnAssertion.model_validate(claim.normalized_value)
        except ValidationError:
            issues.append(
                _issue(
                    [claim],
                    reason_code="semantic.claim_unsupported",
                    summary=f"semantic claim has an invalid typed value: {claim.signature}",
                )
            )
            continue
        matching_facts = facts_by_symbol.get(symbol_id, [])
        if not matching_facts:
            issues.append(
                _issue(
                    [claim],
                    reason_code="semantic.alignment_missing",
                    summary=f"semantic claim has no exact current code fact: {symbol_id}",
                )
            )
            continue
        if len(matching_facts) != 1:
            issues.append(
                _issue(
                    [claim],
                    reason_code="semantic.ambiguity.symbol",
                    summary=(
                        f"semantic alignment has {len(matching_facts)} current facts: {symbol_id}"
                    ),
                )
            )
            continue
        fact = matching_facts[0]
        semantic_facts = [
            semantic_fact
            for semantic_fact in fact.semantic_facts
            if semantic_fact.component_id == component_id
        ]
        if not semantic_facts:
            issues.append(
                _issue(
                    [claim],
                    reason_code="semantic.code_fact_unsupported",
                    summary=(
                        "semantic claim requires one definite constant-return code fact: "
                        f"{symbol_id}"
                    ),
                )
            )
            continue
        if len(semantic_facts) != 1:
            issues.append(
                _issue(
                    [claim],
                    reason_code="semantic.code_fact_ambiguous",
                    summary=f"semantic code fact is ambiguous: {symbol_id}:{component_id}",
                )
            )
            continue
        alignments.append(Alignment(fact=fact, claim=claim))

    return SemanticAlignmentResult(
        alignments=tuple(
            sorted(
                alignments,
                key=lambda item: (
                    item.fact.symbol_id,
                    item.claim.anchor.path,
                    item.claim.anchor.start_byte,
                ),
            )
        ),
        issues=tuple(sorted(issues, key=lambda issue: issue.id)),
    )


__all__ = [
    "SemanticAlignmentIssue",
    "SemanticAlignmentResult",
    "align_semantic_returns",
]

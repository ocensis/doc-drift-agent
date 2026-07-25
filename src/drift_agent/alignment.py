from drift_agent.domain.models import Alignment, CodeFact, DocClaim


def align_exact(facts: list[CodeFact], claims: list[DocClaim]) -> list[Alignment]:
    facts_by_symbol = {fact.symbol_id: fact for fact in facts}
    fact_counts: dict[str, int] = {}
    for fact in facts:
        fact_counts[fact.symbol_id] = fact_counts.get(fact.symbol_id, 0) + 1
    claim_counts: dict[str, int] = {}
    for claim in claims:
        claim_counts[claim.symbol_id] = claim_counts.get(claim.symbol_id, 0) + 1
    return [
        Alignment(fact=facts_by_symbol[claim.symbol_id], claim=claim)
        for claim in claims
        if (
            claim.symbol_id in facts_by_symbol
            and fact_counts[claim.symbol_id] == 1
            and claim_counts[claim.symbol_id] == 1
        )
    ]


def ambiguous_exact_claims(
    facts: list[CodeFact],
    claims: list[DocClaim],
) -> dict[str, list[DocClaim]]:
    fact_symbols = {fact.symbol_id for fact in facts}
    claims_by_symbol: dict[str, list[DocClaim]] = {}
    for claim in claims:
        if claim.symbol_id in fact_symbols:
            claims_by_symbol.setdefault(claim.symbol_id, []).append(claim)
    return {
        symbol_id: grouped_claims
        for symbol_id, grouped_claims in sorted(claims_by_symbol.items())
        if len(grouped_claims) > 1
    }


def ambiguous_exact_facts(
    facts: list[CodeFact],
    claims: list[DocClaim],
) -> dict[str, list[CodeFact]]:
    claim_symbols = {claim.symbol_id for claim in claims}
    facts_by_symbol: dict[str, list[CodeFact]] = {}
    for fact in facts:
        if fact.symbol_id in claim_symbols:
            facts_by_symbol.setdefault(fact.symbol_id, []).append(fact)
    return {
        symbol_id: grouped_facts
        for symbol_id, grouped_facts in sorted(facts_by_symbol.items())
        if len(grouped_facts) > 1
    }

from drift_agent.alignment import (
    align_exact,
    ambiguous_exact_claims,
    ambiguous_exact_facts,
)
from drift_agent.domain.models import CodeFact, DocClaim, SourceAnchor


def test_align_exact_only_returns_unique_fqn_pairs() -> None:
    fact = CodeFact(
        symbol_id="click_demo.api.echo",
        path="src/click_demo/api.py",
        line=1,
        signature="echo(message: str) -> None",
        parameters=[],
        return_annotation="None",
        source_hash="sha256:code",
    )
    claim = DocClaim(
        id="claim_1",
        symbol_id="click_demo.api.echo",
        signature="echo(message: str) -> None",
        parameters=[],
        return_annotation="None",
        anchor=SourceAnchor(
            path="docs/api.md",
            line=1,
            start_byte=0,
            end_byte=1,
            exact_text="x",
            source_hash="sha256:doc",
        ),
    )

    alignments = align_exact([fact], [claim])

    assert len(alignments) == 1
    assert alignments[0].method == "exact_fqn"

    duplicate = claim.model_copy(update={"id": "claim_2"})
    assert align_exact([fact], [claim, duplicate]) == []
    ambiguities = ambiguous_exact_claims([fact], [claim, duplicate])
    assert list(ambiguities) == [fact.symbol_id]
    assert len(ambiguities[fact.symbol_id]) == 2

    duplicate_fact = fact.model_copy(update={"path": "other/api.py"})
    assert align_exact([fact, duplicate_fact], [claim]) == []
    fact_ambiguities = ambiguous_exact_facts([fact, duplicate_fact], [claim])
    assert len(fact_ambiguities[fact.symbol_id]) == 2

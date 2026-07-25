from drift_agent.domain.models import (
    CodeFact,
    DocClaim,
    SemanticCodeFact,
    SemanticScalar,
    SourceAnchor,
)
from drift_agent.semantic_alignment import align_semantic_returns


def _anchor(
    path: str,
    source_hash: str,
    *,
    start: int = 0,
    text: str = "x",
) -> SourceAnchor:
    return SourceAnchor(
        path=path,
        line=1,
        start_byte=start,
        end_byte=start + len(text.encode("utf-8")),
        exact_text=text,
        source_hash=source_hash,
    )


def _fact(
    *,
    symbol_id: str = "click_demo.api.flag",
    semantic_facts: list[SemanticCodeFact] | None = None,
    path: str = "src/click_demo/api.py",
) -> CodeFact:
    if semantic_facts is None:
        semantic_facts = [
            SemanticCodeFact(
                normalized_value=SemanticScalar(type="bool", value=False),
                anchor=_anchor(path, "sha256:code", text="False"),
            )
        ]
    return CodeFact(
        symbol_id=symbol_id,
        path=path,
        line=1,
        signature="flag() -> bool",
        parameters=[],
        return_annotation="bool",
        source_hash="sha256:code",
        semantic_facts=semantic_facts,
    )


def _claim(
    *,
    claim_id: str = "claim_semantic",
    symbol_id: str = "click_demo.api.flag",
    extraction_error: str | None = None,
    start: int = 10,
) -> DocClaim:
    sentence = "Returns `True`."
    return DocClaim(
        id=claim_id,
        symbol_id=symbol_id,
        kind="semantic_assertion",
        signature=sentence,
        extraction_error=extraction_error,
        anchor=_anchor("docs/api.md", "sha256:doc", start=start, text=sentence),
        component_id="return.literal",
        normalized_value=(
            None
            if extraction_error is not None
            else {
                "predicate": "return_literal",
                "mode": "direct",
                "value": {"type": "bool", "value": True},
            }
        ),
    )


def test_aligns_only_one_exact_fact_and_one_semantic_component_claim() -> None:
    fact = _fact()
    claim = _claim()
    structural_claim = DocClaim(
        id="claim_signature",
        symbol_id=fact.symbol_id,
        signature=fact.signature,
        anchor=_anchor("docs/api.md", "sha256:doc", text="signature"),
    )

    result = align_semantic_returns([fact], [structural_claim, claim])

    assert result.issues == ()
    assert len(result.alignments) == 1
    alignment = result.alignments[0]
    assert alignment.fact is fact
    assert alignment.claim is claim
    assert alignment.method == "exact_fqn"


def test_duplicate_claims_are_rejected_with_order_independent_stable_issue() -> None:
    first = _claim(claim_id="claim_first", start=10)
    second = _claim(claim_id="claim_second", start=40)

    forward = align_semantic_returns([_fact()], [first, second])
    reverse = align_semantic_returns([_fact()], [second, first])

    assert forward.alignments == ()
    assert len(forward.issues) == 1
    assert forward.issues[0].reason_code == "semantic.ambiguity.claim"
    assert forward.issues[0].claim_ids == ("claim_first", "claim_second")
    assert forward.issues[0].id == reverse.issues[0].id


def test_duplicate_and_missing_code_symbols_are_rejected_before_detection() -> None:
    claim = _claim()
    duplicate = _fact(path="src/click_demo/duplicate.py")

    ambiguous = align_semantic_returns([_fact(), duplicate], [claim])
    missing = align_semantic_returns([], [claim])

    assert ambiguous.alignments == ()
    assert [issue.reason_code for issue in ambiguous.issues] == ["semantic.ambiguity.symbol"]
    assert missing.alignments == ()
    assert [issue.reason_code for issue in missing.issues] == ["semantic.alignment_missing"]


def test_unsupported_claim_and_absent_behavior_fact_have_specific_issues() -> None:
    unsupported_claim = align_semantic_returns(
        [_fact()],
        [_claim(extraction_error="semantic.claim_unsupported")],
    )
    unsupported_code = align_semantic_returns(
        [_fact(semantic_facts=[])],
        [_claim()],
    )

    assert unsupported_claim.alignments == ()
    assert [issue.reason_code for issue in unsupported_claim.issues] == [
        "semantic.claim_unsupported"
    ]
    assert unsupported_code.alignments == ()
    assert [issue.reason_code for issue in unsupported_code.issues] == [
        "semantic.code_fact_unsupported"
    ]


def test_duplicate_behavior_facts_are_not_admitted_to_the_detector() -> None:
    semantic = SemanticCodeFact(
        normalized_value=SemanticScalar(type="bool", value=False),
        anchor=_anchor("src/click_demo/api.py", "sha256:code", text="False"),
    )

    result = align_semantic_returns(
        [_fact(semantic_facts=[semantic, semantic.model_copy()])],
        [_claim()],
    )

    assert result.alignments == ()
    assert [issue.reason_code for issue in result.issues] == ["semantic.code_fact_ambiguous"]


def test_invalid_typed_claim_is_rejected_before_detection() -> None:
    malformed = _claim().model_copy(update={"normalized_value": None})

    result = align_semantic_returns([_fact()], [malformed])

    assert result.alignments == ()
    assert [issue.reason_code for issue in result.issues] == ["semantic.claim_unsupported"]

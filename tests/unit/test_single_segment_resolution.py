from __future__ import annotations

from drift_agent.application import _resolve_single_segment_claims
from drift_agent.domain.models import CodeFact, DocClaim, SourceAnchor


def _fact(symbol_id: str, *, language: str = "typescript") -> CodeFact:
    return CodeFact(
        symbol_id=symbol_id,
        path="src/demo.ts" if language == "typescript" else "src/demo.py",
        line=1,
        signature=symbol_id,
        parameters=[],
        source_hash="hash",
        language=language,  # type: ignore[arg-type]
    )


def _claim(symbol_id: str, *, single_segment: bool) -> DocClaim:
    return DocClaim(
        id=f"claim-{symbol_id}",
        symbol_id=symbol_id,
        kind="symbol_reference",
        anchor=SourceAnchor(
            path="docs/api.md",
            line=1,
            start_byte=0,
            end_byte=1,
            exact_text=symbol_id,
            source_hash="doc",
        ),
        single_segment=single_segment,
    )


def test_unique_single_segment_reference_is_rewritten() -> None:
    resolved = _resolve_single_segment_claims(
        [_claim("UnifiedMessage", single_segment=True)],
        [_fact("contracts.message.UnifiedMessage")],
    )

    assert [claim.symbol_id for claim in resolved] == ["contracts.message.UnifiedMessage"]


def test_unmatched_and_ambiguous_single_segment_references_are_dropped() -> None:
    resolved = _resolve_single_segment_claims(
        [_claim("FAILED", single_segment=True), _claim("X", single_segment=True)],
        [_fact("a.X"), _fact("b.X")],
    )

    assert resolved == []


def test_without_typescript_facts_single_segment_claims_drop_and_rest_pass() -> None:
    dotted = _claim("pkg.mod.symbol", single_segment=False)

    resolved = _resolve_single_segment_claims(
        [dotted, _claim("Options", single_segment=True)],
        [_fact("pkg.mod.symbol", language="python")],
    )

    assert resolved == [dotted]


def test_exact_match_against_any_fact_is_never_rewritten() -> None:
    # A dotted claim addressed to a real Python symbol must not be hijacked by
    # a TypeScript fact whose id happens to end with the same dotted suffix.
    dotted = _claim("click_demo.api.echo", single_segment=False)

    resolved = _resolve_single_segment_claims(
        [dotted],
        [
            _fact("click_demo.api.echo", language="python"),
            _fact("legacy.click_demo.api.echo"),
        ],
    )

    assert resolved == [dotted]
    assert resolved[0].symbol_id == "click_demo.api.echo"


def test_multi_segment_typescript_reference_resolves_by_unique_suffix() -> None:
    resolved = _resolve_single_segment_claims(
        [_claim("Router.route", single_segment=False)],
        [_fact("adapters.router.Router.route")],
    )

    assert [claim.symbol_id for claim in resolved] == ["adapters.router.Router.route"]


def test_unresolved_multi_segment_reference_keeps_python_parity() -> None:
    stale = _claim("chat.postMessage", single_segment=False)

    resolved = _resolve_single_segment_claims([stale], [_fact("adapters.slack.send")])

    assert resolved == [stale]

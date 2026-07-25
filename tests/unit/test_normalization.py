from __future__ import annotations

import pytest

from drift_agent.normalization import normalize_expression, normalize_ts_type

# --- normalize_ts_type: whitespace around punctuation is irrelevant ---


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Promise<void>", "Promise< void >"),
        ("Record<string, number[]>", "Record<string,number[]>"),
        ("(x: number) => void", "(x:number)=>void"),
        ("() => void", "()=>void"),
        ("Map<string, Array<number>>", "Map< string , Array< number > >"),
        # union / intersection spacing collapses.
        ("string | number", "string|number"),
        ("A & B", "A&B"),
    ],
)
def test_ts_type_whitespace_around_punctuation_is_irrelevant(left: str, right: str) -> None:
    assert normalize_ts_type(left) == normalize_ts_type(right)


def test_ts_type_preserves_space_between_adjacent_word_tokens() -> None:
    # Collapsing this space would merge two identifiers into one token.
    assert normalize_ts_type("readonly string[]") != normalize_ts_type("readonlystring[]")
    assert normalize_ts_type("readonly string[]") == "readonly string[]"
    assert normalize_ts_type("keyof typeof obj") == "keyof typeof obj"


def test_ts_string_literal_types_are_preserved_verbatim() -> None:
    # Internal whitespace inside a string-literal type is significant.
    assert normalize_ts_type("'a b'") != normalize_ts_type("'a  b'")
    assert normalize_ts_type("'a b'") == "'a b'"
    assert normalize_ts_type('"a b"') == '"a b"'
    # A literal embedded in a generic keeps its contents untouched.
    assert normalize_ts_type("Uppercase<'a b'>") == "Uppercase<'a b'>"


def test_ts_template_literal_types_are_preserved() -> None:
    assert normalize_ts_type("`prefix-${string}`") == "`prefix-${string}`"
    # Internal whitespace inside a template-literal type is significant.
    assert normalize_ts_type("`${A} ${B}`") != normalize_ts_type("`${A}${B}`")
    assert normalize_ts_type("`${A} ${B}`") == "`${A} ${B}`"


@pytest.mark.parametrize(
    "expression",
    [
        "Promise<void>",
        "Record<string, number[]>",
        "(x: number) => void",
        "readonly string[]",
        "string | number",
        "'a b'",
        "`${A} ${B}`",
        "Map<string, Array<number>>",
    ],
)
def test_normalize_ts_type_is_idempotent(expression: str) -> None:
    once = normalize_ts_type(expression)
    assert normalize_ts_type(once) == once


# --- normalize_expression: semantic (not textual) Python comparison ---


def test_normalize_expression_ignores_insignificant_formatting() -> None:
    assert normalize_expression("1_000") == normalize_expression("1000")
    assert normalize_expression("list[str]") == normalize_expression("list [ str ]")


def test_normalize_expression_distinguishes_different_values() -> None:
    assert normalize_expression("1000") != normalize_expression("1001")

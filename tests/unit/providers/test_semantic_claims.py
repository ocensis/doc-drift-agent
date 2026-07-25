from pathlib import Path

import pytest

from drift_agent.providers.markdown_claims import MarkdownClaimProvider
from drift_agent.providers.semantic_claims import SemanticReturnClaimProvider


def _collect(tmp_path: Path, raw: bytes, *, name: str = "api.md"):
    path = tmp_path / name
    path.write_bytes(raw)
    declarations = MarkdownClaimProvider().collect(tmp_path, [name])
    claims = SemanticReturnClaimProvider().collect(tmp_path, [name], declarations)
    return path, claims


@pytest.mark.parametrize(
    ("sentence", "mode", "literal", "scalar_type", "value"),
    [
        ("Returns `None`.", "direct", "None", "none", None),
        ("Always returns `True`.", "always", "True", "bool", True),
        ("Returns `7`.", "direct", "7", "int", 7),
        ("Returns `-7`.", "direct", "-7", "int", -7),
        (
            "Returns `9223372036854775807`.",
            "direct",
            "9223372036854775807",
            "int",
            2**63 - 1,
        ),
        (
            "Returns `-9223372036854775808`.",
            "direct",
            "-9223372036854775808",
            "int",
            -(2**63),
        ),
        ("Returns `-0`.", "direct", "-0", "int", 0),
        ('Returns `"完成"`.', "direct", '"完成"', "str", "完成"),
    ],
)
def test_extracts_strict_return_assertions_with_exact_utf8_byte_anchors(
    tmp_path: Path,
    sentence: str,
    mode: str,
    literal: str,
    scalar_type: str,
    value: object,
) -> None:
    text = (
        "---\r\n"
        "drift_truth: code_derived\r\n"
        "---\r\n"
        "# 说明\r\n"
        "### `click_demo.api.flag`\r\n"
        "```python\r\n"
        "def flag() -> bool: ...\r\n"
        "```\r\n"
        f"{sentence}\r\n"
    )
    path, claims = _collect(tmp_path, text.encode("utf-8"))

    assert len(claims) == 1
    claim = claims[0]
    assert claim.symbol_id == "click_demo.api.flag"
    assert claim.kind == "semantic_assertion"
    assert claim.component_id == "return.literal"
    assert claim.explicit_truth == "code_derived"
    assert claim.extraction_error is None
    assert claim.normalized_value == {
        "predicate": "return_literal",
        "mode": mode,
        "value": {"type": scalar_type, "value": value},
    }
    raw = path.read_bytes()
    assert raw[claim.anchor.start_byte : claim.anchor.end_byte].decode("utf-8") == sentence
    assert claim.anchor.exact_text == sentence
    assert claim.value_anchor is not None
    assert (
        raw[claim.value_anchor.start_byte : claim.value_anchor.end_byte].decode("utf-8") == literal
    )
    assert claim.value_anchor.exact_text == literal
    assert claim.anchor.line == 9


@pytest.mark.parametrize(
    "document",
    [
        """\
### `click_demo.api.flag`
```python
def flag() -> bool: ...
```
always returns `True`.
""",
        """\
### `click_demo.api.flag`
```python
def flag() -> bool: ...
```
Always returns **`True`**.
""",
        """\
### `click_demo.api.flag`
```python
def flag() -> bool: ...
```
Always returns `True`!
""",
        """\
### `click_demo.api.flag`
```python
def flag() -> bool: ...
```
Always returns
`True`.
""",
        """\
### `click_demo.api.flag`
```python
def flag() -> bool: ...
```
Context first.

Always returns `True`.
""",
        """\
### `click_demo.api.flag`
```python
def flag() -> bool:
    return True
```
Always returns `True`.
""",
        """\
### `click_demo.api.flag`
```python
def flag() -> bool: ...
```
> Always returns `True`.
""",
    ],
)
def test_ignores_text_outside_the_exact_markdown_grammar(
    tmp_path: Path,
    document: str,
) -> None:
    _, claims = _collect(tmp_path, document.encode("utf-8"))

    assert claims == []


@pytest.mark.parametrize(
    "literal",
    [
        "1.5",
        "9223372036854775808",
        "-9223372036854775809",
        "+1",
        "- 1",
        "-(1)",
        "--1",
        "-True",
        "-1.5",
        "-(2**63)-1",
        "[1]",
        "SENTINEL",
        "b'x'",
        "1 # comment",
        "-1 # comment",
        '"a" "b"',
        "(1)",
        '"\\ud800"',
    ],
)
def test_recognized_but_unsupported_literal_is_an_explicit_claim_issue(
    tmp_path: Path,
    literal: str,
) -> None:
    document = f"""\
### `click_demo.api.flag`
```python
def flag(): ...
```
Returns `{literal}`.
"""
    _, claims = _collect(tmp_path, document.encode("utf-8"))

    assert len(claims) == 1
    claim = claims[0]
    assert claim.signature == f"Returns `{literal}`."
    assert claim.extraction_error == "semantic.claim_unsupported"
    assert claim.normalized_value is None
    assert claim.value_anchor is not None
    assert claim.value_anchor.exact_text == literal


def test_semantic_claim_requires_the_matching_complete_signature_declaration(
    tmp_path: Path,
) -> None:
    document = """\
### `click_demo.api.other`
```python
def flag(): ...
```
Returns `True`.
"""
    _, claims = _collect(tmp_path, document.encode("utf-8"))

    assert claims == []


def test_semantic_claim_rejects_declaration_from_an_older_document_read(
    tmp_path: Path,
) -> None:
    path = tmp_path / "api.md"
    path.write_text(
        "### `click_demo.api.flag`\n```python\ndef flag() -> bool: ...\n```\nReturns `True`.\n",
        encoding="utf-8",
    )
    declarations = MarkdownClaimProvider().collect(tmp_path, ["api.md"])
    path.write_text(
        "### `click_demo.api.flag`\n```python\ndef flag() -> bool: ...\n```\nReturns `False`.\n",
        encoding="utf-8",
    )

    claims = SemanticReturnClaimProvider().collect(
        tmp_path,
        ["api.md"],
        declarations,
    )

    assert claims == []

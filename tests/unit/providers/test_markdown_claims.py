from pathlib import Path

import pytest

from drift_agent.hashing import sha256_bytes
from drift_agent.providers.markdown_claims import MarkdownClaimProvider


def test_extracts_fqn_signature_and_exact_utf8_byte_anchor(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    path = docs / "api.md"
    path.write_text(
        """\
# 接口

### `click_demo.api.echo`

```python
def echo(message: str) -> None: ...
```
""",
        encoding="utf-8",
    )

    claims = MarkdownClaimProvider().collect(tmp_path, ["docs/api.md"])

    assert len(claims) == 1
    claim = claims[0]
    assert claim.symbol_id == "click_demo.api.echo"
    assert claim.signature == "echo(message: str) -> None"
    raw = path.read_bytes()
    assert raw[claim.anchor.start_byte : claim.anchor.end_byte].decode() == (
        "def echo(message: str) -> None: ..."
    )


def test_extracts_explicit_truth_frontmatter(tmp_path: Path) -> None:
    path = tmp_path / "api.md"
    path.write_text(
        """\
---
drift_truth: contract
---
### `click_demo.api.echo`
```python
def echo(message: str) -> None: ...
```
""",
        encoding="utf-8",
    )

    claim = MarkdownClaimProvider().collect(tmp_path, ["api.md"])[0]

    assert claim.explicit_truth == "contract"


def test_indented_frontmatter_opening_is_unknown(tmp_path: Path) -> None:
    path = tmp_path / "indented-opening.md"
    path.write_text(
        """\
 ---
drift_truth: code_derived
---
### `click_demo.api.echo`
```python
def echo(message: str) -> None: ...
```
""",
        encoding="utf-8",
    )

    claim = MarkdownClaimProvider().collect(tmp_path, ["indented-opening.md"])[0]

    assert claim.explicit_truth == "unknown"


def test_indented_frontmatter_closing_is_unknown(tmp_path: Path) -> None:
    path = tmp_path / "indented-closing.md"
    path.write_text(
        """\
---
drift_truth: code_derived
 ---
### `click_demo.api.echo`
```python
def echo(message: str) -> None: ...
```
""",
        encoding="utf-8",
    )

    claim = MarkdownClaimProvider().collect(tmp_path, ["indented-closing.md"])[0]

    assert claim.explicit_truth == "unknown"


def test_malformed_or_duplicate_truth_is_unknown(tmp_path: Path) -> None:
    documents = {
        "unclosed.md": """\
---
drift_truth: code_derived
### `click_demo.api.echo`
```python
def echo(message: str) -> None: ...
```
""",
        "duplicate.md": """\
---
drift_truth: code_derived
drift_truth: contract
---
### `click_demo.api.echo`
```python
def echo(message: str) -> None: ...
```
""",
    }
    for name, content in documents.items():
        (tmp_path / name).write_text(content, encoding="utf-8")
        claim = MarkdownClaimProvider().collect(tmp_path, [name])[0]

        assert claim.explicit_truth == "unknown"


def test_valid_truth_with_malformed_frontmatter_line_is_unknown(tmp_path: Path) -> None:
    path = tmp_path / "malformed.md"
    path.write_text(
        """\
---
drift_truth: code_derived
broken: [not closed
---
### `click_demo.api.echo`
```python
def echo(message: str) -> None: ...
```
""",
        encoding="utf-8",
    )

    claim = MarkdownClaimProvider().collect(tmp_path, ["malformed.md"])[0]

    assert claim.explicit_truth == "unknown"


def test_nested_truth_is_unknown(tmp_path: Path) -> None:
    path = tmp_path / "nested.md"
    path.write_text(
        """\
---
metadata:
  drift_truth: code_derived
---
### `click_demo.api.echo`
```python
def echo(message: str) -> None: ...
```
""",
        encoding="utf-8",
    )

    claim = MarkdownClaimProvider().collect(tmp_path, ["nested.md"])[0]

    assert claim.explicit_truth == "unknown"


def test_invalid_exact_fqn_stub_becomes_an_extraction_issue(tmp_path: Path) -> None:
    path = tmp_path / "invalid.md"
    path.write_text(
        """\
### `click_demo.api.echo`
```python
def echo(
```
""",
        encoding="utf-8",
    )

    claim = MarkdownClaimProvider().collect(tmp_path, ["invalid.md"])[0]

    assert claim.symbol_id == "click_demo.api.echo"
    assert claim.extraction_error is not None
    assert "SyntaxError" in claim.extraction_error


def test_real_function_body_is_not_treated_as_a_signature_stub(tmp_path: Path) -> None:
    path = tmp_path / "example.md"
    path.write_text(
        """\
### `click_demo.api.echo`
```python
def echo(message: str) -> None:
    print(message)
```
""",
        encoding="utf-8",
    )

    claim = MarkdownClaimProvider().collect(tmp_path, ["example.md"])[0]

    assert claim.extraction_error == "expected one undecorated ellipsis signature stub"


def test_blockquote_fence_is_an_extraction_issue(tmp_path: Path) -> None:
    path = tmp_path / "blockquote.md"
    path.write_text(
        """\
### `click_demo.api.echo`

> ```python
> def echo(message: str) -> None: ...
> ```
""",
        encoding="utf-8",
    )

    claim = MarkdownClaimProvider().collect(tmp_path, ["blockquote.md"])[0]

    assert claim.extraction_error is not None


def test_blockquote_anchor_excludes_the_closing_fence(tmp_path: Path) -> None:
    path = tmp_path / "blockquote.md"
    path.write_text(
        """\
### `click_demo.api.echo`

> ```python
> def echo(message: str) -> None: ...
> ```
""",
        encoding="utf-8",
    )

    claim = MarkdownClaimProvider().collect(tmp_path, ["blockquote.md"])[0]
    raw = path.read_bytes()

    assert raw[claim.anchor.start_byte : claim.anchor.end_byte] == (
        b"> def echo(message: str) -> None: ...\n"
    )


def test_unicode_line_separator_before_fence_does_not_shift_anchor(
    tmp_path: Path,
) -> None:
    path = tmp_path / "unicode-separator.md"
    path.write_text(
        """\
### `click_demo.api.echo`
context\u2028same CommonMark line
```python
def echo(message: str) -> None: ...
```
""",
        encoding="utf-8",
    )

    claim = MarkdownClaimProvider().collect(tmp_path, ["unicode-separator.md"])[0]
    raw = path.read_bytes()

    assert raw[claim.anchor.start_byte : claim.anchor.end_byte].decode("utf-8") == (
        "def echo(message: str) -> None: ..."
    )


def test_crlf_anchor_hashes_and_preserves_original_bytes(tmp_path: Path) -> None:
    path = tmp_path / "api.md"
    raw = (
        b"### `click_demo.api.echo`\r\n\r\n"
        b"```python\r\n"
        b"def echo(message: str) -> None: ...\r\n"
        b"```\r\n"
    )
    path.write_bytes(raw)

    claim = MarkdownClaimProvider().collect(tmp_path, ["api.md"])[0]

    assert claim.anchor.exact_text == "def echo(message: str) -> None: ..."
    assert raw[claim.anchor.start_byte : claim.anchor.end_byte] == (
        b"def echo(message: str) -> None: ..."
    )
    assert claim.anchor.source_hash == sha256_bytes(raw)


@pytest.mark.parametrize("symlink_kind", ["final", "component"])
def test_rejects_markdown_symlink_before_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    symlink_kind: str,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "api.md"
    target.write_text(
        "### `click_demo.api.echo`\n```python\ndef echo() -> None: ...\n```\n",
        encoding="utf-8",
    )
    if symlink_kind == "final":
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "api.md").symlink_to(target)
    else:
        (tmp_path / "docs").symlink_to(outside, target_is_directory=True)

    linked_path = tmp_path / "docs/api.md"
    real_read_bytes = Path.read_bytes

    def reject_linked_read(path: Path) -> bytes:
        if path == linked_path:
            raise AssertionError("symlink content was read")
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", reject_linked_read)

    with pytest.raises(RuntimeError, match="symlink"):
        MarkdownClaimProvider().collect(tmp_path, ["docs/api.md"])


@pytest.mark.parametrize(
    "content",
    [
        "# keep leading\ndef echo(message: str) -> None: ...\n",
        "def echo(message: str) -> None: ...\n# keep trailing\n",
        "def echo(message: str) -> None: ...  # keep inline\n",
    ],
)
def test_signature_stub_comments_are_non_repairable_trivia(
    tmp_path: Path,
    content: str,
) -> None:
    path = tmp_path / "api.md"
    path.write_text(
        f"### `click_demo.api.echo`\n```python\n{content}```\n",
        encoding="utf-8",
    )

    claim = MarkdownClaimProvider().collect(tmp_path, ["api.md"])[0]

    assert claim.extraction_error == "signature stub contains comments"


def test_signature_anchor_excludes_surrounding_whitespace(tmp_path: Path) -> None:
    path = tmp_path / "api.md"
    path.write_bytes(
        b"### `click_demo.api.echo`\r\n"
        b"```python\r\n"
        b"\r\n"
        b"def echo(message: str) -> None: ...\r\n"
        b"\r\n"
        b"```\r\n"
    )

    claim = MarkdownClaimProvider().collect(tmp_path, ["api.md"])[0]

    assert claim.anchor.exact_text == "def echo(message: str) -> None: ..."
    raw = path.read_bytes()
    assert raw[claim.anchor.start_byte : claim.anchor.end_byte] == (
        b"def echo(message: str) -> None: ..."
    )


def test_single_identifier_span_emits_single_segment_reference(tmp_path: Path) -> None:
    path = tmp_path / "usage.md"
    path.write_text("Configure retries with `Options` before dialing.\n", encoding="utf-8")

    claims = MarkdownClaimProvider().collect(tmp_path, ["usage.md"])

    assert len(claims) == 1
    claim = claims[0]
    assert claim.kind == "symbol_reference"
    assert claim.symbol_id == "Options"
    assert claim.single_segment is True
    assert claim.raw_value == "Options"
    assert claim.normalized_value == "Options"
    assert claim.value_anchor == claim.anchor
    raw = path.read_bytes()
    assert raw[claim.anchor.start_byte : claim.anchor.end_byte] == b"Options"
    assert claim.anchor.exact_text == "Options"
    assert claim.anchor.line == 1
    assert claim.anchor.source_hash == sha256_bytes(raw)


def test_dollar_identifier_span_is_a_single_segment_reference(tmp_path: Path) -> None:
    path = tmp_path / "usage.md"
    path.write_text("Inject `$scope` into the controller.\n", encoding="utf-8")

    claims = MarkdownClaimProvider().collect(tmp_path, ["usage.md"])

    assert [(claim.symbol_id, claim.single_segment) for claim in claims] == [("$scope", True)]


def test_dotted_reference_stays_multi_segment_next_to_single(tmp_path: Path) -> None:
    path = tmp_path / "usage.md"
    path.write_text("See `click_demo.api.echo` and `Options` together.\n", encoding="utf-8")

    claims = MarkdownClaimProvider().collect(tmp_path, ["usage.md"])

    assert [(claim.symbol_id, claim.single_segment) for claim in claims] == [
        ("click_demo.api.echo", False),
        ("Options", True),
    ]
    raw = path.read_bytes()
    for claim in claims:
        assert claim.kind == "symbol_reference"
        assert raw[claim.anchor.start_byte : claim.anchor.end_byte] == claim.symbol_id.encode()


def test_rejected_single_segment_pass_keeps_dotted_claims(tmp_path: Path) -> None:
    # ` flag ` is a parser code span whose raw form the anchor regex cannot
    # match; only the single-segment pass rejects, never the dotted one.
    path = tmp_path / "usage.md"
    path.write_text("See `click_demo.api.echo` near ` flag `.\n", encoding="utf-8")

    claims = MarkdownClaimProvider().collect(tmp_path, ["usage.md"])

    assert [(claim.symbol_id, claim.single_segment) for claim in claims] == [
        ("click_demo.api.echo", False)
    ]


@pytest.mark.parametrize(
    "span",
    ["kebab-case", "docs/api.md", "--verbose", "42", "x", "$", "pip install foo"],
)
def test_non_identifier_spans_produce_no_claims(tmp_path: Path, span: str) -> None:
    path = tmp_path / "usage.md"
    path.write_text(f"Run `{span}` first.\n", encoding="utf-8")

    assert MarkdownClaimProvider().collect(tmp_path, ["usage.md"]) == []


def test_single_segment_spans_inside_code_blocks_are_ignored(tmp_path: Path) -> None:
    path = tmp_path / "usage.md"
    path.write_text(
        "```md\nUse `Options` here.\n```\n\n    indented `Retry` block\n",
        encoding="utf-8",
    )

    assert MarkdownClaimProvider().collect(tmp_path, ["usage.md"]) == []


def test_single_identifier_heading_is_not_a_declaration(tmp_path: Path) -> None:
    path = tmp_path / "usage.md"
    path.write_text(
        "### `Options`\n```python\ndef Options() -> None: ...\n```\n",
        encoding="utf-8",
    )

    claims = MarkdownClaimProvider().collect(tmp_path, ["usage.md"])

    # Heading declaration mechanics still require a dotted FQN; the heading's
    # code span is only an ordinary inline reference.
    assert [(claim.kind, claim.symbol_id, claim.single_segment) for claim in claims] == [
        ("symbol_reference", "Options", True)
    ]
    assert all(claim.extraction_error is None for claim in claims)


@pytest.mark.parametrize("indent", [b" ", b"  ", b"   "])
def test_indented_commonmark_fence_maps_ast_span_to_raw_crlf_bytes(
    tmp_path: Path,
    indent: bytes,
) -> None:
    path = tmp_path / "api.md"
    raw = (
        b"### `click_demo.api.echo`\r\n"
        + indent
        + b"```python\r\n"
        + indent
        + b"def echo(message: str) -> None: ...\r\n"
        + indent
        + b"```\r\n"
    )
    path.write_bytes(raw)

    claim = MarkdownClaimProvider().collect(tmp_path, ["api.md"])[0]

    assert claim.extraction_error is None
    assert claim.anchor.exact_text == "def echo(message: str) -> None: ..."
    assert raw[claim.anchor.start_byte : claim.anchor.end_byte] == (
        b"def echo(message: str) -> None: ..."
    )

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Literal

from markdown_it import MarkdownIt
from markdown_it.token import Token

from drift_agent.domain.models import DocClaim, SemanticReturnAssertion, SourceAnchor
from drift_agent.domain.semantic import parse_semantic_scalar
from drift_agent.hashing import sha256_bytes
from drift_agent.path_safety import repository_path_without_symlinks

_RETURN_ASSERTION = re.compile(
    r"(?P<label>Returns|Always returns) "
    r"(?P<ticks>`+)(?P<literal>[^`\r\n]+)(?P=ticks)\.\Z"
)


def _line_offsets(raw: bytes) -> list[int]:
    offsets = [0]
    for line in raw.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    return offsets


def _line_anchor(
    path: str,
    raw: bytes,
    source_hash: str,
    start_line: int,
    end_line: int,
) -> SourceAnchor:
    offsets = _line_offsets(raw)
    start = offsets[start_line]
    end = offsets[end_line]
    return SourceAnchor(
        path=path,
        line=start_line + 1,
        start_byte=start,
        end_byte=end,
        exact_text=raw[start:end].decode("utf-8"),
        source_hash=source_hash,
    )


def _heading_symbol(inline: Token) -> str | None:
    content = inline.content.strip()
    children = inline.children or []
    if len(children) == 1 and children[0].type == "code_inline":
        content = children[0].content.strip()
    parts = content.split(".")
    if len(parts) < 2 or any(not part.isidentifier() for part in parts):
        return None
    return content


def _claim_id(path: str, symbol_id: str, start_byte: int, mode: str) -> str:
    seed = f"semantic-return-v1:{path}:{symbol_id}:{start_byte}:{mode}"
    return f"claim_{uuid.uuid5(uuid.NAMESPACE_URL, seed)}"


class SemanticReturnClaimProvider:
    """Extract an intentionally tiny exact-FQN constant-return assertion grammar."""

    id = "markdown.semantic_return"
    version = "1"

    def __init__(self) -> None:
        self.parser = MarkdownIt("commonmark")

    def collect(
        self,
        repo_path: Path,
        doc_paths: list[str],
        declarations: list[DocClaim],
    ) -> list[DocClaim]:
        declaration_index: dict[tuple[str, str, int, int], DocClaim] = {}
        for claim in declarations:
            anchor = claim.declaration_anchor
            if (
                claim.kind == "signature"
                and claim.complete_declaration
                and claim.extraction_error is None
                and anchor is not None
            ):
                declaration_index[
                    (anchor.path, claim.symbol_id, anchor.start_byte, anchor.end_byte)
                ] = claim

        claims: list[DocClaim] = []
        for relative_path in sorted(doc_paths):
            path = repository_path_without_symlinks(repo_path, relative_path)
            raw = path.read_bytes()
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as error:
                raise RuntimeError(
                    f"could not decode semantic Markdown candidate {relative_path}: {error}"
                ) from error
            source_hash = sha256_bytes(raw)
            tokens = self.parser.parse(text)
            for index, heading in enumerate(tokens):
                if (
                    heading.type != "heading_open"
                    or heading.level != 0
                    or heading.map is None
                    or index + 6 >= len(tokens)
                    or tokens[index + 1].type != "inline"
                    or tokens[index + 2].type != "heading_close"
                ):
                    continue
                symbol_id = _heading_symbol(tokens[index + 1])
                fence = tokens[index + 3]
                paragraph = tokens[index + 4]
                if (
                    symbol_id is None
                    or fence.type != "fence"
                    or fence.level != 0
                    or fence.map is None
                    or fence.info.split()[:1] != ["python"]
                    or paragraph.type != "paragraph_open"
                    or paragraph.level != 0
                    or paragraph.map is None
                    or paragraph.map[1] - paragraph.map[0] != 1
                    or tokens[index + 5].type != "inline"
                    or tokens[index + 6].type != "paragraph_close"
                ):
                    continue
                declaration_anchor = _line_anchor(
                    relative_path,
                    raw,
                    source_hash,
                    heading.map[0],
                    fence.map[1],
                )
                declaration = declaration_index.get(
                    (
                        relative_path,
                        symbol_id,
                        declaration_anchor.start_byte,
                        declaration_anchor.end_byte,
                    )
                )
                if declaration is None or declaration.anchor.source_hash != source_hash:
                    # The structural declaration and semantic sentence must
                    # come from one immutable document read.  A later snapshot
                    # check will classify a concurrent rewrite as stale.
                    continue
                paragraph_anchor = _line_anchor(
                    relative_path,
                    raw,
                    source_hash,
                    paragraph.map[0],
                    paragraph.map[1],
                )
                sentence = paragraph_anchor.exact_text
                if sentence.endswith("\r\n"):
                    sentence = sentence[:-2]
                elif sentence.endswith(("\n", "\r")):
                    sentence = sentence[:-1]
                match = _RETURN_ASSERTION.fullmatch(sentence)
                if match is None:
                    continue
                mode: Literal["direct", "always"] = (
                    "always" if match.group("label") == "Always returns" else "direct"
                )
                literal = match.group("literal")
                scalar = parse_semantic_scalar(literal)
                sentence_end = paragraph_anchor.start_byte + len(sentence.encode("utf-8"))
                claim_anchor = paragraph_anchor.model_copy(
                    update={"end_byte": sentence_end, "exact_text": sentence}
                )
                literal_start = paragraph_anchor.start_byte + len(
                    sentence[: match.start("literal")].encode("utf-8")
                )
                literal_end = literal_start + len(literal.encode("utf-8"))
                literal_anchor = SourceAnchor(
                    path=relative_path,
                    line=claim_anchor.line,
                    start_byte=literal_start,
                    end_byte=literal_end,
                    exact_text=literal,
                    source_hash=source_hash,
                )
                claims.append(
                    DocClaim(
                        id=_claim_id(relative_path, symbol_id, claim_anchor.start_byte, mode),
                        symbol_id=symbol_id,
                        kind="semantic_assertion",
                        signature=sentence,
                        explicit_truth=declaration.explicit_truth,
                        extraction_error=(
                            None if scalar is not None else "semantic.claim_unsupported"
                        ),
                        anchor=claim_anchor,
                        component_id="return.literal",
                        raw_value=sentence,
                        normalized_value=(
                            None
                            if scalar is None
                            else SemanticReturnAssertion(
                                mode=mode,
                                value=scalar,
                            ).model_dump(mode="json")
                        ),
                        value_anchor=literal_anchor,
                        declaration_anchor=declaration_anchor,
                        literal_anchor=literal_anchor,
                    )
                )
        return sorted(
            claims,
            key=lambda claim: (claim.anchor.path, claim.anchor.start_byte, claim.id),
        )


__all__ = ["SemanticReturnClaimProvider"]

import ast
import io
import re
import tokenize
import uuid
from pathlib import Path
from typing import Literal

from markdown_it import MarkdownIt
from markdown_it.token import Token

from drift_agent.domain.models import DocClaim, ParameterFact, SourceAnchor
from drift_agent.domain.signatures import render_signature
from drift_agent.hashing import sha256_bytes
from drift_agent.path_safety import repository_path_without_symlinks

_FRONTMATTER_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*\Z")
_FRONTMATTER_VALUE_INDICATORS = frozenset("-?:,[]{}#&*!|>'\"%@`")
_SOURCE_LINE_ENDING = re.compile(r"\r\n|\r|\n")
_INLINE_FQN_SPAN = re.compile(
    r"(?<!`)(?P<ticks>`+)(?P<symbol>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+)(?P=ticks)(?!`)"
)
# Single-segment references accept `$` (TypeScript identifiers) and require at
# least two characters so one-letter spans stay plain formatting.
_SINGLE_SEGMENT_IDENTIFIER = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]+")
_INLINE_IDENTIFIER_SPAN = re.compile(
    rf"(?<!`)(?P<ticks>`+)(?P<symbol>{_SINGLE_SEGMENT_IDENTIFIER.pattern})(?P=ticks)(?!`)"
)


def _is_dotted_reference(content: str) -> bool:
    parts = content.split(".")
    return len(parts) >= 2 and all(part.isidentifier() for part in parts)


def _annotation(node: ast.expr | None) -> str | None:
    return None if node is None else ast.unparse(node)


FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef


def _parameters(function: FunctionNode) -> list[ParameterFact]:
    result: list[ParameterFact] = []
    positional = [*function.args.posonlyargs, *function.args.args]
    positional_defaults: list[ast.expr | None] = [None] * (
        len(positional) - len(function.args.defaults)
    ) + list(function.args.defaults)
    positional_only_count = len(function.args.posonlyargs)
    for index, (argument, default) in enumerate(zip(positional, positional_defaults, strict=True)):
        result.append(
            ParameterFact(
                name=argument.arg,
                kind=(
                    "positional-only" if index < positional_only_count else "positional or keyword"
                ),
                annotation=_annotation(argument.annotation),
                default=None if default is None else ast.unparse(default),
                required=default is None,
                default_present=default is not None,
                annotation_present=argument.annotation is not None,
                position=len(result),
            )
        )
    if function.args.vararg is not None:
        result.append(
            ParameterFact(
                name=function.args.vararg.arg,
                kind="variadic positional",
                annotation=_annotation(function.args.vararg.annotation),
                default_present=False,
                annotation_present=function.args.vararg.annotation is not None,
                position=len(result),
            )
        )
    for argument, default in zip(
        function.args.kwonlyargs,
        function.args.kw_defaults,
        strict=True,
    ):
        result.append(
            ParameterFact(
                name=argument.arg,
                kind="keyword-only",
                annotation=_annotation(argument.annotation),
                default=None if default is None else ast.unparse(default),
                required=default is None,
                default_present=default is not None,
                annotation_present=argument.annotation is not None,
                position=len(result),
            )
        )
    if function.args.kwarg is not None:
        result.append(
            ParameterFact(
                name=function.args.kwarg.arg,
                kind="variadic keyword",
                annotation=_annotation(function.args.kwarg.annotation),
                default_present=False,
                annotation_present=function.args.kwarg.annotation is not None,
                position=len(result),
            )
        )
    return result


def _source_lines(text: str) -> list[str]:
    lines: list[str] = []
    start = 0
    for ending in _SOURCE_LINE_ENDING.finditer(text):
        lines.append(text[start : ending.end()])
        start = ending.end()
    if start < len(text):
        lines.append(text[start:])
    return lines


def _line_byte_offsets(text: str) -> list[int]:
    offsets = [0]
    total = 0
    for line in _source_lines(text):
        total += len(line.encode())
        offsets.append(total)
    return offsets


def _fence_marker_candidate(line: str) -> str:
    candidate = line.rstrip("\r\n")
    while True:
        candidate = candidate.lstrip(" \t")
        if not candidate.startswith(">"):
            return candidate.strip()
        candidate = candidate[1:]


def _content_anchor(path: str, text: str, token: Token) -> SourceAnchor:
    if token.map is None:
        raise ValueError("block token has no source map")
    encoded = text.encode()
    offsets = _line_byte_offsets(text)
    lines = _source_lines(text)
    content_line = token.map[0] + 1
    end_line = token.map[1]
    if end_line > content_line:
        possible_close = _fence_marker_candidate(lines[end_line - 1])
        marker = token.markup[0]
        if possible_close.startswith(token.markup) and not possible_close.strip(marker):
            end_line -= 1
    start = offsets[content_line]
    end = offsets[end_line]
    return SourceAnchor(
        path=path,
        line=token.map[0] + 2,
        start_byte=start,
        end_byte=end,
        exact_text=encoded[start:end].decode("utf-8"),
        source_hash=sha256_bytes(encoded),
    )


def _node_byte_offsets(
    raw_text: str,
    parsed_text: str,
    node: FunctionNode,
) -> tuple[int, int]:
    if (
        node.lineno is None
        or node.col_offset is None
        or node.end_lineno is None
        or node.end_col_offset is None
    ):
        raise ValueError("AST node has no exact source span")
    raw_lines = _source_lines(raw_text)
    parsed_lines = _source_lines(parsed_text)
    offsets = _line_byte_offsets(raw_text)

    def raw_offset(line_number: int, parsed_column: int) -> int:
        raw_body = raw_lines[line_number - 1].rstrip("\r\n")
        parsed_body = parsed_lines[line_number - 1].rstrip("\r\n")
        if not raw_body.endswith(parsed_body):
            raise ValueError("de-indented fence content does not map to raw Markdown")
        prefix = raw_body[: len(raw_body) - len(parsed_body)] if parsed_body else raw_body
        if prefix.strip(" \t"):
            raise ValueError("fence de-indentation removed non-whitespace bytes")
        return offsets[line_number - 1] + len(prefix.encode("utf-8")) + parsed_column

    start = raw_offset(node.lineno, node.col_offset)
    end = raw_offset(node.end_lineno, node.end_col_offset)
    return start, end


def _function_anchor(
    fence_anchor: SourceAnchor,
    parsed_text: str,
    function: FunctionNode,
) -> SourceAnchor:
    encoded = fence_anchor.exact_text.encode("utf-8")
    local_start, local_end = _node_byte_offsets(
        fence_anchor.exact_text,
        parsed_text,
        function,
    )
    return SourceAnchor(
        path=fence_anchor.path,
        line=fence_anchor.line + function.lineno - 1,
        start_byte=fence_anchor.start_byte + local_start,
        end_byte=fence_anchor.start_byte + local_end,
        exact_text=encoded[local_start:local_end].decode("utf-8"),
        source_hash=fence_anchor.source_hash,
    )


def _contains_comment(text: str) -> bool:
    return any(
        token.type == tokenize.COMMENT
        for token in tokenize.generate_tokens(io.StringIO(text).readline)
    )


def _frontmatter_truth(
    text: str,
) -> Literal["code_derived", "design", "contract", "unknown"] | None:
    lines = [line.rstrip("\r\n") for line in _source_lines(text)]
    if not lines:
        return None
    if lines[0] != "---":
        return "unknown" if lines[0].strip() == "---" else None
    closing: int | None = None
    for index, line in enumerate(lines[1:], start=1):
        if line == "---":
            closing = index
            break
        if line.strip() == "---":
            return "unknown"
    if closing is None:
        return "unknown"
    values: dict[str, str] = {}
    for line in lines[1:closing]:
        if not line or line.startswith("#"):
            continue
        if line[0].isspace():
            return "unknown"
        key, separator, value = line.partition(":")
        value = value.strip()
        if (
            not separator
            or _FRONTMATTER_KEY.fullmatch(key) is None
            or not value
            or value[0] in _FRONTMATTER_VALUE_INDICATORS
            or any(character in "\t[]{}" for character in value)
            or ": " in value
            or " #" in value
            or key in values
        ):
            return "unknown"
        values[key] = value
    truth = values.get("drift_truth")
    if truth is None:
        return None
    if truth == "code_derived":
        return "code_derived"
    if truth == "design":
        return "design"
    if truth == "contract":
        return "contract"
    return "unknown"


def _claim_id(relative_path: str, symbol_id: str, anchor: SourceAnchor) -> str:
    seed = f"{relative_path}:{symbol_id}:{anchor.start_byte}"
    return f"claim_{uuid.uuid5(uuid.NAMESPACE_URL, seed)}"


def _invalid_claim(
    relative_path: str,
    symbol_id: str,
    token: Token,
    anchor: SourceAnchor,
    truth: Literal["code_derived", "design", "contract", "unknown"] | None,
    error: str,
) -> DocClaim:
    return DocClaim(
        id=_claim_id(relative_path, symbol_id, anchor),
        symbol_id=symbol_id,
        signature=token.content.strip(),
        parameters=[],
        explicit_truth=truth,
        extraction_error=error,
        anchor=anchor,
    )


def _is_signature_stub(function: FunctionNode) -> bool:
    return (
        not function.decorator_list
        and len(function.body) == 1
        and isinstance(function.body[0], ast.Expr)
        and isinstance(function.body[0].value, ast.Constant)
        and function.body[0].value.value is Ellipsis
    )


class MarkdownClaimProvider:
    def __init__(self) -> None:
        self.parser = MarkdownIt("commonmark")

    @staticmethod
    def _heading_symbol(inline: Token) -> str | None:
        content = inline.content.strip()
        children = inline.children or []
        if len(children) == 1 and children[0].type == "code_inline":
            content = children[0].content.strip()
        parts = content.split(".")
        if len(parts) < 2 or any(not part.isidentifier() for part in parts):
            return None
        return content

    @staticmethod
    def _line_range_anchor(
        relative_path: str,
        text: str,
        start_line: int,
        end_line: int,
    ) -> SourceAnchor:
        offsets = _line_byte_offsets(text)
        encoded = text.encode("utf-8")
        start = offsets[start_line]
        end = offsets[end_line]
        return SourceAnchor(
            path=relative_path,
            line=start_line + 1,
            start_byte=start,
            end_byte=end,
            exact_text=encoded[start:end].decode("utf-8"),
            source_hash=sha256_bytes(encoded),
        )

    @staticmethod
    def _symbol_anchor(
        relative_path: str,
        text: str,
        symbol: str,
        block: Token,
    ) -> SourceAnchor:
        if block.map is None:
            raise ValueError("heading token has no source map")
        block_anchor = MarkdownClaimProvider._line_range_anchor(
            relative_path, text, block.map[0], block.map[1]
        )
        raw = block_anchor.exact_text.encode("utf-8")
        needle = symbol.encode("utf-8")
        local = raw.find(needle)
        if local < 0 or raw.find(needle, local + 1) >= 0:
            raise ValueError("exact heading symbol cannot be uniquely located")
        start = block_anchor.start_byte + local
        end = start + len(needle)
        return SourceAnchor(
            path=relative_path,
            line=block_anchor.line,
            start_byte=start,
            end_byte=end,
            exact_text=symbol,
            source_hash=block_anchor.source_hash,
        )

    @staticmethod
    def _inline_symbol_anchors(
        relative_path: str,
        text: str,
        inline: Token,
        *,
        single_segment: bool,
    ) -> list[tuple[str, SourceAnchor]]:
        """Map parser-recognized top-level code spans to exact source bytes.

        markdown-it child tokens do not expose source offsets.  We therefore
        match their ordered contents against exact code-span candidates inside
        the parent inline block.  Any mismatch is rejected rather than guessing
        an anchor.  Dotted and single-segment references run as separate
        passes so one pass rejecting never suppresses the other's claims.
        """

        if inline.level != 1 or inline.map is None:
            return []
        if single_segment:
            span_pattern = _INLINE_IDENTIFIER_SPAN
            child_symbols = [
                child.content
                for child in (inline.children or [])
                if child.type == "code_inline"
                and _SINGLE_SEGMENT_IDENTIFIER.fullmatch(child.content) is not None
            ]
        else:
            span_pattern = _INLINE_FQN_SPAN
            child_symbols = [
                child.content
                for child in (inline.children or [])
                if child.type == "code_inline" and _is_dotted_reference(child.content)
            ]
        if not child_symbols:
            return []
        block = MarkdownClaimProvider._line_range_anchor(
            relative_path,
            text,
            inline.map[0],
            inline.map[1],
        )
        candidates: list[tuple[str, int, int]] = []
        for match in span_pattern.finditer(block.exact_text):
            backslashes = 0
            cursor = match.start()
            while cursor > 0 and block.exact_text[cursor - 1] == "\\":
                backslashes += 1
                cursor -= 1
            if backslashes % 2:
                continue
            candidates.append(
                (
                    match.group("symbol"),
                    match.start("symbol"),
                    match.end("symbol"),
                )
            )
        if [symbol for symbol, _, _ in candidates] != child_symbols:
            return []
        encoded_block = block.exact_text.encode("utf-8")
        anchors: list[tuple[str, SourceAnchor]] = []
        for symbol, char_start, char_end in candidates:
            local_start = len(block.exact_text[:char_start].encode("utf-8"))
            local_end = len(block.exact_text[:char_end].encode("utf-8"))
            start = block.start_byte + local_start
            end = block.start_byte + local_end
            anchors.append(
                (
                    symbol,
                    SourceAnchor(
                        path=relative_path,
                        line=block.line + encoded_block[:local_start].count(b"\n"),
                        start_byte=start,
                        end_byte=end,
                        exact_text=symbol,
                        source_hash=block.source_hash,
                    ),
                )
            )
        return anchors

    def collect(self, repo_path: Path, doc_paths: list[str]) -> list[DocClaim]:
        claims: list[DocClaim] = []
        for relative_path in sorted(doc_paths):
            path = repository_path_without_symlinks(repo_path, relative_path)
            raw = path.read_bytes()
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as error:
                raise RuntimeError(
                    f"could not decode Markdown candidate {relative_path}: {error}"
                ) from error
            tokens = self.parser.parse(text)
            truth = _frontmatter_truth(text)
            consumed_headings: set[int] = set()
            protected_ranges: list[tuple[int, int]] = []
            for index, token in enumerate(tokens):
                if (
                    token.type != "heading_open"
                    or token.level != 0
                    or not token.markup.startswith("#")
                    or index + 2 >= len(tokens)
                    or tokens[index + 1].type != "inline"
                    or tokens[index + 2].type != "heading_close"
                ):
                    continue
                inline = tokens[index + 1]
                symbol = self._heading_symbol(inline)
                if symbol is None:
                    continue
                symbol_anchor = self._symbol_anchor(relative_path, text, symbol, token)
                next_index = index + 3
                next_token = tokens[next_index] if next_index < len(tokens) else None
                fence_token = (
                    next_token if next_token is not None and next_token.type == "fence" else None
                )
                # Preserve two Stage 1 parsing edge cases while still rejecting
                # ordinary intervening blocks: a container fence is surfaced
                # as an unsupported claim, and U+2028 remains part of the same
                # source line for byte-anchor compatibility.
                if (
                    fence_token is None
                    and next_token is not None
                    and next_token.type == "blockquote_open"
                    and next_index + 1 < len(tokens)
                    and tokens[next_index + 1].type == "fence"
                ):
                    fence_token = tokens[next_index + 1]
                if (
                    fence_token is None
                    and next_token is not None
                    and next_token.type == "paragraph_open"
                    and next_index + 3 < len(tokens)
                    and "\u2028" in tokens[next_index + 1].content
                    and tokens[next_index + 3].type == "fence"
                ):
                    fence_token = tokens[next_index + 3]
                if fence_token is not None and fence_token.info.split()[:1] == ["python"]:
                    consumed_headings.add(index)
                    fence = fence_token
                    fence_anchor = _content_anchor(relative_path, text, fence)
                    if token.map is None or fence.map is None:
                        raise ValueError("declaration token has no source map")
                    declaration_anchor = self._line_range_anchor(
                        relative_path,
                        text,
                        token.map[0],
                        fence.map[1],
                    )
                    protected_ranges.append(
                        (declaration_anchor.start_byte, declaration_anchor.end_byte)
                    )
                    parsed_content = fence.content
                    if fence.level > 0:
                        invalid = _invalid_claim(
                            relative_path,
                            symbol,
                            fence,
                            fence_anchor,
                            truth,
                            "container-prefixed fence cannot be safely replaced",
                        )
                        claims.append(
                            invalid.model_copy(
                                update={
                                    "declaration_anchor": declaration_anchor,
                                    "complete_declaration": False,
                                }
                            )
                        )
                        continue
                    try:
                        module = ast.parse(parsed_content)
                    except SyntaxError as error:
                        invalid = _invalid_claim(
                            relative_path,
                            symbol,
                            fence,
                            fence_anchor,
                            truth,
                            f"SyntaxError: {error.msg}",
                        )
                        claims.append(
                            invalid.model_copy(update={"declaration_anchor": declaration_anchor})
                        )
                        continue
                    candidate = module.body[0] if len(module.body) == 1 else None
                    if not isinstance(
                        candidate, (ast.FunctionDef, ast.AsyncFunctionDef)
                    ) or not _is_signature_stub(candidate):
                        invalid = _invalid_claim(
                            relative_path,
                            symbol,
                            fence,
                            fence_anchor,
                            truth,
                            "expected one undecorated ellipsis signature stub",
                        )
                        claims.append(
                            invalid.model_copy(update={"declaration_anchor": declaration_anchor})
                        )
                        continue
                    function = candidate
                    if function.name != symbol.rsplit(".", 1)[-1]:
                        invalid = _invalid_claim(
                            relative_path,
                            symbol,
                            fence,
                            fence_anchor,
                            truth,
                            "signature stub name does not match exact FQN heading",
                        )
                        claims.append(
                            invalid.model_copy(update={"declaration_anchor": declaration_anchor})
                        )
                        continue
                    if _contains_comment(parsed_content):
                        invalid = _invalid_claim(
                            relative_path,
                            symbol,
                            fence,
                            fence_anchor,
                            truth,
                            "signature stub contains comments",
                        )
                        claims.append(
                            invalid.model_copy(update={"declaration_anchor": declaration_anchor})
                        )
                        continue
                    anchor = _function_anchor(fence_anchor, parsed_content, function)
                    parameters = _parameters(function)
                    returns = _annotation(function.returns)
                    claims.append(
                        DocClaim(
                            id=_claim_id(relative_path, symbol, anchor),
                            symbol_id=symbol,
                            signature=render_signature(function.name, parameters, returns),
                            parameters=parameters,
                            return_annotation=returns,
                            explicit_truth=truth,
                            anchor=anchor,
                            value_anchor=anchor,
                            declaration_anchor=declaration_anchor,
                            complete_declaration=True,
                        )
                    )
                else:
                    claims.append(
                        DocClaim(
                            id=_claim_id(relative_path, symbol, symbol_anchor),
                            symbol_id=symbol,
                            kind="symbol_reference",
                            explicit_truth=truth,
                            anchor=symbol_anchor,
                            value_anchor=symbol_anchor,
                            raw_value=symbol,
                            normalized_value=symbol,
                            complete_declaration=False,
                        )
                    )

            # Only parser-recognized, top-level inline-code tokens are valid
            # rename references. Fences, HTML, escaped text and container
            # contexts never reach this path.
            for inline in (token for token in tokens if token.type == "inline"):
                for single_segment in (False, True):
                    for symbol, anchor in self._inline_symbol_anchors(
                        relative_path,
                        text,
                        inline,
                        single_segment=single_segment,
                    ):
                        start = anchor.start_byte
                        if any(left <= start < right for left, right in protected_ranges):
                            continue
                        if any(
                            claim.kind == "symbol_reference" and claim.anchor.start_byte == start
                            for claim in claims
                        ):
                            continue
                        claims.append(
                            DocClaim(
                                id=_claim_id(relative_path, symbol, anchor),
                                symbol_id=symbol,
                                kind="symbol_reference",
                                explicit_truth=truth,
                                anchor=anchor,
                                value_anchor=anchor,
                                raw_value=symbol,
                                normalized_value=symbol,
                                single_segment=single_segment,
                            )
                        )
        return sorted(
            claims, key=lambda claim: (claim.anchor.path, claim.anchor.start_byte, claim.id)
        )

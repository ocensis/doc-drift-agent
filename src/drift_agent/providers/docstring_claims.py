from __future__ import annotations

import ast
import io
import re
import tokenize
import uuid
from dataclasses import dataclass
from pathlib import Path

from drift_agent.domain.models import CodeFact, DocClaim, ProviderIssue, SourceAnchor
from drift_agent.domain.values import MISSING_DOCSTRING_FIELD
from drift_agent.normalization import normalize_expression
from drift_agent.path_safety import repository_path_without_symlinks

_STRING_OPEN = re.compile(r"(?is)^(?P<prefix>[a-z]*)(?P<quote>'''|\"\"\"|'|\")")
_HEADER = re.compile(r"^(?P<indent>[ \t]*)(?P<name>Args|Returns):[ \t]*(?:\r\n|\r|\n)?$")
_ARG_FIELD = re.compile(
    r"^(?P<name>[A-Za-z_]\w*)(?:[ \t]+\((?P<annotation>.+)\))?[ \t]*:(?P<description>.*)$"
)


@dataclass(frozen=True)
class _Line:
    text: str
    start: int
    end: int
    line_number: int

    @property
    def body(self) -> str:
        return self.text.rstrip("\r\n")

    @property
    def indentation(self) -> int:
        return len(self.body) - len(self.body.lstrip(" \t"))


def _lines(text: str, base_offset: int, first_line: int) -> list[_Line]:
    result: list[_Line] = []
    char_cursor = 0
    byte_cursor = base_offset
    line_number = first_line
    for raw_line in text.splitlines(keepends=True):
        encoded = raw_line.encode("utf-8")
        result.append(_Line(raw_line, byte_cursor, byte_cursor + len(encoded), line_number))
        char_cursor += len(raw_line)
        byte_cursor += len(encoded)
        line_number += 1
    if char_cursor < len(text) or not result:
        tail = text[char_cursor:]
        result.append(
            _Line(tail, byte_cursor, byte_cursor + len(tail.encode("utf-8")), line_number)
        )
    return result


def _anchor(
    *,
    fact: CodeFact,
    source: bytes,
    start: int,
    end: int,
    line: int,
) -> SourceAnchor:
    return SourceAnchor(
        path=fact.path,
        line=line,
        start_byte=start,
        end_byte=end,
        exact_text=source[start:end].decode("utf-8"),
        source_hash=fact.source_hash,
    )


def _claim_id(fact: CodeFact, kind: str, component: str, start: int) -> str:
    seed = f"{fact.symbol_id}:{kind}:{component}:{fact.source_hash}:{start}"
    return f"claim_{uuid.uuid5(uuid.NAMESPACE_URL, seed)}"


def _field_colon(value: str) -> int | None:
    depth = 0
    quote: str | None = None
    escaped = False
    for index, character in enumerate(value):
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in {"'", '"'}:
            quote = character
        elif character in "([{":
            depth += 1
        elif character in ")]}" and depth:
            depth -= 1
        elif character == ":" and depth == 0:
            return index
    return None


class GoogleDocstringClaimProvider:
    def __init__(self) -> None:
        self.issues: list[ProviderIssue] = []

    def _literal_content(
        self,
        fact: CodeFact,
        source: bytes,
    ) -> tuple[str, int, int] | None:
        literal = fact.docstring_anchor
        if literal is None:
            return None
        token_text = literal.exact_text
        try:
            significant = [
                token
                for token in tokenize.generate_tokens(io.StringIO(token_text).readline)
                if token.type
                not in {
                    tokenize.ENCODING,
                    tokenize.NL,
                    tokenize.NEWLINE,
                    tokenize.ENDMARKER,
                    tokenize.INDENT,
                    tokenize.DEDENT,
                }
            ]
        except (tokenize.TokenError, IndentationError):
            significant = []
        match = _STRING_OPEN.match(token_text)
        if (
            len(significant) != 1
            or significant[0].type != tokenize.STRING
            or match is None
            or match.group("prefix").lower() not in {"", "u"}
        ):
            self.issues.append(
                ProviderIssue(
                    path=fact.path,
                    line=literal.line,
                    symbol_id=fact.symbol_id,
                    source_hash=fact.source_hash,
                    reason_code="unsupported.literal",
                    message="docstring is not one plain string literal",
                )
            )
            return None
        quote = match.group("quote")
        if not token_text.endswith(quote):
            return None
        content = token_text[match.end() : -len(quote)]
        try:
            value = ast.literal_eval(token_text)
        except (SyntaxError, ValueError):
            return None
        universal = content.replace("\r\n", "\n").replace("\r", "\n")
        if universal != value:
            self.issues.append(
                ProviderIssue(
                    path=fact.path,
                    line=literal.line,
                    symbol_id=fact.symbol_id,
                    source_hash=fact.source_hash,
                    reason_code="unsupported.literal",
                    message="escaped or transformed docstring content is not byte-addressable",
                )
            )
            return None
        content_start = literal.start_byte + len(token_text[: match.end()].encode("utf-8"))
        return content, content_start, literal.line

    def _missing_claims(
        self,
        fact: CodeFact,
        literal_anchor: SourceAnchor,
        present_parameters: set[str],
        has_return: bool,
    ) -> list[DocClaim]:
        parameters = list(fact.parameters)
        if fact.category == "method" and fact.decorator_kind != "staticmethod" and parameters:
            parameters = parameters[1:]
        claims: list[DocClaim] = []
        for parameter in parameters:
            if parameter.name in present_parameters:
                continue
            claims.append(
                DocClaim(
                    id=_claim_id(
                        fact,
                        "docstring_parameter",
                        parameter.name,
                        literal_anchor.start_byte,
                    ),
                    symbol_id=fact.symbol_id,
                    kind="docstring_parameter",
                    component_id=parameter.name,
                    raw_value=MISSING_DOCSTRING_FIELD,
                    normalized_value=MISSING_DOCSTRING_FIELD,
                    extraction_error="unsupported.literal",
                    anchor=literal_anchor,
                    literal_anchor=literal_anchor,
                )
            )
        if fact.return_annotation_present and not has_return:
            claims.append(
                DocClaim(
                    id=_claim_id(
                        fact,
                        "docstring_return",
                        "return",
                        literal_anchor.start_byte,
                    ),
                    symbol_id=fact.symbol_id,
                    kind="docstring_return",
                    component_id="return",
                    raw_value=MISSING_DOCSTRING_FIELD,
                    normalized_value=MISSING_DOCSTRING_FIELD,
                    extraction_error="unsupported.literal",
                    anchor=literal_anchor,
                    literal_anchor=literal_anchor,
                )
            )
        return claims

    def collect(self, repo_path: Path, facts: list[CodeFact]) -> list[DocClaim]:
        self.issues = []
        claims: list[DocClaim] = []
        source_cache: dict[str, bytes] = {}
        for fact in sorted(facts, key=lambda item: (item.path, item.line, item.symbol_id)):
            if fact.docstring_anchor is None:
                continue
            source = source_cache.get(fact.path)
            if source is None:
                path = repository_path_without_symlinks(repo_path, fact.path)
                source = path.read_bytes()
                source_cache[fact.path] = source
            resolved = self._literal_content(fact, source)
            if resolved is None:
                continue
            content, content_start, first_line = resolved
            literal_anchor = fact.docstring_anchor
            lines = _lines(content, content_start, first_line)
            headers: dict[str, list[int]] = {"Args": [], "Returns": []}
            for index, line in enumerate(lines):
                match = _HEADER.match(line.text)
                if match is not None:
                    headers[match.group("name")].append(index)
            ambiguous = [name for name, indexes in headers.items() if len(indexes) > 1]
            if ambiguous:
                self.issues.append(
                    ProviderIssue(
                        path=fact.path,
                        line=literal_anchor.line,
                        symbol_id=fact.symbol_id,
                        source_hash=fact.source_hash,
                        reason_code="unsupported.docstring_style",
                        message=f"duplicate Google sections: {ambiguous}",
                    )
                )
                continue
            if headers["Args"] and headers["Returns"]:
                args_indent = lines[headers["Args"][0]].indentation
                returns_indent = lines[headers["Returns"][0]].indentation
                if args_indent != returns_indent:
                    self.issues.append(
                        ProviderIssue(
                            path=fact.path,
                            line=literal_anchor.line,
                            symbol_id=fact.symbol_id,
                            source_hash=fact.source_hash,
                            reason_code="unsupported.docstring_style",
                            message="Google Args and Returns sections use different indentation",
                        )
                    )
                    continue

            present_parameters: set[str] = set()
            args_indexes = headers["Args"]
            if args_indexes:
                header_index = args_indexes[0]
                header_indent = lines[header_index].indentation
                section_end = len(lines)
                for index in range(header_index + 1, len(lines)):
                    line = lines[index]
                    if line.body.strip() and line.indentation <= header_indent:
                        section_end = index
                        break
                field_indexes = [
                    index
                    for index in range(header_index + 1, section_end)
                    if lines[index].body.strip()
                ]
                field_indent = min(
                    (lines[index].indentation for index in field_indexes),
                    default=header_indent + 4,
                )
                for position, index in enumerate(field_indexes):
                    line = lines[index]
                    if line.indentation != field_indent:
                        continue
                    stripped = line.body[field_indent:]
                    match = _ARG_FIELD.match(stripped)
                    if match is None:
                        self.issues.append(
                            ProviderIssue(
                                path=fact.path,
                                line=line.line_number,
                                symbol_id=fact.symbol_id,
                                source_hash=fact.source_hash,
                                reason_code="unsupported.docstring_style",
                                message="ambiguous Google Args field",
                            )
                        )
                        continue
                    name = match.group("name")
                    if name in present_parameters:
                        self.issues.append(
                            ProviderIssue(
                                path=fact.path,
                                line=line.line_number,
                                symbol_id=fact.symbol_id,
                                source_hash=fact.source_hash,
                                reason_code="ambiguity.parameter",
                                message=f"duplicate Args field {name}",
                            )
                        )
                        continue
                    present_parameters.add(name)
                    next_field = section_end
                    for candidate in field_indexes[position + 1 :]:
                        if lines[candidate].indentation == field_indent:
                            next_field = candidate
                            break
                    last_content = next_field - 1
                    while last_content > index and not lines[last_content].body.strip():
                        last_content -= 1
                    end = lines[last_content].end
                    field_anchor = _anchor(
                        fact=fact,
                        source=source,
                        start=line.start,
                        end=end,
                        line=line.line_number,
                    )
                    annotation = match.group("annotation")
                    value_anchor: SourceAnchor | None = None
                    normalized: str | None = None
                    if annotation is not None:
                        annotation_char = line.text.index(annotation)
                        annotation_start = line.start + len(
                            line.text[:annotation_char].encode("utf-8")
                        )
                        annotation_end = annotation_start + len(annotation.encode("utf-8"))
                        value_anchor = _anchor(
                            fact=fact,
                            source=source,
                            start=annotation_start,
                            end=annotation_end,
                            line=line.line_number,
                        )
                        try:
                            normalized = normalize_expression(annotation)
                        except SyntaxError:
                            self.issues.append(
                                ProviderIssue(
                                    path=fact.path,
                                    line=line.line_number,
                                    symbol_id=fact.symbol_id,
                                    source_hash=fact.source_hash,
                                    reason_code="unsupported.docstring_style",
                                    message="invalid Args annotation expression",
                                )
                            )
                            continue
                    claims.append(
                        DocClaim(
                            id=_claim_id(
                                fact,
                                "docstring_parameter",
                                name,
                                field_anchor.start_byte,
                            ),
                            symbol_id=fact.symbol_id,
                            kind="docstring_parameter",
                            component_id=name,
                            raw_value=annotation,
                            normalized_value=normalized,
                            anchor=field_anchor,
                            value_anchor=value_anchor,
                            literal_anchor=literal_anchor,
                            complete_declaration=True,
                        )
                    )

            has_return = False
            return_indexes = headers["Returns"]
            if return_indexes:
                header_index = return_indexes[0]
                header_indent = lines[header_index].indentation
                candidates: list[int] = []
                for index in range(header_index + 1, len(lines)):
                    line = lines[index]
                    if line.body.strip() and line.indentation <= header_indent:
                        break
                    if line.body.strip():
                        candidates.append(index)
                if candidates:
                    field_indent = min(lines[index].indentation for index in candidates)
                    fields = [
                        index for index in candidates if lines[index].indentation == field_indent
                    ]
                    if len(fields) != 1:
                        self.issues.append(
                            ProviderIssue(
                                path=fact.path,
                                line=lines[header_index].line_number,
                                symbol_id=fact.symbol_id,
                                source_hash=fact.source_hash,
                                reason_code="unsupported.docstring_style",
                                message="Google Returns section has multiple fields",
                            )
                        )
                    else:
                        index = fields[0]
                        line = lines[index]
                        stripped = line.body[field_indent:]
                        colon = _field_colon(stripped)
                        if colon is not None:
                            annotation = stripped[:colon].strip()
                            try:
                                normalized = normalize_expression(annotation)
                            except SyntaxError:
                                normalized = ""
                            if not normalized:
                                self.issues.append(
                                    ProviderIssue(
                                        path=fact.path,
                                        line=line.line_number,
                                        symbol_id=fact.symbol_id,
                                        source_hash=fact.source_hash,
                                        reason_code="unsupported.docstring_style",
                                        message="Google Returns annotation is invalid",
                                    )
                                )
                            else:
                                annotation_char = line.text.index(annotation)
                                start = line.start + len(
                                    line.text[:annotation_char].encode("utf-8")
                                )
                                end = start + len(annotation.encode("utf-8"))
                                value_anchor = _anchor(
                                    fact=fact,
                                    source=source,
                                    start=start,
                                    end=end,
                                    line=line.line_number,
                                )
                                field_end = lines[candidates[-1]].end
                                field_anchor = _anchor(
                                    fact=fact,
                                    source=source,
                                    start=line.start,
                                    end=field_end,
                                    line=line.line_number,
                                )
                                claims.append(
                                    DocClaim(
                                        id=_claim_id(
                                            fact,
                                            "docstring_return",
                                            "return",
                                            field_anchor.start_byte,
                                        ),
                                        symbol_id=fact.symbol_id,
                                        kind="docstring_return",
                                        component_id="return",
                                        raw_value=annotation,
                                        normalized_value=normalized,
                                        anchor=field_anchor,
                                        value_anchor=value_anchor,
                                        literal_anchor=literal_anchor,
                                        complete_declaration=True,
                                    )
                                )
                                has_return = True
                else:
                    self.issues.append(
                        ProviderIssue(
                            path=fact.path,
                            line=lines[header_index].line_number,
                            symbol_id=fact.symbol_id,
                            source_hash=fact.source_hash,
                            reason_code="unsupported.docstring_style",
                            message="Google Returns section has no field",
                        )
                    )
            # A present but unsupported Returns field is not a missing field.
            # It remains opaque while independent supported Args fields can be
            # repaired. A truly absent section is still surfaced below.
            has_return = has_return or bool(return_indexes)
            claims.extend(
                self._missing_claims(
                    fact,
                    literal_anchor,
                    present_parameters,
                    has_return,
                )
            )
        return sorted(
            claims,
            key=lambda claim: (claim.anchor.path, claim.anchor.start_byte, claim.id),
        )


DocstringClaimProvider = GoogleDocstringClaimProvider

__all__ = ["DocstringClaimProvider", "GoogleDocstringClaimProvider"]

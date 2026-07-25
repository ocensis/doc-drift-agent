from __future__ import annotations

import re
from pathlib import Path

from drift_agent.domain.models import ContractModel
from drift_agent.hashing import sha256_bytes
from drift_agent.path_safety import UnsafeInputPathError, repository_path_without_symlinks

_MAX_TOKENS_PER_SECTION = 40
_CODE_EXTENSIONS = (".ts", ".tsx", ".py", ".md")

# Section boundaries are ATX headings of level 1-3 at column zero; deeper
# headings stay inside the enclosing section so prose judgment sees them.
_HEADING = re.compile(r"^(?P<marks>#{1,3}) (?P<title>.*)$")
_FENCE_OPEN = re.compile(r"^ {0,3}(?P<marker>`{3,}|~{3,})")
_IDENTIFIER = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
# Single-line spans only: multi-line matching could pair an opening fence
# marker with its closing marker and swallow the code block between them.
_BACKTICK_SPAN = re.compile(r"(?<!`)(?P<ticks>`+)(?P<content>[^`\r\n]+)(?P=ticks)(?!`)")
_PATH_CHARS = re.compile(r"[A-Za-z0-9_$./-]+")
_BARE_SRC_PATH = re.compile(r"(?<![A-Za-z0-9_$./-])src/[A-Za-z0-9_$./-]+")


class SectionClaim(ContractModel):
    path: str
    heading: str
    line: int
    start_byte: int
    end_byte: int
    text: str
    tokens: list[str]
    source_hash: str


def _closes_fence(line: str, marker: str) -> bool:
    stripped = line.strip(" \t")
    return len(stripped) >= len(marker) and stripped == marker[0] * len(stripped)


def _is_candidate_anchor(content: str) -> bool:
    # Docs often write callables as `name()`; the call parens are not part of
    # the anchor token, so classification strips them first.
    content = content.removesuffix("()")
    if _IDENTIFIER.fullmatch(content) is not None:
        return len(content) >= 3
    parts = content.split(".")
    if len(parts) >= 2 and all(_IDENTIFIER.fullmatch(part) is not None for part in parts):
        return True
    return (
        "/" in content
        and content.endswith(_CODE_EXTENSIONS)
        and _PATH_CHARS.fullmatch(content) is not None
    )


def _section_tokens(text: str) -> list[str]:
    entries: list[tuple[int, str]] = []
    span_ranges: list[tuple[int, int]] = []
    for match in _BACKTICK_SPAN.finditer(text):
        span_ranges.append((match.start(), match.end()))
        content = match.group("content").strip()
        if _is_candidate_anchor(content):
            entries.append((match.start("content"), content))
    for match in _BARE_SRC_PATH.finditer(text):
        if any(left <= match.start() < right for left, right in span_ranges):
            continue
        token = match.group().rstrip("./")
        if "/" not in token:
            continue
        entries.append((match.start(), token))
    entries.sort(key=lambda entry: entry[0])
    tokens: list[str] = []
    seen: set[str] = set()
    for _, token in entries:
        if token in seen:
            continue
        seen.add(token)
        tokens.append(token)
        if len(tokens) == _MAX_TOKENS_PER_SECTION:
            break
    return tokens


def _doc_sections(relative_path: str, raw: bytes) -> list[SectionClaim]:
    source_hash = sha256_bytes(raw)
    claims: list[SectionClaim] = []
    open_heading: tuple[int, int, int, str] | None = None

    def _close(end: int) -> None:
        nonlocal open_heading
        if open_heading is None:
            return
        line, start, content_start, title = open_heading
        open_heading = None
        if not raw[content_start:end].decode("utf-8").strip():
            return
        section_text = raw[start:end].decode("utf-8")
        claims.append(
            SectionClaim(
                path=relative_path,
                heading=title,
                line=line,
                start_byte=start,
                end_byte=end,
                text=section_text,
                tokens=_section_tokens(section_text),
                source_hash=source_hash,
            )
        )

    offset = 0
    in_fence = False
    fence_marker = ""
    for index, line_bytes in enumerate(raw.splitlines(keepends=True)):
        line = line_bytes.decode("utf-8").rstrip("\r\n")
        if in_fence:
            if _closes_fence(line, fence_marker):
                in_fence = False
        else:
            fence = _FENCE_OPEN.match(line)
            heading = None if fence is not None else _HEADING.match(line)
            if fence is not None:
                in_fence = True
                fence_marker = fence.group("marker")
            elif heading is not None:
                _close(offset)
                open_heading = (
                    index + 1,
                    offset,
                    offset + len(line_bytes),
                    heading.group("title").strip(),
                )
        offset += len(line_bytes)
    _close(len(raw))
    return claims


class SectionClaimProvider:
    """Split Markdown docs into prose sections carrying candidate code anchors.

    Tokens are unresolved lexical candidates only; mapping them to current
    code symbols or files is the application layer's job.
    """

    id = "markdown.section"
    version = "1"

    def collect(self, repo_path: Path, doc_paths: list[str]) -> list[SectionClaim]:
        claims: list[SectionClaim] = []
        for relative_path in sorted(doc_paths):
            try:
                path = repository_path_without_symlinks(repo_path, relative_path)
                raw = path.read_bytes()
                raw.decode("utf-8")
            except (UnsafeInputPathError, OSError, UnicodeDecodeError):
                continue
            claims.extend(_doc_sections(relative_path, raw))
        return sorted(claims, key=lambda claim: (claim.path, claim.start_byte))


__all__ = ["SectionClaim", "SectionClaimProvider"]

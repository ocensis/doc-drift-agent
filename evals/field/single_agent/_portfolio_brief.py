"""Lazy, auditable briefing components for the single-Agent tool portfolio.

The experiment arms in this module deliberately expose one identical
model-visible tool. A frozen :class:`ProfileSpec` changes only the mechanical
content behind that tool. Components are constructed on the first tool call,
cached for replay, and never build another component as a side effect.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from _portfolio_generic import paged_generic_runtime
from _runner import (
    PORTFOLIO_SYSTEM_PROMPT,
    TOOL_PORTFOLIO_PROTOCOL_VERSION,
    AgentContext,
    AgentRuntime,
)

from drift_agent import application
from drift_agent.config import load_config
from drift_agent.languages import source_language
from drift_agent.patterns import matches_any
from drift_agent.providers.python_facts import PythonFactProvider
from drift_agent.providers.typescript_facts import TypeScriptFactProvider
from drift_agent.scope.git import GitChange, GitScopeResolver

PORTFOLIO_PROTOCOL_VERSION = TOOL_PORTFOLIO_PROTOCOL_VERSION
AUDIT_BRIEF_TOOL = "audit_brief"
AUDIT_BRIEF_TOOLS = (AUDIT_BRIEF_TOOL,)
PROFILE_BRIEF_TARGET_CHARS = 16_000

# The union arm uses the exact same independently rendered component strings.
# Fixed component budgets keep that union below the one-call target without
# changing either component according to which profile requested it.
_BRIEF_DIFF_CHAR_LIMIT = 9_000
_DOC_MAP_CHAR_LIMIT = 6_500
_CHANGE_SEED_CHAR_LIMIT = 15_000
_ALIGNMENT_MAP_CHAR_LIMIT = 15_000

ComponentId = Literal["brief_diff", "doc_map", "change_seed", "alignment_map"]

_COMPONENT_DEPENDENCIES: dict[ComponentId, tuple[str, ...]] = {
    "brief_diff": (
        "drift_agent.config.load_config",
        "drift_agent.patterns.matches_any",
        "git.diff.name_status",
        "git.diff.source_patch",
    ),
    "doc_map": (
        "drift_agent.config.load_config",
        "drift_agent.application._documents",
        "drift_agent.application._root_markdown_paths",
        "filesystem.markdown_read",
    ),
    "change_seed": (
        "drift_agent.config.load_config",
        "drift_agent.scope.git.GitScopeResolver.changes",
        "drift_agent.providers.PythonFactProvider.collect_bytes",
        "drift_agent.providers.TypeScriptFactProvider.collect_bytes",
        "git.show.baseline_and_head",
    ),
    "alignment_map": (
        "drift_agent.config.load_config",
        "drift_agent.scope.git.GitScopeResolver.changes",
        "drift_agent.providers.PythonFactProvider.collect_bytes",
        "drift_agent.providers.TypeScriptFactProvider.collect_bytes",
        "git.show.baseline_and_head",
        "drift_agent.application._documents",
        "drift_agent.application._root_markdown_paths",
        "filesystem.markdown_read",
    ),
}


@dataclass(frozen=True, slots=True)
class ProfileSpec:
    """Frozen declaration of the components and dependencies in one arm."""

    profile_id: str
    components: tuple[ComponentId, ...]
    dependencies: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.profile_id or not self.components:
            raise ValueError("a portfolio profile needs an id and at least one component")
        if len(set(self.components)) != len(self.components):
            raise ValueError("portfolio profile components must be unique")
        expected = _ordered_dependencies(self.components)
        if self.dependencies != expected:
            raise ValueError("portfolio profile dependencies do not match its components")


def _ordered_dependencies(components: tuple[ComponentId, ...]) -> tuple[str, ...]:
    ordered: list[str] = []
    for component in components:
        for dependency in _COMPONENT_DEPENDENCIES[component]:
            if dependency not in ordered:
                ordered.append(dependency)
    return tuple(ordered)


BRIEF_DIFF_PROFILE = ProfileSpec(
    profile_id="brief_diff",
    components=("brief_diff",),
    dependencies=_ordered_dependencies(("brief_diff",)),
)
DOC_MAP_PROFILE = ProfileSpec(
    profile_id="doc_map",
    components=("doc_map",),
    dependencies=_ordered_dependencies(("doc_map",)),
)
CHANGE_SEED_PROFILE = ProfileSpec(
    profile_id="change_seed",
    components=("change_seed",),
    dependencies=_ordered_dependencies(("change_seed",)),
)
ALIGNMENT_MAP_PROFILE = ProfileSpec(
    profile_id="alignment_map",
    components=("alignment_map",),
    dependencies=_ordered_dependencies(("alignment_map",)),
)
BRIEF_DIFF_DOC_MAP_PROFILE = ProfileSpec(
    profile_id="brief_diff_doc_map",
    components=("brief_diff", "doc_map"),
    dependencies=_ordered_dependencies(("brief_diff", "doc_map")),
)

PROFILE_SPECS = {
    profile.profile_id: profile
    for profile in (
        BRIEF_DIFF_PROFILE,
        DOC_MAP_PROFILE,
        CHANGE_SEED_PROFILE,
        ALIGNMENT_MAP_PROFILE,
        BRIEF_DIFF_DOC_MAP_PROFILE,
    )
}

# Import and reuse this object in every arm.  Besides making the bytes equal,
# this prevents profile-specific wording from coaching one treatment.
AUDIT_BRIEF_DEFINITION: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": AUDIT_BRIEF_TOOL,
        "description": (
            "Return the configured deterministic audit brief in one call. "
            "It contains mechanical read-only leads; verify them with the generic "
            "repository tools before submitting findings."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
}


@dataclass(frozen=True, slots=True)
class _ComponentResult:
    text: str
    metadata: dict[str, Any]


@dataclass(frozen=True, order=True, slots=True)
class _LiteralSignal:
    label: str
    term: str


@dataclass(frozen=True, slots=True)
class _Hunk:
    path: str
    header: str
    context: str
    lines: tuple[str, ...]


_HUNK_RE = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@(?P<context>.*)$"
)
_DECLARATION_RE = re.compile(
    r"^(?:(?:export|declare|default|public|private|protected|static|readonly|abstract)\s+)*"
    r"(?:(?:async)\s+)?(?:function|class|interface|type|enum|const|let|var|def)\b"
)
_SIGNATURE_RE = re.compile(
    r"^(?:(?:export|public|private|protected|static|readonly|abstract|async)\s+)*"
    r"[A-Za-z_$][\w$]*\s*(?:<[^;{}=]+>)?\s*\([^{};]*\)\s*(?::|\{|=>)"
)
_CONFIG_SIGNAL_RE = re.compile(
    r"(?:process\.env|os\.environ|\bgetenv\b|\bconfig(?:uration)?\b|\bpermission\b|"
    r"\bcapabilit(?:y|ies)\b|\bpolicy\b|\bregister(?:ed|s|ing)?\b|\btool(?:s)?\b|"
    r"\bmodel(?:s)?\b|\bprovider\b|\bstream(?:ing)?\b)",
    re.IGNORECASE,
)


def _sha256_json(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _git_text(context: AgentContext, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=context.repo_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or "git command failed"
        raise RuntimeError(message)
    return completed.stdout


def _git_bytes(context: AgentContext, revision: str, path: str) -> bytes | None:
    completed = subprocess.run(
        ["git", "show", f"{revision}:{path}"],
        cwd=context.repo_path,
        capture_output=True,
        check=False,
    )
    return completed.stdout if completed.returncode == 0 else None


def _status_paths(context: AgentContext, roots: list[str]) -> list[tuple[str, str, str | None]]:
    if not roots:
        return []
    output = subprocess.run(
        [
            "git",
            "diff",
            "--name-status",
            "-z",
            "--find-renames",
            context.baseline_revision,
            context.head_revision,
            "--",
            *roots,
        ],
        cwd=context.repo_path,
        capture_output=True,
        check=True,
    ).stdout
    fields = [item.decode("utf-8", errors="replace") for item in output.split(b"\0") if item]
    result: list[tuple[str, str, str | None]] = []
    index = 0
    while index < len(fields):
        raw_status = fields[index]
        index += 1
        if raw_status.startswith("R"):
            old_path, new_path = fields[index : index + 2]
            index += 2
            result.append(("R", new_path, old_path))
        else:
            path = fields[index]
            index += 1
            result.append((raw_status[:1], path, None))
    return result


def _diff_path(header: str) -> str:
    try:
        fields = shlex.split(header)
    except ValueError:
        return "unknown"
    if len(fields) < 4:
        return "unknown"
    candidate = fields[3]
    return candidate[2:] if candidate.startswith("b/") else candidate


def _parse_hunks(diff_text: str) -> list[_Hunk]:
    path = "unknown"
    current_header: str | None = None
    current_context = ""
    current_lines: list[str] = []
    hunks: list[_Hunk] = []

    def flush() -> None:
        nonlocal current_header, current_context, current_lines
        if current_header is not None:
            hunks.append(
                _Hunk(
                    path=path,
                    header=current_header,
                    context=current_context,
                    lines=tuple(current_lines),
                )
            )
        current_header = None
        current_context = ""
        current_lines = []

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            flush()
            path = _diff_path(line)
            continue
        match = _HUNK_RE.match(line)
        if match is not None:
            flush()
            current_header = line
            current_context = match.group("context").strip()
            continue
        if current_header is not None and not line.startswith(("--- ", "+++ ")):
            current_lines.append(line)
    flush()
    return hunks


def _is_selected_change(line: str) -> bool:
    if not line.startswith(("+", "-")) or line.startswith(("+++", "---")):
        return False
    content = line[1:].strip()
    return bool(
        content
        and (
            _DECLARATION_RE.search(content)
            or _SIGNATURE_RE.search(content)
            or _CONFIG_SIGNAL_RE.search(content)
        )
    )


def _selected_context_entries(hunks: list[_Hunk]) -> list[str]:
    prioritized: list[tuple[int, str]] = []
    seen: set[str] = set()
    for hunk in hunks:
        for index, line in enumerate(hunk.lines):
            if not _is_selected_change(line):
                continue
            first = max(0, index - 1)
            last = min(len(hunk.lines), index + 2)
            neighborhood = "\n".join(hunk.lines[first:last])
            entry = f"### {hunk.path} :: {hunk.header}\n```diff\n{neighborhood}\n```"
            if entry not in seen:
                seen.add(entry)
                content = line[1:].strip()
                priority = (
                    0 if _DECLARATION_RE.search(content) or _SIGNATURE_RE.search(content) else 1
                )
                prioritized.append((priority, entry))
    return [entry for _priority, entry in sorted(prioritized, key=lambda item: item[0])]


def _compact_hunk_entry(hunk: _Hunk) -> str:
    match = _HUNK_RE.match(hunk.header)
    if match is None:
        return f"- {hunk.path}: {hunk.header}"

    def span(start_name: str, count_name: str) -> str:
        start = int(match.group(start_name))
        raw_count = match.group(count_name)
        count = 1 if raw_count is None else int(raw_count)
        if count == 0:
            return f"L{start} (empty)"
        end = start + count - 1
        return f"L{start}" if end == start else f"L{start}-{end}"

    old_span = span("old_start", "old_count")
    new_span = span("new_start", "new_count")
    return f"- {hunk.path}: baseline {old_span} -> HEAD {new_span}"


def _fit_complete_entries(
    *,
    intro: list[str],
    sections: list[tuple[str, list[str]]],
    footer: list[str],
    char_limit: int,
) -> tuple[str, dict[str, dict[str, int]]]:
    """Fit complete line/chunk entries; never slice a logical entry."""

    rendered: list[str] = list(intro)
    counts: dict[str, dict[str, int]] = {}
    # Reserve room for the fixed footer and the final per-section accounting.
    # This keeps omission counts inside the model-visible response without
    # ever repairing an overrun by slicing a selected entry.
    reserved_chars = len("\n".join(footer)) + 800
    for title, entries in sections:
        rendered.extend(["", title])
        included = 0
        for entry in entries:
            candidate = "\n".join([*rendered, entry])
            if len(candidate) + reserved_chars <= char_limit:
                rendered.append(entry)
                included += 1
        counts[title] = {
            "total": len(entries),
            "included": included,
            "omitted": len(entries) - included,
        }
    rendered.extend(["", "## Selection accounting"])
    for title, count in counts.items():
        rendered.append(
            f"- {title.removeprefix('## ')}: included {count['included']}/"
            f"{count['total']}; omitted complete entries {count['omitted']}"
        )
    rendered.extend(["", *footer])
    text = "\n".join(rendered)
    if len(text) > char_limit:
        raise AssertionError("complete-entry selection exceeded its component limit")
    return text, counts


def _build_brief_diff(context: AgentContext) -> _ComponentResult:
    config = load_config(context.repo_path)
    project = config.project
    source_roots = list(project.source_roots)

    def included(path: str | None) -> bool:
        if path is None or source_language(path) is None:
            return False
        relative = PurePosixPath(path)
        return (
            any(relative.is_relative_to(PurePosixPath(root)) for root in source_roots)
            and matches_any(path, project.include)
            and not matches_any(path, project.exclude)
        )

    status_paths = [
        item
        for item in _status_paths(context, source_roots)
        if included(item[1]) or included(item[2])
    ]
    paths = sorted(
        {
            path
            for _status, current, old in status_paths
            for path in (current, old)
            if included(path)
        }
    )
    diff_text = (
        _git_text(
            context,
            "diff",
            "--no-ext-diff",
            "--no-color",
            "--unified=1",
            context.baseline_revision,
            context.head_revision,
            "--",
            *paths,
        )
        if paths
        else ""
    )
    hunks = _parse_hunks(diff_text)
    path_entries = [
        f"- {status}\t{path}" + (f" (from {old})" if old is not None else "")
        for status, path, old in status_paths
    ]
    hunk_entries = [_compact_hunk_entry(hunk) for hunk in hunks]
    evidence_entries = _selected_context_entries(hunks)
    intro = [
        "Compact source diff brief (baseline to frozen HEAD; mechanical, no interpretation).",
        "Dependencies: " + ", ".join(_COMPONENT_DEPENDENCIES["brief_diff"]),
        "Selection: every changed source path and hunk range, then complete neighborhoods "
        "around declaration/signature/config/env-related +/- lines.",
    ]
    footer = [
        "Full-detail continuation: call generic git_diff with a listed path. The compact "
        "brief is a logical-entry selection, not a character slice of the patch.",
        "Inherited product-internal truncation: none; build_seed is not called.",
    ]
    text, counts = _fit_complete_entries(
        intro=intro,
        sections=[
            ("## Changed source paths", path_entries),
            ("## Hunk map", hunk_entries),
            ("## Selected change neighborhoods", evidence_entries),
        ],
        footer=footer,
        char_limit=_BRIEF_DIFF_CHAR_LIMIT,
    )
    return _ComponentResult(
        text=text,
        metadata={
            "raw_diff_chars": len(diff_text),
            "raw_diff_lines": len(diff_text.splitlines()),
            "brief_chars": len(text),
            "char_limit": _BRIEF_DIFF_CHAR_LIMIT,
            "selection_unit": "complete_path_hunk_or_neighborhood_entry",
            "selection_rules": [
                "all_changed_source_paths",
                "all_hunk_headers_until_complete-entry_budget",
                "declaration_signature_config_env_changed_lines_with_one_line_context",
            ],
            "entry_counts": counts,
            "omitted_entries": sum(item["omitted"] for item in counts.values()),
            "hard_character_truncation": False,
            "full_detail_tool": "git_diff(path)",
        },
    )


def _headings(text: str) -> list[tuple[int, str]]:
    headings: list[tuple[int, str]] = []
    in_fence = False
    fence_marker = ""
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            if not in_fence:
                in_fence = True
                fence_marker = marker
            elif marker == fence_marker:
                in_fence = False
                fence_marker = ""
            continue
        if in_fence or not stripped.startswith("#"):
            continue
        level = len(stripped) - len(stripped.lstrip("#"))
        if 1 <= level <= 3 and stripped[level : level + 1] == " ":
            headings.append((number, stripped))
    return headings


def _round_robin_headings(
    documents: list[tuple[str, int, list[tuple[int, str]]]],
    *,
    start_ordinal: int = 0,
) -> list[str]:
    entries: list[str] = []
    max_headings = max((len(headings) for _path, _lines, headings in documents), default=0)
    for ordinal in range(start_ordinal, max_headings):
        for path, _line_count, headings in documents:
            if ordinal < len(headings):
                line, heading = headings[ordinal]
                entries.append(f"- {path}:L{line} {heading}")
    return entries


def _build_doc_map(context: AgentContext) -> _ComponentResult:
    config = load_config(context.repo_path)
    project = config.project
    doc_paths = sorted(
        set(
            application._documents(
                context.repo_path,
                project.docs_roots,
                project.include,
                project.exclude,
            )
        )
        | set(application._root_markdown_paths(context.repo_path, project))
    )
    documents: list[tuple[str, int, list[tuple[int, str]]]] = []
    for path in doc_paths:
        target = context.repo_path / path
        if not target.is_file():
            continue
        text = target.read_text(encoding="utf-8", errors="replace")
        documents.append((path, len(text.splitlines()), _headings(text)))
    inventory = []
    inline_headings = 0
    for path, line_count, headings in documents:
        first_heading = ""
        if headings:
            line, heading = headings[0]
            first_heading = f" | L{line} {heading}"
            inline_headings += 1
        inventory.append(f"- {path} ({line_count} lines){first_heading}")
    heading_entries = _round_robin_headings(documents, start_ordinal=1)
    headings_total = sum(len(headings) for _path, _lines, headings in documents)
    intro = [
        "In-scope documentation map (mechanical inventory, no change alignment).",
        "Dependencies: " + ", ".join(_COMPONENT_DEPENDENCIES["doc_map"]),
        "Heading selection is round-robin by heading ordinal across path-sorted documents "
        "so broad topic coverage precedes deep outlines; the first heading is inlined "
        "with each document inventory entry.",
    ]
    footer = [
        "Full-detail continuation: call generic read_file for any listed document. Omitted "
        "heading entries are counted below; no heading is partially sliced.",
        "Inherited product-internal truncation: none; seed, alignment, and fact providers "
        "are not called.",
    ]
    text, counts = _fit_complete_entries(
        intro=intro,
        sections=[
            ("## Document paths and line counts", inventory),
            ("## H1-H3 headings", heading_entries),
        ],
        footer=footer,
        char_limit=_DOC_MAP_CHAR_LIMIT,
    )
    return _ComponentResult(
        text=text,
        metadata={
            "documents": len(documents),
            "headings_total": headings_total,
            "headings_inlined_with_inventory": inline_headings,
            "brief_chars": len(text),
            "char_limit": _DOC_MAP_CHAR_LIMIT,
            "selection_unit": "complete_document_or_heading_entry",
            "heading_order": "ordinal_round_robin_then_path",
            "entry_counts": counts,
            "omitted_entries": sum(item["omitted"] for item in counts.values()),
            "hard_character_truncation": False,
            "full_detail_tool": "read_file(path, start, end)",
            "alignment_sites_collected": 0,
            "facts_collected": 0,
        },
    )


def _source_root(path: str, roots: list[str]) -> str | None:
    relative = PurePosixPath(path)
    candidates = [root for root in roots if relative.is_relative_to(PurePosixPath(root))]
    return max(candidates, key=len) if candidates else None


def _facts_for_bytes(
    *,
    repo_path: Path,
    roots: list[str],
    path: str,
    raw: bytes,
    version: str,
    python_provider: PythonFactProvider,
    typescript_provider: TypeScriptFactProvider,
) -> list[Any]:
    root = _source_root(path, roots)
    language = source_language(path)
    if root is None or language not in {"python", "typescript"}:
        return []
    provider = typescript_provider if language == "typescript" else python_provider
    return provider.collect_bytes(
        repo_path=repo_path,
        source_root=root,
        relative_path=path,
        raw=raw,
        source_version=version,
    )


def _env_keys(raw: bytes | None) -> set[str]:
    if raw is None:
        return set()
    keys: set[str] = set()
    for line in raw.decode("utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key and key.upper() == key:
            keys.add(key)
    return keys


def _change_entry(change: GitChange) -> str:
    rename = f" (from {change.old_path})" if change.old_path and change.new_path else ""
    return f"- {change.status}\t{change.path}{rename}"


def _build_change_seed(context: AgentContext) -> _ComponentResult:
    config = load_config(context.repo_path)
    project = config.project
    resolver = GitScopeResolver(
        context.repo_path,
        project,
        baseline_revision=context.baseline_revision,
        observed_head_revision=context.head_revision,
    )
    changes = resolver.changes()
    source_paths = sorted(
        {
            path
            for change in changes
            for path in (change.old_path, change.new_path)
            if path is not None and source_language(path) in {"python", "typescript"}
        }
    )
    python_provider = PythonFactProvider()
    typescript_provider = TypeScriptFactProvider()
    before_facts: list[Any] = []
    current_facts: list[Any] = []
    provider_errors: list[str] = []
    for path in source_paths:
        before = _git_bytes(context, context.baseline_revision, path)
        current = _git_bytes(context, context.head_revision, path)
        try:
            if before is not None:
                before_facts.extend(
                    _facts_for_bytes(
                        repo_path=context.repo_path,
                        roots=project.source_roots,
                        path=path,
                        raw=before,
                        version="baseline",
                        python_provider=python_provider,
                        typescript_provider=typescript_provider,
                    )
                )
            if current is not None:
                current_facts.extend(
                    _facts_for_bytes(
                        repo_path=context.repo_path,
                        roots=project.source_roots,
                        path=path,
                        raw=current,
                        version="head",
                        python_provider=python_provider,
                        typescript_provider=typescript_provider,
                    )
                )
        except (RuntimeError, SyntaxError, UnicodeError, ValueError) as error:
            provider_errors.append(f"{path}: {type(error).__name__}: {error}")

    before_by_id = {str(fact.symbol_id): fact for fact in before_facts}
    current_by_id = {str(fact.symbol_id): fact for fact in current_facts}
    before_ids = set(before_by_id)
    current_ids = set(current_by_id)
    deleted_entries = [
        f"- {symbol} :: {before_by_id[symbol].signature}"
        for symbol in sorted(before_ids - current_ids)
    ]
    added_entries = [
        f"- {symbol} :: {current_by_id[symbol].signature}"
        for symbol in sorted(current_ids - before_ids)
    ]
    changed_signature_entries = [
        f"- {symbol}: {before_by_id[symbol].signature} -> {current_by_id[symbol].signature}"
        for symbol in sorted(before_ids & current_ids)
        if before_by_id[symbol].signature != current_by_id[symbol].signature
    ]
    env_before = _env_keys(_git_bytes(context, context.baseline_revision, ".env.example"))
    env_current = _env_keys(_git_bytes(context, context.head_revision, ".env.example"))
    env_entries = [
        *[f"- added {key}" for key in sorted(env_current - env_before)],
        *[f"- removed {key}" for key in sorted(env_before - env_current)],
    ]
    added_docs = [
        f"- {change.path}"
        for change in changes
        if change.status == "A" and change.path.endswith(".md")
    ]
    intro = [
        "Compact deterministic change seed (mechanical export, no interpretation).",
        "Dependencies: " + ", ".join(_COMPONENT_DEPENDENCIES["change_seed"]),
        "Scope: changed-file status, symbol/signature delta from changed source files only, "
        ".env.example key delta, and newly added document paths.",
    ]
    footer = [
        "This component does not collect a full diff, document text, doc alignments, or "
        "repository-wide facts. Verify leads through generic repository tools.",
        "Inherited product-internal truncation: none. This implementation does not call "
        "build_seed, so its 200000-character diff, 60000-character added-doc, and "
        "100-symbol-list caps are not inherited. Any omissions above are whole logical "
        "entries counted in metadata, never partial strings.",
    ]
    sections = [
        ("## Changed files", [_change_entry(change) for change in changes]),
        ("## Deleted symbols", deleted_entries),
        ("## Added symbols", added_entries),
        ("## Changed signatures", changed_signature_entries),
        ("## Config key delta", env_entries),
        ("## Newly added docs", added_docs),
        ("## Fact-provider parse issues", [f"- {item}" for item in provider_errors]),
    ]
    text, counts = _fit_complete_entries(
        intro=intro,
        sections=sections,
        footer=footer,
        char_limit=_CHANGE_SEED_CHAR_LIMIT,
    )
    return _ComponentResult(
        text=text,
        metadata={
            "changed_files": len(changes),
            "changed_source_files": len(source_paths),
            "facts_baseline_changed": len(before_facts),
            "facts_head_changed": len(current_facts),
            "deleted_symbols": len(deleted_entries),
            "added_symbols": len(added_entries),
            "changed_signatures": len(changed_signature_entries),
            "env_added": len(env_current - env_before),
            "env_removed": len(env_before - env_current),
            "added_docs": len(added_docs),
            "provider_errors": provider_errors,
            "brief_chars": len(text),
            "char_limit": _CHANGE_SEED_CHAR_LIMIT,
            "selection_unit": "complete_change_symbol_config_or_error_entry",
            "entry_counts": counts,
            "omitted_entries": sum(item["omitted"] for item in counts.values()),
            "hard_character_truncation": False,
            "inherited_product_internal_truncation": {
                "build_seed_called": False,
                "diff_char_cap_inherited": False,
                "added_doc_char_cap_inherited": False,
                "symbol_list_cap_inherited": False,
            },
            "full_diff_collected": False,
            "documents_read": 0,
            "alignments_collected": 0,
        },
    )


def _fact_display_name(fact: Any) -> str:
    identity = getattr(fact, "symbol_identity", None)
    name = getattr(identity, "name", None)
    if isinstance(name, str) and name:
        return name
    return str(fact.symbol_id).rsplit(".", 1)[-1]


def _build_alignment_map(context: AgentContext) -> _ComponentResult:
    """Map exact changed-code/config/path literals to documentation lines.

    This is deliberately not a change brief: unmatched signals, signatures,
    document headings, and document text are not model-visible. The only leads
    returned are exact, case-sensitive substring matches with stable line
    anchors that the Agent can verify through ``read_file``.
    """

    config = load_config(context.repo_path)
    project = config.project
    resolver = GitScopeResolver(
        context.repo_path,
        project,
        baseline_revision=context.baseline_revision,
        observed_head_revision=context.head_revision,
    )
    changes = resolver.changes()
    source_paths = sorted(
        {
            path
            for change in changes
            for path in (change.old_path, change.new_path)
            if path is not None and source_language(path) in {"python", "typescript"}
        }
    )
    python_provider = PythonFactProvider()
    typescript_provider = TypeScriptFactProvider()
    before_facts: list[Any] = []
    current_facts: list[Any] = []
    provider_errors: list[str] = []
    for path in source_paths:
        before = _git_bytes(context, context.baseline_revision, path)
        current = _git_bytes(context, context.head_revision, path)
        try:
            if before is not None:
                before_facts.extend(
                    _facts_for_bytes(
                        repo_path=context.repo_path,
                        roots=project.source_roots,
                        path=path,
                        raw=before,
                        version="baseline",
                        python_provider=python_provider,
                        typescript_provider=typescript_provider,
                    )
                )
            if current is not None:
                current_facts.extend(
                    _facts_for_bytes(
                        repo_path=context.repo_path,
                        roots=project.source_roots,
                        path=path,
                        raw=current,
                        version="head",
                        python_provider=python_provider,
                        typescript_provider=typescript_provider,
                    )
                )
        except (RuntimeError, SyntaxError, UnicodeError, ValueError) as error:
            provider_errors.append(f"{path}: {type(error).__name__}: {error}")

    before_by_id = {str(fact.symbol_id): fact for fact in before_facts}
    current_by_id = {str(fact.symbol_id): fact for fact in current_facts}
    before_ids = set(before_by_id)
    current_ids = set(current_by_id)
    candidate_signals: list[_LiteralSignal] = []
    seen_terms: set[str] = set()

    def add_signal(label: str, term: str) -> None:
        cleaned = term.strip()
        if cleaned and cleaned not in seen_terms:
            seen_terms.add(cleaned)
            candidate_signals.append(_LiteralSignal(label=label, term=cleaned))

    for symbol in sorted(before_ids - current_ids):
        display = _fact_display_name(before_by_id[symbol])
        add_signal(f"deleted-symbol:{display}", display)
    for symbol in sorted(current_ids - before_ids):
        display = _fact_display_name(current_by_id[symbol])
        add_signal(f"added-symbol:{display}", display)

    env_before = _env_keys(_git_bytes(context, context.baseline_revision, ".env.example"))
    env_current = _env_keys(_git_bytes(context, context.head_revision, ".env.example"))
    for key in sorted(env_before - env_current):
        add_signal(f"env-removed:{key}", key)
    for key in sorted(env_current - env_before):
        add_signal(f"env-added:{key}", key)
    for change in changes:
        path = change.path
        if path:
            add_signal(f"changed-path:{path}", path)

    # Six characters is the existing alignment precision rule, now explicit
    # and measurable instead of being hidden inside the monolithic seed tool.
    signals = sorted({signal for signal in candidate_signals if len(signal.term) >= 6})
    doc_paths = sorted(
        set(
            application._documents(
                context.repo_path,
                project.docs_roots,
                project.include,
                project.exclude,
            )
        )
        | set(application._root_markdown_paths(context.repo_path, project))
    )
    mappings: list[tuple[str, int, tuple[_LiteralSignal, ...]]] = []
    documents_read = 0
    matched_signals: set[_LiteralSignal] = set()
    for doc in doc_paths:
        target = context.repo_path / doc
        if not target.is_file():
            continue
        documents_read += 1
        text = target.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), start=1):
            matches = tuple(signal for signal in signals if signal.term in line)
            if not matches:
                continue
            mappings.append((doc, number, matches))
            matched_signals.update(matches)

    lines = [
        "Literal change-to-document alignment map (mechanical, no interpretation).",
        "Dependencies: " + ", ".join(_COMPONENT_DEPENDENCIES["alignment_map"]),
        "Selection: exact case-sensitive substring matches for changed-source symbol names, "
        ".env.example key deltas, and changed paths; literals shorter than 6 characters are "
        "excluded for precision. This is the isolated alignment role of the prior seeded tool; "
        "signature deltas are not added as a second change-seed channel.",
        "Each signal is rendered as kind:<literal>; the suffix after the first colon is the "
        "exact literal matched on that document line.",
        "Only matched path/line leads are returned. Unmatched seeds, signatures, headings, "
        "and document text are not exposed; verify every lead with generic read_file.",
        "",
        "## Document line mappings",
    ]
    if mappings:
        for doc, number, matches in mappings:
            rendered_matches = ", ".join(signal.label for signal in matches)
            lines.append(f"- {doc}:L{number} | {rendered_matches}")
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Completeness accounting",
            f"- in-scope documents scanned: {documents_read}",
            f"- eligible literal signals: {len(signals)}",
            f"- matched literal signals: {len(matched_signals)}",
            f"- complete document-line mappings: {len(mappings)}",
            "- partial mappings returned: 0",
            "- inherited product-internal truncation: none; build_seed is not called",
        ]
    )
    text = "\n".join(lines)
    if len(text) > _ALIGNMENT_MAP_CHAR_LIMIT:
        raise ValueError(
            "alignment_map complete output has "
            f"{len(text)} characters across {len(mappings)} document-line mappings, "
            f"exceeding the {_ALIGNMENT_MAP_CHAR_LIMIT}-character one-call safety bound; "
            "no partial alignment map was returned"
        )
    return _ComponentResult(
        text=text,
        metadata={
            "changed_files": len(changes),
            "changed_source_files": len(source_paths),
            "facts_baseline_changed": len(before_facts),
            "facts_head_changed": len(current_facts),
            "provider_errors": provider_errors,
            "documents_in_scope": len(doc_paths),
            "documents_read": documents_read,
            "candidate_signals": len(candidate_signals),
            "eligible_signals": len(signals),
            "short_signals_excluded": len(
                {signal for signal in candidate_signals if len(signal.term) < 6}
            ),
            "matched_signals": len(matched_signals),
            "alignment_sites": len(mappings),
            "brief_chars": len(text),
            "char_limit": _ALIGNMENT_MAP_CHAR_LIMIT,
            "selection_unit": "complete_document_line_mapping",
            "matching_rule": "case_sensitive_literal_substring_min_6_chars",
            "hard_character_truncation": False,
            "overflow_behavior": "fail_closed_no_partial_output",
            "full_detail_tool": "read_file(path, start, end)",
            "unmatched_signals_exposed": False,
            "document_text_returned": False,
            "full_diff_collected": False,
            "change_seed_component_called": False,
            "finalizer_gate_or_store_used": False,
        },
    )


_BUILDERS = {
    "brief_diff": _build_brief_diff,
    "doc_map": _build_doc_map,
    "change_seed": _build_change_seed,
    "alignment_map": _build_alignment_map,
}


def _render_profile(profile: ProfileSpec, components: dict[ComponentId, str]) -> str:
    lines = [
        "# Deterministic audit brief",
        f"Profile: {profile.profile_id}",
        "Declared dependencies: " + ", ".join(profile.dependencies),
        "The content below is returned in this call; no index or section lookup is needed.",
    ]
    for component in profile.components:
        lines.extend(["", f"# Component: {component}", components[component]])
    return "\n".join(lines)


def attach_audit_brief(
    runtime: AgentRuntime,
    context: AgentContext,
    profile: ProfileSpec,
    *,
    profile_id: str | None = None,
    base_metadata_prefix: str = "generic",
    setup_started: float | None = None,
) -> AgentRuntime:
    """Attach the existing lazy brief renderer to one already-owned runtime.

    The caller retains ownership of the toolbox, finalizer, and cleanup callback.
    This is what lets a graph treatment compose with ``audit_brief`` without
    constructing a second generic runtime or graph clone.
    """

    if AUDIT_BRIEF_TOOL in runtime.extra_tools:
        raise ValueError(f"runtime already exposes {AUDIT_BRIEF_TOOL}")
    if not base_metadata_prefix or not base_metadata_prefix.isidentifier():
        raise ValueError("base_metadata_prefix must be a non-empty identifier")
    if setup_started is None:
        setup_started = time.monotonic()
    metadata = runtime.metadata
    base_profile_id = metadata.get("profile_id")
    base_dependencies = list(metadata.get("dependencies", []))
    base_dependency_sha256 = metadata.get("dependency_sha256")
    dependencies = [*base_dependencies]
    for dependency in profile.dependencies:
        if dependency not in dependencies:
            dependencies.append(dependency)
    dependency_hash = _sha256_json(dependencies)
    metadata.update(
        {
            "profile_id": profile.profile_id if profile_id is None else profile_id,
            "components": list(profile.components),
            "dependencies": dependencies,
            "dependency_sha256": dependency_hash,
            f"{base_metadata_prefix}_profile_id": base_profile_id,
            f"{base_metadata_prefix}_dependencies": base_dependencies,
            f"{base_metadata_prefix}_dependency_sha256": base_dependency_sha256,
            "brief_component_dependencies": list(profile.dependencies),
            "brief_component_dependency_sha256": _sha256_json(list(profile.dependencies)),
            "component_chars": {},
            "component_sha256": {},
            "component_build_seconds": {},
            "component_metadata": {},
            "internal_component_limits": {
                "brief_diff": {
                    "target_chars": _BRIEF_DIFF_CHAR_LIMIT,
                    "unit": "complete_logical_entry",
                    "hard_character_truncation": False,
                },
                "doc_map": {
                    "target_chars": _DOC_MAP_CHAR_LIMIT,
                    "unit": "complete_logical_entry",
                    "hard_character_truncation": False,
                },
                "change_seed": {
                    "target_chars": _CHANGE_SEED_CHAR_LIMIT,
                    "unit": "complete_logical_entry",
                    "hard_character_truncation": False,
                },
                "alignment_map": {
                    "target_chars": _ALIGNMENT_MAP_CHAR_LIMIT,
                    "unit": "complete_document_line_mapping",
                    "hard_character_truncation": False,
                    "overflow_behavior": "fail_closed_no_partial_output",
                },
            },
            "audit_brief_calls": 0,
            "audit_brief_cache_hits": 0,
            "audit_brief_handler_seconds": [],
            "audit_brief_handler_total_seconds": 0.0,
            "brief_chars": None,
            "brief_target_chars": PROFILE_BRIEF_TARGET_CHARS,
        }
    )
    cached: str | None = None

    def audit_brief(_arguments: dict[str, Any]) -> str:
        nonlocal cached
        handler_started = time.monotonic()
        metadata["audit_brief_calls"] += 1
        if cached is not None:
            metadata["audit_brief_cache_hits"] += 1
        else:
            rendered: dict[ComponentId, str] = {}
            for component in profile.components:
                component_started = time.monotonic()
                result = _BUILDERS[component](context)
                component_seconds = time.monotonic() - component_started
                rendered[component] = result.text
                metadata["component_chars"][component] = len(result.text)
                metadata["component_sha256"][component] = hashlib.sha256(
                    result.text.encode("utf-8")
                ).hexdigest()
                metadata["component_build_seconds"][component] = round(component_seconds, 6)
                metadata["component_metadata"][component] = result.metadata
            cached = _render_profile(profile, rendered)
            metadata["brief_chars"] = len(cached)
            metadata["brief_sha256"] = hashlib.sha256(cached.encode("utf-8")).hexdigest()
            metadata["brief_target_exceeded"] = len(cached) > PROFILE_BRIEF_TARGET_CHARS
        elapsed = time.monotonic() - handler_started
        metadata["audit_brief_handler_seconds"].append(round(elapsed, 6))
        metadata["audit_brief_handler_total_seconds"] = round(
            sum(metadata["audit_brief_handler_seconds"]), 6
        )
        return cached

    runtime.extra_tools[AUDIT_BRIEF_TOOL] = (AUDIT_BRIEF_DEFINITION, audit_brief)
    tool_surface = metadata.get("tool_surface")
    if isinstance(tool_surface, list):
        metadata["tool_surface"] = [*tool_surface, AUDIT_BRIEF_TOOL]
    metadata["portfolio_setup_seconds"] = round(time.monotonic() - setup_started, 6)
    return runtime


def portfolio_runtime(context: AgentContext, profile: ProfileSpec) -> AgentRuntime:
    """Return a lazy runtime containing exactly generic tools plus ``audit_brief``."""

    setup_started = time.monotonic()
    return attach_audit_brief(
        paged_generic_runtime(context),
        context,
        profile,
        setup_started=setup_started,
    )


__all__ = [
    "ALIGNMENT_MAP_PROFILE",
    "AUDIT_BRIEF_DEFINITION",
    "AUDIT_BRIEF_TOOL",
    "AUDIT_BRIEF_TOOLS",
    "BRIEF_DIFF_DOC_MAP_PROFILE",
    "BRIEF_DIFF_PROFILE",
    "CHANGE_SEED_PROFILE",
    "DOC_MAP_PROFILE",
    "PORTFOLIO_PROTOCOL_VERSION",
    "PORTFOLIO_SYSTEM_PROMPT",
    "PROFILE_BRIEF_TARGET_CHARS",
    "PROFILE_SPECS",
    "ProfileSpec",
    "attach_audit_brief",
    "portfolio_runtime",
]

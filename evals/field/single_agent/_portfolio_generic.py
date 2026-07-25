"""Resumable generic repository tools for the tool-portfolio experiment.

The page size is a transport property, not an Agent budget: every generated
page has an opaque continuation cursor and the complete result remains
available for the lifetime of the run.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from _runner import (
    BASE_TOOLS,
    PORTFOLIO_SYSTEM_PROMPT,
    TOOL_PORTFOLIO_PROTOCOL_VERSION,
    AgentContext,
    AgentDefinition,
    AgentRuntime,
    ExtraTools,
    UnboundedRepoToolbox,
    generic_extra_tools,
)

from drift_agent.agent.episode import ToolError

_DEFAULT_PAGE_CHARS = 16_000
_MIN_PAGE_CHARS = 4_000
_MAX_PAGE_CHARS = 32_000
_PAGE_HEADER_RESERVE = 700
_DIFF_SIGNAL = re.compile(
    r"^(?:export\s+|async\s+|function\s+|class\s+|interface\s+|type\s+|enum\s+|"
    r"const\s+|let\s+|var\s+|def\s+|from\s+|import\s+|[A-Z][A-Z0-9_]{2,}\s*=|"
    r".*(?:process\.env|permission|policy|register|route|react|executor|state))",
    re.IGNORECASE,
)


def _function(name: str, description: str, properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "additionalProperties": False,
            },
        },
    }


_CURSOR_PROPERTY = {
    "type": "string",
    "description": "Opaque next_cursor returned by the same tool in this run.",
}
_MAX_CHARS_PROPERTY = {
    "type": "integer",
    "minimum": _MIN_PAGE_CHARS,
    "maximum": _MAX_PAGE_CHARS,
    "default": _DEFAULT_PAGE_CHARS,
}

PAGED_READ_FILE_DEFINITION = _function(
    "read_file",
    "Read a repo-relative text file with line numbers. Large results are split into "
    "complete resumable pages; pass next_cursor to continue.",
    {
        "path": {"type": "string"},
        "start": {"type": "integer", "minimum": 1},
        "end": {"type": "integer", "minimum": 1},
        "cursor": _CURSOR_PROPERTY,
        "max_chars": _MAX_CHARS_PROPERTY,
    },
)
PAGED_READ_FILE_DEFINITION["function"]["parameters"]["anyOf"] = [
    {"required": ["path"]},
    {"required": ["cursor"]},
]

PAGED_GREP_DEFINITION = _function(
    "grep",
    "Search repository text with a regex (literal fallback). Optional glob narrows "
    "the scan. Results are resumable; pass next_cursor to continue.",
    {
        "pattern": {"type": "string"},
        "glob": {"type": "string"},
        "cursor": _CURSOR_PROPERTY,
        "max_chars": _MAX_CHARS_PROPERTY,
    },
)
PAGED_GREP_DEFINITION["function"]["parameters"]["anyOf"] = [
    {"required": ["pattern"]},
    {"required": ["cursor"]},
]

PAGED_LIST_DIR_DEFINITION = _function(
    "list_dir",
    "List one repo-relative directory. Large directories are split into complete "
    "resumable pages; pass next_cursor to continue.",
    {
        "path": {"type": "string"},
        "cursor": _CURSOR_PROPERTY,
        "max_chars": _MAX_CHARS_PROPERTY,
    },
)

PAGED_GIT_CHANGED_FILES_DEFINITION = _function(
    "git_changed_files",
    "List every baseline-to-HEAD changed file using git name-status. Large results "
    "are split into complete resumable pages; pass next_cursor to continue.",
    {
        "cursor": _CURSOR_PROPERTY,
        "max_chars": _MAX_CHARS_PROPERTY,
    },
)

PAGED_GIT_DIFF_DEFINITION = _function(
    "git_diff",
    "Inspect the baseline-to-HEAD change. With no path, return a compact complete "
    "changed-file/hunk index and high-signal changed declarations. With path, return "
    "that file's diff. Pass next_cursor to continue any result.",
    {
        "path": {"type": "string"},
        "context": {"type": "integer", "minimum": 0, "maximum": 40, "default": 8},
        "cursor": _CURSOR_PROPERTY,
        "max_chars": _MAX_CHARS_PROPERTY,
    },
)

PAGED_GIT_SHOW_DEFINITION = _function(
    "git_show",
    "Read one repository-relative file at baseline or HEAD with line numbers. "
    "Large results are resumable; pass next_cursor to continue.",
    {
        "version": {"type": "string", "enum": ["baseline", "head"]},
        "path": {"type": "string"},
        "cursor": _CURSOR_PROPERTY,
        "max_chars": _MAX_CHARS_PROPERTY,
    },
)
PAGED_GIT_SHOW_DEFINITION["function"]["parameters"]["anyOf"] = [
    {"required": ["version", "path"]},
    {"required": ["cursor"]},
]


@dataclass(frozen=True, slots=True)
class _Page:
    body: str
    item_indexes: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _PageQuery:
    kind: str
    query_hash: str
    pages: tuple[_Page, ...]
    item_count: int
    page_chars: int


class _PageStore:
    def __init__(self, metadata: dict[str, Any]) -> None:
        self._queries: dict[str, _PageQuery] = {}
        self._cursors: dict[str, tuple[str, int, str]] = {}
        self._metadata = metadata

    @staticmethod
    def _page_chars(arguments: dict[str, Any]) -> int:
        raw = arguments.get("max_chars", _DEFAULT_PAGE_CHARS)
        if isinstance(raw, bool):
            raise ValueError("max_chars must be an integer")
        try:
            value = int(raw)
        except (TypeError, ValueError) as error:
            raise ValueError("max_chars must be an integer") from error
        if not _MIN_PAGE_CHARS <= value <= _MAX_PAGE_CHARS:
            raise ValueError(f"max_chars must be between {_MIN_PAGE_CHARS} and {_MAX_PAGE_CHARS}")
        return value

    @staticmethod
    def _fragments(item: str, budget: int, item_index: int) -> list[tuple[str, int]]:
        if len(item) <= budget:
            return [(item, item_index)]
        width = max(1, budget - 80)
        count = (len(item) + width - 1) // width
        return [
            (
                f"[fragment {index + 1}/{count}] {item[index * width : (index + 1) * width]}",
                item_index,
            )
            for index in range(count)
        ]

    @classmethod
    def _pages(cls, items: list[str], max_chars: int) -> tuple[_Page, ...]:
        budget = max_chars - _PAGE_HEADER_RESERVE
        fragments = [
            fragment
            for item_index, item in enumerate(items)
            for fragment in cls._fragments(item, budget, item_index)
        ]
        if not fragments:
            return (_Page(body="(no results)", item_indexes=()),)
        pages: list[_Page] = []
        current: list[tuple[str, int]] = []
        current_chars = 0
        for fragment, item_index in fragments:
            extra = len(fragment) + (1 if current else 0)
            if current and current_chars + extra > budget:
                pages.append(
                    _Page(
                        body="\n".join(value for value, _index in current),
                        item_indexes=tuple(dict.fromkeys(index for _value, index in current)),
                    )
                )
                current = []
                current_chars = 0
            current.append((fragment, item_index))
            current_chars += len(fragment) + (1 if len(current) > 1 else 0)
        if current:
            pages.append(
                _Page(
                    body="\n".join(value for value, _index in current),
                    item_indexes=tuple(dict.fromkeys(index for _value, index in current)),
                )
            )
        return tuple(pages)

    def _render(
        self,
        query: _PageQuery,
        page_index: int,
        *,
        request_cursor: str | None,
    ) -> str:
        next_cursor: str | None = None
        if page_index + 1 < len(query.pages):
            next_cursor = f"pg_{query.query_hash[:20]}_{page_index + 1}"
            self._cursors[next_cursor] = (query.query_hash, page_index + 1, query.kind)
        page = query.pages[page_index]
        item_start = min(page.item_indexes) + 1 if page.item_indexes else None
        item_end = max(page.item_indexes) + 1 if page.item_indexes else None
        envelope = {
            "has_more": next_cursor is not None,
            "item_end": item_end,
            "item_start": item_start,
            "kind": query.kind,
            "logical_items": query.item_count,
            "next_cursor": next_cursor,
            "page": page_index + 1,
            "pages": len(query.pages),
            "returned_chars": len(page.body),
            "returned_items": len(page.item_indexes),
            "total_logical_items": query.item_count,
        }
        header = json.dumps(
            envelope,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        output = f"{header}\n{page.body}"
        self._metadata["page_calls"].append(
            {
                **envelope,
                "query_hash": query.query_hash,
                "page_chars": query.page_chars,
                "request_cursor": request_cursor,
                "body_sha256": hashlib.sha256(page.body.encode("utf-8")).hexdigest(),
                "output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
            }
        )
        return output

    def start(
        self,
        *,
        kind: str,
        query_arguments: dict[str, Any],
        items: list[str],
        call_arguments: dict[str, Any],
    ) -> str:
        max_chars = self._page_chars(call_arguments)
        encoded = json.dumps(
            {
                "kind": kind,
                "arguments": query_arguments,
                "items": items,
                "page_chars": max_chars,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        query_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        query = _PageQuery(
            kind=kind,
            query_hash=query_hash,
            pages=self._pages(items, max_chars),
            item_count=len(items),
            page_chars=max_chars,
        )
        self._queries[query_hash] = query
        self._metadata["page_queries"].append(
            {
                "kind": kind,
                "query_hash": query_hash,
                "logical_items": len(items),
                "pages": len(query.pages),
                "page_chars": max_chars,
                "complete_result_resumable": True,
                "logical_items_sha256": hashlib.sha256(
                    json.dumps(
                        items,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
            }
        )
        return self._render(query, 0, request_cursor=None)

    def resume(self, cursor: str, *, expected_kind: str) -> str:
        state = self._cursors.get(cursor)
        if state is None:
            raise ValueError("unknown or expired cursor")
        query_hash, page_index, cursor_kind = state
        if cursor_kind != expected_kind:
            raise ValueError(
                f"cursor belongs to {cursor_kind}, not {expected_kind}; use it only "
                "with the tool that returned it"
            )
        query = self._queries.get(query_hash)
        if query is None or query.kind != expected_kind or page_index >= len(query.pages):
            raise ValueError("unknown or expired cursor")
        return self._render(query, page_index, request_cursor=cursor)


def _run_git(context: AgentContext, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=context.repo_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError(completed.stderr.strip() or "git command failed")
    return completed.stdout


def _safe_relative(toolbox: UnboundedRepoToolbox, value: object) -> str:
    relative = str(value or "").strip()
    target = toolbox._resolve(relative)
    return target.relative_to(toolbox._root).as_posix()


def _diff_index(context: AgentContext) -> list[str]:
    name_status = _run_git(
        context,
        "diff",
        "--name-status",
        context.baseline_revision,
        "--",
    ).rstrip()
    stat = _run_git(
        context,
        "diff",
        "--stat",
        context.baseline_revision,
        "--",
    ).rstrip()
    raw = _run_git(
        context,
        "diff",
        "--no-ext-diff",
        "--unified=0",
        context.baseline_revision,
        "--",
    )
    items = [
        "DIFF INDEX: every changed file and hunk is listed; call git_diff with path for "
        "the complete resumable file diff.",
        "NAME STATUS\n" + (name_status or "(none)"),
        "STAT\n" + (stat or "(none)"),
        "HUNKS AND HIGH-SIGNAL CHANGED LINES",
    ]
    current_file = ""
    current_hunk = ""
    signal_positions: dict[tuple[str, str, str, str], int] = {}
    signal_counts: dict[tuple[str, str, str, str], int] = {}
    for line in raw.splitlines():
        if line.startswith("diff --git "):
            current_file = line.removeprefix("diff --git ").split(" b/", 1)[-1]
            current_hunk = ""
            items.append(f"FILE {current_file}")
        elif line.startswith("@@"):
            current_hunk = line
            items.append(line)
        elif line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
            changed = line[1:].strip()
            if changed and _DIFF_SIGNAL.search(changed):
                key = (current_file, current_hunk, line[0], changed)
                signal_counts[key] = signal_counts.get(key, 0) + 1
                if key not in signal_positions:
                    signal_positions[key] = len(items)
                    items.append(f"{line[0]} {changed}")
    for key, count in signal_counts.items():
        if count > 1:
            position = signal_positions[key]
            items[position] = f"{items[position]}  [repeated {count}x in hunk]"
    return items


def paged_generic_runtime(context: AgentContext) -> AgentRuntime:
    metadata: dict[str, Any] = {
        "profile_id": "paged_generic",
        "dependencies": ["git", "filesystem"],
        "dependency_sha256": hashlib.sha256(b"git\0filesystem\0paged-v1").hexdigest(),
        "transport_pagination": {
            "default_page_chars": _DEFAULT_PAGE_CHARS,
            "minimum_page_chars": _MIN_PAGE_CHARS,
            "maximum_page_chars": _MAX_PAGE_CHARS,
            "whole_result_limit": None,
            "opaque_cursor": True,
            "cursor_bound_to_tool_kind": True,
            "item_range_indexing": "one_based_inclusive",
            "history_trimming": False,
        },
        "page_queries": [],
        "page_calls": [],
        "handler_calls": [],
    }
    pages = _PageStore(metadata)
    toolbox = UnboundedRepoToolbox(context.repo_path)

    def instrument(name: str, handler: Any) -> Any:
        def wrapped(arguments: dict[str, Any]) -> str:
            started = time.monotonic()
            traced_arguments = {
                key: value
                for key, value in sorted(arguments.items())
                if key
                in {
                    "path",
                    "start",
                    "end",
                    "pattern",
                    "glob",
                    "version",
                    "context",
                    "cursor",
                    "max_chars",
                }
            }
            try:
                output = str(handler(arguments))
            except Exception as error:
                metadata["handler_calls"].append(
                    {
                        "tool": name,
                        "arguments": traced_arguments,
                        "seconds": round(time.monotonic() - started, 6),
                        "error": True,
                        "error_type": type(error).__name__,
                        "error_message": str(error),
                    }
                )
                raise
            record: dict[str, Any] = {
                "tool": name,
                "arguments": traced_arguments,
                "seconds": round(time.monotonic() - started, 6),
                "output_chars": len(output),
                "output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
                "error": False,
            }
            try:
                envelope = json.loads(output.partition("\n")[0])
            except json.JSONDecodeError:
                envelope = None
            if isinstance(envelope, dict):
                record["page_envelope"] = envelope
            metadata["handler_calls"].append(record)
            return output

        return wrapped

    def read_file(arguments: dict[str, Any]) -> str:
        cursor = str(arguments.get("cursor", "")).strip()
        if cursor:
            return pages.resume(cursor, expected_kind="read_file")
        relative = _safe_relative(toolbox, arguments.get("path"))
        target = toolbox._resolve(relative)
        if not target.is_file():
            raise ToolError(f"not a file: {relative}")
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        first = max(1, int(arguments.get("start", 1) or 1))
        raw_end = arguments.get("end")
        last = len(lines) if raw_end in {None, ""} else min(len(lines), int(raw_end))
        if last < first:
            raise ValueError("empty line range")
        toolbox.reads.add(relative)
        items = [f"{number}: {lines[number - 1]}" for number in range(first, last + 1)]
        return pages.start(
            kind="read_file",
            query_arguments={"path": relative, "start": first, "end": last},
            items=items,
            call_arguments=arguments,
        )

    def grep(arguments: dict[str, Any]) -> str:
        cursor = str(arguments.get("cursor", "")).strip()
        if cursor:
            return pages.resume(cursor, expected_kind="grep")
        pattern = str(arguments.get("pattern", ""))
        if not pattern:
            raise ValueError("pattern must be non-empty")
        glob = str(arguments.get("glob") or "**/*")
        toolbox._assert_safe_glob(glob)  # type: ignore[attr-defined]
        try:
            compiled = re.compile(pattern)
        except re.error:
            compiled = re.compile(re.escape(pattern))
        items: list[str] = []
        root: Path = toolbox._root
        for unresolved in sorted(root.glob(glob)):
            relative_path = unresolved.relative_to(root)
            if relative_path.parts and relative_path.parts[0] == ".git":
                continue
            candidate = unresolved.resolve()
            if candidate != root and root not in candidate.parents:
                continue
            if not candidate.is_file():
                continue
            text = candidate.read_text(encoding="utf-8", errors="replace")
            if "\x00" in text[:1024]:
                continue
            relative = relative_path.as_posix()
            for number, line in enumerate(text.splitlines(), start=1):
                if compiled.search(line):
                    items.append(f"{relative}:{number}: {line}")
        return pages.start(
            kind="grep",
            query_arguments={"pattern": pattern, "glob": glob},
            items=items,
            call_arguments=arguments,
        )

    def list_dir(arguments: dict[str, Any]) -> str:
        cursor = str(arguments.get("cursor", "")).strip()
        if cursor:
            return pages.resume(cursor, expected_kind="list_dir")
        relative = _safe_relative(toolbox, arguments.get("path") or ".")
        target = toolbox._resolve(relative)
        if not target.is_dir():
            raise ToolError(f"not a directory: {relative}")
        items = [child.name + ("/" if child.is_dir() else "") for child in sorted(target.iterdir())]
        return pages.start(
            kind="list_dir",
            query_arguments={"path": relative},
            items=items,
            call_arguments=arguments,
        )

    def git_changed_files(arguments: dict[str, Any]) -> str:
        cursor = str(arguments.get("cursor", "")).strip()
        if cursor:
            return pages.resume(cursor, expected_kind="git_changed_files")
        rendered = _run_git(
            context,
            "diff",
            "--name-status",
            context.baseline_revision,
            "--",
        )
        return pages.start(
            kind="git_changed_files",
            query_arguments={"baseline_revision": context.baseline_revision},
            items=rendered.splitlines(),
            call_arguments=arguments,
        )

    def git_diff(arguments: dict[str, Any]) -> str:
        cursor = str(arguments.get("cursor", "")).strip()
        if cursor:
            return pages.resume(cursor, expected_kind="git_diff")
        raw_path = str(arguments.get("path", "")).strip()
        if not raw_path:
            items = _diff_index(context)
            query = {"path": None, "mode": "index"}
        else:
            relative = _safe_relative(toolbox, raw_path)
            raw_context = arguments.get("context", 8)
            if isinstance(raw_context, bool):
                raise ValueError("context must be an integer")
            context_lines = int(raw_context)
            if not 0 <= context_lines <= 40:
                raise ValueError("context must be between 0 and 40")
            rendered = _run_git(
                context,
                "diff",
                "--no-ext-diff",
                f"--unified={context_lines}",
                context.baseline_revision,
                "--",
                relative,
            )
            items = rendered.splitlines() or ["(no diff for path)"]
            query = {"path": relative, "context": context_lines, "mode": "file"}
        return pages.start(
            kind="git_diff",
            query_arguments=query,
            items=items,
            call_arguments=arguments,
        )

    def git_show(arguments: dict[str, Any]) -> str:
        cursor = str(arguments.get("cursor", "")).strip()
        if cursor:
            return pages.resume(cursor, expected_kind="git_show")
        relative = _safe_relative(toolbox, arguments.get("path"))
        version = str(arguments.get("version", "")).strip().lower()
        if version not in {"baseline", "head"}:
            raise ValueError("version must be baseline or head")
        revision = context.baseline_revision if version == "baseline" else context.head_revision
        rendered = _run_git(context, "show", f"{revision}:{relative}")
        items = [f"{number}: {line}" for number, line in enumerate(rendered.splitlines(), start=1)]
        return pages.start(
            kind="git_show",
            query_arguments={"version": version, "path": relative},
            items=items,
            call_arguments=arguments,
        )

    extras: ExtraTools = generic_extra_tools(context)
    extras.update(
        {
            "read_file": (PAGED_READ_FILE_DEFINITION, instrument("read_file", read_file)),
            "grep": (PAGED_GREP_DEFINITION, instrument("grep", grep)),
            "list_dir": (PAGED_LIST_DIR_DEFINITION, instrument("list_dir", list_dir)),
            "git_changed_files": (
                PAGED_GIT_CHANGED_FILES_DEFINITION,
                instrument("git_changed_files", git_changed_files),
            ),
            "git_diff": (PAGED_GIT_DIFF_DEFINITION, instrument("git_diff", git_diff)),
            "git_show": (PAGED_GIT_SHOW_DEFINITION, instrument("git_show", git_show)),
        }
    )
    return AgentRuntime(toolbox=toolbox, extra_tools=extras, metadata=metadata)


PAGED_GENERIC_AGENT = AgentDefinition(
    name="paged_generic_agent",
    tools=BASE_TOOLS,
    prepare=paged_generic_runtime,
    protocol_version=TOOL_PORTFOLIO_PROTOCOL_VERSION,
    system_prompt=PORTFOLIO_SYSTEM_PROMPT,
)


__all__ = [
    "PAGED_GENERIC_AGENT",
    "PAGED_GIT_CHANGED_FILES_DEFINITION",
    "PAGED_GIT_DIFF_DEFINITION",
    "PAGED_GIT_SHOW_DEFINITION",
    "PAGED_GREP_DEFINITION",
    "PAGED_LIST_DIR_DEFINITION",
    "PAGED_READ_FILE_DEFINITION",
    "paged_generic_runtime",
]

"""Provider-neutral, lossless code-graph context for tool-portfolio experiments.

Both graph backends expose the same single model-visible ``graph_context``
function.  Provider indexes live in isolated clones, provider output is
projected into semantic records, and oversized records become authenticated-
cursor pages of reassemblable JSON fragments instead of being discarded.  The
opaque cursor is bound to the query, provider, and index.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from _graph_runtime import (
    CODEGRAPH_PACKAGE_INTEGRITY,
    CODEGRAPH_VERSION,
    GITNEXUS_PACKAGE_INTEGRITY,
    GITNEXUS_VERSION,
    _assert_visible_repo_isolated,
    _binary,
    _cleanup_callback,
    _clone_for_index,
    _directory_size,
    _metadata,
    _run,
    _sanitized,
)
from _portfolio_generic import paged_generic_runtime
from _runner import (
    BASE_TOOLS,
    TOOL_PORTFOLIO_PROTOCOL_VERSION,
    AgentContext,
    AgentRuntime,
    ExtraTools,
)

PORTFOLIO_PROTOCOL_VERSION = TOOL_PORTFOLIO_PROTOCOL_VERSION
CODEGRAPH_CONTEXT_AGENT = "codegraph_context_agent"
GITNEXUS_CONTEXT_AGENT = "gitnexus_context_agent"
GRAPH_CONTEXT_TOOL = "graph_context"
GRAPH_CONTEXT_TOOLS = (GRAPH_CONTEXT_TOOL,)
PORTFOLIO_TOOL_MENU = BASE_TOOLS + GRAPH_CONTEXT_TOOLS

_MIN_PAGE_CHARS = 4_000
_MAX_PAGE_CHARS = 16_000
_DEFAULT_PAGE_CHARS = 12_000
# Leaves enough room for the compact envelope and authenticated cursor on the
# minimum 4,000-character page while avoiding any string slicing.
_MAX_ATOMIC_CHARS = 3_300
# Canonical JSON is embedded as a JSON string in each fragment, so quotes and
# backslashes can expand by up to 2x.  Eight hundred characters still fit with
# a worst-case 512-character routed target and the cursor page envelope.
_FRAGMENT_PAYLOAD_CHARS = 800
_MIN_BREADTH = 8
_MAX_BREADTH = 500
_DEFAULT_BREADTH = 100
_MIN_DEPTH = 1
_MAX_DEPTH = 10
_DEFAULT_DEPTH = 3
_MIN_MAX_FILES = 1
_MAX_MAX_FILES = 20
_DEFAULT_MAX_FILES = 20
_CURSOR_VERSION = 1
_SOURCE_LINE = re.compile(r"^(\d+)\t(.*)$")
_SOURCE_HEADING = re.compile(r"^\*\*`([^`]+)`\*\*")
_DROP_JSON_KEYS = frozenset(
    {
        "content",
        "error",
        "errors",
        "instruction",
        "instructions",
        "suggestion",
        "timing",
    }
)


def _function(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


GRAPH_CONTEXT_DEFINITION = _function(
    GRAPH_CONTEXT_TOOL,
    (
        "Retrieve current-HEAD definitions, relationships, impact, and optional source "
        "for 1-4 concrete symbols or paths. Exact match/no-match records come first. "
        "Scope records disclose backend breadth, depth, file, and pagination behavior. "
        "Oversized logical records use lossless JSON fragments with a shared SHA-256. "
        "If next_cursor is present, call again with identical arguments and that cursor "
        "to recover every record. This has no baseline diff or documentation alignment."
    ),
    {
        "targets": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 512},
            "minItems": 1,
            "maxItems": 4,
        },
        "question": {"type": "string", "maxLength": 2_000},
        "cursor": {"type": "string", "minLength": 1, "maxLength": 2_048},
        "include_source": {"type": "boolean", "default": False},
        "breadth": {
            "type": "integer",
            "minimum": _MIN_BREADTH,
            "maximum": _MAX_BREADTH,
            "default": _DEFAULT_BREADTH,
            "description": (
                "Results requested per backend page or non-pageable lookup. Provider "
                "scope records disclose whether that view can be exhaustive."
            ),
        },
        "depth": {
            "type": "integer",
            "minimum": _MIN_DEPTH,
            "maximum": _MAX_DEPTH,
            "default": _DEFAULT_DEPTH,
            "description": (
                "Relationship depth requested where the backend supports an explicit "
                "depth control; scope records disclose unsupported/provider-managed depth."
            ),
        },
        "max_files": {
            "type": "integer",
            "minimum": _MIN_MAX_FILES,
            "maximum": _MAX_MAX_FILES,
            "default": _DEFAULT_MAX_FILES,
            "description": (
                "Maximum source-bearing context files where the backend supports this "
                "control; scope records disclose when it is not applicable."
            ),
        },
        "max_chars": {
            "type": "integer",
            "minimum": _MIN_PAGE_CHARS,
            "maximum": _MAX_PAGE_CHARS,
            "default": _DEFAULT_PAGE_CHARS,
        },
    },
    ["targets"],
)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _safe_text(value: object, *, field: str, max_length: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} must be non-empty")
    if len(text) > max_length:
        raise ValueError(f"{field} exceeds {max_length} characters")
    if "\x00" in text or any(ord(character) < 32 and character not in "\n\t" for character in text):
        raise ValueError(f"{field} contains control characters")
    return text


def _normalized_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    raw_targets = arguments.get("targets")
    if not isinstance(raw_targets, list) or not 1 <= len(raw_targets) <= 4:
        raise ValueError("targets must contain 1-4 strings")
    targets: list[str] = []
    for index, raw in enumerate(raw_targets):
        target = _safe_text(raw, field=f"targets[{index}]", max_length=512)
        if target not in targets:
            targets.append(target)
    question_raw = arguments.get("question")
    question = ""
    if question_raw is not None and str(question_raw).strip():
        question = _safe_text(question_raw, field="question", max_length=2_000)
    raw_include_source = arguments.get("include_source", False)
    if not isinstance(raw_include_source, bool):
        raise ValueError("include_source must be a boolean")
    integer_controls = {
        "breadth": (
            arguments.get("breadth", _DEFAULT_BREADTH),
            _MIN_BREADTH,
            _MAX_BREADTH,
        ),
        "depth": (
            arguments.get("depth", _DEFAULT_DEPTH),
            _MIN_DEPTH,
            _MAX_DEPTH,
        ),
        "max_files": (
            arguments.get("max_files", _DEFAULT_MAX_FILES),
            _MIN_MAX_FILES,
            _MAX_MAX_FILES,
        ),
    }
    normalized_controls: dict[str, int] = {}
    for name, (raw_value, minimum, maximum) in integer_controls.items():
        if isinstance(raw_value, bool):
            raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
        try:
            value = int(raw_value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{name} must be an integer from {minimum} to {maximum}") from error
        if not minimum <= value <= maximum:
            raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
        normalized_controls[name] = value
    raw_max_chars = arguments.get("max_chars", _DEFAULT_PAGE_CHARS)
    if isinstance(raw_max_chars, bool):
        raise ValueError("max_chars must be an integer from 4000 to 16000")
    try:
        max_chars = int(raw_max_chars)
    except (TypeError, ValueError) as error:
        raise ValueError("max_chars must be an integer from 4000 to 16000") from error
    if not _MIN_PAGE_CHARS <= max_chars <= _MAX_PAGE_CHARS:
        raise ValueError("max_chars must be an integer from 4000 to 16000")
    cursor_raw = arguments.get("cursor")
    cursor = ""
    if cursor_raw is not None and str(cursor_raw).strip():
        cursor = _safe_text(cursor_raw, field="cursor", max_length=2_048)
    return {
        "targets": targets,
        "question": question,
        "include_source": raw_include_source,
        **normalized_controls,
        "max_chars": max_chars,
        "cursor": cursor,
    }


def _query_identity(arguments: dict[str, Any]) -> str:
    payload = {key: value for key, value in arguments.items() if key != "cursor"}
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _cursor_key(*, provider: str, index_fingerprint: str) -> bytes:
    return hashlib.sha256(
        f"graph-context-cursor\0{provider}\0{index_fingerprint}".encode()
    ).digest()


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _encode_cursor(
    *,
    provider: str,
    index_fingerprint: str,
    query_identity: str,
    offset: int,
    page_number: int,
) -> str:
    payload = _canonical_json(
        {
            "v": _CURSOR_VERSION,
            "provider": provider,
            "index": index_fingerprint,
            "query": query_identity,
            "offset": offset,
            "page": page_number,
        }
    ).encode("utf-8")
    signature = hmac.new(
        _cursor_key(provider=provider, index_fingerprint=index_fingerprint),
        payload,
        hashlib.sha256,
    ).digest()
    return f"{_b64encode(payload)}.{_b64encode(signature)}"


def _decode_cursor(
    cursor: str,
    *,
    provider: str,
    index_fingerprint: str,
    query_identity: str,
) -> tuple[int, int]:
    try:
        payload_text, signature_text = cursor.split(".", 1)
        payload = _b64decode(payload_text)
        signature = _b64decode(signature_text)
        expected = hmac.new(
            _cursor_key(provider=provider, index_fingerprint=index_fingerprint),
            payload,
            hashlib.sha256,
        ).digest()
        decoded = json.loads(payload)
    except (ValueError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("cursor is invalid for this graph context") from error
    if not hmac.compare_digest(signature, expected) or not isinstance(decoded, dict):
        raise ValueError("cursor is invalid for this graph context")
    if (
        decoded.get("v") != _CURSOR_VERSION
        or decoded.get("provider") != provider
        or decoded.get("index") != index_fingerprint
        or decoded.get("query") != query_identity
    ):
        raise ValueError("cursor is invalid for this graph context")
    offset = decoded.get("offset")
    page_number = decoded.get("page")
    if (
        isinstance(offset, bool)
        or not isinstance(offset, int)
        or offset < 1
        or isinstance(page_number, bool)
        or not isinstance(page_number, int)
        or page_number < 2
    ):
        raise ValueError("cursor is invalid for this graph context")
    return offset, page_number


def _fragment_chunk(chunk: dict[str, Any], rendered: str) -> list[dict[str, Any]]:
    """Represent one oversized logical record as lossless JSON-text fragments."""

    digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    pieces = [
        rendered[start : start + _FRAGMENT_PAYLOAD_CHARS]
        for start in range(0, len(rendered), _FRAGMENT_PAYLOAD_CHARS)
    ]
    routing = {
        key: chunk[key]
        for key in ("kind", "target", "view", "status", "match_type")
        if key in chunk and isinstance(chunk[key], (str, int, float, bool, type(None)))
    }
    fragments: list[dict[str, Any]] = []
    for index, piece in enumerate(pieces):
        fragment = {
            **routing,
            "transport": "json_fragment",
            "record_sha256": digest,
            "record_chars": len(rendered),
            "fragment_index": index,
            "fragment_count": len(pieces),
            "fragment": piece,
        }
        if len(_canonical_json(fragment)) > _MAX_ATOMIC_CHARS:
            raise RuntimeError("lossless graph fragment exceeds the transport record size")
        fragments.append(fragment)
    return fragments


def _dedupe_chunks(chunks: Iterable[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Deduplicate logical records and losslessly fragment oversized records."""

    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for chunk in chunks:
        rendered = _canonical_json(chunk)
        if rendered in seen:
            continue
        seen.add(rendered)
        if len(rendered) <= _MAX_ATOMIC_CHARS:
            unique.append(chunk)
        else:
            unique.extend(_fragment_chunk(chunk, rendered))
    if not unique:
        unique.append({"kind": "no_match", "reason": "no_relevant_graph_context"})
    return tuple(unique)


def _render_page(
    *,
    chunks: tuple[dict[str, Any], ...],
    start: int,
    end: int,
    page_number: int,
    next_cursor: str | None,
) -> str:
    return _canonical_json(
        {
            "chunks": list(chunks[start:end]),
            "next_cursor": next_cursor,
            "page": {
                "number": page_number,
                "start": start,
                "end": end,
                "total": len(chunks),
            },
        }
    )


def _page(
    chunks: tuple[dict[str, Any], ...],
    *,
    start: int,
    page_number: int,
    max_chars: int,
    provider: str,
    index_fingerprint: str,
    query_identity: str,
) -> tuple[str, dict[str, Any]]:
    if start < 0 or start >= len(chunks):
        raise ValueError("cursor points beyond available graph context")
    end = start
    rendered = ""
    while end < len(chunks):
        candidate_end = end + 1
        candidate_cursor = (
            _encode_cursor(
                provider=provider,
                index_fingerprint=index_fingerprint,
                query_identity=query_identity,
                offset=candidate_end,
                page_number=page_number + 1,
            )
            if candidate_end < len(chunks)
            else None
        )
        candidate = _render_page(
            chunks=chunks,
            start=start,
            end=candidate_end,
            page_number=page_number,
            next_cursor=candidate_cursor,
        )
        if len(candidate) > max_chars:
            break
        rendered = candidate
        end = candidate_end
    if end == start:
        raise RuntimeError("one complete graph record cannot fit the requested page")
    next_cursor = (
        _encode_cursor(
            provider=provider,
            index_fingerprint=index_fingerprint,
            query_identity=query_identity,
            offset=end,
            page_number=page_number + 1,
        )
        if end < len(chunks)
        else None
    )
    rendered = _render_page(
        chunks=chunks,
        start=start,
        end=end,
        page_number=page_number,
        next_cursor=next_cursor,
    )
    return rendered, {
        "number": page_number,
        "start": start,
        "end": end,
        "returned": end - start,
        "total": len(chunks),
        "has_more": next_cursor is not None,
        "max_chars": max_chars,
    }


def _index_fingerprint(metadata: dict[str, Any]) -> str:
    identity = {
        "provider": metadata["provider"],
        "source_head": metadata["source_head"],
        "source_tree": metadata["source_tree"],
        "binary_sha256": metadata["binary_sha256"],
        "index_size_bytes": metadata["index_size_bytes"],
        "package_version": metadata["package_version"],
    }
    return hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()


def _sanitized_call_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    cursor = str(arguments.get("cursor") or "")
    return {
        "targets": list(arguments["targets"]),
        "question": arguments["question"],
        "include_source": arguments["include_source"],
        "breadth": arguments["breadth"],
        "depth": arguments["depth"],
        "max_files": arguments["max_files"],
        "max_chars": arguments["max_chars"],
        "cursor_present": bool(cursor),
        "cursor_sha256": hashlib.sha256(cursor.encode()).hexdigest() if cursor else None,
    }


def _portfolio_runtime(
    *,
    context: AgentContext,
    provider: str,
    graph_repo: Path,
    metadata: dict[str, Any],
    collect: Callable[[dict[str, Any]], list[dict[str, Any]]],
) -> AgentRuntime:
    base_runtime = paged_generic_runtime(context)
    base_profile_id = base_runtime.metadata.get("profile_id")
    base_dependencies = list(base_runtime.metadata.get("dependencies", []))
    for key, value in base_runtime.metadata.items():
        metadata.setdefault(key, value)
    graph_dependency = f"{provider}:{metadata['package_version']}:{metadata['binary_sha256']}"
    dependencies = [*base_dependencies, graph_dependency]
    metadata["base_profile_id"] = base_profile_id
    metadata["profile_id"] = f"{provider}_context"
    metadata["dependencies"] = dependencies
    metadata["dependency_sha256"] = hashlib.sha256(
        _canonical_json(dependencies).encode("utf-8")
    ).hexdigest()
    index_fingerprint = _index_fingerprint(metadata)
    metadata["index_fingerprint"] = index_fingerprint
    metadata["cursor_version"] = _CURSOR_VERSION
    metadata["page_char_range"] = [_MIN_PAGE_CHARS, _MAX_PAGE_CHARS]
    metadata["lossless_oversized_records"] = {
        "encoding": "reassemblable-json-text-fragments",
        "fragment_payload_chars": _FRAGMENT_PAYLOAD_CHARS,
        "record_sha256": True,
        "silent_drop": False,
    }
    metadata["model_visible_scope_records"] = True
    metadata["provider_projection"] = {
        "omitted_keys": sorted(_DROP_JSON_KEYS),
        "reason": (
            "provider instructions/errors/raw content excluded; repository source uses read_file"
        ),
        "oversized_retained": True,
    }
    metadata["tool_surface"] = [GRAPH_CONTEXT_TOOL]
    metadata.setdefault("provider_calls", [])
    cache: dict[str, tuple[dict[str, Any], ...]] = {}

    def graph_context(arguments: dict[str, Any]) -> str:
        started = time.monotonic()
        normalized = _normalized_arguments(arguments)
        query_identity = _query_identity(normalized)
        cursor = str(normalized["cursor"])
        cache_hit = query_identity in cache
        if cursor:
            offset, page_number = _decode_cursor(
                cursor,
                provider=provider,
                index_fingerprint=index_fingerprint,
                query_identity=query_identity,
            )
            if query_identity not in cache:
                raise ValueError("cursor has no live graph context in this run")
        else:
            offset, page_number = 0, 1
            if not cache_hit:
                _assert_visible_repo_isolated(context)
                cache[query_identity] = _dedupe_chunks(collect(normalized))
                _assert_visible_repo_isolated(context)
        rendered, page_info = _page(
            cache[query_identity],
            start=offset,
            page_number=page_number,
            max_chars=int(normalized["max_chars"]),
            provider=provider,
            index_fingerprint=index_fingerprint,
            query_identity=query_identity,
        )
        elapsed = time.monotonic() - started
        metadata["query_calls"].append(
            {
                "provider": provider,
                "tool": GRAPH_CONTEXT_TOOL,
                "arguments": _sanitized_call_arguments(normalized),
                "seconds": round(elapsed, 6),
                "output_chars": len(rendered),
                "error": False,
                "cache_hit": cache_hit,
                "page": page_info,
            }
        )
        return rendered

    extras: ExtraTools = dict(base_runtime.extra_tools)
    extras[GRAPH_CONTEXT_TOOL] = (GRAPH_CONTEXT_DEFINITION, graph_context)
    _assert_visible_repo_isolated(context)

    provider_close = _cleanup_callback(graph_repo, metadata, provider)

    def close() -> None:
        try:
            provider_close()
        finally:
            if base_runtime.close is not None:
                base_runtime.close()

    return AgentRuntime(
        toolbox=base_runtime.toolbox,
        extra_tools=extras,
        finalize=base_runtime.finalize,
        metadata=metadata,
        close=close,
    )


def _record_provider_call(
    *,
    provider: str,
    operation: str,
    argv: list[str],
    cwd: Path,
    env: dict[str, str],
    graph_repo: Path,
    metadata: dict[str, Any],
    arguments: dict[str, Any],
) -> subprocess.CompletedProcess[str]:
    started = time.monotonic()
    completed = _run(argv, cwd=cwd, env=env)
    elapsed = time.monotonic() - started
    sanitized_stdout = _sanitized(completed.stdout, graph_repo)
    metadata["provider_calls"].append(
        {
            "provider": provider,
            "operation": operation,
            "arguments": dict(arguments),
            "seconds": round(elapsed, 6),
            "exit_code": completed.returncode,
            "output_chars": len(sanitized_stdout),
            "error": completed.returncode != 0,
        }
    )
    return completed


def _json_result(
    *,
    provider: str,
    operation: str,
    argv: list[str],
    cwd: Path,
    env: dict[str, str],
    graph_repo: Path,
    metadata: dict[str, Any],
    arguments: dict[str, Any],
) -> tuple[object | None, bool]:
    completed = _record_provider_call(
        provider=provider,
        operation=operation,
        argv=argv,
        cwd=cwd,
        env=env,
        graph_repo=graph_repo,
        metadata=metadata,
        arguments=arguments,
    )
    if completed.returncode != 0:
        return None, False
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None, False
    if isinstance(payload, dict) and payload.get("error"):
        return payload, False
    return payload, True


def _symbol_projection(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    aliases = {
        "filePath": "path",
        "startLine": "line_start",
        "endLine": "line_end",
        "qualifiedName": "qualified_name",
        "relationType": "relation",
    }
    allowed = (
        "name",
        "qualifiedName",
        "kind",
        "type",
        "filePath",
        "startLine",
        "endLine",
        "signature",
        "depth",
        "relationType",
        "confidence",
        "risk",
        "step_index",
        "step_count",
        "process_type",
        "symbol_count",
    )
    result: dict[str, Any] = {}
    for key in allowed:
        item = value.get(key)
        if item is not None and item != "":
            result[aliases.get(key, key)] = item
    return result


def _source_chunks(
    graph_repo: Path,
    *,
    target: str,
    symbol: object,
) -> list[dict[str, Any]]:
    projected = _symbol_projection(symbol)
    raw_path = projected.get("path")
    raw_start = projected.get("line_start")
    raw_end = projected.get("line_end")
    if not isinstance(raw_path, str) or not isinstance(raw_start, int):
        return []
    end = raw_end if isinstance(raw_end, int) and raw_end >= raw_start else raw_start
    root = graph_repo.resolve()
    candidate = (root / raw_path).resolve()
    if candidate != root and root not in candidate.parents:
        return []
    if not candidate.is_file():
        return []
    lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
    chunks: list[dict[str, Any]] = []
    for line_number in range(max(1, raw_start), min(end, len(lines)) + 1):
        text = lines[line_number - 1]
        chunk: dict[str, Any] = {
            "kind": "source",
            "target": target,
            "path": raw_path,
            "line": line_number,
            "text": text,
        }
        if len(_canonical_json(chunk)) > _MAX_ATOMIC_CHARS:
            chunk = {
                "kind": "source_reference",
                "target": target,
                "path": raw_path,
                "line": line_number,
                "chars": len(text),
                "sha256": hashlib.sha256(text.encode()).hexdigest(),
                "read_with": "read_file",
            }
        chunks.append(chunk)
    return chunks


def _exact_codegraph_matches(payload: object, target: str) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        return []
    matches: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict) or not isinstance(item.get("node"), dict):
            continue
        node = item["node"]
        names = {
            str(node.get("name") or ""),
            str(node.get("qualifiedName") or ""),
            str(node.get("filePath") or ""),
            Path(str(node.get("filePath") or ".")).name,
        }
        if target in names:
            matches.append(node)
    return matches


def _codegraph_explore_chunks(text: str, *, include_source: bool) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    source_path: str | None = None
    in_fence = False
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        heading = _SOURCE_HEADING.match(line)
        if heading:
            source_path = heading.group(1)
            continue
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence and include_source and source_path is not None:
            match = _SOURCE_LINE.match(line)
            if match:
                line_number = int(match.group(1))
                source_text = match.group(2)
                chunk: dict[str, Any] = {
                    "kind": "source",
                    "path": source_path,
                    "line": line_number,
                    "text": source_text,
                }
                if len(_canonical_json(chunk)) > _MAX_ATOMIC_CHARS:
                    chunk = {
                        "kind": "source_reference",
                        "path": source_path,
                        "line": line_number,
                        "chars": len(source_text),
                        "sha256": hashlib.sha256(source_text.encode()).hexdigest(),
                        "read_with": "read_file",
                    }
                chunks.append(chunk)
            continue
        if line.startswith("- `"):
            chunk = {"kind": "relationship", "detail": line[2:].strip()}
            chunks.append(chunk)
    return chunks


def _codegraph_collector(
    *,
    binary: Path,
    graph_repo: Path,
    env: dict[str, str],
    metadata: dict[str, Any],
) -> Callable[[dict[str, Any]], list[dict[str, Any]]]:
    def collect(arguments: dict[str, Any]) -> list[dict[str, Any]]:
        exact_chunks: list[dict[str, Any]] = []
        scope_chunks: list[dict[str, Any]] = []
        context_chunks: list[dict[str, Any]] = []
        matched_targets: list[str] = []
        breadth = int(arguments["breadth"])
        for target in arguments["targets"]:
            payload, ok = _json_result(
                provider="codegraph",
                operation="exact_query",
                argv=[
                    str(binary),
                    "query",
                    target,
                    "--path",
                    str(graph_repo),
                    "--limit",
                    str(breadth),
                    "--json",
                ],
                cwd=graph_repo,
                env=env,
                graph_repo=graph_repo,
                metadata=metadata,
                arguments={"target": target, "breadth": breadth},
            )
            if not ok:
                exact_chunks.append(
                    {
                        "kind": "no_match",
                        "target": target,
                        "view": "exact_lookup",
                        "status": "query_error",
                        "reason": "query_error",
                    }
                )
                scope_chunks.append(
                    {
                        "kind": "graph_scope",
                        "target": target,
                        "view": "exact_lookup",
                        "breadth": breadth,
                        "pagination": "unavailable",
                        "complete": False,
                    }
                )
                continue
            matches = _exact_codegraph_matches(payload, target)
            raw_result_count = len(payload) if isinstance(payload, list) else 0
            scope_chunks.append(
                {
                    "kind": "graph_scope",
                    "target": target,
                    "view": "exact_lookup",
                    "breadth": breadth,
                    "provider_results": raw_result_count,
                    "pagination": "unavailable",
                    "complete": raw_result_count < breadth,
                    "possible_truncation": raw_result_count >= breadth,
                }
            )
            if not matches:
                exact_chunks.append(
                    {
                        "kind": "no_match",
                        "target": target,
                        "view": "exact_lookup",
                        "status": "no_match",
                        "reason": "not_indexed",
                    }
                )
                continue
            matched_targets.append(target)
            for node in matches:
                exact_chunks.append(
                    {
                        "kind": "match",
                        "target": target,
                        "view": "exact_lookup",
                        "status": "exact_match",
                        "match_type": "exact",
                        "symbol": _symbol_projection(node),
                    }
                )
        if not matched_targets:
            return exact_chunks + scope_chunks
        query_parts = list(matched_targets)
        if arguments["question"]:
            query_parts.append(str(arguments["question"]))
        completed = _record_provider_call(
            provider="codegraph",
            operation="explore",
            argv=[
                str(binary),
                "explore",
                "--path",
                str(graph_repo),
                "--max-files",
                str(arguments["max_files"]),
                " ".join(query_parts),
            ],
            cwd=graph_repo,
            env=env,
            graph_repo=graph_repo,
            metadata=metadata,
            arguments={
                "targets": matched_targets,
                "question": arguments["question"],
                "max_files": arguments["max_files"],
            },
        )
        context_scope = {
            "kind": "graph_scope",
            "targets": matched_targets,
            "view": "context",
            "max_files": arguments["max_files"],
            "file_pagination": "unavailable",
            "relationship_depth": "provider_managed_not_adjustable",
            "requested_depth": arguments["depth"],
            "request_succeeded": completed.returncode == 0,
            "complete": False,
            "possible_file_truncation": True,
        }
        scope_chunks.append(context_scope)
        if completed.returncode != 0:
            context_chunks.append(
                {
                    "kind": "no_match",
                    "targets": matched_targets,
                    "view": "context",
                    "status": "context_unavailable",
                    "reason": "context_unavailable",
                }
            )
            return exact_chunks + scope_chunks + context_chunks
        sanitized = _sanitized(completed.stdout, graph_repo)
        context_chunks.extend(
            _codegraph_explore_chunks(
                sanitized,
                include_source=bool(arguments["include_source"]),
            )
        )
        return exact_chunks + scope_chunks + context_chunks

    return collect


def _project_json(value: object) -> object:
    if isinstance(value, dict):
        return {
            str(key): _project_json(item)
            for key, item in value.items()
            if str(key).lower() not in _DROP_JSON_KEYS
        }
    if isinstance(value, list):
        return [_project_json(item) for item in value]
    return value


def _json_semantic_chunks(
    payload: object,
    *,
    view: str,
    target: str,
    path: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """Flatten JSON without dropping oversized primitives or scalar leaves.

    Oversized records intentionally remain oversized here.  The common
    transport layer fragments their canonical JSON losslessly after semantic
    collection and deduplication.
    """

    projected = _project_json(payload)
    if isinstance(projected, list):
        chunks: list[dict[str, Any]] = []
        for index, item in enumerate(projected):
            chunks.extend(
                _json_semantic_chunks(
                    item,
                    view=view,
                    target=target,
                    path=(*path, str(index)),
                )
            )
        return chunks
    if not isinstance(projected, dict):
        chunk = {
            "kind": "graph_detail",
            "target": target,
            "view": view,
            "path": ".".join(path),
            "value": projected,
        }
        return [chunk]

    scalar: dict[str, Any] = {}
    nested: list[tuple[str, object]] = []
    for key, value in projected.items():
        if isinstance(value, (dict, list)):
            nested.append((key, value))
        else:
            scalar[key] = value
    chunks = []
    if scalar:
        chunk = {
            "kind": "graph_detail",
            "target": target,
            "view": view,
            "path": ".".join(path),
            "value": scalar,
        }
        if len(_canonical_json(chunk)) > _MAX_ATOMIC_CHARS:
            for key, value in scalar.items():
                chunks.append(
                    {
                        "kind": "graph_detail",
                        "target": target,
                        "view": view,
                        "path": ".".join((*path, key)),
                        "value": value,
                    }
                )
        else:
            chunks.append(chunk)
    for key, value in nested:
        chunks.extend(
            _json_semantic_chunks(
                value,
                view=view,
                target=target,
                path=(*path, key),
            )
        )
    return chunks


def _symbols_in_payload(payload: object) -> list[dict[str, Any]]:
    symbols: list[dict[str, Any]] = []

    def visit(value: object) -> None:
        if isinstance(value, dict):
            if isinstance(value.get("filePath"), str) and isinstance(value.get("startLine"), int):
                symbols.append(value)
            for key, item in value.items():
                if str(key).lower() not in _DROP_JSON_KEYS:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(payload)
    return symbols


def _exact_gitnexus_symbols(payload: object, target: str) -> list[dict[str, Any]]:
    normalized = target.strip().casefold()
    matches: list[dict[str, Any]] = []
    for symbol in _symbols_in_payload(payload):
        path = str(symbol.get("filePath") or "")
        candidates = {
            str(symbol.get("name") or "").casefold(),
            str(symbol.get("qualifiedName") or "").casefold(),
            path.casefold(),
            Path(path or ".").name.casefold(),
        }
        if normalized in candidates:
            matches.append(symbol)
    return matches


def _impact_result_records(payload: object) -> list[dict[str, Any]]:
    """Extract relationship identities used to exhaust GitNexus offset pages."""

    projected = _project_json(payload)
    records: list[dict[str, Any]] = []
    found_by_depth = False

    def visit_by_depth(value: object) -> None:
        nonlocal found_by_depth
        if isinstance(value, dict):
            for key, item in value.items():
                if key.casefold() == "bydepth" and isinstance(item, dict):
                    found_by_depth = True
                    for depth, raw_results in item.items():
                        if isinstance(raw_results, list):
                            for raw_result in raw_results:
                                records.append({"depth": str(depth), "result": raw_result})
                else:
                    visit_by_depth(item)
        elif isinstance(value, list):
            for item in value:
                visit_by_depth(item)

    visit_by_depth(projected)
    if found_by_depth:
        return records

    def visit_relationships(value: object) -> None:
        if isinstance(value, dict):
            keys = {str(key).casefold() for key in value}
            if (
                "depth" in keys
                and ({"name", "uid", "filepath"} & keys)
                and ({"relationtype", "relation", "type"} & keys)
            ):
                records.append(dict(value))
                return
            for item in value.values():
                visit_relationships(item)
        elif isinstance(value, list):
            for item in value:
                visit_relationships(item)

    visit_relationships(projected)
    return records


def _gitnexus_collector(
    *,
    binary: Path,
    graph_repo: Path,
    env: dict[str, str],
    metadata: dict[str, Any],
) -> Callable[[dict[str, Any]], list[dict[str, Any]]]:
    def invoke_json(
        operation: str,
        argv: list[str],
        normalized: dict[str, Any],
    ) -> tuple[object | None, bool]:
        return _json_result(
            provider="gitnexus",
            operation=operation,
            argv=[str(binary), *argv],
            cwd=graph_repo,
            env=env,
            graph_repo=graph_repo,
            metadata=metadata,
            arguments=normalized,
        )

    def collect(arguments: dict[str, Any]) -> list[dict[str, Any]]:
        targets = list(arguments["targets"])
        exact_chunks: list[dict[str, Any]] = []
        scope_chunks: list[dict[str, Any]] = []
        detail_chunks: list[dict[str, Any]] = []
        collected_source: list[dict[str, Any]] = []
        source_symbols: list[tuple[str, dict[str, Any]]] = []
        breadth = int(arguments["breadth"])
        depth = int(arguments["depth"])
        for target in targets:
            context_payload, context_ok = invoke_json(
                "context",
                ["context", target, "--limit", str(breadth)],
                {"target": target, "breadth": breadth},
            )
            exact_symbols = _exact_gitnexus_symbols(context_payload, target) if context_ok else []
            exact_view = "context"
            exact_payload = context_payload
            if not (context_ok and exact_symbols):
                query_text = " ".join((target, str(arguments["question"]))).strip()
                query_payload, query_ok = invoke_json(
                    "query",
                    ["query", query_text, "--limit", str(breadth)],
                    {
                        "target": target,
                        "question": arguments["question"],
                        "breadth": breadth,
                    },
                )
                query_matches = _exact_gitnexus_symbols(query_payload, target) if query_ok else []
                if query_ok and query_matches:
                    exact_symbols = query_matches
                    exact_payload = query_payload
                    exact_view = "query"
                else:
                    exact_chunks.append(
                        {
                            "kind": "no_match",
                            "target": target,
                            "view": "exact_lookup",
                            "status": "no_match",
                            "reason": "not_indexed",
                        }
                    )
                    scope_chunks.append(
                        {
                            "kind": "graph_scope",
                            "target": target,
                            "view": "exact_lookup",
                            "breadth": breadth,
                            "operations": ["context", "query"],
                            "pagination": "unavailable",
                            "complete": False,
                            "possible_truncation": True,
                        }
                    )
                    continue

            exact_symbol = exact_symbols[0]
            exact_chunks.append(
                {
                    "kind": "match",
                    "target": target,
                    "view": exact_view,
                    "status": "exact_match",
                    "match_type": "exact",
                    "symbol": _symbol_projection(exact_symbol),
                }
            )
            source_symbols.append((target, exact_symbol))
            returned_symbols = len(_symbols_in_payload(exact_payload))
            scope_chunks.append(
                {
                    "kind": "graph_scope",
                    "target": target,
                    "view": exact_view,
                    "breadth": breadth,
                    "returned_symbols": returned_symbols,
                    "pagination": "unavailable",
                    "request_succeeded": True,
                    "complete": False,
                    "possible_truncation": True,
                    "max_files": "not_applicable",
                }
            )
            detail_chunks.extend(
                _json_semantic_chunks(exact_payload, view=exact_view, target=target)
            )

            offset = 0
            impact_pages = 0
            impact_ok = True
            exhausted = False
            seen_results: set[str] = set()
            while True:
                impact_payload, page_ok = invoke_json(
                    "impact",
                    [
                        "impact",
                        target,
                        "--direction",
                        "upstream",
                        "--depth",
                        str(depth),
                        "--limit",
                        str(breadth),
                        "--offset",
                        str(offset),
                    ],
                    {
                        "target": target,
                        "direction": "upstream",
                        "depth": depth,
                        "breadth": breadth,
                        "offset": offset,
                    },
                )
                impact_pages += 1
                if not page_ok:
                    impact_ok = False
                    detail_chunks.append(
                        {
                            "kind": "no_match",
                            "target": target,
                            "view": "impact",
                            "status": "impact_unavailable",
                            "reason": "impact_unavailable",
                        }
                    )
                    break
                detail_chunks.extend(
                    _json_semantic_chunks(impact_payload, view="impact", target=target)
                )
                page_results = _impact_result_records(impact_payload)
                novel = [
                    record for record in page_results if _canonical_json(record) not in seen_results
                ]
                if not novel:
                    exhausted = True
                    break
                seen_results.update(_canonical_json(record) for record in novel)
                offset += breadth
            scope_chunks.append(
                {
                    "kind": "graph_scope",
                    "target": target,
                    "view": "impact",
                    "direction": "upstream",
                    "depth": depth,
                    "breadth_per_depth_page": breadth,
                    "pagination": "offset_until_no_new_result",
                    "pages_requested": impact_pages,
                    "unique_relationships": len(seen_results),
                    "complete": impact_ok and exhausted,
                }
            )
        if arguments["include_source"]:
            for target, symbol in source_symbols:
                collected_source.extend(_source_chunks(graph_repo, target=target, symbol=symbol))
        return exact_chunks + scope_chunks + detail_chunks + collected_source

    return collect


def codegraph_context_runtime(context: AgentContext) -> AgentRuntime:
    """Build the pinned isolated index and expose exact-query-gated context."""

    _assert_visible_repo_isolated(context)
    binary, digest = _binary("CODEGRAPH_BIN", "codegraph", CODEGRAPH_VERSION)
    graph_repo, clone_seconds = _clone_for_index(context, "codegraph")
    env = dict(os.environ)
    env.update({"DO_NOT_TRACK": "1", "NO_COLOR": "1", "CODEGRAPH_NO_DOWNLOAD": "1"})
    index_started = time.monotonic()
    initialized = _run([str(binary), "init", str(graph_repo)], cwd=graph_repo, env=env)
    index_seconds = time.monotonic() - index_started
    if initialized.returncode != 0:
        detail = initialized.stderr.strip() or initialized.stdout.strip() or "index setup failed"
        raise RuntimeError(_sanitized(detail, graph_repo))
    index_dir = graph_repo / ".codegraph"
    status = _run([str(binary), "status", "--json", str(graph_repo)], cwd=graph_repo, env=env)
    if status.returncode != 0:
        raise RuntimeError("graph index status failed after setup")
    try:
        status_payload: object = json.loads(status.stdout)
    except json.JSONDecodeError:
        status_payload = {"status": "available"}
    metadata = _metadata(
        provider="codegraph",
        context=context,
        package_version=CODEGRAPH_VERSION,
        binary_sha256=digest,
        isolation_clone_seconds=clone_seconds,
        index_seconds=index_seconds,
        index_success=index_dir.is_dir(),
        index_size_bytes=_directory_size(index_dir),
        index_stats=status_payload,
    )
    metadata.update(
        {
            "telemetry_disabled": True,
            "update_checks_disabled": True,
            "package_integrity": CODEGRAPH_PACKAGE_INTEGRITY,
            "implementation_mode": "exact-query-gated-explore-explicit-scope",
            "provider_scope_controls": {
                "exact_lookup": {
                    "breadth_argument": "breadth",
                    "default": _DEFAULT_BREADTH,
                    "pagination": "unavailable",
                },
                "context": {
                    "max_files_argument": "max_files",
                    "default": _DEFAULT_MAX_FILES,
                    "maximum": _MAX_MAX_FILES,
                    "pagination": "unavailable",
                },
                "relationship_depth": "provider_managed_not_adjustable",
            },
        }
    )
    metadata["provider_calls"] = []
    return _portfolio_runtime(
        context=context,
        provider="codegraph",
        graph_repo=graph_repo,
        metadata=metadata,
        collect=_codegraph_collector(
            binary=binary,
            graph_repo=graph_repo,
            env=env,
            metadata=metadata,
        ),
    )


def gitnexus_context_runtime(context: AgentContext) -> AgentRuntime:
    """Build the pinned isolated index and expose query+context+impact context."""

    _assert_visible_repo_isolated(context)
    binary, digest = _binary("GITNEXUS_BIN", "gitnexus", GITNEXUS_VERSION)
    graph_repo, clone_seconds = _clone_for_index(context, "gitnexus")
    if (graph_repo / ".gitnexusrc").exists():
        raise RuntimeError("benchmark graph clone contains .gitnexusrc; fixed config required")
    gitnexus_home = graph_repo.parent / "home"
    gitnexus_home.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update(
        {
            "GITNEXUS_HOME": str(gitnexus_home),
            "GITNEXUS_LBUG_EXTENSION_INSTALL": "load-only",
            "GITNEXUS_LOG_LEVEL": "error",
            "NO_COLOR": "1",
        }
    )
    index_started = time.monotonic()
    initialized = _run(
        [str(binary), "analyze", str(graph_repo), "--index-only", "--no-stats"],
        cwd=graph_repo,
        env=env,
    )
    index_seconds = time.monotonic() - index_started
    if initialized.returncode != 0:
        detail = initialized.stderr.strip() or initialized.stdout.strip() or "index setup failed"
        raise RuntimeError(_sanitized(detail, graph_repo))
    index_dir = graph_repo / ".gitnexus"
    try:
        meta_payload = json.loads((index_dir / "meta.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("graph index metadata is missing or invalid") from error
    capabilities = meta_payload.get("capabilities", {})
    graph_status = capabilities.get("graph", {}).get("status")
    fts_status = capabilities.get("fts", {}).get("status")
    stats = meta_payload.get("stats", {})
    if (
        meta_payload.get("lastCommit") != context.head_revision
        or graph_status != "available"
        or fts_status != "available"
        or stats.get("embeddings") != 0
    ):
        raise RuntimeError("graph index capabilities are incomplete")
    metadata = _metadata(
        provider="gitnexus",
        context=context,
        package_version=GITNEXUS_VERSION,
        binary_sha256=digest,
        isolation_clone_seconds=clone_seconds,
        index_seconds=index_seconds,
        index_success=index_dir.is_dir(),
        index_size_bytes=_directory_size(index_dir),
        index_stats={
            "lastCommit": meta_payload.get("lastCommit"),
            "stats": stats,
            "capabilities": capabilities,
            "schemaVersion": meta_payload.get("schemaVersion"),
        },
    )
    metadata.update(
        {
            "registry_home_isolated": True,
            "wrapper_read_only_allowlist": True,
            "fts_status": fts_status,
            "graph_status": graph_status,
            "fts_extension_policy": "load-only",
            "embeddings_enabled": False,
            "package_integrity": GITNEXUS_PACKAGE_INTEGRITY,
            "implementation_mode": "exact-context-impact-offset-exhaustive-query-fallback",
            "provider_scope_controls": {
                "context_and_query": {
                    "breadth_argument": "breadth",
                    "default": _DEFAULT_BREADTH,
                    "pagination": "unavailable",
                },
                "impact": {
                    "breadth_per_depth_argument": "breadth",
                    "depth_argument": "depth",
                    "pagination": "offset_until_no_new_result",
                    "cumulative_page_or_node_limit": None,
                },
                "max_files": "not_applicable",
            },
        }
    )
    metadata["provider_calls"] = []
    return _portfolio_runtime(
        context=context,
        provider="gitnexus",
        graph_repo=graph_repo,
        metadata=metadata,
        collect=_gitnexus_collector(
            binary=binary,
            graph_repo=graph_repo,
            env=env,
            metadata=metadata,
        ),
    )


__all__ = [
    "CODEGRAPH_CONTEXT_AGENT",
    "GITNEXUS_CONTEXT_AGENT",
    "GRAPH_CONTEXT_DEFINITION",
    "GRAPH_CONTEXT_TOOL",
    "GRAPH_CONTEXT_TOOLS",
    "PORTFOLIO_PROTOCOL_VERSION",
    "PORTFOLIO_TOOL_MENU",
    "codegraph_context_runtime",
    "gitnexus_context_runtime",
]

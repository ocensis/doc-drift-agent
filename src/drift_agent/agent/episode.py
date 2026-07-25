"""Bounded agentic episodes: a read-only, path-jailed tool loop over one repo snapshot.

An episode gives the model a small set of read-only repository tools plus a
mandatory ``submit`` tool, runs at most ``max_turns`` assistant turns, and
returns the validated submit payload. The transport stays single-POST per
turn; the loop, budget accounting, retries, and the tool jail live here.

Repository content read through the tools is untrusted data. The jail is the
hard boundary: tools resolve repo-relative paths only, never write, and never
touch the network.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ValidationError

from drift_agent.agent.budget import BudgetLedger
from drift_agent.model import observability as _obs
from drift_agent.model.client import ModelClientError
from drift_agent.model.contracts import ModelCallUsage, ModelProfile
from drift_agent.model.openrouter import ChatToolChoice, ChatTurnResult

_MAX_READ_LINES = 400
_MAX_TOOL_RESULT_CHARS = 20_000
_MAX_GREP_MATCHES = 60
_MAX_GREP_FILES = 2_000
_MAX_GREP_FILE_BYTES = 1_048_576
_MAX_LIST_ENTRIES = 200
_RETRYABLE_REASONS = frozenset(
    {
        "rate_limited",
        "provider_unavailable",
        "provider_timeout",
        "provider_conflict",
        "timeout",
        "transport_error",
    }
)
_SUBMIT_TOOL = "submit"

# Gateway instability (Cloudflare 403 bursts, wave-shaped provider outages)
# needs tens of seconds per retry, not the 2-4s exponential base.
_SLOW_RETRY_REASONS = frozenset({"request_forbidden", "provider_unavailable"})


def _retry_delay(reason_code: str, attempt: int, base_seconds: float) -> float:
    delay = float(base_seconds * (2**attempt))
    if reason_code in _SLOW_RETRY_REASONS:
        delay = max(delay, 30.0 * (attempt + 1))
    return delay


# The OpenClaw edge 502s any request body over ~128KiB. Keep a margin for
# headers and serialization differences and shrink conversations to fit.
_MAX_REQUEST_BYTES = 118_000
_TRIM_STUB = (
    "[historical tool result omitted from this request projection; do not repeat the "
    "identical call because it will return only a replay receipt; use durable state or "
    "make a different, narrower observation if more evidence is needed]"
)
_KEEP_RECENT_MESSAGES = 8
_MAX_RETAINED_COMPLETED_ARGUMENT_BYTES = 4_000
# Once the provider's high-water envelope is crossed, compact substantially
# below it.  Merely shaving a request back to 118KiB makes every subsequent
# turn resend an expensive near-limit prompt and immediately compact again.
# This is a transport-projection watermark, not an Agent, turn, or token limit.
_REQUEST_COMPACTION_LOW_WATER_BYTES = 72_000
_REQUEST_COMPACTION_LOW_WATER_RATIO = 0.65


class RequestBodyTooLargeError(ValueError):
    """Conversation projection cannot fit the provider request envelope."""

    def __init__(self, *, actual_bytes: int, limit: int) -> None:
        super().__init__(
            f"request body remains {actual_bytes} bytes after safe compaction (limit {limit} bytes)"
        )
        self.actual_bytes = actual_bytes
        self.limit = limit


def request_projection_size_bytes(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> int:
    """Return the canonical UTF-8 size of the transport input projection.

    This measures exactly the ``messages`` and ``tools`` values supplied by an
    episode to ``ChatTransport``.  It intentionally does not claim to be the
    complete HTTP request body: a concrete transport may add model, routing,
    generation, or provider-specific envelope fields after this boundary.

    Args:
        messages: The compacted conversation passed to the transport.
        tools: The tool definitions passed alongside the conversation.

    Returns:
        The deterministic canonical JSON projection size in UTF-8 bytes.
    """

    return len(
        json.dumps(
            {"messages": messages, "tools": tools},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _body_bytes(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> int:
    return request_projection_size_bytes(messages, tools)


def _request_compaction_target(limit: int) -> int:
    """Return the low-water target after a request crosses ``limit``."""

    return min(
        _REQUEST_COMPACTION_LOW_WATER_BYTES,
        max(1, int(limit * _REQUEST_COMPACTION_LOW_WATER_RATIO)),
    )


def _argument_bytes(arguments: object) -> int:
    if isinstance(arguments, str):
        return len(arguments.encode("utf-8"))
    return len(
        json.dumps(
            arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _canonicalize_empty_assistant_tool_calls(
    messages: list[dict[str, Any]],
) -> None:
    """Remove provider-invalid empty tool-call fields before every request.

    Model responses occasionally include ``tool_calls: []``.  An assistant
    message with real prose is canonical once that empty field is omitted.  An
    assistant message with neither content nor calls must be removed entirely;
    provider-specific ``reasoning_details`` do not satisfy the public chat
    schema's content requirement.
    """

    discarded_messages: set[int] = set()
    for message in messages[1:]:
        if message.get("role") != "assistant" or message.get("tool_calls") != []:
            continue
        message.pop("tool_calls", None)
        content = message.get("content")
        if content is None or content == "":
            discarded_messages.add(id(message))
        else:
            # Once the empty tool-call envelope is gone, keep only portable
            # assistant prose.  DeepSeek rejects a replayed assistant prefill
            # carrying provider-specific reasoning details but no real calls.
            message.pop("reasoning_details", None)
            message.pop("reasoning_content", None)
    if discarded_messages:
        messages[:] = [message for message in messages if id(message) not in discarded_messages]


def _drop_oversized_completed_tool_pairs(messages: list[dict[str, Any]]) -> None:
    """Drop both halves of oversized historical calls after they complete.

    Replacing arguments with an internal JSON sentinel is unsafe because that
    sentinel becomes a model-visible example of how to call the tool.  Some
    providers faithfully repeat it, producing a schema-error loop.  A completed
    call and its matching response may instead be removed together from the
    transport projection while the persistent transcript and telemetry remain
    unchanged.
    """

    response_ids = {
        str(message.get("tool_call_id"))
        for message in messages
        if message.get("role") == "tool" and message.get("tool_call_id") is not None
    }
    dropped_ids: set[str] = set()
    discarded_assistant_messages: set[int] = set()
    for message in messages[1:]:
        if message.get("role") != "assistant":
            continue
        calls = message.get("tool_calls")
        if not isinstance(calls, list):
            continue
        retained: list[object] = []
        dropped_from_message = False
        for call in calls:
            if not isinstance(call, dict):
                retained.append(call)
                continue
            call_id = str(call.get("id"))
            if call_id not in response_ids:
                retained.append(call)
                continue
            function = call.get("function")
            if not isinstance(function, dict):
                retained.append(call)
                continue
            arguments = function.get("arguments")
            if _argument_bytes(arguments) > _MAX_RETAINED_COMPLETED_ARGUMENT_BYTES:
                dropped_ids.add(call_id)
                dropped_from_message = True
            else:
                retained.append(call)
        if retained:
            message["tool_calls"] = retained
        else:
            # Never emit ``tool_calls: []``.  If compaction removed this
            # assistant's complete call set, discard the whole source message,
            # including any accompanying prose or reasoning.  Keeping that prose
            # can leave the next request ending in an assistant-only prefill,
            # which is not portable across OpenAI-compatible providers.  The
            # persistent transcript still retains the original message.
            message.pop("tool_calls", None)
            content = message.get("content")
            if dropped_from_message or content is None or content == "":
                discarded_assistant_messages.add(id(message))

    if not dropped_ids and not discarded_assistant_messages:
        return
    compacted: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") == "tool" and str(message.get("tool_call_id")) in dropped_ids:
            continue
        if message.get("role") == "assistant" and id(message) in discarded_assistant_messages:
            continue
        compacted.append(message)
    messages[:] = compacted


def _canonical_message_bytes(message: dict[str, Any]) -> int:
    """Return one message's size inside the canonical request projection."""

    return len(
        json.dumps(
            message,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _old_completed_tool_groups(
    messages: list[dict[str, Any]],
    *,
    before: int,
) -> list[tuple[int, ...]]:
    """Return removable, provider-valid historical tool interaction groups.

    Each returned group contains one assistant message and exactly one later
    tool response for every call in that message.  Ambiguous call IDs, partial
    interactions, and groups crossing the protected recent suffix are excluded.
    Removing a whole group therefore cannot create an orphaned tool call or tool
    response in a valid input transcript.
    """

    assistant_indices_by_call_id: dict[str, list[int]] = {}
    response_indices_by_call_id: dict[str, list[int]] = {}
    for index, message in enumerate(messages):
        if message.get("role") == "assistant":
            calls = message.get("tool_calls")
            if not isinstance(calls, list):
                continue
            for call in calls:
                if not isinstance(call, dict):
                    continue
                call_id = call.get("id")
                if not isinstance(call_id, str) or not call_id:
                    continue
                assistant_indices_by_call_id.setdefault(call_id, []).append(index)
        elif message.get("role") == "tool":
            call_id = message.get("tool_call_id")
            if isinstance(call_id, str) and call_id:
                response_indices_by_call_id.setdefault(call_id, []).append(index)

    groups: list[tuple[int, ...]] = []
    for assistant_index, message in enumerate(messages[:before]):
        if message.get("role") != "assistant":
            continue
        calls = message.get("tool_calls")
        if not isinstance(calls, list) or not calls:
            continue
        call_ids: list[str] = []
        for call in calls:
            if not isinstance(call, dict):
                break
            call_id = call.get("id")
            if not isinstance(call_id, str) or not call_id or call_id in call_ids:
                break
            call_ids.append(call_id)
        else:
            response_indices: list[int] = []
            for call_id in call_ids:
                assistant_indices = assistant_indices_by_call_id.get(call_id, [])
                responses = response_indices_by_call_id.get(call_id, [])
                if assistant_indices != [assistant_index] or len(responses) != 1:
                    break
                response_index = responses[0]
                if response_index <= assistant_index or response_index >= before:
                    break
                response_indices.append(response_index)
            else:
                groups.append((assistant_index, *response_indices))
    return groups


def _drop_old_completed_tool_groups_to_fit(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    limit: int,
) -> None:
    """Drop the oldest completed tool groups until the projection fits.

    This is deliberately a last-resort, transport-only projection compaction.
    It keeps the recent suffix byte-for-byte intact and removes both sides of a
    historical tool interaction as one unit.  The trade-off is loss of
    model-visible memory: a model may repeat an omitted call.  Persistent tool
    state, deterministic replay receipts, and protocol no-progress detection
    remain the safeguards; the durable transcript and telemetry are untouched
    because session callers compact a defensive request copy.
    """

    actual = _body_bytes(messages, tools)
    if actual <= limit:
        return
    before = max(1, len(messages) - _KEEP_RECENT_MESSAGES)
    discarded_indices: set[int] = set()
    # The canonical messages array retains at least the system message.  For
    # each removed element its encoded object and one list comma disappear, so
    # this accounting is exact and avoids serializing a long history hundreds
    # of times.
    for group in _old_completed_tool_groups(messages, before=before):
        new_indices = tuple(index for index in group if index not in discarded_indices)
        if not new_indices:
            continue
        discarded_indices.update(new_indices)
        actual -= sum(_canonical_message_bytes(messages[index]) + 1 for index in new_indices)
        if actual <= limit:
            break
    if discarded_indices:
        messages[:] = [
            message for index, message in enumerate(messages) if index not in discarded_indices
        ]


def shrink_messages(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    limit: int = _MAX_REQUEST_BYTES,
) -> None:
    """Shrink a conversation in place until its request body fits ``limit``.

    Old tool results are stubbed out first.  Exceptionally large completed calls
    are removed together with their paired responses, then old completed tool
    interactions are removed whole while the recent suffix remains unchanged.
    Once the high-water ``limit`` is crossed, compaction aims for a lower
    watermark so later turns do not repeatedly send near-limit prompts.  These
    are transport-only projection changes, not an Agent/turn/token budget.  If
    safe compaction still cannot fit the envelope, an explicit
    ``RequestBodyTooLargeError`` is raised instead of sending an oversized body.
    """

    # Canonicalization is required even when the request is already under the
    # byte ceiling: ``tool_calls: []`` is rejected by some providers.
    _canonicalize_empty_assistant_tool_calls(messages)
    if _body_bytes(messages, tools) <= limit:
        return
    target = _request_compaction_target(limit)
    cutoff = max(1, len(messages) - _KEEP_RECENT_MESSAGES)
    for message in messages[1:cutoff]:
        content = message.get("content")
        if (
            message.get("role") == "tool"
            and isinstance(content, str)
            and content != _TRIM_STUB
            and len(content) > len(_TRIM_STUB)
        ):
            message["content"] = _TRIM_STUB
            if _body_bytes(messages, tools) <= target:
                _drop_oversized_completed_tool_pairs(messages)
                return
    _drop_oversized_completed_tool_pairs(messages)
    if _body_bytes(messages, tools) <= target:
        return
    _drop_old_completed_tool_groups_to_fit(messages, tools, limit=target)
    if _body_bytes(messages, tools) <= target:
        return
    # Low water is only a target for semantics-safe historical tool-pair
    # compaction.  Once the request fits the provider envelope, do not destroy
    # arbitrary conversation text merely to reach that optimization target.
    if _body_bytes(messages, tools) <= limit:
        return
    while _body_bytes(messages, tools) > limit:
        candidates = [
            message
            # The system prompt and initial user assignment are irreducible.
            # Only later, non-recent text may use the legacy truncation fallback.
            for message in messages[2:-2]
            if isinstance(message.get("content"), str) and len(str(message.get("content"))) > 4_000
        ]
        if not candidates:
            break
        biggest = max(candidates, key=lambda message: len(str(message["content"])))
        text = str(biggest["content"])
        biggest["content"] = text[:2_000] + "\n[truncated to fit the gateway request limit]"
    actual = _body_bytes(messages, tools)
    if actual > limit:
        raise RequestBodyTooLargeError(actual_bytes=actual, limit=limit)


@runtime_checkable
class ChatTransport(Protocol):
    """The transport capability an episode needs; OpenRouterTransport satisfies it."""

    def complete_chat(
        self,
        *,
        profile: ModelProfile,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_output_tokens: int,
        temperature: float,
        reasoning_effort: str,
        timeout_seconds: float,
        tool_choice: ChatToolChoice | None = None,
    ) -> ChatTurnResult: ...


@runtime_checkable
class CancellationAwareChatTransport(Protocol):
    """Optional transport capability for interrupting an active model request."""

    def complete_chat_cancellable(
        self,
        *,
        profile: ModelProfile,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_output_tokens: int,
        temperature: float,
        reasoning_effort: str,
        timeout_seconds: float,
        cancel_event: threading.Event,
        tool_choice: ChatToolChoice | None = None,
    ) -> ChatTurnResult: ...


class ToolError(ValueError):
    """A tool rejected its arguments; reported back to the model as data."""


class RepoToolbox:
    """Read-only repository access jailed to one snapshot root.

    ``virtual_docs`` are named read-only texts served by the read_briefing
    tool (e.g. the change seed's diff sections) without living on disk.
    """

    def __init__(
        self,
        root: Path,
        *,
        virtual_docs: dict[str, str] | None = None,
        claim_extractor: Callable[[str], str] | None = None,
    ) -> None:
        self._root = root.resolve()
        self._virtual_docs = dict(virtual_docs or {})
        self._claim_extractor = claim_extractor
        # Repo-relative paths successfully read via read_file/extract_claims;
        # live instruments (e.g. the team worklist) render coverage from it.
        self.reads: set[str] = set()

    @staticmethod
    def _normalize_relative(relative: str) -> str:
        return relative[2:] if relative.startswith("./") else relative

    def extract_claims(self, doc: str) -> str:
        if self._claim_extractor is None:
            return "claim extraction is not available in this run"
        self._resolve(doc)
        self.reads.add(self._normalize_relative(doc))
        return _cap(self._claim_extractor(doc))

    def read_briefing(self, name: str) -> str:
        if name in self._virtual_docs:
            return _cap(self._virtual_docs[name])
        available = "\n".join(f"- {key}" for key in sorted(self._virtual_docs))
        return f"unknown briefing section {name!r}; available sections:\n{available}"

    def _resolve(self, relative: str) -> Path:
        if (
            not relative
            or relative.startswith(("/", "~"))
            or "\x00" in relative
            or relative.split("/", 1)[0] == ".."
            or "/../" in relative
        ):
            raise ToolError("path must be repo-relative without traversal")
        candidate = (self._root / relative).resolve()
        if candidate != self._root and self._root not in candidate.parents:
            raise ToolError("path escapes the repository")
        return candidate

    def read_file(self, path: str, start: int = 1, end: int | None = None) -> str:
        target = self._resolve(path)
        if not target.is_file():
            raise ToolError(f"not a file: {path}")
        self.reads.add(self._normalize_relative(path))
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        first = max(1, int(start))
        last = len(lines) if end is None else min(len(lines), int(end))
        if last < first:
            raise ToolError("empty line range")
        if last - first + 1 > _MAX_READ_LINES:
            last = first + _MAX_READ_LINES - 1
        rendered = "\n".join(
            f"{number}: {lines[number - 1]}" for number in range(first, min(last, len(lines)) + 1)
        )
        suffix = ""
        if last < len(lines):
            suffix = f"\n[truncated: file has {len(lines)} lines; showing {first}-{last}]"
        return _cap(rendered + suffix)

    def grep(self, pattern: str, glob: str = "**/*") -> str:
        if not pattern:
            raise ToolError("pattern must be non-empty")
        try:
            compiled = re.compile(pattern)
        except re.error:
            compiled = re.compile(re.escape(pattern))
        matches: list[str] = []
        scanned = 0
        for candidate in sorted(self._root.glob(glob)):
            if not candidate.is_file() or candidate.stat().st_size > _MAX_GREP_FILE_BYTES:
                continue
            scanned += 1
            if scanned > _MAX_GREP_FILES:
                matches.append("[truncated: file scan limit reached]")
                break
            text = candidate.read_text(encoding="utf-8", errors="replace")
            if "\x00" in text[:1024]:
                continue
            relative = candidate.relative_to(self._root).as_posix()
            for number, line in enumerate(text.splitlines(), start=1):
                if compiled.search(line):
                    matches.append(f"{relative}:{number}: {line[:240]}")
                    if len(matches) >= _MAX_GREP_MATCHES:
                        matches.append("[truncated: match limit reached]")
                        return _cap("\n".join(matches))
        if not matches:
            return "no matches"
        return _cap("\n".join(matches))

    def list_dir(self, path: str = ".") -> str:
        target = self._root if path in {".", ""} else self._resolve(path)
        if not target.is_dir():
            raise ToolError(f"not a directory: {path}")
        entries: list[str] = []
        for child in sorted(target.iterdir()):
            entries.append(child.name + ("/" if child.is_dir() else ""))
            if len(entries) >= _MAX_LIST_ENTRIES:
                entries.append("[truncated]")
                break
        return _cap("\n".join(entries) if entries else "empty directory")


def _cap(text: str) -> str:
    if len(text) <= _MAX_TOOL_RESULT_CHARS:
        return text
    return text[:_MAX_TOOL_RESULT_CHARS] + "\n[truncated]"


def _tool_definitions(submit_schema: dict[str, Any]) -> list[dict[str, Any]]:
    def function(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {"name": name, "description": description, "parameters": parameters},
        }

    return [
        function(
            "read_file",
            "Read a repo-relative text file, optionally a 1-based inclusive line range. "
            "Returns line-numbered text.",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start": {"type": "integer"},
                    "end": {"type": "integer"},
                },
                "required": ["path"],
            },
        ),
        function(
            "grep",
            "Search file contents under the repository with a regular expression "
            "(falls back to literal). Optional glob narrows the file set.",
            {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "glob": {"type": "string"},
                },
                "required": ["pattern"],
            },
        ),
        function(
            "list_dir",
            "List one repo-relative directory.",
            {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": [],
            },
        ),
        function(
            "read_briefing",
            "Read one named section of the change briefing (e.g. the code diff). "
            "Section names are listed in your instructions; an unknown name "
            "returns the available list.",
            {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        ),
        function(
            "extract_claims",
            "Deterministically extract one doc's section claims (heading, line, "
            "excerpt) using the product's claim provider.",
            {
                "type": "object",
                "properties": {"doc": {"type": "string"}},
                "required": ["doc"],
            },
        ),
        function(
            _SUBMIT_TOOL,
            "Submit your single final result. Call exactly once, then stop.",
            submit_schema,
        ),
    ]


@dataclass(slots=True)
class EpisodeOutcome:
    """What one episode produced, plus its accounting for telemetry."""

    submitted: dict[str, Any] | None
    failure_reason: str | None
    turns: int
    tool_calls: int
    input_tokens: int
    output_tokens: int

    @property
    def ok(self) -> bool:
        return self.submitted is not None


def _estimate_request_tokens(payload: object) -> int:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    return len(encoded.encode("utf-8"))


def _assistant_message(message: dict[str, Any]) -> dict[str, Any]:
    kept: dict[str, Any] = {"role": "assistant"}
    for key in ("content", "tool_calls", "reasoning_details"):
        value = message.get(key)
        if value is not None:
            kept[key] = value
    calls = message.get("tool_calls")
    reasoning_content = message.get("reasoning_content")
    if isinstance(calls, list) and calls and reasoning_content is not None:
        kept["reasoning_content"] = reasoning_content
    if "content" not in kept:
        kept["content"] = ""
    return kept


def _tool_call_entries(message: dict[str, Any]) -> list[dict[str, Any]]:
    raw = message.get("tool_calls")
    if not isinstance(raw, list):
        return []
    entries: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            entries.append({str(key): value for key, value in item.items()})
    return entries


def _call_name(entry: dict[str, Any]) -> str:
    function = entry.get("function")
    if isinstance(function, dict):
        name = function.get("name")
        if isinstance(name, str):
            return name
    return ""


def _call_arguments(entry: dict[str, Any]) -> dict[str, Any]:
    function = entry.get("function")
    raw: object = None
    if isinstance(function, dict):
        raw = function.get("arguments")
    if isinstance(raw, dict):
        return {str(key): value for key, value in raw.items()}
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        if isinstance(parsed, dict):
            return {str(key): value for key, value in parsed.items()}
    return {}


def _summarize_turn(result: ChatTurnResult) -> dict[str, Any]:
    """Trace-sized view of one assistant turn."""

    message = result.message
    return {
        "finish_reason": result.finish_reason,
        "model": result.actual_model,
        "content": str(message.get("content") or "")[:2_000],
        "tool_calls": [_call_name(entry) for entry in _tool_call_entries(message)],
    }


class EpisodeRunner:
    """Run bounded tool-loop episodes against one transport and one budget ledger."""

    def __init__(
        self,
        transport: ChatTransport,
        ledger: BudgetLedger,
        *,
        profile: ModelProfile = "strong",
        reasoning_effort: str = "high",
        temperature: float = 1.0,
        max_output_tokens: int = 16_000,
        max_turns: int = 12,
        per_call_timeout_seconds: float = 240.0,
        max_call_attempts: int = 3,
        backoff_seconds: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
        trace_label: str = "episode",
    ) -> None:
        if max_turns < 1:
            raise ValueError("max_turns must be positive")
        if max_call_attempts < 1:
            raise ValueError("max_call_attempts must be positive")
        self._transport = transport
        self._ledger = ledger
        self._trace_label = trace_label
        self._profile: ModelProfile = profile
        self._reasoning_effort = reasoning_effort
        self._temperature = temperature
        self._max_output_tokens = max_output_tokens
        self._max_turns = max_turns
        self._per_call_timeout_seconds = per_call_timeout_seconds
        self._max_call_attempts = max_call_attempts
        self._backoff_seconds = backoff_seconds
        self._sleep = sleep

    def run(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        toolbox: RepoToolbox,
        submit_model: type[BaseModel],
    ) -> EpisodeOutcome:
        tools = _tool_definitions(submit_model.model_json_schema())
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        outcome = EpisodeOutcome(
            submitted=None,
            failure_reason=None,
            turns=0,
            tool_calls=0,
            input_tokens=0,
            output_tokens=0,
        )
        nudged = False
        submit_retries = 0
        for _turn in range(self._max_turns):
            if _turn == self._max_turns - 1:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "FINAL TURN: call the submit tool now with your final result. "
                            "Do not call any other tool."
                        ),
                    }
                )
            try:
                result = self._call(messages, tools)
            except RequestBodyTooLargeError:
                outcome.failure_reason = "request_body_too_large"
                return outcome
            except ModelClientError as error:
                outcome.failure_reason = f"model:{error.reason_code}"
                return outcome
            outcome.turns += 1
            outcome.input_tokens += result.usage.prompt_tokens
            outcome.output_tokens += result.usage.completion_tokens
            message = result.message
            calls = _tool_call_entries(message)
            submit_entry = next(
                (entry for entry in calls if _call_name(entry) == _SUBMIT_TOOL), None
            )
            if submit_entry is not None:
                payload = _call_arguments(submit_entry)
                try:
                    validated = submit_model.model_validate(payload)
                except ValidationError as error:
                    # The episode did the work; a malformed submit is not a
                    # reason to discard it. Feed the error back and let the
                    # model repair its own arguments.
                    if submit_retries < 2:
                        submit_retries += 1
                        messages.append(_assistant_message(message))
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": str(submit_entry.get("id") or "submit"),
                                "content": (
                                    "submit rejected by schema validation:\n"
                                    + str(error)[:600]
                                    + "\nFix the arguments and call submit again."
                                ),
                            }
                        )
                        continue
                    outcome.failure_reason = "submit_schema_invalid"
                    return outcome
                outcome.submitted = validated.model_dump(mode="json")
                return outcome
            if calls:
                messages.append(_assistant_message(message))
                for entry in calls:
                    outcome.tool_calls += 1
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": str(entry.get("id") or f"call_{outcome.tool_calls}"),
                            "content": self._execute(toolbox, entry),
                        }
                    )
                continue
            content = message.get("content")
            parsed = self._parse_inline_submission(content, submit_model)
            if parsed is not None:
                outcome.submitted = parsed
                return outcome
            if not nudged:
                messages.append(_assistant_message(message))
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Finish now: call the submit tool exactly once with your final "
                            "result. Do not reply with prose."
                        ),
                    }
                )
                nudged = True
                continue
            outcome.failure_reason = "no_submit"
            return outcome
        outcome.failure_reason = "turn_limit"
        return outcome

    @staticmethod
    def _parse_inline_submission(
        content: object,
        submit_model: type[BaseModel],
    ) -> dict[str, Any] | None:
        if not isinstance(content, str) or not content.strip():
            return None
        text = content.strip()
        if text.startswith("```"):
            newline = text.find("\n")
            if newline != -1:
                text = text[newline + 1 :]
            fence = text.rfind("```")
            if fence != -1:
                text = text[:fence]
        try:
            payload = json.loads(text)
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        try:
            validated = submit_model.model_validate(payload)
        except ValidationError:
            return None
        dumped: dict[str, Any] = validated.model_dump(mode="json")
        return dumped

    def _execute(self, toolbox: RepoToolbox, entry: dict[str, Any]) -> str:
        name = _call_name(entry)
        arguments = _call_arguments(entry)
        try:
            if name == "read_file":
                path = str(arguments.get("path", ""))
                start = int(arguments.get("start", 1) or 1)
                raw_end = arguments.get("end")
                end = None if raw_end in {None, ""} else int(raw_end)
                return toolbox.read_file(path, start=start, end=end)
            if name == "grep":
                return toolbox.grep(
                    str(arguments.get("pattern", "")),
                    glob=str(arguments.get("glob") or "**/*"),
                )
            if name == "list_dir":
                return toolbox.list_dir(str(arguments.get("path") or "."))
            if name == "read_briefing":
                return toolbox.read_briefing(str(arguments.get("name", "")))
            if name == "extract_claims":
                return toolbox.extract_claims(str(arguments.get("doc", "")))
        except (ToolError, OSError, ValueError) as error:
            return f"ERROR: {error}"
        return f"ERROR: unknown tool {name!r}"

    def _call(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ChatTurnResult:
        shrink_messages(messages, tools)
        last_error: ModelClientError | None = None
        for attempt in range(self._max_call_attempts):
            reservation = self._ledger.reserve_model_call(
                self._profile,
                _estimate_request_tokens({"messages": messages, "tools": tools}),
            )
            remaining = self._ledger.remaining_seconds()
            if remaining <= 0:
                self._ledger.check_deadline()
            timeout = min(self._per_call_timeout_seconds, remaining)
            generation = _obs.start_generation(
                name=self._trace_label,
                model=str(self._profile),
                input_data=_obs.clip_messages(messages),
                metadata={"attempt": attempt},
            )
            try:
                result = self._transport.complete_chat(
                    profile=self._profile,
                    messages=messages,
                    tools=tools,
                    max_output_tokens=self._max_output_tokens,
                    temperature=self._temperature,
                    reasoning_effort=self._reasoning_effort,
                    timeout_seconds=timeout,
                )
            except ModelClientError as error:
                usage = error.usage
                self._ledger.record_model_usage(
                    reservation,
                    input_tokens=usage.prompt_tokens if usage is not None else 0,
                    output_tokens=usage.completion_tokens if usage is not None else 0,
                    estimated_cost_usd=(
                        usage.cost_usd if isinstance(usage, ModelCallUsage) else 0.0
                    ),
                )
                _obs.end_generation(
                    generation,
                    output={
                        "error": {
                            "reason": error.reason_code,
                            "status_code": error.status_code,
                            "provider_status_code": error.provider_status_code,
                            "provider_error_type": error.provider_error_type,
                        }
                    },
                    level="ERROR",
                    status=error.reason_code,
                )
                last_error = error
                if error.reason_code in _RETRYABLE_REASONS and attempt + 1 < (
                    self._max_call_attempts
                ):
                    self._sleep(_retry_delay(error.reason_code, attempt, self._backoff_seconds))
                    continue
                raise
            self._ledger.record_model_usage(
                reservation,
                input_tokens=result.usage.prompt_tokens,
                output_tokens=result.usage.completion_tokens,
                estimated_cost_usd=result.usage.cost_usd,
            )
            _obs.end_generation(
                generation,
                output=_summarize_turn(result),
                usage={
                    "input": result.usage.prompt_tokens,
                    "output": result.usage.completion_tokens,
                },
            )
            return result
        raise last_error if last_error is not None else ModelClientError("transport_error")


__all__ = [
    "CancellationAwareChatTransport",
    "ChatTransport",
    "EpisodeOutcome",
    "EpisodeRunner",
    "RepoToolbox",
    "RequestBodyTooLargeError",
    "ToolError",
    "request_projection_size_bytes",
]

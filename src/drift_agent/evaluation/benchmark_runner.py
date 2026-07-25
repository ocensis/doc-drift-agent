from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import selectors
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from pydantic import ValidationError

from drift_agent.adapters.contracts import PublicBundleV3

DRIFT_ADAPTER_VERSION = "drift-cli-v1"
CODEX_RENDERER_VERSION = "codex-exec-v1"
REDACTION_POLICY_VERSION = "benchmark-redaction-v1"

_SAFE_ENVIRONMENT = {
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PYTHONNOUSERSITE": "1",
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTEST_ADDOPTS": "-p no:cacheprovider",
}
_CODEX_AUTH_ENVIRONMENT = frozenset({"OPENAI_API_KEY"})
_TASK_SHAPE = {
    "protocol_version": 1,
    "baseline": "HEAD",
    "scope": "current_worktree_changes",
    "docs_only": True,
    "report_findings": True,
    "run_configured_validation": True,
    "abstain_on_insufficient_evidence": True,
    "network": False,
    "dependency_install": False,
    "git_mutation": False,
}
_DRIFT_EXIT_CODES = {
    "clean": 0,
    "fixed": 0,
    "drift_found": 1,
    "partial": 1,
    "needs_approval": 1,
    "unresolved": 1,
    "stale": 2,
    "failed": 2,
}
_CODEX_ITEM_TYPES = frozenset(
    {
        "agent_message",
        "reasoning",
        "command_execution",
        "file_change",
        "mcp_tool_call",
        "web_search",
        "todo_list",
        "error",
        "collab_tool_call",
        "subagent",
    }
)
_START_REQUIRED_ITEM_TYPES = frozenset(
    {"command_execution", "mcp_tool_call", "web_search", "collab_tool_call", "subagent"}
)
_COUNTED_TOOL_TYPES = frozenset({"command_execution", "file_change", "mcp_tool_call", "web_search"})
_FORBIDDEN_TOOL_TYPES = frozenset({"mcp_tool_call", "web_search", "collab_tool_call", "subagent"})
_GENERIC_SECRET_PATTERNS = (
    re.compile(rb"(?i)\bBearer\s+[^\s\"']+"),
    re.compile(rb"(?i)\b(?:api[_-]?key|access[_-]?token|secret)\b\s*[:=]\s*[^\s,;\"']+"),
)
_REDACTED = b"<redacted>"

RunClassification = Literal[
    "completed",
    "authorization_missing",
    "runner_internal_error",
    "runner_timeout",
    "output_limit",
    "invalid_jsonl",
    "missing_terminal_event",
    "invalid_final_schema",
    "secret_leakage_detected",
    "auth_failed",
    "model_unavailable",
    "rate_limited_or_provider_error",
    "scoreable_subject_failure",
    "tool_profile_violation",
]


class LiveAuthorizationRequired(PermissionError):
    """Raised before a live Codex process can start without explicit authorization."""


class BenchmarkRunnerConfigurationError(ValueError):
    """Raised when an adapter request is not the frozen benchmark protocol."""


class CodexProtocolError(ValueError):
    """A type-specific Codex JSONL protocol violation."""

    def __init__(self, code: Literal["invalid_jsonl", "missing_terminal_event"], detail: str):
        super().__init__(detail)
        self.code = code


@dataclass(frozen=True)
class ProcessInvocation:
    argv: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str] = field(repr=False)
    stdin: bytes = field(repr=False)
    timeout_seconds: float
    stdout_limit_bytes: int
    stderr_limit_bytes: int
    terminate_grace_seconds: float = 1.0
    sensitive_values: tuple[bytes, ...] = field(default=(), repr=False)


@dataclass(frozen=True)
class ProcessCapture:
    started: bool
    stdout: bytes = field(default=b"", repr=False)
    stderr: bytes = field(default=b"", repr=False)
    returncode: int | None = None
    duration_ms: int = 0
    timed_out: bool = False
    output_limited: bool = False
    secret_leakage_detected: bool = False
    stdout_total_bytes: int = 0
    stderr_total_bytes: int = 0
    signal_number: int | None = None
    start_error: str | None = None


class ProcessRunner(Protocol):
    def run(self, invocation: ProcessInvocation) -> ProcessCapture: ...


@dataclass(frozen=True)
class StreamEvidence:
    sealed_raw: bytes = field(repr=False)
    redacted: bytes = field(repr=False)
    raw_sha256: str
    redacted_sha256: str
    bytes_read: int
    bytes_stored: int
    byte_limit: int
    truncated: bool
    explicit_secret_replacements: int
    generic_replacements: int
    redaction_policy_version: str = REDACTION_POLICY_VERSION


@dataclass(frozen=True)
class EffectiveRequestReceipt:
    operation: Literal["check", "repair"]
    adapter_version: str
    argv: tuple[str, ...]
    stdin_sha256: str


@dataclass(frozen=True)
class TerminalReceipt:
    started: bool
    classification: RunClassification
    scoreable: bool
    returncode: int | None
    signal_number: int | None
    duration_ms: int
    timed_out: bool
    output_limited: bool
    no_retry: Literal[True] = True
    detail: str | None = None


@dataclass(frozen=True)
class CodexUsage:
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    tool_calls: int = 0


@dataclass(frozen=True)
class CodexProtocolResult:
    events: tuple[Mapping[str, Any], ...]
    terminal_type: Literal["turn.completed", "turn.failed"] | None
    has_turn_activity: bool
    final_result: object | None
    final_error: str | None
    usage: CodexUsage
    tool_profile_violations: tuple[str, ...]
    terminal_failure_class: RunClassification | None
    ignored_trailing_bytes: int = 0


@dataclass(frozen=True)
class SubjectRunResult:
    subject: Literal["codex", "drift_agent"]
    request: EffectiveRequestReceipt
    terminal: TerminalReceipt
    stdout: StreamEvidence
    stderr: StreamEvidence
    parsed_result: object | None = field(default=None, repr=False)
    codex_protocol: CodexProtocolResult | None = field(default=None, repr=False)


FinalValidator = Callable[[Mapping[str, Any]], object]


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_text(value: str, *, name: str) -> str:
    if not value or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise BenchmarkRunnerConfigurationError(f"{name} must be non-empty and control-free")
    return value


def _absolute_path(value: str | Path, *, name: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise BenchmarkRunnerConfigurationError(f"{name} must be absolute")
    _safe_text(os.fspath(path), name=name)
    return path


def _task_field(task: object, name: str) -> object:
    if isinstance(task, Mapping):
        if name not in task:
            raise BenchmarkRunnerConfigurationError(f"benchmark task is missing {name}")
        return task[name]
    try:
        return getattr(task, name)
    except AttributeError:
        raise BenchmarkRunnerConfigurationError(f"benchmark task is missing {name}") from None


def _task_operation(task: object) -> Literal["check", "repair"]:
    for field_name, expected in _TASK_SHAPE.items():
        if _task_field(task, field_name) != expected:
            raise BenchmarkRunnerConfigurationError(
                f"benchmark task {field_name} does not match protocol v1"
            )
    operation = _task_field(task, "operation")
    if operation not in {"check", "repair"}:
        raise BenchmarkRunnerConfigurationError("benchmark task operation must be check or repair")
    return operation


def _validate_process_limits(
    *,
    timeout_seconds: float,
    stdout_limit_bytes: int,
    stderr_limit_bytes: int,
) -> None:
    if timeout_seconds <= 0:
        raise BenchmarkRunnerConfigurationError("timeout must be positive")
    if stdout_limit_bytes <= 0 or stderr_limit_bytes <= 0:
        raise BenchmarkRunnerConfigurationError("output limits must be positive")


def sanitized_environment(
    *,
    path: str | Path,
    home: str | Path,
    tmpdir: str | Path,
    codex_home: str | Path | None = None,
    auth_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build an allowlist environment; the parent process environment is never inherited."""

    result = {
        **_SAFE_ENVIRONMENT,
        "PATH": os.fspath(_absolute_path(path, name="PATH")),
        "HOME": os.fspath(_absolute_path(home, name="HOME")),
        "TMPDIR": os.fspath(_absolute_path(tmpdir, name="TMPDIR")),
    }
    if codex_home is not None:
        result["CODEX_HOME"] = os.fspath(_absolute_path(codex_home, name="CODEX_HOME"))
    for name, value in (auth_environment or {}).items():
        if name not in _CODEX_AUTH_ENVIRONMENT:
            raise BenchmarkRunnerConfigurationError(f"unsupported Codex auth variable: {name}")
        result[name] = _safe_text(value, name=name)
    return dict(sorted(result.items()))


def render_drift_argv(
    *,
    executable: str | Path,
    task: object,
    repo: str | Path,
    state_dir: str | Path,
) -> tuple[str, ...]:
    operation = _task_operation(task)
    return (
        os.fspath(_absolute_path(executable, name="Drift executable")),
        operation,
        "--repo",
        os.fspath(_absolute_path(repo, name="repository")),
        "--state-dir",
        os.fspath(_absolute_path(state_dir, name="state directory")),
        "--lock-timeout-seconds",
        "5",
        "--format",
        "json",
        "--output-version",
        "3",
    )


def _toml_string(value: str) -> str:
    return json.dumps(_safe_text(value, name="Codex config value"), ensure_ascii=False)


def render_codex_argv(
    *,
    executable: str | Path,
    task: object,
    repo: str | Path,
    model: str,
    output_schema: str | Path,
    child_path: str | Path,
    child_home: str | Path,
    child_tmpdir: str | Path,
    permission_profile: str = "benchmark",
    reasoning_effort: str = "low",
    argv_prefix: Sequence[str] = (),
) -> tuple[str, ...]:
    _task_operation(task)
    if reasoning_effort not in {"minimal", "low", "medium", "high", "xhigh"}:
        raise BenchmarkRunnerConfigurationError("unsupported reasoning effort")
    model = _safe_text(model, name="Codex model")
    permission_profile = _safe_text(permission_profile, name="Codex permission profile")
    child_values = {
        "PATH": os.fspath(_absolute_path(child_path, name="child PATH")),
        "HOME": os.fspath(_absolute_path(child_home, name="child HOME")),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TMPDIR": os.fspath(_absolute_path(child_tmpdir, name="child TMPDIR")),
        "PYTHONNOUSERSITE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_ADDOPTS": "-p no:cacheprovider",
    }
    child_config = (
        "{" + ",".join(f"{key}={_toml_string(value)}" for key, value in child_values.items()) + "}"
    )
    safe_prefix = tuple(_safe_text(token, name="Codex argv prefix token") for token in argv_prefix)
    return (
        *safe_prefix,
        os.fspath(_absolute_path(executable, name="Codex executable")),
        "-a",
        "never",
        "exec",
        "--ephemeral",
        "--ignore-rules",
        "--strict-config",
        "-C",
        os.fspath(_absolute_path(repo, name="repository")),
        "--model",
        model,
        "-c",
        f"model_reasoning_effort={_toml_string(reasoning_effort)}",
        "-c",
        'web_search="disabled"',
        "-c",
        "features.multi_agent=false",
        "-c",
        f"default_permissions={_toml_string(permission_profile)}",
        "-c",
        'shell_environment_policy.inherit="none"',
        "-c",
        f"shell_environment_policy.set={child_config}",
        "--color",
        "never",
        "--json",
        "--output-schema",
        os.fspath(_absolute_path(output_schema, name="Codex output schema")),
        "-",
    )


def render_codex_prompt(task: object) -> str:
    operation = _task_operation(task).upper()
    return f"""Operation: {operation}.

HEAD is the input baseline. The current uncommitted worktree contains the
candidate code changes. Check whether those changes made Markdown documentation
or Python docstrings stale.

For check tasks, report only and do not modify files. For repair tasks, make the
smallest safe documentation-only repair. Markdown and docstring text are allowed;
executable code, tests, configuration, Git index, refs and Git configuration are
not. Do not install dependencies, use network/web search, invoke drift-agent or
another coding agent, or run Git mutation commands. Use only repository-local
evidence and configured local validation. Abstain when evidence is ambiguous.

Use clean for no drift; drift_found for check-only drift; fixed for a complete
safe repair; partial when a safe documentation repair was applied but findings
remain; needs_approval when the next step needs a user choice or broader write
scope; unresolved when evidence or supported capability is insufficient; stale
when the input precondition changed; and failed only for an unfinished task due
to an internal or tool failure.

Encode every reported finding with this case-neutral V1 ontology:
- parameter_added/parameter_removed use component_kind=parameter, the parameter
  name, and missing->present or present->missing values.
- parameter_default_changed uses typed JSON Python literals (or missing).
- parameter_annotation_changed and return_annotation_changed use canonical
  Python annotation text (or missing), never AST dump strings.
- symbol_renamed/symbol_deleted use symbol_fqn tagged values.
- google_arg_changed/google_returns_changed use the same value rules for stale
  Google-style Python docstrings.
- broken_example uses code_path=drift-agent.toml, component_kind doctest or
  pytest, no symbol FQN/name, and validation_status passed->failed.
- ambiguous_or_unsupported uses component_kind=unsupported when the evidence
  identifies drift but cannot support a safe structured repair.
Use the current symbol FQN except for deletion. Paths must be canonical
repo-relative POSIX paths. old_value is the baseline/doc expectation and
new_value is current code or validation truth. Explanations do not affect
finding identity. Validation claims only report checks actually run; never put
commands or arguments in them.

Return only the requested structured result.
"""


def _redact(raw: bytes, sensitive_values: Sequence[bytes]) -> tuple[bytes, int, int]:
    redacted = raw
    explicit_count = 0
    for sensitive in sorted({value for value in sensitive_values if value}, key=len, reverse=True):
        count = redacted.count(sensitive)
        if count:
            redacted = redacted.replace(sensitive, _REDACTED)
            explicit_count += count
    generic_count = 0
    for pattern in _GENERIC_SECRET_PATTERNS:
        redacted, count = pattern.subn(_REDACTED, redacted)
        generic_count += count
    return redacted, explicit_count, generic_count


def seal_stream(
    raw: bytes,
    *,
    total_bytes: int,
    byte_limit: int,
    sensitive_values: Sequence[bytes] = (),
) -> StreamEvidence:
    if byte_limit <= 0:
        raise BenchmarkRunnerConfigurationError("stream byte limit must be positive")
    if total_bytes < len(raw):
        raise BenchmarkRunnerConfigurationError(
            "stream total bytes may not be smaller than capture"
        )
    sealed = raw[:byte_limit]
    redacted, explicit_count, generic_count = _redact(sealed, sensitive_values)
    return StreamEvidence(
        sealed_raw=sealed,
        redacted=redacted,
        raw_sha256=_sha256(sealed),
        redacted_sha256=_sha256(redacted),
        bytes_read=total_bytes,
        bytes_stored=len(sealed),
        byte_limit=byte_limit,
        truncated=total_bytes > len(sealed),
        explicit_secret_replacements=explicit_count,
        generic_replacements=generic_count,
    )


def _contains_sensitive(raw: bytes, sensitive_values: Sequence[bytes]) -> bool:
    return any(value and value in raw for value in sensitive_values)


class BoundedSubprocessRunner:
    """Explicitly injectable POSIX subprocess runner with bounded pipe capture."""

    def run(self, invocation: ProcessInvocation) -> ProcessCapture:
        _validate_process_limits(
            timeout_seconds=invocation.timeout_seconds,
            stdout_limit_bytes=invocation.stdout_limit_bytes,
            stderr_limit_bytes=invocation.stderr_limit_bytes,
        )
        if invocation.terminate_grace_seconds < 0:
            raise BenchmarkRunnerConfigurationError("termination grace must be non-negative")
        started_at = time.monotonic()
        try:
            process = subprocess.Popen(
                invocation.argv,
                cwd=invocation.cwd,
                env=dict(invocation.env),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                bufsize=0,
                start_new_session=True,
            )
        except OSError as error:
            return ProcessCapture(
                started=False,
                duration_ms=max(0, int((time.monotonic() - started_at) * 1000)),
                start_error=type(error).__name__,
            )

        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        selector = selectors.DefaultSelector()
        stdout_fd = process.stdout.fileno()
        stderr_fd = process.stderr.fileno()
        stdin_fd = process.stdin.fileno()
        for file_descriptor in (stdout_fd, stderr_fd, stdin_fd):
            os.set_blocking(file_descriptor, False)
        selector.register(stdout_fd, selectors.EVENT_READ, "stdout")
        selector.register(stderr_fd, selectors.EVENT_READ, "stderr")
        stdin_offset = 0
        if invocation.stdin:
            selector.register(stdin_fd, selectors.EVENT_WRITE, "stdin")
        else:
            process.stdin.close()

        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        totals = {"stdout": 0, "stderr": 0}
        scan_tails = {"stdout": b"", "stderr": b""}
        limits = {
            "stdout": invocation.stdout_limit_bytes,
            "stderr": invocation.stderr_limit_bytes,
        }
        timed_out = False
        output_limited = False
        secret_leakage = False
        termination_started: float | None = None
        max_secret_length = max((len(value) for value in invocation.sensitive_values), default=1)

        def terminate() -> None:
            nonlocal termination_started
            if termination_started is not None or process.poll() is not None:
                return
            termination_started = time.monotonic()
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                process.terminate()

        try:
            while selector.get_map() or process.poll() is None:
                now = time.monotonic()
                if not timed_out and now - started_at >= invocation.timeout_seconds:
                    timed_out = True
                    terminate()
                if (
                    termination_started is not None
                    and process.poll() is None
                    and now - termination_started >= invocation.terminate_grace_seconds
                ):
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except (OSError, ProcessLookupError):
                        process.kill()

                for key, _ in selector.select(timeout=0.02):
                    descriptor = cast(int, key.fileobj)
                    stream_name = cast(str, key.data)
                    if stream_name == "stdin":
                        try:
                            written = os.write(descriptor, invocation.stdin[stdin_offset:])
                            stdin_offset += written
                        except BrokenPipeError:
                            stdin_offset = len(invocation.stdin)
                        if stdin_offset >= len(invocation.stdin):
                            selector.unregister(descriptor)
                            process.stdin.close()
                        continue
                    try:
                        chunk = os.read(descriptor, 65536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(descriptor)
                        continue
                    totals[stream_name] += len(chunk)
                    remaining = max(0, limits[stream_name] - len(buffers[stream_name]))
                    buffers[stream_name].extend(chunk[:remaining])
                    if totals[stream_name] > limits[stream_name]:
                        output_limited = True
                        terminate()
                    scanned = scan_tails[stream_name] + chunk
                    if _contains_sensitive(scanned, invocation.sensitive_values):
                        secret_leakage = True
                        terminate()
                    scan_tails[stream_name] = scanned[-max_secret_length:]

                if process.poll() is not None:
                    try:
                        selector.unregister(stdin_fd)
                    except KeyError:
                        pass
                    if not process.stdin.closed:
                        process.stdin.close()
                    if not selector.get_map():
                        break
            returncode = process.wait()
        finally:
            selector.close()
            for pipe in (process.stdin, process.stdout, process.stderr):
                if not pipe.closed:
                    pipe.close()

        return ProcessCapture(
            started=True,
            stdout=bytes(buffers["stdout"]),
            stderr=bytes(buffers["stderr"]),
            returncode=returncode,
            duration_ms=max(0, int((time.monotonic() - started_at) * 1000)),
            timed_out=timed_out,
            output_limited=output_limited,
            secret_leakage_detected=secret_leakage,
            stdout_total_bytes=totals["stdout"],
            stderr_total_bytes=totals["stderr"],
            signal_number=-returncode if returncode < 0 else None,
        )


def _strict_json(raw: str | bytes) -> object:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def invalid_constant(value: str) -> object:
        raise ValueError(f"invalid JSON constant: {value}")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)


def _minimal_final_validator(value: Mapping[str, Any]) -> object:
    expected = {"schema_version", "declared_status", "findings", "validation_claims"}
    if set(value) != expected or value.get("schema_version") != 1:
        raise ValueError("Codex result does not match the minimal V1 boundary")
    if value.get("declared_status") not in _DRIFT_EXIT_CODES:
        raise ValueError("Codex result has an unknown status")
    findings = value.get("findings")
    claims = value.get("validation_claims")
    if not isinstance(findings, list) or len(findings) > 64:
        raise ValueError("Codex findings must be a bounded array")
    if not isinstance(claims, list) or len(claims) > 16:
        raise ValueError("Codex validation claims must be a bounded array")
    return dict(value)


def _default_final_validator(value: Mapping[str, Any]) -> object:
    try:
        module = importlib.import_module("drift_agent.evaluation.benchmark_models")
        model = module.__dict__["CodexTaskResultV1"]
    except (ImportError, KeyError):
        return _minimal_final_validator(value)
    return model.model_validate_json(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        strict=True,
    )


def _jsonl_records(raw: bytes, *, allow_trailing_partial: bool) -> tuple[list[object], int]:
    if not raw:
        return [], 0
    parts = raw.splitlines(keepends=True)
    ignored = 0
    if parts and not parts[-1].endswith((b"\n", b"\r")):
        if not allow_trailing_partial:
            raise CodexProtocolError("invalid_jsonl", "JSONL ended with a partial record")
        ignored = len(parts.pop())
    records: list[object] = []
    for line in parts:
        if not line.strip():
            raise CodexProtocolError("invalid_jsonl", "JSONL contains an empty record")
        try:
            records.append(_strict_json(line))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise CodexProtocolError("invalid_jsonl", type(error).__name__) from None
    return records, ignored


def _string_field(value: Mapping[str, Any], name: str) -> str:
    result = value.get(name)
    if not isinstance(result, str) or not result:
        raise CodexProtocolError("invalid_jsonl", f"event field {name} must be non-empty")
    return result


def _usage_from_terminal(event: Mapping[str, Any], *, tool_calls: int) -> CodexUsage:
    raw_usage = event.get("usage")
    if raw_usage is None:
        return CodexUsage(tool_calls=tool_calls)
    if not isinstance(raw_usage, Mapping):
        raise CodexProtocolError("invalid_jsonl", "terminal usage must be an object")

    def token(name: str) -> int | None:
        value = raw_usage.get(name)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise CodexProtocolError("invalid_jsonl", f"usage {name} must be non-negative")
        return value

    return CodexUsage(
        input_tokens=token("input_tokens"),
        cached_input_tokens=token("cached_input_tokens"),
        output_tokens=token("output_tokens"),
        tool_calls=tool_calls,
    )


def _terminal_failure_class(event: Mapping[str, Any]) -> RunClassification | None:
    error = event.get("error")
    if not isinstance(error, Mapping):
        return None
    # Codex CLI v0.144.1 exposes only ``error.message`` on turn.failed.  Keep
    # accepting the structured fields newer protocol versions may add, then
    # normalize punctuation so wire spellings such as rate-limit, rate limit,
    # and rate_limit share one classification path.
    fields = [error.get(name) for name in ("message", "code", "type", "name")]
    normalized = re.sub(
        r"[^a-z0-9]+",
        "_",
        " ".join(value.casefold() for value in fields if isinstance(value, str)),
    ).strip("_")
    if any(
        marker in normalized
        for marker in (
            "invalid_json_schema",
            "invalid_schema",
            "response_format",
            "structured_output_schema",
        )
    ):
        return "runner_internal_error"
    if any(
        marker in normalized
        for marker in (
            "auth",
            "unauthorized",
            "invalid_api_key",
            "login_required",
            "not_logged_in",
            "status_401",
            "http_401",
        )
    ):
        return "auth_failed"
    if "model" in normalized and any(
        marker in normalized
        for marker in (
            "not_found",
            "unavailable",
            "not_available",
            "unknown",
            "unsupported",
            "does_not_exist",
        )
    ):
        return "model_unavailable"
    if any(
        marker in normalized
        for marker in (
            "rate_limit",
            "ratelimit",
            "too_many_requests",
            "usage_limit_exceeded",
            "insufficient_quota",
            "status_429",
            "http_429",
            "provider",
            "upstream",
            "service_unavailable",
            "server_overloaded",
            "internal_server_error",
            "http_connection_failed",
            "response_stream_connection_failed",
            "response_stream_disconnected",
            "response_too_many_failed_attempts",
        )
    ):
        return "rate_limited_or_provider_error"
    return None


def parse_codex_jsonl(
    raw: bytes,
    *,
    allow_incomplete: bool = False,
    allow_trailing_partial: bool = False,
    final_validator: FinalValidator | None = None,
) -> CodexProtocolResult:
    records, ignored = _jsonl_records(raw, allow_trailing_partial=allow_trailing_partial)
    events: list[Mapping[str, Any]] = []
    thread_started = False
    turn_started = False
    terminal: Mapping[str, Any] | None = None
    terminal_type: Literal["turn.completed", "turn.failed"] | None = None
    item_states: dict[str, tuple[str, str]] = {}
    completed_agent_messages: list[Mapping[str, Any]] = []
    completed_tools: set[str] = set()
    violations: set[str] = set()

    for record in records:
        if not isinstance(record, Mapping):
            raise CodexProtocolError("invalid_jsonl", "each JSONL record must be an object")
        event = cast(Mapping[str, Any], record)
        event_type = _string_field(event, "type")
        if terminal is not None:
            raise CodexProtocolError("invalid_jsonl", "event appears after terminal event")
        if event_type == "thread.started":
            if thread_started or turn_started:
                raise CodexProtocolError("invalid_jsonl", "duplicate or late thread.started")
            _string_field(event, "thread_id")
            thread_started = True
        elif event_type == "turn.started":
            if not thread_started or turn_started:
                raise CodexProtocolError("invalid_jsonl", "invalid turn.started transition")
            turn_started = True
        elif event_type in {"item.started", "item.updated", "item.completed"}:
            if not turn_started:
                raise CodexProtocolError("invalid_jsonl", "item event precedes turn.started")
            item = event.get("item")
            if not isinstance(item, Mapping):
                raise CodexProtocolError("invalid_jsonl", "item event requires an item object")
            item_id = _string_field(item, "id")
            item_type = _string_field(item, "type")
            if item_type not in _CODEX_ITEM_TYPES:
                raise CodexProtocolError("invalid_jsonl", f"unknown Codex item type: {item_type}")
            previous = item_states.get(item_id)
            if previous is not None and previous[0] != item_type:
                raise CodexProtocolError("invalid_jsonl", "item id changed type")
            if event_type == "item.started":
                if previous is not None:
                    raise CodexProtocolError("invalid_jsonl", "duplicate item.started")
                item_states[item_id] = (item_type, "started")
            elif event_type == "item.updated":
                if previous is None or previous[1] != "started":
                    raise CodexProtocolError("invalid_jsonl", "item.updated without active item")
            else:
                if previous is not None and previous[1] == "completed":
                    raise CodexProtocolError("invalid_jsonl", "duplicate item.completed")
                if previous is None and item_type in _START_REQUIRED_ITEM_TYPES:
                    raise CodexProtocolError("invalid_jsonl", "tool completed without item.started")
                item_states[item_id] = (item_type, "completed")
                if item_type == "agent_message":
                    completed_agent_messages.append(item)
                if item_type in _COUNTED_TOOL_TYPES:
                    completed_tools.add(item_id)
            if item_type in _FORBIDDEN_TOOL_TYPES:
                violations.add(item_type)
        elif event_type in {"turn.completed", "turn.failed"}:
            if not turn_started:
                raise CodexProtocolError("invalid_jsonl", "terminal event precedes turn.started")
            if event_type == "turn.failed":
                error = event.get("error")
                if not isinstance(error, Mapping):
                    raise CodexProtocolError(
                        "invalid_jsonl", "turn.failed requires an error object"
                    )
                if not _string_field(error, "message").strip():
                    raise CodexProtocolError(
                        "invalid_jsonl", "turn.failed error message must be non-empty"
                    )
            if event_type == "turn.completed" and any(
                state == "started" for _, state in item_states.values()
            ):
                raise CodexProtocolError("invalid_jsonl", "turn completed with active items")
            terminal = event
            terminal_type = cast(Literal["turn.completed", "turn.failed"], event_type)
        elif event_type == "error":
            if not thread_started:
                raise CodexProtocolError("invalid_jsonl", "error event precedes thread.started")
            _string_field(event, "message")
        else:
            raise CodexProtocolError("invalid_jsonl", f"unknown Codex event type: {event_type}")
        events.append(event)

    if terminal is None and not allow_incomplete:
        raise CodexProtocolError("missing_terminal_event", "Codex JSONL has no terminal event")

    final_result: object | None = None
    final_error: str | None = None
    if terminal_type == "turn.completed":
        if not completed_agent_messages:
            final_error = "missing completed agent message"
        else:
            text = completed_agent_messages[-1].get("text")
            if not isinstance(text, str):
                final_error = "last completed agent message has no text"
            else:
                try:
                    candidate = _strict_json(text)
                    if not isinstance(candidate, Mapping):
                        raise ValueError("final result must be an object")
                    final_result = (final_validator or _default_final_validator)(
                        cast(Mapping[str, Any], candidate)
                    )
                except (
                    json.JSONDecodeError,
                    UnicodeDecodeError,
                    ValidationError,
                    ValueError,
                ) as error:
                    final_error = type(error).__name__

    usage = _usage_from_terminal(terminal or {}, tool_calls=len(completed_tools))
    return CodexProtocolResult(
        events=tuple(events),
        terminal_type=terminal_type,
        has_turn_activity=turn_started or bool(item_states),
        final_result=final_result,
        final_error=final_error,
        usage=usage,
        tool_profile_violations=tuple(sorted(violations)),
        terminal_failure_class=(
            _terminal_failure_class(terminal)
            if terminal_type == "turn.failed" and terminal is not None
            else None
        ),
        ignored_trailing_bytes=ignored,
    )


def _stream_evidence(
    capture: ProcessCapture,
    *,
    stdout_limit_bytes: int,
    stderr_limit_bytes: int,
    sensitive_values: Sequence[bytes],
) -> tuple[StreamEvidence, StreamEvidence, bool]:
    stdout_total = max(capture.stdout_total_bytes, len(capture.stdout))
    stderr_total = max(capture.stderr_total_bytes, len(capture.stderr))
    stdout = seal_stream(
        capture.stdout,
        total_bytes=stdout_total,
        byte_limit=stdout_limit_bytes,
        sensitive_values=sensitive_values,
    )
    stderr = seal_stream(
        capture.stderr,
        total_bytes=stderr_total,
        byte_limit=stderr_limit_bytes,
        sensitive_values=sensitive_values,
    )
    leaked = (
        capture.secret_leakage_detected
        or stdout.explicit_secret_replacements > 0
        or stderr.explicit_secret_replacements > 0
    )
    return stdout, stderr, leaked


def _terminal(
    capture: ProcessCapture,
    classification: RunClassification,
    *,
    scoreable: bool,
    detail: str | None = None,
) -> TerminalReceipt:
    return TerminalReceipt(
        started=capture.started,
        classification=classification,
        scoreable=scoreable,
        returncode=capture.returncode,
        signal_number=capture.signal_number,
        duration_ms=capture.duration_ms,
        timed_out=capture.timed_out,
        output_limited=capture.output_limited,
        detail=detail,
    )


def _codex_status_matches_operation(operation: Literal["check", "repair"], result: object) -> bool:
    if isinstance(result, Mapping):
        status = result.get("declared_status")
    else:
        status = getattr(result, "declared_status", None)
        status = getattr(status, "value", status)
    allowed = (
        {"clean", "drift_found", "unresolved", "failed"}
        if operation == "check"
        else {"clean", "fixed", "partial", "needs_approval", "unresolved", "stale", "failed"}
    )
    return status in allowed


def run_drift_subject(
    *,
    executable: str | Path,
    runner: ProcessRunner,
    task: object,
    repo: str | Path,
    state_dir: str | Path,
    path: str | Path,
    home: str | Path,
    tmpdir: str | Path,
    timeout_seconds: float = 120.0,
    stdout_limit_bytes: int = 4 * 1024 * 1024,
    stderr_limit_bytes: int = 1 * 1024 * 1024,
) -> SubjectRunResult:
    _validate_process_limits(
        timeout_seconds=timeout_seconds,
        stdout_limit_bytes=stdout_limit_bytes,
        stderr_limit_bytes=stderr_limit_bytes,
    )
    operation = _task_operation(task)
    argv = render_drift_argv(executable=executable, task=task, repo=repo, state_dir=state_dir)
    request = EffectiveRequestReceipt(
        operation=operation,
        adapter_version=DRIFT_ADAPTER_VERSION,
        argv=argv,
        stdin_sha256=_sha256(b""),
    )
    invocation = ProcessInvocation(
        argv=argv,
        cwd=_absolute_path(repo, name="repository"),
        env=sanitized_environment(path=path, home=home, tmpdir=tmpdir),
        stdin=b"",
        timeout_seconds=timeout_seconds,
        stdout_limit_bytes=stdout_limit_bytes,
        stderr_limit_bytes=stderr_limit_bytes,
    )
    capture = runner.run(invocation)
    stdout, stderr, leaked = _stream_evidence(
        capture,
        stdout_limit_bytes=stdout_limit_bytes,
        stderr_limit_bytes=stderr_limit_bytes,
        sensitive_values=(),
    )
    if not capture.started:
        terminal = _terminal(
            capture, "runner_internal_error", scoreable=False, detail=capture.start_error
        )
        return SubjectRunResult("drift_agent", request, terminal, stdout, stderr)
    if leaked:
        terminal = _terminal(capture, "secret_leakage_detected", scoreable=False)
        return SubjectRunResult("drift_agent", request, terminal, stdout, stderr)
    effective_output_limit = capture.output_limited or stdout.truncated or stderr.truncated
    if capture.timed_out:
        terminal = _terminal(capture, "runner_timeout", scoreable=True)
        return SubjectRunResult("drift_agent", request, terminal, stdout, stderr)
    if effective_output_limit:
        terminal = _terminal(capture, "output_limit", scoreable=True)
        return SubjectRunResult("drift_agent", request, terminal, stdout, stderr)
    if capture.signal_number is not None or capture.returncode is None:
        terminal = _terminal(capture, "scoreable_subject_failure", scoreable=True)
        return SubjectRunResult("drift_agent", request, terminal, stdout, stderr)
    try:
        payload = _strict_json(stdout.sealed_raw)
        if not isinstance(payload, Mapping):
            raise ValueError("Drift bundle must be an object")
        # JSON has no native enum objects; validate the duplicate-free decoded wire
        # payload with normal JSON coercion semantics.
        bundle = PublicBundleV3.model_validate(payload)
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError, ValueError) as error:
        terminal = _terminal(
            capture, "invalid_final_schema", scoreable=True, detail=type(error).__name__
        )
        return SubjectRunResult("drift_agent", request, terminal, stdout, stderr)
    status = bundle.status.value
    if _DRIFT_EXIT_CODES[status] != capture.returncode:
        terminal = _terminal(capture, "invalid_final_schema", scoreable=True, detail="exit_status")
        return SubjectRunResult("drift_agent", request, terminal, stdout, stderr)
    terminal = _terminal(capture, "completed", scoreable=True)
    return SubjectRunResult("drift_agent", request, terminal, stdout, stderr, parsed_result=bundle)


def run_codex_subject(
    *,
    executable: str | Path,
    runner: ProcessRunner,
    task: object,
    repo: str | Path,
    model: str,
    output_schema: str | Path,
    path: str | Path,
    home: str | Path,
    tmpdir: str | Path,
    child_path: str | Path,
    child_home: str | Path,
    child_tmpdir: str | Path,
    codex_home: str | Path | None = None,
    auth_environment: Mapping[str, str] | None = None,
    sensitive_values: Sequence[bytes] = (),
    live: bool,
    authorize_live_codex: bool = False,
    permission_profile: str = "benchmark",
    reasoning_effort: str = "low",
    argv_prefix: Sequence[str] = (),
    timeout_seconds: float = 120.0,
    stdout_limit_bytes: int = 4 * 1024 * 1024,
    stderr_limit_bytes: int = 1 * 1024 * 1024,
    final_validator: FinalValidator | None = None,
) -> SubjectRunResult:
    if live and not authorize_live_codex:
        raise LiveAuthorizationRequired(
            "live Codex execution requires explicit --authorize-live-codex consent"
        )
    _validate_process_limits(
        timeout_seconds=timeout_seconds,
        stdout_limit_bytes=stdout_limit_bytes,
        stderr_limit_bytes=stderr_limit_bytes,
    )
    operation = _task_operation(task)
    prompt = render_codex_prompt(task).encode("utf-8")
    argv = render_codex_argv(
        executable=executable,
        task=task,
        repo=repo,
        model=model,
        output_schema=output_schema,
        child_path=child_path,
        child_home=child_home,
        child_tmpdir=child_tmpdir,
        permission_profile=permission_profile,
        reasoning_effort=reasoning_effort,
        argv_prefix=argv_prefix,
    )
    request = EffectiveRequestReceipt(
        operation=operation,
        adapter_version=CODEX_RENDERER_VERSION,
        argv=argv,
        stdin_sha256=_sha256(prompt),
    )
    environment = sanitized_environment(
        path=path,
        home=home,
        tmpdir=tmpdir,
        codex_home=codex_home,
        auth_environment=auth_environment,
    )
    protected_values = tuple(value for value in sensitive_values if value) + tuple(
        value.encode("utf-8") for value in (auth_environment or {}).values() if value
    )
    invocation = ProcessInvocation(
        argv=argv,
        cwd=_absolute_path(repo, name="repository"),
        env=environment,
        stdin=prompt,
        timeout_seconds=timeout_seconds,
        stdout_limit_bytes=stdout_limit_bytes,
        stderr_limit_bytes=stderr_limit_bytes,
        sensitive_values=protected_values,
    )
    capture = runner.run(invocation)
    stdout, stderr, leaked = _stream_evidence(
        capture,
        stdout_limit_bytes=stdout_limit_bytes,
        stderr_limit_bytes=stderr_limit_bytes,
        sensitive_values=protected_values,
    )
    if not capture.started:
        terminal = _terminal(
            capture, "runner_internal_error", scoreable=False, detail=capture.start_error
        )
        return SubjectRunResult("codex", request, terminal, stdout, stderr)
    if leaked:
        terminal = _terminal(capture, "secret_leakage_detected", scoreable=False)
        return SubjectRunResult("codex", request, terminal, stdout, stderr)

    effective_output_limit = capture.output_limited or stdout.truncated or stderr.truncated
    incomplete = capture.timed_out or effective_output_limit or capture.signal_number is not None
    try:
        protocol = parse_codex_jsonl(
            stdout.sealed_raw,
            allow_incomplete=incomplete,
            allow_trailing_partial=capture.timed_out or effective_output_limit,
            final_validator=final_validator,
        )
    except CodexProtocolError as error:
        terminal = _terminal(
            capture,
            error.code,
            scoreable=False,
            detail=type(error).__name__,
        )
        return SubjectRunResult("codex", request, terminal, stdout, stderr)

    if capture.timed_out:
        terminal = _terminal(capture, "runner_timeout", scoreable=protocol.has_turn_activity)
    elif effective_output_limit:
        terminal = _terminal(capture, "output_limit", scoreable=protocol.has_turn_activity)
    elif capture.signal_number is not None or capture.returncode is None:
        terminal = _terminal(
            capture, "scoreable_subject_failure", scoreable=protocol.has_turn_activity
        )
    elif protocol.terminal_type == "turn.failed":
        classification = protocol.terminal_failure_class or "scoreable_subject_failure"
        terminal = _terminal(
            capture,
            classification,
            scoreable=classification == "scoreable_subject_failure" and protocol.has_turn_activity,
        )
    elif capture.returncode != 0:
        terminal = _terminal(capture, "scoreable_subject_failure", scoreable=True)
    elif protocol.final_error is not None or protocol.final_result is None:
        terminal = _terminal(
            capture, "invalid_final_schema", scoreable=True, detail=protocol.final_error
        )
    elif not _codex_status_matches_operation(operation, protocol.final_result):
        terminal = _terminal(
            capture, "invalid_final_schema", scoreable=True, detail="operation_status"
        )
    elif protocol.tool_profile_violations:
        terminal = _terminal(
            capture,
            "tool_profile_violation",
            scoreable=True,
            detail=",".join(protocol.tool_profile_violations),
        )
    else:
        terminal = _terminal(capture, "completed", scoreable=True)
    return SubjectRunResult(
        "codex",
        request,
        terminal,
        stdout,
        stderr,
        parsed_result=protocol.final_result,
        codex_protocol=protocol,
    )


__all__ = [
    "CODEX_RENDERER_VERSION",
    "DRIFT_ADAPTER_VERSION",
    "REDACTION_POLICY_VERSION",
    "BenchmarkRunnerConfigurationError",
    "BoundedSubprocessRunner",
    "CodexProtocolError",
    "CodexProtocolResult",
    "CodexUsage",
    "EffectiveRequestReceipt",
    "FinalValidator",
    "LiveAuthorizationRequired",
    "ProcessCapture",
    "ProcessInvocation",
    "ProcessRunner",
    "StreamEvidence",
    "SubjectRunResult",
    "TerminalReceipt",
    "parse_codex_jsonl",
    "render_codex_argv",
    "render_codex_prompt",
    "render_drift_argv",
    "run_codex_subject",
    "run_drift_subject",
    "sanitized_environment",
    "seal_stream",
]

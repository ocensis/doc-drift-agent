from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from drift_agent.evaluation.benchmark_runner import (
    BoundedSubprocessRunner,
    CodexProtocolError,
    LiveAuthorizationRequired,
    ProcessCapture,
    ProcessInvocation,
    SubjectRunResult,
    parse_codex_jsonl,
    render_codex_argv,
    render_codex_prompt,
    render_drift_argv,
    run_codex_subject,
    run_drift_subject,
    sanitized_environment,
    seal_stream,
)


def _task(operation: str = "repair") -> dict[str, object]:
    return {
        "protocol_version": 1,
        "operation": operation,
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


class FakeRunner:
    def __init__(self, capture: ProcessCapture) -> None:
        self.capture = capture
        self.invocations: list[ProcessInvocation] = []

    def run(self, invocation: ProcessInvocation) -> ProcessCapture:
        self.invocations.append(invocation)
        return self.capture


def _paths(tmp_path: Path) -> dict[str, Path]:
    result = {
        name: tmp_path / name
        for name in (
            "repo",
            "state",
            "bin",
            "home",
            "tmp",
            "child-bin",
            "child-home",
            "child-tmp",
            "codex-home",
        )
    }
    for path in result.values():
        path.mkdir()
    result["schema"] = tmp_path / "schema.json"
    result["schema"].write_text("{}", encoding="utf-8")
    result["codex"] = tmp_path / "fake-codex"
    result["drift"] = tmp_path / "fake-drift-agent"
    return result


def _final(status: str = "fixed") -> dict[str, object]:
    return {
        "schema_version": 1,
        "declared_status": status,
        "findings": [],
        "validation_claims": [],
    }


def _accept_final(value: Mapping[str, Any]) -> object:
    if set(value) != {"schema_version", "declared_status", "findings", "validation_claims"}:
        raise ValueError("bad fake result")
    return dict(value)


def _jsonl(*events: Mapping[str, object]) -> bytes:
    return b"".join(
        json.dumps(event, separators=(",", ":")).encode("utf-8") + b"\n" for event in events
    )


def _codex_events(*, final_text: str | None = None) -> bytes:
    text = final_text if final_text is not None else json.dumps(_final())
    return _jsonl(
        {"type": "thread.started", "thread_id": "thread-1"},
        {"type": "turn.started"},
        {
            "type": "item.started",
            "item": {"id": "cmd-1", "type": "command_execution", "command": "git diff"},
        },
        {
            "type": "item.completed",
            "item": {
                "id": "cmd-1",
                "type": "command_execution",
                "command": "git diff",
                "exit_code": 0,
            },
        },
        {
            "type": "item.completed",
            "item": {"id": "message-1", "type": "agent_message", "text": text},
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 11, "cached_input_tokens": 2, "output_tokens": 7},
        },
    )


def _run_codex(
    paths: Mapping[str, Path],
    runner: FakeRunner,
    **overrides: object,
) -> SubjectRunResult:
    arguments: dict[str, object] = {
        "executable": paths["codex"],
        "runner": runner,
        "task": _task(),
        "repo": paths["repo"],
        "model": "pinned-test-model",
        "output_schema": paths["schema"],
        "path": paths["bin"],
        "home": paths["home"],
        "tmpdir": paths["tmp"],
        "child_path": paths["child-bin"],
        "child_home": paths["child-home"],
        "child_tmpdir": paths["child-tmp"],
        "codex_home": paths["codex-home"],
        "live": False,
        "final_validator": _accept_final,
    }
    arguments.update(overrides)
    return run_codex_subject(**arguments)  # type: ignore[arg-type]


def test_renderers_are_exact_shell_free_and_support_outer_sandbox_prefix(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)

    drift = render_drift_argv(
        executable=paths["drift"],
        task=_task("check"),
        repo=paths["repo"],
        state_dir=paths["state"],
    )
    assert drift == (
        str(paths["drift"]),
        "check",
        "--repo",
        str(paths["repo"]),
        "--state-dir",
        str(paths["state"]),
        "--lock-timeout-seconds",
        "5",
        "--format",
        "json",
        "--output-version",
        "3",
    )
    assert "--semantic" not in drift

    codex = render_codex_argv(
        executable=paths["codex"],
        task=_task(),
        repo=paths["repo"],
        model="pinned-test-model",
        output_schema=paths["schema"],
        child_path=paths["child-bin"],
        child_home=paths["child-home"],
        child_tmpdir=paths["child-tmp"],
        argv_prefix=("/usr/bin/sandbox-exec", "-f", "/readonly/profile.sb"),
    )
    assert codex[:4] == (
        "/usr/bin/sandbox-exec",
        "-f",
        "/readonly/profile.sb",
        str(paths["codex"]),
    )
    assert "--ignore-user-config" not in codex
    assert "--ignore-rules" in codex
    assert "--strict-config" in codex
    assert "--sandbox" not in codex
    assert 'default_permissions="benchmark"' in codex
    assert "features.multi_agent=false" in codex
    assert 'model_reasoning_effort="low"' in codex
    assert codex[-3:] == ("--output-schema", str(paths["schema"]), "-")
    child_config = codex[codex.index('shell_environment_policy.inherit="none"') + 2]
    assert "drift-agent" not in child_config
    assert "OPENAI_API_KEY" not in child_config
    assert render_codex_prompt(_task()).startswith("Operation: REPAIR.\n")


def test_sanitized_environment_never_inherits_parent_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    monkeypatch.setenv("UNRELATED_TOKEN", "must-not-leak")
    environment = sanitized_environment(
        path=paths["bin"],
        home=paths["home"],
        tmpdir=paths["tmp"],
        codex_home=paths["codex-home"],
        auth_environment={"OPENAI_API_KEY": "dedicated-test-secret"},
    )

    assert environment["OPENAI_API_KEY"] == "dedicated-test-secret"
    assert "PYTHONPATH" not in environment
    assert "UNRELATED_TOKEN" not in environment
    assert environment["PATH"] == str(paths["bin"])
    with pytest.raises(ValueError, match="unsupported Codex auth variable"):
        sanitized_environment(
            path=paths["bin"],
            home=paths["home"],
            tmpdir=paths["tmp"],
            auth_environment={"GITHUB_TOKEN": "nope"},
        )


def test_live_codex_requires_authorization_before_runner_call(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    runner = FakeRunner(ProcessCapture(started=True, returncode=0))

    with pytest.raises(LiveAuthorizationRequired):
        _run_codex(paths, runner, live=True, authorize_live_codex=False)

    assert runner.invocations == []


def test_invalid_process_budget_is_rejected_before_runner_call(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    runner = FakeRunner(ProcessCapture(started=True, returncode=0))

    with pytest.raises(ValueError, match="timeout must be positive"):
        _run_codex(paths, runner, timeout_seconds=0)

    assert runner.invocations == []


@pytest.mark.parametrize(
    ("status", "exit_code"),
    [
        ("clean", 0),
        ("fixed", 0),
        ("drift_found", 1),
        ("partial", 1),
        ("needs_approval", 1),
        ("unresolved", 1),
        ("stale", 2),
        ("failed", 2),
    ],
)
def test_drift_exit_status_matrix_is_business_success(
    tmp_path: Path, status: str, exit_code: int
) -> None:
    paths = _paths(tmp_path)
    payload = {
        "schema_version": 3,
        "status": status,
        "run_id": "run-1",
        "snapshot": {
            "head_revision": "head",
            "workspace_fingerprint": "fingerprint",
            "input_file_hashes": {},
        },
        "scope": [],
        "findings": [],
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    runner = FakeRunner(
        ProcessCapture(
            started=True,
            stdout=raw,
            returncode=exit_code,
            stdout_total_bytes=len(raw),
        )
    )

    result = run_drift_subject(
        executable=paths["drift"],
        runner=runner,
        task=_task(),
        repo=paths["repo"],
        state_dir=paths["state"],
        path=paths["bin"],
        home=paths["home"],
        tmpdir=paths["tmp"],
    )

    assert result.terminal.classification == "completed"
    assert result.terminal.scoreable is True
    assert result.parsed_result is not None
    assert len(runner.invocations) == 1


def test_drift_invalid_bundle_or_exit_mismatch_is_scoreable_and_not_retried(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    runner = FakeRunner(
        ProcessCapture(
            started=True,
            stdout=b'{"schema_version":3}',
            returncode=0,
            stdout_total_bytes=20,
        )
    )

    result = run_drift_subject(
        executable=paths["drift"],
        runner=runner,
        task=_task(),
        repo=paths["repo"],
        state_dir=paths["state"],
        path=paths["bin"],
        home=paths["home"],
        tmpdir=paths["tmp"],
    )

    assert result.terminal.classification == "invalid_final_schema"
    assert result.terminal.scoreable is True
    assert result.terminal.no_retry is True
    assert len(runner.invocations) == 1


def test_codex_parser_uses_last_completed_agent_message_and_terminal_usage() -> None:
    protocol = parse_codex_jsonl(_codex_events(), final_validator=_accept_final)

    assert protocol.terminal_type == "turn.completed"
    assert protocol.final_result == _final()
    assert protocol.usage.input_tokens == 11
    assert protocol.usage.cached_input_tokens == 2
    assert protocol.usage.output_tokens == 7
    assert protocol.usage.tool_calls == 1
    assert protocol.tool_profile_violations == ()


def test_codex_parser_does_not_search_back_from_invalid_last_message() -> None:
    raw = _jsonl(
        {"type": "thread.started", "thread_id": "thread-1"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {
                "id": "message-1",
                "type": "agent_message",
                "text": json.dumps(_final()),
            },
        },
        {
            "type": "item.completed",
            "item": {"id": "message-2", "type": "agent_message", "text": "not-json"},
        },
        {"type": "turn.completed", "usage": {}},
    )

    protocol = parse_codex_jsonl(raw, final_validator=_accept_final)

    assert protocol.final_result is None
    assert protocol.final_error == "JSONDecodeError"


def test_codex_parser_rejects_unknown_events_and_missing_terminal() -> None:
    with pytest.raises(CodexProtocolError) as unknown:
        parse_codex_jsonl(
            _jsonl(
                {"type": "thread.started", "thread_id": "thread-1"},
                {"type": "mystery.event"},
            )
        )
    assert unknown.value.code == "invalid_jsonl"

    with pytest.raises(CodexProtocolError) as missing:
        parse_codex_jsonl(
            _jsonl(
                {"type": "thread.started", "thread_id": "thread-1"},
                {"type": "turn.started"},
            )
        )
    assert missing.value.code == "missing_terminal_event"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ({"message": "Unauthorized: login required"}, "auth_failed"),
        (
            {"message": "The requested model is unavailable"},
            "model_unavailable",
        ),
        (
            {"message": "Rate limit exceeded by the upstream provider"},
            "rate_limited_or_provider_error",
        ),
        (
            {"message": "request failed", "code": "invalid_api_key"},
            "auth_failed",
        ),
        (
            {"message": "request failed", "type": "model_not_found"},
            "model_unavailable",
        ),
        (
            {"message": "request failed", "name": "server_overloaded"},
            "rate_limited_or_provider_error",
        ),
        (
            {"message": "Invalid JSON schema for response_format"},
            "runner_internal_error",
        ),
        ({"message": "turn failed"}, None),
    ],
)
def test_codex_parser_classifies_v0144_turn_failure_messages_and_future_fields(
    error: dict[str, str], expected: str | None
) -> None:
    protocol = parse_codex_jsonl(
        _jsonl(
            {"type": "thread.started", "thread_id": "thread-1"},
            {"type": "turn.started"},
            {"type": "turn.failed", "error": error},
        )
    )

    assert protocol.terminal_type == "turn.failed"
    assert protocol.terminal_failure_class == expected


@pytest.mark.parametrize("error", [None, {}, {"message": ""}, {"message": "   "}, {"message": 401}])
def test_codex_parser_requires_nonempty_turn_failed_error_message(error: object) -> None:
    with pytest.raises(CodexProtocolError) as failure:
        parse_codex_jsonl(
            _jsonl(
                {"type": "thread.started", "thread_id": "thread-1"},
                {"type": "turn.started"},
                {"type": "turn.failed", "error": error},
            )
        )

    assert failure.value.code == "invalid_jsonl"


def test_codex_timeout_is_scoreable_only_after_valid_turn_activity(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    after_turn = (
        _jsonl(
            {"type": "thread.started", "thread_id": "thread-1"},
            {"type": "turn.started"},
        )
        + b'{"type":'
    )
    started_runner = FakeRunner(
        ProcessCapture(
            started=True,
            stdout=after_turn,
            returncode=-15,
            timed_out=True,
            signal_number=15,
            stdout_total_bytes=len(after_turn),
        )
    )
    started = _run_codex(paths, started_runner)
    assert started.terminal.classification == "runner_timeout"
    assert started.terminal.scoreable is True
    assert started.codex_protocol is not None
    assert started.codex_protocol.ignored_trailing_bytes == len(b'{"type":')

    before_turn = _jsonl({"type": "thread.started", "thread_id": "thread-1"})
    pre_runner = FakeRunner(
        ProcessCapture(
            started=True,
            stdout=before_turn,
            returncode=-15,
            timed_out=True,
            signal_number=15,
            stdout_total_bytes=len(before_turn),
        )
    )
    pre = _run_codex(paths, pre_runner)
    assert pre.terminal.classification == "runner_timeout"
    assert pre.terminal.scoreable is False


def test_codex_valid_run_and_forbidden_tool_profile(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    raw = _codex_events()
    completed = _run_codex(
        paths,
        FakeRunner(
            ProcessCapture(
                started=True,
                stdout=raw,
                returncode=0,
                stdout_total_bytes=len(raw),
            )
        ),
        argv_prefix=("/usr/bin/sandbox-exec", "-f", "/readonly/profile.sb"),
    )
    assert completed.terminal.classification == "completed"
    assert completed.parsed_result == _final()
    assert completed.request.argv[0] == "/usr/bin/sandbox-exec"

    web_raw = _jsonl(
        {"type": "thread.started", "thread_id": "thread-1"},
        {"type": "turn.started"},
        {"type": "item.started", "item": {"id": "web-1", "type": "web_search"}},
        {"type": "item.completed", "item": {"id": "web-1", "type": "web_search"}},
        {
            "type": "item.completed",
            "item": {
                "id": "message-1",
                "type": "agent_message",
                "text": json.dumps(_final()),
            },
        },
        {"type": "turn.completed", "usage": {}},
    )
    violation = _run_codex(
        paths,
        FakeRunner(
            ProcessCapture(
                started=True,
                stdout=web_raw,
                returncode=0,
                stdout_total_bytes=len(web_raw),
            )
        ),
    )
    assert violation.terminal.classification == "tool_profile_violation"
    assert violation.terminal.scoreable is True


def test_codex_result_status_must_match_task_operation(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    raw = _codex_events(final_text=json.dumps(_final("fixed")))
    result = _run_codex(
        paths,
        FakeRunner(
            ProcessCapture(
                started=True,
                stdout=raw,
                returncode=0,
                stdout_total_bytes=len(raw),
            )
        ),
        task=_task("check"),
    )

    assert result.terminal.classification == "invalid_final_schema"
    assert result.terminal.detail == "operation_status"
    assert result.terminal.scoreable is True


def test_sealed_evidence_redacts_and_live_secret_leak_stops_scoring(tmp_path: Path) -> None:
    secret = b"benchmark-secret-value"
    evidence = seal_stream(
        b"prefix " + secret + b" api_key=another-value",
        total_bytes=52,
        byte_limit=100,
        sensitive_values=(secret,),
    )
    assert secret not in evidence.redacted
    assert evidence.explicit_secret_replacements == 1
    assert evidence.generic_replacements == 1
    assert evidence.sealed_raw != evidence.redacted

    paths = _paths(tmp_path)
    raw = _codex_events() + secret
    result = _run_codex(
        paths,
        FakeRunner(
            ProcessCapture(
                started=True,
                stdout=raw,
                returncode=0,
                stdout_total_bytes=len(raw),
            )
        ),
        auth_environment={"OPENAI_API_KEY": secret.decode("ascii")},
    )
    assert result.terminal.classification == "secret_leakage_detected"
    assert result.terminal.scoreable is False
    assert secret not in result.stdout.redacted


def test_bounded_subprocess_runner_enforces_output_cap_and_timeout(tmp_path: Path) -> None:
    environment = sanitized_environment(path="/usr/bin", home=tmp_path, tmpdir=tmp_path)
    output = BoundedSubprocessRunner().run(
        ProcessInvocation(
            argv=(sys.executable, "-c", "import os; os.write(1, b'x' * 4096)"),
            cwd=tmp_path,
            env=environment,
            stdin=b"",
            timeout_seconds=2,
            stdout_limit_bytes=32,
            stderr_limit_bytes=32,
        )
    )
    assert output.started is True
    assert output.output_limited is True
    assert len(output.stdout) == 32
    assert output.stdout_total_bytes > 32

    timeout = BoundedSubprocessRunner().run(
        ProcessInvocation(
            argv=(sys.executable, "-c", "import time; time.sleep(2)"),
            cwd=tmp_path,
            env=environment,
            stdin=b"",
            timeout_seconds=0.05,
            stdout_limit_bytes=32,
            stderr_limit_bytes=32,
            terminate_grace_seconds=0.05,
        )
    )
    assert timeout.started is True
    assert timeout.timed_out is True
    assert timeout.signal_number in {9, 15}

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

SINGLE_AGENT_DIR = Path(__file__).resolve().parents[3] / "evals" / "field" / "single_agent"
sys.path.insert(0, str(SINGLE_AGENT_DIR))

import _portfolio_gitnexus_focused_exact as focused_exact  # noqa: E402
import score_tool_portfolio as scorer  # noqa: E402
from _portfolio_generic import paged_generic_runtime  # noqa: E402
from _runner import BASE_TOOLS, AgentContext, SingleAgentRunner  # noqa: E402

STREAMLAKE_ROUTING = {
    "require_parameters": True,
    "data_collection": "deny",
    "order": ["streamlake"],
    "only": ["streamlake"],
    "allow_fallbacks": False,
}


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    docs = repo / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text("# Stale claim\n\nDetails.\n", encoding="utf-8")
    source = repo / "src"
    source.mkdir()
    (source / "current.py").write_text("def previous():\n    return False\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "baseline")
    (source / "current.py").write_text("def current():\n    return True\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "frozen head")
    return repo, _git(repo, "rev-parse", "HEAD")


def _configuration(
    provider: dict[str, Any],
    *,
    protocol_version: str,
    has_graph_tool: bool,
) -> dict[str, Any]:
    configuration: dict[str, Any] = {name: None for name in scorer.UNBOUNDED_CONFIGURATION_FIELDS}
    configuration.update(
        {
            "conversation_history_trimming": False,
            "model": {
                "profile": "strong",
                "reasoning_effort": "high",
                "temperature": 1.0,
            },
            "transport": {
                "request_timeout_seconds": 300.0,
                "retry_attempts": 3,
                "provider_output_token_request": 64_000,
                "provider_routing": provider,
                "response_body_byte_limit": scorer.EXPECTED_RESPONSE_BODY_BYTE_LIMIT,
            },
        }
    )
    if protocol_version == scorer.PROTOCOL_V2:
        configuration["graph_manipulation"] = scorer._expected_graph_manipulation(has_graph_tool)
    return configuration


def _finding() -> dict[str, Any]:
    return {
        "doc": "docs/guide.md",
        "line": 1,
        "quote": "Stale claim",
        "why": "The current implementation differs.",
        "code_evidence": "HEAD src/current.py:1 now owns the behavior.",
        "confidence": "high",
    }


def _artifact(
    *,
    repo: Path,
    revision: str,
    agent: str,
    pair_id: str,
    optional_tool: str | None = None,
    adopt: bool = True,
    provider: dict[str, Any] | None = None,
    outcome: str = "ok",
    hits: bool = True,
    total_seconds: float = 10.0,
    actual_tokens: int = 1_000,
    protocol_version: str = scorer.PROTOCOL_VERSION,
    raw_submit_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    provider = dict(STREAMLAKE_ROUTING if provider is None else provider)
    baseline_revision = _git(repo, "rev-parse", f"{revision}^")
    names = list(BASE_TOOLS)
    if optional_tool is not None:
        names.append(optional_tool)

    calls: list[list[str]] = []
    if optional_tool is not None and adopt:
        if optional_tool == "graph_context":
            calls.append(["git_diff"])
        calls.append([optional_tool])
    if outcome in {"ok", "submit_schema_invalid"}:
        calls.append(["submit"])
    else:
        calls.append([])
    turns = len(calls)
    forced_turn = 2 if optional_tool == "graph_context" and adopt else None
    turn_trace = []
    for index, turn_calls in enumerate(calls, start=1):
        entry: dict[str, Any] = {
            "turn": index,
            "finish_reason": "tool_calls",
            "tool_calls": turn_calls,
        }
        if protocol_version == scorer.PROTOCOL_V2:
            entry.update(
                {
                    "tool_choice": (
                        scorer.FORCED_GRAPH_TOOL_CHOICE if index == forced_turn else "auto"
                    ),
                    "forced_tool": "graph_context" if index == forced_turn else None,
                }
            )
        turn_trace.append(entry)

    tool_trace: list[dict[str, Any]] = []
    tool_counts: dict[str, int] = {}
    for turn, turn_calls in enumerate(calls, start=1):
        for ordinal, name in enumerate(turn_calls, start=1):
            tool_counts[name] = tool_counts.get(name, 0) + 1
            entry: dict[str, Any] = {
                "turn": turn,
                "ordinal": ordinal,
                "name": name,
                "arguments": {},
                "seconds": 0.01,
                "result_chars": 10,
                "arguments_sha256": hashlib.sha256(b"{}").hexdigest(),
            }
            if name == "submit":
                entry.update({"terminal": True, "seconds": 0.0, "result_chars": 0})
            else:
                entry["error"] = False
                if name == "graph_context":
                    entry.update(
                        {
                            "arguments": {"targets": ["current"]},
                            "graph_result_kinds": ["match", "relationship"],
                            "graph_exact_context": True,
                            "graph_result_targets": ["current"],
                        }
                    )
            tool_trace.append(entry)

    output_tokens = min(actual_tokens, max(1, turns * 10))
    input_tokens = actual_tokens - output_tokens
    per_turn_inputs = [input_tokens // turns for _index in range(turns)]
    per_turn_outputs = [output_tokens // turns for _index in range(turns)]
    per_turn_inputs[-1] += input_tokens - sum(per_turn_inputs)
    per_turn_outputs[-1] += output_tokens - sum(per_turn_outputs)
    model_trace = [
        {
            "conversation_turn": index,
            "seconds": 1.0,
            "request_id": f"request-{index}",
            "actual_model": "z-ai/glm-5.2",
            "input_tokens": per_turn_inputs[index - 1],
            "output_tokens": per_turn_outputs[index - 1],
            "cost_usd": 0.01,
            "finish_reason": "tool_calls",
            **(
                {
                    "tool_choice": (
                        scorer.FORCED_GRAPH_TOOL_CHOICE if index == forced_turn else "auto"
                    ),
                    "forced_tool": "graph_context" if index == forced_turn else None,
                }
                if protocol_version == scorer.PROTOCOL_V2
                else {}
            ),
        }
        for index in range(1, turns + 1)
    ]
    transport_attempt_trace = [
        {
            "attempt": 1,
            "seconds": entry["seconds"],
            "status": "success",
            "request_id": entry["request_id"],
            "actual_model": entry["actual_model"],
            "input_tokens": entry["input_tokens"],
            "output_tokens": entry["output_tokens"],
            **(
                {"tool_choice": entry["tool_choice"]}
                if protocol_version == scorer.PROTOCOL_V2
                else {}
            ),
        }
        for entry in model_trace
    ]

    raw_submit: dict[str, Any] | None
    delivered: list[dict[str, Any]]
    if outcome == "ok":
        delivered = [_finding()] if hits else []
        raw_submit = {"findings": delivered} if raw_submit_override is None else raw_submit_override
        delivered = [] if raw_submit == {} else list(raw_submit.get("findings", []))
        failure_reason = None
        conversation_ok = True
    elif outcome == "submit_schema_invalid":
        delivered = []
        raw_submit = (
            {"findings": [{"bad": "shape"}]} if raw_submit_override is None else raw_submit_override
        )
        failure_reason = outcome
        conversation_ok = False
    else:
        delivered = []
        raw_submit = None
        failure_reason = outcome
        conversation_ok = False

    artifact_started = time.time_ns() - 20_000_000
    run_started = artifact_started + 1_000
    completed = time.time_ns() - 10_000_000
    definitions = [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": f"Synthetic {name}",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in names
    ]
    submit_parameters: dict[str, Any] = {
        "type": "object",
        "properties": {"findings": {"type": "array", "items": {"type": "object"}}},
    }
    if protocol_version == scorer.PROTOCOL_V2:
        submit_parameters["required"] = ["findings"]
    definitions[names.index("submit")]["function"]["parameters"] = submit_parameters
    canonical = lambda value: json.dumps(  # noqa: E731
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    digest = lambda value: hashlib.sha256(canonical(value).encode()).hexdigest()  # noqa: E731
    system_prompt = "portfolio system prompt"
    user_prompt = f"compare {baseline_revision} to {revision}"
    provider_hash = digest(provider)
    run = {
        "agent": agent,
        "protocol_version": protocol_version,
        "generation_started_at_ns": run_started,
        "generation_completed_at_ns": completed,
        "completed_at_ns": completed,
        "baseline_revision": baseline_revision,
        "head_revision": revision,
        "prompt": {
            "system": system_prompt,
            "user": user_prompt,
            "system_sha256": hashlib.sha256(system_prompt.encode()).hexdigest(),
            "user_sha256": hashlib.sha256(user_prompt.encode()).hexdigest(),
        },
        "tools": {
            "names": names,
            "schema_sha256": digest(definitions),
            "canonical_schema_chars": len(canonical(definitions)),
            "schemas": [
                {
                    "name": definition["function"]["name"],
                    "sha256": digest(definition),
                    "canonical_chars": len(canonical(definition)),
                }
                for definition in definitions
            ],
            "definitions": definitions,
            "base_names": list(BASE_TOOLS),
            "base_schema_sha256": digest(definitions[: len(BASE_TOOLS)]),
        },
        "configuration": _configuration(
            provider,
            protocol_version=protocol_version,
            has_graph_tool=optional_tool == "graph_context",
        ),
        "conversation": {
            "ok": conversation_ok,
            "failure_reason": failure_reason,
            "turns": turns,
            "tool_calls": sum(len(turn_calls) for turn_calls in calls),
            "actual_models": ["z-ai/glm-5.2"],
            "tool_counts": tool_counts,
            "tool_errors": {},
            "tool_result_chars": {},
            "turn_trace": turn_trace,
            "tool_trace": tool_trace,
            "model_call_trace": model_trace,
            "transport_attempt_trace": transport_attempt_trace,
            **(
                {
                    "manipulation_trace": (
                        [
                            {
                                "event": "forced_tool_request_armed",
                                "conversation_turn": 1,
                                "trigger_tool": "git_diff",
                                "successful_ordinals": [1],
                                "forced_tool": "graph_context",
                                "automatic_target_generation": False,
                            },
                            {
                                "event": "forced_tool_request_started",
                                "conversation_turn": 2,
                                "tool": "graph_context",
                                "trigger": "prior_successful_git_diff",
                            },
                            {
                                "event": "forced_tool_request_completed",
                                "conversation_turn": 2,
                                "tool": "graph_context",
                                "request_id": "request-2",
                                "observed_tool_calls": ["graph_context"],
                                "auto_restored_for_next_request": True,
                            },
                        ]
                        if forced_turn is not None
                        else []
                    ),
                    "terminal_assistant_content": {
                        "kind": "null",
                        "chars": 0,
                        "bytes": 0,
                        "sha256": hashlib.sha256(b"").hexdigest(),
                    },
                    "submit_shape": (
                        {
                            "findings_field_present": "findings" in raw_submit,
                            "findings_is_list": isinstance(raw_submit.get("findings"), list),
                            "finding_count": (
                                len(raw_submit["findings"])
                                if isinstance(raw_submit.get("findings"), list)
                                else None
                            ),
                            "implicit_empty": "findings" not in raw_submit,
                            "explicit_empty": (
                                "findings" in raw_submit
                                and isinstance(raw_submit.get("findings"), list)
                                and not raw_submit["findings"]
                            ),
                        }
                        if raw_submit is not None
                        else None
                    ),
                }
                if protocol_version == scorer.PROTOCOL_V2
                else {}
            ),
        },
        "raw_submit": raw_submit,
        "submission_only": delivered,
        "store": [],
        "delivered": delivered,
        "delivery": {},
        "setup": {},
        "timing": {
            "setup_seconds": 1.0,
            "agent_seconds": total_seconds - 1.0,
            "cleanup_seconds": 0.0,
            "total_seconds": total_seconds,
        },
        "usage": {
            "model_calls": turns,
            "model_calls_by_profile": {"strong": turns},
            "tool_calls": sum(len(turn_calls) for turn_calls in calls),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "estimated_cost_usd": 0.01 * turns,
            "duration_ms": int((total_seconds - 1.0) * 1_000),
        },
        "run": 1,
        "pair_key": f"{pair_id}.1",
        "requested_model": "z-ai/glm-5.2",
        "provider_routing": provider,
        "provider_routing_sha256": provider_hash,
        "openrouter_base_url": scorer.EXPECTED_OPENROUTER_BASE_URL,
    }
    artifact = {
        "protocol_version": protocol_version,
        "agent": agent,
        "pair_id": pair_id,
        "target": {"kind": "repo", "path": str(repo.resolve())},
        "baseline_revision": baseline_revision,
        "head_revision": revision,
        "requested_model": "z-ai/glm-5.2",
        "provider_routing": provider,
        "provider_routing_sha256": provider_hash,
        "openrouter_base_url": scorer.EXPECTED_OPENROUTER_BASE_URL,
        "langfuse_enabled": False,
        "generation_started_at_ns": artifact_started,
        "generation_completed_at_ns": completed,
        "completed_at_ns": completed,
        "runs": [run],
    }

    if optional_tool == "graph_context" and adopt:
        runtime = paged_generic_runtime(
            AgentContext(
                repo_path=repo,
                baseline_revision=baseline_revision,
                head_revision=revision,
            )
        )
        diff_arguments: dict[str, Any] = {}
        diff_content = runtime.extra_tools["git_diff"][1](diff_arguments)
        diff_trace = next(entry for entry in tool_trace if entry.get("name") == "git_diff")
        diff_trace.update(scorer._result_trace(diff_content))
        diff_trace["arguments_sha256"] = hashlib.sha256(b"{}").hexdigest()
        run["setup"] = runtime.metadata
    return artifact


def _write_artifact(path: Path, artifact: dict[str, Any]) -> Path:
    path.write_text(json.dumps(artifact, indent=1), encoding="utf-8")
    return path


def _rehash_tools(run: dict[str, Any]) -> None:
    tools = run["tools"]
    definitions = tools["definitions"]
    canonical = lambda value: json.dumps(  # noqa: E731
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    digest = lambda value: hashlib.sha256(canonical(value).encode()).hexdigest()  # noqa: E731
    tools["schema_sha256"] = digest(definitions)
    tools["canonical_schema_chars"] = len(canonical(definitions))
    tools["base_schema_sha256"] = digest(definitions[: len(BASE_TOOLS)])
    tools["schemas"] = [
        {
            "name": definition["function"]["name"],
            "sha256": digest(definition),
            "canonical_chars": len(canonical(definition)),
        }
        for definition in definitions
    ]


def _add_rejected_same_turn_broad_scan(artifact: dict[str, Any]) -> None:
    run = artifact["runs"][0]
    conversation = run["conversation"]
    arguments = {"pattern": "current", "glob": "**/*"}
    conversation["turn_trace"][0]["tool_calls"].append("grep")
    conversation["tool_trace"].insert(
        1,
        {
            "turn": 1,
            "ordinal": 2,
            "name": "grep",
            "arguments": arguments,
            "arguments_sha256": scorer._hash_json(arguments),
            "seconds": 0.0,
            "error": True,
            "result_chars": 100,
            "result_bytes": 100,
            "result_lines": 1,
        },
    )
    conversation["tool_counts"]["grep"] = 1
    conversation["tool_calls"] += 1
    run["usage"]["tool_calls"] += 1
    conversation["manipulation_trace"].insert(
        0,
        {
            "event": "same_turn_broad_scan_rejected",
            "conversation_turn": 1,
            "ordinal": 2,
            "tool": "grep",
            "policy": "reject_without_execution",
        },
    )


def _launch_manifest(
    path: Path,
    artifacts: list[Path],
    *,
    wall_seconds: dict[str, float] | None = None,
    failed_agent: str | None = None,
    child_parent_map: dict[str, str] | None = None,
) -> Path:
    wall_seconds = {} if wall_seconds is None else wall_seconds
    identities: list[tuple[str, str, Path]] = []
    for artifact_path in artifacts:
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        identities.append((payload["pair_id"], payload["agent"], artifact_path.resolve()))
    pairs = list(dict.fromkeys(pair for pair, _agent, _path in identities))
    agents = list(dict.fromkeys(agent for _pair, agent, _path in identities))
    snapshot_base = max(artifact.stat().st_mtime_ns for artifact in artifacts) + 1_000
    jobs: list[dict[str, Any]] = []
    schedule: list[dict[str, Any]] = []
    for ordinal, (pair, agent, artifact_path) in enumerate(identities, start=1):
        stat = artifact_path.stat()
        raw = artifact_path.read_bytes()
        snapshot_at = snapshot_base + ordinal * 1_000
        wall = float(wall_seconds.get(agent, 0.05))
        popen_started = snapshot_at - int(wall * 1_000_000_000)
        child_exited = max(popen_started, snapshot_at - 1_000)
        process_wall = (child_exited - popen_started) / 1_000_000_000
        snapshot_overhead = (snapshot_at - child_exited) / 1_000_000_000
        schedule.append({"schedule_ordinal": ordinal, "pair_id": pair, "agent": agent})
        jobs.append(
            {
                "schedule_ordinal": ordinal,
                "pair_id": pair,
                "agent": agent,
                "artifact": str(artifact_path),
                "artifact_snapshot": {
                    "exists": True,
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "size_bytes": len(raw),
                    "mtime_ns": stat.st_mtime_ns,
                },
                "artifact_snapshot_at_ns": snapshot_at,
                "popen_started_at_ns": popen_started,
                "child_exited_at_ns": child_exited,
                "process_wall_seconds": round(process_wall, 6),
                "end_to_end_wall_seconds": round(
                    (snapshot_at - popen_started) / 1_000_000_000,
                    6,
                ),
                "artifact_snapshot_overhead_seconds": round(snapshot_overhead, 6),
                "returncode": 1 if agent == failed_agent else 0,
            }
        )
    frozen_at = max(job["artifact_snapshot_at_ns"] for job in jobs) + 1_000
    batch_started = min(job["popen_started_at_ns"] for job in jobs) - 1_000
    starts = [job["popen_started_at_ns"] for job in jobs]
    manifest = {
        "raw_generation_only": True,
        "ground_truth_loaded": False,
        "fixture_export_scope": scorer.EXPECTED_EXPORT_SCOPE,
        "authorization_reference": "unit-test-authorization",
        "provider": "streamlake",
        "provider_fallbacks": False,
        "openrouter_base_url": scorer.EXPECTED_OPENROUTER_BASE_URL,
        "job_count": len(jobs),
        "pair_count": len(pairs),
        "all_started_before_wait": True,
        "schedule_mode": "all-popen-starts-before-any-wait",
        "agents": agents,
        "planned_agents": agents,
        "planned_pairs": pairs,
        "planned_schedule": schedule,
        "batch_started_at_ns": batch_started,
        "batch_completed_at_ns": frozen_at,
        "artifact_frozen_at_ns": frozen_at,
        "launch_spread_seconds": round((max(starts) - min(starts)) / 1e9, 6),
        "batch_makespan_seconds": round((frozen_at - batch_started) / 1e9, 6),
        "jobs": jobs,
    }
    if child_parent_map is not None:
        manifest["child_parent_map"] = dict(child_parent_map)
    path.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return path


def _score(
    artifacts: list[Path],
    ground_truth: Path,
    *,
    manifest_path: Path | None = None,
    wall_seconds: dict[str, float] | None = None,
    child_parent_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    manifest = (
        _launch_manifest(
            artifacts[0].parent / "launch-manifest.json",
            artifacts,
            wall_seconds=wall_seconds,
            child_parent_map=child_parent_map,
        )
        if manifest_path is None
        else manifest_path
    )
    return scorer.score_artifacts(artifacts, ground_truth, manifest)


def _ground_truth(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "window_lines": 40,
                "items": [
                    {
                        "label": "stale-claim",
                        "doc": "docs/guide.md",
                        "line": 1,
                        "class": "prose",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_v1_implicit_empty_is_valid_and_reported(tmp_path: Path) -> None:
    repo, revision = _repo(tmp_path)
    artifact = _write_artifact(
        tmp_path / "v1.json",
        _artifact(
            repo=repo,
            revision=revision,
            agent=scorer.DEFAULT_CONTROL_AGENT,
            pair_id="pair-1",
            protocol_version=scorer.PROTOCOL_V1,
            raw_submit_override={},
        ),
    )

    report = _score([artifact], _ground_truth(tmp_path / "ground-truth.json"))

    assert report["protocol_version"] == scorer.PROTOCOL_V1
    assert report["protocol_audit"]["observed_protocol_version"] == scorer.PROTOCOL_V1
    assert report["runs"][0]["conversation_ok"] is True
    assert report["runs"][0]["metrics"]["evidence_valid_hits"] == 0


def test_v2_implicit_empty_is_invalid_but_explicit_empty_is_valid(tmp_path: Path) -> None:
    repo, revision = _repo(tmp_path)
    implicit = _write_artifact(
        tmp_path / "implicit.json",
        _artifact(
            repo=repo,
            revision=revision,
            agent=scorer.DEFAULT_CONTROL_AGENT,
            pair_id="implicit",
            outcome="submit_schema_invalid",
            raw_submit_override={},
        ),
    )
    implicit_report = _score(
        [implicit],
        _ground_truth(tmp_path / "implicit-ground-truth.json"),
    )
    assert implicit_report["runs"][0]["failure_reason"] == "submit_schema_invalid"

    explicit = _write_artifact(
        tmp_path / "explicit.json",
        _artifact(
            repo=repo,
            revision=revision,
            agent=scorer.DEFAULT_CONTROL_AGENT,
            pair_id="explicit",
            raw_submit_override={"findings": []},
        ),
    )
    explicit_report = _score(
        [explicit],
        _ground_truth(tmp_path / "explicit-ground-truth.json"),
    )
    assert explicit_report["protocol_version"] == scorer.PROTOCOL_V2
    assert explicit_report["runs"][0]["conversation_ok"] is True


def test_stage_rejects_mixed_protocols_before_ground_truth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, revision = _repo(tmp_path)
    v1 = _write_artifact(
        tmp_path / "v1.json",
        _artifact(
            repo=repo,
            revision=revision,
            agent=scorer.DEFAULT_CONTROL_AGENT,
            pair_id="pair-1",
            protocol_version=scorer.PROTOCOL_V1,
        ),
    )
    v2 = _write_artifact(
        tmp_path / "v2.json",
        _artifact(
            repo=repo,
            revision=revision,
            agent="brief_diff_agent",
            pair_id="pair-1",
            optional_tool="audit_brief",
        ),
    )
    monkeypatch.setattr(
        scorer,
        "_read_ground_truth_bytes",
        lambda _path: (_ for _ in ()).throw(AssertionError("GT must remain closed")),
    )

    with pytest.raises(ValueError, match="mixes protocol versions"):
        _score([v1, v2], tmp_path / "unused.json")


@pytest.mark.parametrize(
    ("protocol_version", "required"),
    [(scorer.PROTOCOL_V1, ["findings"]), (scorer.PROTOCOL_V2, [])],
)
def test_submit_schema_requirement_is_protocol_bound(
    tmp_path: Path,
    protocol_version: str,
    required: list[str],
) -> None:
    repo, revision = _repo(tmp_path)
    payload = _artifact(
        repo=repo,
        revision=revision,
        agent=scorer.DEFAULT_CONTROL_AGENT,
        pair_id="pair-1",
        protocol_version=protocol_version,
    )
    run = payload["runs"][0]
    run["tools"]["definitions"][BASE_TOOLS.index("submit")]["function"]["parameters"][
        "required"
    ] = required
    _rehash_tools(run)
    artifact = _write_artifact(tmp_path / "schema.json", payload)

    with pytest.raises(ValueError, match="findings requirement"):
        _score([artifact], tmp_path / "unused.json")


@pytest.mark.parametrize("tamper", ["transport_choice", "completion_event", "arming"])
def test_v2_forced_graph_manipulation_tampering_fails_closed(
    tmp_path: Path,
    tamper: str,
) -> None:
    repo, revision = _repo(tmp_path)
    control = _write_artifact(
        tmp_path / "control.json",
        _artifact(
            repo=repo,
            revision=revision,
            agent=scorer.DEFAULT_CONTROL_AGENT,
            pair_id="pair-1",
        ),
    )
    graph_payload = _artifact(
        repo=repo,
        revision=revision,
        agent="codegraph_context_agent",
        pair_id="pair-1",
        optional_tool="graph_context",
    )
    conversation = graph_payload["runs"][0]["conversation"]
    if tamper == "transport_choice":
        conversation["transport_attempt_trace"][1]["tool_choice"] = "auto"
    elif tamper == "completion_event":
        conversation["manipulation_trace"][2]["auto_restored_for_next_request"] = False
    else:
        conversation["manipulation_trace"][0]["successful_ordinals"] = [2]
    graph = _write_artifact(tmp_path / "graph.json", graph_payload)

    with pytest.raises(ValueError, match=r"tool_choice|completion|arming"):
        _score([control, graph], tmp_path / "unused.json")


def test_rejected_same_turn_broad_scan_is_audited_but_not_adoption_order(
    tmp_path: Path,
) -> None:
    repo, revision = _repo(tmp_path)
    control = _write_artifact(
        tmp_path / "control.json",
        _artifact(
            repo=repo,
            revision=revision,
            agent=scorer.DEFAULT_CONTROL_AGENT,
            pair_id="pair-1",
        ),
    )
    graph_payload = _artifact(
        repo=repo,
        revision=revision,
        agent="codegraph_context_agent",
        pair_id="pair-1",
        optional_tool="graph_context",
    )
    _add_rejected_same_turn_broad_scan(graph_payload)
    graph = _write_artifact(tmp_path / "graph.json", graph_payload)

    report = _score([control, graph], _ground_truth(tmp_path / "ground-truth.json"))
    graph_run = next(row for row in report["runs"] if row["agent"] == "codegraph_context_agent")
    assert graph_run["adoption"]["graph_context"]["passed"] is True

    graph_payload["runs"][0]["conversation"]["manipulation_trace"].pop(0)
    tampered = _write_artifact(tmp_path / "graph-tampered.json", graph_payload)
    with pytest.raises(ValueError, match="first successful diff turn"):
        _score([control, tampered], tmp_path / "unused.json")


def test_preflight_failure_happens_before_ground_truth_is_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, revision = _repo(tmp_path)
    wrong = dict(STREAMLAKE_ROUTING)
    wrong["order"] = ["not-streamlake"]
    artifact = _artifact(
        repo=repo,
        revision=revision,
        agent=scorer.DEFAULT_CONTROL_AGENT,
        pair_id="pair-1",
        provider=wrong,
    )
    path = _write_artifact(tmp_path / "bad-provider.json", artifact)
    opened = False

    def forbidden_read(_path: Path) -> tuple[bytes, int]:
        nonlocal opened
        opened = True
        raise AssertionError("ground truth must not be opened")

    monkeypatch.setattr(scorer, "_read_ground_truth_bytes", forbidden_read)

    with pytest.raises(ValueError, match="provider routing"):
        _score([path], tmp_path / "does-not-exist.json")
    assert opened is False


def test_root_and_run_streamlake_routing_must_be_exact(tmp_path: Path) -> None:
    repo, revision = _repo(tmp_path)
    artifact = _artifact(
        repo=repo,
        revision=revision,
        agent=scorer.DEFAULT_CONTROL_AGENT,
        pair_id="pair-1",
    )
    artifact["provider_routing"] = {
        **STREAMLAKE_ROUTING,
        "data_collection": "allow",
    }
    path = _write_artifact(tmp_path / "routing-mismatch.json", artifact)

    with pytest.raises(ValueError, match="provider routing"):
        _score([path], tmp_path / "unused.json")


def test_external_model_failure_invalidates_stage_before_ground_truth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, revision = _repo(tmp_path)
    path = _write_artifact(
        tmp_path / "external-failure.json",
        _artifact(
            repo=repo,
            revision=revision,
            agent=scorer.DEFAULT_CONTROL_AGENT,
            pair_id="pair-1",
            outcome="model:request_rejected",
        ),
    )
    opened = False

    def forbidden_read(_path: Path) -> tuple[bytes, int]:
        nonlocal opened
        opened = True
        raise AssertionError("ground truth must not be opened")

    monkeypatch.setattr(scorer, "_read_ground_truth_bytes", forbidden_read)

    with pytest.raises(ValueError, match="external generation failure"):
        _score([path], tmp_path / "does-not-exist.json")
    assert opened is False


def test_missing_required_graph_adoption_scores_but_makes_arm_ineligible(
    tmp_path: Path,
) -> None:
    repo, revision = _repo(tmp_path)
    control = _write_artifact(
        tmp_path / "control.json",
        _artifact(
            repo=repo,
            revision=revision,
            agent=scorer.DEFAULT_CONTROL_AGENT,
            pair_id="pair-1",
            total_seconds=10.0,
            actual_tokens=1_000,
        ),
    )
    graph_payload = _artifact(
        repo=repo,
        revision=revision,
        agent="codegraph_context_agent",
        pair_id="pair-1",
        optional_tool="graph_context",
        total_seconds=8.0,
        actual_tokens=900,
    )
    graph_call = graph_payload["runs"][0]["conversation"]["tool_trace"][1]
    graph_call["graph_exact_context"] = False
    graph_call["graph_result_kinds"] = ["no_match"]
    graph = _write_artifact(tmp_path / "graph.json", graph_payload)

    report = _score(
        [control, graph],
        _ground_truth(tmp_path / "ground-truth.json"),
    )
    arm = report["summary"]["arms"]["codegraph_context_agent"]

    assert arm["metrics"]["evidence_valid_hits"]["mean"] == 1.0
    assert arm["adoption"]["graph_context"]["required"] is True
    assert arm["adoption"]["graph_context"]["runs_passed"] == 0
    assert arm["reliability"]["eligible"] is False
    assert "codegraph_context_agent" not in report["summary"]["pareto_frontier"]
    assert report["protocol_audit"]["all_artifacts_completed_before_gt_read"] is True
    assert len(report["protocol_audit"]["artifact_manifest"]) == 2


def test_graph_adoption_rejects_target_absent_from_actual_prior_diff(tmp_path: Path) -> None:
    repo, revision = _repo(tmp_path)
    control = _write_artifact(
        tmp_path / "control.json",
        _artifact(
            repo=repo,
            revision=revision,
            agent=scorer.DEFAULT_CONTROL_AGENT,
            pair_id="pair-1",
        ),
    )
    graph_payload = _artifact(
        repo=repo,
        revision=revision,
        agent="codegraph_context_agent",
        pair_id="pair-1",
        optional_tool="graph_context",
    )
    graph_entry = graph_payload["runs"][0]["conversation"]["tool_trace"][1]
    graph_entry["arguments"] = {"targets": ["notInTheDiff"]}
    graph_entry["graph_result_targets"] = ["notInTheDiff"]
    graph = _write_artifact(tmp_path / "graph.json", graph_payload)

    report = _score([control, graph], _ground_truth(tmp_path / "ground-truth.json"))
    graph_run = next(row for row in report["runs"] if row["agent"] == "codegraph_context_agent")

    assert graph_run["adoption"]["graph_context"]["passed"] is False
    assert graph_run["adoption"]["graph_context"]["unsupported_diff_targets"] == ["notInTheDiff"]
    assert graph_run["reliability"]["eligible"] is False


def test_stage_rejects_a_treatment_with_a_different_base_tool_schema(tmp_path: Path) -> None:
    repo, revision = _repo(tmp_path)
    control_payload = _artifact(
        repo=repo,
        revision=revision,
        agent=scorer.DEFAULT_CONTROL_AGENT,
        pair_id="pair-1",
    )
    graph_payload = _artifact(
        repo=repo,
        revision=revision,
        agent="codegraph_context_agent",
        pair_id="pair-1",
        optional_tool="graph_context",
    )
    graph_payload["runs"][0]["tools"]["base_schema_sha256"] = "f" * 64
    control = _write_artifact(tmp_path / "control-schema.json", control_payload)
    graph = _write_artifact(tmp_path / "graph-schema.json", graph_payload)

    with pytest.raises(ValueError, match="base tool-definition hash mismatch"):
        _score([control, graph], tmp_path / "unused.json")


def test_agent_terminal_failure_is_retained_as_zero_and_ineligible(tmp_path: Path) -> None:
    repo, revision = _repo(tmp_path)
    control = _write_artifact(
        tmp_path / "control.json",
        _artifact(
            repo=repo,
            revision=revision,
            agent=scorer.DEFAULT_CONTROL_AGENT,
            pair_id="pair-1",
        ),
    )
    failed = _write_artifact(
        tmp_path / "failed.json",
        _artifact(
            repo=repo,
            revision=revision,
            agent="brief_diff_agent",
            pair_id="pair-1",
            optional_tool="audit_brief",
            outcome="submit_schema_invalid",
        ),
    )

    report = _score(
        [control, failed],
        _ground_truth(tmp_path / "ground-truth.json"),
    )
    arm = report["summary"]["arms"]["brief_diff_agent"]

    assert arm["metrics"]["evidence_valid_hits"]["mean"] == 0.0
    assert arm["agent_failures"] == 1
    assert arm["reliability"]["eligible"] is False


def test_manifest_rejects_omitted_artifact_before_ground_truth(tmp_path: Path) -> None:
    repo, revision = _repo(tmp_path)
    control = _write_artifact(
        tmp_path / "control.json",
        _artifact(
            repo=repo,
            revision=revision,
            agent=scorer.DEFAULT_CONTROL_AGENT,
            pair_id="pair-1",
        ),
    )
    graph = _write_artifact(
        tmp_path / "graph.json",
        _artifact(
            repo=repo,
            revision=revision,
            agent="codegraph_context_agent",
            pair_id="pair-1",
            optional_tool="graph_context",
        ),
    )
    manifest = _launch_manifest(tmp_path / "manifest.json", [control, graph])

    with pytest.raises(ValueError, match="missing from scorer input"):
        scorer.score_artifacts([control], tmp_path / "unused.json", manifest)


def test_manifest_rejects_failed_job_before_ground_truth(tmp_path: Path) -> None:
    repo, revision = _repo(tmp_path)
    control = _write_artifact(
        tmp_path / "control.json",
        _artifact(
            repo=repo,
            revision=revision,
            agent=scorer.DEFAULT_CONTROL_AGENT,
            pair_id="pair-1",
        ),
    )
    manifest = _launch_manifest(
        tmp_path / "manifest.json",
        [control],
        failed_agent=scorer.DEFAULT_CONTROL_AGENT,
    )

    with pytest.raises(ValueError, match="did not exit successfully"):
        scorer.score_artifacts([control], tmp_path / "unused.json", manifest)


def test_manifest_rejects_tampered_artifact_snapshot(tmp_path: Path) -> None:
    repo, revision = _repo(tmp_path)
    control = _write_artifact(
        tmp_path / "control.json",
        _artifact(
            repo=repo,
            revision=revision,
            agent=scorer.DEFAULT_CONTROL_AGENT,
            pair_id="pair-1",
        ),
    )
    manifest = _launch_manifest(tmp_path / "manifest.json", [control])
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["jobs"][0]["artifact_snapshot"]["sha256"] = "0" * 64
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="snapshot mismatch"):
        scorer.score_artifacts([control], tmp_path / "unused.json", manifest)


def test_manifest_rejects_incomplete_agents_by_pairs_matrix(tmp_path: Path) -> None:
    repo, revision = _repo(tmp_path)
    control = _write_artifact(
        tmp_path / "control.json",
        _artifact(
            repo=repo,
            revision=revision,
            agent=scorer.DEFAULT_CONTROL_AGENT,
            pair_id="pair-1",
        ),
    )
    manifest = _launch_manifest(tmp_path / "manifest.json", [control])
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["agents"].append("brief_diff_agent")
    payload["planned_agents"].append("brief_diff_agent")
    payload["job_count"] = 2
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="planned schedule is incomplete"):
        scorer.score_artifacts([control], tmp_path / "unused.json", manifest)


@pytest.mark.parametrize(
    "mapping",
    [
        {"doc_map_agent": "missing_agent"},
        {"doc_map_agent": scorer.DEFAULT_CONTROL_AGENT},
        {"doc_map_agent": "doc_map_agent"},
        {
            "doc_map_agent": "brief_diff_agent",
            "brief_diff_agent": "doc_map_agent",
        },
        {"doc_map_agent": 1},
    ],
)
def test_manifest_rejects_invalid_child_parent_registration_before_ground_truth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mapping: object,
) -> None:
    repo, revision = _repo(tmp_path)
    control = _write_artifact(
        tmp_path / "control.json",
        _artifact(
            repo=repo,
            revision=revision,
            agent=scorer.DEFAULT_CONTROL_AGENT,
            pair_id="pair-1",
        ),
    )
    parent = _write_artifact(
        tmp_path / "parent.json",
        _artifact(
            repo=repo,
            revision=revision,
            agent="brief_diff_agent",
            pair_id="pair-1",
            optional_tool="audit_brief",
        ),
    )
    child = _write_artifact(
        tmp_path / "child.json",
        _artifact(
            repo=repo,
            revision=revision,
            agent="doc_map_agent",
            pair_id="pair-1",
            optional_tool="audit_brief",
        ),
    )
    manifest = _launch_manifest(tmp_path / "manifest.json", [control, parent, child])
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["child_parent_map"] = mapping
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(
        scorer,
        "_read_ground_truth_bytes",
        lambda _path: (_ for _ in ()).throw(AssertionError("GT must remain closed")),
    )

    with pytest.raises(ValueError, match="child_parent_map"):
        scorer.score_artifacts(
            [control, parent, child],
            tmp_path / "unused.json",
            manifest,
        )


def test_manifest_rejects_pair_without_a_contemporaneous_time_window(
    tmp_path: Path,
) -> None:
    repo, revision = _repo(tmp_path)
    control = _write_artifact(
        tmp_path / "control.json",
        _artifact(
            repo=repo,
            revision=revision,
            agent=scorer.DEFAULT_CONTROL_AGENT,
            pair_id="pair-1",
        ),
    )
    brief = _write_artifact(
        tmp_path / "brief.json",
        _artifact(
            repo=repo,
            revision=revision,
            agent="brief_diff_agent",
            pair_id="pair-1",
            optional_tool="audit_brief",
        ),
    )
    manifest = _launch_manifest(tmp_path / "manifest.json", [control, brief])
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    first_job = payload["jobs"][0]
    first_job["child_exited_at_ns"] = first_job["popen_started_at_ns"] + 1
    process_seconds = (
        first_job["child_exited_at_ns"] - first_job["popen_started_at_ns"]
    ) / 1e9
    snapshot_overhead_seconds = (
        first_job["artifact_snapshot_at_ns"] - first_job["child_exited_at_ns"]
    ) / 1e9
    first_job["process_wall_seconds"] = round(process_seconds, 6)
    first_job["artifact_snapshot_overhead_seconds"] = round(
        snapshot_overhead_seconds,
        6,
    )
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="not contemporaneous"):
        scorer.score_artifacts([control, brief], tmp_path / "unused.json", manifest)


def test_pareto_time_comes_from_launch_snapshot_and_pair_deltas_are_reported(
    tmp_path: Path,
) -> None:
    repo, revision = _repo(tmp_path)
    control = _write_artifact(
        tmp_path / "control.json",
        _artifact(
            repo=repo,
            revision=revision,
            agent=scorer.DEFAULT_CONTROL_AGENT,
            pair_id="pair-1",
            total_seconds=100.0,
            actual_tokens=1_000,
        ),
    )
    brief = _write_artifact(
        tmp_path / "brief.json",
        _artifact(
            repo=repo,
            revision=revision,
            agent="brief_diff_agent",
            pair_id="pair-1",
            optional_tool="audit_brief",
            total_seconds=200.0,
            actual_tokens=900,
        ),
    )
    report = _score(
        [control, brief],
        _ground_truth(tmp_path / "ground-truth.json"),
        wall_seconds={scorer.DEFAULT_CONTROL_AGENT: 12.0, "brief_diff_agent": 7.0},
    )
    rows = {row["agent"]: row for row in report["runs"]}

    assert rows[scorer.DEFAULT_CONTROL_AGENT]["metrics"]["runner_total_seconds"] == 100.0
    assert rows[scorer.DEFAULT_CONTROL_AGENT]["metrics"]["total_seconds"] == 12.0
    assert rows["brief_diff_agent"]["metrics"]["runner_total_seconds"] == 200.0
    assert rows["brief_diff_agent"]["metrics"]["total_seconds"] == 7.0
    assert report["pair_deltas"] == [
        {
            "pair_id": "pair-1",
            "agent": "brief_diff_agent",
            "control_agent": scorer.DEFAULT_CONTROL_AGENT,
            "treatment": {
                "evidence_valid_hits": 1,
                "end_to_end_seconds": 7.0,
                "actual_tokens": 900,
            },
            "control": {
                "evidence_valid_hits": 1,
                "end_to_end_seconds": 12.0,
                "actual_tokens": 1_000,
            },
            "delta_treatment_minus_control": {
                "evidence_valid_hits": 0,
                "end_to_end_seconds": -5.0,
                "actual_tokens": -100,
            },
            "ratio_treatment_over_control": {
                "evidence_valid_hits": 1.0,
                "end_to_end_seconds": 0.583333,
                "actual_tokens": 0.9,
            },
        }
    ]
    assert report["pair_ratio_distributions"] == {
        "brief_diff_agent": {
            "actual_tokens": {
                "mean": 0.9,
                "stdev": 0.0,
                "min": 0.9,
                "max": 0.9,
                "defined_pairs": 1,
                "undefined_pairs": 0,
            },
            "end_to_end_seconds": {
                "mean": 0.583333,
                "stdev": 0.0,
                "min": 0.583333,
                "max": 0.583333,
                "defined_pairs": 1,
                "undefined_pairs": 0,
            },
            "evidence_valid_hits": {
                "mean": 1.0,
                "stdev": 0.0,
                "min": 1.0,
                "max": 1.0,
                "defined_pairs": 1,
                "undefined_pairs": 0,
            },
        }
    }
    assert report["protocol_audit"]["launch_manifest"]["pair_time_windows"][0][
        "all_jobs_overlap"
    ] is True
    assert report["protocol_audit"]["launch_manifest"]["child_parent_map_present"] is False
    stability = report["stability_vs_control"]["brief_diff_agent"]
    assert stability["status"] == "insufficient_repeats"
    assert stability["repeats"] == 1
    assert stability["thresholds"]["required_same_direction_pairs"] is None
    assert stability["stable_recall"] is None
    assert stability["stable_efficiency"] is None


def test_three_pairs_with_two_treatments_report_complete_s2_aggregates(
    tmp_path: Path,
) -> None:
    repo, revision = _repo(tmp_path)
    pair_ids = ("s2-1", "s2-2", "s2-3")
    agents = (
        scorer.DEFAULT_CONTROL_AGENT,
        "brief_diff_agent",
        "doc_map_agent",
    )
    hit_patterns = {
        scorer.DEFAULT_CONTROL_AGENT: (True, False, True),
        "brief_diff_agent": (True, True, False),
        "doc_map_agent": (False, True, True),
    }
    token_totals = {
        scorer.DEFAULT_CONTROL_AGENT: 1_000,
        "brief_diff_agent": 900,
        "doc_map_agent": 1_100,
    }
    optional_tools = {
        scorer.DEFAULT_CONTROL_AGENT: None,
        "brief_diff_agent": "audit_brief",
        "doc_map_agent": "audit_brief",
    }
    artifact_by_identity: dict[tuple[str, str], Path] = {}
    for pair_index, pair_id in enumerate(pair_ids):
        for agent in agents:
            artifact_by_identity[(pair_id, agent)] = _write_artifact(
                tmp_path / f"{pair_id}-{agent}.json",
                _artifact(
                    repo=repo,
                    revision=revision,
                    agent=agent,
                    pair_id=pair_id,
                    optional_tool=optional_tools[agent],
                    hits=hit_patterns[agent][pair_index],
                    actual_tokens=token_totals[agent],
                ),
            )

    schedule = (
        ("s2-1", scorer.DEFAULT_CONTROL_AGENT),
        ("s2-1", "brief_diff_agent"),
        ("s2-1", "doc_map_agent"),
        ("s2-2", "brief_diff_agent"),
        ("s2-2", "doc_map_agent"),
        ("s2-2", scorer.DEFAULT_CONTROL_AGENT),
        ("s2-3", "doc_map_agent"),
        ("s2-3", scorer.DEFAULT_CONTROL_AGENT),
        ("s2-3", "brief_diff_agent"),
    )
    artifacts = [artifact_by_identity[identity] for identity in schedule]
    report = _score(
        artifacts,
        _ground_truth(tmp_path / "ground-truth.json"),
        wall_seconds={
            scorer.DEFAULT_CONTROL_AGENT: 10.0,
            "brief_diff_agent": 8.0,
            "doc_map_agent": 12.0,
        },
    )

    arms = report["summary"]["arms"]
    assert {agent: arms[agent]["runs"] for agent in agents} == {
        scorer.DEFAULT_CONTROL_AGENT: 3,
        "brief_diff_agent": 3,
        "doc_map_agent": 3,
    }
    for agent in agents:
        assert arms[agent]["union"]["union_at_3"] == {
            "k": 3,
            "n": 3,
            "min": 1,
            "max": 1,
            "mean": 1.0,
        }

    assert len(report["pair_deltas"]) == 6
    assert {
        pair_id: sum(delta["pair_id"] == pair_id for delta in report["pair_deltas"])
        for pair_id in pair_ids
    } == {"s2-1": 2, "s2-2": 2, "s2-3": 2}
    zero_control_delta = next(
        delta
        for delta in report["pair_deltas"]
        if delta["pair_id"] == "s2-2" and delta["agent"] == "brief_diff_agent"
    )
    assert zero_control_delta["ratio_treatment_over_control"][
        "evidence_valid_hits"
    ] is None
    assert report["pair_ratio_distributions"]["brief_diff_agent"] == {
        "actual_tokens": {
            "mean": 0.9,
            "stdev": 0.0,
            "min": 0.9,
            "max": 0.9,
            "defined_pairs": 3,
            "undefined_pairs": 0,
        },
        "end_to_end_seconds": {
            "mean": 0.8,
            "stdev": 0.0,
            "min": 0.8,
            "max": 0.8,
            "defined_pairs": 3,
            "undefined_pairs": 0,
        },
        "evidence_valid_hits": {
            "mean": 0.5,
            "stdev": 0.5,
            "min": 0.0,
            "max": 1.0,
            "defined_pairs": 2,
            "undefined_pairs": 1,
        },
    }
    pair_windows = report["protocol_audit"]["launch_manifest"]["pair_time_windows"]
    assert len(pair_windows) == 3
    assert all(window["job_count"] == 3 for window in pair_windows)
    assert all(window["all_jobs_overlap"] is True for window in pair_windows)

    brief_stability = report["stability_vs_control"]["brief_diff_agent"]
    assert brief_stability["status"] == "evaluated"
    assert brief_stability["mean_delta_treatment_minus_comparator"] == {
        "evidence_valid_hits": 0.0,
        "end_to_end_seconds": -2.0,
        "actual_tokens": -100.0,
    }
    assert brief_stability["positive_hits_pairs"] == 1
    assert brief_stability["time_improved_pairs"] == 3
    assert brief_stability["token_improved_pairs"] == 3
    assert brief_stability["stable_recall"] is False
    assert brief_stability["stable_efficiency"] is True
    assert brief_stability["stable_efficiency_by_metric"] == {
        "end_to_end_seconds": True,
        "actual_tokens": True,
    }

    doc_stability = report["stability_vs_control"]["doc_map_agent"]
    assert doc_stability["stable_recall"] is False
    assert doc_stability["stable_efficiency"] is False


def test_three_pairs_report_stable_recall_and_registered_child_parent_delta(
    tmp_path: Path,
) -> None:
    repo, revision = _repo(tmp_path)
    parent = "brief_diff_agent"
    child = "doc_map_agent"
    agents = (scorer.DEFAULT_CONTROL_AGENT, parent, child)
    optional_tools = {
        scorer.DEFAULT_CONTROL_AGENT: None,
        parent: "audit_brief",
        child: "audit_brief",
    }
    artifacts: list[Path] = []
    for pair_id in ("confirm-1", "confirm-2", "confirm-3"):
        for agent in agents:
            artifacts.append(
                _write_artifact(
                    tmp_path / f"{pair_id}-{agent}.json",
                    _artifact(
                        repo=repo,
                        revision=revision,
                        agent=agent,
                        pair_id=pair_id,
                        optional_tool=optional_tools[agent],
                        hits=agent == child,
                        actual_tokens={
                            scorer.DEFAULT_CONTROL_AGENT: 1_200,
                            parent: 1_000,
                            child: 900,
                        }[agent],
                    ),
                )
            )

    report = _score(
        artifacts,
        _ground_truth(tmp_path / "ground-truth.json"),
        wall_seconds={
            scorer.DEFAULT_CONTROL_AGENT: 12.0,
            parent: 10.0,
            child: 8.0,
        },
        child_parent_map={child: parent},
    )

    child_control = report["stability_vs_control"][child]
    assert child_control["thresholds"]["required_same_direction_pairs"] == 2
    assert child_control["mean_delta_treatment_minus_comparator"] == {
        "evidence_valid_hits": 1.0,
        "end_to_end_seconds": -4.0,
        "actual_tokens": -300.0,
    }
    assert child_control["positive_hits_pairs"] == 3
    assert child_control["stable_recall"] is True
    assert child_control["stable_efficiency"] is True

    assert report["child_parent_map"] == {child: parent}
    assert report["protocol_audit"]["launch_manifest"]["child_parent_map_present"] is True
    assert len(report["child_parent_deltas"]) == 3
    assert all(
        delta["delta_child_minus_parent"]
        == {
            "evidence_valid_hits": 1,
            "end_to_end_seconds": -2.0,
            "actual_tokens": -100,
        }
        for delta in report["child_parent_deltas"]
    )
    child_parent = report["stability_vs_parent"][child]
    assert child_parent["comparator_agent"] == parent
    assert child_parent["mean_delta_treatment_minus_comparator"] == {
        "evidence_valid_hits": 1.0,
        "end_to_end_seconds": -2.0,
        "actual_tokens": -100.0,
    }
    assert child_parent["positive_hits_pairs"] == 3
    assert child_parent["time_improved_pairs"] == 3
    assert child_parent["token_improved_pairs"] == 3
    assert child_parent["stable_recall"] is True
    assert child_parent["stable_efficiency"] is True


def test_adoption_requires_early_success_and_exact_graph_context(tmp_path: Path) -> None:
    audit_run = {
        "tools": {"names": [*BASE_TOOLS, "audit_brief"]},
        "conversation": {
            "tool_trace": [
                {"turn": 1, "ordinal": 1, "name": "audit_brief", "error": False},
                {"turn": 2, "ordinal": 1, "name": "grep", "error": False},
            ]
        },
    }
    assert scorer._adoption_status(audit_run)["audit_brief"]["passed"] is True
    audit_run["conversation"]["tool_trace"].insert(
        0,
        {"turn": 1, "ordinal": 0, "name": "audit_brief", "error": True},
    )
    assert scorer._adoption_status(audit_run)["audit_brief"]["passed"] is False

    repo, revision = _repo(tmp_path)
    graph_run = _artifact(
        repo=repo,
        revision=revision,
        agent="codegraph_context_agent",
        pair_id="pair-1",
        optional_tool="graph_context",
    )["runs"][0]
    graph_run["conversation"]["tool_trace"].insert(
        2,
        {
            "turn": 3,
            "ordinal": 1,
            "name": "list_dir",
            "arguments": {"path": "."},
            "error": False,
        },
    )
    graph_entry = graph_run["conversation"]["tool_trace"][1]
    graph_entry.update(
        {
            "graph_result_kinds": ["no_match"],
            "graph_exact_context": False,
        }
    )
    assert scorer._adoption_status(graph_run)["graph_context"]["passed"] is False
    graph_entry.update(
        {
            "graph_result_kinds": ["graph_detail"],
            "graph_exact_context": True,
        }
    )
    status = scorer._adoption_status(graph_run, repo_path=repo)["graph_context"]
    assert status["passed"] is True
    assert status["diff_supported_calls_before_broad_scan"] == 1
    assert status["diff_replay_failures"] == 0

    graph_entry["arguments"] = {"targets": ["unchangedSymbol"]}
    graph_entry["graph_result_targets"] = ["unchangedSymbol"]
    unsupported = scorer._adoption_status(graph_run, repo_path=repo)["graph_context"]
    assert unsupported["passed"] is False
    assert unsupported["unsupported_diff_targets"] == ["unchangedSymbol"]

    graph_entry["arguments"] = {"targets": ["current"]}
    graph_entry["graph_result_targets"] = ["current"]
    frozen_diff = graph_run["setup"]["handler_calls"][0]
    output_sha256 = frozen_diff["output_sha256"]
    frozen_diff["output_sha256"] = "0" * 64
    tampered = scorer._adoption_status(graph_run, repo_path=repo)["graph_context"]
    assert tampered["passed"] is False
    assert tampered["diff_replay_failures"] == 1
    frozen_diff["output_sha256"] = output_sha256

    graph_entry["turn"] = 3
    assert scorer._adoption_status(graph_run, repo_path=repo)["graph_context"]["passed"] is False


def _native_adoption_run(
    repo: Path,
    revision: str,
    *,
    tool: str,
    query: str = "current callers",
) -> dict[str, Any]:
    baseline = _git(repo, "rev-parse", f"{revision}^")
    runtime = paged_generic_runtime(
        AgentContext(
            repo_path=repo,
            baseline_revision=baseline,
            head_revision=revision,
        )
    )
    diff_arguments: dict[str, Any] = {}
    diff_content = runtime.extra_tools["git_diff"][1](diff_arguments)
    diff_trace = {
        "turn": 1,
        "ordinal": 1,
        "name": "git_diff",
        "arguments": diff_arguments,
        "arguments_sha256": scorer._hash_json(diff_arguments),
        "seconds": 0.01,
        "error": False,
        **scorer._result_trace(diff_content),
    }

    if tool == "codegraph_explore":
        model_arguments = {"query": query}
        provider_arguments = {"query": query.strip()}
        provider = "codegraph"
        operation = "explore"
        output = (
            "## Source Code\n"
            "**`src/current.py`**\n"
            "1\tdef current():\n"
            "## Blast Radius\n"
            "current is called by caller\n"
        )
        provider_markers = {
            "contains_source_code": True,
            "contains_blast_radius": True,
            "contains_trim_notice": False,
        }
    elif tool == "gitnexus_change_impact":
        model_arguments = {}
        provider_arguments = {
            "scope": "compare",
            "base_ref": baseline,
            "limit": 500,
        }
        provider = "gitnexus"
        operation = "detect_changes"
        output = "Changed Symbols:\n- current\nAffected Execution Flows:\n- current flow\n"
        provider_markers = {
            "contains_changed_symbols": True,
            "contains_affected_execution_flows": True,
            "scope": "compare",
            "base_ref": baseline,
            "limit": 500,
        }
        runtime.metadata["fixed_provider_arguments"] = dict(provider_arguments)
    else:
        raise AssertionError(tool)

    output_sha = hashlib.sha256(output.encode("utf-8")).hexdigest()
    shared = {
        "provider": provider,
        "tool": tool,
        "operation": operation,
        "seconds": 0.01,
        "exit_code": 0,
        "output_chars": len(output),
        "output_sha256": output_sha,
        "error": False,
        **provider_markers,
    }
    runtime.metadata["query_calls"] = [
        {
            **shared,
            "arguments": dict(model_arguments),
            "provider_arguments": dict(provider_arguments),
        }
    ]
    runtime.metadata["provider_calls"] = [
        {**shared, "arguments": dict(provider_arguments)}
    ]
    native_trace = {
        "turn": 2,
        "ordinal": 1,
        "name": tool,
        "arguments": scorer._trace_arguments(tool, model_arguments),
        "arguments_sha256": scorer._hash_json(model_arguments),
        "seconds": 0.01,
        "error": False,
        **scorer._result_trace(output),
    }
    return {
        "baseline_revision": baseline,
        "head_revision": revision,
        "tools": {"names": [*BASE_TOOLS, tool]},
        "conversation": {
            "tool_trace": [
                diff_trace,
                native_trace,
                {
                    "turn": 3,
                    "ordinal": 1,
                    "name": "list_dir",
                    "arguments": {"path": "."},
                    "error": False,
                },
            ]
        },
        "setup": runtime.metadata,
    }


def _node_impact_adoption_run(repo: Path, revision: str) -> dict[str, Any]:
    run = _native_adoption_run(
        repo,
        revision,
        tool="codegraph_explore",
        query="current",
    )
    baseline = run["baseline_revision"]
    symbol = "current"
    file = "src/current.py"
    arguments = {"symbol": symbol, "file": file}
    normalized_arguments = dict(arguments)
    node_stdout = (
        "**current** (function)\n\n"
        "**Location:** src/current.py:1\n"
        "**Signature:** def current()\n\n"
        "```python\n"
        "1\tdef current():\n"
        "2\t    return True\n"
        "```\n\n"
        "**Trail — immediate relationships**\n"
        "**Called by ←**\n"
        "- caller\n"
    )
    impact_stdout = (
        '\nImpact of changing "current" — 1 affected symbols:\n\n'
        "src/current.py\n"
        "  function    current:1\n"
    )
    payload = {
        "protocol": scorer.CODEGRAPH_NODE_IMPACT_PROTOCOL,
        "query": normalized_arguments,
        "semantics": {
            "ordered_path_available": False,
            "ordered_path_notice": (
                "This composite returns exact source/trail plus upstream blast radius, "
                "not an ordered source-to-target call path."
            ),
            "node_file_disambiguated": True,
            "impact_depth": 3,
            "impact_direction": "upstream_dependents",
            "impact_file_disambiguated": False,
            "impact_definition_scope": "all_exact_same_named_definitions",
        },
        "results": {
            "node_include_source": {
                "stdout": node_stdout,
                "stderr": "",
                "exit_code": 0,
                "error": False,
                "provider_reported_truncation": False,
            },
            "upstream_impact_depth3": {
                "stdout": impact_stdout,
                "stderr": "",
                "exit_code": 0,
                "error": False,
                "provider_reported_truncation": False,
            },
        },
        "transport": {
            "complete_sanitized_stdout_stderr": True,
            "sanitization": "isolated_clone_path_only",
            "wrapper_truncation": False,
            "provider_truncation_notices_preserved": True,
        },
    }
    output = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    result_trace = scorer._result_trace(output)
    structured = result_trace["codegraph_node_impact_result"]

    def provider_record(
        *,
        operation: str,
        semantic_argv: list[str],
        semantic_arguments: dict[str, Any],
        stream: dict[str, Any],
        seconds: float,
    ) -> dict[str, Any]:
        return {
            "provider": "codegraph",
            "tool": "codegraph_node_impact",
            "operation": operation,
            "argv_semantic_args": semantic_argv,
            "semantic_arguments": semantic_arguments,
            "seconds": seconds,
            "exit_code": stream["exit_code"],
            "error": stream["error"],
            "package_version": scorer.CODEGRAPH_VERSION,
            "index_binding_sha256": "pending",
            "complete_sanitized_streams": True,
            "wrapper_truncation": False,
            "stdout_chars": stream["stdout_chars"],
            "stdout_sha256": stream["stdout_sha256"],
            "stderr_chars": stream["stderr_chars"],
            "stderr_sha256": stream["stderr_sha256"],
            "output_chars": stream["output_chars"],
            "output_sha256": stream["output_sha256"],
            "provider_reported_truncation": stream["provider_reported_truncation"],
            "composite_invocation": 1,
        }

    node = provider_record(
        operation="node_include_source",
        semantic_argv=["node", "--path", ".", "--file", file, symbol],
        semantic_arguments={
            "symbol": symbol,
            "include_source": True,
            "relationship_scope": "immediate_callers_and_callees",
            "file": file,
        },
        stream=structured["node_include_source"],
        seconds=0.02,
    )
    impact = provider_record(
        operation="impact_upstream_depth3",
        semantic_argv=["impact", "--path", ".", "--depth", "3", symbol],
        semantic_arguments={
            "symbol": symbol,
            "depth": 3,
            "direction": "upstream_dependents",
            "definition_scope": "all_exact_same_named_definitions",
            "file_disambiguation_applied": False,
        },
        stream=structured["upstream_impact_depth3"],
        seconds=0.03,
    )
    binary_sha256 = "a" * 64
    source_tree = _git(repo, "rev-parse", "HEAD^{tree}")
    index_binding = {
        "provider": "codegraph",
        "package_version": scorer.CODEGRAPH_VERSION,
        "binary_sha256": binary_sha256,
        "source_head": revision,
        "source_tree": source_tree,
        "isolated_clone_head_matches_source": True,
        "isolated_clone_tree_matches_source": True,
        "index_relative_path": ".codegraph",
        "status_initialized": True,
        "status_version": scorer.CODEGRAPH_VERSION,
        "index_state": "complete",
        "index_built_with_version": scorer.CODEGRAPH_VERSION,
        "pending_changes": {"added": 0, "modified": 0, "removed": 0},
        "pending_refs": 0,
        "worktree_mismatch": None,
    }
    index_binding_sha256 = scorer._hash_json(index_binding)
    node["index_binding_sha256"] = index_binding_sha256
    impact["index_binding_sha256"] = index_binding_sha256
    dependencies = [
        "git",
        "filesystem",
        f"codegraph:{scorer.CODEGRAPH_VERSION}:{binary_sha256}",
    ]
    diff = subprocess.run(
        [
            "git",
            "-c",
            "core.quotePath=false",
            "diff",
            "--no-ext-diff",
            "--no-renames",
            "--no-prefix",
            "--unified=0",
            baseline,
            revision,
            "--",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    query = {
        "provider": "codegraph",
        "tool": "codegraph_node_impact",
        "operation": "node_plus_upstream_impact_parallel",
        "arguments": arguments,
        "normalized_arguments": normalized_arguments,
        "matching_diff_paths": [file],
        "combined_seconds": 0.04,
        "provider_seconds_sum": 0.05,
        "parallel_overlap_seconds": 0.01,
        "provider_call_count": 2,
        "provider_call_operations": [
            "node_include_source",
            "impact_upstream_depth3",
        ],
        "execution_mode": "parallel_native_cli_subprocesses",
        "ordered_path_available": False,
        "output_chars": len(output),
        "output_sha256": hashlib.sha256(output.encode()).hexdigest(),
        "error": False,
        "provider_reported_truncation": False,
        "complete_sanitized_provider_outputs": True,
        "wrapper_truncation": False,
        "package_version": scorer.CODEGRAPH_VERSION,
        "index_binding_sha256": index_binding_sha256,
    }
    setup = run["setup"]
    setup.update(
        {
            "profile_id": scorer.CODEGRAPH_NODE_IMPACT_PROFILE,
            "provider": "codegraph",
            "isolated": True,
            "source_head": revision,
            "source_tree": source_tree,
            "agent_repo_clean": True,
            "agent_repo_graph_dirs_absent": True,
            "index_success": True,
            "package_version": scorer.CODEGRAPH_VERSION,
            "binary_sha256": binary_sha256,
            "installer_used": False,
            "mcp_used": False,
            "prompt_or_hook_injection": False,
            "telemetry_disabled": True,
            "update_checks_disabled": True,
            "package_integrity": scorer.CODEGRAPH_PACKAGE_INTEGRITY,
            "implementation_mode": "native-node-source-plus-impact-depth3-parallel",
            "candidate_protocol": scorer.CODEGRAPH_NODE_IMPACT_PROTOCOL,
            "impact_depth": 3,
            "ordered_path_available": False,
            "file_disambiguation": {
                "node": True,
                "impact": False,
                "impact_reason": "CodeGraph 1.5.0 CLI impact has no --file option",
            },
            "base_profile_id": "paged_generic",
            "dependencies": dependencies,
            "dependency_sha256": scorer._hash_json(dependencies),
            "tool_surface": ["codegraph_node_impact"],
            "output_transport": {
                "complete_provider_stdout_stderr": True,
                "pagination": False,
                "wrapper_truncation": False,
                "provider_internal_truncation_possible": True,
                "provider_truncation_notices_preserved": True,
                "projection": False,
                "sanitization": "isolated_clone_path_only",
            },
            "runtime_composition": {
                "isolated_clone_count": 1,
                "codegraph_index_count": 1,
                "generic_runtime_count": 1,
                "cleanup_callback_count": 1,
                "provider_queries_share_index": True,
            },
            "index_binding": index_binding,
            "index_binding_sha256": index_binding_sha256,
            "diff_symbol_guard": {
                "source": "baseline_to_head_changed_source_lines",
                "case_sensitive": True,
                "exact_single_identifier_only": True,
                "diff_sha256": hashlib.sha256(diff.encode()).hexdigest(),
                "eligible_symbol_count": 2,
                "changed_source_paths": [file],
                "ground_truth_used": False,
            },
            "setup_calls": [
                {
                    "operation": "init",
                    "seconds": 0.01,
                    "exit_code": 0,
                    "output_chars": 10,
                    "output_sha256": "b" * 64,
                    "error": False,
                },
                {
                    "operation": "status",
                    "seconds": 0.01,
                    "exit_code": 0,
                    "output_chars": 10,
                    "output_sha256": "c" * 64,
                    "error": False,
                },
            ],
            "query_calls": [query],
            "provider_calls": [node, impact],
        }
    )
    run["tools"]["names"][-1] = "codegraph_node_impact"
    native_trace = run["conversation"]["tool_trace"][1]
    native_trace.clear()
    native_trace.update(
        {
            "turn": 2,
            "ordinal": 1,
            "name": "codegraph_node_impact",
            "arguments": arguments,
            "arguments_sha256": scorer._hash_json(arguments),
            "seconds": 0.04,
            "error": False,
            **result_trace,
        }
    )
    return run


def test_codegraph_native_adoption_binds_diff_symbol_markers_and_ledgers(
    tmp_path: Path,
) -> None:
    repo, revision = _repo(tmp_path)
    run = _native_adoption_run(
        repo,
        revision,
        tool="codegraph_explore",
    )

    status = scorer._adoption_status(run, repo_path=repo)["codegraph_explore"]
    assert status["passed"] is True
    assert status["diff_supported_query_terms"] == ["current"]
    assert status["native_setup_bound_calls_before_broad_scan"] == 1

    keyword_only = _native_adoption_run(
        repo,
        revision,
        tool="codegraph_explore",
        query="def callers",
    )
    keyword_status = scorer._adoption_status(keyword_only, repo_path=repo)[
        "codegraph_explore"
    ]
    assert keyword_status["passed"] is False
    assert keyword_status["diff_supported_query_terms"] == []

    for marker in (
        "codegraph_source",
        "codegraph_blast_radius",
        "codegraph_line_numbered_source",
    ):
        tampered = copy.deepcopy(run)
        tampered["conversation"]["tool_trace"][1]["native_graph_markers"][marker] = False
        assert (
            scorer._adoption_status(tampered, repo_path=repo)["codegraph_explore"][
                "passed"
            ]
            is False
        )


def test_codegraph_node_impact_adoption_binds_both_complete_provider_streams(
    tmp_path: Path,
) -> None:
    repo, revision = _repo(tmp_path)
    run = _node_impact_adoption_run(repo, revision)

    status = scorer._adoption_status(run, repo_path=repo)["codegraph_node_impact"]
    assert status["passed"] is True
    assert status["successful_calls"] == 1
    assert status["native_valid_calls_before_broad_scan"] == 1
    assert status["native_setup_bound_calls_before_broad_scan"] == 1
    assert status["diff_supported_query_terms"] == ["current"]
    traced = run["conversation"]["tool_trace"][1]["codegraph_node_impact_result"]
    assert traced["node_source_marker"] is True
    assert traced["node_include_source"]["source_heading"] is True
    assert traced["node_include_source"]["source_location"] is True
    assert traced["node_include_source"]["line_numbered_source"] is True
    assert traced["impact_marker"] is True
    assert traced["upstream_impact_depth3"]["impact_heading_count"] == 1
    assert traced["upstream_impact_depth3"]["impact_item_count"] == 1


def test_codegraph_node_impact_preflight_requires_the_frozen_tool_schema(
    tmp_path: Path,
) -> None:
    repo, revision = _repo(tmp_path)
    run = _artifact(
        repo=repo,
        revision=revision,
        agent="codegraph_node_impact_agent",
        pair_id="pair-1",
        optional_tool="codegraph_node_impact",
        protocol_version=scorer.PROTOCOL_V3,
    )["runs"][0]
    definitions = run["tools"]["definitions"]
    definitions[-1] = copy.deepcopy(scorer.CODEGRAPH_NODE_IMPACT_DEFINITION)
    definitions[list(BASE_TOOLS).index("submit")]["function"]["parameters"][
        "required"
    ] = ["findings"]
    _rehash_tools(run)

    names, _schema, _base_names, _base_schema = scorer._validate_tool_metadata(
        run["tools"],
        agent="codegraph_node_impact_agent",
        protocol_version=scorer.PROTOCOL_V3,
        location="unit",
    )
    assert names == (*BASE_TOOLS, "codegraph_node_impact")

    definitions[-1]["function"]["description"] = "weakened"
    _rehash_tools(run)
    with pytest.raises(ValueError, match="frozen candidate"):
        scorer._validate_tool_metadata(
            run["tools"],
            agent="codegraph_node_impact_agent",
            protocol_version=scorer.PROTOCOL_V3,
            location="unit",
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "setup_version",
        "index_hash",
        "diff_hash",
        "provider_missing",
        "provider_stream_hash",
        "query_output_hash",
        "source_marker",
        "impact_marker",
        "native_marker",
        "provider_truncation",
        "duplicate_call",
        "diff_not_prior",
    ],
)
def test_codegraph_node_impact_adoption_fails_closed_on_tampering(
    tmp_path: Path,
    mutation: str,
) -> None:
    repo, revision = _repo(tmp_path)
    run = _node_impact_adoption_run(repo, revision)
    trace = run["conversation"]["tool_trace"]
    setup = run["setup"]
    if mutation == "setup_version":
        setup["package_version"] = "1.5.1"
    elif mutation == "index_hash":
        setup["index_binding_sha256"] = "0" * 64
    elif mutation == "diff_hash":
        setup["diff_symbol_guard"]["diff_sha256"] = "0" * 64
    elif mutation == "provider_missing":
        setup["provider_calls"].pop()
    elif mutation == "provider_stream_hash":
        setup["provider_calls"][0]["stdout_sha256"] = "0" * 64
    elif mutation == "query_output_hash":
        setup["query_calls"][0]["output_sha256"] = "0" * 64
    elif mutation == "source_marker":
        trace[1]["codegraph_node_impact_result"]["node_source_marker"] = False
    elif mutation == "impact_marker":
        trace[1]["codegraph_node_impact_result"]["impact_marker"] = False
    elif mutation == "native_marker":
        trace[1]["native_graph_markers"]["codegraph_upstream_impact"] = False
    elif mutation == "provider_truncation":
        setup["provider_calls"][1]["provider_reported_truncation"] = True
    elif mutation == "duplicate_call":
        duplicate = copy.deepcopy(trace[1])
        duplicate.update({"turn": 2, "ordinal": 2})
        trace.insert(2, duplicate)
    elif mutation == "diff_not_prior":
        trace[0]["turn"] = 3
    else:
        raise AssertionError(mutation)

    status = scorer._adoption_status(run, repo_path=repo)["codegraph_node_impact"]
    assert status["passed"] is False
    if mutation == "diff_not_prior":
        assert status["native_setup_bound_calls_before_broad_scan"] == 1
        assert status["native_valid_calls_before_broad_scan"] == 0


def test_codegraph_change_seed_adoption_requires_both_tools_before_broad_scan(
    tmp_path: Path,
) -> None:
    repo, revision = _repo(tmp_path)
    run = _native_adoption_run(
        repo,
        revision,
        tool="codegraph_explore",
    )
    run["tools"]["names"].append("audit_brief")
    for entry in run["conversation"]["tool_trace"]:
        entry["turn"] += 1
    run["conversation"]["tool_trace"].insert(
        0,
        {
            "turn": 1,
            "ordinal": 1,
            "name": "audit_brief",
            "arguments": {},
            "error": False,
        },
    )

    status = scorer._adoption_status(run, repo_path=repo)
    assert status["audit_brief"]["passed"] is True
    assert status["audit_brief"]["successful_calls"] == 1
    assert status["codegraph_explore"]["passed"] is True
    assert status["codegraph_explore"]["native_valid_calls_before_broad_scan"] == 1

    duplicate = copy.deepcopy(run)
    duplicate["conversation"]["tool_trace"].insert(
        1,
        {
            "turn": 1,
            "ordinal": 2,
            "name": "audit_brief",
            "arguments": {},
            "error": False,
        },
    )
    duplicate_status = scorer._adoption_status(duplicate, repo_path=repo)
    assert duplicate_status["audit_brief"]["passed"] is False
    assert duplicate_status["codegraph_explore"]["passed"] is True

    missing = copy.deepcopy(run)
    missing["conversation"]["tool_trace"].pop(0)
    missing_status = scorer._adoption_status(missing, repo_path=repo)
    assert missing_status["audit_brief"]["passed"] is False
    assert missing_status["codegraph_explore"]["passed"] is True


def test_codegraph_change_seed_child_parent_delta_is_schema_bound() -> None:
    child = "codegraph_explore_change_seed_agent"
    parent = "codegraph_explore_direct_agent"
    base_definitions = [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": f"Synthetic {name}",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in BASE_TOOLS
    ]
    parent_definitions = [
        *base_definitions,
        copy.deepcopy(scorer.CODEGRAPH_EXPLORE_DIRECT_DEFINITION),
    ]
    child_definitions = [
        *copy.deepcopy(parent_definitions),
        copy.deepcopy(scorer.AUDIT_BRIEF_DEFINITION),
    ]
    runs = {
        Path("parent.json"): {
            "agent": parent,
            "protocol_version": scorer.PROTOCOL_V3,
            "tools": {
                "names": [*BASE_TOOLS, "codegraph_explore"],
                "definitions": parent_definitions,
            },
        },
        Path("child.json"): {
            "agent": child,
            "protocol_version": scorer.PROTOCOL_V3,
            "tools": {
                "names": [*BASE_TOOLS, "codegraph_explore", "audit_brief"],
                "definitions": child_definitions,
            },
        },
    }

    assert scorer._validate_registered_forward_tool_deltas(
        {child: parent},
        runs,
    ) == [
        {
            "child": child,
            "parent": parent,
            "incremental_tools": ["audit_brief"],
            "shared_tool_definitions": [*BASE_TOOLS, "codegraph_explore"],
            "protocol_version": scorer.PROTOCOL_V3,
        }
    ]

    changed_parent_schema = copy.deepcopy(runs)
    changed_parent_schema[Path("child.json")]["tools"]["definitions"][-2]["function"][
        "description"
    ] = "retargeted"
    with pytest.raises(ValueError, match="changes a parent tool definition"):
        scorer._validate_registered_forward_tool_deltas(
            {child: parent},
            changed_parent_schema,
        )

    changed_audit_schema = copy.deepcopy(runs)
    changed_audit_schema[Path("child.json")]["tools"]["definitions"][-1]["function"][
        "description"
    ] = "retargeted"
    with pytest.raises(ValueError, match="unexpected audit_brief schema"):
        scorer._validate_registered_forward_tool_deltas(
            {child: parent},
            changed_audit_schema,
        )

    with pytest.raises(ValueError, match="must register"):
        scorer._validate_child_parent_map(
            {},
            agents=[scorer.NATIVE_CONTROL_AGENT, parent, child],
            control_agent=scorer.NATIVE_CONTROL_AGENT,
            present=True,
        )
    with pytest.raises(ValueError, match="must register"):
        scorer._validate_child_parent_map(
            None,
            agents=[scorer.NATIVE_CONTROL_AGENT, parent, child],
            control_agent=scorer.NATIVE_CONTROL_AGENT,
            present=False,
        )


@pytest.mark.parametrize(
    "tamper",
    [
        "trace_arguments_sha",
        "query_arguments",
        "query_output_sha",
        "provider_output_sha",
        "provider_arguments",
        "provider_query_retarget",
    ],
)
def test_codegraph_native_adoption_fails_closed_on_ledger_tampering(
    tmp_path: Path,
    tamper: str,
) -> None:
    repo, revision = _repo(tmp_path)
    run = _native_adoption_run(
        repo,
        revision,
        tool="codegraph_explore",
    )
    if tamper == "trace_arguments_sha":
        run["conversation"]["tool_trace"][1]["arguments_sha256"] = "0" * 64
    elif tamper == "query_arguments":
        run["setup"]["query_calls"][0]["arguments"] = {"query": "previous callers"}
    elif tamper == "query_output_sha":
        run["setup"]["query_calls"][0]["output_sha256"] = "0" * 64
    elif tamper == "provider_output_sha":
        run["setup"]["provider_calls"][0]["output_sha256"] = "0" * 64
    elif tamper == "provider_arguments":
        run["setup"]["provider_calls"][0]["arguments"] = {"query": "other"}
    elif tamper == "provider_query_retarget":
        run["setup"]["query_calls"][0]["provider_arguments"] = {"query": "other"}
        run["setup"]["provider_calls"][0]["arguments"] = {"query": "other"}
    else:
        raise AssertionError(tamper)

    status = scorer._adoption_status(run, repo_path=repo)["codegraph_explore"]
    assert status["passed"] is False
    assert status["native_setup_bound_calls_before_broad_scan"] == 0


def test_native_ledger_binding_supports_traced_long_arguments(tmp_path: Path) -> None:
    repo, revision = _repo(tmp_path)
    query = f"current {'caller ' * 100}"
    run = _native_adoption_run(
        repo,
        revision,
        tool="codegraph_explore",
        query=query,
    )

    trace_arguments = run["conversation"]["tool_trace"][1]["arguments"]
    assert "sha256:" in trace_arguments["query"]
    assert scorer._adoption_status(run, repo_path=repo)["codegraph_explore"]["passed"] is True


def test_gitnexus_native_adoption_binds_the_fixed_compare_call(tmp_path: Path) -> None:
    repo, revision = _repo(tmp_path)
    run = _native_adoption_run(
        repo,
        revision,
        tool="gitnexus_change_impact",
    )

    status = scorer._adoption_status(run, repo_path=repo)["gitnexus_change_impact"]
    assert status["passed"] is True
    assert status["native_setup_bound_calls_before_broad_scan"] == 1

    mutations = []
    model_arguments = copy.deepcopy(run)
    model_arguments["conversation"]["tool_trace"][1]["arguments"] = {
        "base_ref": "model-controlled"
    }
    model_arguments["conversation"]["tool_trace"][1]["arguments_sha256"] = scorer._hash_json(
        {"base_ref": "model-controlled"}
    )
    model_arguments["setup"]["query_calls"][0]["arguments"] = {
        "base_ref": "model-controlled"
    }
    mutations.append(model_arguments)

    wrong_baseline = copy.deepcopy(run)
    wrong = {"scope": "compare", "base_ref": "wrong", "limit": 500}
    wrong_baseline["setup"]["query_calls"][0]["provider_arguments"] = wrong
    wrong_baseline["setup"]["provider_calls"][0]["arguments"] = wrong
    mutations.append(wrong_baseline)

    wrong_setup = copy.deepcopy(run)
    wrong_setup["setup"]["fixed_provider_arguments"] = {
        "scope": "compare",
        "base_ref": "wrong",
        "limit": 500,
    }
    mutations.append(wrong_setup)

    unbound_output = copy.deepcopy(run)
    unbound_output["setup"]["provider_calls"][0]["output_sha256"] = "0" * 64
    mutations.append(unbound_output)

    wrong_operation = copy.deepcopy(run)
    wrong_operation["setup"]["query_calls"][0]["operation"] = "query"
    wrong_operation["setup"]["provider_calls"][0]["operation"] = "query"
    mutations.append(wrong_operation)

    before_diff = copy.deepcopy(run)
    before_diff["conversation"]["tool_trace"][0]["turn"] = 2
    before_diff["conversation"]["tool_trace"][1]["turn"] = 1
    mutations.append(before_diff)

    missing_trace_marker = copy.deepcopy(run)
    missing_trace_marker["conversation"]["tool_trace"][1]["native_graph_markers"][
        "gitnexus_affected_flows"
    ] = False
    mutations.append(missing_trace_marker)

    missing_provider_marker = copy.deepcopy(run)
    missing_provider_marker["setup"]["query_calls"][0][
        "contains_changed_symbols"
    ] = False
    mutations.append(missing_provider_marker)

    for tampered in mutations:
        assert (
            scorer._adoption_status(tampered, repo_path=repo)[
                "gitnexus_change_impact"
            ]["passed"]
            is False
        )


def _gitnexus_first_validation_run(repo: Path, revision: str) -> dict[str, Any]:
    run = _native_adoption_run(
        repo,
        revision,
        tool="gitnexus_change_impact",
    )
    run["protocol_version"] = scorer.PROTOCOL_V4
    run["setup"].update(
        {
            "retrieval_profile": "gitnexus-compact-cli-detect-changes",
            "provider_surface": "gitnexus-cli",
            "output_contract": (
                "complete-sanitized-native-cli-response; the CLI formatter may display "
                "only a subset of changed symbols or affected flows"
            ),
        }
    )
    native_trace = run["conversation"]["tool_trace"][1]
    native_trace["turn"] = 1
    run["conversation"]["tool_trace"] = [native_trace]
    forced_choice = {
        "type": "function",
        "function": {"name": "gitnexus_change_impact"},
    }
    run["conversation"].update(
        {
            "turn_trace": [
                {
                    "turn": 1,
                    "tool_calls": ["gitnexus_change_impact"],
                    "tool_choice": forced_choice,
                    "forced_tool": "gitnexus_change_impact",
                },
                {
                    "turn": 2,
                    "tool_calls": [],
                    "tool_choice": "auto",
                    "forced_tool": None,
                },
            ],
            "model_call_trace": [
                {
                    "conversation_turn": 1,
                    "request_id": "request-1",
                    "tool_choice": forced_choice,
                    "forced_tool": "gitnexus_change_impact",
                },
                {
                    "conversation_turn": 2,
                    "request_id": "request-2",
                    "tool_choice": "auto",
                    "forced_tool": None,
                },
            ],
            "transport_attempt_trace": [
                {"tool_choice": forced_choice},
                {"tool_choice": "auto"},
            ],
            "manipulation_trace": [
                {
                    "event": "forced_tool_request_started",
                    "conversation_turn": 1,
                    "tool": "gitnexus_change_impact",
                    "trigger": "initial_request",
                },
                {
                    "event": "forced_tool_request_completed",
                    "conversation_turn": 1,
                    "tool": "gitnexus_change_impact",
                    "request_id": "request-1",
                    "observed_tool_calls": ["gitnexus_change_impact"],
                    "auto_restored_for_next_request": True,
                },
            ],
        }
    )
    return run


def test_gitnexus_first_validation_and_adoption_require_one_initial_bound_call(
    tmp_path: Path,
) -> None:
    repo, revision = _repo(tmp_path)
    run = _gitnexus_first_validation_run(repo, revision)

    scorer._validate_v4_initial_manipulation(run, location="unit")
    status = scorer._adoption_status(run, repo_path=repo)["gitnexus_change_impact"]
    assert status["passed"] is True
    assert status["total_calls"] == 1
    assert status["rule"].startswith("exactly_1_initial")

    duplicate = copy.deepcopy(run)
    duplicate_call = copy.deepcopy(duplicate["conversation"]["tool_trace"][0])
    duplicate_call.update({"turn": 2, "ordinal": 1})
    duplicate["conversation"]["tool_trace"].append(duplicate_call)
    duplicate["conversation"]["turn_trace"][1]["tool_calls"] = [
        "gitnexus_change_impact"
    ]
    duplicate["setup"]["query_calls"].append(
        copy.deepcopy(duplicate["setup"]["query_calls"][0])
    )
    duplicate["setup"]["provider_calls"].append(
        copy.deepcopy(duplicate["setup"]["provider_calls"][0])
    )
    with pytest.raises(ValueError, match="exactly once"):
        scorer._validate_v4_initial_manipulation(duplicate, location="unit")
    assert (
        scorer._adoption_status(duplicate, repo_path=repo)["gitnexus_change_impact"][
            "passed"
        ]
        is False
    )

    for mutation in ("arguments", "provider", "markers", "auto"):
        tampered = copy.deepcopy(run)
        if mutation == "arguments":
            arguments = {"base_ref": "model-controlled"}
            entry = tampered["conversation"]["tool_trace"][0]
            entry["arguments"] = arguments
            entry["arguments_sha256"] = scorer._hash_json(arguments)
            tampered["setup"]["query_calls"][0]["arguments"] = arguments
        elif mutation == "provider":
            tampered["setup"]["provider_calls"][0]["output_sha256"] = "0" * 64
        elif mutation == "markers":
            tampered["conversation"]["tool_trace"][0]["native_graph_markers"][
                "gitnexus_changed_symbols"
            ] = False
        elif mutation == "auto":
            forced = tampered["conversation"]["turn_trace"][0]["tool_choice"]
            tampered["conversation"]["turn_trace"][1]["tool_choice"] = forced
            tampered["conversation"]["model_call_trace"][1]["tool_choice"] = forced
            tampered["conversation"]["transport_attempt_trace"][1][
                "tool_choice"
            ] = forced
        with pytest.raises(ValueError):
            scorer._validate_v4_initial_manipulation(tampered, location="unit")


def _structured_first_validation_run(repo: Path, revision: str) -> dict[str, Any]:
    baseline = _git(repo, "rev-parse", f"{revision}^")
    output = json.dumps(
        {
            "summary": {"changed_count": 2, "affected_count": 1},
            "changed_symbols": [{"name": "one"}, {"name": "two"}],
            "affected_processes": [{"name": "flow"}],
            "partial": False,
        },
        indent=2,
    )
    output_sha = hashlib.sha256(output.encode()).hexdigest()
    metrics = {
        "structured_json": True,
        "provider_error": False,
        "partial": False,
        "partial_field_present": True,
        "partial_value_valid": True,
        "changed_symbols_count": 2,
        "affected_processes_count": 1,
        "summary_changed_count": 2,
        "summary_affected_count": 1,
        "summary_counts_match_arrays": True,
    }
    shared = {
        "provider": "gitnexus",
        "tool": "gitnexus_structured_change",
        "operation": "detect_changes",
        "seconds": 0.01,
        "exit_code": 0,
        "output_chars": len(output),
        "output_sha256": output_sha,
        "error": False,
        **metrics,
    }
    fixed = {"scope": "compare", "base_ref": baseline}
    forced_choice = {
        "type": "function",
        "function": {"name": "gitnexus_structured_change"},
    }
    trace = {
        "turn": 1,
        "ordinal": 1,
        "name": "gitnexus_structured_change",
        "arguments": {},
        "arguments_sha256": scorer._hash_json({}),
        "seconds": 0.01,
        "error": False,
        **scorer._result_trace(output),
    }
    return {
        "protocol_version": scorer.PROTOCOL_V5,
        "baseline_revision": baseline,
        "head_revision": revision,
        "tools": {"names": [*BASE_TOOLS, "gitnexus_structured_change"]},
        "conversation": {
            "turn_trace": [
                {
                    "turn": 1,
                    "tool_calls": ["gitnexus_structured_change"],
                    "tool_choice": forced_choice,
                    "forced_tool": "gitnexus_structured_change",
                },
                {
                    "turn": 2,
                    "tool_calls": [],
                    "tool_choice": "auto",
                    "forced_tool": None,
                },
            ],
            "tool_trace": [trace],
            "model_call_trace": [
                {
                    "conversation_turn": 1,
                    "request_id": "request-1",
                    "tool_choice": forced_choice,
                    "forced_tool": "gitnexus_structured_change",
                },
                {
                    "conversation_turn": 2,
                    "request_id": "request-2",
                    "tool_choice": "auto",
                    "forced_tool": None,
                },
            ],
            "transport_attempt_trace": [
                {"tool_choice": forced_choice},
                {"tool_choice": "auto"},
            ],
            "manipulation_trace": [
                {
                    "event": "forced_tool_request_started",
                    "conversation_turn": 1,
                    "tool": "gitnexus_structured_change",
                    "trigger": "initial_request",
                },
                {
                    "event": "forced_tool_request_completed",
                    "conversation_turn": 1,
                    "tool": "gitnexus_structured_change",
                    "request_id": "request-1",
                    "observed_tool_calls": ["gitnexus_structured_change"],
                    "auto_restored_for_next_request": True,
                },
            ],
        },
        "setup": {
            "provider": "gitnexus",
            "profile_id": "gitnexus_official_structured_change",
            "package_version": "1.6.9",
            "binary_sha256": "a" * 64,
            "backend_module_sha256": "b" * 64,
            "bridge_sha256": "c" * 64,
            "implementation_mode": "official-local-backend-structured-detect-changes",
            "official_backend_export": "LocalBackend",
            "backend_module": "dist/mcp/local/local-backend.js",
            "fixed_provider_arguments": fixed,
            "runtime_bindings": {"repo": "isolated_index_clone"},
            "model_controlled_provider_arguments": [],
            "provider_limit": None,
            "cli_formatter_used": False,
            "source_head": revision,
            "index_stats": {"lastCommit": revision},
            "output_transport": {
                "complete_provider_output": True,
                "structured_json": True,
                "wrapper_truncation": False,
                "projection": False,
                "sanitization": "isolated_clone_path_only",
            },
            "setup_calls": [
                {"operation": "analyze", "exit_code": 0, "error": False}
            ],
            "query_calls": [
                {
                    **shared,
                    "arguments": {},
                    "provider_arguments": fixed,
                    "runtime_bindings": {"repo": "isolated_index_clone"},
                }
            ],
            "provider_calls": [
                {
                    **shared,
                    "arguments": fixed,
                    "runtime_bindings": {"repo": "isolated_index_clone"},
                }
            ],
        },
    }


def test_structured_first_validation_binds_complete_nonpartial_result(
    tmp_path: Path,
) -> None:
    repo, revision = _repo(tmp_path)
    run = _structured_first_validation_run(repo, revision)

    scorer._validate_v5_structured_initial_manipulation(run, location="unit")
    status = scorer._adoption_status(run, repo_path=repo)[
        "gitnexus_structured_change"
    ]
    assert status["passed"] is True
    assert status["total_calls"] == 1
    assert status["native_setup_bound_calls_before_broad_scan"] == 1

    for mutation in (
        "partial",
        "partial_invalid",
        "count",
        "baseline",
        "auto",
        "profile",
    ):
        tampered = copy.deepcopy(run)
        if mutation == "partial":
            for ledger in (
                tampered["setup"]["query_calls"][0],
                tampered["setup"]["provider_calls"][0],
                tampered["conversation"]["tool_trace"][0][
                    "gitnexus_structured_result"
                ],
            ):
                ledger["partial"] = True
        elif mutation == "partial_invalid":
            for ledger in (
                tampered["setup"]["query_calls"][0],
                tampered["setup"]["provider_calls"][0],
                tampered["conversation"]["tool_trace"][0][
                    "gitnexus_structured_result"
                ],
            ):
                ledger["partial_value_valid"] = False
        elif mutation == "count":
            for ledger in (
                tampered["setup"]["query_calls"][0],
                tampered["setup"]["provider_calls"][0],
                tampered["conversation"]["tool_trace"][0][
                    "gitnexus_structured_result"
                ],
            ):
                ledger["summary_counts_match_arrays"] = False
        elif mutation == "baseline":
            tampered["setup"]["fixed_provider_arguments"] = {
                "scope": "compare",
                "base_ref": "wrong",
            }
        elif mutation == "auto":
            forced = tampered["conversation"]["turn_trace"][0]["tool_choice"]
            tampered["conversation"]["turn_trace"][1]["tool_choice"] = forced
            tampered["conversation"]["model_call_trace"][1]["tool_choice"] = forced
            tampered["conversation"]["transport_attempt_trace"][1][
                "tool_choice"
            ] = forced
        elif mutation == "profile":
            tampered["setup"]["profile_id"] = "compact-cli"
        with pytest.raises(ValueError):
            scorer._validate_v5_structured_initial_manipulation(
                tampered,
                location="unit",
            )

    duplicate = copy.deepcopy(run)
    extra = copy.deepcopy(duplicate["conversation"]["tool_trace"][0])
    extra.update({"turn": 2, "ordinal": 1})
    duplicate["conversation"]["tool_trace"].append(extra)
    with pytest.raises(ValueError, match="exactly once"):
        scorer._validate_v5_structured_initial_manipulation(
            duplicate,
            location="unit",
        )
    assert (
        scorer._adoption_status(duplicate, repo_path=repo)[
            "gitnexus_structured_change"
        ]["passed"]
        is False
    )


def _focused_exact_adoption_run(repo: Path, revision: str) -> dict[str, Any]:
    baseline = _git(repo, "rev-parse", f"{revision}^")
    selected_uid = "Function:src/current.py:current"
    selected_name = "current"
    ordering = [
        "cross_community_processes_desc",
        "total_processes_desc",
        "changed_step_occurrences_desc",
        "kind_priority_asc",
        "filePath_asc",
        "uid_asc",
    ]
    score = {
        "cross_community_processes": 11,
        "total_processes": 11,
        "changed_step_occurrences": 11,
        "kind_priority": 0,
    }
    changed = [
        {"id": selected_uid, "name": selected_name, "filePath": "src/current.py"},
        *[
            {
                "id": f"Function:src/generated_{index}.py:symbol_{index}",
                "name": f"symbol_{index}",
                "filePath": f"src/generated_{index}.py",
            }
            for index in range(1, 139)
        ],
    ]
    affected = [
        {
            "id": f"process-{index}",
            "name": f"Current flow {index}",
            "process_type": "cross_community",
            "step_count": 2,
            "changed_steps": [{"symbol": selected_name, "step": 1}],
        }
        for index in range(1, 12)
    ]
    detect = {
        "summary": {
            "changed_count": 139,
            "affected_count": 11,
            "changed_files": 139,
            "risk_level": "high",
        },
        "changed_symbols": changed,
        "affected_processes": affected,
    }
    context_arguments = {
        "include_content": False,
        "name": selected_name,
        "uid": selected_uid,
    }
    impact_arguments = {
        "direction": "upstream",
        "mode": "callgraph",
        "maxDepth": 2,
        "includeTests": False,
        "limit": 8,
        "offset": 0,
        "summaryOnly": False,
        "target": selected_name,
        "target_uid": selected_uid,
    }
    trace_arguments = {
        "from": "entrypoint",
        "from_uid": "Function:src/entry.py:entrypoint",
        "to": selected_name,
        "to_uid": selected_uid,
        "maxDepth": 3,
        "includeTests": False,
    }
    context_result = {
        "status": "found",
        "symbol": {"uid": selected_uid, "name": selected_name},
        "incoming": {"calls": []},
        "outgoing": {"calls": []},
        "processes": [],
        "typed_properties": [],
    }
    impact_result = {
        "target": {"id": selected_uid, "name": selected_name},
        "byDepth": {
            "1": [{"id": "Function:src/mid.py:middle", "name": "middle"}],
            "2": [{"id": trace_arguments["from_uid"], "name": "entrypoint"}],
        },
    }
    trace_result = {"status": "ok", "hopCount": 2}
    process_content = "trace: entrypoint -> middle -> current"
    enrichment = {
        "context": {
            "performed": True,
            "arguments": context_arguments,
            "result": context_result,
        },
        "impact": {
            "performed": True,
            "arguments": impact_arguments,
            "result": impact_result,
        },
        "trace": {
            "performed": True,
            "reason": "single_contiguous_unpaginated_upstream_chain",
            "arguments": trace_arguments,
            "result": trace_result,
        },
        "process": {
            "performed": True,
            "reason": "highest_ranked_cross_community_process_for_selected_symbol",
            "selected_process": affected[0],
            "resource": "gitnexus://repo/{runtime_repo}/process/{selected_process}",
            "content": process_content,
        },
    }
    selection = {
        "policy_version": "k1-cross-community-unique-exact-uid-v1",
        "max_selected": 1,
        "integrity": {
            "clean": True,
            "error": False,
            "partial": False,
            "partial_field_present": False,
            "partial_value_valid": True,
            "changed_symbols_count": 139,
            "affected_processes_count": 11,
            "summary_counts_match_arrays": True,
        },
        "eligible_count": 1,
        "rejection_counts": {},
        "status": "selected",
        "reason": "highest_ranked_eligible_exact_uid",
        "ordering": ordering,
        "selected": {
            "uid": selected_uid,
            "name": selected_name,
            "kind": "Function",
            "filePath": "src/current.py",
            "score": score,
        },
    }
    raw_payload = {
        "protocol_version": "gitnexus-official-structured-k1-exact-composite-v1",
        "normalization": "recursive_object_key_sort_arrays_preserved",
        "detect_changes": detect,
        "selection": selection,
        "enrichment": enrichment,
    }

    def provider_call(
        index: int,
        operation: str,
        arguments: dict[str, Any],
        result: object,
    ) -> dict[str, Any]:
        rendered = result if isinstance(result, str) else focused_exact._json(result)
        result_object = result if isinstance(result, dict) else {}
        return {
            "call_index": index,
            "operation": operation,
            "arguments": arguments,
            "runtime_bindings": {"repo": "isolated_index_clone"},
            "seconds": 0.01 * index,
            "output_chars": len(rendered),
            "output_sha256": hashlib.sha256(rendered.encode()).hexdigest(),
            "bridge_exception": False,
            "error": False,
            "partial": False,
            "partial_field_present": "partial" in result_object,
            "partial_value_valid": True,
            "pagination_field_present": False,
            "pagination": None,
            "status": result_object.get("status"),
            "ambiguity_candidates": 0,
        }

    raw_provider_calls = [
        provider_call(
            1,
            "detect_changes",
            {"base_ref": baseline, "scope": "compare"},
            detect,
        ),
        provider_call(2, "context", context_arguments, context_result),
        provider_call(3, "impact", impact_arguments, impact_result),
        provider_call(4, "trace", trace_arguments, trace_result),
        provider_call(
            5,
            "process_resource",
            {"process_name": affected[0]["name"]},
            process_content,
        ),
    ]
    provider_calls = [
        {**call, "composite_invocation": 1} for call in raw_provider_calls
    ]
    focused_payload, audit = focused_exact._render_focused_payload(
        raw_payload,
        detect_provider_call=provider_calls[0],
    )
    raw_output = focused_exact._json(raw_payload)
    focused_output = focused_exact._json(focused_payload)
    raw_sha = hashlib.sha256(raw_output.encode()).hexdigest()
    focused_sha = hashlib.sha256(focused_output.encode()).hexdigest()
    audit.update(
        {
            "raw_composite_model_view_chars": len(raw_output),
            "raw_composite_model_view_sha256": raw_sha,
            "focused_model_view_chars": len(focused_output),
            "focused_model_view_sha256": focused_sha,
        }
    )
    bridge_output = focused_exact._json(
        {**raw_payload, "provider_calls": raw_provider_calls}
    )
    query = {
        "provider": "gitnexus",
        "tool": "gitnexus_exact_composite",
        "operation": "k1_exact_composite",
        "seconds": 0.1,
        "exit_code": 0,
        "bridge_output_chars": len(bridge_output),
        "bridge_output_sha256": hashlib.sha256(bridge_output.encode()).hexdigest(),
        "error": False,
        "arguments": {},
        "provider_arguments": {"scope": "compare", "base_ref": baseline},
        "runtime_bindings": {"repo": "isolated_index_clone"},
        "structured_json": True,
        "provider_call_count": 5,
        "provider_calls_sha256": scorer._hash_json(raw_provider_calls),
        "detect_metrics": {
            "structured_json": True,
            "provider_error": False,
            "partial": False,
            "partial_field_present": False,
            "partial_value_valid": True,
            "changed_symbols_count": 139,
            "affected_processes_count": 11,
            "summary_changed_count": 139,
            "summary_affected_count": 11,
            "summary_counts_match_arrays": True,
        },
        "selection_status": "selected",
        "selector_policy_version": "k1-cross-community-unique-exact-uid-v1",
        "selected_uid": selected_uid,
        "selected_name": selected_name,
        "selected_score": score,
        "eligible_count": 1,
        "rejection_counts": {},
        "enrichment_performed": {
            "context": True,
            "impact": True,
            "trace": True,
            "process": True,
        },
        "render_profile": "focused-exact-no-detect-rows-v1",
        "focused_rendered": True,
        "raw_composite_result_chars": len(raw_output),
        "raw_composite_result_sha256": raw_sha,
        "complete_detect_audit": audit,
        "result_chars": len(focused_output),
        "result_sha256": focused_sha,
    }

    generic = paged_generic_runtime(
        AgentContext(
            repo_path=repo,
            baseline_revision=baseline,
            head_revision=revision,
        )
    )
    diff_arguments: dict[str, Any] = {}
    diff_content = generic.extra_tools["git_diff"][1](diff_arguments)
    bridge_sha = hashlib.sha256(
        Path(scorer._exact_composite_module._BRIDGE_PATH).read_bytes()
    ).hexdigest()
    renderer_sha = hashlib.sha256(
        Path(focused_exact.__file__).read_bytes()
    ).hexdigest()
    backend_sha = "b" * 64
    dependencies = [
        "git",
        "filesystem",
        f"gitnexus-exact-composite:1.6.9:{backend_sha}:{bridge_sha}",
        (
            "gitnexus-focused-exact-renderer:"
            f"focused-exact-no-detect-rows-v1:{renderer_sha}"
        ),
    ]
    setup = dict(generic.metadata)
    setup.update(
        {
            "provider": "gitnexus",
            "isolated": True,
            "source_head": revision,
            "source_tree": _git(repo, "rev-parse", "HEAD^{tree}"),
            "agent_repo_clean": True,
            "agent_repo_graph_dirs_absent": True,
            "index_success": True,
            "package_version": "1.6.9",
            "binary_sha256": "a" * 64,
            "index_stats": {
                "lastCommit": revision,
                "stats": {"embeddings": 0},
                "capabilities": {
                    "graph": {"status": "available"},
                    "fts": {"status": "available"},
                },
            },
            "installer_used": False,
            "mcp_used": False,
            "prompt_or_hook_injection": False,
            "registry_home_isolated": True,
            "fts_extension_policy": "load-only",
            "embeddings_enabled": False,
            "external_service_started": False,
            "interactive_installer_used": False,
            "package_integrity": scorer.GITNEXUS_PACKAGE_INTEGRITY,
            "package_json_sha256": "c" * 64,
            "backend_module": "dist/mcp/local/local-backend.js",
            "backend_module_sha256": backend_sha,
            "resources_module": "dist/mcp/resources.js",
            "resources_module_sha256": "d" * 64,
            "bridge_sha256": bridge_sha,
            "renderer_sha256": renderer_sha,
            "official_backend_export": "LocalBackend",
            "official_resource_export": "readResource",
            "base_profile_id": "paged_generic",
            "composite_profile_id": "gitnexus_official_structured_k1_exact_composite",
            "profile_id": "gitnexus_official_structured_k1_focused_exact",
            "render_profile": "focused-exact-no-detect-rows-v1",
            "render_protocol_version": "gitnexus-k1-focused-exact-render-v1",
            "implementation_mode": (
                "official-local-backend-k1-exact-composite-focused-renderer"
            ),
            "dependencies": dependencies,
            "dependency_sha256": scorer._hash_json(dependencies),
            "tool_surface": ["gitnexus_focused_exact"],
            "fixed_provider_arguments": {"scope": "compare", "base_ref": baseline},
            "selector_policy": {
                "version": "k1-cross-community-unique-exact-uid-v1",
                "max_selected": 1,
                "allowed_uid_kinds": [
                    "Function",
                    "Method",
                    "Class",
                    "Interface",
                    "Constructor",
                ],
                "exclude_tests": True,
                "require_unique_changed_name": True,
                "require_affected_process_membership": True,
                "ordering": ordering,
            },
            "impact_policy": {
                key: impact_arguments[key]
                for key in (
                    "direction",
                    "mode",
                    "maxDepth",
                    "includeTests",
                    "limit",
                    "offset",
                    "summaryOnly",
                )
            },
            "runtime_bindings": {"repo": "isolated_index_clone"},
            "model_controlled_provider_arguments": [],
            "cli_formatter_used": False,
            "persistent_backend_scope": "one_composite_tool_invocation",
            "setup_calls": [
                {
                    "operation": "analyze",
                    "seconds": 0.1,
                    "exit_code": 0,
                    "output_chars": 10,
                    "output_sha256": "e" * 64,
                    "error": False,
                }
            ],
            "provider_calls": provider_calls,
            "query_calls": [query],
            "focused_render_audits": [audit],
            "output_transport": {
                "complete_detect_changes_output": False,
                "structured_json": True,
                "impact_bounded_by_provider": True,
                "context_backend_category_cap": 30,
                "bridge_telemetry_removed_from_model_output": True,
                "wrapper_truncation": False,
                "normalization": "recursive_object_key_sort_arrays_preserved",
                "sanitization": "isolated_clone_path_only",
                "complete_detect_changes_used_before_render": True,
                "detect_rows_model_visible": 0,
                "focused_rendering": True,
                "cli_formatter_used": False,
            },
            "cleanup_success": True,
        }
    )
    diff_trace = {
        "turn": 1,
        "ordinal": 1,
        "name": "git_diff",
        "arguments": diff_arguments,
        "arguments_sha256": scorer._hash_json(diff_arguments),
        "seconds": 0.01,
        "error": False,
        **scorer._result_trace(diff_content),
    }
    focused_trace = {
        "turn": 2,
        "ordinal": 1,
        "name": "gitnexus_focused_exact",
        "arguments": {},
        "arguments_sha256": scorer._hash_json({}),
        "seconds": 0.1,
        "error": False,
        **scorer._result_trace(focused_output),
    }
    return {
        "protocol_version": scorer.PROTOCOL_GITNEXUS_FOCUSED_EXACT,
        "baseline_revision": baseline,
        "head_revision": revision,
        "tools": {"names": [*BASE_TOOLS, "gitnexus_focused_exact"]},
        "conversation": {"tool_trace": [diff_trace, focused_trace]},
        "setup": setup,
    }


def test_focused_exact_scorer_binds_complete_ledgers_and_exactly_once_adoption(
    tmp_path: Path,
) -> None:
    repo, revision = _repo(tmp_path)
    run = _focused_exact_adoption_run(repo, revision)
    focused_trace = run["conversation"]["tool_trace"][1]

    bound = scorer._gitnexus_focused_exact_setup_call(
        run,
        focused_trace,
        repo_path=repo,
    )
    assert bound is not None
    assert bound["operations"] == [
        "detect_changes",
        "context",
        "impact",
        "trace",
        "process_resource",
    ]
    status = scorer._adoption_status(run, repo_path=repo)["gitnexus_focused_exact"]
    assert status["passed"] is True
    assert status["successful_calls"] == 1
    assert status["diff_replay_failures"] == 0
    assert status["rule"].startswith("exactly_1_after_verified_diff")

    duplicate = copy.deepcopy(run)
    duplicate_call = copy.deepcopy(duplicate["conversation"]["tool_trace"][1])
    duplicate_call["turn"] = 3
    duplicate["conversation"]["tool_trace"].append(duplicate_call)
    duplicate_status = scorer._adoption_status(duplicate, repo_path=repo)[
        "gitnexus_focused_exact"
    ]
    assert duplicate_status["passed"] is False
    assert duplicate_status["total_calls"] == 2


def test_focused_exact_scorer_requires_diff_then_one_forced_turn_then_auto(
    tmp_path: Path,
) -> None:
    repo, revision = _repo(tmp_path)
    run = _focused_exact_adoption_run(repo, revision)
    forced_choice = {
        "type": "function",
        "function": {"name": "gitnexus_focused_exact"},
    }
    run["conversation"].update(
        {
            "turn_trace": [
                {
                    "turn": 1,
                    "tool_calls": ["git_diff"],
                    "tool_choice": "auto",
                    "forced_tool": None,
                },
                {
                    "turn": 2,
                    "tool_calls": ["gitnexus_focused_exact"],
                    "tool_choice": forced_choice,
                    "forced_tool": "gitnexus_focused_exact",
                },
                {
                    "turn": 3,
                    "tool_calls": ["submit"],
                    "tool_choice": "auto",
                    "forced_tool": None,
                },
            ],
            "model_call_trace": [
                {
                    "conversation_turn": 1,
                    "request_id": "request-1",
                    "tool_choice": "auto",
                    "forced_tool": None,
                },
                {
                    "conversation_turn": 2,
                    "request_id": "request-2",
                    "tool_choice": forced_choice,
                    "forced_tool": "gitnexus_focused_exact",
                },
                {
                    "conversation_turn": 3,
                    "request_id": "request-3",
                    "tool_choice": "auto",
                    "forced_tool": None,
                },
            ],
            "transport_attempt_trace": [
                {"tool_choice": "auto"},
                {"tool_choice": forced_choice},
                {"tool_choice": "auto"},
            ],
            "manipulation_trace": [
                {
                    "event": "forced_tool_request_armed",
                    "conversation_turn": 1,
                    "trigger_tool": "git_diff",
                    "successful_ordinals": [1],
                    "forced_tool": "gitnexus_focused_exact",
                    "automatic_target_generation": False,
                },
                {
                    "event": "forced_tool_request_started",
                    "conversation_turn": 2,
                    "tool": "gitnexus_focused_exact",
                    "trigger": "prior_successful_git_diff",
                },
                {
                    "event": "forced_tool_request_completed",
                    "conversation_turn": 2,
                    "tool": "gitnexus_focused_exact",
                    "request_id": "request-2",
                    "observed_tool_calls": ["gitnexus_focused_exact"],
                    "auto_restored_for_next_request": True,
                },
            ],
        }
    )

    scorer._validate_v2_manipulation(run, location="unit")
    duplicate_forced = copy.deepcopy(run)
    duplicate_forced["conversation"]["turn_trace"][1]["tool_calls"] = [
        "gitnexus_focused_exact",
        "gitnexus_focused_exact",
    ]
    duplicate_forced["conversation"]["manipulation_trace"][2][
        "observed_tool_calls"
    ] = ["gitnexus_focused_exact", "gitnexus_focused_exact"]
    with pytest.raises(ValueError, match="exactly one tool call"):
        scorer._validate_v2_manipulation(duplicate_forced, location="unit")


@pytest.mark.parametrize(
    "mutation",
    (
        "detect_count",
        "provider_error",
        "provider_partial",
        "provider_pagination",
        "raw_focused_binding",
        "visible_raw_rows",
        "impact_hash",
        "selector_policy",
    ),
)
def test_focused_exact_scorer_fails_closed_on_ledger_tampering(
    tmp_path: Path,
    mutation: str,
) -> None:
    repo, revision = _repo(tmp_path)
    run = _focused_exact_adoption_run(repo, revision)
    if mutation == "detect_count":
        run["setup"]["focused_render_audits"][0]["changed_symbols_count"] = 138
    elif mutation == "provider_error":
        run["setup"]["provider_calls"][1]["error"] = True
    elif mutation == "provider_partial":
        run["setup"]["provider_calls"][2]["partial"] = True
    elif mutation == "provider_pagination":
        run["setup"]["provider_calls"][2].update(
            {"pagination_field_present": True, "pagination": {"next": "cursor"}}
        )
    elif mutation == "raw_focused_binding":
        run["setup"]["query_calls"][0]["raw_composite_result_sha256"] = "0" * 64
    elif mutation == "visible_raw_rows":
        run["conversation"]["tool_trace"][1]["gitnexus_focused_exact_result"][
            "model_visible_raw_detect_rows"
        ] = 1
    elif mutation == "impact_hash":
        run["setup"]["provider_calls"][2]["output_sha256"] = "0" * 64
    elif mutation == "selector_policy":
        run["setup"]["selector_policy"]["max_selected"] = 2

    focused_trace = run["conversation"]["tool_trace"][1]
    assert (
        scorer._gitnexus_focused_exact_setup_call(
            run,
            focused_trace,
            repo_path=repo,
        )
        is None
    )
    assert (
        scorer._adoption_status(run, repo_path=repo)["gitnexus_focused_exact"][
            "passed"
        ]
        is False
    )


def test_focused_exact_scorer_locks_protocol_prompt_and_tool_schema(
    tmp_path: Path,
) -> None:
    repo, revision = _repo(tmp_path)
    context = AgentContext(
        repo_path=repo,
        baseline_revision=_git(repo, "rev-parse", f"{revision}^"),
        head_revision=revision,
    )
    runtime = paged_generic_runtime(context)
    extras = dict(runtime.extra_tools)
    extras["gitnexus_focused_exact"] = (
        focused_exact.GITNEXUS_FOCUSED_EXACT_DEFINITION,
        lambda _arguments: "unused",
    )
    treatment_names = (*BASE_TOOLS, "gitnexus_focused_exact")
    treatment_definitions = SingleAgentRunner.tool_definitions(
        treatment_names,
        extras,
    )
    control_definitions = treatment_definitions[: len(BASE_TOOLS)]

    def metadata(
        names: tuple[str, ...],
        definitions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "names": list(names),
            "schema_sha256": scorer._hash_json(definitions),
            "canonical_schema_chars": len(scorer._canonical_json(definitions)),
            "schemas": [
                {
                    "name": definition["function"]["name"],
                    "sha256": scorer._hash_json(definition),
                    "canonical_chars": len(scorer._canonical_json(definition)),
                }
                for definition in definitions
            ],
            "definitions": definitions,
            "base_names": list(BASE_TOOLS),
            "base_schema_sha256": scorer._hash_json(control_definitions),
        }

    treatment_tools = metadata(treatment_names, treatment_definitions)
    control_tools = metadata(BASE_TOOLS, control_definitions)
    treatment = scorer._validate_tool_metadata(
        treatment_tools,
        agent="gitnexus_focused_exact_agent",
        protocol_version=scorer.PROTOCOL_GITNEXUS_FOCUSED_EXACT,
        location="unit-treatment",
    )
    control = scorer._validate_tool_metadata(
        control_tools,
        agent=scorer.GITNEXUS_FOCUSED_EXACT_CONTROL_AGENT,
        protocol_version=scorer.PROTOCOL_GITNEXUS_FOCUSED_EXACT,
        location="unit-control",
    )
    assert treatment[3] == control[3]
    scorer._validate_agent_protocol(
        "gitnexus_focused_exact_agent",
        scorer.PROTOCOL_GITNEXUS_FOCUSED_EXACT,
        location="unit",
    )
    scorer._validate_agent_protocol(
        scorer.GITNEXUS_FOCUSED_EXACT_CONTROL_AGENT,
        scorer.PROTOCOL_GITNEXUS_FOCUSED_EXACT,
        location="unit",
    )

    prompt = {
        "system": scorer.GITNEXUS_FOCUSED_EXACT_SYSTEM_PROMPT,
        "user": "same user prompt",
        "system_sha256": scorer.GITNEXUS_FOCUSED_EXACT_SYSTEM_PROMPT_SHA256,
        "user_sha256": scorer._hash_text("same user prompt"),
    }
    scorer._validate_focused_exact_prompt(
        scorer._validate_prompt(prompt, location="unit"),
        protocol_version=scorer.PROTOCOL_GITNEXUS_FOCUSED_EXACT,
        location="unit",
    )
    assert len(prompt["system"]) == 2048

    tampered_prompt = dict(prompt)
    tampered_prompt["system"] += " "
    tampered_prompt["system_sha256"] = scorer._hash_text(tampered_prompt["system"])
    with pytest.raises(ValueError, match="byte-exact"):
        scorer._validate_focused_exact_prompt(
            tampered_prompt,
            protocol_version=scorer.PROTOCOL_GITNEXUS_FOCUSED_EXACT,
            location="unit",
        )

    tampered_tools = copy.deepcopy(treatment_tools)
    tampered_tools["definitions"][-1]["function"]["parameters"][
        "additionalProperties"
    ] = True
    _rehash_tools({"tools": tampered_tools})
    with pytest.raises(ValueError, match="frozen candidate"):
        scorer._validate_tool_metadata(
            tampered_tools,
            agent="gitnexus_focused_exact_agent",
            protocol_version=scorer.PROTOCOL_GITNEXUS_FOCUSED_EXACT,
            location="unit",
        )


def test_native_control_has_no_graph_adoption_requirement() -> None:
    run = {
        "tools": {"names": list(BASE_TOOLS)},
        "conversation": {"tool_trace": []},
        "setup": {},
    }

    status = scorer._adoption_status(run)
    assert status["codegraph_explore"] == {
        "required": False,
        "rule": "not_applicable",
        "successful_calls": None,
        "total_calls": None,
        "passed": True,
    }
    assert status["gitnexus_change_impact"]["required"] is False
    assert all(item["passed"] is True for item in status.values())


def test_native_result_markers_are_format_and_case_tolerant() -> None:
    markers = scorer._result_trace(
        "SOURCE CODE\n  12 | def current():\nBlast Radius\n"
        "CHANGED SYMBOLS:\ncurrent\nAFFECTED EXECUTION FLOWS:\nflow\n"
    )["native_graph_markers"]

    assert markers == {
        "codegraph_source": True,
        "codegraph_blast_radius": True,
        "codegraph_line_numbered_source": True,
        "gitnexus_changed_symbols": True,
        "gitnexus_affected_flows": True,
    }


@pytest.mark.parametrize(
    ("agent", "protocol", "valid"),
    [
        (scorer.NATIVE_CONTROL_AGENT, scorer.PROTOCOL_V3, True),
        ("codegraph_explore_direct_agent", scorer.PROTOCOL_V3, True),
        ("codegraph_explore_change_seed_agent", scorer.PROTOCOL_V3, True),
        ("codegraph_node_impact_agent", scorer.PROTOCOL_V3, True),
        (scorer.GITNEXUS_FIRST_CONTROL_AGENT, scorer.PROTOCOL_V4, True),
        ("gitnexus_change_impact_first_agent", scorer.PROTOCOL_V4, True),
        (scorer.GITNEXUS_STRUCTURED_FIRST_CONTROL_AGENT, scorer.PROTOCOL_V5, True),
        ("gitnexus_structured_change_first_agent", scorer.PROTOCOL_V5, True),
        (
            scorer.GITNEXUS_FOCUSED_EXACT_CONTROL_AGENT,
            scorer.PROTOCOL_GITNEXUS_FOCUSED_EXACT,
            True,
        ),
        (
            "gitnexus_focused_exact_agent",
            scorer.PROTOCOL_GITNEXUS_FOCUSED_EXACT,
            True,
        ),
        (scorer.DEFAULT_CONTROL_AGENT, scorer.PROTOCOL_V2, True),
        (scorer.NATIVE_CONTROL_AGENT, scorer.PROTOCOL_V2, False),
        (scorer.DEFAULT_CONTROL_AGENT, scorer.PROTOCOL_V3, False),
        ("gitnexus_structured_change_first_agent", scorer.PROTOCOL_V4, False),
        ("gitnexus_focused_exact_agent", scorer.PROTOCOL_V3, False),
    ],
)
def test_agent_names_are_bound_to_their_protocol_family(
    agent: str,
    protocol: str,
    valid: bool,
) -> None:
    if valid:
        scorer._validate_agent_protocol(agent, protocol, location="unit")
    else:
        with pytest.raises(ValueError, match="bound to protocol"):
            scorer._validate_agent_protocol(agent, protocol, location="unit")


def test_stability_requires_mean_direction_and_three_of_three_adoption() -> None:
    def row(
        *,
        hits: int,
        seconds: float,
        tokens: int,
        eligible: bool = True,
        adoption: bool = True,
    ) -> dict[str, Any]:
        return {
            "metrics": {
                "evidence_valid_hits": hits,
                "end_to_end_seconds": seconds,
                "actual_tokens": tokens,
            },
            "reliability": {
                "eligible": eligible,
                "adoption_passed": adoption,
            },
        }

    rows_by_pair = {
        "pair-1": {
            "treatment": row(hits=1, seconds=9.0, tokens=90),
            "comparator": row(hits=0, seconds=10.0, tokens=100),
        },
        "pair-2": {
            "treatment": row(hits=1, seconds=9.0, tokens=90),
            "comparator": row(hits=0, seconds=10.0, tokens=100),
        },
        "pair-3": {
            "treatment": row(hits=1, seconds=20.0, tokens=95),
            "comparator": row(hits=0, seconds=10.0, tokens=100),
        },
    }
    stability = scorer._comparison_stability(
        treatment_agent="treatment",
        comparator_agent="comparator",
        rows_by_pair=rows_by_pair,
    )

    assert stability["stable_recall"] is True
    assert stability["time_improved_pairs"] == 2
    assert stability["mean_time_improved"] is False
    assert stability["stable_efficiency_by_metric"]["end_to_end_seconds"] is False
    assert stability["token_improved_pairs"] == 3
    assert stability["mean_token_improved"] is True
    assert stability["stable_efficiency_by_metric"]["actual_tokens"] is True

    rows_by_pair["pair-3"]["treatment"]["reliability"] = {
        "eligible": False,
        "adoption_passed": False,
    }
    adoption_failed = scorer._comparison_stability(
        treatment_agent="treatment",
        comparator_agent="comparator",
        rows_by_pair=rows_by_pair,
    )
    assert adoption_failed["status"] == "reliability_failed"
    assert adoption_failed["reliability"]["treatment_adoption_passed_pairs"] == 2
    assert adoption_failed["stable_recall"] is False
    assert adoption_failed["stable_efficiency"] is False


def test_pareto_frontier_keeps_tradeoffs_and_removes_dominated_points() -> None:
    points = {
        "control": {
            "mean_hits": 5.0,
            "mean_total_seconds": 100.0,
            "mean_actual_tokens": 100_000.0,
        },
        "higher_recall": {
            "mean_hits": 6.0,
            "mean_total_seconds": 110.0,
            "mean_actual_tokens": 105_000.0,
        },
        "faster": {
            "mean_hits": 5.0,
            "mean_total_seconds": 80.0,
            "mean_actual_tokens": 95_000.0,
        },
        "dominated": {
            "mean_hits": 4.0,
            "mean_total_seconds": 120.0,
            "mean_actual_tokens": 110_000.0,
        },
    }

    assert scorer._pareto_frontier(points) == ["higher_recall", "faster"]

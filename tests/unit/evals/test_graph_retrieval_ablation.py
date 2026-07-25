from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

SINGLE_AGENT_DIR = Path(__file__).resolve().parents[3] / "evals" / "field" / "single_agent"
sys.path.insert(0, str(SINGLE_AGENT_DIR))

import _graph_runtime as graph_runtime  # noqa: E402
import codegraph_agent  # noqa: E402
import gitnexus_agent  # noqa: E402
import graph_default_agent  # noqa: E402
import run_graph_ablation  # noqa: E402
import score_graph_ablation  # noqa: E402
from _runner import (  # noqa: E402
    BASE_TOOLS,
    COMMON_SYSTEM_PROMPT,
    SPECIAL_TOOLS,
    AgentContext,
    common_initial_message,
)


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _visible_repo(tmp_path: Path) -> tuple[Path, AgentContext]:
    repo = tmp_path / "visible"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "service.ts").write_text(
        "export function service(): string { return 'ok'; }\n",
        encoding="utf-8",
    )
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "graph-test@example.com")
    _git(repo, "config", "user.name", "Graph Test")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fixture")
    revision = _git(repo, "rev-parse", "HEAD")
    return repo, AgentContext(
        repo_path=repo,
        baseline_revision=revision,
        head_revision=revision,
    )


def _graph_clone(source: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--quiet", "--no-hardlinks", str(source), str(destination)],
        check=True,
        capture_output=True,
        text=True,
    )
    return destination


def _completed(
    argv: list[str],
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)


def _route_real_git(
    real_run: Callable[..., subprocess.CompletedProcess[str]],
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None,
) -> subprocess.CompletedProcess[str] | None:
    if argv[0] != "git":
        return None
    return real_run(argv, cwd=cwd, env=env)


def test_graph_agent_menus_are_strict_seed_free_supersets() -> None:
    default = graph_default_agent.AGENT.tools
    codegraph = codegraph_agent.AGENT.tools
    gitnexus = gitnexus_agent.AGENT.tools

    assert default == BASE_TOOLS
    assert codegraph == BASE_TOOLS + graph_runtime.CODEGRAPH_TOOLS
    assert gitnexus == BASE_TOOLS + graph_runtime.GITNEXUS_TOOLS
    assert graph_runtime.EXPECTED_TOOL_MENUS == {
        graph_runtime.GRAPH_DEFAULT_AGENT: default,
        graph_runtime.CODEGRAPH_AGENT: codegraph,
        graph_runtime.GITNEXUS_AGENT: gitnexus,
    }
    assert codegraph[: len(default)] == default
    assert gitnexus[: len(default)] == default
    assert len(codegraph) > len(default)
    assert len(gitnexus) > len(default)
    assert not set(graph_runtime.CODEGRAPH_TOOLS) & set(default)
    assert not set(graph_runtime.GITNEXUS_TOOLS) & set(default)
    assert not set(SPECIAL_TOOLS) & (set(default) | set(codegraph) | set(gitnexus))


def test_all_graph_arms_use_the_same_seed_free_prompt() -> None:
    prompts = [
        (
            COMMON_SYSTEM_PROMPT.encode("utf-8"),
            common_initial_message(
                baseline_revision="base",
                head_revision="head",
            ).encode("utf-8"),
        )
        for _agent in (
            graph_default_agent.AGENT,
            codegraph_agent.AGENT,
            gitnexus_agent.AGENT,
        )
    ]

    assert prompts[0] == prompts[1] == prompts[2]
    prompt = b"\n".join(prompts[0]).decode("utf-8").lower()
    for forbidden in (
        "read_briefing",
        "extract_claims",
        "record_finding",
        "worklist",
        "deterministic change seed",
        "alignment table",
    ):
        assert forbidden not in prompt


def test_graph_scorer_normalizes_only_the_v3_default_control() -> None:
    legacy_run = {
        "agent": score_graph_ablation.LEGACY_CONTROL_AGENT,
        "protocol_version": score_graph_ablation.LEGACY_CONTROL_PROTOCOL_VERSION,
    }
    normalized, source_agent, source_protocol, reused = score_graph_ablation._normalize_source_run(
        {
            "agent": score_graph_ablation.LEGACY_CONTROL_AGENT,
            "protocol_version": score_graph_ablation.LEGACY_CONTROL_PROTOCOL_VERSION,
        },
        legacy_run,
        path=Path("legacy-control.json"),
    )

    assert normalized["agent"] == graph_runtime.GRAPH_DEFAULT_AGENT
    assert normalized["protocol_version"] == graph_runtime.GRAPH_PROTOCOL_VERSION
    assert normalized["source_agent"] == score_graph_ablation.LEGACY_CONTROL_AGENT
    assert normalized["source_protocol_version"] == source_protocol
    assert normalized["reused_control"] is True
    assert reused is True
    assert source_agent == score_graph_ablation.LEGACY_CONTROL_AGENT
    assert source_protocol == score_graph_ablation.LEGACY_CONTROL_PROTOCOL_VERSION
    assert legacy_run == {
        "agent": score_graph_ablation.LEGACY_CONTROL_AGENT,
        "protocol_version": score_graph_ablation.LEGACY_CONTROL_PROTOCOL_VERSION,
    }

    with pytest.raises(ValueError, match="graph protocol is required"):
        score_graph_ablation._normalize_source_run(
            {
                "agent": graph_runtime.CODEGRAPH_AGENT,
                "protocol_version": score_graph_ablation.LEGACY_CONTROL_PROTOCOL_VERSION,
            },
            {
                "agent": graph_runtime.CODEGRAPH_AGENT,
                "protocol_version": score_graph_ablation.LEGACY_CONTROL_PROTOCOL_VERSION,
            },
            path=Path("invalid-treatment.json"),
        )


def test_graph_default_runtime_never_clones_or_resolves_a_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    visible, context = _visible_repo(tmp_path)

    def forbidden(*_arguments: Any, **_keywords: Any) -> None:
        raise AssertionError("control arm attempted graph setup")

    monkeypatch.setattr(graph_runtime, "_clone_for_index", forbidden)
    monkeypatch.setattr(graph_runtime, "_binary", forbidden)

    runtime = graph_runtime.graph_default_runtime(context)

    assert set(runtime.extra_tools) == {"git_changed_files", "git_diff", "git_show"}
    assert runtime.metadata["provider"] == "none"
    assert runtime.metadata["isolation_clone_seconds"] == 0.0
    assert runtime.metadata["index_seconds"] == 0.0
    assert runtime.metadata["index_size_bytes"] == 0
    assert runtime.metadata["query_calls"] == []
    assert not (visible / ".codegraph").exists()
    assert not (visible / ".gitnexus").exists()
    assert _git(visible, "status", "--porcelain=v1", "--untracked-files=all") == ""


def test_codegraph_runtime_isolated_handler_and_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    visible, context = _visible_repo(tmp_path)
    graph_repo = _graph_clone(visible, tmp_path / "codegraph-index" / "repo")
    binary = tmp_path / "bin" / "codegraph"
    binary.parent.mkdir()
    binary.write_text("fake CodeGraph binary\n", encoding="utf-8")
    calls: list[dict[str, Any]] = []
    real_run = graph_runtime._run
    large_result = f"source from {graph_repo}\n" + "x" * 250_000 + "\nEND"

    def fake_run(
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        git_result = _route_real_git(real_run, argv, cwd=cwd, env=env)
        if git_result is not None:
            return git_result
        calls.append({"argv": list(argv), "cwd": Path(cwd), "env": dict(env or {})})
        command = argv[1]
        if command == "init":
            index = Path(cwd) / ".codegraph"
            index.mkdir()
            (index / "codegraph.db").write_bytes(b"index")
            return _completed(argv, stdout="indexed\n")
        if command == "status":
            return _completed(
                argv,
                stdout=json.dumps({"version": graph_runtime.CODEGRAPH_VERSION, "nodeCount": 11}),
            )
        if command == "explore":
            return _completed(argv, stdout=large_result)
        raise AssertionError(f"unexpected CodeGraph command: {argv}")

    ticks = iter((10.0, 12.5, 20.0, 20.75))
    monkeypatch.setattr(graph_runtime, "_run", fake_run)
    monkeypatch.setattr(
        graph_runtime,
        "_binary",
        lambda *_arguments: (binary, "c" * 64),
    )
    monkeypatch.setattr(
        graph_runtime,
        "_clone_for_index",
        lambda _context, provider: (
            (graph_repo, 0.125)
            if provider == "codegraph"
            else (_ for _ in ()).throw(AssertionError(provider))
        ),
    )
    monkeypatch.setattr(
        graph_runtime,
        "time",
        SimpleNamespace(monotonic=lambda: next(ticks)),
    )

    runtime = graph_runtime.codegraph_runtime(context)
    explore = runtime.extra_tools["codegraph_explore"][1]
    output = explore({"query": "  runSpecialistLoop  ", "max_files": 7})

    assert output == "source from .\n" + "x" * 250_000 + "\nEND"
    assert output.endswith("END")
    assert output.count("x") == 250_000
    assert "[truncated" not in output
    assert calls[0]["argv"] == [str(binary), "init", str(graph_repo)]
    assert calls[0]["cwd"] == graph_repo
    assert calls[1]["argv"] == [str(binary), "status", "--json", str(graph_repo)]
    assert calls[2]["argv"] == [
        str(binary),
        "explore",
        "--path",
        str(graph_repo),
        "--max-files",
        "7",
        "runSpecialistLoop",
    ]
    for call in calls:
        assert call["env"]["DO_NOT_TRACK"] == "1"
        assert call["env"]["NO_COLOR"] == "1"
        assert call["env"]["CODEGRAPH_NO_DOWNLOAD"] == "1"
    assert (graph_repo / ".codegraph" / "codegraph.db").is_file()
    assert not (visible / ".codegraph").exists()
    assert not (visible / ".gitnexus").exists()
    assert runtime.metadata["agent_repo_graph_dirs_absent"] is True
    assert runtime.metadata["agent_repo_clean"] is True
    assert runtime.metadata["index_seconds"] == 2.5
    assert runtime.metadata["index_size_bytes"] == len(b"index")
    assert runtime.metadata["index_stats"] == {
        "version": graph_runtime.CODEGRAPH_VERSION,
        "nodeCount": 11,
    }
    assert runtime.metadata["telemetry_disabled"] is True
    assert runtime.metadata["update_checks_disabled"] is True
    assert runtime.metadata["query_calls"] == [
        {
            "provider": "codegraph",
            "tool": "codegraph_explore",
            "arguments": {"query": "runSpecialistLoop", "max_files": 7},
            "seconds": 0.75,
            "exit_code": 0,
            "output_chars": len(output),
            "error": False,
        }
    ]
    assert _git(visible, "status", "--porcelain=v1", "--untracked-files=all") == ""

    for invalid in (
        {"query": ""},
        {"query": "symbol", "max_files": False},
        {"query": "symbol", "max_files": "not-an-integer"},
        {"query": "symbol", "max_files": 0},
        {"query": "symbol", "max_files": 21},
    ):
        with pytest.raises(ValueError):
            explore(invalid)
    assert len([call for call in calls if call["argv"][1] == "explore"]) == 1


def test_gitnexus_runtime_isolated_handlers_and_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    visible, context = _visible_repo(tmp_path)
    graph_repo = _graph_clone(visible, tmp_path / "gitnexus-index" / "repo")
    binary = tmp_path / "bin" / "gitnexus"
    binary.parent.mkdir()
    binary.write_text("fake GitNexus binary\n", encoding="utf-8")
    calls: list[dict[str, Any]] = []
    real_run = graph_runtime._run
    large_result = f"result from {graph_repo}\n" + "y" * 275_000 + "\nEND"

    def fake_run(
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        git_result = _route_real_git(real_run, argv, cwd=cwd, env=env)
        if git_result is not None:
            return git_result
        calls.append({"argv": list(argv), "cwd": Path(cwd), "env": dict(env or {})})
        command = argv[1]
        if command == "analyze":
            index = Path(cwd) / ".gitnexus"
            index.mkdir()
            (index / "graph.db").write_bytes(b"gitnexus-index")
            (index / "meta.json").write_text(
                json.dumps(
                    {
                        "lastCommit": context.head_revision,
                        "stats": {
                            "files": 3,
                            "nodes": 11,
                            "edges": 12,
                            "communities": 2,
                            "processes": 4,
                            "embeddings": 0,
                        },
                        "capabilities": {
                            "graph": {"provider": "ladybugdb", "status": "available"},
                            "fts": {"provider": "ladybugdb-fts", "status": "available"},
                        },
                        "schemaVersion": 5,
                    }
                ),
                encoding="utf-8",
            )
            return _completed(argv, stdout="analyzed\n")
        if command in {"query", "context", "impact", "trace"}:
            return _completed(argv, stdout=large_result)
        raise AssertionError(f"unexpected GitNexus command: {argv}")

    ticks = iter((100.0, 103.0, 200.0, 200.1, 210.0, 210.2, 220.0, 220.3, 230.0, 230.4))
    monkeypatch.setattr(graph_runtime, "_run", fake_run)
    monkeypatch.setattr(
        graph_runtime,
        "_binary",
        lambda *_arguments: (binary, "g" * 64),
    )
    monkeypatch.setattr(
        graph_runtime,
        "_clone_for_index",
        lambda _context, provider: (
            (graph_repo, 0.25)
            if provider == "gitnexus"
            else (_ for _ in ()).throw(AssertionError(provider))
        ),
    )
    monkeypatch.setattr(
        graph_runtime,
        "time",
        SimpleNamespace(monotonic=lambda: next(ticks)),
    )

    runtime = graph_runtime.gitnexus_runtime(context)
    query = runtime.extra_tools["gitnexus_query"][1]
    symbol_context = runtime.extra_tools["gitnexus_context"][1]
    impact = runtime.extra_tools["gitnexus_impact"][1]
    trace = runtime.extra_tools["gitnexus_trace"][1]
    outputs = [
        query(
            {
                "search_query": "  react loop  ",
                "task_context": "doc drift",
                "goal": "impact",
                "include_content": True,
                "limit": 7,
            }
        ),
        symbol_context(
            {
                "name": "runSpecialistLoop",
                "file_path": "src/agent/react-executor.ts",
                "include_content": True,
                "limit": 4,
            }
        ),
        impact(
            {
                "target": "runSpecialistLoop",
                "file_path": "src/agent/react-executor.ts",
                "direction": "downstream",
                "max_depth": 3,
                "include_tests": True,
                "limit": 8,
            }
        ),
        trace(
            {
                "from_symbol": "createSpecialistNode",
                "to_symbol": "runSpecialistLoop",
                "from_file": "src/agent/nodes.ts",
                "to_file": "src/agent/react-executor.ts",
                "max_depth": 9,
                "include_tests": True,
            }
        ),
    ]

    expected_output = "result from .\n" + "y" * 275_000 + "\nEND"
    assert outputs == [expected_output] * 4
    assert all(output.endswith("END") for output in outputs)
    assert all(output.count("y") == 275_000 for output in outputs)
    assert all("[truncated" not in output for output in outputs)
    expected_argv = [
        [
            str(binary),
            "analyze",
            str(graph_repo),
            "--index-only",
            "--no-stats",
        ],
        [
            str(binary),
            "query",
            "react loop",
            "--context",
            "doc drift",
            "--goal",
            "impact",
            "--limit",
            "7",
            "--content",
        ],
        [
            str(binary),
            "context",
            "runSpecialistLoop",
            "--file",
            "src/agent/react-executor.ts",
            "--limit",
            "4",
            "--content",
        ],
        [
            str(binary),
            "impact",
            "runSpecialistLoop",
            "--file",
            "src/agent/react-executor.ts",
            "--direction",
            "downstream",
            "--depth",
            "3",
            "--limit",
            "8",
            "--include-tests",
        ],
        [
            str(binary),
            "trace",
            "createSpecialistNode",
            "runSpecialistLoop",
            "--from-file",
            "src/agent/nodes.ts",
            "--to-file",
            "src/agent/react-executor.ts",
            "--depth",
            "9",
            "--include-tests",
        ],
    ]
    assert [call["argv"] for call in calls] == expected_argv
    expected_home = str(graph_repo.parent / "home")
    for call in calls:
        assert call["cwd"] == graph_repo
        assert call["env"]["GITNEXUS_HOME"] == expected_home
        assert "GITNEXUS_MCP_READ_ONLY" not in call["env"]
        assert call["env"]["GITNEXUS_LBUG_EXTENSION_INSTALL"] == "load-only"
        assert call["env"]["GITNEXUS_LOG_LEVEL"] == "error"
        assert call["env"]["NO_COLOR"] == "1"
    assert (graph_repo / ".gitnexus" / "graph.db").is_file()
    assert (graph_repo.parent / "home").is_dir()
    assert not (visible / ".gitnexus").exists()
    assert not (visible / ".codegraph").exists()
    assert runtime.metadata["agent_repo_graph_dirs_absent"] is True
    assert runtime.metadata["agent_repo_clean"] is True
    assert runtime.metadata["index_seconds"] == 3.0
    assert runtime.metadata["index_size_bytes"] > len(b"gitnexus-index")
    assert runtime.metadata["index_stats"]["lastCommit"] == context.head_revision
    assert runtime.metadata["index_stats"]["stats"]["embeddings"] == 0
    assert runtime.metadata["registry_home_isolated"] is True
    assert runtime.metadata["wrapper_read_only_allowlist"] is True
    assert runtime.metadata["fts_status"] == "available"
    assert runtime.metadata["graph_status"] == "available"
    assert runtime.metadata["embeddings_enabled"] is False
    assert [entry["seconds"] for entry in runtime.metadata["query_calls"]] == pytest.approx(
        [0.1, 0.2, 0.3, 0.4]
    )
    assert [entry["tool"] for entry in runtime.metadata["query_calls"]] == list(
        graph_runtime.GITNEXUS_TOOLS
    )
    assert all(
        entry["output_chars"] == len(expected_output) for entry in runtime.metadata["query_calls"]
    )
    assert all(entry["exit_code"] == 0 for entry in runtime.metadata["query_calls"])
    assert all(entry["error"] is False for entry in runtime.metadata["query_calls"])
    assert _git(visible, "status", "--porcelain=v1", "--untracked-files=all") == ""

    invalid_calls = (
        (query, {"search_query": ""}),
        (query, {"search_query": "x", "limit": True}),
        (symbol_context, {"name": " "}),
        (symbol_context, {"name": "x", "limit": 0}),
        (impact, {"target": ""}),
        (impact, {"target": "x", "direction": "sideways"}),
        (impact, {"target": "x", "max_depth": "not-an-integer"}),
        (trace, {"from_symbol": "x", "to_symbol": ""}),
        (trace, {"from_symbol": "x", "to_symbol": "y", "max_depth": 0}),
    )
    for handler, arguments in invalid_calls:
        with pytest.raises(ValueError):
            handler(arguments)
    assert len(calls) == len(expected_argv)


def test_launcher_starts_all_nine_processes_before_first_wait(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, int]] = []
    processes: list[Any] = []

    class FakeProcess:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def wait(self) -> int:
            events.append(("wait", self.pid))
            return 0

    def fake_popen(*_arguments: Any, **_keywords: Any) -> FakeProcess:
        process = FakeProcess(10_000 + len(processes))
        processes.append(process)
        events.append(("popen", process.pid))
        return process

    monkeypatch.setattr(run_graph_ablation.subprocess, "Popen", fake_popen)
    arguments = argparse.Namespace(
        fixture=tmp_path / "fixture-without-ground-truth-access",
        repo=None,
        baseline=None,
        output_dir=tmp_path / "output",
        python=Path(sys.executable),
    )

    manifest = run_graph_ablation.launch(arguments)

    assert len(processes) == 9
    assert events[:9] == [("popen", process.pid) for process in processes]
    assert sorted(events[9:]) == sorted(("wait", process.pid) for process in processes)
    assert manifest["job_count"] == 9
    assert manifest["all_started_before_wait"] is True
    assert manifest["raw_generation_only"] is True
    assert manifest["ground_truth_loaded"] is False
    assert [(job["pair_id"], job["agent"]) for job in manifest["jobs"]] == list(
        run_graph_ablation.SCHEDULE
    )


def test_launcher_can_reuse_control_and_start_only_six_treatments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, int]] = []
    processes: list[Any] = []

    class FakeProcess:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def wait(self) -> int:
            events.append(("wait", self.pid))
            return 0

    def fake_popen(*_arguments: Any, **_keywords: Any) -> FakeProcess:
        process = FakeProcess(20_000 + len(processes))
        processes.append(process)
        events.append(("popen", process.pid))
        return process

    monkeypatch.setattr(run_graph_ablation.subprocess, "Popen", fake_popen)
    arguments = argparse.Namespace(
        fixture=tmp_path / "fixture-without-ground-truth-access",
        repo=None,
        baseline=None,
        output_dir=tmp_path / "output",
        python=Path(sys.executable),
        agents=[graph_runtime.CODEGRAPH_AGENT, graph_runtime.GITNEXUS_AGENT],
    )

    manifest = run_graph_ablation.launch(arguments)

    assert len(processes) == 6
    assert events[:6] == [("popen", process.pid) for process in processes]
    assert sorted(events[6:]) == sorted(("wait", process.pid) for process in processes)
    assert manifest["job_count"] == 6
    assert manifest["all_started_before_wait"] is True
    assert manifest["selected_agents"] == [
        graph_runtime.CODEGRAPH_AGENT,
        graph_runtime.GITNEXUS_AGENT,
    ]
    assert [(job["pair_id"], job["agent"]) for job in manifest["jobs"]] == list(
        run_graph_ablation.TREATMENT_ONLY_SCHEDULE
    )


def test_graph_scorer_preflights_nine_runs_and_scores_each_treatment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _context = _visible_repo(tmp_path)
    (repo / "docs").mkdir()
    (repo / "docs" / "guide.md").write_text(
        "# Guide\n\nThe old behavior is still documented.\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "docs")
    revision = _git(repo, "rev-parse", "HEAD")
    tree = _git(repo, "rev-parse", "HEAD^{tree}")

    ground_truth = tmp_path / "synthetic-ground-truth.json"
    ground_truth.write_text(
        json.dumps(
            {
                "window_lines": 5,
                "items": [
                    {
                        "label": "stale-guide",
                        "class": "prose",
                        "doc": "docs/guide.md",
                        "line": 3,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    finding = {
        "doc": "docs/guide.md",
        "line": 3,
        "quote": "The old behavior is still documented.",
        "why": "The implementation changed.",
        "code_evidence": "src/service.ts:1",
        "confidence": "high",
    }
    configuration = {field: None for field in score_graph_ablation.UNBOUNDED_CONFIGURATION_FIELDS}
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
            },
        }
    )
    phases = {
        graph_runtime.GRAPH_DEFAULT_AGENT: {
            "setup_seconds": 0.0,
            "isolation_clone_seconds": 0.0,
            "index_seconds": 0.0,
            "agent_seconds": 1.0,
            "cleanup_seconds": 0.0,
            "total_seconds": 1.0,
        },
        graph_runtime.CODEGRAPH_AGENT: {
            "setup_seconds": 2.5,
            "isolation_clone_seconds": 0.25,
            "index_seconds": 2.0,
            "agent_seconds": 4.0,
            "cleanup_seconds": 0.25,
            "total_seconds": 6.75,
        },
        graph_runtime.GITNEXUS_AGENT: {
            "setup_seconds": 3.5,
            "isolation_clone_seconds": 0.5,
            "index_seconds": 3.0,
            "agent_seconds": 5.0,
            "cleanup_seconds": 0.5,
            "total_seconds": 9.0,
        },
    }

    def setup_for(agent: str) -> dict[str, Any]:
        phase = phases[agent]
        setup: dict[str, Any] = {
            "provider": "none",
            "isolated": True,
            "source_head": revision,
            "source_tree": tree,
            "agent_repo_clean": True,
            "agent_repo_graph_dirs_absent": True,
            "index_success": True,
            "package_version": None,
            "binary_sha256": None,
            "isolation_clone_seconds": phase["isolation_clone_seconds"],
            "index_seconds": phase["index_seconds"],
            "index_size_bytes": 0,
            "index_stats": {},
            "installer_used": False,
            "mcp_used": False,
            "prompt_or_hook_injection": False,
            "query_calls": [],
            "cleanup_seconds": phase["cleanup_seconds"],
        }
        if agent == graph_runtime.CODEGRAPH_AGENT:
            setup.update(
                {
                    "provider": "codegraph",
                    "package_version": graph_runtime.CODEGRAPH_VERSION,
                    "binary_sha256": "a" * 64,
                    "index_size_bytes": 1_024,
                    "cleanup_success": True,
                    "telemetry_disabled": True,
                    "update_checks_disabled": True,
                }
            )
        elif agent == graph_runtime.GITNEXUS_AGENT:
            setup.update(
                {
                    "provider": "gitnexus",
                    "package_version": graph_runtime.GITNEXUS_VERSION,
                    "binary_sha256": "b" * 64,
                    "index_size_bytes": 2_048,
                    "cleanup_success": True,
                    "registry_home_isolated": True,
                    "wrapper_read_only_allowlist": True,
                    "fts_status": "available",
                    "graph_status": "available",
                    "fts_extension_policy": "load-only",
                    "embeddings_enabled": False,
                    "gitnexus_config_present": False,
                }
            )
        return setup

    artifacts: list[Path] = []
    schema_hashes = {
        graph_runtime.GRAPH_DEFAULT_AGENT: "1" * 64,
        graph_runtime.CODEGRAPH_AGENT: "2" * 64,
        graph_runtime.GITNEXUS_AGENT: "3" * 64,
    }
    for pair_number in (1, 2, 3):
        pair_id = f"pair-{pair_number}"
        for agent in score_graph_ablation.EXPECTED_AGENTS:
            reused_control = agent == graph_runtime.GRAPH_DEFAULT_AGENT
            source_agent = score_graph_ablation.LEGACY_CONTROL_AGENT if reused_control else agent
            source_protocol = (
                score_graph_ablation.LEGACY_CONTROL_PROTOCOL_VERSION
                if reused_control
                else graph_runtime.GRAPH_PROTOCOL_VERSION
            )
            invalid_submit = pair_number == 1 and agent == graph_runtime.CODEGRAPH_AGENT
            delivered = (
                [] if invalid_submit or agent == graph_runtime.GRAPH_DEFAULT_AGENT else [finding]
            )
            completed_at_ns = time.time_ns()
            total_seconds = phases[agent]["total_seconds"]
            run_started_at_ns = completed_at_ns - round(total_seconds * 1_000_000_000)
            raw_submit = (
                {"findings": [{"doc": "docs/guide.md", "line": 0}]}
                if invalid_submit
                else {"findings": delivered}
            )
            omitted_timing_fields = {"isolation_clone_seconds", "index_seconds"}
            if reused_control:
                omitted_timing_fields.add("cleanup_seconds")
            run = {
                "agent": source_agent,
                "protocol_version": source_protocol,
                "run": 1,
                "pair_key": f"{pair_id}.1",
                "generation_started_at_ns": run_started_at_ns,
                "generation_completed_at_ns": completed_at_ns,
                "completed_at_ns": completed_at_ns,
                "baseline_revision": revision,
                "head_revision": revision,
                "requested_model": "test-model",
                "prompt": {
                    "system_sha256": "same-system",
                    "user_sha256": "same-user",
                },
                "tools": {
                    "names": list(graph_runtime.EXPECTED_TOOL_MENUS[agent]),
                    "base_names": list(BASE_TOOLS),
                    "base_schema_sha256": "0" * 64,
                    "schema_sha256": schema_hashes[agent],
                },
                "configuration": configuration,
                "conversation": {
                    "ok": not invalid_submit,
                    "failure_reason": "submit_schema_invalid" if invalid_submit else None,
                    "turns": 1,
                    "tool_calls": 1,
                    "actual_models": ["test-model"],
                    "tool_counts": {"submit": 1},
                    "tool_errors": {},
                    "tool_result_chars": {},
                    "turn_trace": [
                        {
                            "turn": 1,
                            "finish_reason": "tool_calls",
                            "tool_calls": ["submit"],
                        }
                    ],
                },
                "raw_submit": raw_submit,
                "submission_only": delivered,
                "store": [],
                "delivered": delivered,
                "setup": {} if reused_control else setup_for(agent),
                "timing": {
                    field: value
                    for field, value in phases[agent].items()
                    if field not in omitted_timing_fields
                },
                "usage": {
                    "model_calls": 1,
                    "tool_calls": 1,
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "estimated_cost_usd": 0.01,
                },
            }
            artifact = {
                "protocol_version": source_protocol,
                "agent": source_agent,
                "pair_id": pair_id,
                "target": {"kind": "repo", "path": str(repo)},
                "baseline_revision": revision,
                "head_revision": revision,
                "requested_model": "test-model",
                "langfuse_enabled": False,
                "generation_started_at_ns": run_started_at_ns - 1_000_000,
                "generation_completed_at_ns": completed_at_ns,
                "completed_at_ns": completed_at_ns,
                "runs": [run],
            }
            path = tmp_path / f"{pair_id}-{agent}.json"
            path.write_text(json.dumps(artifact), encoding="utf-8")
            artifacts.append(path)

    real_read_bytes = Path.read_bytes
    ground_truth_reads: list[Path] = []

    def tracked_read_bytes(path: Path) -> bytes:
        if path.resolve() == ground_truth.resolve():
            ground_truth_reads.append(path.resolve())
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", tracked_read_bytes)
    with pytest.raises(ValueError, match="expected exactly 9 raw artifacts"):
        score_graph_ablation.score_artifacts(artifacts[:8], ground_truth)
    assert ground_truth_reads == []

    legacy_artifact = artifacts[0]
    legacy_original = real_read_bytes(legacy_artifact)
    bad_legacy_payload = json.loads(legacy_original)
    bad_legacy_payload["runs"][0]["setup"] = {"provider": "none"}
    legacy_artifact.write_text(json.dumps(bad_legacy_payload), encoding="utf-8")
    with pytest.raises(ValueError, match="legacy control setup must be exactly empty"):
        score_graph_ablation.score_artifacts(artifacts, ground_truth)
    assert ground_truth_reads == []
    legacy_artifact.write_bytes(legacy_original)

    bad_legacy_payload = json.loads(legacy_original)
    bad_legacy_payload["runs"][0]["timing"]["cleanup_seconds"] = 0.0
    legacy_artifact.write_text(json.dumps(bad_legacy_payload), encoding="utf-8")
    with pytest.raises(ValueError, match="legacy control timing unexpectedly includes cleanup"):
        score_graph_ablation.score_artifacts(artifacts, ground_truth)
    assert ground_truth_reads == []
    legacy_artifact.write_bytes(legacy_original)

    bad_artifact = artifacts[1]
    original = real_read_bytes(bad_artifact)
    bad_payload = json.loads(original)
    bad_payload["runs"][0]["setup"]["cleanup_success"] = False
    bad_artifact.write_text(json.dumps(bad_payload), encoding="utf-8")
    with pytest.raises(ValueError, match="does not prove isolated index cleanup"):
        score_graph_ablation.score_artifacts(artifacts, ground_truth)
    assert ground_truth_reads == []
    bad_artifact.write_bytes(original)

    report = score_graph_ablation.score_artifacts(artifacts, ground_truth)

    assert ground_truth_reads == [ground_truth.resolve()]
    assert report["protocol_audit"]["artifact_count"] == 9
    assert report["protocol_audit"]["reused_control_artifacts"] == 3
    assert report["protocol_audit"]["all_artifacts_completed_before_gt_read"] is True
    reused_manifest = [
        item for item in report["protocol_audit"]["artifact_manifest"] if item["reused_control"]
    ]
    assert len(reused_manifest) == 3
    assert {item["agent"] for item in reused_manifest} == {graph_runtime.GRAPH_DEFAULT_AGENT}
    assert {item["source_agent"] for item in reused_manifest} == {
        score_graph_ablation.LEGACY_CONTROL_AGENT
    }
    assert {item["source_protocol_version"] for item in reused_manifest} == {
        score_graph_ablation.LEGACY_CONTROL_PROTOCOL_VERSION
    }
    assert report["summary"]["comparisons"]["codegraph_vs_default"]["treatment_wins"] == 2
    assert report["summary"]["comparisons"]["gitnexus_vs_default"]["treatment_wins"] == 3
    codegraph_summary = report["summary"]["arms"][graph_runtime.CODEGRAPH_AGENT]
    assert codegraph_summary["agent_failures"] == 1
    assert codegraph_summary["completed_submissions"] == 2
    assert report["pairs"][0]["arms"][graph_runtime.CODEGRAPH_AGENT] == {
        "recall": "0/1",
        "hits": [],
        "extras": 0,
        "conversation_ok": False,
        "failure_reason": "submit_schema_invalid",
    }
    reused_rows = [row for row in report["runs"] if row["reused_control"]]
    assert len(reused_rows) == 3
    for row in reused_rows:
        assert row["agent"] == graph_runtime.GRAPH_DEFAULT_AGENT
        assert row["source_agent"] == score_graph_ablation.LEGACY_CONTROL_AGENT
        assert (
            row["source_protocol_version"] == score_graph_ablation.LEGACY_CONTROL_PROTOCOL_VERSION
        )
        assert row["setup"]["provider"] == "none"
        assert row["setup"]["synthetic_from_legacy_control"] is True
        assert row["setup"]["index_seconds"] == 0.0
        assert row["setup"]["cleanup_seconds"] == 0.0
        assert row["timing"]["setup_seconds"] == 0.0
        assert row["timing"]["cleanup_seconds"] == 0.0
    for control_artifact in artifacts[::3]:
        raw_control = json.loads(real_read_bytes(control_artifact))
        assert raw_control["agent"] == score_graph_ablation.LEGACY_CONTROL_AGENT
        assert (
            raw_control["protocol_version"] == score_graph_ablation.LEGACY_CONTROL_PROTOCOL_VERSION
        )
        assert raw_control["runs"][0]["setup"] == {}
        assert "cleanup_seconds" not in raw_control["runs"][0]["timing"]
    for agent in score_graph_ablation.EXPECTED_AGENTS:
        arm = report["summary"]["arms"][agent]
        for field in (
            "setup_seconds",
            "index_seconds",
            "agent_seconds",
            "cleanup_seconds",
            "total_seconds",
        ):
            assert arm[field]["mean"] == phases[agent][field]

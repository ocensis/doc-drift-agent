from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

SINGLE_AGENT_DIR = Path(__file__).resolve().parents[3] / "evals" / "field" / "single_agent"
sys.path.insert(0, str(SINGLE_AGENT_DIR))

import _portfolio_brief as portfolio  # noqa: E402
import _portfolio_native_graph as native  # noqa: E402
import codegraph_explore_change_seed_agent  # noqa: E402
import codegraph_explore_direct_agent  # noqa: E402
import gitnexus_change_impact_agent  # noqa: E402
import gitnexus_change_impact_first_agent  # noqa: E402
import gitnexus_structured_change_first_agent  # noqa: E402
import portfolio_gitnexus_first_control_agent  # noqa: E402
import portfolio_gitnexus_structured_first_control_agent  # noqa: E402
from _runner import (  # noqa: E402
    BASE_TOOLS,
    PORTFOLIO_GITNEXUS_FIRST_SYSTEM_PROMPT,
    PORTFOLIO_GITNEXUS_STRUCTURED_FIRST_SYSTEM_PROMPT,
    PORTFOLIO_NATIVE_SYSTEM_PROMPT,
    TOOL_PORTFOLIO_GITNEXUS_FIRST_PROTOCOL_VERSION,
    TOOL_PORTFOLIO_GITNEXUS_STRUCTURED_FIRST_PROTOCOL_VERSION,
    TOOL_PORTFOLIO_NATIVE_PROTOCOL_VERSION,
    AgentContext,
)


def test_gitnexus_first_agents_share_an_isolated_v4_prompt_and_protocol() -> None:
    control = portfolio_gitnexus_first_control_agent.AGENT
    treatment = gitnexus_change_impact_first_agent.AGENT

    assert control.name == "portfolio_gitnexus_first_control_agent"
    assert treatment.name == "gitnexus_change_impact_first_agent"
    assert control.tools == BASE_TOOLS
    assert treatment.tools == (*BASE_TOOLS, "gitnexus_change_impact")
    assert control.protocol_version == treatment.protocol_version
    assert control.protocol_version == TOOL_PORTFOLIO_GITNEXUS_FIRST_PROTOCOL_VERSION
    assert control.system_prompt == treatment.system_prompt
    assert control.system_prompt == PORTFOLIO_GITNEXUS_FIRST_SYSTEM_PROMPT


def test_structured_first_agents_share_an_isolated_v5_prompt_and_protocol() -> None:
    control = portfolio_gitnexus_structured_first_control_agent.AGENT
    treatment = gitnexus_structured_change_first_agent.AGENT

    assert control.name == "portfolio_gitnexus_structured_first_control_agent"
    assert treatment.name == "gitnexus_structured_change_first_agent"
    assert control.tools == BASE_TOOLS
    assert treatment.tools == (*BASE_TOOLS, "gitnexus_structured_change")
    assert control.protocol_version == treatment.protocol_version
    assert (
        control.protocol_version
        == TOOL_PORTFOLIO_GITNEXUS_STRUCTURED_FIRST_PROTOCOL_VERSION
    )
    assert control.system_prompt == treatment.system_prompt
    assert control.system_prompt == PORTFOLIO_GITNEXUS_STRUCTURED_FIRST_SYSTEM_PROMPT
    assert "not benchmark ground truth" in control.system_prompt


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
    source = repo / "src" / "service.py"
    source.write_text("def Alpha():\n    return 'old'\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "native-graph-test@example.com")
    _git(repo, "config", "user.name", "Native Graph Test")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "baseline")
    baseline = _git(repo, "rev-parse", "HEAD")
    source.write_text(
        "def Alpha():\n    return 'head'\n\n\ndef Beta():\n    return Alpha()\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "head")
    head = _git(repo, "rev-parse", "HEAD")
    return repo, AgentContext(
        repo_path=repo,
        baseline_revision=baseline,
        head_revision=head,
    )


def _completed(
    argv: list[str],
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)


def test_native_agents_have_independent_v3_schemas() -> None:
    codegraph = codegraph_explore_direct_agent.AGENT
    codegraph_change_seed = codegraph_explore_change_seed_agent.AGENT
    gitnexus = gitnexus_change_impact_agent.AGENT

    assert codegraph.name == native.CODEGRAPH_EXPLORE_DIRECT_AGENT
    assert gitnexus.name == native.GITNEXUS_CHANGE_IMPACT_AGENT
    assert codegraph.tools == (*BASE_TOOLS, "codegraph_explore")
    assert codegraph_change_seed.tools == (
        *codegraph.tools,
        portfolio.AUDIT_BRIEF_TOOL,
    )
    assert gitnexus.tools == (*BASE_TOOLS, "gitnexus_change_impact")
    assert codegraph.protocol_version == codegraph_change_seed.protocol_version
    assert codegraph.protocol_version == gitnexus.protocol_version
    assert codegraph.protocol_version == TOOL_PORTFOLIO_NATIVE_PROTOCOL_VERSION
    assert codegraph.system_prompt == codegraph_change_seed.system_prompt
    assert codegraph.system_prompt == gitnexus.system_prompt == PORTFOLIO_NATIVE_SYSTEM_PROMPT
    assert len(PORTFOLIO_NATIVE_SYSTEM_PROMPT) == 2532
    assert hashlib.sha256(PORTFOLIO_NATIVE_SYSTEM_PROMPT.encode()).hexdigest() == (
        "c9e116004a0a08331245374048116b54c240a44c3d481b0b6f2c1040adfdf53d"
    )

    codegraph_parameters = native.CODEGRAPH_EXPLORE_DIRECT_DEFINITION["function"][
        "parameters"
    ]
    assert codegraph_parameters["required"] == ["query"]
    assert codegraph_parameters["properties"]["max_files"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 20,
        "description": (
            "Optional maximum source-bearing files (1-20). Omit it to preserve "
            "CodeGraph's project-size-adaptive native default."
        ),
    }
    gitnexus_parameters = native.GITNEXUS_CHANGE_IMPACT_DEFINITION["function"][
        "parameters"
    ]
    assert gitnexus_parameters == {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }


def test_codegraph_native_runtime_returns_complete_sanitized_explore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    visible, context = _visible_repo(tmp_path)
    binary = tmp_path / "bin" / "codegraph"
    binary.parent.mkdir()
    binary.write_text("fake pinned binary\n", encoding="utf-8")
    calls: list[dict[str, Any]] = []
    graph_repo: Path | None = None
    large_tail = "x" * 75_000

    def fake_run(
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal graph_repo
        calls.append({"argv": list(argv), "cwd": Path(cwd), "env": dict(env or {})})
        operation = argv[1]
        if operation == "init":
            graph_repo = Path(cwd)
            index = graph_repo / ".codegraph"
            index.mkdir()
            (index / "codegraph.db").write_bytes(b"index")
            return _completed(argv, stdout=f"initialized {graph_repo}\n")
        if operation == "status":
            return _completed(argv, stdout=json.dumps({"nodeCount": 7, "path": str(cwd)}))
        if operation == "explore":
            assert graph_repo is not None
            return _completed(
                argv,
                stdout=(
                    f"Exploration from {graph_repo}\n"
                    "**Source Code**\n"
                    "**`src/service.py`**\n"
                    "1\tdef Alpha():\n"
                    "Blast Radius\n"
                    f"{large_tail}\nEND\n"
                ),
            )
        raise AssertionError(argv)

    monkeypatch.setattr(native, "_run", fake_run)
    monkeypatch.setattr(native, "_binary", lambda *_args: (binary, "c" * 64))

    runtime = native.codegraph_explore_direct_runtime(context)
    explore = runtime.extra_tools["codegraph_explore"][1]
    output = explore({"query": "  Alpha callers  "})

    assert output == (
        "Exploration from .\n"
        "**Source Code**\n"
        "**`src/service.py`**\n"
        "1\tdef Alpha():\n"
        "Blast Radius\n"
        f"{large_tail}\nEND"
    )
    assert str(graph_repo) not in output
    assert "[truncated" not in output
    assert calls[-1]["argv"] == [
        str(binary),
        "explore",
        "--path",
        str(graph_repo),
        "Alpha callers",
    ]
    assert calls[-1]["env"]["DO_NOT_TRACK"] == "1"
    assert calls[-1]["env"]["CODEGRAPH_NO_DOWNLOAD"] == "1"
    assert not (visible / ".codegraph").exists()

    digest = hashlib.sha256(output.encode("utf-8")).hexdigest()
    assert len(runtime.metadata["provider_calls"]) == 1
    assert len(runtime.metadata["query_calls"]) == 1
    for record in (runtime.metadata["provider_calls"][0], runtime.metadata["query_calls"][0]):
        assert record["output_chars"] == len(output)
        assert record["output_sha256"] == digest
        assert record["contains_source_code"] is True
        assert record["contains_blast_radius"] is True
        assert record["contains_trim_notice"] is False
    assert runtime.metadata["query_calls"][0]["arguments"] == {"query": "  Alpha callers  "}
    assert runtime.metadata["query_calls"][0]["provider_arguments"] == {
        "query": "Alpha callers",
    }
    assert runtime.metadata["upstream_default_max_files"] == "adaptive_by_project_size"
    assert runtime.metadata["output_transport"] == {
        "complete_provider_output": True,
        "pagination": False,
        "wrapper_truncation": False,
        "provider_internal_truncation_possible": True,
        "projection": False,
        "sanitization": "isolated_clone_path_only",
    }
    assert runtime.metadata["index_stats"] == {"nodeCount": 7, "path": "."}

    for invalid in (
        {"query": ""},
        {"query": "Alpha", "max_files": False},
        {"query": "Alpha", "max_files": 0},
        {"query": "Alpha", "max_files": 21},
    ):
        with pytest.raises(ValueError):
            explore(invalid)
    assert len([call for call in calls if call["argv"][1] == "explore"]) == 1

    assert runtime.close is not None
    graph_root = graph_repo.parent if graph_repo is not None else None
    runtime.close()
    assert graph_root is not None and not graph_root.exists()
    assert runtime.metadata["cleanup_success"] is True


def test_codegraph_change_seed_reuses_one_runtime_clone_and_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _visible, context = _visible_repo(tmp_path)
    binary = tmp_path / "bin" / "codegraph"
    binary.parent.mkdir()
    binary.write_text("fake pinned binary\n", encoding="utf-8")
    graph_repo: Path | None = None
    generic_runtime_calls = 0
    clone_calls = 0
    cleanup_factory_calls = 0
    cleanup_calls = 0
    seed_build_calls = 0

    original_generic_runtime = native.paged_generic_runtime
    original_clone = native._clone_for_index
    original_cleanup = native._cleanup_callback

    def counted_generic_runtime(inner_context: AgentContext) -> Any:
        nonlocal generic_runtime_calls
        generic_runtime_calls += 1
        return original_generic_runtime(inner_context)

    def counted_clone(inner_context: AgentContext, provider: str) -> tuple[Path, float]:
        nonlocal clone_calls
        clone_calls += 1
        return original_clone(inner_context, provider)

    def counted_cleanup(
        inner_graph_repo: Path,
        metadata: dict[str, Any],
        provider: str,
    ) -> Any:
        nonlocal cleanup_factory_calls
        cleanup_factory_calls += 1
        original_callback = original_cleanup(inner_graph_repo, metadata, provider)

        def close() -> None:
            nonlocal cleanup_calls
            cleanup_calls += 1
            original_callback()

        return close

    def fake_run(
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del env
        nonlocal graph_repo
        operation = argv[1]
        if operation == "init":
            graph_repo = Path(cwd)
            index = graph_repo / ".codegraph"
            index.mkdir()
            (index / "codegraph.db").write_bytes(b"index")
            return _completed(argv, stdout="initialized\n")
        if operation == "status":
            return _completed(argv, stdout=json.dumps({"nodeCount": 3}))
        if operation == "explore":
            return _completed(
                argv,
                stdout=(
                    "**Source Code**\n"
                    "**`src/service.py`**\n"
                    "1\tdef Alpha():\n"
                    "Blast Radius\n"
                    "Beta calls Alpha\n"
                ),
            )
        raise AssertionError(argv)

    def counted_seed(_context: AgentContext) -> Any:
        nonlocal seed_build_calls
        seed_build_calls += 1
        return portfolio._ComponentResult(
            text="existing change-seed renderer input",
            metadata={"source": "unit-test"},
        )

    monkeypatch.setattr(native, "paged_generic_runtime", counted_generic_runtime)
    monkeypatch.setattr(native, "_clone_for_index", counted_clone)
    monkeypatch.setattr(native, "_cleanup_callback", counted_cleanup)
    monkeypatch.setattr(native, "_run", fake_run)
    monkeypatch.setattr(native, "_binary", lambda *_args: (binary, "d" * 64))
    monkeypatch.setitem(portfolio._BUILDERS, "change_seed", counted_seed)

    runtime = codegraph_explore_change_seed_agent.AGENT.prepare(context)
    graph_output = runtime.extra_tools["codegraph_explore"][1](
        {"query": "Alpha callers"}
    )
    first_brief = runtime.extra_tools[portfolio.AUDIT_BRIEF_TOOL][1]({})
    second_brief = runtime.extra_tools[portfolio.AUDIT_BRIEF_TOOL][1]({})

    assert generic_runtime_calls == clone_calls == cleanup_factory_calls == 1
    assert seed_build_calls == 1
    assert "Blast Radius" in graph_output
    assert first_brief == second_brief
    assert "Profile: change_seed" in first_brief
    assert "# Component: change_seed" in first_brief
    assert runtime.metadata["profile_id"] == "codegraph_native_change_seed"
    assert runtime.metadata["graph_profile_id"] == "codegraph_native"
    assert runtime.metadata["base_profile_id"] == "paged_generic"
    assert runtime.metadata["tool_surface"] == [
        "codegraph_explore",
        "audit_brief",
    ]
    assert runtime.metadata["incremental_tool_surface"] == ["audit_brief"]
    assert runtime.metadata["runtime_composition"] == {
        "shared_toolbox": True,
        "shared_generic_runtime": True,
        "shared_codegraph_clone": True,
        "cleanup_callback_reused": True,
    }
    assert runtime.metadata["dependencies"][:3] == [
        "git",
        "filesystem",
        "codegraph:1.5.0:" + "d" * 64,
    ]
    assert runtime.metadata["graph_dependencies"] == runtime.metadata["dependencies"][:3]
    assert runtime.metadata["dependency_sha256"] == portfolio._sha256_json(
        runtime.metadata["dependencies"]
    )
    assert runtime.metadata["brief_component_dependencies"] == list(
        portfolio.CHANGE_SEED_PROFILE.dependencies
    )
    assert runtime.metadata["audit_brief_calls"] == 2
    assert runtime.metadata["audit_brief_cache_hits"] == 1

    assert runtime.close is not None
    graph_root = graph_repo.parent if graph_repo is not None else None
    runtime.close()
    assert cleanup_calls == 1
    assert graph_root is not None and not graph_root.exists()


def test_codegraph_change_seed_closes_parent_if_attachment_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _visible, context = _visible_repo(tmp_path)
    close_calls = 0

    def close() -> None:
        nonlocal close_calls
        close_calls += 1

    runtime = SimpleNamespace(close=close)
    monkeypatch.setattr(
        codegraph_explore_change_seed_agent,
        "codegraph_explore_direct_runtime",
        lambda _context: runtime,
    )
    monkeypatch.setattr(
        codegraph_explore_change_seed_agent,
        "attach_audit_brief",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("attach failed")),
    )

    with pytest.raises(RuntimeError, match="attach failed"):
        codegraph_explore_change_seed_agent.codegraph_explore_change_seed_runtime(context)
    assert close_calls == 1


def test_gitnexus_native_runtime_fixes_compare_baseline_and_returns_complete_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    visible, context = _visible_repo(tmp_path)
    binary = tmp_path / "bin" / "gitnexus"
    binary.parent.mkdir()
    binary.write_text("fake pinned binary\n", encoding="utf-8")
    calls: list[dict[str, Any]] = []
    graph_repo: Path | None = None
    large_tail = "y" * 80_000

    def fake_run(
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal graph_repo
        calls.append({"argv": list(argv), "cwd": Path(cwd), "env": dict(env or {})})
        operation = argv[1]
        if operation == "analyze":
            graph_repo = Path(cwd)
            index = graph_repo / ".gitnexus"
            index.mkdir()
            (index / "graph.db").write_bytes(b"index")
            (index / "meta.json").write_text(
                json.dumps(
                    {
                        "lastCommit": context.head_revision,
                        "stats": {
                            "files": 1,
                            "nodes": 2,
                            "edges": 1,
                            "embeddings": 0,
                        },
                        "capabilities": {
                            "graph": {"status": "available"},
                            "fts": {"status": "available"},
                        },
                        "schemaVersion": 5,
                    }
                ),
                encoding="utf-8",
            )
            return _completed(argv, stdout=f"analyzed {graph_repo}\n")
        if operation == "detect-changes":
            assert graph_repo is not None
            return _completed(
                argv,
                stdout=(
                    f"Changes in {graph_repo}\n"
                    "Changes: 1 files, 2 symbols\n"
                    "Changed symbols:\n"
                    "  Function Alpha → src/service.py\n"
                    "Affected execution flows:\n"
                    "  • request (2 steps) — changed: Alpha\n"
                    f"{large_tail}\nEND\n"
                ),
            )
        raise AssertionError(argv)

    monkeypatch.setattr(native, "_run", fake_run)
    monkeypatch.setattr(native, "_binary", lambda *_args: (binary, "g" * 64))

    runtime = native.gitnexus_change_impact_runtime(context)
    change_impact = runtime.extra_tools["gitnexus_change_impact"][1]
    output = change_impact({})

    assert output == (
        "Changes in .\n"
        "Changes: 1 files, 2 symbols\n"
        "Changed symbols:\n"
        "  Function Alpha → src/service.py\n"
        "Affected execution flows:\n"
        "  • request (2 steps) — changed: Alpha\n"
        f"{large_tail}\nEND"
    )
    assert str(graph_repo) not in output
    assert calls[-1]["argv"] == [
        str(binary),
        "detect-changes",
        "--scope",
        "compare",
        "--base-ref",
        context.baseline_revision,
        "--limit",
        "500",
    ]
    expected_home = str(graph_repo.parent / "home") if graph_repo is not None else ""
    assert calls[-1]["env"]["GITNEXUS_HOME"] == expected_home
    assert calls[-1]["env"]["GITNEXUS_LBUG_EXTENSION_INSTALL"] == "load-only"
    assert not (visible / ".gitnexus").exists()

    digest = hashlib.sha256(output.encode("utf-8")).hexdigest()
    fixed = {"scope": "compare", "base_ref": context.baseline_revision, "limit": 500}
    assert runtime.metadata["fixed_provider_arguments"] == fixed
    for record in (runtime.metadata["provider_calls"][0], runtime.metadata["query_calls"][0]):
        assert record["output_chars"] == len(output)
        assert record["output_sha256"] == digest
        assert record["contains_changed_symbols"] is True
        assert record["contains_affected_execution_flows"] is True
        assert record["scope"] == "compare"
        assert record["base_ref"] == context.baseline_revision
        assert record["limit"] == 500
    assert runtime.metadata["provider_calls"][0]["arguments"] == fixed
    assert runtime.metadata["provider_calls"][0]["operation"] == "detect_changes"
    assert runtime.metadata["query_calls"][0]["arguments"] == {}
    assert runtime.metadata["query_calls"][0]["provider_arguments"] == fixed

    with pytest.raises(ValueError, match="takes no arguments"):
        change_impact({"base_ref": "attacker-controlled"})
    assert len([call for call in calls if call["argv"][1] == "detect-changes"]) == 1

    assert runtime.close is not None
    graph_root = graph_repo.parent if graph_repo is not None else None
    runtime.close()
    assert graph_root is not None and not graph_root.exists()
    assert runtime.metadata["cleanup_success"] is True


def test_native_output_markers_are_mechanical() -> None:
    assert native._codegraph_markers("Source Code\nBlast Radius\n... 4 files omitted") == {
        "contains_source_code": True,
        "contains_blast_radius": True,
        "contains_trim_notice": True,
    }
    assert native._gitnexus_change_markers("No changes detected.") == {
        "contains_changed_symbols": False,
        "contains_affected_execution_flows": False,
    }

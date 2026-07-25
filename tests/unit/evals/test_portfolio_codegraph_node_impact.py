from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

SINGLE_AGENT_DIR = Path(__file__).resolve().parents[3] / "evals" / "field" / "single_agent"
sys.path.insert(0, str(SINGLE_AGENT_DIR))

import _portfolio_codegraph_node_impact as candidate  # noqa: E402
import _portfolio_native_graph as native  # noqa: E402
import codegraph_node_impact_agent  # noqa: E402
from _runner import (  # noqa: E402
    BASE_TOOLS,
    PORTFOLIO_NATIVE_SYSTEM_PROMPT,
    TOOL_PORTFOLIO_NATIVE_PROTOCOL_VERSION,
    AgentContext,
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
    source = repo / "src" / "service.py"
    source.write_text("def Alpha():\n    return 'old'\n", encoding="utf-8")
    (repo / "README.md").write_text("baseline docs\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "node-impact-test@example.com")
    _git(repo, "config", "user.name", "Node Impact Test")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "baseline")
    baseline = _git(repo, "rev-parse", "HEAD")
    source.write_text(
        "def Alpha():\n    return 'head'\n\n\ndef Beta():\n    return Alpha()\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text("OnlyDocs changed here\n", encoding="utf-8")
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


def _status_payload(graph_repo: Path) -> dict[str, Any]:
    return {
        "initialized": True,
        "version": candidate.CODEGRAPH_VERSION,
        "projectPath": str(graph_repo),
        "indexPath": str(graph_repo / ".codegraph"),
        "fileCount": 1,
        "nodeCount": 2,
        "edgeCount": 1,
        "pendingChanges": {"added": 0, "modified": 0, "removed": 0},
        "worktreeMismatch": None,
        "index": {
            "builtWithVersion": candidate.CODEGRAPH_VERSION,
            "state": "complete",
            "reindexRecommended": False,
            "pendingRefs": 0,
        },
    }


def test_candidate_agent_is_v3_prompt_compatible_but_not_registered_here() -> None:
    agent = codegraph_node_impact_agent.AGENT

    assert agent.name == candidate.CODEGRAPH_NODE_IMPACT_AGENT
    assert agent.tools == (*BASE_TOOLS, candidate.CODEGRAPH_NODE_IMPACT_TOOL)
    assert agent.protocol_version == TOOL_PORTFOLIO_NATIVE_PROTOCOL_VERSION
    assert agent.system_prompt == PORTFOLIO_NATIVE_SYSTEM_PROMPT
    assert codegraph_node_impact_agent.PROMPT_COMPATIBILITY == (
        "single-agent-tool-portfolio-v3-native-graph"
    )
    assert codegraph_node_impact_agent.PROTOCOL_INTEGRATION_STATUS == "pending-root-agent"

    function = candidate.CODEGRAPH_NODE_IMPACT_DEFINITION["function"]
    assert function["name"] == candidate.CODEGRAPH_NODE_IMPACT_TOOL
    assert function["parameters"] == {
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "pattern": "^[A-Za-z_$][A-Za-z0-9_$]*$",
                "description": (
                    "One exact identifier copied from a changed source line in git_diff; "
                    "natural-language questions and symbol lists are rejected."
                ),
            },
            "file": {
                "type": "string",
                "description": (
                    "Optional changed repo-relative source path containing that symbol. "
                    "Pins node source only; upstream impact stays exact-name aggregate."
                ),
            },
        },
        "required": ["symbol"],
        "additionalProperties": False,
    }
    assert "ordered call path" in function["description"]
    assert "no --file option" in function["description"]


def test_runtime_runs_complete_native_streams_in_parallel_and_reuses_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    visible, context = _visible_repo(tmp_path)
    binary = tmp_path / "bin" / "codegraph"
    binary.parent.mkdir()
    binary.write_text("fake pinned binary\n", encoding="utf-8")

    calls: list[dict[str, Any]] = []
    calls_lock = threading.Lock()
    provider_barrier = threading.Barrier(2)
    mode = {"value": "success"}
    graph_repo: Path | None = None
    node_tail = "n" * 24_000
    impact_tail = "i" * 21_000

    clone_calls = 0
    generic_runtime_calls = 0
    cleanup_factory_calls = 0
    cleanup_calls = 0
    original_clone = candidate._clone_for_index
    original_generic = native.paged_generic_runtime
    original_cleanup = native._cleanup_callback

    def counted_clone(inner_context: AgentContext, provider: str) -> tuple[Path, float]:
        nonlocal clone_calls
        clone_calls += 1
        return original_clone(inner_context, provider)

    def counted_generic(inner_context: AgentContext) -> Any:
        nonlocal generic_runtime_calls
        generic_runtime_calls += 1
        return original_generic(inner_context)

    def counted_cleanup(
        inner_graph_repo: Path,
        metadata: dict[str, Any],
        provider: str,
    ) -> Any:
        nonlocal cleanup_factory_calls
        cleanup_factory_calls += 1
        callback = original_cleanup(inner_graph_repo, metadata, provider)

        def close() -> None:
            nonlocal cleanup_calls
            cleanup_calls += 1
            callback()

        return close

    def fake_run(
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal graph_repo
        if argv[0] == "git":
            return subprocess.run(
                argv,
                cwd=cwd,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
        operation = argv[1]
        with calls_lock:
            calls.append({"argv": list(argv), "cwd": Path(cwd), "env": dict(env or {})})
        if operation == "init":
            graph_repo = Path(cwd)
            index = graph_repo / ".codegraph"
            index.mkdir()
            (index / "codegraph.db").write_bytes(b"index")
            return _completed(argv, stdout=f"initialized {graph_repo}\n")
        if operation == "status":
            assert graph_repo is not None
            return _completed(argv, stdout=json.dumps(_status_payload(graph_repo)))
        if operation in {"node", "impact"}:
            provider_barrier.wait(timeout=2)
            if mode["value"] == "error":
                if operation == "node":
                    return _completed(
                        argv,
                        returncode=7,
                        stdout=f"partial node output from {cwd}\n",
                        stderr=f"node failed in {cwd}\n",
                    )
                return _completed(
                    argv,
                    stdout=(
                        f"impact from {cwd}\n"
                        "... (output truncated to budget; provider notice preserved)\n"
                    ),
                )
            if operation == "node":
                return _completed(
                    argv,
                    stdout=f"NODE {cwd}\n5\tdef Beta():\n{node_tail}\nEND NODE\n",
                )
            return _completed(
                argv,
                stdout=f"IMPACT {cwd}\nBeta -> Alpha\n{impact_tail}\nEND IMPACT\n",
            )
        raise AssertionError(argv)

    monkeypatch.setattr(candidate, "_clone_for_index", counted_clone)
    monkeypatch.setattr(native, "paged_generic_runtime", counted_generic)
    monkeypatch.setattr(native, "_cleanup_callback", counted_cleanup)
    monkeypatch.setattr(candidate, "_run", fake_run)
    monkeypatch.setattr(candidate, "_binary", lambda *_args: (binary, "b" * 64))

    runtime = candidate.codegraph_node_impact_runtime(context)
    handler = runtime.extra_tools[candidate.CODEGRAPH_NODE_IMPACT_TOOL][1]
    output = handler({"symbol": "Beta", "file": "src/service.py"})
    payload = json.loads(output)

    assert clone_calls == generic_runtime_calls == cleanup_factory_calls == 1
    assert not (visible / ".codegraph").exists()
    assert payload["protocol"] == candidate.CODEGRAPH_NODE_IMPACT_PROTOCOL
    assert payload["query"] == {"symbol": "Beta", "file": "src/service.py"}
    assert payload["semantics"] == {
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
    }
    node_result = payload["results"]["node_include_source"]
    impact_result = payload["results"]["upstream_impact_depth3"]
    assert node_result["stdout"] == f"NODE .\n5\tdef Beta():\n{node_tail}\nEND NODE\n"
    assert node_result["stderr"] == ""
    assert impact_result["stdout"] == (
        f"IMPACT .\nBeta -> Alpha\n{impact_tail}\nEND IMPACT\n"
    )
    assert str(graph_repo) not in output
    assert payload["transport"] == {
        "complete_sanitized_stdout_stderr": True,
        "provider_truncation_notices_preserved": True,
        "sanitization": "isolated_clone_path_only",
        "wrapper_truncation": False,
    }

    provider_calls = runtime.metadata["provider_calls"]
    assert [call["operation"] for call in provider_calls] == [
        "node_include_source",
        "impact_upstream_depth3",
    ]
    assert provider_calls[0]["argv_semantic_args"] == [
        "node",
        "--path",
        ".",
        "--file",
        "src/service.py",
        "Beta",
    ]
    assert provider_calls[0]["semantic_arguments"] == {
        "symbol": "Beta",
        "include_source": True,
        "relationship_scope": "immediate_callers_and_callees",
        "file": "src/service.py",
    }
    assert provider_calls[1]["argv_semantic_args"] == [
        "impact",
        "--path",
        ".",
        "--depth",
        "3",
        "Beta",
    ]
    assert provider_calls[1]["semantic_arguments"] == {
        "symbol": "Beta",
        "depth": 3,
        "direction": "upstream_dependents",
        "definition_scope": "all_exact_same_named_definitions",
        "file_disambiguation_applied": False,
    }
    for call in provider_calls:
        assert call["seconds"] >= 0
        assert call["output_chars"] > 20_000
        assert len(call["output_sha256"]) == 64
        assert call["error"] is False
        assert call["package_version"] == "1.5.0"
        assert call["index_binding_sha256"] == runtime.metadata["index_binding_sha256"]
        assert call["complete_sanitized_streams"] is True
        assert call["wrapper_truncation"] is False
        assert call["provider_reported_truncation"] is False

    query = runtime.metadata["query_calls"][0]
    assert query["execution_mode"] == "parallel_native_cli_subprocesses"
    assert query["provider_call_count"] == 2
    assert query["ordered_path_available"] is False
    assert query["matching_diff_paths"] == ["src/service.py"]
    assert query["output_chars"] == len(output)
    assert query["output_sha256"] == hashlib.sha256(output.encode()).hexdigest()
    assert query["combined_seconds"] >= 0
    assert query["parallel_overlap_seconds"] >= 0
    assert query["error"] is False
    assert runtime.metadata["index_binding"] == {
        "provider": "codegraph",
        "package_version": "1.5.0",
        "binary_sha256": "b" * 64,
        "source_head": context.head_revision,
        "source_tree": _git(visible, "rev-parse", "HEAD^{tree}"),
        "isolated_clone_head_matches_source": True,
        "isolated_clone_tree_matches_source": True,
        "index_relative_path": ".codegraph",
        "status_initialized": True,
        "status_version": "1.5.0",
        "index_state": "complete",
        "index_built_with_version": "1.5.0",
        "pending_changes": {"added": 0, "modified": 0, "removed": 0},
        "pending_refs": 0,
        "worktree_mismatch": None,
    }
    assert runtime.metadata["runtime_composition"] == {
        "isolated_clone_count": 1,
        "codegraph_index_count": 1,
        "generic_runtime_count": 1,
        "cleanup_callback_count": 1,
        "provider_queries_share_index": True,
    }
    assert runtime.metadata["output_transport"]["wrapper_truncation"] is False
    assert runtime.metadata["output_transport"]["projection"] is False

    provider_call_count = len(provider_calls)
    for invalid in (
        {"symbol": "Beta callers"},
        {"symbol": "beta"},
        {"symbol": "OnlyDocs"},
        {"symbol": "Beta", "file": "README.md"},
        {"symbol": "Beta", "unknown": True},
    ):
        with pytest.raises(ValueError):
            handler(invalid)
    assert len(runtime.metadata["provider_calls"]) == provider_call_count

    mode["value"] = "error"
    error_output = handler({"symbol": "Beta"})
    error_payload = json.loads(error_output)
    error_node = error_payload["results"]["node_include_source"]
    error_impact = error_payload["results"]["upstream_impact_depth3"]
    assert error_node == {
        "stdout": "partial node output from .\n",
        "stderr": "node failed in .\n",
        "exit_code": 7,
        "error": True,
        "provider_reported_truncation": False,
    }
    assert error_impact["error"] is False
    assert error_impact["provider_reported_truncation"] is True
    assert "output truncated to budget" in error_impact["stdout"]
    second_provider_calls = runtime.metadata["provider_calls"][2:]
    assert second_provider_calls[0]["error"] is True
    assert second_provider_calls[0]["stderr_chars"] == len("node failed in .\n")
    assert second_provider_calls[1]["provider_reported_truncation"] is True
    assert runtime.metadata["query_calls"][1]["error"] is True
    assert runtime.metadata["query_calls"][1]["provider_reported_truncation"] is True
    assert runtime.metadata["query_calls"][1]["wrapper_truncation"] is False

    node_call = next(call for call in calls if call["argv"][1] == "node")
    impact_call = next(call for call in calls if call["argv"][1] == "impact")
    assert node_call["argv"] == [
        str(binary),
        "node",
        "--path",
        str(graph_repo),
        "--file",
        "src/service.py",
        "Beta",
    ]
    assert impact_call["argv"] == [
        str(binary),
        "impact",
        "--path",
        str(graph_repo),
        "--depth",
        "3",
        "Beta",
    ]
    assert node_call["env"]["DO_NOT_TRACK"] == "1"
    assert impact_call["env"]["CODEGRAPH_NO_DOWNLOAD"] == "1"

    assert runtime.close is not None
    graph_root = graph_repo.parent if graph_repo is not None else None
    runtime.close()
    assert cleanup_calls == 1
    assert graph_root is not None and not graph_root.exists()
    assert runtime.metadata["cleanup_success"] is True

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

SINGLE_AGENT_DIR = Path(__file__).resolve().parents[3] / "evals" / "field" / "single_agent"
sys.path.insert(0, str(SINGLE_AGENT_DIR))

import _portfolio_gitnexus_exact_composite as composite  # noqa: E402
import gitnexus_exact_composite_agent  # noqa: E402
from _runner import BASE_TOOLS, AgentContext  # noqa: E402


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
    _git(repo, "config", "user.email", "exact-composite-test@example.com")
    _git(repo, "config", "user.name", "Exact Composite Test")
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


def _fake_package(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "runtime" / "node_modules" / "gitnexus"
    binary = root / "dist" / "cli" / "index.js"
    backend = root / "dist" / "mcp" / "local" / "local-backend.js"
    resources = root / "dist" / "mcp" / "resources.js"
    binary.parent.mkdir(parents=True)
    backend.parent.mkdir(parents=True)
    binary.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    backend.write_text(
        "\n".join(
            (
                "export class LocalBackend {}",
                "// case 'detect_changes':",
                "// async detectChanges(repo, params)",
                "// changed_symbols: changedSymbols",
                "// affected_processes: Array.from(affectedProcesses.values())",
            )
        ),
        encoding="utf-8",
    )
    resources.write_text(
        "export async function readResource() {}\n// getProcessDetailResource\n",
        encoding="utf-8",
    )
    (root / "package.json").write_text(
        json.dumps(
            {
                "name": "gitnexus",
                "version": "1.6.9",
                "type": "module",
                "bin": {"gitnexus": "dist/cli/index.js"},
            }
        ),
        encoding="utf-8",
    )
    return binary, backend, resources


def _completed(
    argv: list[str],
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)


def _provider_call(index: int, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "call_index": index,
        "operation": operation,
        "arguments": arguments,
        "runtime_bindings": {"repo": "isolated_index_clone"},
        "seconds": 0.01 * index,
        "output_chars": 100 * index,
        "output_sha256": f"{index:x}" * 64,
        "bridge_exception": False,
        "error": False,
        "partial": False,
        "partial_field_present": False,
        "partial_value_valid": True,
        "pagination_field_present": False,
        "pagination": None,
        "status": "found" if operation == "context" else None,
        "ambiguity_candidates": 0,
    }


def _bridge_payload(baseline: str) -> dict[str, Any]:
    selected = {
        "uid": "Function:src/agent/react-executor.ts:runSpecialistLoop",
        "name": "runSpecialistLoop",
        "kind": "Function",
        "filePath": "src/agent/react-executor.ts",
        "score": {
            "cross_community_processes": 2,
            "total_processes": 3,
            "changed_step_occurrences": 3,
            "kind_priority": 0,
        },
    }
    detect = {
        "summary": {
            "changed_count": 1,
            "affected_count": 1,
            "changed_files": 1,
            "risk_level": "medium",
        },
        "changed_symbols": [
            {
                "id": selected["uid"],
                "name": selected["name"],
                "filePath": selected["filePath"],
                "change_type": "touched",
            }
        ],
        "affected_processes": [
            {
                "id": "proc-1",
                "name": "RunSpecialistLoop → GetPermissions",
                "process_type": "cross_community",
                "step_count": 4,
                "changed_steps": [{"symbol": selected["name"], "step": 1}],
            }
        ],
    }
    context_arguments = {
        "include_content": False,
        "name": selected["name"],
        "uid": selected["uid"],
    }
    impact_arguments = {
        "direction": "upstream",
        "includeTests": False,
        "limit": 8,
        "maxDepth": 2,
        "mode": "callgraph",
        "offset": 0,
        "summaryOnly": False,
        "target": selected["name"],
        "target_uid": selected["uid"],
    }
    trace_arguments = {
        "from": "createAgentGraph",
        "from_uid": "Function:src/agent/graph.ts:createAgentGraph",
        "includeTests": False,
        "maxDepth": 3,
        "to": selected["name"],
        "to_uid": selected["uid"],
    }
    calls = [
        _provider_call(1, "detect_changes", {"base_ref": baseline, "scope": "compare"}),
        _provider_call(2, "context", context_arguments),
        _provider_call(3, "impact", impact_arguments),
        _provider_call(4, "trace", trace_arguments),
        _provider_call(
            5,
            "process_resource",
            {"process_name": "RunSpecialistLoop → GetPermissions"},
        ),
    ]
    return {
        "protocol_version": "gitnexus-official-structured-k1-exact-composite-v1",
        "normalization": "recursive_object_key_sort_arrays_preserved",
        "detect_changes": detect,
        "selection": {
            "policy_version": "k1-cross-community-unique-exact-uid-v1",
            "max_selected": 1,
            "integrity": {
                "clean": True,
                "error": False,
                "partial": False,
                "partial_field_present": False,
                "partial_value_valid": True,
                "changed_symbols_count": 1,
                "affected_processes_count": 1,
                "summary_counts_match_arrays": True,
            },
            "eligible_count": 1,
            "rejection_counts": {},
            "status": "selected",
            "reason": "highest_ranked_eligible_exact_uid",
            "selected": selected,
        },
        "enrichment": {
            "context": {
                "performed": True,
                "arguments": context_arguments,
                "result": {
                    "status": "found",
                    "symbol": {"uid": selected["uid"], "name": selected["name"]},
                },
            },
            "impact": {
                "performed": True,
                "arguments": impact_arguments,
                "result": {
                    "target": {"id": selected["uid"], "name": selected["name"]},
                    "byDepth": {
                        "1": [{"id": "Function:src/agent/nodes.ts:createSpecialistNode"}],
                        "2": [{"id": "Function:src/agent/graph.ts:createAgentGraph"}],
                    },
                },
            },
            "trace": {
                "performed": True,
                "reason": "single_contiguous_unpaginated_upstream_chain",
                "arguments": trace_arguments,
                "result": {"status": "ok", "hopCount": 2},
            },
            "process": {
                "performed": True,
                "reason": "highest_ranked_cross_community_process_for_selected_symbol",
                "selected_process": detect["affected_processes"][0],
                "resource": "gitnexus://repo/{runtime_repo}/process/{selected_process}",
                "content": "trace:\n  1: runSpecialistLoop (src/agent/react-executor.ts)",
            },
        },
        "provider_calls": calls,
    }


def test_candidate_agent_has_independent_names_and_protocol() -> None:
    agent = gitnexus_exact_composite_agent.AGENT

    assert agent.name == "gitnexus_exact_composite_agent"
    assert agent.tools == (*BASE_TOOLS, "gitnexus_exact_composite")
    assert agent.protocol_version == (
        "single-agent-tool-portfolio-candidate-gitnexus-k1-exact-composite-v1"
    )
    assert "candidate protocol" in agent.system_prompt
    assert composite.GITNEXUS_EXACT_COMPOSITE_PROFILE_ID == (
        "gitnexus_official_structured_k1_exact_composite"
    )
    parameters = composite.GITNEXUS_EXACT_COMPOSITE_DEFINITION["function"][
        "parameters"
    ]
    assert parameters == {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }


def test_runtime_records_individual_provider_calls_and_hides_bridge_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    visible, context = _visible_repo(tmp_path)
    binary, backend, resources = _fake_package(tmp_path)
    fake_node = tmp_path / "bin" / "node"
    fake_node.parent.mkdir()
    fake_node.write_text("fake node\n", encoding="utf-8")
    graph_repo: Path | None = None
    calls: list[dict[str, Any]] = []
    payload = _bridge_payload(context.baseline_revision)

    def fake_run(
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal graph_repo
        calls.append({"argv": list(argv), "cwd": Path(cwd), "env": dict(env or {})})
        if argv[0] == str(binary) and argv[1] == "analyze":
            graph_repo = Path(cwd)
            index = graph_repo / ".gitnexus"
            index.mkdir()
            (index / "lbug").write_bytes(b"index")
            (index / "meta.json").write_text(
                json.dumps(
                    {
                        "lastCommit": context.head_revision,
                        "stats": {
                            "files": 1,
                            "nodes": 5,
                            "edges": 4,
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
        if argv[0] == str(fake_node):
            return _completed(argv, stdout=json.dumps(payload, indent=2) + "\n")
        raise AssertionError(argv)

    monkeypatch.setattr(composite, "_run", fake_run)
    monkeypatch.setattr(
        composite,
        "_binary",
        lambda *_args: (binary.resolve(), "b" * 64),
    )
    monkeypatch.setattr(composite, "_node_binary", lambda: fake_node)

    runtime = composite.gitnexus_exact_composite_runtime(context)
    invoke = runtime.extra_tools["gitnexus_exact_composite"][1]
    output = invoke({})
    public = json.loads(output)

    assert "provider_calls" not in public
    assert public["detect_changes"] == payload["detect_changes"]
    assert public["selection"]["selected"]["name"] == "runSpecialistLoop"
    assert public["enrichment"]["context"]["performed"] is True
    assert public["enrichment"]["impact"]["performed"] is True
    assert public["enrichment"]["trace"]["performed"] is True
    assert public["enrichment"]["process"]["performed"] is True
    assert calls[-1]["argv"] == [
        str(fake_node),
        str(composite._BRIDGE_PATH),
        str(backend),
        str(resources),
        str(graph_repo),
        context.baseline_revision,
    ]
    assert calls[-1]["env"]["GITNEXUS_HOME"] == str(graph_repo.parent / "home")
    assert not (visible / ".gitnexus").exists()

    assert runtime.metadata["profile_id"] == (
        "gitnexus_official_structured_k1_exact_composite"
    )
    assert runtime.metadata["persistent_backend_scope"] == (
        "one_composite_tool_invocation"
    )
    assert [call["operation"] for call in runtime.metadata["provider_calls"]] == [
        "detect_changes",
        "context",
        "impact",
        "trace",
        "process_resource",
    ]
    assert all(
        call["composite_invocation"] == 1
        for call in runtime.metadata["provider_calls"]
    )
    query = runtime.metadata["query_calls"][0]
    assert query["provider_call_count"] == 5
    assert query["selected_uid"] == (
        "Function:src/agent/react-executor.ts:runSpecialistLoop"
    )
    assert query["detect_metrics"]["summary_counts_match_arrays"] is True
    assert query["detect_metrics"]["partial_value_valid"] is True
    assert query["enrichment_performed"] == {
        "context": True,
        "impact": True,
        "process": True,
        "trace": True,
    }
    assert query["result_chars"] == len(output)
    assert query["result_sha256"] == hashlib.sha256(output.encode()).hexdigest()

    with pytest.raises(ValueError, match="takes no arguments"):
        invoke({"limit": 1000})
    assert len(runtime.metadata["query_calls"]) == 1

    assert runtime.close is not None
    graph_root = graph_repo.parent if graph_repo is not None else None
    runtime.close()
    assert graph_root is not None and not graph_root.exists()
    assert runtime.metadata["cleanup_success"] is True


def test_bridge_uses_one_backend_and_executes_exact_k1_sequence(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    repo = tmp_path / "repo"
    repo.mkdir()
    baseline = "1" * 40
    backend = tmp_path / "local-backend.mjs"
    resources = tmp_path / "resources.mjs"
    backend.write_text(
        """
let instances = 0;
export class LocalBackend {
  constructor() { this.instance = ++instances; }
  async init() { return true; }
  async dispose() {}
  async listRepos() { return [{name: "fixture", path: process.argv[4]}]; }
  async callTool(method, params) {
    if (method === "detect_changes") return {
      summary: {changed_count: 6, affected_count: 3, changed_files: 4, risk_level: "high"},
      changed_symbols: [
        {id:"Section:docs/new.md:Title",name:"Title",filePath:"docs/new.md",change_type:"touched"},
        {id:"Function:src/agent/react-executor.ts:runSpecialistLoop",name:"runSpecialistLoop",filePath:"src/agent/react-executor.ts",change_type:"touched"},
        {id:"Function:src/models/prompts.ts:specialistStepPrompt",name:"specialistStepPrompt",filePath:"src/models/prompts.ts",change_type:"touched"},
        {id:"Method:src/a.ts:A.specialistStep#1",name:"specialistStep",filePath:"src/a.ts",change_type:"touched"},
        {id:"Method:src/b.ts:B.specialistStep#1",name:"specialistStep",filePath:"src/b.ts",change_type:"touched"},
        {id:"Function:test/agent.test.ts:testOnly",name:"testOnly",filePath:"test/agent.test.ts",change_type:"touched"},
      ],
      affected_processes: [
        {
          id:"p1", name:"RunSpecialistLoop → GetPermissions",
          process_type:"cross_community", step_count:4,
          changed_steps:[
            {symbol:"runSpecialistLoop",step:1},
            {symbol:"specialistStepPrompt",step:2},
          ],
        },
        {
          id:"p2", name:"RunSpecialistLoop → HasPermissions",
          process_type:"cross_community", step_count:3,
          changed_steps:[{symbol:"runSpecialistLoop",step:1}],
        },
        {
          id:"p3", name:"SpecialistStep → Commit",
          process_type:"intra_community", step_count:3,
          changed_steps:[{symbol:"specialistStep",step:1}],
        },
      ],
    };
    if (method === "context") return {
      status:"found", instance:this.instance,
      symbol:{uid:params.uid,name:params.name},
      incoming:{calls:[]}, outgoing:{calls:[]}, processes:[],
    };
    if (method === "impact") return {
      instance:this.instance,
      target:{id:params.target_uid,name:params.target},
      byDepth:{
        1:[{
          id:"Function:src/agent/nodes.ts:createSpecialistNode",
          name:"createSpecialistNode",
        }],
        2:[{
          id:"Function:src/agent/graph.ts:createAgentGraph",
          name:"createAgentGraph",
        }],
      },
      byDepthCounts:{1:1,2:1}, affected_processes:[], affected_modules:[],
    };
    if (method === "trace") return {
      status:"ok", instance:this.instance, hopCount:2,
      hops:[{name:params.from},{name:"createSpecialistNode"},{name:params.to}],
    };
    throw new Error(`unexpected method: ${method}`);
  }
}
""",
        encoding="utf-8",
    )
    resources.write_text(
        """
export async function readResource(uri, backend) {
  return `instance: ${backend.instance}\\nuri: ${uri}\\ntrace: runSpecialistLoop -> getPermissions`;
}
""",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            node,
            str(composite._BRIDGE_PATH),
            str(backend),
            str(resources),
            str(repo),
            baseline,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)

    assert payload["selection"]["selected"] == {
        "filePath": "src/agent/react-executor.ts",
        "kind": "Function",
        "name": "runSpecialistLoop",
        "score": {
            "changed_step_occurrences": 2,
            "cross_community_processes": 2,
            "kind_priority": 0,
            "total_processes": 2,
        },
        "uid": "Function:src/agent/react-executor.ts:runSpecialistLoop",
    }
    assert len(payload["detect_changes"]["changed_symbols"]) == 6
    assert [call["operation"] for call in payload["provider_calls"]] == [
        "detect_changes",
        "context",
        "impact",
        "trace",
        "process_resource",
    ]
    assert payload["enrichment"]["context"]["result"]["instance"] == 1
    assert payload["enrichment"]["impact"]["result"]["instance"] == 1
    assert payload["enrichment"]["trace"]["result"]["instance"] == 1
    assert payload["enrichment"]["trace"]["result"]["hopCount"] == 2
    assert payload["enrichment"]["process"]["performed"] is True
    assert "instance: 1" in payload["enrichment"]["process"]["content"]
    assert payload["enrichment"]["process"]["selected_process"]["name"] == (
        "RunSpecialistLoop → GetPermissions"
    )
    assert payload["provider_calls"][0]["arguments"] == {
        "base_ref": baseline,
        "scope": "compare",
    }
    assert payload["provider_calls"][2]["arguments"]["limit"] == 8
    assert payload["provider_calls"][2]["arguments"]["maxDepth"] == 2
    assert all(
        call["runtime_bindings"] == {"repo": "isolated_index_clone"}
        for call in payload["provider_calls"]
    )


def test_bridge_payload_validation_rejects_model_controllable_detect_limit() -> None:
    baseline = "1" * 40
    payload = _bridge_payload(baseline)
    payload["provider_calls"][0]["arguments"]["limit"] = 500

    with pytest.raises(RuntimeError, match="detect_changes binding"):
        composite._validate_bridge_payload(payload, baseline_revision=baseline)

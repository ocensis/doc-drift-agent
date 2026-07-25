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

import _portfolio_gitnexus_structured as structured  # noqa: E402
import gitnexus_structured_change_agent  # noqa: E402
from _runner import BASE_TOOLS, TOOL_PORTFOLIO_NATIVE_PROTOCOL_VERSION, AgentContext  # noqa: E402


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
    _git(repo, "config", "user.email", "structured-test@example.com")
    _git(repo, "config", "user.name", "Structured Test")
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


def _fake_package(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "runtime" / "node_modules" / "gitnexus"
    binary = root / "dist" / "cli" / "index.js"
    backend = root / "dist" / "mcp" / "local" / "local-backend.js"
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
    return binary, backend


def _completed(
    argv: list[str],
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)


def test_agent_and_tool_use_independent_structured_names() -> None:
    agent = gitnexus_structured_change_agent.AGENT

    assert agent.name == "gitnexus_structured_change_agent"
    assert agent.tools == (*BASE_TOOLS, "gitnexus_structured_change")
    assert agent.protocol_version == TOOL_PORTFOLIO_NATIVE_PROTOCOL_VERSION
    assert "gitnexus_structured_change" in agent.system_prompt
    assert structured.GITNEXUS_STRUCTURED_PROFILE_ID == (
        "gitnexus_official_structured_change"
    )
    parameters = structured.GITNEXUS_STRUCTURED_CHANGE_DEFINITION["function"][
        "parameters"
    ]
    assert parameters == {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }
    rendered = json.dumps(structured.GITNEXUS_STRUCTURED_CHANGE_DEFINITION)
    assert "limit" not in rendered
    assert "base_ref" not in rendered


def test_structured_runtime_uses_official_backend_without_cli_caps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    visible, context = _visible_repo(tmp_path)
    binary, backend = _fake_package(tmp_path)
    fake_node = tmp_path / "bin" / "node"
    fake_node.parent.mkdir()
    fake_node.write_text("fake node\n", encoding="utf-8")
    graph_repo: Path | None = None
    calls: list[dict[str, Any]] = []

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
                            "nodes": 30,
                            "edges": 20,
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
            assert graph_repo is not None
            changed = [
                {
                    "id": f"Function:symbol-{index}",
                    "name": f"symbol{index}",
                    "type": "Function",
                    "filePath": f"{graph_repo}/src/service.py",
                    "change_type": "touched",
                }
                for index in range(18)
            ]
            affected = [
                {
                    "id": f"process-{index}",
                    "name": f"flow {index}",
                    "process_type": "intra_repository",
                    "step_count": 2,
                    "changed_steps": [{"symbol": "Alpha", "step": 1}],
                }
                for index in range(12)
            ]
            payload = {
                "summary": {
                    "changed_count": len(changed),
                    "affected_count": len(affected),
                    "changed_files": 1,
                    "risk_level": "high",
                },
                "changed_symbols": changed,
                "affected_processes": affected,
                "partial": False,
            }
            return _completed(argv, stdout=json.dumps(payload, indent=2) + "\n")
        raise AssertionError(argv)

    monkeypatch.setattr(structured, "_run", fake_run)
    monkeypatch.setattr(
        structured,
        "_binary",
        lambda *_args: (binary.resolve(), "b" * 64),
    )
    monkeypatch.setattr(structured, "_node_binary", lambda: fake_node)

    runtime = structured.gitnexus_structured_change_runtime(context)
    invoke = runtime.extra_tools["gitnexus_structured_change"][1]
    output = invoke({})
    payload = json.loads(output)

    assert len(payload["changed_symbols"]) == 18
    assert len(payload["affected_processes"]) == 12
    assert payload["partial"] is False
    assert str(graph_repo) not in output
    assert all(item["filePath"] == "./src/service.py" for item in payload["changed_symbols"])
    bridge_call = calls[-1]
    assert bridge_call["argv"] == [
        str(fake_node),
        str(structured._BRIDGE_PATH),
        str(backend),
        str(graph_repo),
        context.baseline_revision,
    ]
    assert "--limit" not in bridge_call["argv"]
    assert bridge_call["env"]["GITNEXUS_HOME"] == str(graph_repo.parent / "home")
    assert bridge_call["env"]["GITNEXUS_LBUG_EXTENSION_INSTALL"] == "load-only"
    assert bridge_call["env"]["DO_NOT_TRACK"] == "1"
    assert not (visible / ".gitnexus").exists()

    fixed = {"scope": "compare", "base_ref": context.baseline_revision}
    assert runtime.metadata["profile_id"] == "gitnexus_official_structured_change"
    assert runtime.metadata["fixed_provider_arguments"] == fixed
    assert runtime.metadata["provider_limit"] is None
    assert runtime.metadata["cli_formatter_used"] is False
    assert runtime.metadata["backend_module"] == "dist/mcp/local/local-backend.js"
    assert runtime.metadata["backend_module_sha256"] == hashlib.sha256(
        backend.read_bytes()
    ).hexdigest()
    record = runtime.metadata["provider_calls"][0]
    assert record["arguments"] == fixed
    assert record["runtime_bindings"] == {"repo": "isolated_index_clone"}
    assert record["structured_json"] is True
    assert record["provider_error"] is False
    assert record["partial"] is False
    assert record["changed_symbols_count"] == 18
    assert record["affected_processes_count"] == 12
    assert record["summary_counts_match_arrays"] is True
    assert record["output_chars"] == len(output)
    assert record["output_sha256"] == hashlib.sha256(output.encode()).hexdigest()
    query = runtime.metadata["query_calls"][0]
    assert query["arguments"] == {}
    assert query["provider_arguments"] == fixed

    with pytest.raises(ValueError, match="takes no arguments"):
        invoke({"base_ref": "attacker-controlled"})
    assert len(runtime.metadata["provider_calls"]) == 1

    assert runtime.close is not None
    graph_root = graph_repo.parent if graph_repo is not None else None
    runtime.close()
    assert graph_root is not None and not graph_root.exists()
    assert runtime.metadata["cleanup_success"] is True


def test_structured_metrics_preserve_partial_error_and_count_mismatch() -> None:
    partial = structured._structured_metrics(
        json.dumps(
            {
                "summary": {"changed_count": 2, "affected_count": 1},
                "changed_symbols": [{"id": "one"}],
                "affected_processes": [],
                "partial": True,
                "error": "degraded",
            }
        )
    )

    assert partial["structured_json"] is True
    assert partial["partial"] is True
    assert partial["provider_error"] is True
    assert partial["summary_counts_match_arrays"] is False
    assert partial["summary_changed_count"] == 2
    assert partial["changed_symbols_count"] == 1


def test_bridge_calls_local_backend_and_emits_complete_json(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    repo = tmp_path / "repo"
    repo.mkdir()
    baseline = "1" * 40
    backend = tmp_path / "local-backend.mjs"
    backend.write_text(
        """
export class LocalBackend {
  async init() { return true; }
  async callTool(method, params) {
    return {
      method,
      params,
      summary: { changed_count: 18, affected_count: 12 },
      changed_symbols: Array.from({length: 18}, (_, i) => ({id: `s${i}`})),
      affected_processes: Array.from({length: 12}, (_, i) => ({id: `p${i}`})),
      partial: true,
    };
  }
  async dispose() {}
}
""",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            node,
            str(structured._BRIDGE_PATH),
            str(backend),
            str(repo),
            baseline,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)

    assert payload["method"] == "detect_changes"
    assert payload["params"] == {
        "scope": "compare",
        "base_ref": baseline,
        "repo": str(repo),
    }
    assert len(payload["changed_symbols"]) == 18
    assert len(payload["affected_processes"]) == 12
    assert payload["partial"] is True

"""Isolated code-graph runtimes for the three-arm retrieval ablation.

The Agent-visible repository is never indexed.  Each treatment builds its graph
in a second clone at the same commit, then exposes a deliberately small,
read-only CLI wrapper through the existing single-Agent tool protocol.  This
avoids installer/MCP prompt injection and prevents generic ``grep``/``list_dir``
from seeing provider index files.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from _runner import (
    BASE_TOOLS,
    AgentContext,
    AgentRuntime,
    ExtraTools,
    UnboundedRepoToolbox,
    generic_extra_tools,
)

GRAPH_PROTOCOL_VERSION = "single-agent-code-graph-ablation-v1-unbounded"

GRAPH_DEFAULT_AGENT = "graph_default_agent"
CODEGRAPH_AGENT = "codegraph_agent"
GITNEXUS_AGENT = "gitnexus_agent"

CODEGRAPH_VERSION = "1.5.0"
GITNEXUS_VERSION = "1.6.9"
CODEGRAPH_PACKAGE_INTEGRITY = (
    "sha512-/l1JMVOQ9WGQLrc/IIuAg7Igr944t79/oNCJTcnGkYtIeQx2XFIqI0ho+9Les/"
    "Yu4zKfmPU17hIUshD6yP1fKw=="
)
GITNEXUS_PACKAGE_INTEGRITY = (
    "sha512-Rq5LXFygx7jjMp/YFsIAcnnzuKvvCsb4rxHFILnu05ZOqk7xNXTUSMRa968EOCb"
    "xcKFxnhKYaGXoabOUeGZX6A=="
)

CODEGRAPH_TOOLS = ("codegraph_explore",)
GITNEXUS_TOOLS = (
    "gitnexus_query",
    "gitnexus_context",
    "gitnexus_impact",
    "gitnexus_trace",
)

EXPECTED_TOOL_MENUS: dict[str, tuple[str, ...]] = {
    GRAPH_DEFAULT_AGENT: BASE_TOOLS,
    CODEGRAPH_AGENT: BASE_TOOLS + CODEGRAPH_TOOLS,
    GITNEXUS_AGENT: BASE_TOOLS + GITNEXUS_TOOLS,
}

_SEMVER = re.compile(r"(?<!\d)(\d+\.\d+\.\d+)(?!\d)")
_FORBIDDEN_VISIBLE_DIRS = (".codegraph", ".gitnexus")


def _run(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one command without a harness timeout or output truncation."""

    return subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def _git(repo: Path, *arguments: str) -> str:
    completed = _run(["git", *arguments], cwd=repo)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "git command failed"
        raise RuntimeError(detail)
    return completed.stdout.strip()


def _clean(repo: Path) -> bool:
    return not _git(repo, "status", "--porcelain=v1", "--untracked-files=all")


def _tree(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD^{tree}")


def _assert_visible_repo_isolated(context: AgentContext) -> None:
    if _git(context.repo_path, "rev-parse", "HEAD") != context.head_revision:
        raise RuntimeError("Agent-visible repository HEAD changed before graph setup")
    if not _clean(context.repo_path):
        raise RuntimeError("Agent-visible repository is not clean before graph setup")
    leaked = [name for name in _FORBIDDEN_VISIBLE_DIRS if (context.repo_path / name).exists()]
    if leaked:
        raise RuntimeError(f"Agent-visible repository contains graph state: {', '.join(leaked)}")


def _clone_for_index(context: AgentContext, provider: str) -> tuple[Path, float]:
    started = time.monotonic()
    root = Path(tempfile.mkdtemp(prefix=f"fr009-{provider}-index-"))
    graph_repo = root / "repo"
    completed = _run(
        ["git", "clone", "--quiet", "--no-hardlinks", str(context.repo_path), str(graph_repo)],
        cwd=root,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "failed to clone graph repository")
    _git(graph_repo, "checkout", "--quiet", context.head_revision)
    if _git(graph_repo, "rev-parse", "HEAD") != context.head_revision:
        raise RuntimeError("graph repository HEAD does not match Agent-visible HEAD")
    if _tree(graph_repo) != _tree(context.repo_path):
        raise RuntimeError("graph and Agent-visible repository trees differ")
    return graph_repo, time.monotonic() - started


def _directory_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return total
    for candidate in path.rglob("*"):
        try:
            if candidate.is_file():
                total += candidate.stat().st_size
        except OSError:
            continue
    return total


def _binary(env_name: str, fallback: str, expected_version: str) -> tuple[Path, str]:
    configured = os.environ.get(env_name, "").strip()
    resolved = configured or shutil.which(fallback)
    if not resolved:
        raise RuntimeError(
            f"{fallback} is not installed; set {env_name} to the pinned {expected_version} binary"
        )
    path = Path(resolved).resolve()
    if not path.is_file():
        raise RuntimeError(f"{env_name} does not resolve to a file: {path}")
    completed = _run([str(path), "--version"], cwd=path.parent)
    rendered = "\n".join((completed.stdout, completed.stderr)).strip()
    match = _SEMVER.search(rendered)
    actual = match.group(1) if match else ""
    if completed.returncode != 0 or actual != expected_version:
        raise RuntimeError(
            f"expected {fallback} {expected_version}, got {rendered or 'no version output'}"
        )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return path, digest


def _sanitized(text: str, graph_repo: Path) -> str:
    rendered = text
    aliases = {str(graph_repo), str(graph_repo.resolve())}
    for alias in sorted(aliases, key=len, reverse=True):
        rendered = rendered.replace(alias, ".")
    return rendered


def _command_result(
    completed: subprocess.CompletedProcess[str],
    *,
    graph_repo: Path,
) -> str:
    stdout = _sanitized(completed.stdout, graph_repo).strip()
    stderr = _sanitized(completed.stderr, graph_repo).strip()
    if completed.returncode != 0:
        return f"ERROR: {stderr or stdout or f'provider exited {completed.returncode}'}"
    return stdout or "(no output)"


def _metadata(
    *,
    provider: str,
    context: AgentContext,
    package_version: str | None,
    binary_sha256: str | None,
    isolation_clone_seconds: float,
    index_seconds: float,
    index_success: bool,
    index_size_bytes: int,
    index_stats: object,
) -> dict[str, Any]:
    return {
        "provider": provider,
        "isolated": True,
        "source_head": context.head_revision,
        "source_tree": _tree(context.repo_path),
        "agent_repo_clean": _clean(context.repo_path),
        "agent_repo_graph_dirs_absent": all(
            not (context.repo_path / name).exists() for name in _FORBIDDEN_VISIBLE_DIRS
        ),
        "index_success": index_success,
        "package_version": package_version,
        "binary_sha256": binary_sha256,
        "isolation_clone_seconds": round(isolation_clone_seconds, 6),
        "index_seconds": round(index_seconds, 6),
        "index_size_bytes": index_size_bytes,
        "index_stats": index_stats,
        "installer_used": False,
        "mcp_used": False,
        "prompt_or_hook_injection": False,
        "query_calls": [],
    }


def _cleanup_callback(
    graph_repo: Path,
    metadata: dict[str, Any],
    provider: str,
) -> Callable[[], None]:
    graph_root = graph_repo.parent.resolve()
    expected_parent = Path(tempfile.gettempdir()).resolve()
    expected_prefix = f"fr009-{provider}-index-"

    def cleanup() -> None:
        if graph_root.parent != expected_parent or not graph_root.name.startswith(expected_prefix):
            raise RuntimeError(f"refusing to clean unexpected graph root: {graph_root}")
        shutil.rmtree(graph_root)
        metadata["cleanup_success"] = not graph_root.exists()

    return cleanup


def graph_default_runtime(context: AgentContext) -> AgentRuntime:
    """Prepare the graph experiment control without cloning or indexing."""

    _assert_visible_repo_isolated(context)
    metadata = _metadata(
        provider="none",
        context=context,
        package_version=None,
        binary_sha256=None,
        isolation_clone_seconds=0.0,
        index_seconds=0.0,
        index_success=True,
        index_size_bytes=0,
        index_stats={},
    )
    return AgentRuntime(
        toolbox=UnboundedRepoToolbox(context.repo_path),
        extra_tools=generic_extra_tools(context),
        metadata=metadata,
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


CODEGRAPH_EXPLORE_DEFINITION = _function(
    "codegraph_explore",
    (
        "Explore the current HEAD code graph using a natural-language question or concrete "
        "symbol names. Returns provider-selected line-numbered source, call paths, and blast "
        "radius. Prefer exact function/class symbols; path-only queries may expand to related "
        "files. This tool does not contain the baseline diff or documentation alignment."
    ),
    {
        "query": {"type": "string", "minLength": 1},
        "max_files": {
            "type": "integer",
            "minimum": 1,
            "maximum": 20,
            "default": 20,
            "description": "Provider hard limit for source-bearing files (1-20).",
        },
    },
    ["query"],
)


def _recorded_handler(
    *,
    provider: str,
    name: str,
    argv: list[str],
    cwd: Path,
    env: dict[str, str],
    graph_repo: Path,
    metadata: dict[str, Any],
    arguments: dict[str, Any],
) -> str:
    started = time.monotonic()
    completed = _run(argv, cwd=cwd, env=env)
    elapsed = time.monotonic() - started
    rendered = _command_result(completed, graph_repo=graph_repo)
    metadata["query_calls"].append(
        {
            "provider": provider,
            "tool": name,
            "arguments": dict(arguments),
            "seconds": round(elapsed, 6),
            "exit_code": completed.returncode,
            "output_chars": len(rendered),
            "error": rendered.startswith("ERROR:"),
        }
    )
    return rendered


def codegraph_runtime(context: AgentContext) -> AgentRuntime:
    """Build an isolated CodeGraph 1.5.0 index and expose one neutral tool."""

    _assert_visible_repo_isolated(context)
    binary, digest = _binary("CODEGRAPH_BIN", "codegraph", CODEGRAPH_VERSION)
    graph_repo, clone_seconds = _clone_for_index(context, "codegraph")
    env = dict(os.environ)
    env.update(
        {
            "DO_NOT_TRACK": "1",
            "NO_COLOR": "1",
            "CODEGRAPH_NO_DOWNLOAD": "1",
        }
    )
    index_started = time.monotonic()
    initialized = _run([str(binary), "init", str(graph_repo)], cwd=graph_repo, env=env)
    index_seconds = time.monotonic() - index_started
    if initialized.returncode != 0:
        detail = initialized.stderr.strip() or initialized.stdout.strip() or "CodeGraph init failed"
        raise RuntimeError(_sanitized(detail, graph_repo))
    index_dir = graph_repo / ".codegraph"
    status = _run([str(binary), "status", "--json", str(graph_repo)], cwd=graph_repo, env=env)
    if status.returncode != 0:
        raise RuntimeError(status.stderr.strip() or "CodeGraph status failed after init")
    try:
        status_payload: object = json.loads(status.stdout)
    except json.JSONDecodeError:
        status_payload = _sanitized(status.stdout.strip(), graph_repo)
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
    metadata["telemetry_disabled"] = True
    metadata["update_checks_disabled"] = True
    metadata["upstream_max_files"] = 20
    metadata["package_integrity"] = CODEGRAPH_PACKAGE_INTEGRITY

    def explore(arguments: dict[str, Any]) -> str:
        query = str(arguments.get("query", "")).strip()
        if not query:
            raise ValueError("query must be non-empty")
        raw_max_files = arguments.get("max_files", 20)
        if isinstance(raw_max_files, bool):
            raise ValueError("max_files must be an integer from 1 to 20")
        try:
            max_files = int(raw_max_files)
        except (TypeError, ValueError) as error:
            raise ValueError("max_files must be an integer from 1 to 20") from error
        if max_files < 1 or max_files > 20:
            raise ValueError("max_files must be an integer from 1 to 20")
        normalized = {"query": query, "max_files": max_files}
        return _recorded_handler(
            provider="codegraph",
            name="codegraph_explore",
            argv=[
                str(binary),
                "explore",
                "--path",
                str(graph_repo),
                "--max-files",
                str(max_files),
                query,
            ],
            cwd=graph_repo,
            env=env,
            graph_repo=graph_repo,
            metadata=metadata,
            arguments=normalized,
        )

    extras = generic_extra_tools(context)
    extras["codegraph_explore"] = (CODEGRAPH_EXPLORE_DEFINITION, explore)
    _assert_visible_repo_isolated(context)
    return AgentRuntime(
        toolbox=UnboundedRepoToolbox(context.repo_path),
        extra_tools=extras,
        metadata=metadata,
        close=_cleanup_callback(graph_repo, metadata, "codegraph"),
    )


GITNEXUS_QUERY_DEFINITION = _function(
    "gitnexus_query",
    (
        "Search the current HEAD code graph for relevant execution flows and symbols. "
        "Optionally include source. This tool has no baseline diff or doc alignment."
    ),
    {
        "search_query": {"type": "string", "minLength": 1},
        "task_context": {"type": "string"},
        "goal": {"type": "string"},
        "include_content": {"type": "boolean", "default": False},
        "limit": {"type": "integer", "minimum": 1},
    },
    ["search_query"],
)

GITNEXUS_CONTEXT_DEFINITION = _function(
    "gitnexus_context",
    "Return a current HEAD symbol's callers, callees, references, and execution processes.",
    {
        "name": {"type": "string", "minLength": 1},
        "uid": {"type": "string", "minLength": 1},
        "file_path": {"type": "string"},
        "include_content": {"type": "boolean", "default": False},
        "limit": {"type": "integer", "minimum": 1},
    },
    [],
)
GITNEXUS_CONTEXT_DEFINITION["function"]["parameters"]["anyOf"] = [
    {"required": ["name"]},
    {"required": ["uid"]},
]

GITNEXUS_IMPACT_DEFINITION = _function(
    "gitnexus_impact",
    "Analyze the current HEAD call-graph blast radius of changing one concrete symbol.",
    {
        "target": {"type": "string", "minLength": 1},
        "target_uid": {"type": "string", "minLength": 1},
        "file_path": {"type": "string"},
        "kind": {"type": "string"},
        "direction": {"type": "string", "enum": ["upstream", "downstream"]},
        "max_depth": {"type": "integer", "minimum": 1},
        "include_tests": {"type": "boolean", "default": False},
        "limit": {"type": "integer", "minimum": 1},
    },
    [],
)
GITNEXUS_IMPACT_DEFINITION["function"]["parameters"]["anyOf"] = [
    {"required": ["target"]},
    {"required": ["target_uid"]},
]

GITNEXUS_TRACE_DEFINITION = _function(
    "gitnexus_trace",
    "Find the shortest directed current HEAD call/class-member path between two symbols.",
    {
        "from_symbol": {"type": "string", "minLength": 1},
        "to_symbol": {"type": "string", "minLength": 1},
        "from_uid": {"type": "string", "minLength": 1},
        "to_uid": {"type": "string", "minLength": 1},
        "from_file": {"type": "string"},
        "to_file": {"type": "string"},
        "max_depth": {"type": "integer", "minimum": 1},
        "include_tests": {"type": "boolean", "default": False},
    },
    [],
)
GITNEXUS_TRACE_DEFINITION["function"]["parameters"]["allOf"] = [
    {"anyOf": [{"required": ["from_symbol"]}, {"required": ["from_uid"]}]},
    {"anyOf": [{"required": ["to_symbol"]}, {"required": ["to_uid"]}]},
]


def _optional_text(arguments: dict[str, Any], key: str) -> str | None:
    text = str(arguments.get(key, "")).strip()
    return text or None


def _optional_positive_int(arguments: dict[str, Any], key: str) -> int | None:
    raw = arguments.get(key)
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise ValueError(f"{key} must be a positive integer")
    try:
        value = int(raw)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{key} must be a positive integer") from error
    if value < 1:
        raise ValueError(f"{key} must be a positive integer")
    return value


def gitnexus_runtime(context: AgentContext) -> AgentRuntime:
    """Build an isolated GitNexus 1.6.9 index and expose four read-only tools."""

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
        detail = (
            initialized.stderr.strip() or initialized.stdout.strip() or "GitNexus analyze failed"
        )
        raise RuntimeError(_sanitized(detail, graph_repo))
    index_dir = graph_repo / ".gitnexus"
    meta_path = index_dir / "meta.json"
    try:
        meta_payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("GitNexus index metadata is missing or invalid") from error
    capabilities = meta_payload.get("capabilities", {})
    graph_status = capabilities.get("graph", {}).get("status")
    fts_status = capabilities.get("fts", {}).get("status")
    stats = meta_payload.get("stats", {})
    embeddings = stats.get("embeddings")
    if (
        meta_payload.get("lastCommit") != context.head_revision
        or graph_status != "available"
        or fts_status != "available"
        or embeddings != 0
    ):
        raise RuntimeError(
            "GitNexus index capabilities are incomplete or embeddings unexpectedly enabled"
        )
    safe_meta = {
        "lastCommit": meta_payload.get("lastCommit"),
        "stats": stats,
        "capabilities": capabilities,
        "schemaVersion": meta_payload.get("schemaVersion"),
    }
    metadata = _metadata(
        provider="gitnexus",
        context=context,
        package_version=GITNEXUS_VERSION,
        binary_sha256=digest,
        isolation_clone_seconds=clone_seconds,
        index_seconds=index_seconds,
        index_success=index_dir.is_dir(),
        index_size_bytes=_directory_size(index_dir),
        index_stats=safe_meta,
    )
    metadata["registry_home_isolated"] = True
    metadata["wrapper_read_only_allowlist"] = True
    metadata["fts_status"] = fts_status
    metadata["graph_status"] = graph_status
    metadata["fts_extension_policy"] = "load-only"
    metadata["embeddings_enabled"] = False
    metadata["gitnexus_config_present"] = False
    metadata["gitnexus_ignore_present"] = (graph_repo / ".gitnexusignore").exists()
    metadata["package_integrity"] = GITNEXUS_PACKAGE_INTEGRITY

    def invoke(name: str, argv: list[str], normalized: dict[str, Any]) -> str:
        return _recorded_handler(
            provider="gitnexus",
            name=name,
            argv=[str(binary), *argv],
            cwd=graph_repo,
            env=env,
            graph_repo=graph_repo,
            metadata=metadata,
            arguments=normalized,
        )

    def query(arguments: dict[str, Any]) -> str:
        search_query = str(arguments.get("search_query", "")).strip()
        if not search_query:
            raise ValueError("search_query must be non-empty")
        normalized: dict[str, Any] = {"search_query": search_query}
        argv = ["query", search_query]
        for key, flag in (("task_context", "--context"), ("goal", "--goal")):
            value = _optional_text(arguments, key)
            if value is not None:
                normalized[key] = value
                argv.extend((flag, value))
        limit = _optional_positive_int(arguments, "limit")
        if limit is not None:
            normalized["limit"] = limit
            argv.extend(("--limit", str(limit)))
        include_content = bool(arguments.get("include_content", False))
        normalized["include_content"] = include_content
        if include_content:
            argv.append("--content")
        return invoke("gitnexus_query", argv, normalized)

    def symbol_context(arguments: dict[str, Any]) -> str:
        name = _optional_text(arguments, "name")
        uid = _optional_text(arguments, "uid")
        if name is None and uid is None:
            raise ValueError("name or uid must be non-empty")
        normalized: dict[str, Any] = {}
        argv = ["context"]
        if name is not None:
            normalized["name"] = name
            argv.append(name)
        if uid is not None:
            normalized["uid"] = uid
            argv.extend(("--uid", uid))
        file_path = _optional_text(arguments, "file_path")
        if file_path is not None:
            normalized["file_path"] = file_path
            argv.extend(("--file", file_path))
        limit = _optional_positive_int(arguments, "limit")
        if limit is not None:
            normalized["limit"] = limit
            argv.extend(("--limit", str(limit)))
        include_content = bool(arguments.get("include_content", False))
        normalized["include_content"] = include_content
        if include_content:
            argv.append("--content")
        return invoke("gitnexus_context", argv, normalized)

    def impact(arguments: dict[str, Any]) -> str:
        target = _optional_text(arguments, "target")
        target_uid = _optional_text(arguments, "target_uid")
        if target is None and target_uid is None:
            raise ValueError("target or target_uid must be non-empty")
        normalized: dict[str, Any] = {}
        argv = ["impact"]
        if target is not None:
            normalized["target"] = target
            argv.append(target)
        if target_uid is not None:
            normalized["target_uid"] = target_uid
            argv.extend(("--uid", target_uid))
        file_path = _optional_text(arguments, "file_path")
        if file_path is not None:
            normalized["file_path"] = file_path
            argv.extend(("--file", file_path))
        kind = _optional_text(arguments, "kind")
        if kind is not None:
            normalized["kind"] = kind
            argv.extend(("--kind", kind))
        direction = str(arguments.get("direction", "upstream")).strip().lower()
        if direction not in {"upstream", "downstream"}:
            raise ValueError("direction must be upstream or downstream")
        normalized["direction"] = direction
        argv.extend(("--direction", direction))
        depth = _optional_positive_int(arguments, "max_depth")
        if depth is not None:
            normalized["max_depth"] = depth
            argv.extend(("--depth", str(depth)))
        limit = _optional_positive_int(arguments, "limit")
        if limit is not None:
            normalized["limit"] = limit
            argv.extend(("--limit", str(limit)))
        include_tests = bool(arguments.get("include_tests", False))
        normalized["include_tests"] = include_tests
        if include_tests:
            argv.append("--include-tests")
        return invoke("gitnexus_impact", argv, normalized)

    def trace(arguments: dict[str, Any]) -> str:
        from_symbol = _optional_text(arguments, "from_symbol")
        to_symbol = _optional_text(arguments, "to_symbol")
        from_uid = _optional_text(arguments, "from_uid")
        to_uid = _optional_text(arguments, "to_uid")
        if (from_symbol is None and from_uid is None) or (to_symbol is None and to_uid is None):
            raise ValueError("both trace endpoints require a symbol or uid")
        normalized: dict[str, Any] = {}
        if from_symbol is not None:
            normalized["from_symbol"] = from_symbol
        if to_symbol is not None:
            normalized["to_symbol"] = to_symbol
        if from_uid is not None:
            normalized["from_uid"] = from_uid
        if to_uid is not None:
            normalized["to_uid"] = to_uid
        argv = ["trace", from_symbol or from_uid or "", to_symbol or to_uid or ""]
        if from_uid is not None:
            argv.extend(("--from-uid", from_uid))
        if to_uid is not None:
            argv.extend(("--to-uid", to_uid))
        for key, flag in (("from_file", "--from-file"), ("to_file", "--to-file")):
            value = _optional_text(arguments, key)
            if value is not None:
                normalized[key] = value
                argv.extend((flag, value))
        depth = _optional_positive_int(arguments, "max_depth")
        if depth is not None:
            normalized["max_depth"] = depth
            argv.extend(("--depth", str(depth)))
        include_tests = bool(arguments.get("include_tests", False))
        normalized["include_tests"] = include_tests
        if include_tests:
            argv.append("--include-tests")
        return invoke("gitnexus_trace", argv, normalized)

    extras: ExtraTools = generic_extra_tools(context)
    extras.update(
        {
            "gitnexus_query": (GITNEXUS_QUERY_DEFINITION, query),
            "gitnexus_context": (GITNEXUS_CONTEXT_DEFINITION, symbol_context),
            "gitnexus_impact": (GITNEXUS_IMPACT_DEFINITION, impact),
            "gitnexus_trace": (GITNEXUS_TRACE_DEFINITION, trace),
        }
    )
    _assert_visible_repo_isolated(context)
    return AgentRuntime(
        toolbox=UnboundedRepoToolbox(context.repo_path),
        extra_tools=extras,
        metadata=metadata,
        close=_cleanup_callback(graph_repo, metadata, "gitnexus"),
    )


__all__ = [
    "CODEGRAPH_AGENT",
    "CODEGRAPH_TOOLS",
    "CODEGRAPH_VERSION",
    "EXPECTED_TOOL_MENUS",
    "GITNEXUS_AGENT",
    "GITNEXUS_TOOLS",
    "GITNEXUS_VERSION",
    "GRAPH_DEFAULT_AGENT",
    "GRAPH_PROTOCOL_VERSION",
    "codegraph_runtime",
    "gitnexus_runtime",
    "graph_default_runtime",
]

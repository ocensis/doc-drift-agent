from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import TextIO

import pytest
from mcp import ClientSession, StdioServerParameters, stdio_client

from drift_agent.adapters.contracts import PublicBundleV3
from drift_agent.adapters.mcp import SERVER_INSTRUCTIONS


async def _exercise_stdio_server(
    parameters: StdioServerParameters,
    errlog: TextIO,
) -> tuple[object, object, object]:
    async with stdio_client(parameters, errlog=errlog) as (read, write):
        async with ClientSession(
            read,
            write,
            read_timeout_seconds=timedelta(seconds=10),
        ) as session:
            initialized = await session.initialize()
            tools = await session.list_tools()
            result = await session.call_tool(
                "check_drift",
                {
                    "scope": {"kind": "changed"},
                    "semantic": False,
                },
            )
            return initialized, tools, result


def test_real_stdio_client_initializes_lists_and_calls_with_clean_protocol_stdout(
    drift_repo: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    state_dir = drift_repo.parent / f"{drift_repo.name}-mcp-state"
    stderr_path = drift_repo.parent / f"{drift_repo.name}-mcp-stderr.log"
    project_root = Path(__file__).resolve().parents[2]
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "drift_agent.adapters.mcp",
            "--repo",
            str(drift_repo),
            "--state-dir",
            str(state_dir),
        ],
        cwd=project_root,
        env={"PYTHONUNBUFFERED": "1"},
    )
    caplog.set_level(logging.ERROR, logger="mcp.client.stdio")

    with stderr_path.open("w", encoding="utf-8") as errlog:
        initialized, listed, called = asyncio.run(
            asyncio.wait_for(
                _exercise_stdio_server(parameters, errlog),
                timeout=20,
            )
        )

    assert initialized.serverInfo.name == "doc-code-drift-agent"
    assert initialized.instructions == SERVER_INSTRUCTIONS
    assert [tool.name for tool in listed.tools] == ["check_drift", "repair_drift"]
    assert called.isError is False
    assert isinstance(called.structuredContent, dict)
    payload = called.structuredContent
    validated = PublicBundleV3.model_validate(payload)
    assert validated.model_dump(mode="json") == payload
    assert payload["schema_version"] == 3
    assert payload["status"] == "drift_found"
    assert set(payload) == set(PublicBundleV3.model_fields)
    encoded = json.dumps(payload, ensure_ascii=False)
    assert "semantic_analysis" not in encoded
    assert "validation_input_hashes" not in encoded
    assert "symbol_identity" not in encoded

    # The SDK transport validates every non-empty stdout line as JSON-RPC and
    # logs this exact error if server diagnostics pollute the protocol stream.
    assert not any(
        record.name == "mcp.client.stdio"
        and "Failed to parse JSONRPC message from server" in record.getMessage()
        for record in caplog.records
    )


def test_unconfigured_repo_reports_structured_config_guidance(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    for command in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test"],
        ["git", "commit", "-qm", "base", "--allow-empty"],
    ):
        subprocess.run(command, cwd=repo, check=True, capture_output=True)
    stderr_path = tmp_path / "mcp-stderr.log"
    project_root = Path(__file__).resolve().parents[2]
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "drift_agent.adapters.mcp",
            "--repo",
            str(repo),
            "--state-dir",
            str(tmp_path / "state"),
        ],
        cwd=project_root,
        env={"PYTHONUNBUFFERED": "1"},
    )

    with stderr_path.open("w", encoding="utf-8") as errlog:
        _, _, called = asyncio.run(
            asyncio.wait_for(
                _exercise_stdio_server(parameters, errlog),
                timeout=20,
            )
        )

    assert called.isError is False
    assert isinstance(called.structuredContent, dict)
    payload = called.structuredContent
    assert PublicBundleV3.model_validate(payload).model_dump(mode="json") == payload
    assert payload["status"] == "failed"
    assert len(payload["validation"]) == 1
    entry = payload["validation"][0]
    assert entry["check"] == "config"
    assert entry["status"] == "unavailable"
    assert entry["summary"].startswith("config.missing:")
    assert "drift-agent init" in entry["summary"]

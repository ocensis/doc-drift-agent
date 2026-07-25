from __future__ import annotations

import argparse
import asyncio
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Any, cast

import pydantic_core
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ContentBlock, TextContent
from mcp.types import Tool as MCPTool
from pydantic import BaseModel, ConfigDict, Field, ValidationError, WithJsonSchema

from drift_agent import application
from drift_agent.adapters.contracts import PublicBundleV3, bounded_bundle
from drift_agent.domain.enums import RunMode
from drift_agent.domain.models import RunRequest, ScopeSpec, scope_spec_json_schema
from drift_agent.workspace.identity import resolve_identities, resolve_state_path

_TOOL_ARGUMENTS = {
    "check_drift": frozenset({"scope", "semantic", "max_findings", "summary_only"}),
    "repair_drift": frozenset({"scope", "semantic", "max_findings", "summary_only"}),
}
DEFAULT_MAX_FINDINGS = 200
SERVER_INSTRUCTIONS = (
    "Use check_drift by default with scope.kind=changed and semantic=false. "
    "It reports without editing repository source or documentation files. "
    "Call repair_drift only when the user explicitly asks to apply documentation "
    "edits; tool availability is not repair authorization. "
    'If a result reports status "failed" with a "config" validation entry, the '
    "bound repository has no valid drift-agent.toml; scaffold one with "
    "`drift-agent init` and retry. "
    f"Results inline at most max_findings findings (default {DEFAULT_MAX_FINDINGS}; "
    "null removes the cap). When findings are dropped, omitted_findings is non-zero "
    "and findings_summary carries complete counts by kind, reason_code, doc path, "
    "and disposition; findings referenced by approval_required are always retained "
    "so approval evidence stays in-band. For large scopes prefer summary_only=true "
    "first, then re-query with a narrower scope or a higher cap if detail is "
    "needed. Interpretation note: on "
    'docstring findings, reason_code "unsupported.literal" means the docstring '
    "literal cannot serve as a deterministic anchor — including Args/Returns "
    "entries that are absent entirely — and does not by itself indicate a "
    "doc-code contradiction."
)
_MCP_SCOPE_SCHEMA = scope_spec_json_schema()
# MCP requires an explicit scope even though the library-level ScopeSpec keeps
# legacy `{}` -> changed input compatibility.
_MCP_SCOPE_SCHEMA["oneOf"][0]["required"] = ["kind"]
McpScopeSpec = Annotated[ScopeSpec, WithJsonSchema(_MCP_SCOPE_SCHEMA)]


class _McpToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    scope: McpScopeSpec
    semantic: bool = False
    max_findings: int | None = Field(default=DEFAULT_MAX_FINDINGS, ge=0)
    summary_only: bool = False


class DriftFastMCP(FastMCP[None]):
    """FastMCP with an explicit extra-forbid boundary for tool arguments."""

    async def list_tools(self) -> list[MCPTool]:
        tools = await super().list_tools()
        return [
            tool.model_copy(
                update={
                    "inputSchema": {
                        **tool.inputSchema,
                        "additionalProperties": False,
                    }
                }
            )
            if tool.name in _TOOL_ARGUMENTS
            else tool
            for tool in tools
        ]

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> Sequence[ContentBlock] | dict[str, Any]:
        allowed = _TOOL_ARGUMENTS.get(name)
        if allowed is not None:
            try:
                validated = _McpToolInput.model_validate(arguments)
            except ValidationError as error:
                raise ToolError("Invalid tool input") from error
            if "kind" not in validated.scope.model_fields_set:
                raise ToolError("Invalid tool input")
        result = await super().call_tool(name, arguments)
        if allowed is None or not isinstance(result, tuple) or len(result) != 2:
            return result
        _unstructured, structured = result
        if not isinstance(structured, dict):
            return result
        # FastMCP mirrors structured output as indent=2 TextContent; re-serialize
        # compactly so the payload is not carried twice at nearly double size.
        compact = pydantic_core.to_json(structured, fallback=str).decode()
        return cast(
            "Sequence[ContentBlock] | dict[str, Any]",
            ([TextContent(type="text", text=compact)], structured),
        )


def _request(
    *,
    mode: RunMode,
    repo_path: Path,
    scope: ScopeSpec,
    semantic: bool,
    state_dir: Path | None,
    lock_timeout_seconds: float,
) -> RunRequest:
    return RunRequest(
        mode=mode,
        repo_path=repo_path,
        scope=scope,
        state_dir=state_dir,
        lock_timeout_seconds=lock_timeout_seconds,
        apply_policy="docs_only",
        semantic_analysis=semantic if mode is RunMode.CHECK else False,
        semantic_repair=semantic if mode is RunMode.REPAIR else False,
    )


async def _execute(
    request: RunRequest,
    *,
    max_findings: int | None,
    summary_only: bool,
) -> PublicBundleV3:
    bundle = await asyncio.to_thread(application.run, request)
    return bounded_bundle(
        PublicBundleV3.from_bundle(bundle),
        max_findings=max_findings,
        summary_only=summary_only,
    )


def create_server(
    repo_path: Path,
    *,
    state_dir: Path | None = None,
    lock_timeout_seconds: float = 5.0,
) -> DriftFastMCP:
    """Create a stdio-oriented server bound to one repository.

    Args:
        repo_path (Path): Repository bound to both drift tools.
        state_dir (Path | None): Optional persistent state directory outside the worktree.
        lock_timeout_seconds (float): Maximum repair lock wait in seconds.

    Returns:
        DriftFastMCP: The configured stdio MCP server.
    """

    resolved_repo = repo_path.expanduser().resolve(strict=True)
    if not resolved_repo.is_dir():
        raise ValueError("MCP repository must be a directory")
    if not math.isfinite(lock_timeout_seconds) or lock_timeout_seconds < 0:
        raise ValueError("MCP lock timeout must be finite and non-negative")
    resolved_state_dir = None if state_dir is None else state_dir.expanduser().resolve(strict=False)
    identities = resolve_identities(resolved_repo)
    resolve_state_path(
        resolved_repo,
        resolved_state_dir,
        identities=identities,
    )
    server = DriftFastMCP(
        "doc-drift-agent",
        instructions=SERVER_INSTRUCTIONS,
    )

    @server.tool(structured_output=True)
    async def check_drift(
        scope: McpScopeSpec,
        semantic: bool = False,
        max_findings: int | None = DEFAULT_MAX_FINDINGS,
        summary_only: bool = False,
    ) -> PublicBundleV3:
        """Check drift without modifying repository source or documentation files.

        Findings beyond max_findings (null = unlimited) are dropped and counted
        in omitted_findings/findings_summary; summary_only=true inlines only
        findings referenced by approval_required, which are always retained.
        """

        return await _execute(
            _request(
                mode=RunMode.CHECK,
                repo_path=resolved_repo,
                scope=scope,
                semantic=semantic,
                state_dir=resolved_state_dir,
                lock_timeout_seconds=lock_timeout_seconds,
            ),
            max_findings=max_findings,
            summary_only=summary_only,
        )

    @server.tool(structured_output=True)
    async def repair_drift(
        scope: McpScopeSpec,
        semantic: bool = False,
        max_findings: int | None = DEFAULT_MAX_FINDINGS,
        summary_only: bool = False,
    ) -> PublicBundleV3:
        """Apply only verified documentation repairs within the requested Git scope.

        Findings beyond max_findings (null = unlimited) are dropped and counted
        in omitted_findings/findings_summary; summary_only=true inlines only
        findings referenced by approval_required, which are always retained.
        """

        return await _execute(
            _request(
                mode=RunMode.REPAIR,
                repo_path=resolved_repo,
                scope=scope,
                semantic=semantic,
                state_dir=resolved_state_dir,
                lock_timeout_seconds=lock_timeout_seconds,
            ),
            max_findings=max_findings,
            summary_only=summary_only,
        )

    return server


def _parser() -> argparse.ArgumentParser:
    def finite_non_negative(value: str) -> float:
        try:
            parsed = float(value)
        except ValueError as error:
            raise argparse.ArgumentTypeError("must be a number") from error
        if not math.isfinite(parsed) or parsed < 0:
            raise argparse.ArgumentTypeError("must be finite and non-negative")
        return parsed

    parser = argparse.ArgumentParser(description="Run the Doc-Code Drift MCP server")
    parser.add_argument(
        "--repo",
        required=True,
        type=Path,
        help="repository root to bind to both MCP tools",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help="optional state directory outside the version-controlled worktree",
    )
    parser.add_argument(
        "--lock-timeout-seconds",
        type=finite_non_negative,
        default=5.0,
        help="repair lock wait limit",
    )
    return parser


def main() -> None:
    parser = _parser()
    arguments = parser.parse_args()
    try:
        server = create_server(
            arguments.repo,
            state_dir=arguments.state_dir,
            lock_timeout_seconds=arguments.lock_timeout_seconds,
        )
    except (OSError, RuntimeError, ValueError) as error:
        parser.error(str(error))
    server.run(transport="stdio")


__all__ = ["create_server", "main"]


if __name__ == "__main__":
    main()

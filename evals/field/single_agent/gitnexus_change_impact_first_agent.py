"""Protocol-v4 arm invoking fixed GitNexus change impact on request one."""

from __future__ import annotations

from _portfolio_native_graph import (
    GITNEXUS_CHANGE_IMPACT_TOOLS,
    gitnexus_change_impact_runtime,
)
from _runner import (
    BASE_TOOLS,
    PORTFOLIO_GITNEXUS_FIRST_SYSTEM_PROMPT,
    TOOL_PORTFOLIO_GITNEXUS_FIRST_PROTOCOL_VERSION,
    AgentContext,
    AgentDefinition,
    AgentRuntime,
    main,
)


def gitnexus_change_impact_first_runtime(context: AgentContext) -> AgentRuntime:
    """Label this arm as the compact CLI profile, not GitNexus's MCP surface."""

    runtime = gitnexus_change_impact_runtime(context)
    runtime.metadata.update(
        {
            "retrieval_profile": "gitnexus-compact-cli-detect-changes",
            "provider_surface": "gitnexus-cli",
            "output_contract": (
                "complete-sanitized-native-cli-response; the CLI formatter may display "
                "only a subset of changed symbols or affected flows"
            ),
        }
    )
    return runtime

AGENT = AgentDefinition(
    name="gitnexus_change_impact_first_agent",
    tools=BASE_TOOLS + GITNEXUS_CHANGE_IMPACT_TOOLS,
    prepare=gitnexus_change_impact_first_runtime,
    protocol_version=TOOL_PORTFOLIO_GITNEXUS_FIRST_PROTOCOL_VERSION,
    system_prompt=PORTFOLIO_GITNEXUS_FIRST_SYSTEM_PROMPT,
)


if __name__ == "__main__":
    main(AGENT)

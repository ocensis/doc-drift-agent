"""Tool-portfolio arm using the provider-neutral GitNexus context surface."""

from __future__ import annotations

from _portfolio_graph import (
    GITNEXUS_CONTEXT_AGENT,
    GRAPH_CONTEXT_TOOLS,
    gitnexus_context_runtime,
)
from _runner import (
    BASE_TOOLS,
    PORTFOLIO_SYSTEM_PROMPT,
    TOOL_PORTFOLIO_PROTOCOL_VERSION,
    AgentDefinition,
    main,
)

AGENT = AgentDefinition(
    name=GITNEXUS_CONTEXT_AGENT,
    tools=BASE_TOOLS + GRAPH_CONTEXT_TOOLS,
    prepare=gitnexus_context_runtime,
    protocol_version=TOOL_PORTFOLIO_PROTOCOL_VERSION,
    system_prompt=PORTFOLIO_SYSTEM_PROMPT,
)


if __name__ == "__main__":
    main(AGENT)

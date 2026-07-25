"""Protocol-v3 portfolio arm exposing native CodeGraph ``explore``."""

from __future__ import annotations

from _portfolio_native_graph import (
    CODEGRAPH_EXPLORE_DIRECT_AGENT,
    CODEGRAPH_EXPLORE_DIRECT_TOOLS,
    codegraph_explore_direct_runtime,
)
from _runner import (
    BASE_TOOLS,
    PORTFOLIO_NATIVE_SYSTEM_PROMPT,
    TOOL_PORTFOLIO_NATIVE_PROTOCOL_VERSION,
    AgentDefinition,
    main,
)

AGENT = AgentDefinition(
    name=CODEGRAPH_EXPLORE_DIRECT_AGENT,
    tools=BASE_TOOLS + CODEGRAPH_EXPLORE_DIRECT_TOOLS,
    prepare=codegraph_explore_direct_runtime,
    protocol_version=TOOL_PORTFOLIO_NATIVE_PROTOCOL_VERSION,
    system_prompt=PORTFOLIO_NATIVE_SYSTEM_PROMPT,
)


if __name__ == "__main__":
    main(AGENT)

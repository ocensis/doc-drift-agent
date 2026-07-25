"""Protocol-v3 portfolio arm exposing fixed GitNexus change impact."""

from __future__ import annotations

from _portfolio_native_graph import (
    GITNEXUS_CHANGE_IMPACT_AGENT,
    GITNEXUS_CHANGE_IMPACT_TOOLS,
    gitnexus_change_impact_runtime,
)
from _runner import (
    BASE_TOOLS,
    PORTFOLIO_NATIVE_SYSTEM_PROMPT,
    TOOL_PORTFOLIO_NATIVE_PROTOCOL_VERSION,
    AgentDefinition,
    main,
)

AGENT = AgentDefinition(
    name=GITNEXUS_CHANGE_IMPACT_AGENT,
    tools=BASE_TOOLS + GITNEXUS_CHANGE_IMPACT_TOOLS,
    prepare=gitnexus_change_impact_runtime,
    protocol_version=TOOL_PORTFOLIO_NATIVE_PROTOCOL_VERSION,
    system_prompt=PORTFOLIO_NATIVE_SYSTEM_PROMPT,
)


if __name__ == "__main__":
    main(AGENT)

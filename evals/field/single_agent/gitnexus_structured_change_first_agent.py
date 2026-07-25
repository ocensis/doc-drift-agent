"""Protocol-v5 arm invoking official structured change on request one."""

from __future__ import annotations

from _portfolio_gitnexus_structured import (
    GITNEXUS_STRUCTURED_CHANGE_TOOLS,
    gitnexus_structured_change_runtime,
)
from _runner import (
    BASE_TOOLS,
    PORTFOLIO_GITNEXUS_STRUCTURED_FIRST_SYSTEM_PROMPT,
    TOOL_PORTFOLIO_GITNEXUS_STRUCTURED_FIRST_PROTOCOL_VERSION,
    AgentDefinition,
    main,
)

AGENT = AgentDefinition(
    name="gitnexus_structured_change_first_agent",
    tools=BASE_TOOLS + GITNEXUS_STRUCTURED_CHANGE_TOOLS,
    prepare=gitnexus_structured_change_runtime,
    protocol_version=TOOL_PORTFOLIO_GITNEXUS_STRUCTURED_FIRST_PROTOCOL_VERSION,
    system_prompt=PORTFOLIO_GITNEXUS_STRUCTURED_FIRST_SYSTEM_PROMPT,
)


if __name__ == "__main__":
    main(AGENT)

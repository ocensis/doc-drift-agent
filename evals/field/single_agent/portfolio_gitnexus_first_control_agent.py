"""Contemporaneous control for the protocol-v4 GitNexus-first stage."""

from _portfolio_generic import paged_generic_runtime
from _runner import (
    BASE_TOOLS,
    PORTFOLIO_GITNEXUS_FIRST_SYSTEM_PROMPT,
    TOOL_PORTFOLIO_GITNEXUS_FIRST_PROTOCOL_VERSION,
    AgentDefinition,
    main,
)

AGENT = AgentDefinition(
    name="portfolio_gitnexus_first_control_agent",
    tools=BASE_TOOLS,
    prepare=paged_generic_runtime,
    protocol_version=TOOL_PORTFOLIO_GITNEXUS_FIRST_PROTOCOL_VERSION,
    system_prompt=PORTFOLIO_GITNEXUS_FIRST_SYSTEM_PROMPT,
)


if __name__ == "__main__":
    main(AGENT)

"""Contemporaneous control for native graph tool-portfolio stages."""

from _portfolio_generic import paged_generic_runtime
from _runner import (
    BASE_TOOLS,
    PORTFOLIO_NATIVE_SYSTEM_PROMPT,
    TOOL_PORTFOLIO_NATIVE_PROTOCOL_VERSION,
    AgentDefinition,
    main,
)

AGENT = AgentDefinition(
    name="portfolio_native_control_agent",
    tools=BASE_TOOLS,
    prepare=paged_generic_runtime,
    protocol_version=TOOL_PORTFOLIO_NATIVE_PROTOCOL_VERSION,
    system_prompt=PORTFOLIO_NATIVE_SYSTEM_PROMPT,
)


if __name__ == "__main__":
    main(AGENT)

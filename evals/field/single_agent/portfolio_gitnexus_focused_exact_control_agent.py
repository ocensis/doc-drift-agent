"""Contemporaneous generic control for the focused GitNexus K=1 protocol."""

from _portfolio_generic import paged_generic_runtime
from _portfolio_gitnexus_focused_exact import (
    GITNEXUS_FOCUSED_EXACT_PROTOCOL_VERSION,
)
from _runner import BASE_TOOLS, AgentDefinition, main
from gitnexus_focused_exact_agent import GITNEXUS_FOCUSED_EXACT_SYSTEM_PROMPT

AGENT = AgentDefinition(
    name="portfolio_gitnexus_focused_exact_control_agent",
    tools=BASE_TOOLS,
    prepare=paged_generic_runtime,
    protocol_version=GITNEXUS_FOCUSED_EXACT_PROTOCOL_VERSION,
    system_prompt=GITNEXUS_FOCUSED_EXACT_SYSTEM_PROMPT,
)


if __name__ == "__main__":
    main(AGENT)

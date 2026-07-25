"""Single-Agent portfolio arm exposing only the documentation map."""

from __future__ import annotations

from _portfolio_brief import (
    AUDIT_BRIEF_TOOLS,
    DOC_MAP_PROFILE,
    portfolio_runtime,
)
from _runner import (
    BASE_TOOLS,
    PORTFOLIO_SYSTEM_PROMPT,
    TOOL_PORTFOLIO_PROTOCOL_VERSION,
    AgentDefinition,
    main,
)

AGENT = AgentDefinition(
    name="doc_map_agent",
    tools=BASE_TOOLS + AUDIT_BRIEF_TOOLS,
    prepare=lambda context: portfolio_runtime(context, DOC_MAP_PROFILE),
    protocol_version=TOOL_PORTFOLIO_PROTOCOL_VERSION,
    system_prompt=PORTFOLIO_SYSTEM_PROMPT,
)


if __name__ == "__main__":
    main(AGENT)

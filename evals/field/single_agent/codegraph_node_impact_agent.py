"""Candidate Agent exposing only the CodeGraph node+impact composite.

The definition intentionally reuses the protocol-v3 native-graph prompt and
base tool menu.  Runner manipulation, launch-matrix, and scorer registration
are pending integration by the root experiment owner; this file does not alter
those shared protocol surfaces and no model run is implied by importing it.
"""

from __future__ import annotations

from _portfolio_codegraph_node_impact import (
    CODEGRAPH_NODE_IMPACT_AGENT,
    CODEGRAPH_NODE_IMPACT_TOOLS,
    codegraph_node_impact_runtime,
)
from _runner import (
    BASE_TOOLS,
    PORTFOLIO_NATIVE_SYSTEM_PROMPT,
    TOOL_PORTFOLIO_NATIVE_PROTOCOL_VERSION,
    AgentDefinition,
    main,
)

PROMPT_COMPATIBILITY = "single-agent-tool-portfolio-v3-native-graph"
PROTOCOL_INTEGRATION_STATUS = "pending-root-agent"

AGENT = AgentDefinition(
    name=CODEGRAPH_NODE_IMPACT_AGENT,
    tools=BASE_TOOLS + CODEGRAPH_NODE_IMPACT_TOOLS,
    prepare=codegraph_node_impact_runtime,
    protocol_version=TOOL_PORTFOLIO_NATIVE_PROTOCOL_VERSION,
    system_prompt=PORTFOLIO_NATIVE_SYSTEM_PROMPT,
)


if __name__ == "__main__":
    main(AGENT)

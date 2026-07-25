"""Treatment arm: generic repository tools plus isolated GitNexus retrieval."""

from __future__ import annotations

from _graph_runtime import (
    GITNEXUS_AGENT,
    GITNEXUS_TOOLS,
    GRAPH_PROTOCOL_VERSION,
    gitnexus_runtime,
)
from _runner import BASE_TOOLS, AgentDefinition, main

AGENT = AgentDefinition(
    name=GITNEXUS_AGENT,
    tools=BASE_TOOLS + GITNEXUS_TOOLS,
    prepare=gitnexus_runtime,
    protocol_version=GRAPH_PROTOCOL_VERSION,
)


if __name__ == "__main__":
    main(AGENT)

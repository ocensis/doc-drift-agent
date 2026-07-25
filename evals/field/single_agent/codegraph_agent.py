"""Treatment arm: generic repository tools plus isolated CodeGraph retrieval."""

from __future__ import annotations

from _graph_runtime import (
    CODEGRAPH_AGENT,
    CODEGRAPH_TOOLS,
    GRAPH_PROTOCOL_VERSION,
    codegraph_runtime,
)
from _runner import BASE_TOOLS, AgentDefinition, main

AGENT = AgentDefinition(
    name=CODEGRAPH_AGENT,
    tools=BASE_TOOLS + CODEGRAPH_TOOLS,
    prepare=codegraph_runtime,
    protocol_version=GRAPH_PROTOCOL_VERSION,
)


if __name__ == "__main__":
    main(AGENT)

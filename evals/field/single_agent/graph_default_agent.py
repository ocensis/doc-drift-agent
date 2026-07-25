"""Control arm for the code-graph retrieval experiment."""

from __future__ import annotations

from _graph_runtime import GRAPH_DEFAULT_AGENT, GRAPH_PROTOCOL_VERSION, graph_default_runtime
from _runner import BASE_TOOLS, AgentDefinition, main

AGENT = AgentDefinition(
    name=GRAPH_DEFAULT_AGENT,
    tools=BASE_TOOLS,
    prepare=graph_default_runtime,
    protocol_version=GRAPH_PROTOCOL_VERSION,
)


if __name__ == "__main__":
    main(AGENT)

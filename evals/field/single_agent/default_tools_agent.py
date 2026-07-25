"""Control arm: one Agent with only generic repository and git tools."""

from __future__ import annotations

from _runner import BASE_TOOLS, AgentDefinition, default_runtime, main

AGENT = AgentDefinition(
    name="default_tools_agent",
    tools=BASE_TOOLS,
    prepare=default_runtime,
)


if __name__ == "__main__":
    main(AGENT)

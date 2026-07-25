"""Single-Agent arm with GitNexus' full official structured change result."""

from __future__ import annotations

from _portfolio_gitnexus_structured import (
    GITNEXUS_STRUCTURED_CHANGE_AGENT,
    GITNEXUS_STRUCTURED_CHANGE_TOOLS,
    gitnexus_structured_change_runtime,
)
from _runner import (
    BASE_TOOLS,
    COMMON_SYSTEM_PROMPT,
    TOOL_PORTFOLIO_NATIVE_PROTOCOL_VERSION,
    AgentDefinition,
    main,
)

GITNEXUS_STRUCTURED_SYSTEM_PROMPT = (
    COMMON_SYSTEM_PROMPT
    + """\

Work efficiently without sacrificing coverage. Batch independent tool calls in
the same turn when possible, prefer path-scoped or resumable repository reads,
and do not fetch the same content again when it is already in the conversation.
If gitnexus_structured_change is available, call it exactly once after inspecting
the diff and before your first repository-wide grep or list_dir. It performs a
runtime-bound baseline-to-HEAD comparison and returns the provider's complete
structured changed-symbol and affected-process arrays. The result is a lead:
verify documentation quotes and current-code claims against repository files.
"""
)

AGENT = AgentDefinition(
    name=GITNEXUS_STRUCTURED_CHANGE_AGENT,
    tools=BASE_TOOLS + GITNEXUS_STRUCTURED_CHANGE_TOOLS,
    prepare=gitnexus_structured_change_runtime,
    protocol_version=TOOL_PORTFOLIO_NATIVE_PROTOCOL_VERSION,
    system_prompt=GITNEXUS_STRUCTURED_SYSTEM_PROMPT,
)


if __name__ == "__main__":
    main(AGENT)

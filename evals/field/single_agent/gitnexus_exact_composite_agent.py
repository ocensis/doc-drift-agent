"""Candidate Agent with GitNexus' structured K=1 exact composite."""

from __future__ import annotations

from _portfolio_gitnexus_exact_composite import (
    GITNEXUS_EXACT_COMPOSITE_AGENT,
    GITNEXUS_EXACT_COMPOSITE_PROTOCOL_VERSION,
    GITNEXUS_EXACT_COMPOSITE_TOOLS,
    gitnexus_exact_composite_runtime,
)
from _runner import BASE_TOOLS, COMMON_SYSTEM_PROMPT, AgentDefinition, main

GITNEXUS_EXACT_COMPOSITE_SYSTEM_PROMPT = (
    COMMON_SYSTEM_PROMPT
    + """\

Work efficiently without sacrificing coverage. Batch independent tool calls in
the same turn when possible, prefer path-scoped or resumable repository reads,
and do not fetch the same content again when it is already in the conversation.
If gitnexus_exact_composite is available, call it exactly once after inspecting
the diff and before your first repository-wide grep or list_dir. Its no-argument
result clearly separates the complete fixed detect_changes payload from a
deterministic K=1 exact-symbol context/impact enrichment and conditional flow
details. These are leads: verify documentation quotes and current-code claims
against repository files before submit.

This is a candidate protocol pending paired-stage runner/scorer integration; it
does not alter the semantics of any existing portfolio Agent.
"""
)

AGENT = AgentDefinition(
    name=GITNEXUS_EXACT_COMPOSITE_AGENT,
    tools=BASE_TOOLS + GITNEXUS_EXACT_COMPOSITE_TOOLS,
    prepare=gitnexus_exact_composite_runtime,
    protocol_version=GITNEXUS_EXACT_COMPOSITE_PROTOCOL_VERSION,
    system_prompt=GITNEXUS_EXACT_COMPOSITE_SYSTEM_PROMPT,
)


if __name__ == "__main__":
    main(AGENT)

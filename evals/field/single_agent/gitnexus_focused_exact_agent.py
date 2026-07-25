"""Candidate Agent using the focused GitNexus K=1 exact renderer."""

from __future__ import annotations

from _portfolio_gitnexus_focused_exact import (
    GITNEXUS_FOCUSED_EXACT_AGENT,
    GITNEXUS_FOCUSED_EXACT_PROTOCOL_VERSION,
    GITNEXUS_FOCUSED_EXACT_TOOLS,
    gitnexus_focused_exact_runtime,
)
from _runner import BASE_TOOLS, COMMON_SYSTEM_PROMPT, AgentDefinition, main

GITNEXUS_FOCUSED_EXACT_SYSTEM_PROMPT = (
    COMMON_SYSTEM_PROMPT
    + """\

Work efficiently without sacrificing coverage. If gitnexus_focused_exact is
available, call it exactly once after inspecting the diff and before your first
repository-wide grep or list_dir. It provides one exact-symbol graph focus, not
an exhaustive candidate list: heed its omitted counts and non-coverage notice,
then use repository tools to cover all documentation and changed-code regions.
Verify documentation quotes and current-code claims against repository files
before submit.

This is an independent candidate protocol pending paired-stage integration; it
does not alter any existing portfolio Agent.
"""
)

AGENT = AgentDefinition(
    name=GITNEXUS_FOCUSED_EXACT_AGENT,
    tools=BASE_TOOLS + GITNEXUS_FOCUSED_EXACT_TOOLS,
    prepare=gitnexus_focused_exact_runtime,
    protocol_version=GITNEXUS_FOCUSED_EXACT_PROTOCOL_VERSION,
    system_prompt=GITNEXUS_FOCUSED_EXACT_SYSTEM_PROMPT,
)


if __name__ == "__main__":
    main(AGENT)

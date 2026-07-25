"""Protocol-v3 forward child combining CodeGraph explore with change_seed."""

from __future__ import annotations

import time

from _portfolio_brief import (
    AUDIT_BRIEF_TOOLS,
    CHANGE_SEED_PROFILE,
    attach_audit_brief,
)
from _portfolio_native_graph import (
    CODEGRAPH_EXPLORE_DIRECT_AGENT,
    CODEGRAPH_EXPLORE_DIRECT_TOOLS,
    codegraph_explore_direct_runtime,
)
from _runner import (
    BASE_TOOLS,
    PORTFOLIO_NATIVE_SYSTEM_PROMPT,
    TOOL_PORTFOLIO_NATIVE_PROTOCOL_VERSION,
    AgentContext,
    AgentDefinition,
    AgentRuntime,
    main,
)

CODEGRAPH_EXPLORE_CHANGE_SEED_AGENT = "codegraph_explore_change_seed_agent"
CODEGRAPH_EXPLORE_CHANGE_SEED_PROFILE = "codegraph_native_change_seed"


def codegraph_explore_change_seed_runtime(context: AgentContext) -> AgentRuntime:
    """Attach the existing change seed to the single native CodeGraph runtime."""

    setup_started = time.monotonic()
    runtime = codegraph_explore_direct_runtime(context)
    try:
        attach_audit_brief(
            runtime,
            context,
            CHANGE_SEED_PROFILE,
            profile_id=CODEGRAPH_EXPLORE_CHANGE_SEED_PROFILE,
            base_metadata_prefix="graph",
            setup_started=setup_started,
        )
        runtime.metadata.update(
            {
                "parent_agent": CODEGRAPH_EXPLORE_DIRECT_AGENT,
                "brief_profile_id": CHANGE_SEED_PROFILE.profile_id,
                "incremental_tool_surface": list(AUDIT_BRIEF_TOOLS),
                "runtime_composition": {
                    "shared_toolbox": True,
                    "shared_generic_runtime": True,
                    "shared_codegraph_clone": True,
                    "cleanup_callback_reused": True,
                },
            }
        )
    except Exception:
        if runtime.close is not None:
            try:
                runtime.close()
            except Exception:
                pass
        raise
    return runtime


AGENT = AgentDefinition(
    name=CODEGRAPH_EXPLORE_CHANGE_SEED_AGENT,
    tools=BASE_TOOLS + CODEGRAPH_EXPLORE_DIRECT_TOOLS + AUDIT_BRIEF_TOOLS,
    prepare=codegraph_explore_change_seed_runtime,
    protocol_version=TOOL_PORTFOLIO_NATIVE_PROTOCOL_VERSION,
    system_prompt=PORTFOLIO_NATIVE_SYSTEM_PROMPT,
)


if __name__ == "__main__":
    main(AGENT)

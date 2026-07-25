"""The fixed detect → repair → validate pipeline the harness drives.

This replaces a five-node LangGraph ``StateGraph``.  The graph never used
cycles, checkpointers, interrupts or concurrent nodes: it declared one linear
path with a single conditional edge, which is the whole of :func:`run_pipeline`
below.  Keeping the framework meant a dependency and an indirection for control
flow that fits in a dozen lines, so the flow now lives here in plain Python.

The one behaviour worth naming: LangGraph merges each node's returned mapping
into the running state rather than replacing it, and the nodes rely on that —
several return only the keys they touched.  ``run_pipeline`` reproduces exactly
that (last write wins per key), so node implementations are unchanged.
"""

from __future__ import annotations

from typing import Literal, Protocol

from drift_agent.agent.state import AgentState
from drift_agent.domain.enums import FindingDisposition, RunMode


class RuntimeNodes(Protocol):
    def scope_node(self, state: AgentState) -> AgentState: ...

    def evidence_node(self, state: AgentState) -> AgentState: ...

    def plan_node(self, state: AgentState) -> AgentState: ...

    def apply_validate_node(self, state: AgentState) -> AgentState: ...

    def finalize_node(self, state: AgentState) -> AgentState: ...


def route_after_evidence(state: AgentState) -> Literal["finalize", "plan"]:
    if not state.get("findings") or state["request"].mode is RunMode.CHECK:
        return "finalize"
    if any(finding.disposition is FindingDisposition.DETECTED for finding in state["findings"]):
        return "plan"
    return "finalize"


def run_pipeline(runtime: RuntimeNodes, initial: AgentState) -> AgentState:
    """Run scope → evidence → (plan → apply_validate)? → finalize."""

    state: AgentState = dict(initial)  # type: ignore[assignment]
    state.update(runtime.scope_node(state))
    state.update(runtime.evidence_node(state))
    if route_after_evidence(state) == "plan":
        state.update(runtime.plan_node(state))
        state.update(runtime.apply_validate_node(state))
    state.update(runtime.finalize_node(state))
    return state

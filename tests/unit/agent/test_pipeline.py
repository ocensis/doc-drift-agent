from types import SimpleNamespace
from typing import cast

from drift_agent.agent.pipeline import route_after_evidence, run_pipeline
from drift_agent.agent.state import AgentState
from drift_agent.domain.enums import FindingDisposition, RunMode
from drift_agent.domain.models import RunRequest


def test_route_clean_check_and_repair() -> None:
    check = RunRequest(mode=RunMode.CHECK, repo_path=".")
    repair = RunRequest(mode=RunMode.REPAIR, repo_path=".")
    detected = SimpleNamespace(disposition=FindingDisposition.DETECTED)
    approval = SimpleNamespace(disposition=FindingDisposition.NEEDS_APPROVAL)

    assert route_after_evidence(cast(AgentState, {"request": check, "findings": []})) == "finalize"
    assert (
        route_after_evidence(cast(AgentState, {"request": check, "findings": [detected]}))
        == "finalize"
    )
    assert (
        route_after_evidence(cast(AgentState, {"request": repair, "findings": [detected]}))
        == "plan"
    )
    assert (
        route_after_evidence(cast(AgentState, {"request": repair, "findings": [approval]}))
        == "finalize"
    )


class _RecordingRuntime:
    """Nodes that return partial updates, the way the real runtime does."""

    def __init__(self) -> None:
        self.visited: list[str] = []

    def _step(self, name: str, **update: object) -> AgentState:
        self.visited.append(name)
        return cast(AgentState, update)

    def scope_node(self, state: AgentState) -> AgentState:
        return self._step("scope", changed_paths=["docs/guide.md"])

    def evidence_node(self, state: AgentState) -> AgentState:
        return self._step("evidence", findings=state.get("findings", []))

    def plan_node(self, state: AgentState) -> AgentState:
        return self._step("plan", attempts=[])

    def apply_validate_node(self, state: AgentState) -> AgentState:
        return self._step("apply_validate", validation=[])

    def finalize_node(self, state: AgentState) -> AgentState:
        return self._step("finalize", stale=False)


def test_check_mode_skips_the_repair_leg() -> None:
    runtime = _RecordingRuntime()
    request = RunRequest(mode=RunMode.CHECK, repo_path=".")

    result = run_pipeline(runtime, cast(AgentState, {"request": request}))

    assert runtime.visited == ["scope", "evidence", "finalize"]
    assert result["changed_paths"] == ["docs/guide.md"]


def test_a_detected_finding_in_repair_mode_runs_plan_and_validate() -> None:
    runtime = _RecordingRuntime()
    request = RunRequest(mode=RunMode.REPAIR, repo_path=".")
    detected = SimpleNamespace(disposition=FindingDisposition.DETECTED)

    run_pipeline(runtime, cast(AgentState, {"request": request, "findings": [detected]}))

    assert runtime.visited == ["scope", "evidence", "plan", "apply_validate", "finalize"]


def test_partial_node_updates_accumulate_instead_of_replacing_state() -> None:
    """The nodes rely on LangGraph's merge semantics; the plain loop keeps them."""

    runtime = _RecordingRuntime()
    request = RunRequest(mode=RunMode.REPAIR, repo_path=".")
    detected = SimpleNamespace(disposition=FindingDisposition.DETECTED)

    result = run_pipeline(runtime, cast(AgentState, {"request": request, "findings": [detected]}))

    # Written by four different nodes; none of them saw the others' keys.
    assert result["request"] is request
    assert result["changed_paths"] == ["docs/guide.md"]
    assert result["attempts"] == []
    assert result["validation"] == []
    assert result["stale"] is False

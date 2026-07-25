from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

SINGLE_AGENT_DIR = Path(__file__).resolve().parents[3] / "evals" / "field" / "single_agent"
sys.path.insert(0, str(SINGLE_AGENT_DIR))

import run_tool_portfolio as launcher  # noqa: E402


def test_launcher_registers_independent_alignment_map_agent() -> None:
    assert launcher.AGENT_FILES["alignment_map_agent"] == "alignment_map_agent.py"
    assert (
        launcher.AGENT_FILES["codegraph_explore_change_seed_agent"]
        == "codegraph_explore_change_seed_agent.py"
    )
    assert (
        launcher.AGENT_FILES["codegraph_node_impact_agent"]
        == "codegraph_node_impact_agent.py"
    )
    assert launcher.AGENT_FILES["gitnexus_focused_exact_agent"] == (
        "gitnexus_focused_exact_agent.py"
    )
    assert launcher.AGENT_FILES[launcher.GITNEXUS_FOCUSED_EXACT_CONTROL_AGENT] == (
        "portfolio_gitnexus_focused_exact_control_agent.py"
    )


def test_schedule_rotates_agents_and_always_keeps_contemporaneous_control() -> None:
    agents = (
        launcher.CONTROL_AGENT,
        "brief_diff_agent",
        "codegraph_context_agent",
    )
    schedule = launcher._schedule(agents, 3, "s2")

    assert schedule[:3] == (
        ("s2-1", launcher.CONTROL_AGENT),
        ("s2-1", "brief_diff_agent"),
        ("s2-1", "codegraph_context_agent"),
    )
    assert schedule[3:6] == (
        ("s2-2", "brief_diff_agent"),
        ("s2-2", "codegraph_context_agent"),
        ("s2-2", launcher.CONTROL_AGENT),
    )
    for pair in ("s2-1", "s2-2", "s2-3"):
        assert (pair, launcher.CONTROL_AGENT) in schedule


def test_schedule_rejects_missing_control_and_duplicates() -> None:
    with pytest.raises(ValueError, match="contemporaneous"):
        launcher._schedule(("brief_diff_agent",), 1, "screen")
    with pytest.raises(ValueError, match="duplicates"):
        launcher._schedule(
            (launcher.CONTROL_AGENT, launcher.CONTROL_AGENT),
            1,
            "screen",
        )


def test_schedule_binds_treatments_to_their_protocol_control() -> None:
    native = launcher._schedule(
        (
            launcher.NATIVE_CONTROL_AGENT,
            "codegraph_explore_direct_agent",
            "codegraph_node_impact_agent",
            "gitnexus_change_impact_agent",
        ),
        1,
        "native",
    )
    assert ("native-1", launcher.NATIVE_CONTROL_AGENT) in native

    with pytest.raises(ValueError, match="native graph treatments require"):
        launcher._schedule(
            (launcher.CONTROL_AGENT, "codegraph_explore_direct_agent"),
            1,
            "wrong",
        )
    with pytest.raises(ValueError, match="legacy treatments require"):
        launcher._schedule(
            (launcher.NATIVE_CONTROL_AGENT, "brief_diff_agent"),
            1,
            "wrong",
        )

    gitnexus_first = launcher._schedule(
        (
            launcher.GITNEXUS_FIRST_CONTROL_AGENT,
            "gitnexus_change_impact_first_agent",
        ),
        1,
        "gitnexus-first",
    )
    assert (
        "gitnexus-first-1",
        launcher.GITNEXUS_FIRST_CONTROL_AGENT,
    ) in gitnexus_first
    with pytest.raises(ValueError, match="GitNexus-first treatments require"):
        launcher._schedule(
            (launcher.NATIVE_CONTROL_AGENT, "gitnexus_change_impact_first_agent"),
            1,
            "wrong",
        )

    structured_first = launcher._schedule(
        (
            launcher.GITNEXUS_STRUCTURED_FIRST_CONTROL_AGENT,
            "gitnexus_structured_change_first_agent",
        ),
        1,
        "structured-first",
    )
    assert (
        "structured-first-1",
        launcher.GITNEXUS_STRUCTURED_FIRST_CONTROL_AGENT,
    ) in structured_first
    with pytest.raises(ValueError, match="GitNexus-structured-first treatments require"):
        launcher._schedule(
            (launcher.GITNEXUS_FIRST_CONTROL_AGENT, "gitnexus_structured_change_first_agent"),
            1,
            "wrong",
        )

    focused_exact = launcher._schedule(
        (
            launcher.GITNEXUS_FOCUSED_EXACT_CONTROL_AGENT,
            "gitnexus_focused_exact_agent",
        ),
        1,
        "focused-exact",
    )
    assert (
        "focused-exact-1",
        launcher.GITNEXUS_FOCUSED_EXACT_CONTROL_AGENT,
    ) in focused_exact
    with pytest.raises(ValueError, match="GitNexus-focused-exact treatments require"):
        launcher._schedule(
            (launcher.NATIVE_CONTROL_AGENT, "gitnexus_focused_exact_agent"),
            1,
            "wrong",
        )
    with pytest.raises(ValueError, match="native graph treatments require"):
        launcher._schedule(
            (
                launcher.GITNEXUS_FOCUSED_EXACT_CONTROL_AGENT,
                "gitnexus_focused_exact_agent",
                "codegraph_node_impact_agent",
            ),
            1,
            "wrong",
        )


def test_child_parent_map_is_prevalidated_against_the_same_stage() -> None:
    agents = (
        launcher.CONTROL_AGENT,
        "brief_diff_agent",
        "doc_map_agent",
    )
    assert launcher._parse_child_parent_map(
        ["doc_map_agent=brief_diff_agent"],
        agents,
    ) == {"doc_map_agent": "brief_diff_agent"}

    invalid = (
        ["doc_map_agent"],
        ["doc_map_agent=missing_agent"],
        [f"doc_map_agent={launcher.CONTROL_AGENT}"],
        ["doc_map_agent=doc_map_agent"],
        ["doc_map_agent=brief_diff_agent", "doc_map_agent=brief_diff_agent"],
        ["doc_map_agent=brief_diff_agent", "brief_diff_agent=doc_map_agent"],
    )
    for registrations in invalid:
        with pytest.raises(ValueError):
            launcher._parse_child_parent_map(registrations, agents)


def test_codegraph_change_seed_requires_its_exact_forward_parent() -> None:
    child = "codegraph_explore_change_seed_agent"
    parent = "codegraph_explore_direct_agent"
    agents = (launcher.NATIVE_CONTROL_AGENT, parent, child)

    assert launcher._parse_child_parent_map(
        [f"{child}={parent}"],
        agents,
    ) == {child: parent}
    with pytest.raises(ValueError, match="requires --child-parent"):
        launcher._parse_child_parent_map(None, agents)
    with pytest.raises(ValueError, match="requires planned parent"):
        launcher._parse_child_parent_map(
            None,
            (launcher.NATIVE_CONTROL_AGENT, child),
        )


def test_child_environment_requires_exact_streamlake_and_disables_observability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_PROVIDER", "streamlake")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "not-forwarded")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "not-forwarded")
    child = launcher._child_environment()

    assert child["OPENROUTER_PROVIDER"] == "streamlake"
    assert child["OPENROUTER_BASE_URL"] == launcher.OPENROUTER_BASE_URL
    assert "LANGFUSE_PUBLIC_KEY" not in child
    assert "LANGFUSE_SECRET_KEY" not in child

    monkeypatch.setenv("OPENROUTER_PROVIDER", "other")
    with pytest.raises(ValueError, match="streamlake"):
        launcher._child_environment()


def test_child_environment_rejects_nonofficial_openrouter_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_PROVIDER", "streamlake")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://proxy.invalid/v1")

    with pytest.raises(ValueError, match="OPENROUTER_BASE_URL"):
        launcher._child_environment()


def test_output_directory_must_be_new_and_is_claimed_exclusively(tmp_path: Path) -> None:
    output = tmp_path / "fresh-stage"

    assert launcher._create_exclusive_output_dir(output) == output.resolve()
    assert output.is_dir()
    with pytest.raises(ValueError, match="must be new"):
        launcher._create_exclusive_output_dir(output)

    preexisting_empty = tmp_path / "already-empty"
    preexisting_empty.mkdir()
    with pytest.raises(ValueError, match="must be new"):
        launcher._create_exclusive_output_dir(preexisting_empty)


def test_launch_manifest_records_frozen_artifact_and_end_to_end_timestamps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_PROVIDER", "streamlake")
    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)

    class CompletedProcess:
        pid = 1234

        @staticmethod
        def wait() -> int:
            return 0

    def fake_popen(argv: list[str], **_kwargs: object) -> CompletedProcess:
        output = Path(argv[argv.index("--output") + 1])
        output.write_text(json.dumps({"complete": True}), encoding="utf-8")
        return CompletedProcess()

    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)
    arguments = argparse.Namespace(
        agents=[launcher.CONTROL_AGENT],
        pairs=1,
        pair_prefix="unit",
        fixture=tmp_path / "fixture",
        repo=None,
        baseline=None,
        output_dir=tmp_path / "new-stage",
        python=Path(sys.executable),
        authorization_ref="unit-test-authorization",
    )

    manifest = launcher.launch(arguments)
    job = manifest["jobs"][0]

    assert manifest["planned_agents"] == [launcher.CONTROL_AGENT]
    assert manifest["planned_pairs"] == ["unit-1"]
    assert manifest["child_parent_map"] == {}
    assert manifest["job_count"] == 1
    assert job["returncode"] == 0
    assert job["artifact_snapshot"]["exists"] is True
    assert (
        job["popen_started_at_ns"]
        <= job["child_exited_at_ns"]
        <= job["artifact_snapshot_at_ns"]
        <= manifest["artifact_frozen_at_ns"]
    )
    assert job["end_to_end_wall_seconds"] == round(
        (job["artifact_snapshot_at_ns"] - job["popen_started_at_ns"]) / 1e9,
        6,
    )

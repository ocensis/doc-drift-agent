"""Launch one leak-free tool-portfolio generation stage.

The launcher has no ground-truth argument, starts every planned process before
waiting, and imposes no process timeout. Each child still owns exactly one
persistent Agent conversation and one terminal submit.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

CONTROL_AGENT = "portfolio_control_agent"
NATIVE_CONTROL_AGENT = "portfolio_native_control_agent"
GITNEXUS_FIRST_CONTROL_AGENT = "portfolio_gitnexus_first_control_agent"
GITNEXUS_STRUCTURED_FIRST_CONTROL_AGENT = (
    "portfolio_gitnexus_structured_first_control_agent"
)
GITNEXUS_FOCUSED_EXACT_CONTROL_AGENT = (
    "portfolio_gitnexus_focused_exact_control_agent"
)
CONTROL_AGENTS = frozenset(
    {
        CONTROL_AGENT,
        NATIVE_CONTROL_AGENT,
        GITNEXUS_FIRST_CONTROL_AGENT,
        GITNEXUS_STRUCTURED_FIRST_CONTROL_AGENT,
        GITNEXUS_FOCUSED_EXACT_CONTROL_AGENT,
    }
)
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
AGENT_FILES = {
    CONTROL_AGENT: "portfolio_control_agent.py",
    NATIVE_CONTROL_AGENT: "portfolio_native_control_agent.py",
    GITNEXUS_FIRST_CONTROL_AGENT: "portfolio_gitnexus_first_control_agent.py",
    GITNEXUS_STRUCTURED_FIRST_CONTROL_AGENT: (
        "portfolio_gitnexus_structured_first_control_agent.py"
    ),
    GITNEXUS_FOCUSED_EXACT_CONTROL_AGENT: (
        "portfolio_gitnexus_focused_exact_control_agent.py"
    ),
    "brief_diff_agent": "brief_diff_agent.py",
    "doc_map_agent": "doc_map_agent.py",
    "change_seed_agent": "change_seed_agent.py",
    "alignment_map_agent": "alignment_map_agent.py",
    "brief_diff_doc_map_agent": "brief_diff_doc_map_agent.py",
    "codegraph_context_agent": "codegraph_context_agent.py",
    "gitnexus_context_agent": "gitnexus_context_agent.py",
    "codegraph_explore_direct_agent": "codegraph_explore_direct_agent.py",
    "codegraph_explore_change_seed_agent": "codegraph_explore_change_seed_agent.py",
    "codegraph_node_impact_agent": "codegraph_node_impact_agent.py",
    "gitnexus_change_impact_agent": "gitnexus_change_impact_agent.py",
    "gitnexus_change_impact_first_agent": "gitnexus_change_impact_first_agent.py",
    "gitnexus_structured_change_first_agent": "gitnexus_structured_change_first_agent.py",
    "gitnexus_focused_exact_agent": "gitnexus_focused_exact_agent.py",
}
NATIVE_AGENTS = frozenset(
    {
        NATIVE_CONTROL_AGENT,
        "codegraph_explore_direct_agent",
        "codegraph_explore_change_seed_agent",
        "codegraph_node_impact_agent",
        "gitnexus_change_impact_agent",
    }
)
EXPECTED_FORWARD_CHILD_PARENTS = {
    "codegraph_explore_change_seed_agent": "codegraph_explore_direct_agent",
}
GITNEXUS_FIRST_AGENTS = frozenset(
    {
        GITNEXUS_FIRST_CONTROL_AGENT,
        "gitnexus_change_impact_first_agent",
    }
)
GITNEXUS_STRUCTURED_FIRST_AGENTS = frozenset(
    {
        GITNEXUS_STRUCTURED_FIRST_CONTROL_AGENT,
        "gitnexus_structured_change_first_agent",
    }
)
GITNEXUS_FOCUSED_EXACT_AGENTS = frozenset(
    {
        GITNEXUS_FOCUSED_EXACT_CONTROL_AGENT,
        "gitnexus_focused_exact_agent",
    }
)


@dataclass(slots=True)
class RunningJob:
    schedule_ordinal: int
    pair_id: str
    agent: str
    artifact: Path
    stdout_path: Path
    stderr_path: Path
    process: subprocess.Popen[str]
    stdout_file: IO[str]
    stderr_file: IO[str]
    popen_started_at_ns: int


def _schedule(agents: tuple[str, ...], pairs: int, pair_prefix: str) -> tuple[tuple[str, str], ...]:
    if pairs < 1:
        raise ValueError("--pairs must be positive")
    if len(set(agents)) != len(agents):
        raise ValueError("--agents must not contain duplicates")
    unknown = sorted(set(agents) - set(AGENT_FILES))
    if unknown:
        raise ValueError(f"unknown tool-portfolio agents: {', '.join(unknown)}")
    controls = CONTROL_AGENTS.intersection(agents)
    if len(controls) != 1:
        expected = " or ".join(sorted(CONTROL_AGENTS))
        raise ValueError(f"every stage must include exactly one contemporaneous {expected}")
    selected_control = next(iter(controls))
    has_native_treatment = bool((set(agents) & NATIVE_AGENTS) - {NATIVE_CONTROL_AGENT})
    has_gitnexus_first_treatment = bool(
        (set(agents) & GITNEXUS_FIRST_AGENTS) - {GITNEXUS_FIRST_CONTROL_AGENT}
    )
    has_gitnexus_structured_first_treatment = bool(
        (set(agents) & GITNEXUS_STRUCTURED_FIRST_AGENTS)
        - {GITNEXUS_STRUCTURED_FIRST_CONTROL_AGENT}
    )
    has_gitnexus_focused_exact_treatment = bool(
        (set(agents) & GITNEXUS_FOCUSED_EXACT_AGENTS)
        - {GITNEXUS_FOCUSED_EXACT_CONTROL_AGENT}
    )
    has_legacy_treatment = bool(
        set(agents)
        - NATIVE_AGENTS
        - GITNEXUS_FIRST_AGENTS
        - GITNEXUS_STRUCTURED_FIRST_AGENTS
        - GITNEXUS_FOCUSED_EXACT_AGENTS
        - {CONTROL_AGENT}
    )
    if has_native_treatment and selected_control != NATIVE_CONTROL_AGENT:
        raise ValueError("native graph treatments require portfolio_native_control_agent")
    if (
        has_gitnexus_first_treatment
        and selected_control != GITNEXUS_FIRST_CONTROL_AGENT
    ):
        raise ValueError(
            "GitNexus-first treatments require portfolio_gitnexus_first_control_agent"
        )
    if (
        has_gitnexus_structured_first_treatment
        and selected_control != GITNEXUS_STRUCTURED_FIRST_CONTROL_AGENT
    ):
        raise ValueError(
            "GitNexus-structured-first treatments require "
            "portfolio_gitnexus_structured_first_control_agent"
        )
    if (
        has_gitnexus_focused_exact_treatment
        and selected_control != GITNEXUS_FOCUSED_EXACT_CONTROL_AGENT
    ):
        raise ValueError(
            "GitNexus-focused-exact treatments require "
            "portfolio_gitnexus_focused_exact_control_agent"
        )
    if has_legacy_treatment and selected_control != CONTROL_AGENT:
        raise ValueError("legacy treatments require portfolio_control_agent")
    selected_treatment_families = sum(
        (
            has_native_treatment,
            has_gitnexus_first_treatment,
            has_gitnexus_structured_first_treatment,
            has_gitnexus_focused_exact_treatment,
            has_legacy_treatment,
        )
    )
    if selected_treatment_families > 1:
        raise ValueError("one stage cannot mix treatment protocol families")
    expected_control = {
        NATIVE_CONTROL_AGENT: has_native_treatment,
        GITNEXUS_FIRST_CONTROL_AGENT: has_gitnexus_first_treatment,
        GITNEXUS_STRUCTURED_FIRST_CONTROL_AGENT: (
            has_gitnexus_structured_first_treatment
        ),
        GITNEXUS_FOCUSED_EXACT_CONTROL_AGENT: has_gitnexus_focused_exact_treatment,
        CONTROL_AGENT: has_legacy_treatment,
    }[selected_control]
    if selected_treatment_families and not expected_control:
        raise ValueError("stage treatment protocol does not match its selected control")
    allowed_pair_characters = "abcdefghijklmnopqrstuvwxyz0123456789-_"
    if not pair_prefix or any(
        character not in allowed_pair_characters for character in pair_prefix
    ):
        raise ValueError("--pair-prefix must use lowercase letters, digits, '-' or '_'")
    scheduled: list[tuple[str, str]] = []
    for index in range(pairs):
        offset = index % len(agents)
        rotated = (*agents[offset:], *agents[:offset])
        pair_id = f"{pair_prefix}-{index + 1}"
        scheduled.extend((pair_id, agent) for agent in rotated)
    return tuple(scheduled)


def _parse_child_parent_map(
    values: list[str] | tuple[str, ...] | None,
    agents: tuple[str, ...],
) -> dict[str, str]:
    """Validate and normalize optional pre-registered forward-search ancestry."""

    controls = CONTROL_AGENTS.intersection(agents)
    if len(controls) != 1:
        raise ValueError("child-parent mappings require exactly one stage control")
    control = next(iter(controls))
    planned = set(agents)
    mapping: dict[str, str] = {}
    for raw in values or ():
        if not isinstance(raw, str) or raw.count("=") != 1:
            raise ValueError("--child-parent must use CHILD=PARENT")
        child, parent = (part.strip() for part in raw.split("=", 1))
        if not child or not parent:
            raise ValueError("--child-parent must use non-empty CHILD=PARENT names")
        if child in mapping:
            raise ValueError(f"child {child!r} has multiple parent registrations")
        if child not in planned or parent not in planned:
            raise ValueError("child and parent must both be planned agents in this stage")
        if child == control or parent == control:
            raise ValueError("child-parent mappings cannot use the control agent")
        if child == parent:
            raise ValueError("child and parent must be different agents")
        mapping[child] = parent

    for start in mapping:
        seen: set[str] = set()
        current = start
        while current in mapping:
            if current in seen:
                raise ValueError("child-parent mappings must be acyclic")
            seen.add(current)
            current = mapping[current]

    for child, expected_parent in EXPECTED_FORWARD_CHILD_PARENTS.items():
        if child not in planned:
            continue
        if expected_parent not in planned:
            raise ValueError(
                f"forward child {child!r} requires planned parent {expected_parent!r}"
            )
        if mapping.get(child) != expected_parent:
            raise ValueError(
                f"forward child {child!r} requires --child-parent "
                f"{child}={expected_parent}"
            )
    return dict(sorted(mapping.items()))


def _target_arguments(arguments: argparse.Namespace) -> list[str]:
    if arguments.fixture is not None:
        return ["--fixture", str(arguments.fixture.resolve())]
    if arguments.repo is not None and arguments.baseline:
        return [
            "--repo",
            str(arguments.repo.resolve()),
            "--baseline",
            str(arguments.baseline),
        ]
    raise ValueError("provide --fixture, or both --repo and --baseline")


def _child_environment() -> dict[str, str]:
    child = dict(os.environ)
    provider = child.get("OPENROUTER_PROVIDER", "").strip()
    if provider != "streamlake":
        raise ValueError("OPENROUTER_PROVIDER must be exactly 'streamlake'")
    base_url = child.get("OPENROUTER_BASE_URL", "").strip()
    if base_url and base_url != OPENROUTER_BASE_URL:
        raise ValueError(f"OPENROUTER_BASE_URL must be exactly {OPENROUTER_BASE_URL!r}")
    child.pop("LANGFUSE_PUBLIC_KEY", None)
    child.pop("LANGFUSE_SECRET_KEY", None)
    child.pop("LANGFUSE_BASE_URL", None)
    child["OPENROUTER_BASE_URL"] = OPENROUTER_BASE_URL
    child["PYTHONUNBUFFERED"] = "1"
    return child


def _wait(job: RunningJob) -> tuple[int, int]:
    return job.process.wait(), time.time_ns()


def _artifact_snapshot(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"exists": False, "sha256": None, "size_bytes": None, "mtime_ns": None}
    before = path.stat()
    raw = path.read_bytes()
    after = path.stat()
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ) or len(raw) != after.st_size:
        raise ValueError(f"artifact changed while being snapshotted: {path}")
    return {
        "exists": True,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
        "mtime_ns": after.st_mtime_ns,
    }


def _create_exclusive_output_dir(path: Path) -> Path:
    """Atomically claim a fresh stage directory; never reuse prior evidence."""

    output_dir = path.resolve()
    try:
        output_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError as error:
        raise ValueError("--output-dir must be new and must not already exist") from error
    return output_dir


def launch(arguments: argparse.Namespace) -> dict[str, Any]:
    if not str(arguments.authorization_ref).strip():
        raise ValueError("--authorization-ref must be non-empty")
    agents = tuple(arguments.agents)
    schedule = _schedule(agents, arguments.pairs, arguments.pair_prefix)
    child_parent_map = _parse_child_parent_map(
        getattr(arguments, "child_parent", None),
        agents,
    )
    target_arguments = _target_arguments(arguments)
    child_env = _child_environment()
    output_dir = _create_exclusive_output_dir(arguments.output_dir)
    script_dir = Path(__file__).resolve().parent

    batch_started_at_ns = time.time_ns()
    jobs: list[RunningJob] = []
    results: list[dict[str, Any]] = []
    try:
        for schedule_ordinal, (pair_id, agent) in enumerate(schedule, start=1):
            stem = f"{pair_id}-{agent}"
            artifact = output_dir / f"{stem}.json"
            stdout_path = output_dir / f"{stem}.stdout.log"
            stderr_path = output_dir / f"{stem}.stderr.log"
            stdout_file = stdout_path.open("w", encoding="utf-8")
            stderr_file = stderr_path.open("w", encoding="utf-8")
            argv = [
                str(arguments.python),
                str(script_dir / AGENT_FILES[agent]),
                *target_arguments,
                "--repeats",
                "1",
                "--pair-id",
                pair_id,
                "--output",
                str(artifact),
            ]
            popen_started_at_ns = time.time_ns()
            try:
                process = subprocess.Popen(
                    argv,
                    cwd=script_dir.parent.parent.parent,
                    env=child_env,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    text=True,
                )
            except BaseException:
                stdout_file.close()
                stderr_file.close()
                raise
            jobs.append(
                RunningJob(
                    schedule_ordinal=schedule_ordinal,
                    pair_id=pair_id,
                    agent=agent,
                    artifact=artifact,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                    process=process,
                    stdout_file=stdout_file,
                    stderr_file=stderr_file,
                    popen_started_at_ns=popen_started_at_ns,
                )
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as executor:
            futures = {executor.submit(_wait, job): job for job in jobs}
            for future in concurrent.futures.as_completed(futures):
                job = futures[future]
                returncode, child_exited_at_ns = future.result()
                job.stdout_file.close()
                job.stderr_file.close()
                # Snapshot each artifact as soon as its child exits. This avoids
                # charging a fast arm for time spent waiting on a slower peer.
                artifact_snapshot = _artifact_snapshot(job.artifact)
                artifact_snapshot_at_ns = time.time_ns()
                results.append(
                    {
                        "schedule_ordinal": job.schedule_ordinal,
                        "pair_id": job.pair_id,
                        "agent": job.agent,
                        "artifact": str(job.artifact),
                        "artifact_snapshot": artifact_snapshot,
                        "artifact_snapshot_at_ns": artifact_snapshot_at_ns,
                        "stdout": str(job.stdout_path),
                        "stderr": str(job.stderr_path),
                        "pid": job.process.pid,
                        "returncode": returncode,
                        "popen_started_at_ns": job.popen_started_at_ns,
                        "child_exited_at_ns": child_exited_at_ns,
                        "process_wall_seconds": round(
                            (child_exited_at_ns - job.popen_started_at_ns) / 1_000_000_000,
                            6,
                        ),
                        "end_to_end_wall_seconds": round(
                            (artifact_snapshot_at_ns - job.popen_started_at_ns) / 1_000_000_000,
                            6,
                        ),
                        "artifact_snapshot_overhead_seconds": round(
                            (artifact_snapshot_at_ns - child_exited_at_ns) / 1_000_000_000,
                            6,
                        ),
                    }
                )
    finally:
        for job in jobs:
            if not job.stdout_file.closed:
                job.stdout_file.close()
            if not job.stderr_file.closed:
                job.stderr_file.close()

    artifact_frozen_at_ns = time.time_ns()
    batch_completed_at_ns = artifact_frozen_at_ns
    planned_pairs = list(dict.fromkeys(pair_id for pair_id, _agent in schedule))
    return {
        "raw_generation_only": True,
        "ground_truth_loaded": False,
        "authorization_reference": arguments.authorization_ref,
        "fixture_export_scope": "code/docs/diff/derived-retrieval; ground truth excluded",
        "provider": "streamlake",
        "provider_fallbacks": False,
        "openrouter_base_url": OPENROUTER_BASE_URL,
        "job_count": len(results),
        "pair_count": arguments.pairs,
        "all_started_before_wait": len(jobs) == len(schedule),
        "schedule_mode": "all-popen-starts-before-any-wait",
        "agents": list(agents),
        "planned_agents": list(agents),
        "planned_pairs": planned_pairs,
        "child_parent_map": child_parent_map,
        "planned_schedule": [
            {"schedule_ordinal": index, "pair_id": pair_id, "agent": agent}
            for index, (pair_id, agent) in enumerate(schedule, start=1)
        ],
        "batch_started_at_ns": batch_started_at_ns,
        "batch_completed_at_ns": batch_completed_at_ns,
        "artifact_frozen_at_ns": artifact_frozen_at_ns,
        "launch_spread_seconds": round(
            (
                max(job.popen_started_at_ns for job in jobs)
                - min(job.popen_started_at_ns for job in jobs)
            )
            / 1_000_000_000,
            6,
        ),
        "batch_makespan_seconds": round(
            (batch_completed_at_ns - batch_started_at_ns) / 1_000_000_000,
            6,
        ),
        "jobs": sorted(results, key=lambda item: int(item["schedule_ordinal"])),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Launch a raw tool-portfolio generation stage")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--fixture", type=Path)
    target.add_argument("--repo", type=Path)
    parser.add_argument("--baseline")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--agents", nargs="+", required=True, choices=tuple(AGENT_FILES))
    parser.add_argument("--pairs", type=int, default=1)
    parser.add_argument("--pair-prefix", default="screen")
    parser.add_argument(
        "--child-parent",
        action="append",
        default=None,
        metavar="CHILD=PARENT",
        help=(
            "Pre-register a same-stage forward-search child and parent; repeat for "
            "multiple children."
        ),
    )
    parser.add_argument("--authorization-ref", required=True)
    arguments = parser.parse_args()
    try:
        manifest = launch(arguments)
    except ValueError as error:
        parser.error(str(error))
        raise AssertionError("unreachable") from error
    manifest_path = arguments.output_dir.resolve() / "launch-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=1))
    if any(job["returncode"] != 0 for job in manifest["jobs"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

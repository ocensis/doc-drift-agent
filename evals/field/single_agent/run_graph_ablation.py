"""Launch selected default/CodeGraph/GitNexus generation jobs concurrently.

This launcher never accepts or reads ground truth.  It starts every process
before waiting for any of them and imposes no process-level timeout.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from _graph_runtime import CODEGRAPH_AGENT, GITNEXUS_AGENT, GRAPH_DEFAULT_AGENT


@dataclass(slots=True)
class RunningJob:
    pair_id: str
    agent: str
    artifact: Path
    stdout_path: Path
    stderr_path: Path
    process: subprocess.Popen[str]
    stdout_file: IO[str]
    stderr_file: IO[str]
    started_at_ns: int


def _wait_for_job(job: RunningJob) -> tuple[int, int]:
    """Wait for one child and capture its completion at the actual wait return."""

    return job.process.wait(), time.time_ns()


AGENT_FILES = {
    GRAPH_DEFAULT_AGENT: "graph_default_agent.py",
    CODEGRAPH_AGENT: "codegraph_agent.py",
    GITNEXUS_AGENT: "gitnexus_agent.py",
}

# Rotate the first-launched arm even though the complete schedule is started in
# a tight window.  This avoids assigning one provider the same tiny ordering
# advantage in every pair.
SCHEDULE = (
    ("pair-1", CODEGRAPH_AGENT),
    ("pair-1", GRAPH_DEFAULT_AGENT),
    ("pair-1", GITNEXUS_AGENT),
    ("pair-2", GITNEXUS_AGENT),
    ("pair-2", CODEGRAPH_AGENT),
    ("pair-2", GRAPH_DEFAULT_AGENT),
    ("pair-3", GRAPH_DEFAULT_AGENT),
    ("pair-3", GITNEXUS_AGENT),
    ("pair-3", CODEGRAPH_AGENT),
)

# When the already-completed control artifacts are reused, keep the six new
# treatment jobs balanced by which provider is launched first within a pair.
TREATMENT_ONLY_SCHEDULE = (
    ("pair-1", CODEGRAPH_AGENT),
    ("pair-1", GITNEXUS_AGENT),
    ("pair-2", GITNEXUS_AGENT),
    ("pair-2", CODEGRAPH_AGENT),
    ("pair-3", CODEGRAPH_AGENT),
    ("pair-3", GITNEXUS_AGENT),
)


def _selected_schedule(arguments: argparse.Namespace) -> tuple[tuple[str, str], ...]:
    selected = tuple(getattr(arguments, "agents", None) or AGENT_FILES)
    if len(set(selected)) != len(selected):
        raise ValueError("--agents must not contain duplicates")
    unknown = sorted(set(selected) - set(AGENT_FILES))
    if unknown:
        raise ValueError(f"unknown graph-ablation agents: {', '.join(unknown)}")
    if not selected:
        raise ValueError("select at least one graph-ablation agent")
    if set(selected) == {CODEGRAPH_AGENT, GITNEXUS_AGENT}:
        return TREATMENT_ONLY_SCHEDULE
    selected_set = set(selected)
    return tuple(item for item in SCHEDULE if item[1] in selected_set)


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


def launch(arguments: argparse.Namespace) -> dict[str, object]:
    output_dir = arguments.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    script_dir = Path(__file__).resolve().parent
    target_arguments = _target_arguments(arguments)
    child_env = dict(os.environ)
    child_env.pop("LANGFUSE_PUBLIC_KEY", None)
    child_env.pop("LANGFUSE_SECRET_KEY", None)
    child_env.pop("LANGFUSE_BASE_URL", None)
    child_env["PYTHONUNBUFFERED"] = "1"
    schedule = _selected_schedule(arguments)

    batch_started_at_ns = time.time_ns()
    jobs: list[RunningJob] = []
    try:
        for pair_id, agent in schedule:
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
            started_at_ns = time.time_ns()
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
                    pair_id=pair_id,
                    agent=agent,
                    artifact=artifact,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                    process=process,
                    stdout_file=stdout_file,
                    stderr_file=stderr_file,
                    started_at_ns=started_at_ns,
                )
            )

        # Every selected process exists before any wait call. Wait concurrently
        # so each job's completion timestamp is not inflated by schedule order.
        results: list[dict[str, object]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as executor:
            futures = [(job, executor.submit(_wait_for_job, job)) for job in jobs]
            for job, future in futures:
                returncode, completed_at_ns = future.result()
                job.stdout_file.close()
                job.stderr_file.close()
                results.append(
                    {
                        "pair_id": job.pair_id,
                        "agent": job.agent,
                        "artifact": str(job.artifact),
                        "stdout": str(job.stdout_path),
                        "stderr": str(job.stderr_path),
                        "pid": job.process.pid,
                        "returncode": returncode,
                        "started_at_ns": job.started_at_ns,
                        "completed_at_ns": completed_at_ns,
                        "wall_seconds": round(
                            (completed_at_ns - job.started_at_ns) / 1_000_000_000,
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

    batch_completed_at_ns = time.time_ns()
    return {
        "raw_generation_only": True,
        "ground_truth_loaded": False,
        "job_count": len(results),
        "all_started_before_wait": len(jobs) == len(schedule),
        "selected_agents": sorted({agent for _pair_id, agent in schedule}),
        "batch_started_at_ns": batch_started_at_ns,
        "batch_completed_at_ns": batch_completed_at_ns,
        "launch_spread_seconds": round(
            (max(job.started_at_ns for job in jobs) - min(job.started_at_ns for job in jobs))
            / 1_000_000_000,
            6,
        ),
        "batch_makespan_seconds": round(
            (batch_completed_at_ns - batch_started_at_ns) / 1_000_000_000,
            6,
        ),
        "jobs": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Launch the unbounded three-arm code-graph ablation"
    )
    parser.add_argument("--fixture", type=Path, default=None)
    parser.add_argument("--repo", type=Path, default=None)
    parser.add_argument("--baseline", default=None)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument(
        "--agents",
        nargs="+",
        choices=tuple(AGENT_FILES),
        default=tuple(AGENT_FILES),
        help="Agent arms to launch; each selected arm runs all three pairs",
    )
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
    failures = [job for job in manifest["jobs"] if job["returncode"] != 0]
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

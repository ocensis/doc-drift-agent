"""No-model, no-ground-truth S0 probe for the CodeGraph node+impact candidate."""

# The field harness is a sibling script rather than an installed package.
# ruff: noqa: E402

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

FIELD_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = FIELD_DIR.parent.parent
sys.path.insert(0, str(FIELD_DIR))
sys.path.insert(0, str(REPO_ROOT))

import _harness as H
from _portfolio_codegraph_node_impact import (
    CODEGRAPH_NODE_IMPACT_TOOL,
    codegraph_node_impact_runtime,
)
from _runner import AgentContext


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _summary(output: str) -> dict[str, Any]:
    return {
        "chars": len(output),
        "bytes": len(output.encode("utf-8")),
        "lines": len(output.splitlines()),
        "sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
    }


def _cleanup_fixture(repo: Path) -> None:
    root = repo.parent.resolve()
    expected_parent = Path(tempfile.gettempdir()).resolve()
    if root.parent != expected_parent or not root.name.startswith("fr009-fixture-"):
        raise RuntimeError(f"refusing to clean unexpected fixture root: {root}")
    shutil.rmtree(root)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Probe CodeGraph node+impact locally without a model or ground truth"
    )
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--file")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()

    fixture_repo, baseline_revision = H.materialize_fixture(arguments.fixture.resolve())
    runtime = None
    setup_started = time.monotonic()
    try:
        context = AgentContext(
            repo_path=fixture_repo,
            baseline_revision=baseline_revision,
            head_revision=_git(fixture_repo, "rev-parse", "HEAD"),
        )
        runtime = codegraph_node_impact_runtime(context)
        setup_seconds = time.monotonic() - setup_started

        diff_started = time.monotonic()
        diff_output = runtime.extra_tools["git_diff"][1]({})
        diff_seconds = time.monotonic() - diff_started

        tool_arguments = {"symbol": arguments.symbol}
        if arguments.file is not None:
            tool_arguments["file"] = arguments.file
        handler_started = time.monotonic()
        output = runtime.extra_tools[CODEGRAPH_NODE_IMPACT_TOOL][1](tool_arguments)
        handler_seconds = time.monotonic() - handler_started
        decoded = json.loads(output)

        cleanup_started = time.monotonic()
        if runtime.close is not None:
            runtime.close()
        cleanup_seconds = time.monotonic() - cleanup_started
        artifact = {
            "stage": "S0-codegraph-node-impact-local-handler-probe",
            "model_called": False,
            "ground_truth_loaded": False,
            "fixture": str(arguments.fixture.resolve()),
            "baseline_revision": context.baseline_revision,
            "head_revision": context.head_revision,
            "head_tree": _git(fixture_repo, "rev-parse", "HEAD^{tree}"),
            "tool": CODEGRAPH_NODE_IMPACT_TOOL,
            "arguments": tool_arguments,
            "diff_probe": {**_summary(diff_output), "seconds": round(diff_seconds, 6)},
            "result": decoded,
            "result_summary": _summary(output),
            "timing": {
                "setup_seconds": round(setup_seconds, 6),
                "diff_seconds": round(diff_seconds, 6),
                "handler_seconds": round(handler_seconds, 6),
                "cleanup_seconds": round(cleanup_seconds, 6),
                "total_seconds": round(
                    setup_seconds + diff_seconds + handler_seconds + cleanup_seconds,
                    6,
                ),
            },
            "metadata": runtime.metadata,
        }
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(
            json.dumps(artifact, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        print(json.dumps(artifact, ensure_ascii=False, indent=1))
    finally:
        if runtime is not None and runtime.metadata.get("cleanup_success") is not True:
            if runtime.close is not None:
                runtime.close()
        _cleanup_fixture(fixture_repo)


if __name__ == "__main__":
    main()

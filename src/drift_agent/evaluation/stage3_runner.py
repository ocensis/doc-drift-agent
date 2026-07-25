from __future__ import annotations

import hashlib
import math
import os
import shutil
import socket
import stat
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn
from unittest.mock import patch

from drift_agent.agent.state import AgentState
from drift_agent.application import AgentRuntime, run
from drift_agent.domain.enums import FindingDisposition, RunMode, ValidationStatus
from drift_agent.domain.models import RunBudgets, RunRequest, ValidationResult
from drift_agent.evaluation.models import ChangedBytes
from drift_agent.evaluation.runner import observed_finding_from_domain
from drift_agent.evaluation.stage3_catalog import (
    default_stage3_dataset_root,
    load_stage3_catalog,
)
from drift_agent.evaluation.stage3_fake_model import ScriptedModelTransport
from drift_agent.evaluation.stage3_metrics import (
    build_stage3_report,
    deterministic_stage3_projection,
    evaluate_stage3_case,
)
from drift_agent.evaluation.stage3_models import (
    Stage3Accounting,
    Stage3CaseEvaluation,
    Stage3CaseManifest,
    Stage3CaseObservation,
    Stage3EvaluationReport,
    Stage3RepairOutcome,
)
from drift_agent.model.client import ModelTransport
from drift_agent.repair.planner import RepairGroup
from drift_agent.validation.commands import ValidationCommandRunner
from drift_agent.workspace.transaction import WorkspaceTransaction

_GIT_OVERRIDES = (
    "-c",
    "commit.gpgSign=false",
    "-c",
    "tag.gpgSign=false",
    "-c",
    f"core.hooksPath={os.devnull}",
    "-c",
    "core.autocrlf=false",
    "-c",
    "core.eol=lf",
)
_MODEL_ENVIRONMENT_NAMES = (
    "ANTHROPIC_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "COHERE_API_KEY",
    "DEEPSEEK_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GROQ_API_KEY",
    "MISTRAL_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "OPENROUTER_BASE_URL",
    "OPENROUTER_DATA_COLLECTION",
    "OPENROUTER_FAST_MODEL",
    "OPENROUTER_MODEL",
    "OPENROUTER_PROVIDER",
    "OPENROUTER_STRONG_MODEL",
    "OPENROUTER_TIMEOUT_SECONDS",
    "TOGETHER_API_KEY",
    "XAI_API_KEY",
)


class Stage3OfflineViolation(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _WorktreeEntry:
    sha256: str
    size_bytes: int
    mode: str


class _OfflineGuard:
    def __init__(self) -> None:
        self.attempts = 0

    def deny(self, *_args: object, **_kwargs: object) -> NoReturn:
        self.attempts += 1
        raise Stage3OfflineViolation("stage3-v1 replays forbid network access")

    def install(self, stack: ExitStack) -> None:
        stack.enter_context(patch("socket.create_connection", new=self.deny))
        stack.enter_context(patch.object(socket.socket, "connect", new=self.deny))
        stack.enter_context(patch.object(socket.socket, "connect_ex", new=self.deny))


class _TimeoutProcess:
    def __call__(
        self,
        argv: Sequence[str],
        **_kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(list(argv), 0.01, output="")


class _Stage3Runtime(AgentRuntime):
    def __init__(
        self,
        transport: ModelTransport,
        *,
        semantic_validation_failures: int,
    ) -> None:
        super().__init__(model_transport=transport)
        self._semantic_validation_failures = semantic_validation_failures

    def _validate_group(
        self,
        state: AgentState,
        group: RepairGroup,
        transaction: WorkspaceTransaction,
    ) -> ValidationResult:
        result = super()._validate_group(state, group, transaction)
        if result.check == "semantic_redetect" and self._semantic_validation_failures > 0:
            self._semantic_validation_failures -= 1
            return result.model_copy(
                update={
                    "status": ValidationStatus.FAILED,
                    "summary": "stage3-v1 scripted semantic validation failure",
                }
            )
        return result


def _git_environment() -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(
        {
            "GIT_ALLOW_PROTOCOL": "file",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return environment


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *_GIT_OVERRIDES, *args],
        cwd=repo,
        check=True,
        capture_output=True,
        shell=False,
        env=_git_environment(),
    )


def _copy_fixture(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    destination.chmod(0o644)


def _prepare_repository(case_root: Path, manifest: Stage3CaseManifest, repo: Path) -> None:
    repo.mkdir(parents=True)
    for fixture in manifest.files:
        if fixture.role == "base":
            _copy_fixture(case_root / fixture.path, repo / fixture.target_path)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "stage3-v1@example.invalid")
    _git(repo, "config", "user.name", "stage3-v1")
    _git(repo, "add", ".")
    _git(repo, "commit", "--no-verify", "--no-gpg-sign", "-qm", "stage3-v1 base")
    for rename in manifest.workspace.renames:
        destination = repo / rename.new_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        (repo / rename.old_path).replace(destination)
    for relative in manifest.workspace.deleted_paths:
        path = repo / relative
        if path.exists():
            path.unlink()
    for fixture in manifest.files:
        if fixture.role == "current":
            _copy_fixture(case_root / fixture.path, repo / fixture.target_path)
    if manifest.workspace.staged_paths:
        _git(repo, "add", "-A", "--", *manifest.workspace.staged_paths)


def _snapshot_worktree(repo: Path) -> dict[str, _WorktreeEntry]:
    snapshot: dict[str, _WorktreeEntry] = {}
    for path in sorted(repo.rglob("*")):
        relative = path.relative_to(repo)
        if not relative.parts or relative.parts[0] == ".git" or path.is_dir():
            continue
        metadata = path.lstat()
        raw = (
            os.readlink(path).encode("utf-8", errors="surrogateescape")
            if path.is_symlink()
            else path.read_bytes()
        )
        snapshot[relative.as_posix()] = _WorktreeEntry(
            sha256=hashlib.sha256(raw).hexdigest(),
            size_bytes=len(raw),
            mode=f"{stat.S_IMODE(metadata.st_mode):04o}",
        )
    return snapshot


def _changed_bytes(
    before: Mapping[str, _WorktreeEntry],
    after: Mapping[str, _WorktreeEntry],
) -> tuple[ChangedBytes, ...]:
    return tuple(
        ChangedBytes(
            path=path,
            before_sha256=before[path].sha256 if path in before else None,
            after_sha256=after[path].sha256 if path in after else None,
            before_size_bytes=before[path].size_bytes if path in before else None,
            after_size_bytes=after[path].size_bytes if path in after else None,
            before_mode=before[path].mode if path in before else None,
            after_mode=after[path].mode if path in after else None,
        )
        for path in sorted(before.keys() | after.keys())
        if before.get(path) != after.get(path)
    )


def _known_cost_nano_usd(cost_usd: float) -> int:
    nano = round(cost_usd * 1_000_000_000)
    if not math.isclose(cost_usd, nano / 1_000_000_000, abs_tol=1e-15):
        raise ValueError("stage3-v1 fake cost is not an exact nano-USD value")
    return nano


def _semantic_failures(manifest: Stage3CaseManifest) -> int:
    if manifest.validation_driver == "semantic_fail_once":
        return 1
    if manifest.validation_driver == "semantic_fail_twice":
        return 2
    return 0


def _actual_repair_outcome(
    manifest: Stage3CaseManifest,
    *,
    fixed: bool,
    patch_attempts: int,
) -> Stage3RepairOutcome:
    if manifest.case_kind == "executable":
        return "not_applicable"
    if fixed:
        return "success"
    if patch_attempts == 2:
        return "abstained"
    return "failed"


class Stage3EvaluationRunner:
    def __init__(
        self,
        dataset_root: Path | None = None,
        *,
        workspace_root: Path | None = None,
        enforce_offline: bool = True,
    ) -> None:
        self.dataset_root = (
            dataset_root.resolve() if dataset_root is not None else default_stage3_dataset_root()
        )
        self.workspace_root = workspace_root.resolve() if workspace_root else None
        self.enforce_offline = enforce_offline

    def _manifest(self, case_id: str) -> Stage3CaseManifest:
        manifests = {
            manifest.case_id: manifest for manifest in load_stage3_catalog(self.dataset_root)
        }
        try:
            return manifests[case_id]
        except KeyError as error:
            raise KeyError(f"unknown stage3-v1 case: {case_id}") from error

    def _new_temp_root(self, case_id: str) -> tempfile.TemporaryDirectory[str]:
        if self.workspace_root is not None:
            self.workspace_root.mkdir(parents=True, exist_ok=True)
        return tempfile.TemporaryDirectory(
            prefix=f"stage3-v1-{case_id.replace('.', '-')}-",
            dir=self.workspace_root,
        )

    def _run_prepared(
        self,
        manifest: Stage3CaseManifest,
        temp_root: Path,
    ) -> Stage3CaseEvaluation:
        case_root = self.dataset_root / manifest.case_id
        repo = temp_root / "repo"
        state_dir = temp_root / "state"
        runtime_root = temp_root / "runtime"
        state_dir.mkdir(parents=True)
        runtime_root.mkdir(parents=True)
        transport = ScriptedModelTransport(manifest.model_script)
        runtime = _Stage3Runtime(
            transport,
            semantic_validation_failures=_semantic_failures(manifest),
        )
        if manifest.validation_driver == "timeout":
            runtime.validation_runner = ValidationCommandRunner(process_runner=_TimeoutProcess())
        guard = _OfflineGuard()
        with ExitStack() as stack:
            environment = _git_environment()
            environment.update(
                {
                    "DRIFT_AGENT_EVALUATION_OFFLINE": "1",
                    "XDG_RUNTIME_DIR": str(runtime_root),
                }
            )
            environment.update({name: "" for name in _MODEL_ENVIRONMENT_NAMES})
            stack.enter_context(patch.dict(os.environ, environment, clear=True))
            if self.enforce_offline:
                guard.install(stack)
            _prepare_repository(case_root, manifest, repo)
            before = _snapshot_worktree(repo)
            bundle = run(
                RunRequest(
                    mode=RunMode(manifest.operation),
                    repo_path=repo,
                    state_dir=state_dir,
                    semantic_repair=manifest.semantic_repair,
                    budgets=RunBudgets.model_validate(manifest.budgets.model_dump(mode="python")),
                ),
                runtime=runtime,
            )
        after = _snapshot_worktree(repo)
        ledger = runtime.budget_ledger
        if ledger is None:
            raise RuntimeError("Stage 3 runtime did not initialize its budget ledger")
        patch_attempts = max(
            (ledger.patch_attempts_for(finding.id) for finding in bundle.findings),
            default=0,
        )
        fixed = any(finding.disposition is FindingDisposition.FIXED for finding in bundle.findings)
        observation = Stage3CaseObservation(
            status=bundle.status.value,
            findings=tuple(observed_finding_from_domain(finding) for finding in bundle.findings),
            changed_bytes=_changed_bytes(before, after),
            accounting=Stage3Accounting.model_validate(
                {
                    "repair_outcome": _actual_repair_outcome(
                        manifest,
                        fixed=fixed,
                        patch_attempts=patch_attempts,
                    ),
                    "patch_attempts": patch_attempts,
                    "model_calls_by_profile": bundle.usage.model_calls_by_profile,
                    "validation_commands": bundle.usage.validation_commands,
                    "input_tokens": bundle.usage.input_tokens,
                    "output_tokens": bundle.usage.output_tokens,
                    "known_cost_nano_usd": _known_cost_nano_usd(bundle.usage.estimated_cost_usd),
                }
            ),
            network_calls=guard.attempts,
            offline=self.enforce_offline and guard.attempts == 0,
            model_script_consumed=transport.consumed,
        )
        return evaluate_stage3_case(manifest, observation)

    def run_case(self, case_id: str) -> Stage3CaseEvaluation:
        manifest = self._manifest(case_id)
        with self._new_temp_root(case_id) as temporary:
            return self._run_prepared(manifest, Path(temporary))

    def run_catalog(self) -> Stage3EvaluationReport:
        evaluations: list[Stage3CaseEvaluation] = []
        for manifest in load_stage3_catalog(self.dataset_root):
            with self._new_temp_root(manifest.case_id) as temporary:
                evaluations.append(self._run_prepared(manifest, Path(temporary)))
        return build_stage3_report(evaluations)

    def assert_deterministic(self, case_id: str) -> Stage3CaseEvaluation:
        first = self.run_case(case_id)
        second = self.run_case(case_id)
        if deterministic_stage3_projection(first) != deterministic_stage3_projection(second):
            raise AssertionError(f"non-deterministic stage3-v1 replay: {case_id}")
        return first


__all__ = [
    "Stage3EvaluationRunner",
    "Stage3OfflineViolation",
]

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import stat
import subprocess
import tempfile
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import NoReturn, Protocol, cast
from unittest.mock import patch

from pydantic import BaseModel, JsonValue, TypeAdapter, ValidationError

from drift_agent.evaluation.catalog import default_dataset_root, load_catalog
from drift_agent.evaluation.metrics import build_report, evaluate_case
from drift_agent.evaluation.models import (
    CaseEvaluation,
    CaseManifest,
    CaseObservation,
    ChangedBytes,
    Disposition,
    EvaluationReport,
    MatchingKey,
    ObservedFinding,
    SymbolIdentity,
)

_JSON_VALUE_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)

_GIT_CONFIG_OVERRIDES = (
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

_GIT_ENVIRONMENT_CONFIG = (
    ("commit.gpgSign", "false"),
    ("tag.gpgSign", "false"),
    ("core.hooksPath", os.devnull),
    ("core.autocrlf", "false"),
    ("core.eol", "lf"),
)
_MODEL_PROVIDER_ENVIRONMENT_NAMES = (
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


class CaseExecutor(Protocol):
    """Adapter invoked once inside a prepared, isolated case repository."""

    def __call__(
        self,
        repo_path: Path,
        manifest: CaseManifest,
        state_dir: Path,
    ) -> object: ...


class OfflineViolation(RuntimeError):
    """Raised when evaluated Python code attempts a network connection."""


@dataclass(frozen=True)
class _WorktreeEntry:
    sha256: str
    size_bytes: int
    mode: str


class _OfflineGuard:
    def __init__(self) -> None:
        self.attempts = 0

    def deny(self, *_args: object, **_kwargs: object) -> NoReturn:
        self.attempts += 1
        raise OfflineViolation("structural-v1 replays forbid network access")

    def install(self, stack: ExitStack) -> None:
        stack.enter_context(patch("socket.create_connection", new=self.deny))
        stack.enter_context(patch.object(socket.socket, "connect", new=self.deny))
        stack.enter_context(patch.object(socket.socket, "connect_ex", new=self.deny))


def _default_executor(
    repo_path: Path,
    manifest: CaseManifest,
    state_dir: Path,
) -> object:
    # Imports stay local so metrics/catalog users do not initialize the Agent graph.
    from drift_agent.application import run
    from drift_agent.domain.models import RunRequest

    request_data: dict[str, object] = {
        "mode": manifest.operation,
        "repo_path": repo_path,
    }
    # This compatibility branch lets the evaluation package type-check and audit
    # independently while Stage 2's additive RunRequest fields are introduced.
    if "state_dir" in RunRequest.model_fields:
        request_data["state_dir"] = state_dir
    if "lock_timeout_seconds" in RunRequest.model_fields:
        request_data["lock_timeout_seconds"] = 5.0
    request = RunRequest.model_validate(request_data)
    return run(request)


def _mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, BaseModel):
        return cast(dict[str, object], value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def _field(value: object, name: str, default: object = None) -> object:
    if isinstance(value, BaseModel) and hasattr(value, name):
        return getattr(value, name)
    data = _mapping(value)
    if name in data:
        return data[name]
    return getattr(value, name, default)


def _string(value: object, default: str = "") -> str:
    if isinstance(value, str):
        return value
    enum_value = getattr(value, "value", None)
    return enum_value if isinstance(enum_value, str) else default


def _integer(value: object, default: int = 0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _json_value(value: object) -> JsonValue:
    try:
        return _JSON_VALUE_ADAPTER.validate_python(value)
    except ValidationError:
        return _string(value) or None


def _symbol_from_fqn(value: str) -> SymbolIdentity:
    parts = value.split(".")
    if len(parts) < 2 or any(not part.isidentifier() for part in parts):
        return SymbolIdentity(
            module="unknown",
            name="unknown",
            category="module_function",
        )
    return SymbolIdentity(
        module=".".join(parts[:-1]),
        name=parts[-1],
        category="module_function",
    )


def _symbol_identity(finding: object) -> SymbolIdentity:
    value = _field(finding, "symbol_identity")
    if value is not None:
        try:
            return SymbolIdentity.model_validate(_mapping(value))
        except ValidationError:
            pass
    return _symbol_from_fqn(_string(_field(finding, "symbol_id")))


def _evidence_path(finding: object, field: str) -> str:
    evidence = _field(finding, field)
    path = _string(_field(evidence, "path"))
    return PurePosixPath(path).as_posix() if path else "unknown"


def observed_finding_from_domain(finding: object) -> ObservedFinding:
    """Project a domain finding onto the repository-independent eval key."""

    disposition = _string(_field(finding, "disposition"), "detected")
    if disposition not in {"detected", "fixed", "needs_approval", "unresolved"}:
        disposition = "unresolved"
    key = MatchingKey(
        symbol_identity=_symbol_identity(finding),
        kind=_string(_field(finding, "kind"), "signature_changed"),
        component=_string(_field(finding, "component_id")),
        old_value=_json_value(_field(finding, "old_value")),
        new_value=_json_value(_field(finding, "new_value")),
        code_path=_evidence_path(finding, "code_evidence"),
        doc_path=_evidence_path(finding, "doc_evidence"),
        detector_id=_string(_field(finding, "detector_id"), "structural.signature"),
        detector_version=_string(_field(finding, "detector_version"), "1"),
    )
    reason_code = _string(_field(finding, "reason_code"))
    return ObservedFinding(
        key=key,
        disposition=cast(Disposition, disposition),
        reason_code=reason_code or _string(_field(finding, "reason")),
    )


def observation_from_bundle(
    bundle: object,
    *,
    changed_bytes: tuple[ChangedBytes, ...] = (),
    network_calls: int = 0,
    offline: bool = True,
) -> CaseObservation:
    """Normalize a domain bundle without retaining repository/run-specific IDs."""

    raw_findings = _field(bundle, "findings", ())
    findings: tuple[ObservedFinding, ...]
    if isinstance(raw_findings, (list, tuple)):
        findings = tuple(observed_finding_from_domain(item) for item in raw_findings)
    else:
        findings = ()
    usage = _field(bundle, "usage")
    model_calls = _integer(_field(usage, "model_calls"))
    return CaseObservation(
        status=_string(_field(bundle, "status"), "failed"),
        findings=findings,
        changed_bytes=changed_bytes,
        model_calls=model_calls,
        network_calls=network_calls,
        offline=offline,
    )


def _sanitized_git_environment() -> dict[str, str]:
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
    environment["GIT_CONFIG_COUNT"] = str(len(_GIT_ENVIRONMENT_CONFIG))
    for index, (key, value) in enumerate(_GIT_ENVIRONMENT_CONFIG):
        environment[f"GIT_CONFIG_KEY_{index}"] = key
        environment[f"GIT_CONFIG_VALUE_{index}"] = value
    return environment


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *_GIT_CONFIG_OVERRIDES, *args],
        cwd=repo,
        check=True,
        capture_output=True,
        shell=False,
        env=_sanitized_git_environment(),
    )


def _copy_fixture(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    destination.chmod(0o644)


def _prepare_repository(case_root: Path, manifest: CaseManifest, repo: Path) -> None:
    repo.mkdir(parents=True)
    for fixture in manifest.files:
        if fixture.role == "base":
            _copy_fixture(case_root / fixture.path, repo / fixture.target_path)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "structural-v1@example.invalid")
    _git(repo, "config", "user.name", "structural-v1")
    _git(repo, "add", ".")
    _git(repo, "commit", "--no-verify", "--no-gpg-sign", "-qm", "structural-v1 base")

    for rename in manifest.workspace.renames:
        source = repo / rename.old_path
        destination = repo / rename.new_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.replace(destination)
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
        relative_path = relative.as_posix()
        metadata = path.lstat()
        mode = f"{stat.S_IMODE(metadata.st_mode):04o}"
        if path.is_symlink():
            raw = os.readlink(path).encode("utf-8", errors="surrogateescape")
        else:
            raw = path.read_bytes()
        snapshot[relative_path] = _WorktreeEntry(
            sha256=hashlib.sha256(raw).hexdigest(),
            size_bytes=len(raw),
            mode=mode,
        )
    return snapshot


def _changed_bytes(
    before: Mapping[str, _WorktreeEntry],
    after: Mapping[str, _WorktreeEntry],
) -> tuple[ChangedBytes, ...]:
    changes: list[ChangedBytes] = []
    for path in sorted(set(before) | set(after)):
        old = before.get(path)
        new = after.get(path)
        if old == new:
            continue
        changes.append(
            ChangedBytes(
                path=path,
                before_sha256=old.sha256 if old is not None else None,
                after_sha256=new.sha256 if new is not None else None,
                before_size_bytes=old.size_bytes if old is not None else None,
                after_size_bytes=new.size_bytes if new is not None else None,
                before_mode=old.mode if old is not None else None,
                after_mode=new.mode if new is not None else None,
            )
        )
    return tuple(changes)


def deterministic_projection(value: CaseEvaluation | EvaluationReport) -> bytes:
    """Serialize only deterministic evaluation fields for replay comparison."""

    return json.dumps(
        value.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class EvaluationRunner:
    """Replay structural-v1 cases in fresh local Git/state/runtime roots."""

    def __init__(
        self,
        dataset_root: Path | None = None,
        *,
        executor: CaseExecutor | None = None,
        workspace_root: Path | None = None,
        enforce_offline: bool = True,
    ) -> None:
        self.dataset_root = (
            dataset_root.resolve() if dataset_root is not None else default_dataset_root()
        )
        self.executor = executor or _default_executor
        self.workspace_root = workspace_root.resolve() if workspace_root is not None else None
        self.enforce_offline = enforce_offline

    def _manifest(self, case_id: str) -> CaseManifest:
        manifests = {manifest.case_id: manifest for manifest in load_catalog(self.dataset_root)}
        try:
            return manifests[case_id]
        except KeyError as error:
            raise KeyError(f"unknown structural-v1 case: {case_id}") from error

    def _run_prepared(
        self,
        manifest: CaseManifest,
        temp_root: Path,
    ) -> tuple[CaseEvaluation, CaseObservation]:
        case_root = self.dataset_root / manifest.case_id
        repo = temp_root / "repo"
        state_dir = temp_root / "state"
        runtime_root = temp_root / "runtime"
        state_dir.mkdir(parents=True)
        runtime_root.mkdir(parents=True)
        guard = _OfflineGuard()
        with ExitStack() as stack:
            environment = _sanitized_git_environment()
            environment.update(
                {
                    "DRIFT_AGENT_EVALUATION_OFFLINE": "1",
                    "XDG_RUNTIME_DIR": str(runtime_root),
                }
            )
            environment.update({name: "" for name in _MODEL_PROVIDER_ENVIRONMENT_NAMES})
            stack.enter_context(
                patch.dict(
                    os.environ,
                    environment,
                    clear=True,
                )
            )
            if self.enforce_offline:
                guard.install(stack)
            _prepare_repository(case_root, manifest, repo)
            before = _snapshot_worktree(repo)
            bundle = self.executor(repo, manifest, state_dir)
        after = _snapshot_worktree(repo)
        observation = observation_from_bundle(
            bundle,
            changed_bytes=_changed_bytes(before, after),
            network_calls=guard.attempts,
            offline=self.enforce_offline and guard.attempts == 0,
        )
        return evaluate_case(manifest, observation), observation

    def _new_temp_root(self, case_id: str) -> tempfile.TemporaryDirectory[str]:
        if self.workspace_root is not None:
            self.workspace_root.mkdir(parents=True, exist_ok=True)
        safe_case_id = case_id.replace(".", "-")
        return tempfile.TemporaryDirectory(
            prefix=f"structural-v1-{safe_case_id}-",
            dir=self.workspace_root,
        )

    def run_case(self, case_id: str) -> CaseEvaluation:
        """Audit, prepare, execute, and score one case in fresh state."""

        manifest = self._manifest(case_id)
        with self._new_temp_root(case_id) as temporary:
            evaluation, _ = self._run_prepared(manifest, Path(temporary))
        return evaluation

    def run_catalog(self) -> EvaluationReport:
        """Replay every frozen case once and return the conserved summary."""

        manifests = load_catalog(self.dataset_root)
        evaluations: list[CaseEvaluation] = []
        observations: list[CaseObservation] = []
        for manifest in manifests:
            with self._new_temp_root(manifest.case_id) as temporary:
                evaluation, observation = self._run_prepared(
                    manifest,
                    Path(temporary),
                )
            evaluations.append(evaluation)
            observations.append(observation)
        return build_report(evaluations, observations)

    def assert_deterministic(self, case_id: str) -> CaseEvaluation:
        """Replay a case twice in fresh roots and assert byte-equal projection."""

        first = self.run_case(case_id)
        second = self.run_case(case_id)
        if deterministic_projection(first) != deterministic_projection(second):
            raise AssertionError(f"non-deterministic structural-v1 replay: {case_id}")
        return first

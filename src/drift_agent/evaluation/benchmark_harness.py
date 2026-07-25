from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import secrets
import shutil
import stat
import subprocess
import sys
import sysconfig
import tempfile
import time
import tomllib
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import ValidationError

from drift_agent import __version__
from drift_agent.adapters.contracts import PublicBundleV3
from drift_agent.evaluation.benchmark_cases import (
    PORTABLE_CASE_IDS,
    PreparedBenchmarkCase,
    canonical_digest,
    capture_git_metadata,
    capture_repository_snapshot,
    git_metadata_sha256,
    load_benchmark_cases,
    prepare_benchmark_case,
    project_neutral_oracle,
)
from drift_agent.evaluation.benchmark_models import (
    TRIAL_IDS_BY_COUNT,
    V1_MISSING_METRICS,
    BenchmarkArtifactDigestsV1,
    BenchmarkAuthorizationV1,
    BenchmarkCodexRuntimeV1,
    BenchmarkContractDigestsV1,
    BenchmarkCoverageSummaryV1,
    BenchmarkDatasetCatalogV1,
    BenchmarkDriftRuntimeV1,
    BenchmarkLimitsV1,
    BenchmarkPlanV1,
    BenchmarkReportV1,
    BenchmarkToolchainV1,
    BoundedStreamReceiptV1,
    CodexTaskResultV1,
    ControlReportV1,
    ControlResultV1,
    ControlSummaryV1,
    CoverageEntryV1,
    CoverageReportV1,
    FailureCountV1,
    RawRunEvidenceV1,
    RawUsageEvidenceV1,
    RawUsageMetricV1,
    RedactionReceiptV1,
    TerminalReceiptV1,
    build_benchmark_schedule,
    bytes_sha256,
    canonical_json_bytes,
    canonical_sha256,
    deterministic_observation_id,
    fixed_benchmark_case_selections,
    neutral_finding_encoding_bytes,
    neutral_finding_encoding_sha256,
)
from drift_agent.evaluation.benchmark_runner import (
    CODEX_RENDERER_VERSION,
    DRIFT_ADAPTER_VERSION,
    REDACTION_POLICY_VERSION,
    BoundedSubprocessRunner,
    StreamEvidence,
    SubjectRunResult,
    render_codex_prompt,
    run_codex_subject,
    run_drift_subject,
)
from drift_agent.evaluation.benchmark_runtime import (
    ExecutableIdentity,
    SlimRuntime,
    build_codex_permission_config,
    build_slim_runtime,
    codex_auth_sensitive_values,
    copy_isolated_codex_auth,
    identify_executable,
    interpreter_runtime_roots,
    resolve_codex_executable,
    sha256_file,
)
from drift_agent.evaluation.catalog import default_dataset_root
from drift_agent.evaluation.stage3_catalog import default_stage3_dataset_root
from drift_agent.evaluation.stage3_runner import Stage3EvaluationRunner
from drift_agent.evaluation.stage4_comparison import (
    build_stage4_comparison,
    stage4_comparison_artifacts,
)
from drift_agent.evaluation.stage4_models import (
    ComparisonObservationV1,
    Stage4ComparisonReport,
)

_MAX_PLAN_BYTES = 4 * 1024 * 1024
_DEFAULT_SHUFFLE_SEED = 20_260_715
_SCORER_VERSION = "trusted-neutral-scorer-v1"
_PERMISSION_PROFILE = "benchmark"
_BATCH_STOPPING_CODEX_FAILURES = frozenset(
    {
        "runner_internal_error",
        "auth_failed",
        "model_unavailable",
        "rate_limited_or_provider_error",
        "invalid_jsonl",
        "missing_terminal_event",
    }
)
_SENTINEL_SUCCESS = "benchmark-sandbox-sentinel-v1"
_APPLE_DEVELOPER_TOOLS = Path("/Library/Developer/CommandLineTools")


class BenchmarkHarnessError(RuntimeError):
    """Raised when planning or replay integrity cannot be established."""


@dataclass(frozen=True, slots=True)
class NeutralToolchain:
    root: Path
    python: ExecutableIdentity
    git: ExecutableIdentity
    pytest: ExecutableIdentity
    distributions_sha256: str
    plugin_set_sha256: str

    @property
    def bin_path(self) -> str:
        return os.pathsep.join((os.fspath(self.root / "bin"), "/usr/bin", "/bin"))


@dataclass(frozen=True, slots=True)
class PlannedRuntime:
    root: Path
    toolchain: NeutralToolchain
    slim: SlimRuntime
    codex: ExecutableIdentity


@dataclass(frozen=True, slots=True)
class BenchmarkRunArtifacts:
    artifacts_dir: Path
    plan_digest: str
    coverage: CoverageReportV1
    comparison: Stage4ComparisonReport
    controls: ControlReportV1
    headline: BenchmarkReportV1


def _source_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _system_temp_roots() -> tuple[Path, ...]:
    return tuple(
        dict.fromkeys(
            (
                Path("/private/tmp").resolve(),
                Path(tempfile.gettempdir()).expanduser().resolve(),
            )
        )
    )


def _require_formal_runtime_root(path: Path, *, label: str) -> Path:
    candidate = path.expanduser().absolute()
    for component in (candidate, *candidate.parents):
        if component.exists() and component.is_symlink():
            raise BenchmarkHarnessError(f"{label} path contains a symlink")
    root = candidate.resolve()
    prohibited = (Path.home().resolve(), *_system_temp_roots())
    if any(root == denied or denied in root.parents for denied in prohibited):
        raise BenchmarkHarnessError(
            f"{label} must be outside the user home and system temp; "
            "use a private 0700 directory under /Users/Shared"
        )
    return root


def _require_private_directory(path: Path, *, label: str, create: bool) -> Path:
    directory = _require_formal_runtime_root(path, label=label)
    if create:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not directory.is_dir():
        raise BenchmarkHarnessError(f"{label} must be a directory")
    if directory.stat().st_mode & 0o077:
        raise BenchmarkHarnessError(f"{label} must have mode 0700 or stricter")
    return directory


def _strict_json(raw: bytes) -> object:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def invalid_constant(value: str) -> object:
        raise ValueError(f"invalid JSON constant: {value}")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)


def _write_private(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    path.chmod(0o600)


def _run_text(argv: list[str], *, env: dict[str, str] | None = None) -> str:
    try:
        completed = subprocess.run(
            argv,
            check=True,
            capture_output=True,
            text=True,
            shell=False,
            timeout=120,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise BenchmarkHarnessError(f"benchmark preflight command failed: {argv[0]}") from error
    output = (completed.stdout or completed.stderr).strip()
    if not output:
        raise BenchmarkHarnessError(f"benchmark preflight returned no output: {argv[0]}")
    return output


def _normalized_distribution_name(value: str) -> str:
    return value.lower().replace("_", "-").replace(".", "-")


def _copy_locked_dependencies(*, source_root: Path, destination_python: Path) -> None:
    source_site = Path(sysconfig.get_path("purelib")).resolve()
    if not source_site.is_dir() or Path(sys.prefix).resolve() == Path(sys.base_prefix).resolve():
        raise BenchmarkHarnessError("benchmark planning must run from the locked project venv")
    lock = tomllib.loads((source_root / "uv.lock").read_text(encoding="utf-8"))
    locked = {
        (_normalized_distribution_name(item["name"]), item["version"])
        for item in lock.get("package", [])
        if isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and isinstance(item.get("version"), str)
    }
    installed: set[tuple[str, str]] = set()
    for distribution in importlib.metadata.distributions(path=[os.fspath(source_site)]):
        name = distribution.metadata["Name"]
        if name is None:
            continue
        normalized = _normalized_distribution_name(name)
        if normalized != "doc-code-drift-agent":
            installed.add((normalized, distribution.version))
    extras = sorted(installed - locked)
    if extras:
        raise BenchmarkHarnessError(
            f"project venv contains distributions outside uv.lock: {extras!r}"
        )
    destination_site = Path(
        _run_text(
            [
                os.fspath(destination_python),
                "-c",
                "import sysconfig;print(sysconfig.get_path('purelib'))",
            ],
            env={"PATH": "/opt/homebrew/bin:/usr/bin:/bin"},
        )
    )
    destination_site.mkdir(parents=True, exist_ok=True)
    for source in sorted(source_site.rglob("*")):
        relative = source.relative_to(source_site)
        top = relative.parts[0]
        if (
            source.is_dir()
            or "__pycache__" in relative.parts
            or source.suffix in {".pth", ".egg-link", ".pyc"}
            or top == "drift_agent"
            or top.startswith("__editable__")
            or top.startswith("doc_code_drift_agent-")
        ):
            continue
        if source.is_symlink() or not source.is_file():
            raise BenchmarkHarnessError(
                f"locked dependency tree contains unsupported entry: {relative}"
            )
        destination = destination_site / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    pytest_launcher = destination_python.parent / "pytest"
    pytest_launcher.write_text(
        f"#!{destination_python}\n"
        "from pytest import console_main\n"
        "raise SystemExit(console_main())\n",
        encoding="utf-8",
    )
    pytest_launcher.chmod(0o755)
    probe = subprocess.run(
        [
            os.fspath(destination_python),
            "-c",
            (
                "import importlib.util;"
                "import griffe,httpx,langgraph,markdown_it,mcp,platformdirs,pydantic,pytest,typer;"
                "assert importlib.util.find_spec('drift_agent') is None"
            ),
        ],
        check=False,
        capture_output=True,
        shell=False,
        env={
            "PATH": "/opt/homebrew/bin:/usr/bin:/bin",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    if probe.returncode != 0:
        raise BenchmarkHarnessError("copied neutral dependency runtime failed its import audit")


def _ensure_neutral_toolchain(runtime_root: Path, *, source_root: Path) -> NeutralToolchain:
    root = runtime_root / "neutral-toolchain"
    python = root / "bin" / "python"
    if not python.is_file():
        base_python = Path("/opt/homebrew/bin/python3.11")
        if not base_python.is_file() or not os.access(base_python, os.X_OK):
            raise BenchmarkHarnessError(
                "formal macOS benchmark requires /opt/homebrew/bin/python3.11"
            )
        root.parent.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(
                [
                    os.fspath(base_python),
                    "-m",
                    "venv",
                    "--without-pip",
                    "--copies",
                    os.fspath(root),
                ],
                check=True,
                capture_output=True,
                shell=False,
                timeout=120,
            )
            _copy_locked_dependencies(
                source_root=source_root,
                destination_python=python,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise BenchmarkHarnessError(
                "cannot create the offline neutral Python dependency toolchain"
            ) from error

    python_identity = identify_executable(python, "--version")
    git_path = Path("/usr/bin/git")
    git_identity = identify_executable(git_path, "--version")
    pytest_identity = identify_executable(root / "bin" / "pytest", "--version")
    distributions = _run_text(
        [
            os.fspath(python),
            "-c",
            (
                "import importlib.metadata as m,json;"
                "print(json.dumps(sorted((d.metadata['Name'],d.version) "
                "for d in m.distributions()),separators=(',',':')))"
            ),
        ],
        env={
            "PATH": f"{root / 'bin'}:/usr/bin:/bin",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    neutral_site = Path(
        _run_text(
            [
                os.fspath(python),
                "-c",
                "import sysconfig;print(sysconfig.get_path('purelib'))",
            ],
            env={"PATH": f"{root / 'bin'}:/usr/bin:/bin"},
        )
    )
    distribution_files = {
        path.relative_to(neutral_site).as_posix(): sha256_file(path)
        for path in sorted(neutral_site.rglob("*"))
        if path.is_file()
        and not path.is_symlink()
        and "__pycache__" not in path.parts
        and path.suffix != ".pyc"
    }
    probe = subprocess.run(
        [
            os.fspath(python),
            "-c",
            "import importlib.util; assert importlib.util.find_spec('drift_agent') is None",
        ],
        check=False,
        capture_output=True,
        shell=False,
        cwd=runtime_root,
        env={
            "PATH": f"{root / 'bin'}:/usr/bin:/bin",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    if probe.returncode != 0:
        raise BenchmarkHarnessError("neutral Codex toolchain can import drift_agent")
    return NeutralToolchain(
        root=root,
        python=python_identity,
        git=git_identity,
        pytest=pytest_identity,
        distributions_sha256=canonical_sha256(
            {
                "inventory": _strict_json(distributions.encode("utf-8")),
                "files": distribution_files,
            }
        ),
        plugin_set_sha256=canonical_sha256(()),
    )


def _source_namespace_digest(source_root: Path, relative_root: Path) -> str:
    members = {
        path.relative_to(source_root).as_posix(): sha256_file(path)
        for path in sorted(relative_root.rglob("*.py"))
        if "__pycache__" not in path.parts
    }
    return canonical_sha256(members)


def _toolchain_contract(
    *,
    source_root: Path,
    neutral: NeutralToolchain,
    slim: SlimRuntime,
) -> BenchmarkToolchainV1:
    local_runtime = {
        "kind": "local-macos-seatbelt",
        "platform": platform.platform(),
        "machine": platform.machine(),
    }
    runtime_manifest = {
        "python": neutral.python.sha256,
        "python_runtime_roots": tuple(
            os.fspath(path)
            for path in interpreter_runtime_roots(
                neutral.root,
                neutral.python.path,
            )
        ),
        "git": neutral.git.sha256,
        "git_runtime_root": os.fspath(_APPLE_DEVELOPER_TOOLS.resolve()),
        "pytest": neutral.pytest.sha256,
        "distributions": neutral.distributions_sha256,
    }
    supervisor_digest = _source_namespace_digest(
        source_root,
        source_root / "src" / "drift_agent" / "evaluation",
    )
    codex_namespace = canonical_sha256(
        {
            "toolchain": runtime_manifest,
            "drift_agent_importable": False,
            "plugin_autoload": False,
        }
    )
    drift_namespace = canonical_sha256(
        {"toolchain": runtime_manifest, "slim_wheel_sha256": slim.wheel_sha256}
    )
    return BenchmarkToolchainV1(
        container_image_sha256=canonical_sha256(local_runtime),
        runtime_toolchain_sha256=canonical_sha256(runtime_manifest),
        python_version=neutral.python.version,
        python_executable_sha256=neutral.python.sha256,
        git_version=neutral.git.version,
        git_executable_sha256=neutral.git.sha256,
        pytest_version=neutral.pytest.version,
        pytest_executable_sha256=neutral.pytest.sha256,
        distributions_sha256=neutral.distributions_sha256,
        plugin_set_sha256=neutral.plugin_set_sha256,
        supervisor_namespace_sha256=supervisor_digest,
        codex_namespace_sha256=codex_namespace,
        drift_namespace_sha256=drift_namespace,
    )


def _schema_bundle() -> dict[str, object]:
    return {
        "BenchmarkReportV1": BenchmarkReportV1.model_json_schema(),
        "CodexTaskResultV1": CodexTaskResultV1.model_json_schema(),
        "RawRunEvidenceV1": RawRunEvidenceV1.model_json_schema(),
        "TerminalReceiptV1": TerminalReceiptV1.model_json_schema(),
        "CoverageReportV1": CoverageReportV1.model_json_schema(),
        "ControlReportV1": ControlReportV1.model_json_schema(),
    }


def _contract_digests(source_root: Path) -> BenchmarkContractDigestsV1:
    cases = {case.case_id: case for case in load_benchmark_cases()}
    projections = tuple(project_neutral_oracle(cases[case_id]) for case_id in PORTABLE_CASE_IDS)
    output_schema = CodexTaskResultV1.model_json_schema()
    prompt_contract = {
        operation: render_codex_prompt(
            {  # The renderer itself validates the complete frozen task shape.
                "protocol_version": 1,
                "operation": operation,
                "baseline": "HEAD",
                "scope": "current_worktree_changes",
                "docs_only": True,
                "report_findings": True,
                "run_configured_validation": True,
                "abstain_on_insufficient_evidence": True,
                "network": False,
                "dependency_install": False,
                "git_mutation": False,
            }
        )
        for operation in ("check", "repair")
    }
    scorer = source_root / "src" / "drift_agent" / "evaluation" / "benchmark_scoring.py"
    scorer_digest = sha256_file(scorer) if scorer.is_file() else canonical_sha256(_SCORER_VERSION)
    return BenchmarkContractDigestsV1(
        neutral_encoding_sha256=neutral_finding_encoding_sha256(),
        neutral_projection_table_sha256=canonical_sha256(projections),
        codex_output_schema_sha256=canonical_sha256(output_schema),
        schema_bundle_sha256=canonical_sha256(_schema_bundle()),
        prompt_renderer_version=CODEX_RENDERER_VERSION,
        prompt_renderer_sha256=canonical_sha256(prompt_contract),
        scorer_version=_SCORER_VERSION,
        scorer_contract_sha256=scorer_digest,
    )


def _planned_runtime(
    *,
    source_root: Path,
    runtime_root: Path,
    codex_binary: Path | None,
) -> PlannedRuntime:
    runtime_root = _require_private_directory(
        runtime_root,
        label="benchmark runtime",
        create=True,
    )
    neutral = _ensure_neutral_toolchain(runtime_root, source_root=source_root)
    slim = build_slim_runtime(
        source_root=source_root,
        destination=runtime_root / "drift-slim",
        python_executable=neutral.python.path,
    )
    codex = resolve_codex_executable(codex_binary)
    return PlannedRuntime(root=runtime_root, toolchain=neutral, slim=slim, codex=codex)


def _write_public_contracts(runtime_root: Path) -> None:
    public = runtime_root / "public-contracts"
    public.mkdir(parents=True, exist_ok=True)
    schema = canonical_json_bytes(CodexTaskResultV1.model_json_schema()) + b"\n"
    encoding = neutral_finding_encoding_bytes() + b"\n"
    _write_private(public / "CodexTaskResultV1.schema.json", schema)
    _write_private(public / "NeutralFindingEncodingV1.json", encoding)


def create_benchmark_plan(
    *,
    output_path: Path,
    codex_model: str,
    reasoning_effort: Literal["low", "medium", "high", "xhigh"] = "low",
    trials: Literal[1, 3] = 1,
    codex_binary: Path | None = None,
    source_root: Path | None = None,
    runtime_root: Path | None = None,
    shuffle_seed: int = _DEFAULT_SHUFFLE_SEED,
    timeout_seconds: int = 120,
) -> BenchmarkPlanV1:
    """Audit the frozen suite and write a deterministic, non-live benchmark plan."""

    root = (source_root or _source_root()).resolve()
    output = output_path.expanduser().absolute()
    _require_private_directory(output.parent, label="benchmark plan root", create=True)
    runtime = _require_formal_runtime_root(
        runtime_root or output.parent / "benchmark-runtime",
        label="benchmark runtime",
    )
    if output.exists() and output.is_symlink():
        raise BenchmarkHarnessError("benchmark plan path may not be a symlink")
    planned = _planned_runtime(
        source_root=root,
        runtime_root=runtime,
        codex_binary=codex_binary,
    )
    portable, controls = fixed_benchmark_case_selections()
    trial_ids = TRIAL_IDS_BY_COUNT[trials]
    schedule = build_benchmark_schedule(
        portable_cases=portable,
        control_cases=controls,
        trial_ids=trial_ids,
        shuffle_seed=shuffle_seed,
    )
    structural_root = default_dataset_root()
    stage3_root = default_stage3_dataset_root()
    # Loading all cases performs the frozen catalog, manifest, and fixture audit.
    load_benchmark_cases(
        structural_root=structural_root,
        stage3_root=stage3_root,
    )
    lock = root / "uv.lock"
    if not lock.is_file():
        raise BenchmarkHarnessError("uv.lock is required for the pinned Drift runtime")
    plan = BenchmarkPlanV1(
        dataset_catalogs=(
            BenchmarkDatasetCatalogV1(
                dataset_id="structural-v1",
                catalog_sha256=sha256_file(structural_root / "catalog.json"),
            ),
            BenchmarkDatasetCatalogV1(
                dataset_id="stage3-v1",
                catalog_sha256=sha256_file(stage3_root / "catalog.json"),
            ),
        ),
        portable_cases=portable,
        control_cases=controls,
        trial_ids=trial_ids,
        shuffle_seed=shuffle_seed,
        schedule=schedule,
        contracts=_contract_digests(root),
        codex=BenchmarkCodexRuntimeV1(
            cli_version=planned.codex.version,
            binary_sha256=planned.codex.sha256,
            model_id=codex_model,
            reasoning_effort=reasoning_effort,
        ),
        drift_agent=BenchmarkDriftRuntimeV1(
            agent_version=__version__,
            wheel_sha256=planned.slim.wheel_sha256,
            runtime_lock_sha256=sha256_file(lock),
        ),
        toolchain=_toolchain_contract(
            source_root=root,
            neutral=planned.toolchain,
            slim=planned.slim,
        ),
        limits=BenchmarkLimitsV1(
            hard_wall_timeout_seconds=timeout_seconds,
            maximum_live_invocations=len(portable) * trials,
        ),
        budget_source="Codex CLI exposes no enforced token or billed-cost cap",
    )
    _write_public_contracts(runtime)
    _write_private(output, canonical_json_bytes(plan) + b"\n")
    _write_private(output.with_suffix(".sha256"), f"{plan.plan_digest}\n".encode("ascii"))
    return plan


def load_benchmark_plan(path: Path) -> BenchmarkPlanV1:
    raw = path.expanduser().absolute().read_bytes()
    if len(raw) > _MAX_PLAN_BYTES:
        raise BenchmarkHarnessError("benchmark plan exceeds its byte limit")
    try:
        document = _strict_json(raw)
        if not isinstance(document, dict):
            raise ValueError("plan must be a JSON object")
        plan = BenchmarkPlanV1.model_validate_json(raw)
    except (UnicodeError, json.JSONDecodeError, ValidationError, ValueError) as error:
        raise BenchmarkHarnessError("invalid benchmark plan") from error
    digest_file = path.with_suffix(".sha256")
    if digest_file.is_file():
        expected = digest_file.read_text(encoding="ascii").strip()
        if expected != plan.plan_digest:
            raise BenchmarkHarnessError("benchmark plan digest sidecar does not match")
    return plan


def runtime_root_for_plan(path: Path) -> Path:
    return _require_formal_runtime_root(
        path.expanduser().absolute().parent / "benchmark-runtime",
        label="benchmark runtime",
    )


def verify_planned_runtime(
    *,
    plan: BenchmarkPlanV1,
    plan_path: Path,
    codex_binary: Path | None = None,
    source_root: Path | None = None,
    runtime_root: Path | None = None,
) -> PlannedRuntime:
    root = (source_root or _source_root()).resolve()
    planned = _planned_runtime(
        source_root=root,
        runtime_root=(runtime_root or runtime_root_for_plan(plan_path)).expanduser().absolute(),
        codex_binary=codex_binary,
    )
    if (
        planned.codex.version != plan.codex.cli_version
        or planned.codex.sha256 != plan.codex.binary_sha256
        or planned.slim.wheel_sha256 != plan.drift_agent.wheel_sha256
        or sha256_file(root / "uv.lock") != plan.drift_agent.runtime_lock_sha256
    ):
        raise BenchmarkHarnessError("planned Codex/Drift runtime identity changed")
    toolchain = _toolchain_contract(
        source_root=root,
        neutral=planned.toolchain,
        slim=planned.slim,
    )
    if toolchain != plan.toolchain or _contract_digests(root) != plan.contracts:
        raise BenchmarkHarnessError("planned toolchain or benchmark contract changed")
    return planned


def _external_artifact_directory(path: Path, *, source_root: Path) -> Path:
    if not path.is_absolute():
        raise BenchmarkHarnessError("benchmark artifacts directory must be absolute")
    candidate = path.expanduser().absolute()
    for component in (candidate, *candidate.parents):
        if component.exists() and component.is_symlink():
            raise BenchmarkHarnessError("benchmark artifacts path contains a symlink")
    resolved = _require_formal_runtime_root(candidate.resolve(), label="benchmark artifacts")
    if resolved == source_root or source_root in resolved.parents:
        raise BenchmarkHarnessError("benchmark artifacts must live outside the source worktree")
    if resolved.exists() and any(resolved.iterdir()):
        raise BenchmarkHarnessError("benchmark artifacts directory must be new or empty")
    resolved.mkdir(parents=True, exist_ok=True, mode=0o700)
    resolved.chmod(0o700)
    return resolved


def _stream_receipt(
    stream: StreamEvidence,
    *,
    name: Literal["stdout", "stderr", "events", "final"],
) -> BoundedStreamReceiptV1:
    return BoundedStreamReceiptV1(
        stream_name=name,
        total_bytes=stream.bytes_read,
        captured_bytes=stream.bytes_stored,
        byte_limit=stream.byte_limit,
        truncated=stream.truncated,
        raw_sha256=stream.raw_sha256,
        redacted_sha256=stream.redacted_sha256,
        replacement_count=(stream.explicit_secret_replacements + stream.generic_replacements),
    )


def _terminal_receipt(
    *,
    plan: BenchmarkPlanV1,
    slot_id: str,
    prepared: PreparedBenchmarkCase,
    trial_id: str,
    result: SubjectRunResult,
    available_artifacts: tuple[str, ...],
) -> TerminalReceiptV1:
    stdout_name: Literal["stdout", "events"] = "events" if result.subject == "codex" else "stdout"
    return TerminalReceiptV1(
        plan_digest=plan.plan_digest,
        slot_id=slot_id,
        run_class="portable",
        subject=result.subject,
        dataset_id=prepared.dataset_id,
        case_id=prepared.case_id,
        trial_id=trial_id,
        process_started=result.terminal.started,
        terminal_classification=result.terminal.classification,
        exit_code=(
            result.terminal.returncode
            if result.terminal.returncode is not None and result.terminal.returncode >= 0
            else None
        ),
        signal=result.terminal.signal_number,
        timed_out=result.terminal.timed_out,
        duration_ms=(result.terminal.duration_ms if result.terminal.started else None),
        streams=(
            _stream_receipt(result.stdout, name=stdout_name),
            _stream_receipt(result.stderr, name="stderr"),
        ),
        available_artifacts=tuple(sorted(available_artifacts)),
    )


def _measured_usage(value: int, source: str) -> RawUsageMetricV1:
    return RawUsageMetricV1(
        status="measured",
        value=value,
        evidence_source=source,
    )


def _incomplete_usage(value: int | None, source: str | None, reason: str) -> RawUsageMetricV1:
    return RawUsageMetricV1(
        status="accounting_incomplete",
        value=value,
        evidence_source=source if value is not None else None,
        reason=reason,
    )


def _not_measured_usage(reason: str) -> RawUsageMetricV1:
    return RawUsageMetricV1(status="not_measured", reason=reason)


def _raw_usage(result: SubjectRunResult) -> RawUsageEvidenceV1:
    if result.subject == "drift_agent" and isinstance(result.parsed_result, PublicBundleV3):
        usage = result.parsed_result.usage
        strong = usage.model_calls_by_profile.get("strong", 0)
        cost = round(usage.estimated_cost_usd * 1_000_000_000)
        return RawUsageEvidenceV1(
            model_calls=_measured_usage(usage.model_calls, "Drift Public V3 bundle"),
            strong_model_calls=_measured_usage(strong, "Drift Public V3 bundle"),
            tool_calls=_measured_usage(usage.tool_calls, "Drift Public V3 bundle"),
            input_tokens=_measured_usage(usage.input_tokens, "Drift Public V3 bundle"),
            output_tokens=_measured_usage(usage.output_tokens, "Drift Public V3 bundle"),
            cost_nano_usd=_measured_usage(cost, "Drift Public V3 bundle"),
            duration_ms=_measured_usage(
                result.terminal.duration_ms,
                "supervisor monotonic wall clock",
            ),
        )
    if result.subject == "codex":
        reason = "Codex CLI omits provider call count and billed cost"
        protocol_usage = None if result.codex_protocol is None else result.codex_protocol.usage
        return RawUsageEvidenceV1(
            model_calls=_incomplete_usage(None, None, reason),
            strong_model_calls=_incomplete_usage(None, None, reason),
            tool_calls=(
                _incomplete_usage(None, None, reason)
                if protocol_usage is None
                else _measured_usage(
                    protocol_usage.tool_calls,
                    "Codex completed JSONL tool item IDs",
                )
            ),
            input_tokens=(
                _incomplete_usage(None, None, reason)
                if protocol_usage is None or protocol_usage.input_tokens is None
                else _measured_usage(
                    protocol_usage.input_tokens,
                    "Codex terminal JSONL usage",
                )
            ),
            output_tokens=(
                _incomplete_usage(None, None, reason)
                if protocol_usage is None or protocol_usage.output_tokens is None
                else _measured_usage(
                    protocol_usage.output_tokens,
                    "Codex terminal JSONL usage",
                )
            ),
            cost_nano_usd=_incomplete_usage(None, None, reason),
            duration_ms=(
                _measured_usage(
                    result.terminal.duration_ms,
                    "supervisor monotonic wall clock",
                )
                if result.terminal.started
                else _incomplete_usage(None, None, reason)
            ),
        )
    reason = "valid subject result was unavailable"
    return RawUsageEvidenceV1(
        model_calls=_not_measured_usage(reason),
        strong_model_calls=_not_measured_usage(reason),
        tool_calls=_not_measured_usage(reason),
        input_tokens=_not_measured_usage(reason),
        output_tokens=_not_measured_usage(reason),
        cost_nano_usd=_not_measured_usage(reason),
        duration_ms=(
            _measured_usage(result.terminal.duration_ms, "supervisor monotonic wall clock")
            if result.terminal.started
            else _not_measured_usage(reason)
        ),
    )


def _final_result_sha256(result: SubjectRunResult) -> str | None:
    return None if result.parsed_result is None else canonical_sha256(result.parsed_result)


def _raw_evidence(
    *,
    plan: BenchmarkPlanV1,
    authorization_sha256: str,
    prepared: PreparedBenchmarkCase,
    trial_id: str,
    result: SubjectRunResult,
    terminal: TerminalReceiptV1,
    post_snapshot_digest: str,
    post_git_metadata_sha256: str,
) -> RawRunEvidenceV1:
    stdout_name: Literal["stdout", "events"] = "events" if result.subject == "codex" else "stdout"
    is_codex = result.subject == "codex"
    replacement_count = sum(stream.replacement_count for stream in terminal.streams)
    return RawRunEvidenceV1(
        plan_digest=plan.plan_digest,
        authorization_ledger_sha256=authorization_sha256,
        subject=result.subject,
        dataset_id=prepared.dataset_id,
        case_id=prepared.case_id,
        trial_id=trial_id,
        case_manifest_sha256=prepared.case_manifest_sha256,
        snapshot_digest=prepared.snapshot_digest,
        task_digest=prepared.task_digest,
        scope_digest=prepared.scope_digest,
        tool_profile_digest=f"sha256:{canonical_sha256(plan.tool_profile)}",
        runner_version=(plan.codex.cli_version if is_codex else DRIFT_ADAPTER_VERSION),
        runner_binary_sha256=(
            plan.codex.binary_sha256 if is_codex else plan.drift_agent.wheel_sha256
        ),
        model_id=(plan.codex.model_id if is_codex else "zero-model-portable-v1"),
        effective_request_sha256=canonical_sha256(asdict(result.request)),
        rendered_input_sha256=result.request.stdin_sha256,
        terminal=terminal,
        pre_snapshot_digest=prepared.snapshot_digest,
        post_snapshot_digest=post_snapshot_digest,
        pre_git_metadata_sha256=git_metadata_sha256(prepared.prepared_git_metadata),
        post_git_metadata_sha256=post_git_metadata_sha256,
        streams=(
            _stream_receipt(result.stdout, name=stdout_name),
            _stream_receipt(result.stderr, name="stderr"),
        ),
        redaction=RedactionReceiptV1(
            policy_version=REDACTION_POLICY_VERSION,
            replacement_count=replacement_count,
            secret_detected=(result.terminal.classification == "secret_leakage_detected"),
        ),
        final_result_sha256=_final_result_sha256(result),
        usage=_raw_usage(result),
    )


def _subject_artifact_names(result: SubjectRunResult) -> tuple[str, ...]:
    stream_names = (
        ("events.raw.jsonl", "events.redacted.jsonl")
        if result.subject == "codex"
        else ("stdout.raw.bin", "stdout.redacted.bin")
    )
    names = [
        "effective-request.json",
        "git-metadata.json",
        "input-snapshot.json",
        "output-snapshot.json",
        "raw-evidence.json",
        "stderr.raw.bin",
        "stderr.redacted.txt",
        "task.json",
        "terminal-receipt.json",
        *stream_names,
    ]
    if result.subject == "codex":
        names.append("prompt.sha256")
    if result.parsed_result is not None:
        names.append("final-result.json" if result.subject == "codex" else "bundle.json")
    return tuple(sorted(names))


def _write_subject_artifacts(
    *,
    directory: Path,
    prepared: PreparedBenchmarkCase,
    result: SubjectRunResult,
    post_snapshot: object,
    post_git_metadata: object,
    terminal: TerminalReceiptV1,
    evidence: RawRunEvidenceV1,
    observation: ComparisonObservationV1 | None,
) -> None:
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    directory.chmod(0o700)
    _write_private(
        directory / "input-snapshot.json",
        canonical_json_bytes(prepared.prepared_snapshot) + b"\n",
    )
    _write_private(
        directory / "output-snapshot.json",
        canonical_json_bytes(post_snapshot) + b"\n",
    )
    _write_private(
        directory / "git-metadata.json",
        canonical_json_bytes(
            {
                "before": prepared.prepared_git_metadata,
                "after": post_git_metadata,
            }
        )
        + b"\n",
    )
    _write_private(directory / "task.json", canonical_json_bytes(prepared.task) + b"\n")
    _write_private(
        directory / "effective-request.json",
        canonical_json_bytes(asdict(result.request)) + b"\n",
    )
    if result.subject == "codex":
        _write_private(
            directory / "events.raw.jsonl",
            result.stdout.sealed_raw,
        )
        _write_private(
            directory / "events.redacted.jsonl",
            result.stdout.redacted,
        )
        _write_private(
            directory / "prompt.sha256",
            f"{result.request.stdin_sha256}\n".encode("ascii"),
        )
    else:
        _write_private(directory / "stdout.raw.bin", result.stdout.sealed_raw)
        _write_private(directory / "stdout.redacted.bin", result.stdout.redacted)
    _write_private(directory / "stderr.raw.bin", result.stderr.sealed_raw)
    _write_private(directory / "stderr.redacted.txt", result.stderr.redacted)
    if result.parsed_result is not None:
        final_name = "final-result.json" if result.subject == "codex" else "bundle.json"
        _write_private(
            directory / final_name,
            canonical_json_bytes(result.parsed_result) + b"\n",
        )
    _write_private(
        directory / "terminal-receipt.json",
        canonical_json_bytes(terminal) + b"\n",
    )
    _write_private(
        directory / "raw-evidence.json",
        canonical_json_bytes(evidence) + b"\n",
    )
    if observation is not None:
        _write_private(
            directory / "observation.json",
            canonical_json_bytes(observation) + b"\n",
        )


def _control_terminal(
    *,
    plan: BenchmarkPlanV1,
    slot_id: str,
    case_id: str,
    duration_ms: int,
) -> TerminalReceiptV1:
    return TerminalReceiptV1(
        plan_digest=plan.plan_digest,
        slot_id=slot_id,
        run_class="control",
        subject="drift_agent",
        dataset_id="stage3-v1",
        case_id=case_id,
        trial_id="control-1",
        process_started=True,
        terminal_classification="completed",
        exit_code=0,
        duration_ms=duration_ms,
        available_artifacts=(
            "control-result.json",
            "stage3-evaluation.json",
            "terminal-receipt.json",
        ),
    )


def _failure_counts(entries: list[CoverageEntryV1]) -> tuple[FailureCountV1, ...]:
    counts: dict[str, int] = {}
    for entry in entries:
        if entry.terminal_classification != "completed":
            counts[entry.terminal_classification] = counts.get(entry.terminal_classification, 0) + 1
    return tuple(
        FailureCountV1(failure_class=cast(Any, name), count=count)
        for name, count in sorted(counts.items())
    )


def _coverage_report(
    plan: BenchmarkPlanV1,
    entries: list[CoverageEntryV1],
) -> CoverageReportV1:
    paired_slots = 12 * len(plan.trial_ids)
    execution_accounted = len(entries) == len(plan.schedule)
    portable = [entry for entry in entries if entry.run_class == "portable"]
    groups: dict[tuple[str, str, str], list[CoverageEntryV1]] = {}
    for entry in portable:
        groups.setdefault((entry.dataset_id, entry.case_id, entry.trial_id), []).append(entry)
    portable_complete = len(groups) == paired_slots and all(
        {entry.subject for entry in group} == {"codex", "drift_agent"}
        and all(entry.observation_sha256 is not None for entry in group)
        for group in groups.values()
    )
    controls = [entry for entry in entries if entry.run_class == "control"]
    controls_complete = len(controls) == 6 and all(
        entry.control_result_sha256 is not None for entry in controls
    )
    return CoverageReportV1(
        plan_digest=plan.plan_digest,
        paired_trial_slots=cast(Literal[12, 36], paired_slots),
        planned_subject_slots=cast(Literal[30, 78], len(plan.schedule)),
        entries=tuple(entries),
        failure_counts=_failure_counts(entries),
        execution_accounted=execution_accounted,
        portable_score_complete=portable_complete,
        controls_complete=controls_complete,
        benchmark_complete=(execution_accounted and portable_complete and controls_complete),
    )


def _authorization(
    *,
    plan: BenchmarkPlanV1,
    authorized_by: str,
) -> BenchmarkAuthorizationV1:
    return BenchmarkAuthorizationV1(
        plan_digest=plan.plan_digest,
        maximum_live_invocations=plan.limits.maximum_live_invocations,
        hard_token_cap_available=False,
        hard_cost_cap_available=False,
        authorized_by=authorized_by,
        authorized_at=datetime.now(UTC).isoformat(),
    )


def _permission_child_config(
    *,
    path: str,
    home: Path,
    tmpdir: Path,
) -> str:
    values = {
        "PATH": path,
        "HOME": os.fspath(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TMPDIR": os.fspath(tmpdir),
        "PYTHONNOUSERSITE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_ADDOPTS": "-p no:cacheprovider",
    }
    return "{" + ",".join(f"{key}={json.dumps(value)}" for key, value in values.items()) + "}"


def _run_codex_sandbox_sentinel(
    *,
    planned: PlannedRuntime,
    batch_runtime: Path,
    artifact_root: Path,
    source_root: Path,
    isolated_auth_home: Path,
) -> str:
    """Prove the exact spawned-command boundary without contacting a model."""

    sentinel_root = batch_runtime / "sentinel"
    repo = sentinel_root / f"repo-{secrets.token_hex(16)}"
    child_home = batch_runtime / "ephemeral" / "sentinel" / "home"
    child_tmp = batch_runtime / "ephemeral" / "sentinel" / "tmp"
    sibling = batch_runtime / "sentinel-sibling" / "canary.txt"
    artifact_canary = artifact_root / ".sandbox-canary"
    for directory in (repo, child_home, child_tmp, sibling.parent):
        directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    _write_private(sibling, b"sibling-workspace-canary\n")
    _write_private(artifact_canary, b"trusted-artifact-canary\n")
    git_canary = repo / ".git" / "read-only-canary"
    _write_private(git_canary, b"git-metadata-canary\n")
    temp_canaries: list[Path] = []
    for index, temp_root in enumerate(_system_temp_roots(), start=1):
        canary = temp_root / f"drift-benchmark-canary-{secrets.token_hex(12)}-{index}"
        _write_private(canary, b"system-temp-canary\n")
        temp_canaries.append(canary)

    forbidden = (
        isolated_auth_home / "auth.json",
        source_root / "pyproject.toml",
        artifact_canary,
        sibling,
        planned.slim.wheel,
    )
    script = (
        "import errno,importlib.util,json,os,socket,subprocess,sys;from pathlib import Path;"
        "forbidden=json.loads(sys.argv[1]);"
        "temp_carveouts=json.loads(sys.argv[9]);"
        "bad=[];"
        "\nfor raw in forbidden:\n"
        " p=Path(raw)\n"
        " try: p.read_bytes()\n"
        " except OSError as e:\n"
        "  assert e.errno in (errno.EACCES,errno.EPERM), (raw,e.errno)\n"
        " else: bad.append(raw)\n"
        "\nassert not bad, bad\n"
        "\nfor raw in forbidden:\n"
        " try: Path(raw).write_bytes(b'forbidden')\n"
        " except OSError as e:\n"
        "  assert e.errno in (errno.EACCES,errno.EPERM), (raw,e.errno)\n"
        " else: raise AssertionError(('writable',raw))\n"
        "\nfor raw in temp_carveouts:\n"
        " p=Path(raw)\n"
        " try: contents=p.read_bytes()\n"
        " except OSError as e:\n"
        "  assert e.errno in (errno.EACCES,errno.EPERM), (raw,e.errno)\n"
        " else: assert contents==b'system-temp-canary\\n', raw\n"
        "assert 'CODEX_HOME' not in os.environ\n"
        "assert 'OPENAI_API_KEY' not in os.environ\n"
        "assert importlib.util.find_spec('drift_agent') is None;"
        "probe=Path.cwd()/'.sentinel-write';probe.write_text('ok');"
        "assert probe.read_text()=='ok';probe.unlink();"
        "ephemeral=Path(sys.argv[2])/'write-probe';"
        "ephemeral.write_text('ok');ephemeral.unlink();"
        "git=subprocess.run([sys.argv[3],'--version'],check=True,capture_output=True,text=True);"
        "assert git.stdout.strip()==sys.argv[4],git.stdout;"
        "pytest=subprocess.run([sys.argv[5],'--version'],check=True,capture_output=True,text=True);"
        "assert pytest.stdout.strip()==sys.argv[6],pytest.stdout;"
        "git_canary=Path(sys.argv[7]);assert git_canary.read_bytes()==b'git-metadata-canary\\n';"
        "\ntry: git_canary.write_bytes(b'forbidden')\n"
        "except OSError as e:\n assert e.errno in (errno.EACCES,errno.EPERM)\n"
        "else:\n raise AssertionError('git metadata writable')\n"
        "neutral_probe=Path(sys.argv[8])/'forbidden-write';"
        "\ntry: neutral_probe.write_bytes(b'forbidden')\n"
        "except OSError as e:\n assert e.errno in (errno.EACCES,errno.EPERM)\n"
        "else:\n raise AssertionError('neutral toolchain writable')\n"
        "\ntry:\n"
        " s=socket.socket();result=s.connect_ex(('127.0.0.1',9));s.close()\n"
        "except PermissionError:\n pass\n"
        "else:\n assert result in (errno.EACCES,errno.EPERM), result\n"
        f"print({_SENTINEL_SUCCESS!r})"
    )
    child_config = _permission_child_config(
        path=planned.toolchain.bin_path,
        home=child_home,
        tmpdir=child_tmp,
    )
    argv = [
        os.fspath(planned.codex.path),
        "sandbox",
        "-P",
        _PERMISSION_PROFILE,
        "-C",
        os.fspath(repo),
        "-c",
        'shell_environment_policy.inherit="none"',
        "-c",
        f"shell_environment_policy.set={child_config}",
        os.fspath(planned.toolchain.python.path.resolve()),
        "-c",
        script,
        json.dumps([os.fspath(path) for path in forbidden]),
        os.fspath(child_tmp),
        os.fspath(planned.toolchain.git.path),
        planned.toolchain.git.version,
        os.fspath(planned.toolchain.pytest.path),
        planned.toolchain.pytest.version,
        os.fspath(git_canary),
        os.fspath(planned.toolchain.root),
        json.dumps([os.fspath(path) for path in temp_canaries]),
    ]
    parent_home = sentinel_root / "parent-home"
    parent_tmp = sentinel_root / "parent-tmp"
    parent_home.mkdir(mode=0o700)
    parent_tmp.mkdir(mode=0o700)
    environment = {
        "CODEX_HOME": os.fspath(isolated_auth_home),
        "HOME": os.fspath(parent_home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        "TMPDIR": os.fspath(parent_tmp),
    }
    try:
        completed = subprocess.run(
            argv,
            cwd=repo,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            shell=False,
            timeout=60,
        )
    finally:
        artifact_canary.unlink(missing_ok=True)
        for canary in temp_canaries:
            canary.unlink(missing_ok=True)
    if completed.returncode != 0 or completed.stdout.strip() != _SENTINEL_SUCCESS:
        detail = (completed.stderr or completed.stdout)[-500:].strip()
        raise BenchmarkHarnessError(
            f"Codex no-model sandbox sentinel failed closed "
            f"(exit={completed.returncode}, detail={detail!r})"
        )
    return canonical_sha256(
        {
            "version": _SENTINEL_SUCCESS,
            "permission_config_sha256": sha256_file(isolated_auth_home / "config.toml"),
            "forbidden_count": len(forbidden),
            "system_temp_denied": False,
            "system_temp_policy": "platform-carveout-no-benchmark-data",
            "system_temp_canary_count": len(temp_canaries),
        }
    )


def _prepared_pair_projection(prepared: PreparedBenchmarkCase) -> tuple[str, str, str]:
    return (
        prepared.snapshot_digest,
        prepared.task_digest,
        prepared.scope_digest,
    )


def _render_ratio(value: object) -> str:
    status = getattr(value, "status", "not_measured")
    numerator = getattr(value, "numerator", None)
    denominator = getattr(value, "denominator", None)
    return f"{numerator}/{denominator}" if status == "measured" else "not_measured"


def render_benchmark_markdown(
    *,
    headline: BenchmarkReportV1,
    comparison: Stage4ComparisonReport,
    controls: ControlReportV1,
) -> str:
    systems = {system.subject: system for system in comparison.systems}
    lines = [
        "# Codex vs Drift Agent — frozen-case conformance smoke",
        "",
        f"- Benchmark complete: {'yes' if headline.coverage.benchmark_complete else 'no'}",
        f"- Plan digest: `{headline.plan_digest}`",
        f"- Unique paired cases: {headline.unique_paired_cases}",
        f"- Paired trial slots: {headline.paired_trial_slots}",
        f"- Controls passed: {controls.summary.passed}/{controls.summary.planned}",
        "- Structural interpretation: `frozen-policy-conformance-only`",
        "- This is a 12-case smoke, not a statistical or general superiority claim.",
        "",
        "## Paired quality",
        "",
        "| Subject | Passed | TP | FP | FN | Precision | Recall | F1 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for subject in ("codex", "drift_agent"):
        system = systems[subject]
        quality = system.quality
        lines.append(
            "| "
            + " | ".join(
                (
                    subject,
                    _render_ratio(quality.passed),
                    str(quality.tp),
                    str(quality.fp),
                    str(quality.fn),
                    _render_ratio(quality.precision),
                    _render_ratio(quality.recall),
                    _render_ratio(quality.f1),
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Coverage",
            "",
            f"- Execution accounted: {headline.coverage.execution_accounted}",
            f"- Portable score complete: {headline.coverage.portable_score_complete}",
            f"- Controls complete: {headline.coverage.controls_complete}",
            "",
            "## V1 limitations",
            "",
        ]
    )
    lines.extend(f"- `{metric}`" for metric in headline.missing_metrics)
    lines.extend(
        [
            "",
            "> Codex was launched by the local supervisor, but the existing normalized V1 "
            "provenance contract intentionally remains `unverified_external_declaration`. "
            "Process authorization and raw evidence are bound through the batch ledger.",
            "",
        ]
    )
    return "\n".join(lines)


def _publish_reports(
    *,
    plan: BenchmarkPlanV1,
    artifacts: Path,
    observations: list[ComparisonObservationV1],
    coverage: CoverageReportV1,
    control_results: list[ControlResultV1],
) -> tuple[Stage4ComparisonReport, ControlReportV1, BenchmarkReportV1]:
    comparison = build_stage4_comparison(observations)
    ordered_controls = tuple(sorted(control_results, key=lambda item: item.case_id))
    passed = sum(result.passed for result in ordered_controls)
    controls = ControlReportV1(
        plan_digest=plan.plan_digest,
        results=ordered_controls,
        summary=ControlSummaryV1(
            passed=passed,
            failed=6 - passed,
            control_all_passed=passed == 6,
        ),
    )
    comparison_payloads = stage4_comparison_artifacts(comparison)
    for name, raw in comparison_payloads.items():
        _write_private(artifacts / name, raw)
    coverage_raw = canonical_json_bytes(coverage) + b"\n"
    controls_raw = canonical_json_bytes(controls) + b"\n"
    adjudication = {
        "schema_version": 1,
        "plan_digest": plan.plan_digest,
        "status": "not_scored_in_v1",
        "candidates": [],
    }
    adjudication_raw = canonical_json_bytes(adjudication) + b"\n"
    _write_private(artifacts / "coverage-report.json", coverage_raw)
    _write_private(artifacts / "control-report.json", controls_raw)
    _write_private(artifacts / "adjudication-sidecar.json", adjudication_raw)
    headline = BenchmarkReportV1(
        plan_digest=plan.plan_digest,
        paired_trial_slots=cast(Literal[12, 36], 12 * len(plan.trial_ids)),
        coverage=BenchmarkCoverageSummaryV1(
            execution_accounted=coverage.execution_accounted,
            portable_score_complete=coverage.portable_score_complete,
            controls_complete=coverage.controls_complete,
            benchmark_complete=coverage.benchmark_complete,
        ),
        failure_counts=coverage.failure_counts,
        control_summary=controls.summary,
        missing_metrics=V1_MISSING_METRICS,
        artifacts=BenchmarkArtifactDigestsV1(
            coverage_report_sha256=bytes_sha256(coverage_raw),
            comparison_report_sha256=bytes_sha256(comparison_payloads["comparison-report.json"]),
            control_report_sha256=bytes_sha256(controls_raw),
            adjudication_sidecar_sha256=bytes_sha256(adjudication_raw),
        ),
    )
    _write_private(
        artifacts / "benchmark-report.json",
        canonical_json_bytes(headline) + b"\n",
    )
    _write_private(
        artifacts / "benchmark-report.md",
        render_benchmark_markdown(
            headline=headline,
            comparison=comparison,
            controls=controls,
        ).encode("utf-8"),
    )
    return comparison, controls, headline


def run_benchmark(
    *,
    plan_path: Path,
    artifacts_dir: Path,
    authorize_live_codex: bool,
    codex_binary: Path | None = None,
    source_root: Path | None = None,
    codex_auth_home: Path | None = None,
    authorized_by: str = "local-user",
    progress: Any | None = None,
) -> BenchmarkRunArtifacts:
    """Run one no-retry smoke or full batch and immediately score sealed evidence."""

    if not authorize_live_codex:
        raise BenchmarkHarnessError("live benchmark requires explicit --authorize-live-codex")
    root = (source_root or _source_root()).resolve()
    plan = load_benchmark_plan(plan_path)
    artifact_root = _external_artifact_directory(artifacts_dir, source_root=root)
    planned = verify_planned_runtime(
        plan=plan,
        plan_path=plan_path,
        codex_binary=codex_binary,
        source_root=root,
    )
    authorization = _authorization(plan=plan, authorized_by=authorized_by)
    authorization_raw = canonical_json_bytes(authorization) + b"\n"
    _write_private(artifact_root / "authorization.json", authorization_raw)
    authorization_sha256 = bytes_sha256(canonical_json_bytes(authorization))
    _write_private(
        artifact_root / "benchmark-plan.json",
        canonical_json_bytes(plan) + b"\n",
    )

    batch_token = secrets.token_hex(12)
    batch_runtime = planned.root / "batches" / batch_token
    batch_runtime.mkdir(parents=True, exist_ok=False, mode=0o700)
    isolated_auth_home = batch_runtime / "codex-home"
    ephemeral_root = batch_runtime / "ephemeral"
    ephemeral_root.mkdir(mode=0o700)
    denied_roots = (Path.home(), artifact_root)
    output_schema = planned.root / "public-contracts" / "CodexTaskResultV1.schema.json"
    if canonical_sha256(_strict_json(output_schema.read_bytes())) != (
        plan.contracts.codex_output_schema_sha256
    ):
        raise BenchmarkHarnessError("public Codex output schema changed after planning")

    auth_source = codex_auth_home or Path.home() / ".codex"
    permission_config_sha256: str | None = None
    try:
        copy_isolated_codex_auth(
            source_home=auth_source,
            destination_home=isolated_auth_home,
        )
        permission_config = build_codex_permission_config(
            codex_home=isolated_auth_home,
            neutral_toolchain_root=planned.toolchain.root,
            ephemeral_root=ephemeral_root,
            denied_roots=(planned.root, batch_runtime, *denied_roots),
            toolchain_read_roots=(_APPLE_DEVELOPER_TOOLS,),
            profile_name=_PERMISSION_PROFILE,
        )
        permission_config_sha256 = permission_config.sha256
        sentinel_sha256 = _run_codex_sandbox_sentinel(
            planned=planned,
            batch_runtime=batch_runtime,
            artifact_root=artifact_root,
            source_root=root,
            isolated_auth_home=isolated_auth_home,
        )
    finally:
        shutil.rmtree(isolated_auth_home, ignore_errors=True)
    _write_private(
        artifact_root / "sandbox-sentinel.json",
        canonical_json_bytes(
            {
                "schema_version": 1,
                "status": "passed",
                "sentinel_sha256": sentinel_sha256,
                "permission_config_sha256": permission_config_sha256,
                "live_invocations_before_sentinel": 0,
                "system_temp_denied": False,
                "system_temp_policy": "platform-carveout-no-benchmark-data",
            }
        )
        + b"\n",
    )

    process_runner = BoundedSubprocessRunner()
    paired_projection: dict[tuple[str, str], tuple[str, str, str]] = {}
    observations: list[ComparisonObservationV1] = []
    control_results: list[ControlResultV1] = []
    coverage_entries: list[CoverageEntryV1] = []

    for slot in plan.schedule:
        if slot.run_class == "control":
            started = time.monotonic()
            evaluation = Stage3EvaluationRunner().run_case(slot.case_id)
            duration_ms = max(0, int((time.monotonic() - started) * 1000))
            control_evidence_sha = canonical_sha256(
                {
                    "plan_digest": plan.plan_digest,
                    "slot_id": slot.slot_id,
                    "evaluation": evaluation,
                }
            )
            control = ControlResultV1(
                plan_digest=plan.plan_digest,
                case_id=slot.case_id,
                case_manifest_sha256=next(
                    item.case_manifest_sha256
                    for item in plan.control_cases
                    if item.case_id == slot.case_id
                ),
                runner_contract_sha256=sha256_file(
                    root / "src" / "drift_agent" / "evaluation" / "stage3_runner.py"
                ),
                evidence_sha256=control_evidence_sha,
                evaluation=evaluation,
            )
            terminal = _control_terminal(
                plan=plan,
                slot_id=slot.slot_id,
                case_id=slot.case_id,
                duration_ms=duration_ms,
            )
            directory = (
                artifact_root / "runs" / "controls" / slot.case_id / "control-1" / "drift_agent"
            )
            directory.mkdir(parents=True, exist_ok=False, mode=0o700)
            _write_private(
                directory / "stage3-evaluation.json",
                canonical_json_bytes(evaluation) + b"\n",
            )
            _write_private(
                directory / "control-result.json",
                canonical_json_bytes(control) + b"\n",
            )
            _write_private(
                directory / "terminal-receipt.json",
                canonical_json_bytes(terminal) + b"\n",
            )
            control_results.append(control)
            coverage_entries.append(
                CoverageEntryV1(
                    slot_id=slot.slot_id,
                    run_class="control",
                    subject="drift_agent",
                    dataset_id="stage3-v1",
                    case_id=slot.case_id,
                    trial_id="control-1",
                    terminal_classification="completed",
                    terminal_receipt_sha256=canonical_sha256(terminal),
                    control_result_sha256=canonical_sha256(control),
                )
            )
            if callable(progress):
                progress(
                    f"[{slot.ordinal}/{len(plan.schedule)}] control "
                    f"{slot.case_id}: {'pass' if evaluation.passed else 'fail'}"
                )
            continue

        workspace = batch_runtime / "subjects" / slot.slot_id
        workspace.mkdir(parents=True, exist_ok=False, mode=0o700)
        prepared = prepare_benchmark_case(slot.case_id, workspace)
        prepared.repo_path.chmod(0o700)
        key = (slot.case_id, slot.trial_id)
        projection = _prepared_pair_projection(prepared)
        prior = paired_projection.setdefault(key, projection)
        if prior != projection:
            raise BenchmarkHarnessError("paired subjects received different prepared inputs")
        state = workspace / "state"
        slot_ephemeral = ephemeral_root / slot.slot_id
        home = slot_ephemeral / "parent-home"
        temporary = slot_ephemeral / "parent-tmp"
        child_home = slot_ephemeral / "child-home"
        child_tmp = slot_ephemeral / "child-tmp"
        for directory in (state, home, temporary, child_home, child_tmp):
            directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        if slot.subject == "codex":
            try:
                auth_path = copy_isolated_codex_auth(
                    source_home=auth_source,
                    destination_home=isolated_auth_home,
                )
                permission_config = build_codex_permission_config(
                    codex_home=isolated_auth_home,
                    neutral_toolchain_root=planned.toolchain.root,
                    ephemeral_root=ephemeral_root,
                    denied_roots=(planned.root, batch_runtime, *denied_roots),
                    toolchain_read_roots=(_APPLE_DEVELOPER_TOOLS,),
                    profile_name=_PERMISSION_PROFILE,
                )
                if permission_config.sha256 != permission_config_sha256:
                    raise BenchmarkHarnessError("Codex permission profile changed after sentinel")
                sensitive_values = codex_auth_sensitive_values(auth_path)
                result = run_codex_subject(
                    executable=planned.codex.path,
                    runner=process_runner,
                    task=prepared.task,
                    repo=prepared.repo_path,
                    model=plan.codex.model_id,
                    output_schema=output_schema,
                    path="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
                    home=home,
                    tmpdir=temporary,
                    child_path=planned.toolchain.bin_path,
                    child_home=child_home,
                    child_tmpdir=child_tmp,
                    codex_home=isolated_auth_home,
                    sensitive_values=sensitive_values,
                    live=True,
                    authorize_live_codex=True,
                    permission_profile=_PERMISSION_PROFILE,
                    reasoning_effort=plan.codex.reasoning_effort,
                    timeout_seconds=plan.limits.hard_wall_timeout_seconds,
                    stdout_limit_bytes=plan.limits.max_raw_stream_bytes,
                    stderr_limit_bytes=plan.limits.max_stderr_bytes,
                )
            finally:
                shutil.rmtree(isolated_auth_home, ignore_errors=True)
            if result.terminal.classification == "secret_leakage_detected":
                raise BenchmarkHarnessError(
                    "credential leakage detected; batch stopped and isolated auth destroyed"
                )
        else:
            result = run_drift_subject(
                executable=planned.slim.executable,
                runner=process_runner,
                task=prepared.task,
                repo=prepared.repo_path,
                state_dir=state,
                path=planned.toolchain.bin_path,
                home=home,
                tmpdir=temporary,
                timeout_seconds=plan.limits.hard_wall_timeout_seconds,
                stdout_limit_bytes=plan.limits.max_raw_stream_bytes,
                stderr_limit_bytes=plan.limits.max_stderr_bytes,
            )
        post_snapshot = capture_repository_snapshot(prepared.repo_path)
        post_git_metadata = capture_git_metadata(prepared.repo_path)
        terminal = _terminal_receipt(
            plan=plan,
            slot_id=slot.slot_id,
            prepared=prepared,
            trial_id=slot.trial_id,
            result=result,
            available_artifacts=_subject_artifact_names(result),
        )
        evidence = _raw_evidence(
            plan=plan,
            authorization_sha256=authorization_sha256,
            prepared=prepared,
            trial_id=slot.trial_id,
            result=result,
            terminal=terminal,
            post_snapshot_digest=canonical_digest(post_snapshot),
            post_git_metadata_sha256=git_metadata_sha256(post_git_metadata),
        )
        observation: ComparisonObservationV1 | None = None
        if result.terminal.scoreable:
            from drift_agent.evaluation.benchmark_scoring import score_subject_run

            observation = score_subject_run(
                prepared,
                result,
                prepared.prepared_snapshot,
                post_snapshot,
                prepared.prepared_git_metadata,
                post_git_metadata,
                evidence=evidence,
                budget_source=plan.budget_source,
            )
            observations.append(observation)
        run_directory = (
            artifact_root / "runs" / "portable" / slot.case_id / slot.trial_id / slot.subject
        )
        _write_subject_artifacts(
            directory=run_directory,
            prepared=prepared,
            result=result,
            post_snapshot=post_snapshot,
            post_git_metadata=post_git_metadata,
            terminal=terminal,
            evidence=evidence,
            observation=observation,
        )
        coverage_entries.append(
            CoverageEntryV1(
                slot_id=slot.slot_id,
                run_class="portable",
                subject=slot.subject,
                dataset_id=slot.dataset_id,
                case_id=slot.case_id,
                trial_id=slot.trial_id,
                terminal_classification=terminal.terminal_classification,
                terminal_receipt_sha256=canonical_sha256(terminal),
                observation_sha256=(None if observation is None else canonical_sha256(observation)),
            )
        )
        if callable(progress):
            progress(
                f"[{slot.ordinal}/{len(plan.schedule)}] {slot.subject} "
                f"{slot.case_id}: {terminal.terminal_classification}"
            )
        if (
            slot.subject == "codex"
            and terminal.terminal_classification in _BATCH_STOPPING_CODEX_FAILURES
        ):
            _write_private(
                artifact_root / "batch-stop.json",
                canonical_json_bytes(
                    {
                        "schema_version": 1,
                        "plan_digest": plan.plan_digest,
                        "slot_id": slot.slot_id,
                        "terminal_classification": terminal.terminal_classification,
                        "terminal_receipt_sha256": canonical_sha256(terminal),
                        "reason": "batch-wide Codex infrastructure or protocol failure",
                    }
                )
                + b"\n",
            )
            raise BenchmarkHarnessError(
                "Codex batch-wide failure stopped remaining live invocations: "
                f"{terminal.terminal_classification}"
            )

    coverage = _coverage_report(plan, coverage_entries)
    comparison, controls, headline = _publish_reports(
        plan=plan,
        artifacts=artifact_root,
        observations=observations,
        coverage=coverage,
        control_results=control_results,
    )
    return BenchmarkRunArtifacts(
        artifacts_dir=artifact_root,
        plan_digest=plan.plan_digest,
        coverage=coverage,
        comparison=comparison,
        controls=controls,
        headline=headline,
    )


def _load_model_json(path: Path, model: Any) -> Any:
    try:
        raw = path.read_bytes()
        _strict_json(raw)
        result = model.model_validate_json(raw)
        if raw != canonical_json_bytes(result) + b"\n":
            raise ValueError("artifact is not the canonical serialized model")
        return result
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError, ValueError) as error:
        raise BenchmarkHarnessError(f"invalid benchmark artifact: {path.name}") from error


def _sealed_artifact_bytes(path: Path, *, byte_limit: int) -> bytes:
    """Read one sealed artifact without following a replacement symlink."""

    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ValueError("sealed artifact must be a regular file")
        if metadata.st_size > byte_limit:
            raise ValueError("sealed artifact exceeds its planned byte limit")
        return path.read_bytes()
    except (OSError, ValueError) as error:
        raise BenchmarkHarnessError(f"invalid sealed benchmark artifact: {path.name}") from error


def _validate_sealed_streams(
    *,
    directory: Path,
    evidence: RawRunEvidenceV1,
    artifact_byte_limit: int,
) -> None:
    expected_names = {"events", "stderr"} if evidence.subject == "codex" else {"stdout", "stderr"}
    receipts = {receipt.stream_name: receipt for receipt in evidence.streams}
    if set(receipts) != expected_names:
        raise BenchmarkHarnessError("sealed stream receipts differ from the subject contract")
    paths = {
        "events": ("events.raw.jsonl", "events.redacted.jsonl"),
        "stdout": ("stdout.raw.bin", "stdout.redacted.bin"),
        "stderr": ("stderr.raw.bin", "stderr.redacted.txt"),
    }
    for name, receipt in receipts.items():
        raw_name, redacted_name = paths[name]
        raw = _sealed_artifact_bytes(
            directory / raw_name,
            byte_limit=min(artifact_byte_limit, receipt.byte_limit),
        )
        redacted = _sealed_artifact_bytes(
            directory / redacted_name,
            byte_limit=artifact_byte_limit,
        )
        if (
            len(raw) != receipt.captured_bytes
            or bytes_sha256(raw) != receipt.raw_sha256
            or bytes_sha256(redacted) != receipt.redacted_sha256
            or (receipt.replacement_count == 0 and raw != redacted)
        ):
            raise BenchmarkHarnessError(f"sealed {name} stream differs from its receipt")


def _validate_sealed_final(
    *,
    directory: Path,
    evidence: RawRunEvidenceV1,
) -> None:
    expected_name = "final-result.json" if evidence.subject == "codex" else "bundle.json"
    unexpected_name = "bundle.json" if evidence.subject == "codex" else "final-result.json"
    expected = directory / expected_name
    unexpected = directory / unexpected_name
    if unexpected.exists() or unexpected.is_symlink():
        raise BenchmarkHarnessError("subject directory contains a foreign final artifact")
    if evidence.final_result_sha256 is None:
        if expected.exists() or expected.is_symlink():
            raise BenchmarkHarnessError("uncommitted final artifact is present")
        return
    model = CodexTaskResultV1 if evidence.subject == "codex" else PublicBundleV3
    final = _load_model_json(expected, model)
    if canonical_sha256(final) != evidence.final_result_sha256:
        raise BenchmarkHarnessError("final artifact differs from sealed raw evidence")


def rebuild_benchmark_reports(
    *,
    plan_path: Path,
    artifacts_dir: Path,
) -> BenchmarkRunArtifacts:
    """Offline digest validation and deterministic report regeneration."""

    plan = load_benchmark_plan(plan_path)
    artifacts = artifacts_dir.expanduser().absolute().resolve()
    if not artifacts.is_dir():
        raise BenchmarkHarnessError("benchmark artifacts directory is unavailable")
    authorization = cast(
        BenchmarkAuthorizationV1,
        _load_model_json(artifacts / "authorization.json", BenchmarkAuthorizationV1),
    )
    if (
        authorization.plan_digest != plan.plan_digest
        or authorization.maximum_live_invocations != plan.limits.maximum_live_invocations
    ):
        raise BenchmarkHarnessError("authorization ledger belongs to a different benchmark plan")
    authorization_sha256 = bytes_sha256(canonical_json_bytes(authorization))
    observations: list[ComparisonObservationV1] = []
    controls: list[ControlResultV1] = []
    entries: list[CoverageEntryV1] = []
    for slot in plan.schedule:
        if slot.run_class == "portable":
            directory = (
                artifacts / "runs" / "portable" / slot.case_id / slot.trial_id / slot.subject
            )
            terminal = cast(
                TerminalReceiptV1,
                _load_model_json(directory / "terminal-receipt.json", TerminalReceiptV1),
            )
            evidence = cast(
                RawRunEvidenceV1,
                _load_model_json(directory / "raw-evidence.json", RawRunEvidenceV1),
            )
            if terminal != evidence.terminal or evidence.plan_digest != plan.plan_digest:
                raise BenchmarkHarnessError("raw evidence is not bound to the planned terminal")
            if (
                terminal.slot_id != slot.slot_id
                or terminal.run_class != slot.run_class
                or terminal.subject != slot.subject
                or terminal.dataset_id != slot.dataset_id
                or terminal.case_id != slot.case_id
                or terminal.trial_id != slot.trial_id
                or evidence.authorization_ledger_sha256 != authorization_sha256
            ):
                raise BenchmarkHarnessError("portable evidence identity or authorization differs")
            _validate_sealed_streams(
                directory=directory,
                evidence=evidence,
                artifact_byte_limit=plan.limits.max_artifact_bytes,
            )
            _validate_sealed_final(directory=directory, evidence=evidence)
            observation: ComparisonObservationV1 | None = None
            observation_path = directory / "observation.json"
            if observation_path.is_file():
                observation = cast(
                    ComparisonObservationV1,
                    _load_model_json(observation_path, ComparisonObservationV1),
                )
                if observation.evidence_sha256 != evidence.evidence_sha256:
                    raise BenchmarkHarnessError(
                        "observation evidence digest differs from sealed raw evidence"
                    )
                expected_observation_id = deterministic_observation_id(
                    plan_digest=plan.plan_digest,
                    subject=slot.subject,
                    pair_key=observation.pair_key,
                    evidence_sha256=evidence.evidence_sha256,
                )
                if (
                    observation.observation_id != expected_observation_id
                    or observation.subject != slot.subject
                    or observation.dataset_id != slot.dataset_id
                    or observation.case_id != slot.case_id
                    or observation.trial_id != slot.trial_id
                    or observation.case_manifest_sha256 != evidence.case_manifest_sha256
                    or observation.snapshot_digest != evidence.snapshot_digest
                    or observation.task_digest != evidence.task_digest
                    or observation.scope_digest != evidence.scope_digest
                ):
                    raise BenchmarkHarnessError("observation identity differs from sealed evidence")
                observations.append(observation)
            entries.append(
                CoverageEntryV1(
                    slot_id=slot.slot_id,
                    run_class="portable",
                    subject=slot.subject,
                    dataset_id=slot.dataset_id,
                    case_id=slot.case_id,
                    trial_id=slot.trial_id,
                    terminal_classification=terminal.terminal_classification,
                    terminal_receipt_sha256=canonical_sha256(terminal),
                    observation_sha256=(
                        None if observation is None else canonical_sha256(observation)
                    ),
                )
            )
        else:
            directory = artifacts / "runs" / "controls" / slot.case_id / "control-1" / "drift_agent"
            terminal = cast(
                TerminalReceiptV1,
                _load_model_json(directory / "terminal-receipt.json", TerminalReceiptV1),
            )
            control = cast(
                ControlResultV1,
                _load_model_json(directory / "control-result.json", ControlResultV1),
            )
            if terminal.plan_digest != plan.plan_digest or control.plan_digest != plan.plan_digest:
                raise BenchmarkHarnessError("control artifact belongs to a different plan")
            controls.append(control)
            entries.append(
                CoverageEntryV1(
                    slot_id=slot.slot_id,
                    run_class="control",
                    subject="drift_agent",
                    dataset_id="stage3-v1",
                    case_id=slot.case_id,
                    trial_id="control-1",
                    terminal_classification=terminal.terminal_classification,
                    terminal_receipt_sha256=canonical_sha256(terminal),
                    control_result_sha256=canonical_sha256(control),
                )
            )
    coverage = _coverage_report(plan, entries)
    comparison, control_report, headline = _publish_reports(
        plan=plan,
        artifacts=artifacts,
        observations=observations,
        coverage=coverage,
        control_results=controls,
    )
    return BenchmarkRunArtifacts(
        artifacts_dir=artifacts,
        plan_digest=plan.plan_digest,
        coverage=coverage,
        comparison=comparison,
        controls=control_report,
        headline=headline,
    )


__all__ = [
    "BenchmarkHarnessError",
    "BenchmarkRunArtifacts",
    "NeutralToolchain",
    "PlannedRuntime",
    "create_benchmark_plan",
    "load_benchmark_plan",
    "rebuild_benchmark_reports",
    "render_benchmark_markdown",
    "run_benchmark",
    "runtime_root_for_plan",
    "verify_planned_runtime",
]

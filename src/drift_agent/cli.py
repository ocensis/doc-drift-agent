import json
import os
from dataclasses import asdict
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, cast

import typer
from pydantic import ValidationError

from drift_agent import application
from drift_agent.adapters.ci import (
    ArtifactDirectoryError,
    prepare_artifact_directory,
    write_ci_artifacts,
)
from drift_agent.adapters.contracts import (
    PublicBundleV3,
    blocking_finding_count,
    bounded_bundle,
)
from drift_agent.agent.budget import BudgetExhausted
from drift_agent.config import ScaffoldError, scaffold_config
from drift_agent.domain.enums import RunMode, RunStatus, ValidationStatus
from drift_agent.domain.models import RunBudgets, RunRequest, ScopeSpec
from drift_agent.domain.serialization import WireVersion, bundle_to_json
from drift_agent.domain.status import exit_code_for_status
from drift_agent.memory import (
    AliasAddRequest,
    AliasRevokeRequest,
    AliasService,
    DecisionAddRequest,
    DecisionRevokeRequest,
    DecisionService,
    SQLiteStateStore,
)
from drift_agent.model.client import ModelClientError
from drift_agent.model.openrouter import ModelConfigurationError
from drift_agent.model.probe import probe_openrouter
from drift_agent.workspace.identity import resolve_identities, resolve_state_path


class OutputFormat(StrEnum):
    HUMAN = "human"
    JSON = "json"


class ModelProfileOption(StrEnum):
    FAST = "fast"
    STRONG = "strong"


class BenchmarkReasoningOption(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"


app = typer.Typer(no_args_is_help=True)
decision_app = typer.Typer(no_args_is_help=True)
alias_app = typer.Typer(no_args_is_help=True)
model_app = typer.Typer(no_args_is_help=True)
ci_app = typer.Typer(no_args_is_help=True)
benchmark_app = typer.Typer(no_args_is_help=True)
app.add_typer(decision_app, name="decision")
app.add_typer(alias_app, name="alias")
app.add_typer(model_app, name="model")
app.add_typer(ci_app, name="ci")
if os.environ.get("DRIFT_AGENT_SUBJECT_RUNTIME") != "1":
    app.add_typer(benchmark_app, name="benchmark")


def _state_store(repo: Path, state_dir: Path | None) -> tuple[SQLiteStateStore, str]:
    identities = resolve_identities(repo.resolve())
    path = resolve_state_path(repo.resolve(), state_dir, identities=identities)
    store = SQLiteStateStore(path)
    store.register_identities(identities)
    return store, identities.repository.digest


def _exit_code(status: RunStatus) -> int:
    return exit_code_for_status(status)


def _scope_from_since(since: str | None) -> ScopeSpec:
    try:
        return ScopeSpec(kind="since", revision=since) if since is not None else ScopeSpec()
    except ValidationError:
        raise typer.BadParameter(
            "revision must be non-empty and contain no control characters",
            param_hint="--since",
        ) from None


def _execute(
    mode: RunMode,
    repo: Path,
    output_format: OutputFormat,
    *,
    output_version: int = 1,
    semantic_analysis: bool = False,
    semantic_repair: bool = False,
    since: str | None = None,
    state_dir: Path | None = None,
    lock_timeout_seconds: float = 5.0,
    max_findings: int | None = None,
    summary_only: bool = False,
    max_model_calls: int | None = None,
) -> None:
    if type(output_version) is not int or output_version not in {1, 2, 3}:
        raise typer.BadParameter("output version must be 1, 2, or 3")
    if (
        (semantic_analysis or semantic_repair)
        and output_format is OutputFormat.JSON
        and output_version != 3
    ):
        raise typer.BadParameter("--semantic JSON output requires --output-version 3")
    bounding = max_findings is not None or summary_only
    if bounding and (output_format is not OutputFormat.JSON or output_version != 3):
        raise typer.BadParameter(
            "--max-findings/--summary require --format json --output-version 3"
        )
    request_kwargs: dict[str, object] = {}
    if max_model_calls is not None:
        request_kwargs["budgets"] = RunBudgets(
            max_model_calls_per_run=max_model_calls,
            max_input_tokens_per_run=max(20_000, max_model_calls * 8_000),
        )
    bundle = application.run(
        RunRequest(
            mode=mode,
            repo_path=repo.resolve(),
            scope=_scope_from_since(since),
            state_dir=state_dir,
            lock_timeout_seconds=lock_timeout_seconds,
            semantic_analysis=semantic_analysis,
            semantic_repair=semantic_repair,
            **request_kwargs,  # type: ignore[arg-type]
        )
    )
    if output_format is OutputFormat.JSON:
        if bounding:
            public = bounded_bundle(
                PublicBundleV3.from_bundle(bundle),
                max_findings=max_findings,
                summary_only=summary_only,
            )
            typer.echo(
                json.dumps(
                    public.model_dump(mode="json"),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        else:
            typer.echo(bundle_to_json(bundle, cast(WireVersion, output_version)))
    else:
        typer.echo(f"status: {bundle.status}")
        for path in bundle.changes.files:
            typer.echo(f"changed: {path}")
        if bundle.status is RunStatus.FAILED:
            for item in bundle.validation:
                if item.required and item.status in {
                    ValidationStatus.FAILED,
                    ValidationStatus.UNAVAILABLE,
                }:
                    typer.echo(f"{item.check}: {item.summary}")
    raise typer.Exit(_exit_code(bundle.status))


def _benchmark_failure(action: str, error: Exception) -> None:
    typer.echo(f"benchmark {action} failed: {error}", err=True)
    raise typer.Exit(2)


@benchmark_app.command("plan")
def benchmark_plan(
    codex_model: Annotated[str, typer.Option("--codex-model")],
    output: Annotated[Path, typer.Option("--output")],
    reasoning_effort: Annotated[
        BenchmarkReasoningOption,
        typer.Option("--reasoning-effort"),
    ] = BenchmarkReasoningOption.LOW,
    trials: Annotated[
        int,
        typer.Option("--trials", help="1 for the smoke batch or 3 for the full batch."),
    ] = 1,
    codex_binary: Annotated[Path | None, typer.Option("--codex-binary")] = None,
    timeout_seconds: Annotated[
        int,
        typer.Option("--timeout-seconds", min=1, max=3600),
    ] = 120,
) -> None:
    """Audit the frozen suite and create a deterministic non-live plan."""

    if trials not in {1, 3}:
        raise typer.BadParameter("trials must be 1 (smoke) or 3 (full)", param_hint="--trials")
    if not codex_model or codex_model != codex_model.strip():
        raise typer.BadParameter(
            "Codex model must be a non-empty explicit identifier",
            param_hint="--codex-model",
        )
    try:
        from drift_agent.evaluation.benchmark_harness import create_benchmark_plan

        plan = create_benchmark_plan(
            output_path=output,
            codex_model=codex_model,
            reasoning_effort=reasoning_effort.value,
            trials=cast(Literal[1, 3], trials),
            codex_binary=codex_binary,
            timeout_seconds=timeout_seconds,
        )
    except Exception as error:  # the CLI is the final error-to-exit boundary
        _benchmark_failure("plan", error)
        return
    typer.echo(f"plan: {output.expanduser().absolute()}")
    typer.echo(f"plan digest: {plan.plan_digest}")
    typer.echo(f"portable pairs: {12 * trials}")
    typer.echo(f"Codex live invocation ceiling: {plan.limits.maximum_live_invocations}")
    typer.echo(f"hard wall timeout per subject: {plan.limits.hard_wall_timeout_seconds}s")
    typer.echo("warning: Codex CLI has no enforced token or billed-cost cap")


@benchmark_app.command("run")
def benchmark_run(
    plan_path: Annotated[
        Path,
        typer.Option("--plan", exists=True, dir_okay=False),
    ],
    artifacts_dir: Annotated[Path, typer.Option("--artifacts-dir")],
    authorize_live_codex: Annotated[
        bool,
        typer.Option("--authorize-live-codex"),
    ] = False,
    codex_binary: Annotated[Path | None, typer.Option("--codex-binary")] = None,
    codex_auth_home: Annotated[Path | None, typer.Option("--codex-auth-home")] = None,
) -> None:
    """Execute every planned subject once, with no automatic retry."""

    try:
        from drift_agent.evaluation.benchmark_harness import (
            load_benchmark_plan,
            run_benchmark,
        )

        plan = load_benchmark_plan(plan_path)
        typer.echo(f"plan digest: {plan.plan_digest}")
        typer.echo(f"live Codex invocation ceiling: {plan.limits.maximum_live_invocations}")
        typer.echo(f"hard wall timeout per subject: {plan.limits.hard_wall_timeout_seconds}s")
        typer.echo(f"sealed raw evidence: {artifacts_dir.expanduser().absolute()}")
        typer.echo("warning: no token or billed-cost hard cap is available")
        if not authorize_live_codex:
            raise ValueError("missing explicit --authorize-live-codex")
        typer.echo("authorization method: explicit_cli_flag (--authorize-live-codex)")
        result = run_benchmark(
            plan_path=plan_path,
            artifacts_dir=artifacts_dir,
            authorize_live_codex=True,
            codex_binary=codex_binary,
            codex_auth_home=codex_auth_home,
            progress=typer.echo,
        )
    except Exception as error:  # the CLI is the final error-to-exit boundary
        _benchmark_failure("run", error)
        return
    typer.echo(f"benchmark complete: {result.headline.coverage.benchmark_complete}")
    typer.echo(f"report: {result.artifacts_dir / 'benchmark-report.md'}")
    if not result.headline.coverage.benchmark_complete:
        raise typer.Exit(2)


def _benchmark_rebuild(plan_path: Path, artifacts_dir: Path, action: str) -> None:
    try:
        from drift_agent.evaluation.benchmark_harness import rebuild_benchmark_reports

        result = rebuild_benchmark_reports(
            plan_path=plan_path,
            artifacts_dir=artifacts_dir,
        )
    except Exception as error:  # the CLI is the final error-to-exit boundary
        _benchmark_failure(action, error)
        return
    typer.echo(f"plan digest: {result.plan_digest}")
    typer.echo(f"benchmark complete: {result.headline.coverage.benchmark_complete}")
    typer.echo(f"report: {result.artifacts_dir / 'benchmark-report.md'}")


@benchmark_app.command("score")
def benchmark_score(
    plan_path: Annotated[
        Path,
        typer.Option("--plan", exists=True, dir_okay=False),
    ],
    artifacts_dir: Annotated[
        Path,
        typer.Option("--artifacts-dir", exists=True, file_okay=False),
    ],
) -> None:
    """Offline-validate sealed evidence and regenerate deterministic scores."""

    _benchmark_rebuild(plan_path, artifacts_dir, "score")


@benchmark_app.command("report")
def benchmark_report(
    plan_path: Annotated[
        Path,
        typer.Option("--plan", exists=True, dir_okay=False),
    ],
    artifacts_dir: Annotated[
        Path,
        typer.Option("--artifacts-dir", exists=True, file_okay=False),
    ],
) -> None:
    """Offline-regenerate the plan-aware JSON and Markdown headline."""

    _benchmark_rebuild(plan_path, artifacts_dir, "report")


@ci_app.command("check")
def ci_check(
    since: Annotated[str, typer.Option("--since")],
    state_dir: Annotated[Path, typer.Option("--state-dir")],
    artifacts_dir: Annotated[
        Path,
        typer.Option("--artifacts-dir"),
    ],
    repo: Annotated[
        Path,
        typer.Option(exists=True, file_okay=False),
    ] = Path("."),
    semantic_analysis: Annotated[bool, typer.Option("--semantic")] = False,
) -> None:
    """Run a read-only committed-range gate and write portable CI artifacts."""

    resolved_repo = repo.resolve()
    scope = _scope_from_since(since)
    try:
        paths = prepare_artifact_directory(
            resolved_repo,
            artifacts_dir,
            state_dir=state_dir,
        )
    except (ArtifactDirectoryError, OSError, RuntimeError) as error:
        raise typer.BadParameter(
            str(error),
            param_hint="--artifacts-dir",
        ) from None
    bundle = application.run(
        RunRequest(
            mode=RunMode.CHECK,
            repo_path=resolved_repo,
            scope=scope,
            state_dir=paths.state_directory,
            semantic_analysis=semantic_analysis,
        )
    )
    try:
        public = write_ci_artifacts(bundle, paths, revision=since)
    except (OSError, ValidationError) as error:
        typer.echo(f"CI artifact publication failed: {type(error).__name__}", err=True)
        raise typer.Exit(2) from None
    typer.echo(f"status: {bundle.status.value}")
    # The exit code still answers "were there findings"; it is this line that says
    # how many of them assert a defect. The gate reports, the workflow decides.
    typer.echo(f"blocking: {blocking_finding_count(public)}")
    typer.echo(f"bundle: {paths.bundle}")
    typer.echo(f"summary: {paths.summary}")
    typer.echo(f"sarif: {paths.sarif}")
    raise typer.Exit(_exit_code(bundle.status))


@app.command()
def check(
    repo: Annotated[
        Path,
        typer.Option(exists=True, file_okay=False),
    ] = Path("."),
    output_format: Annotated[
        OutputFormat,
        typer.Option("--format"),
    ] = OutputFormat.HUMAN,
    output_version: Annotated[int, typer.Option("--output-version", min=1, max=3)] = 1,
    semantic_analysis: Annotated[bool, typer.Option("--semantic")] = False,
    since: Annotated[str | None, typer.Option("--since")] = None,
    state_dir: Annotated[Path | None, typer.Option("--state-dir")] = None,
    lock_timeout_seconds: Annotated[
        float,
        typer.Option("--lock-timeout-seconds", min=0.0),
    ] = 5.0,
    max_findings: Annotated[int | None, typer.Option("--max-findings", min=0)] = None,
    summary_only: Annotated[bool, typer.Option("--summary")] = False,
    max_model_calls: Annotated[int | None, typer.Option("--max-model-calls", min=0)] = None,
) -> None:
    _execute(
        RunMode.CHECK,
        repo,
        output_format,
        output_version=output_version,
        semantic_analysis=semantic_analysis,
        since=since,
        state_dir=state_dir,
        lock_timeout_seconds=lock_timeout_seconds,
        max_findings=max_findings,
        summary_only=summary_only,
        max_model_calls=max_model_calls,
    )


@app.command()
def repair(
    repo: Annotated[
        Path,
        typer.Option(exists=True, file_okay=False),
    ] = Path("."),
    output_format: Annotated[
        OutputFormat,
        typer.Option("--format"),
    ] = OutputFormat.HUMAN,
    output_version: Annotated[int, typer.Option("--output-version", min=1, max=3)] = 1,
    semantic_repair: Annotated[bool, typer.Option("--semantic")] = False,
    since: Annotated[str | None, typer.Option("--since")] = None,
    state_dir: Annotated[Path | None, typer.Option("--state-dir")] = None,
    lock_timeout_seconds: Annotated[
        float,
        typer.Option("--lock-timeout-seconds", min=0.0),
    ] = 5.0,
    max_findings: Annotated[int | None, typer.Option("--max-findings", min=0)] = None,
    summary_only: Annotated[bool, typer.Option("--summary")] = False,
) -> None:
    _execute(
        RunMode.REPAIR,
        repo,
        output_format,
        output_version=output_version,
        semantic_repair=semantic_repair,
        since=since,
        state_dir=state_dir,
        lock_timeout_seconds=lock_timeout_seconds,
        max_findings=max_findings,
        summary_only=summary_only,
    )


@app.command()
def init(
    repo: Annotated[
        Path,
        typer.Option(exists=True, file_okay=False),
    ] = Path("."),
) -> None:
    try:
        config_path, config = scaffold_config(repo.resolve())
    except (ScaffoldError, OSError) as error:
        raise typer.BadParameter(str(error), param_hint="--repo") from None
    typer.echo(f"created: {config_path}")
    typer.echo(f"source_roots: {', '.join(config.project.source_roots)}")
    typer.echo(f"docs_roots: {', '.join(config.project.docs_roots) or '(none found)'}")
    typer.echo("review the [truth] sections to classify documentation before repair")


@model_app.command("probe")
def model_probe(
    profile: Annotated[
        ModelProfileOption,
        typer.Option("--profile"),
    ] = ModelProfileOption.FAST,
    model: Annotated[str | None, typer.Option("--model")] = None,
    output_format: Annotated[
        OutputFormat,
        typer.Option("--format"),
    ] = OutputFormat.HUMAN,
) -> None:
    """Make one small, explicit OpenRouter structured-output request."""

    try:
        report = probe_openrouter(
            profile=profile.value,
            model_override=model,
        )
    except (ModelConfigurationError, ModelClientError, BudgetExhausted) as error:
        typer.echo(f"model probe failed: {error.reason_code}", err=True)
        raise typer.Exit(2) from None
    except (ValidationError, ValueError):
        typer.echo("model probe failed: invalid_model_response", err=True)
        raise typer.Exit(2) from None
    if output_format is OutputFormat.JSON:
        typer.echo(report.model_dump_json())
        return
    typer.echo(f"status: {report.status}")
    typer.echo(f"provider: {report.provider}")
    typer.echo(f"profile: {report.profile}")
    typer.echo(f"model: {report.model}")
    typer.echo(f"request id: {report.request_id}")
    typer.echo(f"prompt tokens: {report.usage.prompt_tokens}")
    typer.echo(f"completion tokens: {report.usage.completion_tokens}")
    typer.echo(f"cost usd: {report.usage.cost_usd:.8f}")


@decision_app.command("add")
def decision_add(
    repo: Annotated[Path, typer.Option(exists=True, file_okay=False)] = Path("."),
    run_id: Annotated[str, typer.Option("--run-id")] = "",
    finding_id: Annotated[str, typer.Option("--finding-id")] = "",
    action: Annotated[str, typer.Option("--action")] = "ignore",
    reason: Annotated[str, typer.Option("--reason")] = "",
    actor: Annotated[str, typer.Option("--actor")] = "",
    confirm: Annotated[bool, typer.Option("--confirm")] = False,
    state_dir: Annotated[Path | None, typer.Option("--state-dir")] = None,
) -> None:
    if action not in {"ignore", "false_positive"}:
        raise typer.BadParameter("action must be ignore or false_positive")
    store, repository_id = _state_store(repo, state_dir)
    record = DecisionService(store).add(
        DecisionAddRequest(
            repository_id=repository_id,
            run_id=run_id,
            finding_id=finding_id,
            action=action,  # type: ignore[arg-type]
            reason=reason,
            actor=actor,
            confirmation=confirm,
        )
    )
    typer.echo(json.dumps(asdict(record), ensure_ascii=False, sort_keys=True))


@decision_app.command("list")
def decision_list(
    repo: Annotated[Path, typer.Option(exists=True, file_okay=False)] = Path("."),
    state_dir: Annotated[Path | None, typer.Option("--state-dir")] = None,
    include_revoked: Annotated[bool, typer.Option("--include-revoked")] = True,
) -> None:
    store, repository_id = _state_store(repo, state_dir)
    records = DecisionService(store).list(
        repository_id,
        include_revoked=include_revoked,
    )
    typer.echo(
        json.dumps([asdict(record) for record in records], ensure_ascii=False, sort_keys=True)
    )


@decision_app.command("revoke")
def decision_revoke(
    repo: Annotated[Path, typer.Option(exists=True, file_okay=False)] = Path("."),
    decision_id: Annotated[str, typer.Option("--decision-id")] = "",
    reason: Annotated[str, typer.Option("--reason")] = "",
    actor: Annotated[str, typer.Option("--actor")] = "",
    state_dir: Annotated[Path | None, typer.Option("--state-dir")] = None,
) -> None:
    store, repository_id = _state_store(repo, state_dir)
    record = DecisionService(store).revoke(
        DecisionRevokeRequest(
            repository_id=repository_id,
            decision_id=decision_id,
            reason=reason,
            actor=actor,
        )
    )
    typer.echo(json.dumps(asdict(record), ensure_ascii=False, sort_keys=True))


@alias_app.command("add")
def alias_add(
    repo: Annotated[Path, typer.Option(exists=True, file_okay=False)] = Path("."),
    run_id: Annotated[str, typer.Option("--run-id")] = "",
    alignment_id: Annotated[str, typer.Option("--alignment-id")] = "",
    reason: Annotated[str, typer.Option("--reason")] = "",
    actor: Annotated[str, typer.Option("--actor")] = "",
    confirm: Annotated[bool, typer.Option("--confirm")] = False,
    state_dir: Annotated[Path | None, typer.Option("--state-dir")] = None,
) -> None:
    store, repository_id = _state_store(repo, state_dir)
    record = AliasService(store).add(
        AliasAddRequest(
            repository_id=repository_id,
            run_id=run_id,
            alignment_id=alignment_id,
            reason=reason,
            actor=actor,
            confirmation=confirm,
        )
    )
    typer.echo(json.dumps(asdict(record), ensure_ascii=False, sort_keys=True))


@alias_app.command("list")
def alias_list(
    repo: Annotated[Path, typer.Option(exists=True, file_okay=False)] = Path("."),
    state_dir: Annotated[Path | None, typer.Option("--state-dir")] = None,
    include_revoked: Annotated[bool, typer.Option("--include-revoked")] = True,
) -> None:
    store, repository_id = _state_store(repo, state_dir)
    records = AliasService(store).list(
        repository_id,
        include_revoked=include_revoked,
    )
    typer.echo(
        json.dumps([asdict(record) for record in records], ensure_ascii=False, sort_keys=True)
    )


@alias_app.command("revoke")
def alias_revoke(
    repo: Annotated[Path, typer.Option(exists=True, file_okay=False)] = Path("."),
    alias_id: Annotated[str, typer.Option("--alias-id")] = "",
    reason: Annotated[str, typer.Option("--reason")] = "",
    actor: Annotated[str, typer.Option("--actor")] = "",
    state_dir: Annotated[Path | None, typer.Option("--state-dir")] = None,
) -> None:
    store, repository_id = _state_store(repo, state_dir)
    record = AliasService(store).revoke(
        AliasRevokeRequest(
            repository_id=repository_id,
            alias_id=alias_id,
            reason=reason,
            actor=actor,
        )
    )
    typer.echo(json.dumps(asdict(record), ensure_ascii=False, sort_keys=True))

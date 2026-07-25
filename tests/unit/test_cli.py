import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from drift_agent import application
from drift_agent.cli import _exit_code, app
from drift_agent.domain.enums import RunStatus
from drift_agent.domain.models import RunRequest, VerifiedRepairBundle
from drift_agent.model.client import ModelClientError
from drift_agent.model.contracts import ModelCallUsage
from drift_agent.model.probe import ModelProbeReport

runner = CliRunner()


def _flattened(output: str) -> str:
    """Rich wraps its error panel at the terminal width, which differs between a
    developer's terminal and a CI runner. Strip the styling and the wrapping so
    an assertion tests the message rather than the environment."""

    plain = re.sub(r"\x1b\[[0-9;]*m", "", output)
    plain = plain.replace("\u2502", " ")
    return re.sub(r"\s+", " ", plain)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (RunStatus.CLEAN, 0),
        (RunStatus.FIXED, 0),
        (RunStatus.DRIFT_FOUND, 1),
        (RunStatus.PARTIAL, 1),
        (RunStatus.NEEDS_APPROVAL, 1),
        (RunStatus.UNRESOLVED, 1),
        (RunStatus.STALE, 2),
        (RunStatus.FAILED, 2),
    ],
)
def test_exit_code_maps_run_statuses(status: RunStatus, expected: int) -> None:
    assert _exit_code(status) == expected


def test_check_json_returns_exit_one_for_drift(drift_repo: Path) -> None:
    result = runner.invoke(
        app,
        ["check", "--repo", str(drift_repo), "--format", "json"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "drift_found"
    assert payload["changes"]["applied"] is False


def test_repair_human_output_reports_verified_change(drift_repo: Path) -> None:
    result = runner.invoke(app, ["repair", "--repo", str(drift_repo)])

    assert result.exit_code == 0
    assert "status: fixed" in result.stdout
    assert "changed: docs/api.md" in result.stdout


def test_default_v1_and_explicit_v2_json_keep_legacy_semantics(
    drift_repo: Path,
) -> None:
    default = runner.invoke(
        app,
        ["check", "--repo", str(drift_repo), "--format", "json"],
    )
    explicit = runner.invoke(
        app,
        [
            "check",
            "--repo",
            str(drift_repo),
            "--format",
            "json",
            "--output-version",
            "2",
        ],
    )

    assert default.exit_code == explicit.exit_code == 1
    v1 = json.loads(default.stdout)
    v2 = json.loads(explicit.stdout)
    assert "schema_version" not in v1
    assert v2["schema_version"] == 2
    assert {
        "repository_id",
        "workspace_id",
        "suppressed_findings",
        "memory_events",
        "repair_groups",
        "residual_changes",
    }.isdisjoint(v1)
    assert v2["status"] == v1["status"] == "drift_found"
    assert len(v1["findings"]) == len(v2["findings"]) == 1
    legacy = v1["findings"][0]
    detailed = v2["findings"][0]
    assert legacy["type"] == detailed["type"] == "signature_drift"
    for field in (
        "id",
        "symbol_id",
        "type",
        "disposition",
        "truth_source",
        "reason",
    ):
        assert detailed[field] == legacy[field]


def test_check_json_v3_max_findings_zero_returns_summary_only_payload(
    drift_repo: Path,
) -> None:
    result = runner.invoke(
        app,
        [
            "check",
            "--repo",
            str(drift_repo),
            "--format",
            "json",
            "--output-version",
            "3",
            "--max-findings",
            "0",
        ],
    )

    assert result.exit_code == 1
    assert "\n" not in result.stdout.strip()  # bounded output stays compact
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 3
    assert payload["status"] == "drift_found"
    assert payload["findings"] == []
    assert payload["omitted_findings"] == 1
    assert payload["findings_summary"]["total_findings"] == 1


def test_check_summary_flag_matches_bounded_v3_shape(drift_repo: Path) -> None:
    result = runner.invoke(
        app,
        [
            "check",
            "--repo",
            str(drift_repo),
            "--format",
            "json",
            "--output-version",
            "3",
            "--summary",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["findings"] == []
    assert payload["omitted_findings"] == 1
    assert payload["findings_summary"]["total_findings"] == 1


def test_repair_json_v3_summary_flag_bounds_fixed_findings(drift_repo: Path) -> None:
    result = runner.invoke(
        app,
        [
            "repair",
            "--repo",
            str(drift_repo),
            "--format",
            "json",
            "--output-version",
            "3",
            "--summary",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "fixed"
    assert payload["findings"] == []
    assert payload["omitted_findings"] == 1
    assert payload["findings_summary"]["by_disposition"] == {"fixed": 1}


@pytest.mark.parametrize("command", ["check", "repair"])
@pytest.mark.parametrize(
    "arguments",
    [
        ["--format", "json", "--max-findings", "1"],
        ["--format", "json", "--output-version", "2", "--max-findings", "1"],
        ["--summary"],
    ],
    ids=["v1-json", "v2-json", "human"],
)
def test_bounding_options_require_json_v3(
    drift_repo: Path, command: str, arguments: list[str]
) -> None:
    result = runner.invoke(app, [command, "--repo", str(drift_repo), *arguments])

    assert result.exit_code == 2
    flattened = _flattened(result.output)
    assert "--max-findings/--summary require" in flattened
    assert "--output-version 3" in flattened


def test_explicit_v3_json_is_forwarded_without_enabling_semantic_analysis(
    drift_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_run = application.run
    requests = []

    def capture_request(request: RunRequest) -> VerifiedRepairBundle:
        requests.append(request)
        return original_run(request)

    monkeypatch.setattr(application, "run", capture_request)

    result = runner.invoke(
        app,
        [
            "check",
            "--repo",
            str(drift_repo),
            "--format",
            "json",
            "--output-version",
            "3",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 3
    assert payload["status"] == "drift_found"
    assert all(finding["type"] == "signature_drift" for finding in payload["findings"])
    assert len(requests) == 1
    assert requests[0].semantic_analysis is False


def test_explicit_v3_with_semantic_flag_enables_semantic_analysis(
    drift_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_run = application.run
    requests = []

    def capture_request(request: RunRequest) -> VerifiedRepairBundle:
        requests.append(request)
        return original_run(request)

    monkeypatch.setattr(application, "run", capture_request)

    result = runner.invoke(
        app,
        [
            "check",
            "--repo",
            str(drift_repo),
            "--format",
            "json",
            "--output-version",
            "3",
            "--semantic",
        ],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["schema_version"] == 3
    assert len(requests) == 1
    assert requests[0].semantic_analysis is True
    assert requests[0].semantic_repair is False


def test_repair_semantic_flag_enables_repair_capability(
    drift_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_run = application.run
    requests = []

    def capture_request(request: RunRequest) -> VerifiedRepairBundle:
        requests.append(request)
        return original_run(request)

    monkeypatch.setattr(application, "run", capture_request)

    result = runner.invoke(
        app,
        [
            "repair",
            "--repo",
            str(drift_repo),
            "--format",
            "json",
            "--output-version",
            "3",
            "--semantic",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["schema_version"] == 3
    assert len(requests) == 1
    assert requests[0].semantic_analysis is False
    assert requests[0].semantic_repair is True


@pytest.mark.parametrize(
    "version_arguments",
    [[], ["--output-version", "2"]],
    ids=["default-v1", "explicit-v2"],
)
@pytest.mark.parametrize("command", ["check", "repair"])
def test_semantic_json_requires_v3_before_application_run(
    drift_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    version_arguments: list[str],
    command: str,
) -> None:
    calls: list[object] = []

    def unexpected_run(request: object) -> None:
        calls.append(request)
        raise AssertionError("application.run must not be called")

    monkeypatch.setattr(application, "run", unexpected_run)

    result = runner.invoke(
        app,
        [
            command,
            "--repo",
            str(drift_repo),
            "--format",
            "json",
            "--semantic",
            *version_arguments,
        ],
    )

    assert result.exit_code == 2
    assert "--semantic JSON output requires --output-version 3" in result.output
    assert calls == []


def test_cli_rejects_unknown_output_version_before_application_run(
    drift_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(application, "run", calls.append)

    result = runner.invoke(
        app,
        [
            "check",
            "--repo",
            str(drift_repo),
            "--format",
            "json",
            "--output-version",
            "4",
        ],
    )

    assert result.exit_code == 2
    assert calls == []


def test_model_probe_json_reports_only_safe_connectivity_and_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = ModelProbeReport(
        status="connected",
        provider="openrouter",
        profile="fast",
        model="provider/model",
        request_id="gen-safe",
        usage=ModelCallUsage(
            prompt_tokens=9,
            completion_tokens=2,
            total_tokens=11,
            cost_usd=0.0002,
        ),
    )
    monkeypatch.setattr("drift_agent.cli.probe_openrouter", lambda **kwargs: report)

    result = runner.invoke(app, ["model", "probe", "--format", "json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == report.model_dump(mode="json")


def test_model_probe_human_output_includes_request_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = ModelProbeReport(
        status="connected",
        provider="openrouter",
        profile="fast",
        model="provider/model",
        request_id="gen-safe",
        usage=ModelCallUsage(
            prompt_tokens=9,
            completion_tokens=2,
            total_tokens=11,
            cost_usd=0.0002,
        ),
    )
    monkeypatch.setattr("drift_agent.cli.probe_openrouter", lambda **kwargs: report)

    result = runner.invoke(app, ["model", "probe"])

    assert result.exit_code == 0
    assert "request id: gen-safe" in result.stdout


def test_model_probe_failure_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(**kwargs: object) -> None:
        raise ModelClientError(
            "authentication_failed",
            status_code=401,
            provider_error_type="sensitive-provider-detail",
        )

    monkeypatch.setattr("drift_agent.cli.probe_openrouter", fail)

    result = runner.invoke(app, ["model", "probe"])

    assert result.exit_code == 2
    assert "model probe failed: authentication_failed" in result.output
    assert "sensitive-provider-detail" not in result.output


@pytest.mark.parametrize("command", ["check", "repair"])
def test_json_reports_config_guidance_for_unconfigured_repo(command: str, tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [command, "--repo", str(tmp_path), "--format", "json", "--output-version", "3"],
    )

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["status"] == "failed"
    assert len(payload["validation"]) == 1
    entry = payload["validation"][0]
    assert entry["check"] == "config"
    assert entry["status"] == "unavailable"
    assert entry["summary"].startswith("config.missing:")
    assert "drift-agent init" in entry["summary"]


def test_check_human_mode_prints_config_guidance(tmp_path: Path) -> None:
    result = runner.invoke(app, ["check", "--repo", str(tmp_path)])

    assert result.exit_code == 2
    assert "status: failed" in result.stdout
    assert "config: config.missing:" in result.stdout
    assert "drift-agent init" in result.stdout


def test_init_scaffolds_starter_config_once(tmp_path: Path) -> None:
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")

    result = runner.invoke(app, ["init", "--repo", str(tmp_path)])

    assert result.exit_code == 0
    assert (tmp_path / "drift-agent.toml").is_file()
    assert "created:" in result.stdout
    assert "review the [truth] sections" in result.stdout

    again = runner.invoke(app, ["init", "--repo", str(tmp_path)])

    assert again.exit_code == 2
    assert "already exists" in again.output


def test_init_fails_closed_for_unsupported_layout(tmp_path: Path) -> None:
    (tmp_path / "script.py").write_text("x = 1\n", encoding="utf-8")

    result = runner.invoke(app, ["init", "--repo", str(tmp_path)])

    assert result.exit_code == 2
    assert "could not infer a supported source layout" in result.output
    assert not (tmp_path / "drift-agent.toml").exists()

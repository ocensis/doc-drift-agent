from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from drift_agent import application
from drift_agent.adapters import ci as ci_adapter
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
from drift_agent.adapters.rendering import bundle_to_sarif, render_markdown_summary
from drift_agent.cli import app
from drift_agent.domain.enums import (
    FindingDisposition,
    RunMode,
    RunStatus,
    ValidationStatus,
)
from drift_agent.domain.models import (
    DriftFinding,
    EvidenceAnchor,
    Usage,
    ValidationResult,
    VerifiedRepairBundle,
    WorkspaceSnapshot,
)
from drift_agent.domain.serialization import bundle_to_wire
from drift_agent.domain.status import exit_code_for_status


def _bundle(
    *,
    status: RunStatus = RunStatus.DRIFT_FOUND,
    include_finding: bool = True,
    include_failed_validation: bool = False,
    disposition: FindingDisposition = FindingDisposition.DETECTED,
    reason_code: str = "parameter.added",
) -> VerifiedRepairBundle:
    findings = []
    if include_finding:
        findings.append(
            DriftFinding(
                id="finding-v3",
                symbol_id="demo.@unsafe|name\nnext",
                type="signature_drift",
                disposition=disposition,
                truth_source="code",
                code_evidence=EvidenceAnchor(
                    path="src/demo/<api>.py",
                    line=4,
                    source_hash="code-hash",
                    start_byte=9_999,
                    end_byte=10_999,
                ),
                doc_evidence=EvidenceAnchor(
                    path="docs/接口 @team|api.md",
                    line=7,
                    source_hash="doc-hash",
                    start_byte=20_000,
                    end_byte=25_000,
                ),
                reason="bad | @alice <script>alert(1)</script>\r\nnext",
                kind="parameter added/<script>",
                component_id="color",
                old_value={"type": "missing_parameter"},
                new_value={"name": "color", "required": False},
                detector_id="structural.signature",
                detector_version="2",
                fingerprint="stable-fingerprint",
                reason_code=reason_code,
            )
        )
    validation = []
    if include_failed_validation:
        validation.append(
            ValidationResult(
                finding_ids=[finding.id for finding in findings],
                attempt_id="validation-only",
                check="pytest | @reviewers <check>",
                required=True,
                status=ValidationStatus.FAILED,
                summary="failed\r\n@ops <b>inspect</b> | now",
                duration_ms=17,
            )
        )
    return VerifiedRepairBundle(
        status=status,
        run_id="run-@bot|<unsafe>\ncontinued",
        snapshot=WorkspaceSnapshot(
            head_revision="head",
            workspace_fingerprint="workspace-hash",
            input_file_hashes={
                "src/demo/<api>.py": "code-hash",
                "docs/接口 @team|api.md": "doc-hash",
            },
        ),
        scope=["src/demo/<api>.py"],
        findings=findings,
        validation=validation,
        usage=Usage(
            model_calls=1,
            validation_commands=1,
            tool_calls=2,
            duration_ms=23,
        ),
        repository_id="repository-id",
        workspace_id="workspace-id",
    )


def test_markdown_summary_is_deterministic_and_escapes_untrusted_cells() -> None:
    public = PublicBundleV3.from_bundle(_bundle(include_failed_validation=True))
    revision = "refs/heads/main|@reviewers\ncontinued"

    first = render_markdown_summary(public, revision=revision)
    second = render_markdown_summary(public, revision=revision)

    assert first == second
    assert "<!-- drift-agent:stage4 -->" in first
    assert "bad &#124; &#64;alice &lt;script&gt;alert(1)&lt;/script&gt;<br>next" in first
    assert "docs/接口 &#64;team&#124;api.md:7" in first
    assert "pytest &#124; &#64;reviewers &lt;check&gt;" in first
    assert "failed<br>&#64;ops &lt;b&gt;inspect&lt;/b&gt; &#124; now" in first
    assert "refs/heads/main&#124;&#64;reviewers<br>continued" in first
    assert "<script>" not in first
    assert "@alice" not in first


def test_validation_only_failure_is_visible_in_markdown_and_sarif() -> None:
    public = PublicBundleV3.from_bundle(
        _bundle(
            status=RunStatus.UNRESOLVED,
            include_finding=False,
            include_failed_validation=True,
        )
    )

    summary = render_markdown_summary(public)
    sarif = bundle_to_sarif(public)

    assert "Findings: 0" in summary
    assert "### Incomplete required validation" in summary
    assert "pytest &#124; &#64;reviewers &lt;check&gt;" in summary
    run = sarif["runs"][0]
    assert run["results"] == []
    assert run["invocations"][0]["toolExecutionNotifications"] == [
        {
            "level": "error",
            "message": {"text": "failed\r\n@ops <b>inspect</b> | now"},
            "descriptor": {"id": "pytest | @reviewers <check>"},
        }
    ]


def test_sarif_21_maps_rules_locations_fingerprint_without_byte_columns() -> None:
    public = PublicBundleV3.from_bundle(_bundle())

    sarif = bundle_to_sarif(public)

    assert sarif == bundle_to_sarif(public)
    assert sarif["version"] == "2.1.0"
    assert sarif["$schema"].endswith("sarif-2.1.0.json")
    run = sarif["runs"][0]
    rules = run["tool"]["driver"]["rules"]
    assert rules == [
        {
            "id": "drift-agent.parameter_added_script",
            "name": "parameter added/<script>",
            "shortDescription": {"text": "Documentation drift: parameter added/<script>"},
            "properties": {
                "findingType": "signature_drift",
                "detectorId": "structural.signature",
                "detectorVersion": "2",
            },
        }
    ]
    result = run["results"][0]
    assert result["ruleId"] == rules[0]["id"]
    assert result["ruleIndex"] == 0
    assert result["level"] == "warning"
    assert result["partialFingerprints"] == {"driftAgentFinding/v1": "stable-fingerprint"}
    assert result["locations"] == [
        {
            "physicalLocation": {
                "artifactLocation": {"uri": "docs/%E6%8E%A5%E5%8F%A3%20%40team%7Capi.md"},
                "region": {"startLine": 7},
            }
        }
    ]
    assert result["relatedLocations"] == [
        {
            "id": 1,
            "message": {"text": "Code evidence"},
            "physicalLocation": {
                "artifactLocation": {"uri": "src/demo/%3Capi%3E.py"},
                "region": {"startLine": 4},
            },
        }
    ]
    assert "startColumn" not in json.dumps(sarif)
    assert "endColumn" not in json.dumps(sarif)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (RunStatus.CLEAN, 0),
        (RunStatus.DRIFT_FOUND, 1),
        (RunStatus.FIXED, 0),
        (RunStatus.PARTIAL, 1),
        (RunStatus.NEEDS_APPROVAL, 1),
        (RunStatus.UNRESOLVED, 1),
        (RunStatus.STALE, 2),
        (RunStatus.FAILED, 2),
    ],
)
def test_sarif_reuses_shared_eight_status_exit_matrix(
    status: RunStatus,
    expected: int,
) -> None:
    public = PublicBundleV3.from_bundle(_bundle(status=status, include_finding=False))

    invocation = bundle_to_sarif(public)["runs"][0]["invocations"][0]

    assert exit_code_for_status(status) == expected
    assert invocation["exitCode"] == expected


@pytest.mark.parametrize(
    ("relative_directory", "message"),
    [
        (Path("artifacts"), "outside the worktree"),
        (Path(".git/drift-agent-ci"), "outside the worktree"),
        (Path("nested/../artifacts"), "parent traversal"),
    ],
)
@pytest.mark.parametrize("target", ["artifacts", "state"])
def test_ci_directories_reject_worktree_git_state_and_parent_traversal(
    drift_repo: Path,
    relative_directory: Path,
    message: str,
    target: str,
) -> None:
    artifacts_dir = drift_repo.parent / f"{drift_repo.name}-artifacts"
    state_dir = drift_repo.parent / f"{drift_repo.name}-state"
    if target == "artifacts":
        artifacts_dir = drift_repo / relative_directory
    else:
        state_dir = drift_repo / relative_directory

    with pytest.raises(ArtifactDirectoryError, match=message):
        prepare_artifact_directory(drift_repo, artifacts_dir, state_dir=state_dir)


@pytest.mark.parametrize("target", ["artifacts", "state"])
def test_ci_directories_reject_any_symlink_component(
    drift_repo: Path,
    target: str,
) -> None:
    outside = drift_repo.parent / f"{drift_repo.name}-outside-link"
    outside.mkdir()
    link = outside / "repo-link"
    link.symlink_to(drift_repo, target_is_directory=True)

    artifacts_dir = drift_repo.parent / f"{drift_repo.name}-artifacts"
    state_dir = drift_repo.parent / f"{drift_repo.name}-state"
    if target == "artifacts":
        artifacts_dir = link / "ci"
    else:
        state_dir = link / "ci"

    with pytest.raises(ArtifactDirectoryError, match="symlink component"):
        prepare_artifact_directory(drift_repo, artifacts_dir, state_dir=state_dir)


@pytest.mark.parametrize("target", ["artifacts", "state"])
def test_ci_paths_must_be_directories(drift_repo: Path, target: str) -> None:
    artifacts_dir = drift_repo.parent / f"{drift_repo.name}-artifacts"
    state_dir = drift_repo.parent / f"{drift_repo.name}-state"
    file_path = drift_repo.parent / f"{drift_repo.name}-{target}-file"
    file_path.write_text("not a directory", encoding="utf-8")
    if target == "artifacts":
        artifacts_dir = file_path
    else:
        state_dir = file_path

    with pytest.raises(ArtifactDirectoryError, match="must be a directory"):
        prepare_artifact_directory(drift_repo, artifacts_dir, state_dir=state_dir)


@pytest.mark.parametrize("relationship", ["same", "state_inside", "artifacts_inside"])
def test_state_and_artifact_directories_must_be_separate(
    drift_repo: Path,
    relationship: str,
) -> None:
    root = drift_repo.parent / f"{drift_repo.name}-ci-root"
    artifacts_dir = root / "artifacts"
    state_dir = root / "state"
    if relationship == "same":
        state_dir = artifacts_dir
    elif relationship == "state_inside":
        state_dir = artifacts_dir / "state"
    else:
        artifacts_dir = state_dir / "artifacts"

    with pytest.raises(
        ArtifactDirectoryError,
        match=r"must not be inside|must be separate",
    ):
        prepare_artifact_directory(drift_repo, artifacts_dir, state_dir=state_dir)


@pytest.mark.parametrize("omitted", ["since", "state", "artifacts"])
def test_ci_required_options_fail_before_application(
    drift_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    omitted: str,
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(application, "run", calls.append)
    args = ["ci", "check", "--repo", str(drift_repo)]
    options = {
        "since": ["--since", "HEAD"],
        "state": [
            "--state-dir",
            str(drift_repo.parent / f"{drift_repo.name}-state"),
        ],
        "artifacts": [
            "--artifacts-dir",
            str(drift_repo.parent / f"{drift_repo.name}-artifacts"),
        ],
    }
    for name, option in options.items():
        if name != omitted:
            args.extend(option)

    result = CliRunner().invoke(app, args)

    assert result.exit_code == 2
    assert calls == []


def test_ci_check_maps_one_check_since_request_with_explicit_external_state(
    drift_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def fake_run(request: object) -> VerifiedRepairBundle:
        calls.append(request)
        return _bundle(status=RunStatus.CLEAN, include_finding=False)

    monkeypatch.setattr(application, "run", fake_run)
    artifacts_dir = drift_repo.parent / f"{drift_repo.name}-cli-artifacts"
    state_dir = drift_repo.parent / f"{drift_repo.name}-cli-state"

    result = CliRunner().invoke(
        app,
        [
            "ci",
            "check",
            "--repo",
            str(drift_repo),
            "--since",
            "HEAD~1",
            "--state-dir",
            str(state_dir),
            "--artifacts-dir",
            str(artifacts_dir),
        ],
    )

    assert result.exit_code == 0
    assert len(calls) == 1
    request = calls[0]
    assert request.mode is RunMode.CHECK
    assert request.repo_path == drift_repo.resolve()
    assert request.scope.kind == "since"
    assert request.scope.revision == "HEAD~1"
    assert request.state_dir == state_dir.resolve()
    assert request.semantic_analysis is False
    assert {path.name for path in artifacts_dir.iterdir()} == {
        "bundle.json",
        "results.sarif",
        "summary.md",
        "pr-comment.md",
    }


def test_unverifiable_finding_annotates_as_a_note_rather_than_an_error() -> None:
    unresolved = {"disposition": FindingDisposition.UNRESOLVED, "status": RunStatus.UNRESOLVED}
    advisory = PublicBundleV3.from_bundle(
        _bundle(reason_code="unsupported.symbol_kind", **unresolved)
    )
    contradiction = PublicBundleV3.from_bundle(
        _bundle(reason_code="precondition_changed", **unresolved)
    )

    assert bundle_to_sarif(advisory)["runs"][0]["results"][0]["level"] == "note"
    assert bundle_to_sarif(contradiction)["runs"][0]["results"][0]["level"] == "error"
    # Both are still reported: advisory means "do not block", never "do not tell".
    assert len(advisory.findings) == 1
    assert blocking_finding_count(advisory) == 0
    assert blocking_finding_count(contradiction) == 1


def test_markdown_summary_separates_blocking_findings_from_advisory_ones() -> None:
    advisory = PublicBundleV3.from_bundle(
        _bundle(
            reason_code="unsupported.literal",
            disposition=FindingDisposition.UNRESOLVED,
            status=RunStatus.UNRESOLVED,
        )
    )

    summary = render_markdown_summary(advisory, revision="head")

    assert "Findings: 1" in summary
    assert "Blocking findings: 0 (1 advisory)" in summary
    assert "| advisory | unresolved |" in summary


def test_blocking_count_reads_complete_counts_when_findings_were_dropped() -> None:
    public = PublicBundleV3.from_bundle(_bundle(reason_code="precondition_changed"))

    bounded = bounded_bundle(public, summary_only=True)

    # The blocking finding is no longer inlined, so counting the visible list
    # would report zero and quietly turn a blocked merge into a passing one.
    assert bounded.findings == []
    assert bounded.omitted_findings == 1
    assert blocking_finding_count(bounded) == 1


def test_ci_check_reports_the_blocking_count_alongside_the_status(
    drift_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        application,
        "run",
        lambda request: _bundle(
            status=RunStatus.DRIFT_FOUND,
            reason_code="unsupported.markdown_claim",
            disposition=FindingDisposition.UNRESOLVED,
        ),
    )

    result = CliRunner().invoke(
        app,
        [
            "ci",
            "check",
            "--repo",
            str(drift_repo),
            "--since",
            "HEAD~1",
            "--state-dir",
            str(drift_repo.parent / f"{drift_repo.name}-blocking-state"),
            "--artifacts-dir",
            str(drift_repo.parent / f"{drift_repo.name}-blocking-artifacts"),
        ],
    )

    # The exit code still answers "were there findings"; the action reads the
    # count to decide whether any of them may block a merge.
    assert result.exit_code == 1
    assert "status: drift_found" in result.output
    assert "blocking: 0" in result.output


def test_ci_writer_emits_exact_v3_and_four_deterministic_files(
    drift_repo: Path,
) -> None:
    artifact_dir = drift_repo.parent / f"{drift_repo.name}-ci-output"
    state_dir = drift_repo.parent / f"{drift_repo.name}-ci-state"
    paths = prepare_artifact_directory(
        drift_repo,
        artifact_dir,
        state_dir=state_dir,
    )
    bundle = _bundle(include_failed_validation=True)

    public = write_ci_artifacts(bundle, paths, revision="refs/heads/main")
    expected_files = {paths.bundle, paths.sarif, paths.summary, paths.pr_comment}
    first_bytes = {path.name: path.read_bytes() for path in expected_files}
    write_ci_artifacts(bundle, paths, revision="refs/heads/main")

    assert set(paths.directory.iterdir()) == expected_files
    assert paths.state_directory == state_dir.resolve()
    assert json.loads(paths.bundle.read_text(encoding="utf-8")) == bundle_to_wire(
        bundle,
        3,
    )
    assert public.model_dump(mode="json") == bundle_to_wire(bundle, 3)
    assert json.loads(paths.sarif.read_text(encoding="utf-8"))["version"] == "2.1.0"
    assert paths.summary.read_bytes() == paths.pr_comment.read_bytes()
    assert b"Baseline revision: `refs/heads/main`" in paths.summary.read_bytes()
    assert {path.name: path.read_bytes() for path in expected_files} == first_bytes


def test_ci_writer_removes_the_known_set_after_mid_publication_failure(
    drift_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = prepare_artifact_directory(
        drift_repo,
        drift_repo.parent / f"{drift_repo.name}-failed-output",
        state_dir=drift_repo.parent / f"{drift_repo.name}-failed-state",
    )
    for path in (paths.bundle, paths.sarif, paths.summary, paths.pr_comment):
        path.write_text("previous run", encoding="utf-8")
    real_atomic_write = ci_adapter._atomic_write

    def fail_on_summary(path: Path, data: bytes) -> None:
        if path == paths.summary:
            raise OSError("simulated publication failure")
        real_atomic_write(path, data)

    monkeypatch.setattr(ci_adapter, "_atomic_write", fail_on_summary)

    with pytest.raises(OSError, match="simulated publication failure"):
        write_ci_artifacts(_bundle(), paths, revision="HEAD~1")

    assert not any(
        path.exists() for path in (paths.bundle, paths.sarif, paths.summary, paths.pr_comment)
    )
    assert list(paths.directory.glob(".*.tmp")) == []

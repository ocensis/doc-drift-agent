import shutil
import subprocess
from pathlib import Path

import pytest

from drift_agent import application as application_module
from drift_agent.agent.state import AgentState
from drift_agent.application import AgentRuntime, run
from drift_agent.detectors.structural import StructuralDetector
from drift_agent.domain.enums import (
    FindingDisposition,
    RunMode,
    RunStatus,
    ValidationStatus,
)
from drift_agent.domain.models import (
    Alignment,
    DriftFinding,
    PatchAttempt,
    RunRequest,
    ValidationResult,
)
from drift_agent.hashing import sha256_file
from drift_agent.repair.signature import SignaturePatcher
from drift_agent.workspace import transaction as transaction_module


def _commit_nested_markdown_document(repo: Path) -> Path:
    docs = repo / "docs"
    nested = docs / "sub/api.md"
    nested.parent.mkdir()
    (docs / "api.md").replace(nested)
    subprocess.run(
        ["git", "add", "-A"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-qm", "configure nested documentation"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return nested


def test_check_returns_drift_found_without_editing_docs(drift_repo: Path) -> None:
    before = (drift_repo / "docs/api.md").read_bytes()

    bundle = run(RunRequest(mode=RunMode.CHECK, repo_path=drift_repo))

    assert bundle.status is RunStatus.DRIFT_FOUND
    assert len(bundle.findings) == 1
    assert bundle.changes.applied is False
    assert (drift_repo / "docs/api.md").read_bytes() == before
    assert bundle.usage.model_calls == 0


@pytest.mark.parametrize("mode", [RunMode.CHECK, RunMode.REPAIR])
@pytest.mark.parametrize("symlink_kind", ["final", "component"])
def test_markdown_symlink_inputs_fail_without_out_of_scope_mutation(
    drift_repo: Path,
    mode: RunMode,
    symlink_kind: str,
) -> None:
    outside = drift_repo.parent / f"outside-docs-{mode}-{symlink_kind}"
    outside.mkdir()
    target = outside / "api.md"
    target.write_text(
        "### `click_demo.api.echo`\n```python\ndef echo(message: str) -> None: ...\n```\n",
        encoding="utf-8",
    )
    before = target.read_bytes()
    docs = drift_repo / "docs"
    if symlink_kind == "final":
        (docs / "api.md").unlink()
        (docs / "api.md").symlink_to(target)
    else:
        shutil.rmtree(docs)
        docs.symlink_to(outside, target_is_directory=True)

    bundle = run(RunRequest(mode=mode, repo_path=drift_repo))

    assert bundle.status is RunStatus.FAILED
    assert bundle.changes.applied is False
    assert any("symlink" in result.summary for result in bundle.validation)
    assert target.read_bytes() == before


@pytest.mark.parametrize("component_kind", ["mismatched", "broken"])
def test_dirty_markdown_under_symlink_component_fails_explicitly(
    drift_repo: Path,
    component_kind: str,
) -> None:
    nested = _commit_nested_markdown_document(drift_repo)
    component = nested.parent
    shutil.rmtree(component)
    target = drift_repo / "notes/redirected-docs"
    if component_kind == "mismatched":
        target.mkdir(parents=True)
    component.symlink_to(target, target_is_directory=True)

    bundle = run(RunRequest(mode=RunMode.CHECK, repo_path=drift_repo))

    assert bundle.status is RunStatus.FAILED
    assert any("symlink" in result.summary for result in bundle.validation)
    assert bundle.changes.applied is False


def test_code_only_scan_rejects_markdown_directory_symlink(
    drift_repo: Path,
) -> None:
    target = drift_repo / "notes/linked-docs"
    target.mkdir(parents=True)
    target_doc = target / "api.md"
    target_doc.write_text(
        "### `click_demo.api.echo`\n```python\ndef echo(message: str) -> None: ...\n```\n",
        encoding="utf-8",
    )
    target_before = target_doc.read_bytes()
    (drift_repo / "docs/linked").symlink_to(target, target_is_directory=True)

    bundle = run(RunRequest(mode=RunMode.CHECK, repo_path=drift_repo))

    assert bundle.status is RunStatus.FAILED
    assert any("symlink" in result.summary for result in bundle.validation)
    assert target_doc.read_bytes() == target_before


@pytest.mark.parametrize("mode", [RunMode.CHECK, RunMode.REPAIR])
def test_included_python_symlink_fails_instead_of_returning_clean(
    drift_repo: Path,
    mode: RunMode,
) -> None:
    target = drift_repo / "notes/api.py"
    target.parent.mkdir()
    target.write_text(
        "def echo(message: str, color: bool = True) -> None: ...\n",
        encoding="utf-8",
    )
    target_before = target.read_bytes()
    source = drift_repo / "src/click_demo/api.py"
    source.unlink()
    source.symlink_to(target)
    docs = drift_repo / "docs/api.md"
    docs_before = docs.read_bytes()

    bundle = run(RunRequest(mode=mode, repo_path=drift_repo))

    assert bundle.status is RunStatus.FAILED
    assert any("symlink" in result.summary for result in bundle.validation)
    assert bundle.changes.applied is False
    assert target.read_bytes() == target_before
    assert docs.read_bytes() == docs_before


@pytest.mark.parametrize("mode", [RunMode.CHECK, RunMode.REPAIR])
@pytest.mark.parametrize(
    "content",
    [
        "# keep leading\ndef echo(message: str) -> None: ...\n",
        "def echo(message: str) -> None: ...\n# keep trailing\n",
        "def echo(message: str) -> None: ...  # keep inline\n",
    ],
)
def test_signature_stub_comments_are_unresolved_without_byte_changes(
    drift_repo: Path,
    mode: RunMode,
    content: str,
) -> None:
    docs = drift_repo / "docs/api.md"
    docs.write_text(
        f"### `click_demo.api.echo`\n```python\n{content}```\n",
        encoding="utf-8",
    )
    before = docs.read_bytes()

    bundle = run(RunRequest(mode=mode, repo_path=drift_repo))

    expected_status = RunStatus.DRIFT_FOUND if mode is RunMode.CHECK else RunStatus.UNRESOLVED
    assert bundle.status is expected_status
    assert bundle.findings[0].disposition is FindingDisposition.UNRESOLVED
    assert "comments" in bundle.findings[0].reason
    assert bundle.changes.applied is False
    assert docs.read_bytes() == before


def test_repair_preserves_crlf_markdown_bytes(drift_repo: Path) -> None:
    docs = drift_repo / "docs/api.md"
    docs.write_bytes(
        b"### `click_demo.api.echo`\r\n```python\r\ndef echo(message: str) -> None: ...\r\n```\r\n"
    )

    bundle = run(RunRequest(mode=RunMode.REPAIR, repo_path=drift_repo))

    raw = docs.read_bytes()
    assert bundle.status is RunStatus.FIXED
    assert b"color: bool = True" in raw
    assert b"\n" not in raw.replace(b"\r\n", b"")


@pytest.mark.parametrize("indent", [b" ", b"  ", b"   "])
def test_repair_preserves_valid_indented_commonmark_fence_bytes(
    drift_repo: Path,
    indent: bytes,
) -> None:
    docs = drift_repo / "docs/api.md"
    docs.write_bytes(
        b"### `click_demo.api.echo`\r\n"
        + indent
        + b"```python\r\n"
        + indent
        + b"def echo(message: str) -> None: ...\r\n"
        + indent
        + b"```\r\n"
    )

    bundle = run(RunRequest(mode=RunMode.REPAIR, repo_path=drift_repo))

    assert bundle.status is RunStatus.FIXED
    assert docs.read_bytes() == (
        b"### `click_demo.api.echo`\r\n"
        + indent
        + b"```python\r\n"
        + indent
        + b"def echo(message: str, color: bool = True) -> None: ...\r\n"
        + indent
        + b"```\r\n"
    )


def test_docs_only_change_is_compared_to_static_code_facts(drift_repo: Path) -> None:
    source = drift_repo / "src/click_demo/api.py"
    source.write_text("def echo(message: str) -> None: ...\n", encoding="utf-8")
    docs = drift_repo / "docs/api.md"
    docs.write_text(
        "### `click_demo.api.echo`\n```python\n"
        "def echo(message: str, obsolete: bool = False) -> None: ...\n```\n",
        encoding="utf-8",
    )
    before = docs.read_bytes()

    bundle = run(RunRequest(mode=RunMode.CHECK, repo_path=drift_repo))

    assert bundle.status is RunStatus.DRIFT_FOUND
    assert len(bundle.findings) == 1
    assert docs.read_bytes() == before


def test_docs_only_scan_honors_excluded_python_paths(drift_repo: Path) -> None:
    config_path = drift_repo / "drift-agent.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "exclude = []",
            'exclude = ["src/generated/**"]',
        ),
        encoding="utf-8",
    )
    excluded = drift_repo / "src/generated/plugin.py"
    excluded.parent.mkdir()
    excluded.write_text("def ignored() -> None: ...\n", encoding="utf-8")
    subprocess.run(
        [
            "git",
            "add",
            "drift-agent.toml",
            "src/click_demo/api.py",
            "src/generated/plugin.py",
        ],
        cwd=drift_repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-qm", "update code with excluded source"],
        cwd=drift_repo,
        check=True,
        capture_output=True,
    )
    docs = drift_repo / "docs/api.md"
    docs.write_text(
        "# Changed docs\n\n" + docs.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    before = docs.read_bytes()

    bundle = run(RunRequest(mode=RunMode.CHECK, repo_path=drift_repo))

    assert bundle.status is RunStatus.DRIFT_FOUND
    assert len(bundle.findings) == 1
    assert docs.read_bytes() == before


def test_docs_only_scan_skips_source_root_outside_include(drift_repo: Path) -> None:
    config_path = drift_repo / "drift-agent.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            'source_roots = ["src"]',
            'source_roots = ["src", "vendor"]',
        ),
        encoding="utf-8",
    )
    outside_include = drift_repo / "vendor/standalone.py"
    outside_include.parent.mkdir()
    outside_include.write_text("def ignored() -> None: ...\n", encoding="utf-8")
    subprocess.run(
        [
            "git",
            "add",
            "drift-agent.toml",
            "src/click_demo/api.py",
            "vendor/standalone.py",
        ],
        cwd=drift_repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-qm", "add source root outside include"],
        cwd=drift_repo,
        check=True,
        capture_output=True,
    )
    docs = drift_repo / "docs/api.md"
    docs.write_text(
        "# Changed docs\n\n" + docs.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    bundle = run(RunRequest(mode=RunMode.CHECK, repo_path=drift_repo))

    assert bundle.status is RunStatus.DRIFT_FOUND
    assert len(bundle.findings) == 1


def test_mixed_scope_ignores_preexisting_unrelated_drift(drift_repo: Path) -> None:
    other_source = drift_repo / "src/click_demo/other.py"
    other_source.write_text(
        "def other(value: int, flag: bool = True) -> None: ...\n",
        encoding="utf-8",
    )
    other_doc = drift_repo / "docs/other.md"
    other_doc.write_text(
        "### `click_demo.other.other`\n```python\ndef other(value: int) -> None: ...\n```\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "add", "src/click_demo/other.py", "docs/other.md"],
        cwd=drift_repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-qm", "add unrelated historical drift"],
        cwd=drift_repo,
        check=True,
        capture_output=True,
    )
    (drift_repo / "docs/note.md").write_text("# Changed note\n", encoding="utf-8")
    other_before = other_doc.read_bytes()

    bundle = run(RunRequest(mode=RunMode.REPAIR, repo_path=drift_repo))

    assert bundle.status is RunStatus.FIXED
    assert other_doc.read_bytes() == other_before


def test_deleted_python_source_reports_structural_drift_without_editing_docs(
    drift_repo: Path,
) -> None:
    (drift_repo / "src/click_demo/api.py").unlink()
    docs = drift_repo / "docs/api.md"
    before = docs.read_bytes()

    bundle = run(RunRequest(mode=RunMode.CHECK, repo_path=drift_repo))

    assert bundle.status is RunStatus.DRIFT_FOUND
    assert len(bundle.findings) == 1
    assert bundle.findings[0].kind == "symbol_reference_deleted"
    assert bundle.findings[0].disposition is FindingDisposition.DETECTED
    assert bundle.snapshot.input_file_hashes["src/click_demo/api.py"] == "missing"
    assert docs.read_bytes() == before


@pytest.mark.parametrize("mode", [RunMode.CHECK, RunMode.REPAIR])
def test_nested_namespace_python_candidate_fails_explicitly(
    drift_repo: Path,
    mode: RunMode,
) -> None:
    package = drift_repo / "src/click_demo"
    original_source = package / "api.py"
    original_source.unlink()
    nested_source = package / "plugins/api.py"
    nested_source.parent.mkdir()
    nested_source.write_text(
        "def echo(message: str) -> None: ...\n",
        encoding="utf-8",
    )
    docs = drift_repo / "docs/api.md"
    docs.write_text(
        "### `click_demo.plugins.api.echo`\n```python\ndef echo(message: str) -> None: ...\n```\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "add", "-A"],
        cwd=drift_repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-qm", "configure nested namespace layout"],
        cwd=drift_repo,
        check=True,
        capture_output=True,
    )
    nested_source.write_text(
        "def echo(message: str, color: bool = True) -> None: ...\n",
        encoding="utf-8",
    )
    before = docs.read_bytes()

    bundle = run(RunRequest(mode=mode, repo_path=drift_repo))

    assert bundle.status is RunStatus.FAILED
    assert any("unsupported Python layout" in result.summary for result in bundle.validation)
    assert bundle.changes.applied is False
    assert docs.read_bytes() == before


@pytest.mark.parametrize("mode", [RunMode.CHECK, RunMode.REPAIR])
def test_malformed_changed_python_candidate_fails_instead_of_returning_clean(
    drift_repo: Path,
    mode: RunMode,
) -> None:
    source = drift_repo / "src/click_demo/api.py"
    source.write_text("def echo(\n", encoding="utf-8")
    docs = drift_repo / "docs/api.md"
    before = docs.read_bytes()

    bundle = run(RunRequest(mode=mode, repo_path=drift_repo))

    assert bundle.status is RunStatus.FAILED
    assert any("could not parse" in result.summary for result in bundle.validation)
    assert bundle.changes.applied is False
    assert docs.read_bytes() == before


def test_broad_include_ignores_deleted_python_outside_source_roots(
    drift_repo: Path,
) -> None:
    config_path = drift_repo / "drift-agent.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            'include = ["src/**/*.py", "docs/**/*.md"]',
            'include = ["**/*.py", "**/*.md"]',
        ),
        encoding="utf-8",
    )
    outside = drift_repo / "scripts/outside.py"
    outside.parent.mkdir()
    outside.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "drift-agent.toml", "scripts/outside.py"],
        cwd=drift_repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-qm", "configure broad include"],
        cwd=drift_repo,
        check=True,
        capture_output=True,
    )
    outside.unlink()

    bundle = run(RunRequest(mode=RunMode.REPAIR, repo_path=drift_repo))

    assert bundle.status is RunStatus.FIXED
    assert bundle.scope == ["src/click_demo/api.py"]
    assert "color: bool = True" in (drift_repo / "docs/api.md").read_text(encoding="utf-8")


def test_repair_rolls_back_when_required_validation_fails(drift_repo: Path) -> None:
    class FailingValidationDetector(StructuralDetector):
        def validate(
            self,
            alignments: list[Alignment],
            attempt: PatchAttempt,
        ) -> ValidationResult:
            result = super().validate(alignments, attempt)
            return result.model_copy(
                update={"status": ValidationStatus.FAILED, "summary": "forced failure"}
            )

    before = (drift_repo / "docs/api.md").read_bytes()
    runtime = AgentRuntime()
    runtime.detector = FailingValidationDetector()

    bundle = run(
        RunRequest(mode=RunMode.REPAIR, repo_path=drift_repo),
        runtime=runtime,
    )

    assert bundle.status is RunStatus.UNRESOLVED
    assert (drift_repo / "docs/api.md").read_bytes() == before


def test_persistent_rollback_failure_reports_residual_agent_mutation(
    drift_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingValidationDetector(StructuralDetector):
        def validate(
            self,
            alignments: list[Alignment],
            attempt: PatchAttempt,
        ) -> ValidationResult:
            result = super().validate(alignments, attempt)
            return result.model_copy(
                update={"status": ValidationStatus.FAILED, "summary": "forced failure"}
            )

    real_replace = transaction_module.os.replace
    replace_calls = 0

    def fail_rollback_replace(source: str, destination: Path) -> None:
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls >= 2:
            raise OSError("persistent rollback replace failure")
        real_replace(source, destination)

    monkeypatch.setattr(transaction_module.os, "replace", fail_rollback_replace)
    docs = drift_repo / "docs/api.md"
    before = docs.read_bytes()
    runtime = AgentRuntime()
    runtime.detector = FailingValidationDetector()

    bundle = run(
        RunRequest(mode=RunMode.REPAIR, repo_path=drift_repo),
        runtime=runtime,
    )

    assert bundle.status is RunStatus.FAILED
    assert bundle.changes.applied is False
    assert bundle.changes.files == []
    assert bundle.changes.patch == ""
    assert len(bundle.residual_changes) == 1
    assert bundle.residual_changes[0].path == "docs/api.md"
    assert bundle.residual_changes[0].reason_code == "rollback_unavailable"
    assert bundle.residual_changes[0].current_hash == sha256_file(docs)
    assert {finding.reason_code for finding in bundle.findings} == {"rollback_unavailable"}
    assert any(
        result.check == "rollback"
        and result.status is ValidationStatus.UNAVAILABLE
        and "persistent rollback replace failure" in result.summary
        for result in bundle.validation
    )
    assert docs.read_bytes() != before
    assert b"color: bool = True" in docs.read_bytes()


def test_validation_exception_with_failed_rollback_reports_residual_mutation(
    drift_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExplodingValidationDetector(StructuralDetector):
        def validate(
            self,
            alignments: list[Alignment],
            attempt: PatchAttempt,
        ) -> ValidationResult:
            raise RuntimeError("forced validation exception")

    real_replace = transaction_module.os.replace
    replace_calls = 0

    def fail_rollback_replace(source: str, destination: Path) -> None:
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls >= 2:
            raise OSError("persistent exception rollback failure")
        real_replace(source, destination)

    monkeypatch.setattr(transaction_module.os, "replace", fail_rollback_replace)
    docs = drift_repo / "docs/api.md"
    runtime = AgentRuntime()
    runtime.detector = ExplodingValidationDetector()

    bundle = run(
        RunRequest(mode=RunMode.REPAIR, repo_path=drift_repo),
        runtime=runtime,
    )

    assert bundle.status is RunStatus.FAILED
    assert bundle.changes.applied is False
    assert bundle.changes.files == []
    assert bundle.changes.patch == ""
    assert len(bundle.residual_changes) == 1
    assert bundle.residual_changes[0].path == "docs/api.md"
    assert bundle.residual_changes[0].reason_code == "rollback_unavailable"
    assert bundle.residual_changes[0].current_hash == sha256_file(docs)
    assert {finding.reason_code for finding in bundle.findings} == {"rollback_unavailable"}
    assert any(result.check == "rollback" for result in bundle.validation)
    assert b"color: bool = True" in docs.read_bytes()


def test_residual_hash_is_fail_closed_when_target_becomes_unsafe(
    tmp_path: Path,
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("external\n", encoding="utf-8")
    (docs / "api.md").symlink_to(outside)

    assert application_module._residual_current_hash(tmp_path, "docs/api.md") == ("unavailable")
    assert application_module._residual_current_hash(tmp_path, "docs/missing.md") == ("missing")


def test_keyboard_interrupt_rolls_back_before_propagating(
    drift_repo: Path,
) -> None:
    class InterruptingValidationDetector(StructuralDetector):
        def validate(
            self,
            alignments: list[Alignment],
            attempt: PatchAttempt,
        ) -> ValidationResult:
            raise KeyboardInterrupt

    docs = drift_repo / "docs/api.md"
    before = docs.read_bytes()
    runtime = AgentRuntime()
    runtime.detector = InterruptingValidationDetector()

    with pytest.raises(KeyboardInterrupt):
        run(
            RunRequest(mode=RunMode.REPAIR, repo_path=drift_repo),
            runtime=runtime,
        )

    assert docs.read_bytes() == before


def test_interrupt_after_atomic_replace_remains_rollbackable(
    drift_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_replace = transaction_module.os.replace
    replace_calls = 0

    def replace_then_interrupt(source: str, destination: Path) -> None:
        nonlocal replace_calls
        replace_calls += 1
        real_replace(source, destination)
        if replace_calls == 1:
            raise KeyboardInterrupt

    monkeypatch.setattr(transaction_module.os, "replace", replace_then_interrupt)
    docs = drift_repo / "docs/api.md"
    before = docs.read_bytes()

    with pytest.raises(KeyboardInterrupt):
        run(RunRequest(mode=RunMode.REPAIR, repo_path=drift_repo))

    assert replace_calls == 2
    assert docs.read_bytes() == before


def test_interrupt_before_applied_bookkeeping_rolls_back_landed_bytes(
    drift_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def interrupt_bookkeeping(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(
        transaction_module.WorkspaceTransaction,
        "_record_applied",
        interrupt_bookkeeping,
    )
    docs = drift_repo / "docs/api.md"
    before = docs.read_bytes()

    with pytest.raises(KeyboardInterrupt):
        run(RunRequest(mode=RunMode.REPAIR, repo_path=drift_repo))

    assert docs.read_bytes() == before


@pytest.mark.parametrize("interrupt_stage", ["before_clear", "after_clear"])
def test_interrupt_during_transaction_clear_returns_published_bundle(
    drift_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupt_stage: str,
) -> None:
    real_clear = transaction_module.WorkspaceTransaction._clear

    def interrupt_clear(
        transaction: transaction_module.WorkspaceTransaction,
    ) -> None:
        if interrupt_stage == "after_clear":
            real_clear(transaction)
        raise KeyboardInterrupt

    monkeypatch.setattr(
        transaction_module.WorkspaceTransaction,
        "_clear",
        interrupt_clear,
    )

    bundle = run(RunRequest(mode=RunMode.REPAIR, repo_path=drift_repo))

    assert bundle.status is RunStatus.FIXED
    assert bundle.changes.applied is True
    assert b"color: bool = True" in (drift_repo / "docs/api.md").read_bytes()


def test_commit_preflight_failure_never_publishes_fixed_bundle(
    drift_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_preflight(
        transaction: transaction_module.WorkspaceTransaction,
    ) -> None:
        raise RuntimeError("forced commit preflight failure")

    monkeypatch.setattr(
        transaction_module.WorkspaceTransaction,
        "preflight_commit",
        fail_preflight,
    )
    docs = drift_repo / "docs/api.md"
    before = docs.read_bytes()
    runtime = AgentRuntime()

    bundle = run(
        RunRequest(mode=RunMode.REPAIR, repo_path=drift_repo),
        runtime=runtime,
    )

    assert bundle.status is RunStatus.FAILED
    assert runtime.committed_bundle is None
    assert bundle.changes.applied is False
    assert docs.read_bytes() == before


def test_repair_rolls_back_when_claim_cannot_be_realigned(drift_repo: Path) -> None:
    class RemovingClaimPatcher(SignaturePatcher):
        def propose(
            self,
            repo_path: Path,
            alignment: Alignment,
            finding: DriftFinding,
        ) -> PatchAttempt:
            attempt = super().propose(repo_path, alignment, finding)
            return attempt.model_copy(update={"replacement_text": "VALUE = 1\n"})

    docs = drift_repo / "docs/api.md"
    before = docs.read_bytes()
    runtime = AgentRuntime()
    runtime.patcher = RemovingClaimPatcher()

    bundle = run(
        RunRequest(mode=RunMode.REPAIR, repo_path=drift_repo),
        runtime=runtime,
    )

    assert bundle.status is RunStatus.UNRESOLVED
    assert docs.read_bytes() == before


def test_explicit_contract_truth_requires_approval_without_edit(drift_repo: Path) -> None:
    docs = drift_repo / "docs/api.md"
    docs.write_text(
        "---\ndrift_truth: contract\n---\n" + docs.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    before = docs.read_bytes()

    bundle = run(RunRequest(mode=RunMode.REPAIR, repo_path=drift_repo))

    assert bundle.status is RunStatus.NEEDS_APPROVAL
    assert bundle.changes.applied is False
    assert len(bundle.approval_required) == 1
    approval = bundle.approval_required[0]
    assert approval.id.startswith("approval_")
    assert approval.kind == "truth_direction"
    assert {
        "src/click_demo/api.py",
        "docs/api.md",
        "drift-agent.toml",
    } <= set(approval.input_file_hashes)
    assert docs.read_bytes() == before


def test_mixed_code_and_contract_findings_return_partial(drift_repo: Path) -> None:
    source = drift_repo / "src/click_demo/api.py"
    source.write_text(
        source.read_text(encoding="utf-8")
        + "\ndef contract(value: int, enabled: bool = True) -> None: ...\n",
        encoding="utf-8",
    )
    contract_doc = drift_repo / "docs/contract.md"
    contract_doc.write_text(
        """\
---
drift_truth: contract
---
### `click_demo.api.contract`
```python
def contract(value: int) -> None: ...
```
""",
        encoding="utf-8",
    )
    contract_before = contract_doc.read_bytes()

    bundle = run(RunRequest(mode=RunMode.REPAIR, repo_path=drift_repo))

    assert bundle.status is RunStatus.PARTIAL
    assert bundle.changes.applied is True
    assert "color: bool = True" in (drift_repo / "docs/api.md").read_text(encoding="utf-8")
    assert contract_doc.read_bytes() == contract_before
    assert len(bundle.approval_required) == 1
    for path, expected_hash in bundle.approval_required[0].input_file_hashes.items():
        assert sha256_file(drift_repo / path) == expected_hash


def test_stage_two_repairs_two_independent_code_derived_findings(
    drift_repo: Path,
) -> None:
    source = drift_repo / "src/click_demo/api.py"
    source.write_text(
        source.read_text(encoding="utf-8")
        + "\ndef wave(name: str, loud: bool = True) -> None: ...\n",
        encoding="utf-8",
    )
    wave_doc = drift_repo / "docs/wave.md"
    wave_doc.write_text(
        "### `click_demo.api.wave`\n```python\ndef wave(name: str) -> None: ...\n```\n",
        encoding="utf-8",
    )
    bundle = run(RunRequest(mode=RunMode.REPAIR, repo_path=drift_repo))

    findings = {finding.symbol_id: finding for finding in bundle.findings}
    assert bundle.status is RunStatus.FIXED
    assert findings["click_demo.api.echo"].disposition is FindingDisposition.FIXED
    assert findings["click_demo.api.wave"].disposition is FindingDisposition.FIXED
    assert all("stage1_limit" not in finding.reason for finding in bundle.findings)
    assert bundle.changes.applied is True
    assert bundle.changes.files == ["docs/api.md", "docs/wave.md"]
    assert bundle.changes.patch.count("--- docs/api.md\n") == 1
    assert bundle.changes.patch.count("+++ docs/api.md\n") == 1
    assert bundle.changes.patch.count("--- docs/wave.md\n") == 1
    assert bundle.changes.patch.count("+++ docs/wave.md\n") == 1
    subprocess.run(
        ["git", "apply", "-p0", "--reverse", "--check", "-"],
        cwd=drift_repo,
        input=bundle.changes.patch.encode("utf-8"),
        check=True,
        capture_output=True,
    )
    assert b"loud: bool = True" in wave_doc.read_bytes()


def test_conflicting_path_truth_rules_are_unresolved(drift_repo: Path) -> None:
    config_path = drift_repo / "drift-agent.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "design = []",
            'design = ["docs/**"]',
        ),
        encoding="utf-8",
    )
    docs = drift_repo / "docs/api.md"
    before = docs.read_bytes()

    bundle = run(RunRequest(mode=RunMode.REPAIR, repo_path=drift_repo))

    assert bundle.status is RunStatus.UNRESOLVED
    assert bundle.approval_required == []
    assert docs.read_bytes() == before


def test_duplicate_exact_fqn_claims_are_unresolved_not_clean(drift_repo: Path) -> None:
    docs = drift_repo / "docs/api.md"
    duplicate = drift_repo / "docs/duplicate.md"
    duplicate.write_bytes(docs.read_bytes())
    before = docs.read_bytes()

    bundle = run(RunRequest(mode=RunMode.REPAIR, repo_path=drift_repo))

    assert bundle.status is RunStatus.UNRESOLVED
    assert len(bundle.findings) == 1
    assert "ambiguous" in bundle.findings[0].reason
    assert docs.read_bytes() == before
    assert duplicate.read_bytes() == before


def test_docs_only_duplicate_claim_closes_over_unchanged_claim(
    drift_repo: Path,
) -> None:
    subprocess.run(
        ["git", "add", "src/click_demo/api.py"],
        cwd=drift_repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-qm", "update source signature"],
        cwd=drift_repo,
        check=True,
        capture_output=True,
    )
    docs = drift_repo / "docs/api.md"
    duplicate = drift_repo / "docs/duplicate.md"
    duplicate.write_bytes(docs.read_bytes())
    before = docs.read_bytes()

    bundle = run(RunRequest(mode=RunMode.REPAIR, repo_path=drift_repo))

    assert bundle.status is RunStatus.UNRESOLVED
    assert len(bundle.findings) == 1
    assert "ambiguous" in bundle.findings[0].reason
    assert docs.read_bytes() == before
    assert duplicate.read_bytes() == before


def test_python_only_duplicate_fact_closes_over_unchanged_fact(
    drift_repo: Path,
) -> None:
    config_path = drift_repo / "drift-agent.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        .replace(
            'source_roots = ["src"]',
            'source_roots = ["src", "vendor"]',
        )
        .replace(
            'include = ["src/**/*.py", "docs/**/*.md"]',
            'include = ["src/**/*.py", "vendor/**/*.py", "docs/**/*.md"]',
        ),
        encoding="utf-8",
    )
    vendor_package = drift_repo / "vendor/click_demo"
    vendor_package.mkdir(parents=True)
    (vendor_package / "__init__.py").write_text("", encoding="utf-8")
    (vendor_package / "api.py").write_text(
        "def echo(message: str, color: bool = True) -> None: ...\n",
        encoding="utf-8",
    )
    subprocess.run(
        [
            "git",
            "add",
            "drift-agent.toml",
            "src/click_demo/api.py",
            "vendor/click_demo/__init__.py",
            "vendor/click_demo/api.py",
        ],
        cwd=drift_repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-qm", "add duplicate configured package"],
        cwd=drift_repo,
        check=True,
        capture_output=True,
    )
    source = drift_repo / "src/click_demo/api.py"
    source.write_text(
        "def echo(message: str, color: bool = True, loud: bool = False) -> None: ...\n",
        encoding="utf-8",
    )
    docs = drift_repo / "docs/api.md"
    before = docs.read_bytes()

    bundle = run(RunRequest(mode=RunMode.REPAIR, repo_path=drift_repo))

    assert bundle.status is RunStatus.UNRESOLVED
    assert len(bundle.findings) == 1
    assert "ambiguous" in bundle.findings[0].reason
    assert docs.read_bytes() == before


def test_overlapping_docs_roots_enumerate_one_physical_claim(
    drift_repo: Path,
) -> None:
    config_path = drift_repo / "drift-agent.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            'docs_roots = ["docs"]',
            'docs_roots = ["docs", "docs/reference"]',
        ),
        encoding="utf-8",
    )
    docs = drift_repo / "docs/api.md"
    nested = drift_repo / "docs/reference/api.md"
    nested.parent.mkdir()
    docs.rename(nested)

    bundle = run(RunRequest(mode=RunMode.REPAIR, repo_path=drift_repo))

    assert bundle.status is RunStatus.FIXED
    assert bundle.changes.files == ["docs/reference/api.md"]
    assert "color: bool = True" in nested.read_text(encoding="utf-8")


def test_duplicate_source_roots_enumerate_one_physical_fact(
    drift_repo: Path,
) -> None:
    config_path = drift_repo / "drift-agent.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            'source_roots = ["src"]',
            'source_roots = ["src", "src"]',
        ),
        encoding="utf-8",
    )

    bundle = run(RunRequest(mode=RunMode.REPAIR, repo_path=drift_repo))

    assert bundle.status is RunStatus.FIXED
    assert "color: bool = True" in (drift_repo / "docs/api.md").read_text(encoding="utf-8")


def test_invalid_exact_fqn_claim_is_unresolved_not_failed(drift_repo: Path) -> None:
    docs = drift_repo / "docs/api.md"
    docs.write_text(
        """\
### `click_demo.api.echo`
```python
def echo(
```
""",
        encoding="utf-8",
    )
    before = docs.read_bytes()

    bundle = run(RunRequest(mode=RunMode.REPAIR, repo_path=drift_repo))

    assert bundle.status is RunStatus.UNRESOLVED
    assert "extraction failed" in bundle.findings[0].reason
    assert docs.read_bytes() == before


def test_unrelated_invalid_python_fence_does_not_block_valid_repair(
    drift_repo: Path,
) -> None:
    unrelated = drift_repo / "docs/unrelated.md"
    unrelated.write_text("# Examples\n```python\ndef broken(\n```\n", encoding="utf-8")
    before = unrelated.read_bytes()

    bundle = run(RunRequest(mode=RunMode.REPAIR, repo_path=drift_repo))

    assert bundle.status is RunStatus.FIXED
    assert unrelated.read_bytes() == before


def test_source_change_during_validation_returns_stale_and_rolls_back_docs(
    drift_repo: Path,
) -> None:
    source = drift_repo / "src/click_demo/api.py"

    class SourceMutatingDetector(StructuralDetector):
        def validate(
            self,
            alignments: list[Alignment],
            attempt: PatchAttempt,
        ) -> ValidationResult:
            result = super().validate(alignments, attempt)
            source.write_text("def externally_changed() -> None: ...\n", encoding="utf-8")
            return result

    docs = drift_repo / "docs/api.md"
    before = docs.read_bytes()
    runtime = AgentRuntime()
    runtime.detector = SourceMutatingDetector()

    bundle = run(
        RunRequest(mode=RunMode.REPAIR, repo_path=drift_repo),
        runtime=runtime,
    )

    assert bundle.status is RunStatus.STALE
    assert docs.read_bytes() == before
    assert "externally_changed" in source.read_text(encoding="utf-8")


def test_source_replaced_by_symlink_during_validation_is_not_read(
    drift_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = drift_repo / "src/click_demo/api.py"
    target = drift_repo / "notes/secret.py"
    target.parent.mkdir()
    target.write_text("SECRET = 'must not be read'\n", encoding="utf-8")
    target_before = target.read_bytes()

    class SourceSymlinkingDetector(StructuralDetector):
        def validate(
            self,
            alignments: list[Alignment],
            attempt: PatchAttempt,
        ) -> ValidationResult:
            result = super().validate(alignments, attempt)
            source.unlink()
            source.symlink_to(target)
            return result

    real_sha256_file = application_module.sha256_file

    def reject_symlink_read(path: Path) -> str:
        if path == source and path.is_symlink():
            raise AssertionError("Python symlink content was read")
        return real_sha256_file(path)

    monkeypatch.setattr(application_module, "sha256_file", reject_symlink_read)
    docs = drift_repo / "docs/api.md"
    before = docs.read_bytes()
    runtime = AgentRuntime()
    runtime.detector = SourceSymlinkingDetector()

    bundle = run(
        RunRequest(mode=RunMode.REPAIR, repo_path=drift_repo),
        runtime=runtime,
    )

    assert bundle.status is RunStatus.STALE
    assert docs.read_bytes() == before
    assert target.read_bytes() == target_before


def test_source_change_then_validation_error_still_returns_stale(
    drift_repo: Path,
) -> None:
    source = drift_repo / "src/click_demo/api.py"

    class SourceMutatingExplodingDetector(StructuralDetector):
        def validate(
            self,
            alignments: list[Alignment],
            attempt: PatchAttempt,
        ) -> ValidationResult:
            source.write_text("def externally_changed() -> None: ...\n", encoding="utf-8")
            raise RuntimeError("forced runtime failure after source change")

    docs = drift_repo / "docs/api.md"
    before = docs.read_bytes()
    runtime = AgentRuntime()
    runtime.detector = SourceMutatingExplodingDetector()

    bundle = run(
        RunRequest(mode=RunMode.REPAIR, repo_path=drift_repo),
        runtime=runtime,
    )

    assert bundle.status is RunStatus.STALE
    assert docs.read_bytes() == before
    assert "externally_changed" in source.read_text(encoding="utf-8")


def test_truth_config_change_during_validation_returns_stale(
    drift_repo: Path,
) -> None:
    config_path = drift_repo / "drift-agent.toml"

    class ConfigMutatingDetector(StructuralDetector):
        def validate(
            self,
            alignments: list[Alignment],
            attempt: PatchAttempt,
        ) -> ValidationResult:
            result = super().validate(alignments, attempt)
            config_path.write_text(
                config_path.read_text(encoding="utf-8").replace(
                    'code_derived = ["docs/**"]',
                    "code_derived = []",
                ),
                encoding="utf-8",
            )
            return result

    docs = drift_repo / "docs/api.md"
    before = docs.read_bytes()
    runtime = AgentRuntime()
    runtime.detector = ConfigMutatingDetector()

    bundle = run(
        RunRequest(mode=RunMode.REPAIR, repo_path=drift_repo),
        runtime=runtime,
    )

    assert bundle.status is RunStatus.STALE
    assert docs.read_bytes() == before
    assert "code_derived = []" in config_path.read_text(encoding="utf-8")


def test_unexpected_validation_error_returns_failed_and_rolls_back_docs(
    drift_repo: Path,
) -> None:
    class ExplodingValidationDetector(StructuralDetector):
        def validate(
            self,
            alignments: list[Alignment],
            attempt: PatchAttempt,
        ) -> ValidationResult:
            raise RuntimeError("forced runtime failure")

    docs = drift_repo / "docs/api.md"
    before = docs.read_bytes()
    runtime = AgentRuntime()
    runtime.detector = ExplodingValidationDetector()

    bundle = run(
        RunRequest(mode=RunMode.REPAIR, repo_path=drift_repo),
        runtime=runtime,
    )

    assert bundle.status is RunStatus.FAILED
    assert len(bundle.findings) == 1
    assert bundle.validation[0].status is ValidationStatus.UNAVAILABLE
    assert docs.read_bytes() == before


def test_post_commit_graph_error_returns_the_verified_bundle(drift_repo: Path) -> None:
    class ExplodingFinalizeRuntime(AgentRuntime):
        def finalize_node(self, state: AgentState) -> AgentState:
            raise RuntimeError("forced post-commit failure")

    bundle = run(
        RunRequest(mode=RunMode.REPAIR, repo_path=drift_repo),
        runtime=ExplodingFinalizeRuntime(),
    )

    assert bundle.status is RunStatus.FIXED
    assert bundle.changes.applied is True
    assert "color: bool = True" in (drift_repo / "docs/api.md").read_text(encoding="utf-8")


def test_post_commit_keyboard_interrupt_returns_the_verified_bundle(
    drift_repo: Path,
) -> None:
    class InterruptingFinalizeRuntime(AgentRuntime):
        def finalize_node(self, state: AgentState) -> AgentState:
            assert self.committed_bundle is not None
            raise KeyboardInterrupt

    runtime = InterruptingFinalizeRuntime()

    bundle = run(
        RunRequest(mode=RunMode.REPAIR, repo_path=drift_repo),
        runtime=runtime,
    )

    assert bundle.status is RunStatus.FIXED
    assert bundle.changes.applied is True
    assert "color: bool = True" in (drift_repo / "docs/api.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("interrupt_type", [KeyboardInterrupt, SystemExit])
def test_base_interrupt_without_published_bundle_still_propagates(
    drift_repo: Path,
    interrupt_type: type[BaseException],
) -> None:
    class InterruptingScopeRuntime(AgentRuntime):
        def scope_node(self, state: AgentState) -> AgentState:
            raise interrupt_type

    runtime = InterruptingScopeRuntime()

    with pytest.raises(interrupt_type):
        run(
            RunRequest(mode=RunMode.REPAIR, repo_path=drift_repo),
            runtime=runtime,
        )

    assert runtime.committed_bundle is None

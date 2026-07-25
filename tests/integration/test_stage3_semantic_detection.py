from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Literal

import pytest

from drift_agent.application import AgentRuntime, run
from drift_agent.domain.enums import (
    FindingDisposition,
    RunMode,
    RunStatus,
    ValidationStatus,
)
from drift_agent.domain.models import DocClaim, RunRequest, VerifiedRepairBundle
from drift_agent.domain.serialization import (
    UnsupportedWireVersionError,
    bundle_to_wire,
)
from drift_agent.memory import (
    DecisionAddRequest,
    DecisionService,
    SQLiteStateStore,
)
from drift_agent.providers.semantic_claims import SemanticReturnClaimProvider
from drift_agent.workspace.identity import resolve_state_path

TruthKind = Literal["code_derived", "design", "contract", "unknown"]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        shell=False,
    )


def _constant_source(value: int) -> str:
    return f"def answer() -> int:\n    return {value}\n"


def _document(assertion: str, *, suffix: str = "") -> str:
    return (
        "### `demo.api.answer`\n\n"
        "```python\n"
        "def answer() -> int: ...\n"
        "```\n\n"
        f"{assertion}\n"
        f"{suffix}"
    )


def _config(truth: TruthKind) -> str:
    rules = {
        "code_derived": ("docs/**", "", ""),
        "design": ("", "docs/**", ""),
        "contract": ("", "", "docs/**"),
        "unknown": ("", "", ""),
    }
    code_derived, design, contract = rules[truth]

    def values(pattern: str) -> str:
        return f'["{pattern}"]' if pattern else "[]"

    return f"""\
[project]
source_roots = ["src"]
docs_roots = ["docs"]
include = ["src/**/*.py", "docs/**/*.md"]
exclude = []

[truth]
code_derived = {values(code_derived)}
design = {values(design)}
contract = {values(contract)}

[validation]
commands = []
network = false
"""


def _semantic_repo(
    tmp_path: Path,
    *,
    truth: TruthKind = "code_derived",
    base_source: str | None = None,
    current_source: str | None = None,
    base_document: str | None = None,
    current_document: str | None = None,
    duplicate_claim: bool = False,
) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    package = repo / "src/demo"
    docs = repo / "docs"
    package.mkdir(parents=True)
    docs.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    source = package / "api.py"
    source.write_text(base_source or _constant_source(1), encoding="utf-8")
    document = base_document or _document("Returns `1`.")
    (docs / "api.md").write_text(document, encoding="utf-8")
    if duplicate_claim:
        (docs / "duplicate.md").write_text(document, encoding="utf-8")
    (repo / "drift-agent.toml").write_text(_config(truth), encoding="utf-8")

    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "semantic-test@example.invalid")
    _git(repo, "config", "user.name", "semantic-test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "semantic baseline")

    if current_source is not None:
        source.write_text(current_source, encoding="utf-8")
    if current_document is not None:
        (docs / "api.md").write_text(current_document, encoding="utf-8")
    return repo, tmp_path / "state"


def _worktree_bytes(repo: Path) -> dict[str, bytes]:
    return {
        path.relative_to(repo).as_posix(): path.read_bytes()
        for path in sorted(repo.rglob("*"))
        if path.is_file() and ".git" not in path.relative_to(repo).parts
    }


def _semantic_check(repo: Path, state_dir: Path) -> VerifiedRepairBundle:
    return run(
        RunRequest(
            mode=RunMode.CHECK,
            repo_path=repo,
            state_dir=state_dir,
            semantic_analysis=True,
        )
    )


def _assert_zero_model_read_only(
    repo: Path,
    before: dict[str, bytes],
    bundle: VerifiedRepairBundle,
) -> None:
    assert _worktree_bytes(repo) == before
    assert bundle.changes.applied is False
    assert bundle.changes.files == []
    assert bundle.changes.patch == ""
    assert bundle.usage.model_calls == 0
    assert bundle.usage.model_calls_by_profile == {}
    assert bundle.usage.input_tokens == 0
    assert bundle.usage.output_tokens == 0
    assert bundle.usage.estimated_cost_usd == 0
    assert bundle.usage.validation_commands == 0


def test_opt_in_code_change_reports_direct_mismatch_in_v3_only(tmp_path: Path) -> None:
    repo, state_dir = _semantic_repo(
        tmp_path,
        current_source=_constant_source(2),
    )
    before = _worktree_bytes(repo)

    bundle = _semantic_check(repo, state_dir)

    assert bundle.status is RunStatus.DRIFT_FOUND
    assert bundle.scope == ["src/demo/api.py"]
    # Only the section-semantic receipt, which records that the
    # model-backed pass did not run in this key-free environment.
    assert [item.check for item in bundle.validation] == ["section_semantic"]
    assert len(bundle.findings) == 1
    finding = bundle.findings[0]
    assert finding.type == "semantic_drift"
    assert finding.kind == "semantic_direct_mismatch"
    assert finding.reason_code == "semantic.direct_mismatch"
    assert finding.disposition is FindingDisposition.DETECTED
    assert finding.truth_source == "code"
    assert finding.symbol_id == "demo.api.answer"
    assert finding.component_id == "return.literal"
    assert finding.detector_id == "semantic.constant_return"
    assert finding.detector_version == "1"
    assert finding.old_value == {
        "predicate": "return_literal",
        "mode": "direct",
        "value": {"type": "int", "value": 1},
    }
    assert finding.new_value == {
        "predicate": "return_literal",
        "value": {"type": "int", "value": 2},
    }
    assert finding.code_evidence.path == "src/demo/api.py"
    assert finding.doc_evidence.path == "docs/api.md"

    v3 = bundle_to_wire(bundle, 3)
    assert v3["schema_version"] == 3
    assert v3["findings"][0]["type"] == "semantic_drift"
    assert v3["findings"][0]["kind"] == "semantic_direct_mismatch"
    with pytest.raises(UnsupportedWireVersionError, match="cannot represent semantic analysis"):
        bundle_to_wire(bundle, 1)
    with pytest.raises(UnsupportedWireVersionError, match="cannot represent semantic analysis"):
        bundle_to_wire(bundle, 2)
    _assert_zero_model_read_only(repo, before, bundle)


def test_always_assertion_reports_over_promise(tmp_path: Path) -> None:
    repo, state_dir = _semantic_repo(
        tmp_path,
        base_document=_document("Always returns `1`."),
        current_source=_constant_source(2),
    )
    before = _worktree_bytes(repo)

    bundle = _semantic_check(repo, state_dir)

    assert bundle.status is RunStatus.DRIFT_FOUND
    assert len(bundle.findings) == 1
    finding = bundle.findings[0]
    assert finding.type == "semantic_drift"
    assert finding.kind == "semantic_over_promise"
    assert finding.reason_code == "semantic.over_promise"
    assert finding.old_value["mode"] == "always"
    _assert_zero_model_read_only(repo, before, bundle)


def test_equal_semantic_assertion_is_clean(tmp_path: Path) -> None:
    repo, state_dir = _semantic_repo(
        tmp_path,
        current_document=_document(
            "Returns `1`.",
            suffix="\n<!-- unrelated documentation edit -->\n",
        ),
    )
    before = _worktree_bytes(repo)

    bundle = _semantic_check(repo, state_dir)

    assert bundle.status is RunStatus.CLEAN
    assert bundle.scope == ["docs/api.md"]
    assert bundle.findings == []
    # Only the section-semantic receipt, which records that the
    # model-backed pass did not run in this key-free environment.
    assert [item.check for item in bundle.validation] == ["section_semantic"]
    assert bundle_to_wire(bundle, 3)["schema_version"] == 3
    with pytest.raises(UnsupportedWireVersionError, match="cannot represent semantic analysis"):
        bundle_to_wire(bundle, 1)
    with pytest.raises(UnsupportedWireVersionError, match="cannot represent semantic analysis"):
        bundle_to_wire(bundle, 2)
    _assert_zero_model_read_only(repo, before, bundle)


def test_semantic_analysis_is_disabled_by_default_and_keeps_v1_v2_compatible(
    tmp_path: Path,
) -> None:
    repo, state_dir = _semantic_repo(
        tmp_path,
        current_source=_constant_source(2),
    )
    before = _worktree_bytes(repo)

    bundle = run(
        RunRequest(
            mode=RunMode.CHECK,
            repo_path=repo,
            state_dir=state_dir,
        )
    )

    assert bundle.status is RunStatus.CLEAN
    assert bundle.findings == []
    v1 = bundle_to_wire(bundle, 1)
    v2 = bundle_to_wire(bundle, 2)
    assert "schema_version" not in v1
    assert v2["schema_version"] == 2
    assert v1["status"] == v2["status"] == "clean"
    assert v1["findings"] == v2["findings"] == []
    _assert_zero_model_read_only(repo, before, bundle)


def test_semantic_analysis_is_check_only(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="supported only in check mode"):
        RunRequest(
            mode=RunMode.REPAIR,
            repo_path=tmp_path,
            semantic_analysis=True,
        )


def test_early_semantic_failure_still_rejects_legacy_wire(tmp_path: Path) -> None:
    bundle = _semantic_check(tmp_path, tmp_path / "state")

    assert bundle.status is RunStatus.FAILED
    assert bundle_to_wire(bundle, 3)["schema_version"] == 3
    for version in (1, 2):
        with pytest.raises(
            UnsupportedWireVersionError,
            match=rf"schema V{version} cannot represent semantic analysis",
        ):
            bundle_to_wire(bundle, version)


def test_document_rewrite_between_providers_is_stale(tmp_path: Path) -> None:
    repo, state_dir = _semantic_repo(
        tmp_path,
        current_source=_constant_source(2),
    )

    class RewritingProvider(SemanticReturnClaimProvider):
        def collect(
            self,
            repo_path: Path,
            doc_paths: list[str],
            declarations: list[DocClaim],
        ) -> list[DocClaim]:
            (repo_path / "docs/api.md").write_text(
                _document("Returns `2`."),
                encoding="utf-8",
            )
            return super().collect(repo_path, doc_paths, declarations)

    runtime = AgentRuntime()
    runtime.semantic_claim_provider = RewritingProvider()

    bundle = run(
        RunRequest(
            mode=RunMode.CHECK,
            repo_path=repo,
            state_dir=state_dir,
            semantic_analysis=True,
        ),
        runtime=runtime,
    )

    assert bundle.status is RunStatus.STALE


def test_zero_claim_document_rewrite_between_providers_is_stale(
    tmp_path: Path,
) -> None:
    repo, state_dir = _semantic_repo(
        tmp_path,
        current_document="# dirty document without a declaration\n",
    )

    class AddingClaimProvider(SemanticReturnClaimProvider):
        def collect(
            self,
            repo_path: Path,
            doc_paths: list[str],
            declarations: list[DocClaim],
        ) -> list[DocClaim]:
            (repo_path / "docs/api.md").write_text(
                _document("Returns `1`."),
                encoding="utf-8",
            )
            return super().collect(repo_path, doc_paths, declarations)

    runtime = AgentRuntime()
    runtime.semantic_claim_provider = AddingClaimProvider()

    bundle = run(
        RunRequest(
            mode=RunMode.CHECK,
            repo_path=repo,
            state_dir=state_dir,
            semantic_analysis=True,
        ),
        runtime=runtime,
    )

    assert bundle.status is RunStatus.STALE
    assert bundle.findings == []


def test_conflicting_evidence_hash_makes_existing_finding_unresolved(
    tmp_path: Path,
) -> None:
    repo, state_dir = _semantic_repo(
        tmp_path,
        current_source=_constant_source(2),
    )

    class ConflictingHashProvider(SemanticReturnClaimProvider):
        def collect(
            self,
            repo_path: Path,
            doc_paths: list[str],
            declarations: list[DocClaim],
        ) -> list[DocClaim]:
            claims = super().collect(repo_path, doc_paths, declarations)
            return [
                claim.model_copy(
                    update={
                        "anchor": claim.anchor.model_copy(
                            update={"source_hash": "conflicting-doc-hash"}
                        )
                    }
                )
                for claim in claims
            ]

    runtime = AgentRuntime()
    runtime.semantic_claim_provider = ConflictingHashProvider()

    bundle = run(
        RunRequest(
            mode=RunMode.CHECK,
            repo_path=repo,
            state_dir=state_dir,
            semantic_analysis=True,
        ),
        runtime=runtime,
    )

    assert bundle.status is RunStatus.STALE
    assert len(bundle.findings) == 1
    assert bundle.findings[0].disposition is FindingDisposition.UNRESOLVED
    assert bundle.findings[0].reason_code == "global_snapshot_changed"


def test_config_only_truth_change_reanalyzes_semantic_claims(tmp_path: Path) -> None:
    repo, state_dir = _semantic_repo(
        tmp_path,
        truth="code_derived",
        base_source=_constant_source(2),
        base_document=_document("Returns `1`."),
    )
    (repo / "drift-agent.toml").write_text(_config("design"), encoding="utf-8")
    before = _worktree_bytes(repo)

    bundle = _semantic_check(repo, state_dir)

    assert bundle.status is RunStatus.DRIFT_FOUND
    assert len(bundle.findings) == 1
    assert bundle.findings[0].disposition is FindingDisposition.NEEDS_APPROVAL
    assert bundle.findings[0].reason_code == "truth_requires_approval"
    assert len(bundle.approval_required) == 1
    _assert_zero_model_read_only(repo, before, bundle)


@pytest.mark.parametrize(
    ("truth", "status", "disposition", "reason_code", "approval_count", "truth_source"),
    [
        (
            "unknown",
            RunStatus.DRIFT_FOUND,
            FindingDisposition.UNRESOLVED,
            "unknown_truth",
            0,
            "unknown",
        ),
        (
            "design",
            RunStatus.DRIFT_FOUND,
            FindingDisposition.NEEDS_APPROVAL,
            "truth_requires_approval",
            1,
            "human",
        ),
        (
            "contract",
            RunStatus.DRIFT_FOUND,
            FindingDisposition.NEEDS_APPROVAL,
            "truth_requires_approval",
            1,
            "human",
        ),
    ],
)
def test_semantic_truth_policy_routes_without_model_or_write(
    tmp_path: Path,
    truth: TruthKind,
    status: RunStatus,
    disposition: FindingDisposition,
    reason_code: str,
    approval_count: int,
    truth_source: str,
) -> None:
    repo, state_dir = _semantic_repo(
        tmp_path,
        truth=truth,
        current_source=_constant_source(2),
    )
    before = _worktree_bytes(repo)

    bundle = _semantic_check(repo, state_dir)

    assert bundle.status is status
    assert len(bundle.findings) == 1
    finding = bundle.findings[0]
    assert finding.type == "semantic_drift"
    assert finding.disposition is disposition
    assert finding.reason_code == reason_code
    assert finding.truth_source == truth_source
    assert len(bundle.approval_required) == approval_count
    if bundle.approval_required:
        assert bundle.approval_required[0].finding_id == finding.id
    _assert_zero_model_read_only(repo, before, bundle)


def test_unsupported_code_fact_is_unresolved_alignment_validation(tmp_path: Path) -> None:
    repo, state_dir = _semantic_repo(
        tmp_path,
        current_source=("def answer() -> int:\n    value = 2\n    return value\n"),
    )
    before = _worktree_bytes(repo)

    bundle = _semantic_check(repo, state_dir)

    assert bundle.status is RunStatus.UNRESOLVED
    assert bundle.findings == []
    semantic_validation = [
        result for result in bundle.validation if result.check == "semantic_alignment"
    ]
    assert len(semantic_validation) == 1
    assert semantic_validation[0].required is True
    assert semantic_validation[0].status is ValidationStatus.UNAVAILABLE
    assert semantic_validation[0].finding_ids == []
    assert semantic_validation[0].summary.startswith("semantic.code_fact_unsupported:")
    _assert_zero_model_read_only(repo, before, bundle)


def test_duplicate_semantic_claim_is_unresolved_alignment_validation(tmp_path: Path) -> None:
    repo, state_dir = _semantic_repo(
        tmp_path,
        current_source=_constant_source(2),
        duplicate_claim=True,
    )
    before = _worktree_bytes(repo)

    bundle = _semantic_check(repo, state_dir)

    assert bundle.status is RunStatus.UNRESOLVED
    semantic_validation = [
        result for result in bundle.validation if result.check == "semantic_alignment"
    ]
    assert len(semantic_validation) == 1
    assert semantic_validation[0].required is True
    assert semantic_validation[0].status is ValidationStatus.UNAVAILABLE
    assert semantic_validation[0].finding_ids == []
    assert semantic_validation[0].summary.startswith("semantic.ambiguity.claim:")
    _assert_zero_model_read_only(repo, before, bundle)


def test_doc_only_change_triggers_semantic_detection(tmp_path: Path) -> None:
    repo, state_dir = _semantic_repo(
        tmp_path,
        current_document=_document("Returns `2`."),
    )
    before = _worktree_bytes(repo)

    bundle = _semantic_check(repo, state_dir)

    assert bundle.status is RunStatus.DRIFT_FOUND
    assert bundle.scope == ["docs/api.md"]
    assert len(bundle.findings) == 1
    assert bundle.findings[0].kind == "semantic_direct_mismatch"
    assert bundle.findings[0].old_value["value"] == {"type": "int", "value": 2}
    assert bundle.findings[0].new_value["value"] == {"type": "int", "value": 1}
    _assert_zero_model_read_only(repo, before, bundle)


def test_semantic_decision_is_invalidated_when_doc_evidence_changes(
    tmp_path: Path,
) -> None:
    repo, state_dir = _semantic_repo(
        tmp_path,
        current_source=_constant_source(2),
    )
    first = _semantic_check(repo, state_dir)
    assert len(first.findings) == 1
    DecisionService(SQLiteStateStore(resolve_state_path(repo, state_dir))).add(
        DecisionAddRequest(
            repository_id=first.repository_id,
            run_id=first.run_id,
            finding_id=first.findings[0].id,
            action="ignore",
            reason="reviewed semantic evidence",
            actor="maintainer",
            confirmation=True,
        )
    )

    suppressed = _semantic_check(repo, state_dir)
    assert suppressed.status is RunStatus.CLEAN
    assert suppressed.findings == []
    assert len(suppressed.suppressed_findings) == 1

    document = repo / "docs/api.md"
    document.write_text(
        _document("Returns `1`.", suffix="\n<!-- evidence changed -->\n"),
        encoding="utf-8",
    )
    before = _worktree_bytes(repo)

    current = _semantic_check(repo, state_dir)

    assert current.status is RunStatus.DRIFT_FOUND
    assert len(current.findings) == 1
    assert current.findings[0].symbol_id == first.findings[0].symbol_id
    assert current.findings[0].id != first.findings[0].id
    assert any(
        event.kind == "decision_invalidated" and event.reason == "decision.doc_evidence_mismatch"
        for event in current.memory_events
    )
    _assert_zero_model_read_only(repo, before, current)


def test_missing_model_configuration_is_recorded_rather_than_silently_skipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--semantic` without a usable credential must not look like a clean pass.

    The deterministic constant-return detector is designed to run without a
    model, so an unusable credential cannot fail the run. But the model-backed
    section pass then does not run at all, and `model_calls == 0` alone cannot
    distinguish "skipped" from "ran and found nothing".
    """

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    repo, state_dir = _semantic_repo(
        tmp_path,
        current_source=_constant_source(2),
    )

    bundle = _semantic_check(repo, state_dir)

    receipts = [item for item in bundle.validation if item.check == "section_semantic"]
    assert len(receipts) == 1
    assert receipts[0].status is ValidationStatus.UNAVAILABLE
    assert receipts[0].summary.startswith("openrouter_api_key_missing:")
    # The deterministic half still ran, so the run itself is not a failure.
    assert bundle.status is not RunStatus.FAILED
    assert bundle.usage.model_calls == 0

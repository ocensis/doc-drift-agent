from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from drift_agent.evaluation.benchmark_cases import (
    CONTROL_CASE_IDS,
    PORTABLE_CASE_IDS,
    BenchmarkCaseError,
    CanonicalRepositorySnapshotV1,
    RepositorySnapshotError,
    canonical_digest,
    canonical_json_bytes,
    capture_git_metadata,
    capture_repository_snapshot,
    effective_changed_bytes,
    git_metadata_sha256,
    load_benchmark_case,
    load_benchmark_cases,
    prepare_benchmark_case,
    project_neutral_oracle,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        shell=False,
        env={
            "PATH": os.environ["PATH"],
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "LC_ALL": "C",
        },
    )


def test_frozen_selection_is_exactly_twelve_portable_and_six_controls() -> None:
    cases = load_benchmark_cases()

    assert len(cases) == 18
    assert tuple(case.case_id for case in cases if case.case_class == "portable") == (
        PORTABLE_CASE_IDS
    )
    assert tuple(case.case_id for case in cases if case.case_class == "control") == (
        CONTROL_CASE_IDS
    )
    assert len(PORTABLE_CASE_IDS) == 12
    assert len(CONTROL_CASE_IDS) == 6
    assert {case.operation for case in cases[:8]} == {"repair"}
    assert {case.operation for case in cases[8:12]} == {"check"}
    assert {case.layer for case in cases if case.case_class == "control"} == {
        "executable",
        "semantic",
    }


@pytest.mark.parametrize("case_id", PORTABLE_CASE_IDS)
def test_prepare_all_portable_cases_is_opaque_answer_free_and_digest_bound(
    tmp_path: Path,
    case_id: str,
) -> None:
    prepared = prepare_benchmark_case(case_id, tmp_path)

    assert prepared.repo_path.is_absolute()
    assert prepared.repo_path.parent == tmp_path.resolve()
    assert prepared.repo_path.name.startswith("repo-")
    assert case_id not in prepared.repo_path.as_posix()
    assert case_id not in repr(prepared)
    assert not (prepared.repo_path / "catalog.json").exists()
    assert not (prepared.repo_path / "manifest.json").exists()
    assert not any(path.name == "expected" for path in prepared.repo_path.rglob("*"))
    assert prepared.baseline_snapshot.status_records == ()
    assert prepared.prepared_snapshot.head_tree == prepared.baseline_snapshot.head_tree
    assert prepared.prepared_git_metadata == prepared.baseline_git_metadata
    assert prepared.snapshot_digest == canonical_digest(prepared.prepared_snapshot)
    assert prepared.task_digest == canonical_digest(prepared.task)
    assert prepared.scope_digest == canonical_digest(prepared.scope)
    assert prepared.task.operation == prepared.case.operation
    assert prepared.hidden_oracle.case_id == case_id

    visible = json.dumps(prepared.subject_contract(), sort_keys=True)
    assert case_id not in visible
    assert "expected_status" not in visible
    assert "expected_changed_bytes" not in visible
    assert "finding_multiset" not in visible

    baseline_entries = {entry.path: entry for entry in prepared.baseline_snapshot.worktree_entries}
    prepared_entries = {entry.path: entry for entry in prepared.prepared_snapshot.worktree_entries}
    worktree_hashes = {entry.sha256 for entry in prepared.prepared_snapshot.worktree_entries}
    for fixture in prepared.case.manifest.files:
        if fixture.role == "base":
            assert baseline_entries[fixture.target_path].sha256 == fixture.sha256
        elif fixture.role == "current":
            assert prepared_entries[fixture.target_path].sha256 == fixture.sha256
        else:
            assert fixture.sha256 not in worktree_hashes


def test_two_fresh_repositories_have_identical_canonical_inputs_and_digests(
    tmp_path: Path,
) -> None:
    first = prepare_benchmark_case(
        "click.multi-group-partial.v1",
        tmp_path / "one",
        opaque_id="1" * 32,
    )
    second = prepare_benchmark_case(
        "click.multi-group-partial.v1",
        tmp_path / "two",
        opaque_id="2" * 32,
    )

    assert first.repo_path != second.repo_path
    assert first.baseline_snapshot == second.baseline_snapshot
    assert first.prepared_snapshot == second.prepared_snapshot
    assert first.baseline_git_metadata == second.baseline_git_metadata
    assert first.prepared_git_metadata == second.prepared_git_metadata
    assert first.scope == second.scope
    assert first.snapshot_digest == second.snapshot_digest
    assert first.task_digest == second.task_digest
    assert first.scope_digest == second.scope_digest
    assert first.hidden_oracle == second.hidden_oracle


def test_scope_preserves_explicit_rename_and_staged_vs_unstaged_state(
    tmp_path: Path,
) -> None:
    prepared = prepare_benchmark_case("click.multi-group-partial.v1", tmp_path)
    by_path = {path.path: path for path in prepared.scope.paths}

    rename = by_path["src/click_eval/current.py"]
    assert rename.change_kind == "renamed"
    assert rename.old_path == "src/click_eval/legacy.py"
    assert rename.staged is True
    assert rename.unstaged is False
    assert rename.index_status == "R"
    assert rename.before is not None
    assert rename.after is not None

    modified = by_path["src/click_eval/design.py"]
    assert modified.change_kind == "modified"
    assert modified.staged is False
    assert modified.unstaged is True
    assert modified.worktree_status == "M"
    assert prepared.scope.explicit_staged_paths == (
        "src/click_eval/current.py",
        "src/click_eval/legacy.py",
    )


def test_neutral_projection_covers_every_portable_oracle_without_collisions() -> None:
    oracles = [
        project_neutral_oracle(load_benchmark_case(case_id)) for case_id in PORTABLE_CASE_IDS
    ]
    findings = [finding for oracle in oracles for finding in oracle.findings]

    assert len(findings) == 14
    assert len({canonical_json_bytes(finding) for finding in findings}) == len(findings)
    assert {finding.finding_family for finding in findings} == {
        "parameter_default_changed",
        "parameter_annotation_changed",
        "symbol_renamed",
        "symbol_deleted",
        "google_arg_changed",
        "google_returns_changed",
        "broken_example",
        "ambiguous_or_unsupported",
    }
    for oracle in oracles:
        assert type(oracle).model_validate_json(oracle.model_dump_json()) == oracle


def test_neutral_projection_uses_typed_values_and_hides_private_wire_encodings() -> None:
    default = project_neutral_oracle(load_benchmark_case("click.parameter-default.v1"))
    default_key = default.findings[0]
    assert (default_key.old_value.kind, default_key.old_value.value) == (
        "python_literal",
        False,
    )
    assert (default_key.new_value.kind, default_key.new_value.value) == (
        "python_literal",
        True,
    )
    assert "Constant(value=" not in canonical_json_bytes(default_key).decode("utf-8")

    conflict = project_neutral_oracle(load_benchmark_case("click.conflict.v1"))
    annotation = next(
        finding
        for finding in conflict.findings
        if finding.finding_family == "parameter_annotation_changed"
    )
    assert annotation.old_value.value == "str"
    assert annotation.new_value.value == "Color"
    assert "Name(id=" not in canonical_json_bytes(annotation).decode("utf-8")

    google_arg = project_neutral_oracle(
        load_benchmark_case("pydantic.apply-validators-field-name.v1")
    ).findings[0]
    assert google_arg.old_value.kind == "present"
    assert google_arg.new_value.kind == "missing"


@pytest.mark.parametrize(
    ("case_id", "component_kind", "doc_path"),
    (
        ("executable.doctest-fail.v1", "doctest", "docs/api.md"),
        ("executable.pytest-fail.v1", "pytest", "tests/test_example.py"),
    ),
)
def test_executable_oracle_exposes_only_validation_status_not_command_arguments(
    case_id: str,
    component_kind: str,
    doc_path: str,
) -> None:
    oracle = project_neutral_oracle(load_benchmark_case(case_id))
    finding = oracle.findings[0]

    assert finding.code_path == "drift-agent.toml"
    assert finding.doc_path == doc_path
    assert finding.symbol_fqn is None
    assert finding.component_kind == component_kind
    assert (finding.old_value.kind, finding.old_value.value) == (
        "validation_status",
        "passed",
    )
    assert (finding.new_value.kind, finding.new_value.value) == (
        "validation_status",
        "failed",
    )
    encoded = canonical_json_bytes(finding)
    assert b"arguments" not in encoded
    assert b"-q" not in encoded


@pytest.mark.parametrize("case_id", CONTROL_CASE_IDS)
def test_controls_cannot_enter_portable_preparer_or_neutral_oracle(
    tmp_path: Path,
    case_id: str,
) -> None:
    case = load_benchmark_case(case_id)

    with pytest.raises(BenchmarkCaseError, match="private Stage 3 scorer"):
        project_neutral_oracle(case)
    with pytest.raises(BenchmarkCaseError, match="isolated control runner"):
        prepare_benchmark_case(case_id, tmp_path)
    assert not list(tmp_path.glob("repo-*"))


def test_effective_changed_bytes_are_pre_subject_to_post_subject_and_include_symlinks(
    tmp_path: Path,
) -> None:
    prepared = prepare_benchmark_case("click.parameter-default.v1", tmp_path)
    before = prepared.prepared_snapshot
    document = prepared.repo_path / "docs/api.md"
    document.write_text(document.read_text(encoding="utf-8") + "\nupdated\n", encoding="utf-8")
    os.symlink("/outside/oracle-must-not-be-read", prepared.repo_path / "link")

    after = capture_repository_snapshot(prepared.repo_path)
    changes = effective_changed_bytes(before, after)
    by_path = {change.path: change for change in changes}

    assert set(by_path) == {"docs/api.md", "link"}
    assert by_path["docs/api.md"].before is not None
    assert by_path["docs/api.md"].after is not None
    assert by_path["docs/api.md"].to_comparison_change().path == "docs/api.md"
    assert by_path["link"].before is None
    assert by_path["link"].after is not None
    assert by_path["link"].after.kind == "symlink"
    assert (
        by_path["link"].after.sha256
        == hashlib.sha256(b"/outside/oracle-must-not-be-read").hexdigest()
    )


def test_canonical_index_projection_records_flags_without_hashing_stat_cache(
    tmp_path: Path,
) -> None:
    prepared = prepare_benchmark_case("click.parameter-default.v1", tmp_path)
    repo = prepared.repo_path
    (repo / "intent.txt").write_text("intent\n", encoding="utf-8")
    _git(repo, "add", "-N", "intent.txt")
    _git(repo, "update-index", "--assume-unchanged", "docs/api.md")
    _git(repo, "update-index", "--skip-worktree", "src/click_eval/__init__.py")

    snapshot = capture_repository_snapshot(repo)
    index = {entry.path: entry for entry in snapshot.index_entries}

    assert index["intent.txt"].intent_to_add is True
    assert index["docs/api.md"].assume_unchanged is True
    assert index["src/click_eval/__init__.py"].skip_worktree is True
    assert canonical_json_bytes(snapshot) == canonical_json_bytes(
        CanonicalRepositorySnapshotV1.model_validate_json(snapshot.model_dump_json())
    )


def test_snapshot_digest_changes_for_index_only_state_change(tmp_path: Path) -> None:
    prepared = prepare_benchmark_case("click.parameter-default.v1", tmp_path)
    original = prepared.prepared_snapshot
    _git(prepared.repo_path, "add", "src/click_eval/api.py")
    staged = capture_repository_snapshot(prepared.repo_path)

    assert original.worktree_entries == staged.worktree_entries
    assert original.index_entries != staged.index_entries
    assert original.status_records != staged.status_records
    assert canonical_digest(original) != canonical_digest(staged)


def test_git_metadata_separately_detects_ref_config_and_hook_mutations(
    tmp_path: Path,
) -> None:
    prepared = prepare_benchmark_case("click.parameter-default.v1", tmp_path)
    before = prepared.prepared_git_metadata
    repo = prepared.repo_path
    _git(repo, "config", "--local", "benchmark.mutated", "true")
    _git(repo, "tag", "subject-created")
    hook = repo / ".git/hooks/subject-created"
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    hook.chmod(0o755)

    after = capture_git_metadata(repo)

    assert after != before
    assert after.head_oid == before.head_oid
    assert any(ref.name == "refs/tags/subject-created" for ref in after.refs)
    assert any(entry.key == "benchmark.mutated" for entry in after.config)
    assert any(entry.path == "hooks/subject-created" for entry in after.hooks)
    assert git_metadata_sha256(after) != git_metadata_sha256(before)
    # Pair identity intentionally excludes these safety fields; the scorer checks
    # their separate evidence digest instead.
    assert capture_repository_snapshot(repo) == prepared.prepared_snapshot


@pytest.mark.parametrize("opaque_id", ["short", "A" * 32, "g" * 32, "0" * 33])
def test_preparer_rejects_noncanonical_opaque_ids(tmp_path: Path, opaque_id: str) -> None:
    with pytest.raises(BenchmarkCaseError, match="32 lowercase hexadecimal"):
        prepare_benchmark_case(
            "click.parameter-default.v1",
            tmp_path,
            opaque_id=opaque_id,
        )


def test_snapshot_rejects_non_git_directories(tmp_path: Path) -> None:
    with pytest.raises(RepositorySnapshotError, match="not a Git worktree"):
        capture_repository_snapshot(tmp_path)

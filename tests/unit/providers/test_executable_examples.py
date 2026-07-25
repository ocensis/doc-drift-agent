from pathlib import Path

from drift_agent.domain.models import WorkspaceSnapshot
from drift_agent.hashing import sha256_file
from drift_agent.providers.executable_examples import (
    ConfiguredExecutableExampleProvider,
    ExecutableExample,
    ExecutableExampleIssue,
)
from drift_agent.validation.commands import compile_validation_command


def _snapshot(repo: Path, *paths: str) -> WorkspaceSnapshot:
    return WorkspaceSnapshot(
        head_revision="head",
        workspace_fingerprint="workspace",
        input_file_hashes={path: sha256_file(repo / path) for path in paths},
    )


def test_provider_builds_stable_single_target_examples_and_deduplicates(
    tmp_path: Path,
) -> None:
    target = tmp_path / "docs/example.md"
    target.parent.mkdir()
    target.write_text(">>> 1 + 1\n2\n", encoding="utf-8")
    snapshot = _snapshot(tmp_path, "docs/example.md")
    provider = ConfiguredExecutableExampleProvider()

    first = provider.collect(
        tmp_path,
        [
            "python -m doctest docs/example.md",
            "python -m doctest docs/example.md",
        ],
        snapshot=snapshot,
        config_hash="config-hash",
        compiler=compile_validation_command,
    )
    second = provider.collect(
        tmp_path,
        ["python -m doctest docs/example.md"],
        snapshot=snapshot,
        config_hash="config-hash",
        compiler=compile_validation_command,
    )

    assert len(first.entries) == 1
    example = first.entries[0]
    assert isinstance(example, ExecutableExample)
    assert example == second.entries[0]
    assert example.kind == "doctest"
    assert example.target == "docs/example.md"
    assert example.target_hash == sha256_file(target)
    assert example.component_id == "doctest:docs/example.md"


def test_provider_reports_unsafe_or_ambiguous_commands_without_guessing(
    tmp_path: Path,
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    for name in ("one.md", "two.md"):
        (docs / name).write_text(">>> 1\n1\n", encoding="utf-8")
    snapshot = _snapshot(tmp_path, "docs/one.md", "docs/two.md")

    collection = ConfiguredExecutableExampleProvider().collect(
        tmp_path,
        [
            "python -m pip list",
            "python -m doctest docs/one.md docs/two.md",
            "python -m doctest docs/missing.md",
        ],
        snapshot=snapshot,
        config_hash="config-hash",
        compiler=compile_validation_command,
    )

    assert len(collection.entries) == 3
    assert all(isinstance(entry, ExecutableExampleIssue) for entry in collection.entries)
    summaries = [entry.summary for entry in collection.entries]
    assert any("not allowlisted" in summary for summary in summaries)
    assert any("exactly one" in summary for summary in summaries)
    assert any("unavailable" in summary for summary in summaries)


def test_example_id_is_stable_across_evidence_hash_changes(tmp_path: Path) -> None:
    target = tmp_path / "docs/example.md"
    target.parent.mkdir()
    target.write_text(">>> 1\n2\n", encoding="utf-8")
    provider = ConfiguredExecutableExampleProvider()
    first = provider.collect(
        tmp_path,
        ["python -m doctest docs/example.md"],
        snapshot=_snapshot(tmp_path, "docs/example.md"),
        config_hash="first-config-hash",
        compiler=compile_validation_command,
    ).entries[0]
    target.write_text(">>> 1\n3\n", encoding="utf-8")
    second = provider.collect(
        tmp_path,
        ["python -m doctest docs/example.md"],
        snapshot=_snapshot(tmp_path, "docs/example.md"),
        config_hash="second-config-hash",
        compiler=compile_validation_command,
    ).entries[0]

    assert isinstance(first, ExecutableExample)
    assert isinstance(second, ExecutableExample)
    assert first.id == second.id
    assert first.target_hash != second.target_hash

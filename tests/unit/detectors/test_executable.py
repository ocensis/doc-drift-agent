from pathlib import Path

from drift_agent.detectors.executable import ExecutableExampleDetector
from drift_agent.domain.enums import ValidationStatus
from drift_agent.domain.models import ValidationResult, WorkspaceSnapshot
from drift_agent.hashing import sha256_file
from drift_agent.providers.executable_examples import (
    ConfiguredExecutableExampleProvider,
    ExecutableExample,
)
from drift_agent.validation.commands import compile_validation_command


def _example(tmp_path: Path) -> ExecutableExample:
    target = tmp_path / "docs/example.md"
    target.parent.mkdir()
    target.write_text(">>> 1 + 1\n3\n", encoding="utf-8")
    snapshot = WorkspaceSnapshot(
        head_revision="head",
        workspace_fingerprint="workspace",
        input_file_hashes={"docs/example.md": sha256_file(target)},
    )
    entry = (
        ConfiguredExecutableExampleProvider()
        .collect(
            tmp_path,
            ["python -m doctest docs/example.md"],
            snapshot=snapshot,
            config_hash="config-hash",
            compiler=compile_validation_command,
        )
        .entries[0]
    )
    assert isinstance(entry, ExecutableExample)
    return entry


def _result(status: ValidationStatus, summary: str, duration_ms: int) -> ValidationResult:
    return ValidationResult(
        finding_ids=[],
        attempt_id="check-example",
        check="check_doctest",
        required=True,
        status=status,
        summary=summary,
        duration_ms=duration_ms,
    )


def test_detector_emits_only_for_real_failures_with_stable_identity(
    tmp_path: Path,
) -> None:
    example = _example(tmp_path)
    detector = ExecutableExampleDetector()

    first = detector.detect(
        example,
        _result(ValidationStatus.FAILED, "first diagnostic", 3),
        repository_id="repository",
    )
    second = detector.detect(
        example,
        _result(ValidationStatus.FAILED, "different diagnostic", 999),
        repository_id="repository",
    )

    assert first is not None
    assert second is not None
    assert first.id == second.id
    assert first.fingerprint == second.fingerprint
    assert first.type == "signature_drift"
    assert first.kind == "broken_example"
    assert first.reason_code == "validation_failed"
    assert first.truth_source == "unknown"
    assert first.code_evidence.path == "drift-agent.toml"
    assert first.doc_evidence.path == "docs/example.md"


def test_detector_does_not_turn_pass_or_unavailable_into_drift(tmp_path: Path) -> None:
    example = _example(tmp_path)
    detector = ExecutableExampleDetector()

    assert (
        detector.detect(
            example,
            _result(ValidationStatus.PASSED, "passed", 1),
            repository_id="repository",
        )
        is None
    )
    assert (
        detector.detect(
            example,
            _result(ValidationStatus.UNAVAILABLE, "timed out", 1),
            repository_id="repository",
        )
        is None
    )

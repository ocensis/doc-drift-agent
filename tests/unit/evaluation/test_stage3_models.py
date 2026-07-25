from __future__ import annotations

import pytest
from pydantic import ValidationError

from drift_agent.evaluation.models import Provenance, WorkspaceInput
from drift_agent.evaluation.stage3_models import (
    Stage3Accounting,
    Stage3CaseManifest,
    Stage3ExpectedResult,
    Stage3ModelStep,
)


def _provenance() -> Provenance:
    return Provenance(
        kind="project_authored",
        repository="project://doc-code-drift-agent",
        code_revision="stage3-v1",
        doc_revision="stage3-v1",
        source_urls=(),
        license_spdx="LicenseRef-Project-Authored",
        copied_bytes=0,
    )


def _expected(accounting: Stage3Accounting) -> Stage3ExpectedResult:
    return Stage3ExpectedResult(
        status="clean",
        finding_multiset=(),
        dispositions=(),
        reason_codes=(),
        changed_bytes=(),
        accounting=accounting,
    )


def test_executable_manifest_freezes_zero_model_accounting() -> None:
    manifest = Stage3CaseManifest(
        schema_version=1,
        dataset_id="stage3-v1",
        case_id="executable.doctest-pass.v1",
        case_kind="executable",
        provenance=_provenance(),
        files=(),
        workspace=WorkspaceInput(),
        operation="check",
        coverage_tags=("doctest", "passing"),
        expected=_expected(Stage3Accounting(validation_commands=1)),
        offline=True,
    )

    assert manifest.expected.accounting.model_calls == 0
    assert manifest.model_script == ()


def test_semantic_manifest_cross_checks_every_scripted_usage_field() -> None:
    step = Stage3ModelStep(
        profile="fast",
        request_sha256="a" * 64,
        output={"decision": "replace"},
        prompt_tokens=10,
        completion_tokens=2,
        cost_nano_usd=50,
    )
    accounting = Stage3Accounting(
        repair_outcome="success",
        patch_attempts=1,
        model_calls_by_profile={"fast": 1},
        input_tokens=10,
        output_tokens=2,
        known_cost_nano_usd=50,
    )

    manifest = Stage3CaseManifest(
        schema_version=1,
        dataset_id="stage3-v1",
        case_id="semantic.fast-success.v1",
        case_kind="semantic",
        provenance=_provenance(),
        files=(),
        workspace=WorkspaceInput(),
        operation="repair",
        semantic_repair=True,
        coverage_tags=("semantic", "fast_success"),
        model_script=(step,),
        expected=_expected(accounting),
        offline=True,
    )

    assert manifest.expected.accounting.model_calls == 1

    with pytest.raises(ValidationError, match="prompt tokens"):
        Stage3CaseManifest.model_validate(
            manifest.model_dump(
                mode="python",
                exclude={"expected": {"accounting": {"input_tokens"}}},
            )
            | {
                "expected": manifest.expected.model_copy(
                    update={"accounting": accounting.model_copy(update={"input_tokens": 11})}
                )
            }
        )


def test_accounting_rejects_direct_strong_route_and_inexact_abstention() -> None:
    with pytest.raises(ValidationError, match="earlier fast call"):
        Stage3Accounting(
            repair_outcome="success",
            patch_attempts=1,
            model_calls_by_profile={"strong": 1},
        )

    with pytest.raises(ValidationError, match="exactly two"):
        Stage3Accounting(
            repair_outcome="abstained",
            patch_attempts=1,
            model_calls_by_profile={"fast": 1},
        )


def test_executable_case_rejects_a_script_even_when_its_usage_matches() -> None:
    step = Stage3ModelStep(
        profile="fast",
        request_sha256="b" * 64,
        output={"ok": True},
        prompt_tokens=1,
        completion_tokens=1,
        cost_nano_usd=1,
    )
    with pytest.raises(ValidationError, match="zero model calls"):
        Stage3CaseManifest(
            schema_version=1,
            dataset_id="stage3-v1",
            case_id="executable.invalid-model.v1",
            case_kind="executable",
            provenance=_provenance(),
            files=(),
            workspace=WorkspaceInput(),
            operation="check",
            coverage_tags=("executable",),
            model_script=(step,),
            expected=_expected(
                Stage3Accounting(
                    model_calls_by_profile={"fast": 1},
                    input_tokens=1,
                    output_tokens=1,
                    known_cost_nano_usd=1,
                )
            ),
            offline=True,
        )


def test_semantic_manifest_rejects_direct_strong_or_fast_after_strong() -> None:
    def step(profile: str, digest: str) -> Stage3ModelStep:
        return Stage3ModelStep.model_validate(
            {
                "profile": profile,
                "request_sha256": digest * 64,
                "output": {"decision": "replace"},
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "cost_nano_usd": 1,
            }
        )

    common = {
        "schema_version": 1,
        "dataset_id": "stage3-v1",
        "case_id": "semantic.invalid-route.v1",
        "case_kind": "semantic",
        "provenance": _provenance(),
        "files": (),
        "workspace": WorkspaceInput(),
        "operation": "repair",
        "semantic_repair": True,
        "coverage_tags": ("semantic",),
        "offline": True,
    }
    with pytest.raises(ValidationError, match="start with fast"):
        Stage3CaseManifest.model_validate(
            common
            | {
                "model_script": (step("strong", "c"), step("fast", "d")),
                "expected": _expected(
                    Stage3Accounting(
                        repair_outcome="success",
                        patch_attempts=1,
                        model_calls_by_profile={"fast": 1, "strong": 1},
                        input_tokens=2,
                        output_tokens=2,
                        known_cost_nano_usd=2,
                    )
                ),
            }
        )

    with pytest.raises(ValidationError, match="return to fast"):
        Stage3CaseManifest.model_validate(
            common
            | {
                "model_script": (
                    step("fast", "a"),
                    step("strong", "b"),
                    step("fast", "c"),
                ),
                "expected": _expected(
                    Stage3Accounting(
                        repair_outcome="success",
                        patch_attempts=2,
                        model_calls_by_profile={"fast": 2, "strong": 1},
                        input_tokens=3,
                        output_tokens=3,
                        known_cost_nano_usd=3,
                    )
                ),
            }
        )

from __future__ import annotations

import hashlib
from typing import Literal

from drift_agent.evaluation.models import Provenance, WorkspaceInput
from drift_agent.evaluation.stage3_metrics import (
    build_stage3_report,
    deterministic_stage3_projection,
    evaluate_stage3_case,
)
from drift_agent.evaluation.stage3_models import (
    Stage3Accounting,
    Stage3CaseEvaluation,
    Stage3CaseManifest,
    Stage3CaseObservation,
    Stage3ExpectedResult,
    Stage3ModelStep,
)
from drift_agent.model.contracts import ModelProfile

Status = Literal[
    "clean",
    "drift_found",
    "fixed",
    "partial",
    "needs_approval",
    "unresolved",
    "stale",
    "failed",
]


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


def _script(accounting: Stage3Accounting, seed: str) -> tuple[Stage3ModelStep, ...]:
    steps: list[Stage3ModelStep] = []
    ordinal = 0
    remaining_input = accounting.input_tokens
    remaining_output = accounting.output_tokens
    remaining_cost = accounting.known_cost_nano_usd
    profiles: list[ModelProfile] = []
    for profile in ("fast", "strong"):
        profiles.extend([profile] * accounting.model_calls_by_profile.get(profile, 0))
    request_sha256 = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    for profile in profiles:
        ordinal += 1
        calls_left = len(profiles) - ordinal + 1
        prompt_tokens = remaining_input // calls_left
        completion_tokens = remaining_output // calls_left
        cost = remaining_cost // calls_left
        remaining_input -= prompt_tokens
        remaining_output -= completion_tokens
        remaining_cost -= cost
        steps.append(
            Stage3ModelStep(
                profile=profile,
                request_sha256=request_sha256,
                output={"decision": "replace", "ordinal": ordinal},
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost_nano_usd=cost,
            )
        )
    return tuple(steps)


def _manifest(
    case_id: str,
    accounting: Stage3Accounting,
    *,
    status: Status,
) -> Stage3CaseManifest:
    semantic = accounting.repair_outcome != "not_applicable"
    return Stage3CaseManifest(
        schema_version=1,
        dataset_id="stage3-v1",
        case_id=case_id,
        case_kind="semantic" if semantic else "executable",
        provenance=_provenance(),
        files=(),
        workspace=WorkspaceInput(),
        operation="repair" if semantic else "check",
        semantic_repair=semantic,
        coverage_tags=("semantic",) if semantic else ("executable",),
        model_script=_script(accounting, case_id[0]),
        expected=Stage3ExpectedResult(
            status=status,
            finding_multiset=(),
            dispositions=(),
            reason_codes=(),
            changed_bytes=(),
            accounting=accounting,
        ),
        offline=True,
    )


def _evaluation(manifest: Stage3CaseManifest) -> Stage3CaseEvaluation:
    observation = Stage3CaseObservation(
        status=manifest.expected.status,
        findings=(),
        changed_bytes=(),
        accounting=manifest.expected.accounting,
        network_calls=0,
        offline=True,
        model_script_consumed=True,
    )
    return evaluate_stage3_case(manifest, observation)


def test_sc3_019_metrics_are_exact_counts_ratios_tokens_commands_and_cost() -> None:
    executable = Stage3Accounting(validation_commands=1)
    fast_success = Stage3Accounting(
        repair_outcome="success",
        patch_attempts=1,
        model_calls_by_profile={"fast": 1},
        input_tokens=10,
        output_tokens=2,
        known_cost_nano_usd=100,
    )
    strong_success = Stage3Accounting(
        repair_outcome="success",
        patch_attempts=2,
        model_calls_by_profile={"fast": 1, "strong": 1},
        input_tokens=30,
        output_tokens=6,
        known_cost_nano_usd=300,
    )
    abstained = Stage3Accounting(
        repair_outcome="abstained",
        patch_attempts=2,
        model_calls_by_profile={"fast": 1, "strong": 1},
        input_tokens=30,
        output_tokens=6,
        known_cost_nano_usd=300,
    )
    evaluations = tuple(
        _evaluation(manifest)
        for manifest in (
            _manifest("executable.pass.v1", executable, status="clean"),
            _manifest("semantic.fast.v1", fast_success, status="fixed"),
            _manifest("semantic.strong.v1", strong_success, status="fixed"),
            _manifest("semantic.abstain.v1", abstained, status="unresolved"),
        )
    )

    report = build_stage3_report(evaluations)
    summary = report.summary

    assert (summary.total, summary.passed, summary.failed) == (4, 4, 0)
    assert summary.semantic_repair_opportunities == 3
    assert summary.repair_success_at_1.model_dump() == {
        "numerator": 1,
        "denominator": 3,
    }
    assert summary.repair_success_at_2.model_dump() == {
        "numerator": 2,
        "denominator": 3,
    }
    assert summary.abstention_correctness.model_dump() == {
        "numerator": 1,
        "denominator": 1,
    }
    assert summary.fast_route_ratio.model_dump() == {"numerator": 3, "denominator": 5}
    assert summary.strong_route_ratio.model_dump() == {
        "numerator": 2,
        "denominator": 5,
    }
    assert summary.model_calls == 5
    assert summary.validation_commands == 1
    assert (summary.input_tokens, summary.output_tokens) == (70, 14)
    assert summary.known_cost_nano_usd == 700
    assert summary.executable_zero_model_compliance is True
    assert summary.offline_compliance is True
    assert summary.model_script_compliance is True
    assert deterministic_stage3_projection(report) == deterministic_stage3_projection(
        build_stage3_report(evaluations)
    )


def test_accounting_or_unconsumed_script_mismatch_fails_the_case() -> None:
    expected = Stage3Accounting(
        repair_outcome="success",
        patch_attempts=1,
        model_calls_by_profile={"fast": 1},
        input_tokens=10,
        output_tokens=2,
        known_cost_nano_usd=100,
    )
    manifest = _manifest("semantic.mismatch.v1", expected, status="fixed")
    observation = Stage3CaseObservation(
        status="fixed",
        findings=(),
        changed_bytes=(),
        accounting=expected.model_copy(update={"input_tokens": 11}),
        network_calls=0,
        offline=True,
        model_script_consumed=False,
    )

    evaluation = evaluate_stage3_case(manifest, observation)

    assert evaluation.passed is False
    assert evaluation.accounting_matches is False
    assert evaluation.model_script_compliance is False

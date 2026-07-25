from drift_agent.detectors.structural import StructuralDetector
from drift_agent.domain.enums import ValidationStatus
from drift_agent.domain.models import (
    Alignment,
    CodeFact,
    DocClaim,
    ParameterFact,
    PatchAttempt,
    SourceAnchor,
)


def _alignment(doc_has_color: bool) -> Alignment:
    message = ParameterFact(name="message", kind="positional or keyword", annotation="str")
    color = ParameterFact(
        name="color",
        kind="positional or keyword",
        annotation="bool",
        default="True",
        required=False,
    )
    fact = CodeFact(
        symbol_id="click_demo.api.echo",
        path="src/click_demo/api.py",
        line=1,
        signature="echo(message: str, color: bool = True) -> None",
        parameters=[message, color],
        return_annotation="None",
        source_hash="sha256:code",
    )
    claim_parameters = [message, color] if doc_has_color else [message]
    claim = DocClaim(
        id="claim_1",
        symbol_id=fact.symbol_id,
        signature=(
            "echo(message: str, color: bool = True) -> None"
            if doc_has_color
            else "echo(message: str) -> None"
        ),
        parameters=claim_parameters,
        return_annotation="None",
        anchor=SourceAnchor(
            path="docs/api.md",
            line=1,
            start_byte=0,
            end_byte=1,
            exact_text="x",
            source_hash="sha256:doc",
        ),
    )
    return Alignment(fact=fact, claim=claim)


def test_detect_reports_parameter_mismatch_with_double_anchor() -> None:
    findings = StructuralDetector().detect([_alignment(doc_has_color=False)])

    assert len(findings) == 1
    assert "color" in findings[0].reason
    assert findings[0].code_evidence.path == "src/click_demo/api.py"
    assert findings[0].doc_evidence.path == "docs/api.md"


def test_validate_passes_when_reparsed_alignment_matches() -> None:
    attempt = PatchAttempt(
        id="attempt_1",
        finding_ids=["finding_1"],
        path="docs/api.md",
        source_hash="sha256:doc",
        start_byte=0,
        end_byte=1,
        expected_text="x",
        replacement_text="y",
        unified_diff="diff",
    )

    result = StructuralDetector().validate([_alignment(doc_has_color=True)], attempt)

    assert result.status is ValidationStatus.PASSED


def test_detect_reports_wrong_stub_function_name() -> None:
    alignment = _alignment(doc_has_color=True)
    wrong_name = alignment.model_copy(
        update={
            "claim": alignment.claim.model_copy(
                update={"signature": "wrong(message: str, color: bool = True) -> None"}
            )
        }
    )

    findings = StructuralDetector().detect([wrong_name])

    assert len(findings) == 1
    assert "signature" in findings[0].reason

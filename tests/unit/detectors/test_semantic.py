import pytest

from drift_agent.detectors.semantic import ConstantReturnSemanticDetector
from drift_agent.domain.enums import FindingDisposition
from drift_agent.domain.models import (
    Alignment,
    CodeFact,
    DocClaim,
    SemanticCodeFact,
    SemanticScalar,
    SourceAnchor,
)


def _anchor(path: str, source_hash: str, text: str, start: int) -> SourceAnchor:
    return SourceAnchor(
        path=path,
        line=2,
        start_byte=start,
        end_byte=start + len(text.encode("utf-8")),
        exact_text=text,
        source_hash=source_hash,
    )


def _alignment(
    *,
    mode: str,
    claimed_type: str = "bool",
    claimed_value: object = True,
    code_type: str = "bool",
    code_value: object = False,
    claim_id: str = "claim_semantic",
    doc_hash: str = "sha256:doc",
) -> Alignment:
    code_anchor = _anchor(
        "src/click_demo/api.py",
        "sha256:code",
        repr(code_value),
        30,
    )
    fact = CodeFact(
        symbol_id="click_demo.api.flag",
        path=code_anchor.path,
        line=1,
        signature="flag() -> bool",
        parameters=[],
        return_annotation="bool",
        source_hash=code_anchor.source_hash,
        semantic_facts=[
            SemanticCodeFact(
                normalized_value=SemanticScalar(
                    type=code_type,  # type: ignore[arg-type]
                    value=code_value,
                ),
                anchor=code_anchor,
            )
        ],
    )
    sentence = f"{'Always returns' if mode == 'always' else 'Returns'} `{claimed_value!r}`."
    claim = DocClaim(
        id=claim_id,
        symbol_id=fact.symbol_id,
        kind="semantic_assertion",
        signature=sentence,
        anchor=_anchor("docs/api.md", doc_hash, sentence, 80),
        component_id="return.literal",
        normalized_value={
            "predicate": "return_literal",
            "mode": mode,
            "value": {"type": claimed_type, "value": claimed_value},
        },
    )
    return Alignment(fact=fact, claim=claim)


def test_direct_mismatch_uses_typed_v3_evidence_and_values() -> None:
    alignment = _alignment(mode="direct")

    findings = ConstantReturnSemanticDetector().detect(
        [alignment],
        repository_id="repository",
    )

    assert len(findings) == 1
    finding = findings[0]
    assert finding.type == "semantic_drift"
    assert finding.kind == "semantic_direct_mismatch"
    assert finding.reason_code == "semantic.direct_mismatch"
    assert finding.component_id == "return.literal"
    assert finding.disposition is FindingDisposition.DETECTED
    assert finding.truth_source == "code"
    assert finding.old_value == {
        "predicate": "return_literal",
        "mode": "direct",
        "value": {"type": "bool", "value": True},
    }
    assert finding.new_value == {
        "predicate": "return_literal",
        "value": {"type": "bool", "value": False},
    }
    assert finding.code_evidence.start_byte == 30
    assert finding.code_evidence.end_byte == 35
    assert finding.doc_evidence.start_byte == alignment.claim.anchor.start_byte
    assert finding.doc_evidence.end_byte == alignment.claim.anchor.end_byte
    assert finding.fingerprint
    assert finding.id == f"finding_{finding.fingerprint}"


def test_always_assertion_is_classified_as_over_promise() -> None:
    finding = ConstantReturnSemanticDetector().detect(
        [_alignment(mode="always")],
        repository_id="repository",
    )[0]

    assert finding.kind == "semantic_over_promise"
    assert finding.reason_code == "semantic.over_promise"
    assert finding.old_value["mode"] == "always"


def test_equal_constant_return_produces_no_finding() -> None:
    alignment = _alignment(mode="direct", code_value=True)

    assert (
        ConstantReturnSemanticDetector().detect(
            [alignment],
            repository_id="repository",
        )
        == []
    )


def test_bool_and_int_values_do_not_collapse_under_python_equality() -> None:
    finding = ConstantReturnSemanticDetector().detect(
        [
            _alignment(
                mode="direct",
                claimed_type="bool",
                claimed_value=True,
                code_type="int",
                code_value=1,
            )
        ],
        repository_id="repository",
    )[0]

    assert finding.old_value["value"] == {"type": "bool", "value": True}
    assert finding.new_value["value"] == {"type": "int", "value": 1}


def test_fingerprint_is_stable_and_ignores_ephemeral_claim_identity() -> None:
    detector = ConstantReturnSemanticDetector()
    first_alignment = _alignment(mode="direct", claim_id="claim_one")
    second_alignment = _alignment(mode="direct", claim_id="claim_two")

    first = detector.detect([first_alignment], repository_id="repository")[0]
    repeated = detector.detect([first_alignment], repository_id="repository")[0]
    equivalent = detector.detect([second_alignment], repository_id="repository")[0]
    changed_evidence = detector.detect(
        [_alignment(mode="direct", doc_hash="sha256:changed-doc")],
        repository_id="repository",
    )[0]

    assert first.id == repeated.id == equivalent.id
    assert first.fingerprint == repeated.fingerprint == equivalent.fingerprint
    assert changed_evidence.id != first.id
    assert changed_evidence.fingerprint != first.fingerprint


def test_detector_rejects_non_exact_alignment() -> None:
    alignment = _alignment(mode="direct").model_copy(
        update={"method": "git_rename", "old_symbol_id": "old.api.flag"}
    )

    with pytest.raises(ValueError, match="requires one eligible alignment"):
        ConstantReturnSemanticDetector().detect(
            [alignment],
            repository_id="repository",
        )

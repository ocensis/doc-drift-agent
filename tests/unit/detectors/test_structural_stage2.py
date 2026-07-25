from __future__ import annotations

from drift_agent.detectors.structural import StructuralDetector
from drift_agent.domain.models import (
    Alignment,
    CodeFact,
    DocClaim,
    ParameterFact,
    SourceAnchor,
    SymbolIdentity,
)
from drift_agent.domain.values import (
    MISSING_ANNOTATION,
    MISSING_DEFAULT,
    MISSING_PARAMETER,
    MISSING_RETURN,
)
from drift_agent.normalization import normalize_expression


def _anchor(
    path: str,
    *,
    start: int = 10,
    text: str = "value",
    digest: str = "source-hash",
) -> SourceAnchor:
    return SourceAnchor(
        path=path,
        line=2,
        start_byte=start,
        end_byte=start + len(text.encode("utf-8")),
        exact_text=text,
        source_hash=digest,
    )


def _parameter(
    name: str,
    *,
    kind: str = "positional or keyword",
    annotation: str | None = "str",
    default: str | None = None,
    default_present: bool = False,
    position: int = 0,
) -> ParameterFact:
    return ParameterFact(
        name=name,
        kind=kind,
        annotation=annotation,
        default=default,
        required=not default_present,
        default_present=default_present,
        annotation_present=annotation is not None,
        position=position,
        anchor=_anchor(
            "src/demo/api.py",
            start=20 + position * 10,
            text=name,
            digest="code-hash",
        ),
    )


def _alignment(
    code_parameters: list[ParameterFact],
    claim_parameters: list[ParameterFact],
    *,
    code_return: str | None = "None",
    claim_return: str | None = "None",
) -> Alignment:
    fact = CodeFact(
        symbol_id="demo.api.publish",
        symbol_identity=SymbolIdentity(
            module="demo.api",
            name="publish",
            category="module_function",
        ),
        path="src/demo/api.py",
        line=1,
        signature="publish(...)",
        parameters=code_parameters,
        return_annotation=code_return,
        return_annotation_present=code_return is not None,
        source_hash="code-hash",
        signature_anchor=_anchor(
            "src/demo/api.py",
            start=0,
            text="def publish(...):",
            digest="code-hash",
        ),
    )
    claim = DocClaim(
        id="claim-publish",
        symbol_id=fact.symbol_id,
        signature="publish(...)",
        parameters=claim_parameters,
        return_annotation=claim_return,
        anchor=_anchor(
            "docs/api.md",
            start=100,
            text="def publish(...): ...",
            digest="doc-hash",
        ),
    )
    return Alignment(fact=fact, claim=claim)


def test_parameter_added_has_component_granularity_and_two_sided_evidence() -> None:
    message = _parameter("message", position=0)
    color = _parameter(
        "color",
        annotation="bool",
        default="True",
        default_present=True,
        position=1,
    )

    findings = StructuralDetector().detect(
        [_alignment([message, color], [message.model_copy(update={"anchor": None})])],
        repository_id="repository-a",
    )

    assert [finding.kind for finding in findings] == ["parameter_added"]
    finding = findings[0]
    assert finding.component_id == "color"
    assert finding.old_value == MISSING_PARAMETER
    assert finding.new_value == {
        "name": "color",
        "kind": "positional or keyword",
        "annotation": normalize_expression("bool"),
        "default": normalize_expression("True"),
    }
    assert finding.code_evidence.path == "src/demo/api.py"
    assert finding.code_evidence.start_byte == color.anchor.start_byte
    assert finding.doc_evidence.path == "docs/api.md"
    assert all(finding.kind != "signature_changed" for finding in findings)


def test_parameter_removed_identifies_only_the_removed_parameter() -> None:
    current = _parameter("message", position=0)
    documented = current.model_copy(update={"anchor": None})
    obsolete = _parameter(
        "legacy",
        annotation=None,
        position=1,
    ).model_copy(update={"anchor": None})

    findings = StructuralDetector().detect(
        [_alignment([current], [documented, obsolete])],
        repository_id="repository-a",
    )

    assert [finding.kind for finding in findings] == ["parameter_removed"]
    assert findings[0].component_id == "legacy"
    assert findings[0].old_value == {
        "name": "legacy",
        "kind": "positional or keyword",
        "annotation": MISSING_ANNOTATION,
        "default": MISSING_DEFAULT,
    }
    assert findings[0].new_value == MISSING_PARAMETER


def test_parameter_order_and_kind_changes_are_distinct_and_stably_sorted() -> None:
    code_first = _parameter("first", kind="positional-only", position=0)
    code_second = _parameter("second", kind="keyword-only", position=1)
    claim_second = code_second.model_copy(
        update={"kind": "positional or keyword", "position": 0, "anchor": None}
    )
    claim_first = code_first.model_copy(update={"position": 1, "anchor": None})

    findings = StructuralDetector().detect(
        [_alignment([code_first, code_second], [claim_second, claim_first])],
        repository_id="repository-a",
    )

    assert [finding.kind for finding in findings] == [
        "parameter_order_changed",
        "parameter_kind_changed",
    ]
    order, kind = findings
    assert order.component_id == "parameters"
    assert order.old_value == ["second", "first"]
    assert order.new_value == ["first", "second"]
    assert kind.component_id == "second"
    assert kind.old_value == "positional or keyword"
    assert kind.new_value == "keyword-only"
    assert kind.code_evidence.start_byte == code_second.anchor.start_byte


def test_annotation_default_and_requiredness_changes_do_not_overlap() -> None:
    code = [
        _parameter("annotated", annotation="bytes", position=0),
        _parameter(
            "defaulted",
            default="True",
            default_present=True,
            position=1,
        ),
        _parameter(
            "optional",
            default="None",
            default_present=True,
            position=2,
        ),
    ]
    claims = [
        _parameter("annotated", annotation="str", position=0).model_copy(update={"anchor": None}),
        _parameter(
            "defaulted",
            default="False",
            default_present=True,
            position=1,
        ).model_copy(update={"anchor": None}),
        _parameter("optional", position=2).model_copy(update={"anchor": None}),
    ]

    findings = StructuralDetector().detect(
        [_alignment(code, claims)],
        repository_id="repository-a",
    )

    assert [(finding.kind, finding.component_id) for finding in findings] == [
        ("parameter_annotation_changed", "annotated"),
        ("parameter_requiredness_changed", "optional"),
        ("parameter_default_changed", "defaulted"),
    ]
    by_kind = {finding.kind: finding for finding in findings}
    assert by_kind["parameter_annotation_changed"].old_value == normalize_expression("str")
    assert by_kind["parameter_annotation_changed"].new_value == normalize_expression("bytes")
    assert by_kind["parameter_default_changed"].old_value == normalize_expression("False")
    assert by_kind["parameter_default_changed"].new_value == normalize_expression("True")
    assert by_kind["parameter_requiredness_changed"].old_value == MISSING_DEFAULT
    assert by_kind["parameter_requiredness_changed"].new_value == normalize_expression("None")


def test_return_annotation_change_uses_claim_to_code_direction() -> None:
    findings = StructuralDetector().detect(
        [_alignment([], [], code_return="str", claim_return="bytes")],
        repository_id="repository-a",
    )

    assert [finding.kind for finding in findings] == ["return_annotation_changed"]
    finding = findings[0]
    assert finding.component_id == "return"
    assert finding.old_value == normalize_expression("bytes")
    assert finding.new_value == normalize_expression("str")


def test_empty_signature_and_missing_return_do_not_equal_literal_none() -> None:
    no_drift = StructuralDetector().detect(
        [_alignment([], [], code_return=None, claim_return=None)],
        repository_id="repository-a",
    )
    literal_none = StructuralDetector().detect(
        [_alignment([], [], code_return="None", claim_return=None)],
        repository_id="repository-a",
    )

    assert no_drift == []
    assert len(literal_none) == 1
    assert literal_none[0].kind == "return_annotation_changed"
    assert literal_none[0].old_value == MISSING_RETURN
    assert literal_none[0].new_value == normalize_expression("None")


def test_literal_none_default_is_presence_not_default_value_drift() -> None:
    code = _parameter(
        "value",
        annotation=None,
        default="None",
        default_present=True,
    )
    claim = _parameter("value", annotation=None).model_copy(update={"anchor": None})

    findings = StructuralDetector().detect(
        [_alignment([code], [claim])],
        repository_id="repository-a",
    )

    assert [finding.kind for finding in findings] == ["parameter_requiredness_changed"]
    assert findings[0].old_value == MISSING_DEFAULT
    assert findings[0].new_value == normalize_expression("None")


def test_expression_normalization_and_fingerprint_are_semantic_and_stable() -> None:
    code = _parameter(
        "limit",
        annotation="list[str]",
        default="1_000",
        default_present=True,
    )
    equivalent = _parameter(
        "limit",
        annotation="list [ str ]",
        default="1000",
        default_present=True,
    ).model_copy(update={"anchor": None})
    changed = equivalent.model_copy(update={"default": "1001"})
    detector = StructuralDetector()

    assert (
        detector.detect(
            [_alignment([code], [equivalent])],
            repository_id="repository-a",
        )
        == []
    )
    first = detector.detect(
        [_alignment([code], [changed])],
        repository_id="repository-a",
    )
    second = detector.detect(
        [_alignment([code], [changed])],
        repository_id="repository-a",
    )
    other_repository = detector.detect(
        [_alignment([code], [changed])],
        repository_id="repository-b",
    )

    assert len(first) == 1
    assert first[0].fingerprint == second[0].fingerprint
    assert first[0].id == second[0].id
    assert first[0].fingerprint != other_repository[0].fingerprint

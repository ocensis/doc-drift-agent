from __future__ import annotations

from collections import Counter
from typing import Any

from drift_agent.domain.enums import FindingDisposition, ValidationStatus
from drift_agent.domain.models import (
    Alignment,
    CodeFact,
    DocClaim,
    DriftFinding,
    EvidenceAnchor,
    ParameterFact,
    PatchAttempt,
    SourceAnchor,
    SymbolIdentity,
    ValidationResult,
)
from drift_agent.domain.values import (
    MISSING_ANNOTATION,
    MISSING_DEFAULT,
    MISSING_DOCSTRING_FIELD,
    MISSING_PARAMETER,
    MISSING_RETURN,
    MISSING_SYMBOL,
)
from drift_agent.normalization import (
    finding_fingerprint,
    finding_sort_key,
    normalize_expression,
    normalize_ts_type,
)


def _evidence(path: str, line: int, digest: str, anchor: SourceAnchor | None) -> EvidenceAnchor:
    return EvidenceAnchor(
        path=path,
        line=line,
        source_hash=digest,
        start_byte=0 if anchor is None else anchor.start_byte,
        end_byte=0 if anchor is None else anchor.end_byte,
    )


def _normalized(expression: str | None, missing: Any, *, language: str = "python") -> Any:
    if expression is None:
        return missing
    if language == "typescript":
        return {"type": "ts_type", "normalized": normalize_ts_type(expression)}
    try:
        return normalize_expression(expression)
    except SyntaxError:
        # Inputs obtained from Python AST normally cannot reach this branch.
        # Keeping a typed raw value makes compatibility-created facts fail
        # conservatively instead of executing or guessing the expression.
        return {"type": "unparsed_expression", "raw": expression}


def _parameter_value(parameter: ParameterFact, *, language: str = "python") -> dict[str, Any]:
    return {
        "name": parameter.name,
        "kind": parameter.kind,
        "annotation": _normalized(parameter.annotation, MISSING_ANNOTATION, language=language),
        "default": _normalized(parameter.default, MISSING_DEFAULT, language=language)
        if parameter.default_present
        else MISSING_DEFAULT,
    }


class StructuralDetector:
    id = "structural.signature"
    version = "2"

    def _finding(
        self,
        *,
        alignment: Alignment,
        kind: str,
        component_id: str,
        old_value: Any,
        new_value: Any,
        reason: str,
        repository_id: str,
        code_anchor: SourceAnchor | None = None,
        doc_anchor: SourceAnchor | None = None,
        reason_code: str = "detected",
    ) -> DriftFinding:
        fact = alignment.fact
        claim = alignment.claim
        code_evidence = _evidence(
            fact.path,
            fact.line,
            fact.source_hash,
            code_anchor or fact.signature_anchor,
        )
        selected_doc_anchor = doc_anchor or claim.value_anchor or claim.anchor
        doc_evidence = _evidence(
            selected_doc_anchor.path,
            selected_doc_anchor.line,
            selected_doc_anchor.source_hash,
            selected_doc_anchor,
        )
        identity = fact.symbol_identity or fact.symbol_id
        fingerprint = finding_fingerprint(
            repository_id=repository_id,
            symbol_identity=identity,
            kind=kind,
            component_id=component_id,
            old_value=old_value,
            new_value=new_value,
            code_evidence=code_evidence,
            doc_evidence=doc_evidence,
            detector_id=self.id,
            detector_version=self.version,
        )
        return DriftFinding(
            id=f"finding_{fingerprint}",
            symbol_id=fact.symbol_id,
            disposition=FindingDisposition.DETECTED,
            code_evidence=code_evidence,
            doc_evidence=doc_evidence,
            reason=reason,
            kind=kind,
            component_id=component_id,
            old_value=old_value,
            new_value=new_value,
            detector_id=self.id,
            detector_version=self.version,
            fingerprint=fingerprint,
            reason_code=reason_code,
        )

    def detect(
        self,
        alignments: list[Alignment],
        *,
        repository_id: str = "",
    ) -> list[DriftFinding]:
        findings: list[DriftFinding] = []
        for alignment in alignments:
            if alignment.claim.kind != "signature":
                continue
            fact = alignment.fact
            claim = alignment.claim
            code_counts = Counter(parameter.name for parameter in fact.parameters)
            doc_counts = Counter(parameter.name for parameter in claim.parameters)
            duplicate_names = sorted(
                name for name, count in (code_counts | doc_counts).items() if count > 1
            )
            if duplicate_names:
                findings.append(
                    self._finding(
                        alignment=alignment,
                        kind="unsupported",
                        component_id=duplicate_names[0],
                        old_value=duplicate_names,
                        new_value=duplicate_names,
                        reason="duplicate parameter facts prevent deterministic comparison",
                        repository_id=repository_id,
                        reason_code="ambiguity.parameter",
                    ).model_copy(
                        update={
                            "disposition": FindingDisposition.UNRESOLVED,
                            "truth_source": "unknown",
                        }
                    )
                )
                continue

            code_by_name = {parameter.name: parameter for parameter in fact.parameters}
            doc_by_name = {parameter.name: parameter for parameter in claim.parameters}
            for name in sorted(code_by_name.keys() - doc_by_name.keys()):
                parameter = code_by_name[name]
                findings.append(
                    self._finding(
                        alignment=alignment,
                        kind="parameter_added",
                        component_id=name,
                        old_value=MISSING_PARAMETER,
                        new_value=_parameter_value(parameter, language=fact.language),
                        reason=f"parameter {name!r} exists in code but not documentation",
                        repository_id=repository_id,
                        code_anchor=parameter.anchor,
                    )
                )
            for name in sorted(doc_by_name.keys() - code_by_name.keys()):
                parameter = doc_by_name[name]
                findings.append(
                    self._finding(
                        alignment=alignment,
                        kind="parameter_removed",
                        component_id=name,
                        old_value=_parameter_value(parameter, language=fact.language),
                        new_value=MISSING_PARAMETER,
                        reason=f"documented parameter {name!r} no longer exists in code",
                        repository_id=repository_id,
                    )
                )

            common = code_by_name.keys() & doc_by_name.keys()
            doc_order = [p.name for p in claim.parameters if p.name in common]
            code_order = [p.name for p in fact.parameters if p.name in common]
            if doc_order != code_order:
                findings.append(
                    self._finding(
                        alignment=alignment,
                        kind="parameter_order_changed",
                        component_id="parameters",
                        old_value=doc_order,
                        new_value=code_order,
                        reason=f"parameter order {doc_order!r} differs from code {code_order!r}",
                        repository_id=repository_id,
                    )
                )

            for name in sorted(common):
                code_parameter = code_by_name[name]
                doc_parameter = doc_by_name[name]
                if code_parameter.kind != doc_parameter.kind:
                    findings.append(
                        self._finding(
                            alignment=alignment,
                            kind="parameter_kind_changed",
                            component_id=name,
                            old_value=doc_parameter.kind,
                            new_value=code_parameter.kind,
                            reason=f"parameter {name!r} kind differs from code",
                            repository_id=repository_id,
                            code_anchor=code_parameter.anchor,
                        )
                    )
                old_annotation = _normalized(
                    doc_parameter.annotation,
                    MISSING_ANNOTATION,
                    language=fact.language,
                )
                new_annotation = _normalized(
                    code_parameter.annotation,
                    MISSING_ANNOTATION,
                    language=fact.language,
                )
                if old_annotation != new_annotation:
                    findings.append(
                        self._finding(
                            alignment=alignment,
                            kind="parameter_annotation_changed",
                            component_id=name,
                            old_value=old_annotation,
                            new_value=new_annotation,
                            reason=f"parameter {name!r} annotation differs from code",
                            repository_id=repository_id,
                            code_anchor=code_parameter.anchor,
                        )
                    )
                old_present = bool(doc_parameter.default_present)
                new_present = bool(code_parameter.default_present)
                old_default = (
                    _normalized(doc_parameter.default, MISSING_DEFAULT, language=fact.language)
                    if old_present
                    else MISSING_DEFAULT
                )
                new_default = (
                    _normalized(code_parameter.default, MISSING_DEFAULT, language=fact.language)
                    if new_present
                    else MISSING_DEFAULT
                )
                if old_present != new_present:
                    findings.append(
                        self._finding(
                            alignment=alignment,
                            kind="parameter_requiredness_changed",
                            component_id=name,
                            old_value=old_default,
                            new_value=new_default,
                            reason=f"parameter {name!r} requiredness differs from code",
                            repository_id=repository_id,
                            code_anchor=code_parameter.anchor,
                        )
                    )
                elif old_present and old_default != new_default:
                    findings.append(
                        self._finding(
                            alignment=alignment,
                            kind="parameter_default_changed",
                            component_id=name,
                            old_value=old_default,
                            new_value=new_default,
                            reason=f"parameter {name!r} default differs from code",
                            repository_id=repository_id,
                            code_anchor=code_parameter.anchor,
                        )
                    )

            old_return = _normalized(
                claim.return_annotation, MISSING_RETURN, language=fact.language
            )
            new_return = _normalized(fact.return_annotation, MISSING_RETURN, language=fact.language)
            if old_return != new_return:
                findings.append(
                    self._finding(
                        alignment=alignment,
                        kind="return_annotation_changed",
                        component_id="return",
                        old_value=old_return,
                        new_value=new_return,
                        reason="document return annotation differs from code",
                        repository_id=repository_id,
                    )
                )
            if (
                alignment.method == "exact_fqn"
                and not any(finding.symbol_id == fact.symbol_id for finding in findings)
                and fact.signature != claim.signature
            ):
                findings.append(
                    self._finding(
                        alignment=alignment,
                        kind="unsupported",
                        component_id="signature",
                        old_value=claim.signature,
                        new_value=fact.signature,
                        reason="signature text differs outside supported components",
                        repository_id=repository_id,
                        reason_code="unsupported.markdown_claim",
                    ).model_copy(
                        update={
                            "disposition": FindingDisposition.UNRESOLVED,
                            "truth_source": "unknown",
                        }
                    )
                )
        return sorted(findings, key=finding_sort_key)

    def detect_docstrings(
        self,
        facts: list[CodeFact],
        claims: list[DocClaim],
        *,
        repository_id: str = "",
    ) -> list[DriftFinding]:
        facts_by_symbol = {fact.symbol_id: fact for fact in facts}
        findings: list[DriftFinding] = []
        grouped: dict[str, list[DocClaim]] = {}
        for claim in claims:
            if claim.kind in {"docstring_parameter", "docstring_return"}:
                grouped.setdefault(claim.symbol_id, []).append(claim)
        for symbol_id, symbol_claims in sorted(grouped.items()):
            fact = facts_by_symbol.get(symbol_id)
            if fact is None:
                continue
            parameter_claims = {
                claim.component_id: claim
                for claim in symbol_claims
                if claim.kind == "docstring_parameter"
            }
            parameters = list(fact.parameters)
            if (
                fact.language == "python"
                and fact.category == "method"
                and fact.decorator_kind != "staticmethod"
                and parameters
            ):
                parameters = parameters[1:]
            code_parameters = {parameter.name: parameter for parameter in parameters}
            template_claim = symbol_claims[0]

            for name in sorted(parameter_claims.keys() | code_parameters.keys()):
                parameter_claim = parameter_claims.get(name)
                parameter = code_parameters.get(name)
                if parameter_claim is None:
                    assert parameter is not None
                    synthetic = template_claim.model_copy(
                        update={"component_id": name, "value_anchor": None}
                    )
                    alignment = Alignment(fact=fact, claim=synthetic)
                    finding = self._finding(
                        alignment=alignment,
                        kind="docstring_parameter_changed",
                        component_id=name,
                        old_value=MISSING_DOCSTRING_FIELD,
                        new_value={"name": name},
                        reason=f"Google Args field for {name!r} is missing",
                        repository_id=repository_id,
                        reason_code="unsupported.literal",
                    ).model_copy(
                        update={
                            "disposition": FindingDisposition.UNRESOLVED,
                            "truth_source": "unknown",
                        }
                    )
                    findings.append(finding)
                    continue
                alignment = Alignment(fact=fact, claim=parameter_claim)
                if parameter_claim.extraction_error is not None:
                    findings.append(
                        self._finding(
                            alignment=alignment,
                            kind="docstring_parameter_changed",
                            component_id=name,
                            old_value=MISSING_DOCSTRING_FIELD,
                            new_value={"name": name},
                            reason=f"Google Args field for {name!r} is missing",
                            repository_id=repository_id,
                            reason_code=parameter_claim.extraction_error,
                        ).model_copy(
                            update={
                                "disposition": FindingDisposition.UNRESOLVED,
                                "truth_source": "unknown",
                            }
                        )
                    )
                    continue
                if parameter is None:
                    findings.append(
                        self._finding(
                            alignment=alignment,
                            kind="docstring_parameter_changed",
                            component_id=name,
                            old_value=parameter_claim.normalized_value,
                            new_value=MISSING_PARAMETER,
                            reason=f"Google Args field {name!r} is stale",
                            repository_id=repository_id,
                        )
                    )
                    continue
                claimed_annotation = parameter_claim.normalized_value
                if claimed_annotation is None:
                    continue
                code_annotation = _normalized(parameter.annotation, MISSING_ANNOTATION)
                if claimed_annotation != code_annotation:
                    findings.append(
                        self._finding(
                            alignment=alignment,
                            kind="docstring_parameter_changed",
                            component_id=name,
                            old_value=claimed_annotation,
                            new_value=code_annotation,
                            reason=f"Google Args annotation for {name!r} differs from code",
                            repository_id=repository_id,
                            code_anchor=parameter.anchor,
                        )
                    )

            return_claims = [claim for claim in symbol_claims if claim.kind == "docstring_return"]
            return_claim = return_claims[0] if return_claims else None
            code_return = _normalized(fact.return_annotation, MISSING_RETURN)
            if return_claim is not None and return_claim.extraction_error is not None:
                findings.append(
                    self._finding(
                        alignment=Alignment(fact=fact, claim=return_claim),
                        kind="docstring_return_changed",
                        component_id="return",
                        old_value=MISSING_DOCSTRING_FIELD,
                        new_value=code_return,
                        reason="Google Returns field is missing",
                        repository_id=repository_id,
                        reason_code=return_claim.extraction_error,
                    ).model_copy(
                        update={
                            "disposition": FindingDisposition.UNRESOLVED,
                            "truth_source": "unknown",
                        }
                    )
                )
            elif return_claim is not None and return_claim.normalized_value != code_return:
                findings.append(
                    self._finding(
                        alignment=Alignment(fact=fact, claim=return_claim),
                        kind="docstring_return_changed",
                        component_id="return",
                        old_value=return_claim.normalized_value,
                        new_value=code_return,
                        reason="Google Returns annotation differs from code",
                        repository_id=repository_id,
                    )
                )
        return sorted(findings, key=finding_sort_key)

    def detect_symbols(
        self,
        *,
        current_facts: list[CodeFact],
        before_facts: list[CodeFact],
        claims: list[DocClaim],
        rename_map: dict[str, str] | None = None,
        manual_aliases: set[str] | None = None,
        repository_id: str = "",
    ) -> tuple[list[DriftFinding], list[Alignment]]:
        current_by_symbol: dict[str, list[CodeFact]] = {}
        before_by_symbol: dict[str, list[CodeFact]] = {}
        for fact in current_facts:
            current_by_symbol.setdefault(fact.symbol_id, []).append(fact)
        for fact in before_facts:
            before_by_symbol.setdefault(fact.symbol_id, []).append(fact)
        renamed = rename_map or {}
        aliased = manual_aliases or set()
        findings: list[DriftFinding] = []
        alignments: list[Alignment] = []
        signature_claim_counts = Counter(
            claim.symbol_id for claim in claims if claim.kind == "signature"
        )
        for claim in claims:
            if claim.kind not in {"signature", "symbol_reference"}:
                continue
            if claim.kind == "signature" and signature_claim_counts[claim.symbol_id] != 1:
                continue
            current = current_by_symbol.get(claim.symbol_id, [])
            if len(current) == 1:
                alignments.append(Alignment(fact=current[0], claim=claim))
                continue
            new_symbol = renamed.get(claim.symbol_id)
            renamed_facts = current_by_symbol.get(new_symbol, []) if new_symbol else []
            if new_symbol is not None and len(renamed_facts) == 1:
                alignment = Alignment(
                    fact=renamed_facts[0],
                    claim=claim,
                    method=("manual_alias" if claim.symbol_id in aliased else "git_rename"),
                    old_symbol_id=claim.symbol_id,
                )
                alignments.append(alignment)
                findings.append(
                    self._finding(
                        alignment=alignment,
                        kind="symbol_reference_renamed",
                        component_id="symbol",
                        old_value=claim.symbol_id,
                        new_value=new_symbol,
                        reason=f"document references renamed symbol {claim.symbol_id!r}",
                        repository_id=repository_id,
                    )
                )
                continue
            before = before_by_symbol.get(claim.symbol_id, [])
            if len(before) == 1 and not current:
                alignment = Alignment(fact=before[0], claim=claim)
                alignments.append(alignment)
                finding = self._finding(
                    alignment=alignment,
                    kind="symbol_reference_deleted",
                    component_id="symbol",
                    old_value=claim.symbol_id,
                    new_value=MISSING_SYMBOL,
                    reason=f"document references deleted symbol {claim.symbol_id!r}",
                    repository_id=repository_id,
                )
                if not claim.complete_declaration:
                    finding = finding.model_copy(
                        update={
                            "disposition": FindingDisposition.UNRESOLVED,
                            "truth_source": "unknown",
                            "reason_code": "unsupported.incomplete_declaration",
                            "reason": (
                                finding.reason + "; reference is not a complete declaration"
                            ),
                        }
                    )
                findings.append(finding)
                continue
            if not before and not current:
                parts = claim.symbol_id.split(".")
                identity = SymbolIdentity(
                    module=".".join(parts[:-1]),
                    name=parts[-1],
                    category="module_function",
                )
                doc_anchor = claim.value_anchor or claim.anchor
                code_evidence = EvidenceAnchor(
                    path=doc_anchor.path,
                    line=doc_anchor.line,
                    source_hash=doc_anchor.source_hash,
                    start_byte=0,
                    end_byte=0,
                )
                doc_evidence = _evidence(
                    doc_anchor.path,
                    doc_anchor.line,
                    doc_anchor.source_hash,
                    doc_anchor,
                )
                old_value = claim.symbol_id
                fingerprint = finding_fingerprint(
                    repository_id=repository_id,
                    symbol_identity=identity,
                    kind="unsupported",
                    component_id="symbol",
                    old_value=old_value,
                    new_value=MISSING_SYMBOL,
                    code_evidence=code_evidence,
                    doc_evidence=doc_evidence,
                    detector_id=self.id,
                    detector_version=self.version,
                )
                findings.append(
                    DriftFinding(
                        id=f"finding_{fingerprint}",
                        symbol_id=claim.symbol_id,
                        disposition=FindingDisposition.UNRESOLVED,
                        truth_source="unknown",
                        code_evidence=code_evidence,
                        doc_evidence=doc_evidence,
                        reason="document declaration has no deterministic code symbol",
                        kind="unsupported",
                        component_id="symbol",
                        old_value=old_value,
                        new_value=MISSING_SYMBOL,
                        detector_id=self.id,
                        detector_version=self.version,
                        fingerprint=fingerprint,
                        reason_code="unsupported.markdown_claim",
                    )
                )
        return sorted(findings, key=finding_sort_key), alignments

    def validate(
        self,
        alignments: list[Alignment],
        attempt: PatchAttempt,
    ) -> ValidationResult:
        remaining = self.detect(alignments)
        attempted_symbols = {alignment.fact.symbol_id for alignment in alignments}
        scoped = [finding for finding in remaining if finding.symbol_id in attempted_symbols]
        status = ValidationStatus.PASSED if not scoped else ValidationStatus.FAILED
        return ValidationResult(
            finding_ids=attempt.finding_ids,
            attempt_id=attempt.id,
            check="drift_redetect",
            required=True,
            status=status,
            summary="signature drift removed" if not scoped else "signature drift remains",
        )

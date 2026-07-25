from __future__ import annotations

from drift_agent.domain.enums import FindingDisposition
from drift_agent.domain.models import (
    Alignment,
    DriftFinding,
    EvidenceAnchor,
    SemanticReturnAssertion,
)
from drift_agent.normalization import finding_sort_key, semantic_finding_fingerprint


class ConstantReturnSemanticDetector:
    """Compare exact prose assertions with definite local constant returns."""

    id = "semantic.constant_return"
    version = "1"

    def detect(
        self,
        alignments: list[Alignment],
        *,
        repository_id: str,
    ) -> list[DriftFinding]:
        findings: list[DriftFinding] = []
        for alignment in alignments:
            claim = alignment.claim
            semantic_facts = [
                fact
                for fact in alignment.fact.semantic_facts
                if fact.component_id == claim.component_id
            ]
            if (
                alignment.method != "exact_fqn"
                or claim.kind != "semantic_assertion"
                or len(semantic_facts) != 1
            ):
                raise ValueError("semantic detector requires one eligible alignment")
            assertion = SemanticReturnAssertion.model_validate(claim.normalized_value)
            mode = assertion.mode
            claimed = assertion.value
            semantic_fact = semantic_facts[0]
            if claimed == semantic_fact.normalized_value:
                continue

            kind = "semantic_over_promise" if mode == "always" else "semantic_direct_mismatch"
            reason_code = (
                "semantic.over_promise" if mode == "always" else "semantic.direct_mismatch"
            )
            code_anchor = semantic_fact.anchor
            code_evidence = EvidenceAnchor(
                path=code_anchor.path,
                line=code_anchor.line,
                source_hash=code_anchor.source_hash,
                start_byte=code_anchor.start_byte,
                end_byte=code_anchor.end_byte,
            )
            doc_anchor = claim.anchor
            doc_evidence = EvidenceAnchor(
                path=doc_anchor.path,
                line=doc_anchor.line,
                source_hash=doc_anchor.source_hash,
                start_byte=doc_anchor.start_byte,
                end_byte=doc_anchor.end_byte,
            )
            old_value = assertion.model_dump(mode="json")
            new_value = {
                "predicate": semantic_fact.predicate,
                "value": semantic_fact.normalized_value.model_dump(mode="json"),
            }
            fingerprint = semantic_finding_fingerprint(
                repository_id=repository_id,
                symbol_identity=(alignment.fact.symbol_identity or alignment.fact.symbol_id),
                kind=kind,
                component_id=claim.component_id,
                old_value=old_value,
                new_value=new_value,
                code_evidence=code_evidence,
                doc_evidence=doc_evidence,
                detector_id=self.id,
                detector_version=self.version,
            )
            findings.append(
                DriftFinding(
                    id=f"finding_{fingerprint}",
                    symbol_id=alignment.fact.symbol_id,
                    type="semantic_drift",
                    disposition=FindingDisposition.DETECTED,
                    truth_source="code",
                    code_evidence=code_evidence,
                    doc_evidence=doc_evidence,
                    reason=(
                        f"documented constant return {claimed.model_dump(mode='json')} "
                        "differs from definite code return "
                        f"{semantic_fact.normalized_value.model_dump(mode='json')}"
                    ),
                    kind=kind,
                    component_id=claim.component_id,
                    old_value=old_value,
                    new_value=new_value,
                    detector_id=self.id,
                    detector_version=self.version,
                    fingerprint=fingerprint,
                    reason_code=reason_code,
                    symbol_identity=alignment.fact.symbol_identity,
                )
            )
        return sorted(findings, key=finding_sort_key)


__all__ = ["ConstantReturnSemanticDetector"]

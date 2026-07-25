from __future__ import annotations

from drift_agent.domain.enums import FindingDisposition, ValidationStatus
from drift_agent.domain.models import DriftFinding, EvidenceAnchor, ValidationResult
from drift_agent.normalization import finding_fingerprint
from drift_agent.providers.executable_examples import ExecutableExample


class ExecutableExampleDetector:
    """Classify a real command failure as a stable broken-example finding."""

    id = "executable.example"
    version = "1"

    def detect(
        self,
        example: ExecutableExample,
        result: ValidationResult,
        *,
        repository_id: str,
    ) -> DriftFinding | None:
        if result.status is not ValidationStatus.FAILED:
            return None
        code_evidence = EvidenceAnchor(
            path="drift-agent.toml",
            line=1,
            source_hash=example.config_hash,
        )
        doc_evidence = EvidenceAnchor(
            path=example.target,
            line=1,
            source_hash=example.target_hash,
        )
        symbol_id = f"validation::{example.id}"
        old_value = {
            "kind": example.kind,
            "arguments": list(example.normalized_arguments),
            "expected_status": "passed",
        }
        new_value = {"status": "failed"}
        fingerprint = finding_fingerprint(
            repository_id=repository_id,
            symbol_identity=symbol_id,
            kind="broken_example",
            component_id=example.component_id,
            old_value=old_value,
            new_value=new_value,
            code_evidence=code_evidence,
            doc_evidence=doc_evidence,
            detector_id=self.id,
            detector_version=self.version,
        )
        return DriftFinding(
            id=f"finding_{fingerprint}",
            symbol_id=symbol_id,
            disposition=FindingDisposition.DETECTED,
            truth_source="unknown",
            code_evidence=code_evidence,
            doc_evidence=doc_evidence,
            reason=f"configured {example.kind} validation failed for {example.target}",
            kind="broken_example",
            component_id=example.component_id,
            old_value=old_value,
            new_value=new_value,
            detector_id=self.id,
            detector_version=self.version,
            fingerprint=fingerprint,
            reason_code="validation_failed",
        )


__all__ = ["ExecutableExampleDetector"]

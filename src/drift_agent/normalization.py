from __future__ import annotations

import ast
import hashlib
import json
import re
from enum import Enum
from typing import Any

from pydantic import BaseModel

from drift_agent.domain.models import DriftFinding, EvidenceAnchor, SymbolIdentity

_TS_TOKEN = re.compile(
    r"\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|`(?:[^`\\]|\\.)*`|=>|[A-Za-z0-9_$.]+|\S"
)
_TS_WORD = re.compile(r"[A-Za-z0-9_$.]")

KIND_RANK: dict[str, int] = {
    "parameter_added": 0,
    "parameter_removed": 1,
    "parameter_order_changed": 2,
    "parameter_kind_changed": 3,
    "parameter_annotation_changed": 4,
    "parameter_requiredness_changed": 5,
    "parameter_default_changed": 6,
    "return_annotation_changed": 7,
    "symbol_reference_deleted": 8,
    "symbol_reference_renamed": 9,
    "docstring_parameter_changed": 10,
    "docstring_return_changed": 11,
    "unsupported": 12,
    "config_key_removed": 17,
    "config_key_undocumented": 18,
    "signature_changed": 13,
    "broken_example": 14,
    "semantic_direct_mismatch": 15,
    "semantic_over_promise": 16,
    "semantic_section_drift": 19,
}


def normalize_expression(expression: str) -> str:
    """Normalize a Python expression without importing or evaluating it."""

    node = ast.parse(expression, mode="eval").body
    return ast.dump(node, include_attributes=False)


def normalize_ts_type(expression: str) -> str:
    """Canonicalize a TypeScript type/default expression for equality comparison.

    Token-level whitespace canonicalization: comments are not expected inside
    annotation text (providers extract annotation node text without trivia), so
    collapsing whitespace between punctuation and identifiers is sufficient for
    a deterministic comparison key without importing a TS parser here.
    """

    out: list[str] = []
    previous_wordlike = False
    for token in _TS_TOKEN.findall(expression):
        wordlike = bool(_TS_WORD.match(token))
        if wordlike and previous_wordlike:
            out.append(" ")
        out.append(token)
        previous_wordlike = wordlike
    return "".join(out)


def normalize_optional_expression(expression: str | None, missing: Any) -> Any:
    return missing if expression is None else normalize_expression(expression)


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _jsonable(value.model_dump(mode="json"))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _evidence_material(anchor: EvidenceAnchor) -> dict[str, Any]:
    return {
        "path": anchor.path,
        "start": anchor.start_byte,
        "end": anchor.end_byte,
        "hash": anchor.source_hash,
    }


def finding_fingerprint(
    *,
    repository_id: str,
    symbol_identity: SymbolIdentity | str,
    kind: str,
    component_id: str,
    old_value: Any,
    new_value: Any,
    code_evidence: EvidenceAnchor,
    doc_evidence: EvidenceAnchor,
    detector_id: str,
    detector_version: str,
) -> str:
    material = {
        "schema": "finding-v1",
        "repository_id": repository_id,
        "symbol_identity": symbol_identity,
        "legacy_type": "signature_drift",
        "kind": kind,
        "component": component_id,
        "old_value": old_value,
        "new_value": new_value,
        "code_evidence": _evidence_material(code_evidence),
        "doc_evidence": _evidence_material(doc_evidence),
        "detector_id": detector_id,
        "detector_version": detector_version,
    }
    return hashlib.sha256(canonical_json_bytes(material)).hexdigest()


def semantic_finding_fingerprint(
    *,
    repository_id: str,
    symbol_identity: SymbolIdentity | str,
    kind: str,
    component_id: str,
    old_value: Any,
    new_value: Any,
    code_evidence: EvidenceAnchor,
    doc_evidence: EvidenceAnchor,
    detector_id: str,
    detector_version: str,
) -> str:
    """Fingerprint V3 semantic evidence without changing frozen legacy IDs."""

    material = {
        "schema": "semantic-finding-v1",
        "finding_type": "semantic_drift",
        "repository_id": repository_id,
        "symbol_identity": symbol_identity,
        "kind": kind,
        "component": component_id,
        "old_value": old_value,
        "new_value": new_value,
        "code_evidence": _evidence_material(code_evidence),
        "doc_evidence": _evidence_material(doc_evidence),
        "detector_id": detector_id,
        "detector_version": detector_version,
    }
    return hashlib.sha256(canonical_json_bytes(material)).hexdigest()


def finding_sort_key(finding: DriftFinding) -> tuple[Any, ...]:
    return (
        finding.symbol_id,
        KIND_RANK.get(finding.kind, 10_000),
        finding.component_id,
        finding.code_evidence.path,
        finding.code_evidence.start_byte,
        finding.doc_evidence.path,
        finding.doc_evidence.start_byte,
        finding.detector_id,
        finding.detector_version,
        finding.fingerprint,
    )

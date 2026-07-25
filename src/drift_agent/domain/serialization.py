from __future__ import annotations

import json
from typing import Any, Literal

from drift_agent.domain.models import VerifiedRepairBundle

WireVersion = Literal[1, 2, 3]

_FINDING_V1 = {
    "id",
    "symbol_id",
    "type",
    "disposition",
    "truth_source",
    "code_evidence",
    "doc_evidence",
    "reason",
}
_EVIDENCE_V1 = {"path", "line", "source_hash"}
_BUNDLE_ADDITIVE = {
    "repository_id",
    "workspace_id",
    "suppressed_findings",
    "memory_events",
    "repair_groups",
    "residual_changes",
}


class UnsupportedWireVersionError(ValueError):
    """Raised when a bundle cannot be represented by the requested schema."""


def bundle_to_wire(
    bundle: VerifiedRepairBundle,
    version: WireVersion = 1,
) -> dict[str, Any]:
    """Return the explicit wire projection.

    Internal models intentionally carry both contracts.  This function, not
    Pydantic's default-value exclusion, is the compatibility boundary.
    """

    if type(version) is not int or version not in {1, 2, 3}:
        raise UnsupportedWireVersionError(f"unsupported output version: {version!r}")
    if version in {1, 2} and (
        bundle.semantic_analysis
        or any(finding.type == "semantic_drift" for finding in bundle.findings)
    ):
        raise UnsupportedWireVersionError(
            f"schema V{version} cannot represent semantic analysis; request schema V3"
        )

    data = bundle.model_dump(mode="json")
    if version == 3:
        # Bounding is an adapter-layer concern; the domain projection is always
        # complete, so the bounding fields hold their unbounded defaults here.
        return {"schema_version": 3, **data, "omitted_findings": 0, "findings_summary": None}
    if version == 2:
        return {"schema_version": 2, **data}

    for key in _BUNDLE_ADDITIVE:
        data.pop(key, None)
    projected_findings: list[dict[str, Any]] = []
    for finding in data["findings"]:
        legacy = {key: finding[key] for key in _FINDING_V1}
        for evidence_key in ("code_evidence", "doc_evidence"):
            evidence = legacy[evidence_key]
            legacy[evidence_key] = {key: evidence[key] for key in _EVIDENCE_V1}
        projected_findings.append(legacy)
    data["findings"] = projected_findings
    return data


def bundle_to_json(
    bundle: VerifiedRepairBundle,
    version: WireVersion = 1,
) -> str:
    return json.dumps(
        bundle_to_wire(bundle, version),
        ensure_ascii=False,
        separators=(",", ":"),
    )


__all__ = [
    "UnsupportedWireVersionError",
    "WireVersion",
    "bundle_to_json",
    "bundle_to_wire",
]

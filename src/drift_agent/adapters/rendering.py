from __future__ import annotations

import html
import re
from collections import Counter
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

from drift_agent.adapters.contracts import PublicBundleV3, PublicDriftFinding
from drift_agent.domain.enums import FindingDisposition, RunStatus, ValidationStatus
from drift_agent.domain.status import exit_code_for_status

_MAX_SUMMARY_FINDINGS = 50
_MAX_CELL_CHARACTERS = 320
_RULE_CHARACTER = re.compile(r"[^A-Za-z0-9_.-]+")


def _markdown_cell(value: object) -> str:
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    if len(text) > _MAX_CELL_CHARACTERS:
        text = f"{text[: _MAX_CELL_CHARACTERS - 1]}…"
    return (
        html.escape(text, quote=False)
        .replace("@", "&#64;")
        .replace("`", "&#96;")
        .replace("|", "&#124;")
        .replace("\n", "<br>")
    )


def render_markdown_summary(
    bundle: PublicBundleV3,
    *,
    revision: str | None = None,
) -> str:
    findings = bundle.findings[:_MAX_SUMMARY_FINDINGS]
    dispositions = Counter(finding.disposition.value for finding in bundle.findings)
    disposition_summary = (
        ", ".join(
            f"{disposition.value}={dispositions[disposition.value]}"
            for disposition in FindingDisposition
            if dispositions[disposition.value]
        )
        or "none"
    )
    validation_counts = Counter(result.status.value for result in bundle.validation)
    validation_summary = (
        ", ".join(
            f"{status.value}={validation_counts[status.value]}"
            for status in ValidationStatus
            if validation_counts[status.value]
        )
        or "none"
    )
    lines = [
        "<!-- drift-agent:stage4 -->",
        "## Doc-Code Drift check",
        "",
        f"Status: `{bundle.status.value}`  ",
        f"Run: `{_markdown_cell(bundle.run_id)}`  ",
        f"Baseline revision: `{_markdown_cell(revision or 'not recorded')}`  ",
        f"Changed scope: {len(bundle.scope)} file(s)  ",
        f"Findings: {len(bundle.findings)}  ",
        f"Disposition counts: {_markdown_cell(disposition_summary)}  ",
        f"Validation results: {_markdown_cell(validation_summary)}",
        "",
    ]
    if findings:
        lines.extend(
            [
                "| Disposition | Kind | Symbol | Documentation evidence | Reason |",
                "|---|---|---|---|---|",
            ]
        )
        lines.extend(
            "| "
            + " | ".join(
                (
                    _markdown_cell(finding.disposition.value),
                    _markdown_cell(finding.kind),
                    _markdown_cell(finding.symbol_id),
                    _markdown_cell(f"{finding.doc_evidence.path}:{finding.doc_evidence.line}"),
                    _markdown_cell(finding.reason),
                )
            )
            + " |"
            for finding in findings
        )
        omitted = len(bundle.findings) - len(findings)
        if omitted:
            lines.extend(("", f"{omitted} additional finding(s) omitted from this summary."))
        lines.append("")
    incomplete_validation = [
        result
        for result in bundle.validation
        if result.required
        and result.status in {ValidationStatus.FAILED, ValidationStatus.UNAVAILABLE}
    ]
    if incomplete_validation:
        lines.extend(("### Incomplete required validation", ""))
        lines.extend(
            f"- `{_markdown_cell(result.check)}`: "
            f"{_markdown_cell(result.status.value)} — {_markdown_cell(result.summary)}"
            for result in incomplete_validation[:20]
        )
        omitted = len(incomplete_validation) - min(len(incomplete_validation), 20)
        if omitted:
            lines.append(f"- {omitted} additional validation result(s) omitted.")
        lines.append("")
    lines.extend(
        (
            "### Usage",
            "",
            f"- Model calls: {bundle.usage.model_calls}",
            f"- Validation commands: {bundle.usage.validation_commands}",
            f"- Tool calls: {bundle.usage.tool_calls}",
            f"- Input tokens: {bundle.usage.input_tokens}",
            f"- Output tokens: {bundle.usage.output_tokens}",
            f"- Known estimated cost: ${bundle.usage.estimated_cost_usd:.8f}",
            f"- Duration: {bundle.usage.duration_ms} ms",
            "- Accounting completeness: limited to fields published by V3 bundle",
            "",
        )
    )
    return "\n".join(lines)


def _rule_id(finding: PublicDriftFinding) -> str:
    family = _RULE_CHARACTER.sub("_", finding.kind).strip("_") or "drift"
    return f"drift-agent.{family}"


def _sarif_uri(path: str) -> str:
    return quote(path, safe="/._-")


def _level(finding: PublicDriftFinding) -> str:
    if finding.disposition is FindingDisposition.FIXED:
        return "note"
    if finding.disposition is FindingDisposition.DETECTED:
        return "warning"
    return "error"


def _location(path: str, line: int) -> dict[str, Any]:
    return {
        "physicalLocation": {
            "artifactLocation": {"uri": _sarif_uri(path)},
            "region": {"startLine": max(1, line)},
        }
    }


def _rules(bundle: PublicBundleV3) -> tuple[list[dict[str, Any]], Mapping[str, int]]:
    representatives: dict[str, PublicDriftFinding] = {}
    for finding in bundle.findings:
        representatives.setdefault(_rule_id(finding), finding)
    rule_ids = sorted(representatives)
    rules = [
        {
            "id": rule_id,
            "name": representatives[rule_id].kind,
            "shortDescription": {"text": f"Documentation drift: {representatives[rule_id].kind}"},
            "properties": {
                "findingType": representatives[rule_id].type,
                "detectorId": representatives[rule_id].detector_id,
                "detectorVersion": representatives[rule_id].detector_version,
            },
        }
        for rule_id in rule_ids
    ]
    return rules, {rule_id: index for index, rule_id in enumerate(rule_ids)}


def bundle_to_sarif(bundle: PublicBundleV3) -> dict[str, Any]:
    rules, rule_indexes = _rules(bundle)
    results = []
    for finding in bundle.findings:
        rule_id = _rule_id(finding)
        fingerprint = finding.fingerprint or finding.id
        results.append(
            {
                "ruleId": rule_id,
                "ruleIndex": rule_indexes[rule_id],
                "level": _level(finding),
                "message": {"text": finding.reason},
                "locations": [_location(finding.doc_evidence.path, finding.doc_evidence.line)],
                "relatedLocations": [
                    {
                        "id": 1,
                        "message": {"text": "Code evidence"},
                        **_location(
                            finding.code_evidence.path,
                            finding.code_evidence.line,
                        ),
                    }
                ],
                "partialFingerprints": {"driftAgentFinding/v1": fingerprint},
                "properties": {
                    "disposition": finding.disposition.value,
                    "reasonCode": finding.reason_code,
                    "symbolId": finding.symbol_id,
                },
            }
        )
    notifications = [
        {
            "level": "error",
            "message": {"text": result.summary},
            "descriptor": {"id": result.check},
        }
        for result in bundle.validation
        if result.required
        and result.status in {ValidationStatus.FAILED, ValidationStatus.UNAVAILABLE}
    ]
    exit_code = exit_code_for_status(bundle.status)
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "doc-drift-agent",
                        "semanticVersion": "0.1.0",
                        "rules": rules,
                    }
                },
                "invocations": [
                    {
                        "executionSuccessful": bundle.status is not RunStatus.FAILED,
                        "exitCode": exit_code,
                        "toolExecutionNotifications": notifications,
                    }
                ],
                "results": results,
            }
        ],
    }


__all__ = ["bundle_to_sarif", "render_markdown_summary"]

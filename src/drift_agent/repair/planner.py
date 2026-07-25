from __future__ import annotations

import hashlib
from collections import defaultdict

from pydantic import BaseModel, ConfigDict, Field

from drift_agent.domain.models import DriftFinding, PatchAttempt
from drift_agent.normalization import canonical_json_bytes


class RepairPlanModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RepairGroup(RepairPlanModel):
    id: str
    finding_ids: list[str]
    attempts: list[PatchAttempt]
    conflict_key: str = ""

    @property
    def path(self) -> str:
        return self.attempts[0].path if self.attempts else ""

    @property
    def start_byte(self) -> int:
        return min((attempt.start_byte for attempt in self.attempts), default=0)

    @property
    def end_byte(self) -> int:
        return max((attempt.end_byte for attempt in self.attempts), default=0)


class RepairPlan(RepairPlanModel):
    groups: list[RepairGroup] = Field(default_factory=list)


def _group_id(findings: list[DriftFinding], attempts: list[PatchAttempt]) -> str:
    material = {
        "findings": sorted(finding.fingerprint or finding.id for finding in findings),
        "anchors": sorted(
            (
                attempt.path,
                attempt.start_byte,
                attempt.end_byte,
                attempt.source_hash,
                attempt.expected_text,
                attempt.replacement_text,
            )
            for attempt in attempts
        ),
    }
    return "group_" + hashlib.sha256(canonical_json_bytes(material)).hexdigest()


def _overlap(left: PatchAttempt, right: PatchAttempt) -> bool:
    if left.path != right.path:
        return False
    if left.start_byte == left.end_byte == right.start_byte == right.end_byte:
        return True
    return left.start_byte < right.end_byte and right.start_byte < left.end_byte


def _group_findings(
    group: RepairGroup,
    findings_by_id: dict[str, DriftFinding],
) -> list[DriftFinding]:
    return [findings_by_id[finding_id] for finding_id in group.finding_ids]


def _validation_dependency(
    left: RepairGroup,
    right: RepairGroup,
    findings_by_id: dict[str, DriftFinding],
) -> bool:
    """Detect an actual cross-file validation read/write dependency."""

    if left.path == right.path:
        return False
    left_findings = _group_findings(left, findings_by_id)
    right_findings = _group_findings(right, findings_by_id)
    left_writes = {attempt.path for attempt in left.attempts}
    right_writes = {attempt.path for attempt in right.attempts}
    left_reads = {
        path
        for finding in left_findings
        for path in (finding.code_evidence.path, finding.doc_evidence.path)
    }
    right_reads = {
        path
        for finding in right_findings
        for path in (finding.code_evidence.path, finding.doc_evidence.path)
    }
    return bool((left_writes & right_reads) or (right_writes & left_reads))


class RepairPlanner:
    def plan(
        self,
        findings: list[DriftFinding],
        attempts: list[PatchAttempt],
    ) -> RepairPlan:
        findings_by_id = {finding.id: finding for finding in findings}
        by_replacement: dict[tuple[object, ...], list[PatchAttempt]] = defaultdict(list)
        for attempt in attempts:
            key = (
                attempt.path,
                attempt.start_byte,
                attempt.end_byte,
                attempt.source_hash,
                attempt.expected_text,
                attempt.replacement_text,
                attempt.target_kind,
            )
            by_replacement[key].append(attempt)

        groups: list[RepairGroup] = []
        for grouped_attempts in by_replacement.values():
            finding_ids = sorted(
                {finding_id for attempt in grouped_attempts for finding_id in attempt.finding_ids}
            )
            group_findings = [findings_by_id[item] for item in finding_ids]
            representative = grouped_attempts[0].model_copy(update={"finding_ids": finding_ids})
            group_id = _group_id(group_findings, [representative])
            representative = representative.model_copy(update={"group_id": group_id})
            groups.append(
                RepairGroup(
                    id=group_id,
                    finding_ids=finding_ids,
                    attempts=[representative],
                )
            )

        conflicts: dict[str, set[str]] = defaultdict(set)
        for left_index, left in enumerate(groups):
            for right in groups[left_index + 1 :]:
                for left_attempt in left.attempts:
                    for right_attempt in right.attempts:
                        conflict_key = ""
                        same_anchor = (
                            left_attempt.path == right_attempt.path
                            and left_attempt.start_byte == right_attempt.start_byte
                            and left_attempt.end_byte == right_attempt.end_byte
                        )
                        if (
                            same_anchor
                            and left_attempt.replacement_text != right_attempt.replacement_text
                        ):
                            conflict_key = "replacement"
                        elif (
                            same_anchor
                            and left_attempt.expected_text != right_attempt.expected_text
                        ):
                            conflict_key = "expected_text"
                        elif (
                            left_attempt.path == right_attempt.path
                            and left_attempt.source_hash != right_attempt.source_hash
                        ):
                            conflict_key = "base"
                        elif _overlap(left_attempt, right_attempt):
                            conflict_key = "overlap"
                        if conflict_key:
                            conflicts[left.id].add(conflict_key)
                            conflicts[right.id].add(conflict_key)
                if _validation_dependency(left, right, findings_by_id):
                    conflicts[left.id].add("validation_dependency")
                    conflicts[right.id].add("validation_dependency")
        resolved = [
            group.model_copy(
                update={
                    "conflict_key": sorted(conflicts[group.id])[0] if conflicts[group.id] else ""
                }
            )
            for group in groups
        ]
        return RepairPlan(
            groups=sorted(
                resolved,
                key=lambda group: (
                    group.path,
                    group.start_byte,
                    group.end_byte,
                    group.id,
                ),
            )
        )

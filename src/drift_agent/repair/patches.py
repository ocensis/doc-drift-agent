from __future__ import annotations

import difflib
import uuid
from pathlib import Path

from drift_agent.domain.models import Alignment, DocClaim, DriftFinding, PatchAttempt, SourceAnchor
from drift_agent.domain.values import MISSING_PARAMETER, MISSING_RETURN, MISSING_SYMBOL
from drift_agent.path_safety import repository_path_without_symlinks


def _attempt(
    repo_path: Path,
    *,
    finding: DriftFinding,
    anchor: SourceAnchor,
    replacement: str,
    target_kind: str,
) -> PatchAttempt:
    path = repository_path_without_symlinks(repo_path, anchor.path)
    before_bytes = path.read_bytes()
    before = before_bytes.decode("utf-8")
    after_bytes = (
        before_bytes[: anchor.start_byte]
        + replacement.encode("utf-8")
        + before_bytes[anchor.end_byte :]
    )
    after = after_bytes.decode("utf-8")
    records = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=anchor.path,
        tofile=anchor.path,
    )
    diff = "".join(
        record if record.endswith(("\r", "\n")) else f"{record}\n\\ No newline at end of file\n"
        for record in records
    )
    attempt_key = finding.fingerprint + ":" + str(anchor.start_byte)
    return PatchAttempt(
        id=f"attempt_{uuid.uuid5(uuid.NAMESPACE_URL, attempt_key)}",
        finding_ids=[finding.id],
        path=anchor.path,
        source_hash=anchor.source_hash,
        start_byte=anchor.start_byte,
        end_byte=anchor.end_byte,
        expected_text=anchor.exact_text,
        replacement_text=replacement,
        unified_diff=diff,
        target_kind="docstring" if target_kind == "docstring" else "markdown",
    )


def propose_symbol_patch(
    repo_path: Path,
    claim: DocClaim,
    finding: DriftFinding,
) -> list[PatchAttempt]:
    if finding.kind == "symbol_reference_deleted":
        if not claim.complete_declaration or claim.declaration_anchor is None:
            return []
        anchor = claim.declaration_anchor
        path = repository_path_without_symlinks(repo_path, anchor.path)
        before = path.read_bytes()
        start = anchor.start_byte
        if before[:start].endswith(b"\r\n\r\n"):
            start -= 2
        elif before[:start].endswith(b"\n\n"):
            start -= 1
        if start != anchor.start_byte:
            anchor = anchor.model_copy(
                update={
                    "start_byte": start,
                    "exact_text": before[start : anchor.end_byte].decode("utf-8"),
                }
            )
        return [
            _attempt(
                repo_path,
                finding=finding,
                anchor=anchor,
                replacement="",
                target_kind="markdown",
            )
        ]
    if (
        finding.kind != "symbol_reference_renamed"
        or finding.new_value is None
        or finding.new_value == MISSING_SYMBOL
    ):
        return []
    if claim.kind == "signature" and claim.declaration_anchor is not None:
        old_symbol = str(finding.old_value)
        new_symbol = str(finding.new_value)
        replacement = claim.declaration_anchor.exact_text
        if replacement.count(old_symbol) != 1:
            return []
        replacement = replacement.replace(old_symbol, new_symbol, 1)
        old_name = old_symbol.rsplit(".", 1)[-1]
        new_name = new_symbol.rsplit(".", 1)[-1]
        function_pattern = f"def {old_name}"
        async_pattern = f"async def {old_name}"
        if async_pattern in replacement:
            replacement = replacement.replace(
                async_pattern,
                f"async def {new_name}",
                1,
            )
        elif function_pattern in replacement:
            replacement = replacement.replace(
                function_pattern,
                f"def {new_name}",
                1,
            )
        else:
            return []
        return [
            _attempt(
                repo_path,
                finding=finding,
                anchor=claim.declaration_anchor,
                replacement=replacement,
                target_kind="markdown",
            )
        ]
    anchor = claim.value_anchor or claim.anchor
    return [
        _attempt(
            repo_path,
            finding=finding,
            anchor=anchor,
            replacement=str(finding.new_value),
            target_kind="markdown",
        )
    ]


def propose_docstring_patch(
    repo_path: Path,
    alignment: Alignment,
    finding: DriftFinding,
) -> list[PatchAttempt]:
    claim = alignment.claim
    fact = alignment.fact
    if claim.extraction_error is not None:
        return []
    if finding.kind == "docstring_parameter_changed":
        parameter = next(
            (item for item in fact.parameters if item.name == finding.component_id),
            None,
        )
        if parameter is None or finding.new_value == MISSING_PARAMETER:
            return [
                _attempt(
                    repo_path,
                    finding=finding,
                    anchor=claim.anchor,
                    replacement="",
                    target_kind="docstring",
                )
            ]
        if claim.value_anchor is None or parameter.annotation is None:
            return []
        return [
            _attempt(
                repo_path,
                finding=finding,
                anchor=claim.value_anchor,
                replacement=parameter.annotation,
                target_kind="docstring",
            )
        ]
    if finding.kind == "docstring_return_changed":
        if finding.new_value == MISSING_RETURN:
            return [
                _attempt(
                    repo_path,
                    finding=finding,
                    anchor=claim.anchor,
                    replacement="",
                    target_kind="docstring",
                )
            ]
        if claim.value_anchor is None or fact.return_annotation is None:
            return []
        return [
            _attempt(
                repo_path,
                finding=finding,
                anchor=claim.value_anchor,
                replacement=fact.return_annotation,
                target_kind="docstring",
            )
        ]
    return []

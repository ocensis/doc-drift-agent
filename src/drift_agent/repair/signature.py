import difflib
import uuid
from pathlib import Path

from drift_agent.domain.models import Alignment, DriftFinding, PatchAttempt
from drift_agent.path_safety import repository_path_without_symlinks


class SignaturePatcher:
    def propose(
        self,
        repo_path: Path,
        alignment: Alignment,
        finding: DriftFinding,
    ) -> PatchAttempt:
        claim = alignment.claim
        anchor = claim.anchor
        path = repository_path_without_symlinks(repo_path, anchor.path)
        encoded = path.read_bytes()
        before = encoded.decode("utf-8")
        if anchor.exact_text.endswith("\r\n"):
            newline = "\r\n"
        elif anchor.exact_text.endswith("\n"):
            newline = "\n"
        else:
            newline = ""
        prefix = "async def" if alignment.fact.is_async else "def"
        replacement = f"{prefix} {alignment.fact.signature}: ...{newline}"
        if alignment.method != "exact_fqn" and claim.declaration_anchor is not None:
            anchor = claim.declaration_anchor
            if anchor.exact_text.count(claim.anchor.exact_text) != 1:
                raise ValueError("signature is not unique inside declaration anchor")
            replacement = anchor.exact_text.replace(claim.anchor.exact_text, replacement, 1)
        after_bytes = (
            encoded[: anchor.start_byte] + replacement.encode("utf-8") + encoded[anchor.end_byte :]
        )
        after = after_bytes.decode("utf-8")
        diff_records = difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=claim.anchor.path,
            tofile=claim.anchor.path,
        )
        diff = "".join(
            record if record.endswith(("\r", "\n")) else f"{record}\n\\ No newline at end of file\n"
            for record in diff_records
        )
        attempt_key = f"{finding.fingerprint}:{anchor.path}:{anchor.start_byte}:{anchor.end_byte}"
        return PatchAttempt(
            id=f"attempt_{uuid.uuid5(uuid.NAMESPACE_URL, attempt_key)}",
            finding_ids=[finding.id],
            path=anchor.path,
            source_hash=claim.anchor.source_hash,
            start_byte=anchor.start_byte,
            end_byte=anchor.end_byte,
            expected_text=anchor.exact_text,
            replacement_text=replacement,
            unified_diff=diff,
            target_kind="markdown",
        )

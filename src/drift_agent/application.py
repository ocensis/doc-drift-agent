import math
import re
import subprocess
import uuid
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast

from drift_agent.agent.budget import BudgetExhausted, BudgetLedger
from drift_agent.agent.pipeline import run_pipeline
from drift_agent.agent.state import AgentState
from drift_agent.alignment import (
    align_exact,
    ambiguous_exact_claims,
    ambiguous_exact_facts,
)
from drift_agent.config import DriftConfig, ProjectConfig, TruthConfig, load_config_with_hash
from drift_agent.detectors.executable import ExecutableExampleDetector
from drift_agent.detectors.section_semantic import (
    ResolvedSection,
    SectionEvidence,
    SectionSemanticDetector,
)
from drift_agent.detectors.semantic import ConstantReturnSemanticDetector
from drift_agent.detectors.structural import StructuralDetector
from drift_agent.domain.enums import (
    FindingDisposition,
    RunMode,
    RunStatus,
    ValidationStatus,
)
from drift_agent.domain.models import (
    Alignment,
    ApprovalRequest,
    ChangeSet,
    CodeFact,
    DocClaim,
    DriftFinding,
    EvidenceAnchor,
    PatchAttempt,
    ProviderIssue,
    RepairGroupOutcome,
    ResidualChange,
    RunRequest,
    SuppressionRecord,
    UnsupportedSymbolFact,
    Usage,
    ValidationResult,
    VerifiedRepairBundle,
    WorkspaceSnapshot,
)
from drift_agent.domain.models import (
    MemoryEvent as BundleMemoryEvent,
)
from drift_agent.domain.status import aggregate_status
from drift_agent.domain.values import MISSING_SYMBOL
from drift_agent.hashing import sha256_bytes, sha256_file
from drift_agent.languages import source_language
from drift_agent.memory import (
    AliasService,
    AliasValidationContext,
    AlignmentEvidenceKey,
    DecisionService,
    FindingValidityKey,
    ManualStateRevisionChangedError,
    PersistedAlignment,
    PersistedFinding,
    RunCompletion,
    RunRecord,
    RunService,
    SQLiteStateStore,
)
from drift_agent.memory.models import canonical_json as memory_canonical_json
from drift_agent.model import ModelCallUsage, ModelClient, ModelTokenUsage
from drift_agent.model.client import ModelClientError, ModelTransport
from drift_agent.model.openrouter import (
    ModelConfigurationError,
    OpenRouterSettings,
    OpenRouterTransport,
)
from drift_agent.normalization import finding_fingerprint, finding_sort_key
from drift_agent.path_safety import (
    UnsafeInputPathError,
    repository_path_without_symlinks,
)
from drift_agent.patterns import matches_any
from drift_agent.providers.docstring_claims import GoogleDocstringClaimProvider
from drift_agent.providers.executable_examples import (
    ConfiguredExecutableExampleProvider,
    ExecutableExample,
    ExecutableExampleIssue,
)
from drift_agent.providers.markdown_claims import MarkdownClaimProvider
from drift_agent.providers.python_facts import PythonFactProvider
from drift_agent.providers.section_claims import SectionClaim, SectionClaimProvider
from drift_agent.providers.semantic_claims import SemanticReturnClaimProvider
from drift_agent.providers.typescript_facts import TypeScriptFactProvider
from drift_agent.repair.patches import propose_docstring_patch, propose_symbol_patch
from drift_agent.repair.planner import RepairGroup, RepairPlanner
from drift_agent.repair.semantic import (
    SemanticRepairBoundaryError,
    SemanticRepairCandidate,
    SemanticRepairSession,
    build_semantic_attempt,
)
from drift_agent.repair.signature import SignaturePatcher
from drift_agent.scope.git import (
    MISSING_INPUT_HASH,
    GitChange,
    GitScopeResolver,
    SnapshotChangedError,
)
from drift_agent.semantic_alignment import align_semantic_returns
from drift_agent.validation.commands import (
    CommandCompileError,
    ValidationCommandRunner,
    ValidationInputChangedError,
    validation_input_manifest,
)
from drift_agent.validation.docstring_ast import docstring_ast_unchanged
from drift_agent.workspace.identity import (
    IdentitySet,
    resolve_identities,
    resolve_state_path,
)
from drift_agent.workspace.lock import (
    LockObservation,
    LockTimeoutError,
    WorkspaceLock,
    probe_workspace_lock,
)
from drift_agent.workspace.transaction import (
    StaleWorkspaceError,
    WorkspaceTransaction,
)

TruthKind = Literal["code_derived", "design", "contract", "unknown"]


def _reject_linked_directories(repo: Path, root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_symlink() and (path.is_dir() or not path.exists()):
            repository_path_without_symlinks(
                repo,
                path.relative_to(repo).as_posix(),
            )


def _documents(
    repo: Path,
    roots: list[str],
    include: list[str],
    exclude: list[str],
) -> list[str]:
    candidates: set[str] = set()
    for root in roots:
        root_path = repository_path_without_symlinks(repo, root)
        _reject_linked_directories(repo, root_path)
        for path in root_path.rglob("*.md"):
            relative = path.relative_to(repo).as_posix()
            if matches_any(relative, include) and not matches_any(relative, exclude):
                repository_path_without_symlinks(repo, relative)
                candidates.add(relative)
    return sorted(candidates)


def _python_sources(
    repo: Path,
    roots: list[str],
    include: list[str],
    exclude: list[str],
) -> list[str]:
    candidates: set[str] = set()
    for root in roots:
        root_path = repository_path_without_symlinks(repo, root)
        _reject_linked_directories(repo, root_path)
        for path in root_path.rglob("*.py"):
            relative = path.relative_to(repo).as_posix()
            if matches_any(relative, include) and not matches_any(relative, exclude):
                repository_path_without_symlinks(repo, relative)
                candidates.add(relative)
    return sorted(candidates)


def _typescript_sources(
    repo: Path,
    roots: list[str],
    include: list[str],
    exclude: list[str],
) -> list[str]:
    candidates: set[str] = set()
    for root in roots:
        root_path = repository_path_without_symlinks(repo, root)
        _reject_linked_directories(repo, root_path)
        for pattern in ("*.ts", "*.tsx"):
            for path in root_path.rglob(pattern):
                if path.name.endswith(".d.ts"):
                    continue
                relative = path.relative_to(repo).as_posix()
                if matches_any(relative, include) and not matches_any(relative, exclude):
                    repository_path_without_symlinks(repo, relative)
                    candidates.add(relative)
    return sorted(candidates)


def _resolve_single_segment_claims(
    claims: list[DocClaim],
    facts: list[CodeFact],
) -> list[DocClaim]:
    """Resolve short doc references against name-addressable TypeScript exports.

    A reference that exactly matches any fact id (either language, current or
    before) is never rewritten — normal alignment must win, so TypeScript
    suffix matches can never hijack a claim addressed to a real symbol.
    Otherwise a unique TS export suffix match rewrites the claim to the full
    symbol id.  Unresolved single-segment claims are dropped silently — prose
    backticks must never surface as unsupported-claim noise — while unresolved
    multi-segment claims keep today's Python-parity behavior.
    """

    exact_ids = {fact.symbol_id for fact in facts if fact.symbol_id}
    ts_ids = [fact.symbol_id for fact in facts if fact.language == "typescript" and fact.symbol_id]
    if not ts_ids:
        return [claim for claim in claims if not claim.single_segment]
    resolved: list[DocClaim] = []
    for claim in claims:
        reference = claim.symbol_id
        if reference in exact_ids:
            resolved.append(claim)
            continue
        matches = {symbol_id for symbol_id in ts_ids if symbol_id.endswith(f".{reference}")}
        if len(matches) == 1:
            resolved.append(claim.model_copy(update={"symbol_id": next(iter(matches))}))
        elif claim.single_segment:
            continue
        else:
            resolved.append(claim)
    return resolved


_STRING_LITERAL = re.compile(r"[\"'`]([A-Za-z_][A-Za-z0-9_.:-]{3,63})[\"'`]")


def _string_literal_index(
    repo_path: Path,
    source_paths: set[str],
) -> dict[str, dict[str, int]]:
    """Index identifier-like string literals across the known source files.

    Prose docs reference runtime vocabulary (graph node names, span names,
    error codes, routing keys) that exists in code only as string literals; a
    section mentioning such a literal maps to the file that declares it. Where
    that file lives has nothing to do with whether this change touched it — an
    error code documented in a runbook is declared once and rarely edited — so
    the index spans every source file the run knows about, and relevance to the
    change is left to evidence ranking and audit ordering. Values map
    path -> 1-based line of the first occurrence so excerpts can centre on the
    referenced region.
    """

    index: dict[str, dict[str, int]] = {}
    for source_path in sorted(source_paths):
        try:
            raw = repository_path_without_symlinks(repo_path, source_path).read_bytes()
        except (OSError, UnsafeInputPathError):
            continue
        content = raw.decode("utf-8", "replace")
        for match in _STRING_LITERAL.finditer(content):
            lines = index.setdefault(match.group(1), {})
            if source_path not in lines:
                lines[source_path] = content.count("\n", 0, match.start()) + 1
    return index


_SECTION_EVIDENCE_FILES = 3
_SECTION_EXCERPT_CHARS = 3200
_SECTION_TOPUP_RARE_FRACTION = 0.25
_SECTION_TOPUP_MIN_TERM = 4
_SOURCE_IDENTIFIER = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]{3,}")


def _changed_file_vocabulary(
    repo_path: Path, changed_source: set[str]
) -> dict[str, frozenset[str]]:
    """Identifier vocabulary of each changed file, for section-to-file ranking."""

    vocabulary: dict[str, frozenset[str]] = {}
    for source_path in sorted(changed_source):
        try:
            raw = repository_path_without_symlinks(repo_path, source_path).read_bytes()
        except (OSError, UnsafeInputPathError):
            continue
        content = raw.decode("utf-8", "replace")
        vocabulary[source_path] = frozenset(
            match.group(0) for match in _SOURCE_IDENTIFIER.finditer(content)
        )
    return vocabulary


def _rank_topup_candidates(
    claim: SectionClaim,
    vocabulary: dict[str, frozenset[str]],
    exclude: set[str],
) -> list[str]:
    """Changed files ranked by rare vocabulary shared with the section."""

    if not vocabulary:
        return []
    # Only the section's deliberate code references count, not every
    # identifier-shaped word in the prose: prose vocabulary is large and
    # generic, so scoring on it ranks whichever source file is biggest rather
    # than whichever one the section is about.
    terms = {token.removesuffix("()").rsplit(".", 1)[-1] for token in claim.tokens}
    terms = {term for term in terms if len(term) >= _SECTION_TOPUP_MIN_TERM}
    if not terms:
        return []
    rarity_cut = max(1, int(len(vocabulary) * _SECTION_TOPUP_RARE_FRACTION))
    scored: list[tuple[float, str]] = []
    for path, words in vocabulary.items():
        if path in exclude:
            continue
        score = 0.0
        for term in terms:
            if term not in words:
                continue
            frequency = sum(1 for other in vocabulary.values() if term in other)
            if frequency > rarity_cut:
                continue
            score += math.log(len(vocabulary) / frequency)
        if score > 0:
            scored.append((score, path))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [path for _, path in scored]
def _root_markdown_paths(repo_path: Path, project: ProjectConfig) -> list[str]:
    paths: list[str] = []
    for root_doc in sorted(repo_path.glob("*.md")):
        relative = root_doc.name
        if matches_any(relative, project.include) and not matches_any(relative, project.exclude):
            paths.append(relative)
    return paths


def _resolve_sections(
    repo_path: Path,
    section_claims: list[SectionClaim],
    facts: list[CodeFact],
    *,
    changed_source: set[str],
    changed_docs: set[str],
    changed_ranges: dict[str, list[tuple[int, int]]] | None = None,
) -> list[ResolvedSection]:
    """Map prose sections to code evidence via their extracted anchors.

    A section qualifies when at least one token resolves to a known fact
    (exact or unique name suffix) or to an existing repository file.
    Sections whose evidence intersects the changed source set (or whose doc
    changed) sort first so tight model budgets audit the affected area.
    """

    exact = {fact.symbol_id: (fact.path, fact.line) for fact in facts if fact.symbol_id}
    by_name: dict[str, set[tuple[str, int]]] = {}
    for fact in facts:
        if fact.symbol_identity is not None:
            by_name.setdefault(fact.symbol_identity.name, set()).add((fact.path, fact.line))
    literal_paths = _string_literal_index(repo_path, changed_source)
    vocabulary = _changed_file_vocabulary(repo_path, changed_source)
    resolved: list[tuple[tuple[int, int], ResolvedSection]] = []
    for claim in section_claims:
        entries: list[tuple[str, int | None]] = []
        for raw_token in claim.tokens:
            token = raw_token.removesuffix("()")
            if token in exact:
                entries.append(exact[token])
            elif token in by_name and len({entry[0] for entry in by_name[token]}) == 1:
                entries.append(min(by_name[token], key=lambda item: item[1]))
            elif "." in token and "/" not in token:
                suffix_matches = {
                    entry
                    for symbol_id, entry in exact.items()
                    if symbol_id == token or symbol_id.endswith(f".{token}")
                }
                if len({entry[0] for entry in suffix_matches}) == 1:
                    entries.append(min(suffix_matches, key=lambda item: item[1]))
            elif token in literal_paths and len(literal_paths[token]) == 1:
                entries.append(next(iter(literal_paths[token].items())))
            elif "/" in token:
                try:
                    candidate = repository_path_without_symlinks(
                        repo_path, token, allow_missing=True
                    )
                except (OSError, UnsafeInputPathError):
                    continue
                if candidate.is_file():
                    entries.append((token, None))
                elif candidate.is_dir():
                    # Docs name a module by its directory as readily as by a
                    # file ("configuration lives in src/models"). Resolve to the
                    # directory's own changed files: an untouched tree cannot
                    # evidence drift, so nothing is manufactured when the
                    # directory is not part of this change.
                    prefix = f"{token.rstrip('/')}/"
                    entries.extend(
                        (changed_path, None)
                        for changed_path in sorted(changed_source)
                        if changed_path.startswith(prefix)
                        and "/" not in changed_path[len(prefix) :]
                    )
        paths = [entry[0] for entry in entries]
        anchor_lines: dict[str, list[int]] = {}
        for entry_path, entry_line in entries:
            if entry_line is not None:
                anchor_lines.setdefault(entry_path, []).append(entry_line)
        unique_paths = list(dict.fromkeys(paths))
        if not unique_paths:
            continue
        # The contradiction often lives in a changed sibling of the referenced
        # file (a refactor moves behavior next door), so top up the evidence
        # with changed files sharing a directory with the referenced ones.
        referenced_dirs = {PurePosixPath(path).parent for path in unique_paths}
        for changed_path in sorted(changed_source):
            if len(unique_paths) >= _SECTION_EVIDENCE_FILES:
                break
            if (
                changed_path not in unique_paths
                and PurePosixPath(changed_path).parent in referenced_dirs
            ):
                unique_paths.append(changed_path)
        unique_paths = unique_paths[:_SECTION_EVIDENCE_FILES]
        evidence: list[SectionEvidence] = []
        changed_evidence = 0
        for code_path in unique_paths:
            try:
                raw = repository_path_without_symlinks(repo_path, code_path).read_bytes()
            except (OSError, UnsafeInputPathError):
                continue
            if code_path in changed_source:
                changed_evidence += 1
            excerpt = _anchored_excerpt(
                raw.decode("utf-8", "replace"), anchor_lines.get(code_path, [])
            )
            evidence.append(
                SectionEvidence(
                    path=code_path,
                    excerpt=excerpt,
                    source_hash=sha256_bytes(raw),
                    changed_text=_changed_region_text(
                        raw.decode("utf-8", "replace"),
                        (changed_ranges or {}).get(code_path, []),
                    ),
                )
            )
        if not evidence:
            continue
        affected = claim.path in changed_docs or changed_evidence > 0
        alternate: list[SectionEvidence] = []
        for alt_path in _rank_topup_candidates(claim, vocabulary, set(unique_paths))[
            :_SECTION_EVIDENCE_FILES
        ]:
            try:
                alt_raw = repository_path_without_symlinks(repo_path, alt_path).read_bytes()
            except (OSError, UnsafeInputPathError):
                continue
            alt_ranges = (changed_ranges or {}).get(alt_path, [])
            alternate.append(
                SectionEvidence(
                    path=alt_path,
                    excerpt=_anchored_excerpt(
                        alt_raw.decode("utf-8", "replace"),
                        [start for start, _ in alt_ranges],
                    ),
                    source_hash=sha256_bytes(alt_raw),
                    changed_text=_changed_region_text(
                        alt_raw.decode("utf-8", "replace"), alt_ranges
                    ),
                )
            )
        resolved.append(
            (
                (0 if affected else 1, -changed_evidence),
                ResolvedSection(
                    claim=claim, evidence=evidence, alternate_evidence=alternate
                ),
            )
        )
    resolved.sort(key=lambda item: (item[0], item[1].claim.path, item[1].claim.start_byte))
    return [section for _, section in resolved]


def _changed_region_text(content: str, ranges: list[tuple[int, int]]) -> str:
    """Head-side text of the regions this change touched, for the quote guard."""

    if not ranges:
        return ""
    lines = content.splitlines(keepends=True)
    parts: list[str] = []
    for start, end in ranges:
        parts.append("".join(lines[max(0, start - 1) : min(len(lines), end)]))
    return "".join(parts)


_EXCERPT_WINDOW_LINES = 30


def _anchored_excerpt(content: str, anchors: list[int]) -> str:
    """Excerpt code around referenced lines instead of truncating the head.

    Sections talk about the regions they reference; a head-of-file cut mostly
    shows imports and hides the behavior under audit. Windows around each
    anchor are merged and capped; files without anchors keep the head cut.
    """

    if not anchors:
        return content[:_SECTION_EXCERPT_CHARS]
    lines = content.splitlines(keepends=True)
    windows: list[tuple[int, int]] = []
    for anchor in sorted(set(anchors)):
        start = max(0, anchor - 1 - _EXCERPT_WINDOW_LINES)
        end = min(len(lines), anchor - 1 + _EXCERPT_WINDOW_LINES)
        if windows and start <= windows[-1][1]:
            windows[-1] = (windows[-1][0], max(windows[-1][1], end))
        else:
            windows.append((start, end))
    parts: list[str] = []
    for start, end in windows:
        parts.append(f"... (lines {start + 1}-{end}) ...\n")
        parts.append("".join(lines[start:end]))
    return "".join(parts)[:_SECTION_EXCERPT_CHARS]


_ENV_KEY_PATTERN = re.compile(r"^([A-Z][A-Z0-9_]{2,})=", re.MULTILINE)
_ENV_ENUMERATING_THRESHOLD = 3


def _env_example_keys(raw: bytes) -> dict[str, int]:
    text = raw.decode("utf-8", "replace")
    keys: dict[str, int] = {}
    for lineno, line in enumerate(text.splitlines(), start=1):
        match = _ENV_KEY_PATTERN.match(line.strip())
        if match is not None and match.group(1) not in keys:
            keys[match.group(1)] = lineno
    return keys


def _env_key_findings(
    repo_path: Path,
    resolver: GitScopeResolver,
    doc_paths: list[str],
    *,
    project: ProjectConfig,
    repository_id: str,
) -> list[DriftFinding]:
    """Compare declared configuration keys (.env.example) against doc coverage.

    ``.env.example`` is read directly (it is a well-known configuration
    manifest, not part of the include globs).  Both directions are gated on
    the file actually differing from the baseline, so unchanged repositories
    never produce these findings:

    - a key added since baseline that is missing from every "enumerating"
      doc (a doc mentioning >= 3 known keys, i.e. one that curates a key
      table) is an omission;
    - a key removed since baseline but still mentioned in any in-scope doc
      is a stale reference.
    """

    try:
        env_path = repository_path_without_symlinks(repo_path, ".env.example", allow_missing=True)
        current_raw = env_path.read_bytes() if env_path.is_file() else b""
    except (OSError, UnsafeInputPathError):
        current_raw = b""
    before_raw = resolver.head_bytes(".env.example") or b""
    if current_raw == before_raw:
        return []
    current_keys = _env_example_keys(current_raw)
    before_keys = _env_example_keys(before_raw)
    added = {key: line for key, line in current_keys.items() if key not in before_keys}
    removed = {key: line for key, line in before_keys.items() if key not in current_keys}
    if not added and not removed:
        return []

    known_keys = set(current_keys) | set(before_keys)
    scan_paths = list(doc_paths)
    # Root-level docs (README.md and friends) sit outside docs_roots but are
    # exactly where configuration tables live.
    for relative in _root_markdown_paths(repo_path, project):
        if relative not in scan_paths:
            scan_paths.append(relative)
    doc_mentions: dict[str, dict[str, int]] = {}
    doc_hashes: dict[str, str] = {}
    for doc_path in scan_paths:
        try:
            raw = repository_path_without_symlinks(repo_path, doc_path).read_bytes()
        except (OSError, UnsafeInputPathError):
            continue
        doc_hashes[doc_path] = sha256_bytes(raw)
        doc_text = raw.decode("utf-8", "replace")
        mentions: dict[str, int] = {}
        for key in known_keys:
            offset = doc_text.find(key)
            if offset >= 0:
                mentions[key] = doc_text.count("\n", 0, offset) + 1
        if mentions:
            doc_mentions[doc_path] = mentions

    env_hash = sha256_bytes(current_raw)
    findings: list[DriftFinding] = []

    def _finding(
        *,
        kind: str,
        key: str,
        reason: str,
        reason_code: str,
        disposition: FindingDisposition,
        code_line: int,
        doc_path: str,
        doc_line: int,
        doc_hash: str,
    ) -> DriftFinding:
        code_evidence = EvidenceAnchor(
            path=".env.example", line=code_line, source_hash=env_hash, start_byte=0, end_byte=0
        )
        doc_evidence = EvidenceAnchor(
            path=doc_path, line=doc_line, source_hash=doc_hash, start_byte=0, end_byte=0
        )
        fingerprint = finding_fingerprint(
            repository_id=repository_id,
            symbol_identity=f"env.{key}",
            kind=kind,
            component_id=key,
            old_value=None,
            new_value=None,
            code_evidence=code_evidence,
            doc_evidence=doc_evidence,
            detector_id="structural.config_keys",
            detector_version="1",
        )
        return DriftFinding(
            id=f"finding_{fingerprint}",
            symbol_id=f"env.{key}",
            disposition=disposition,
            truth_source="unknown" if disposition is FindingDisposition.UNRESOLVED else "code",
            code_evidence=code_evidence,
            doc_evidence=doc_evidence,
            reason=reason,
            kind=kind,
            component_id=key,
            detector_id="structural.config_keys",
            detector_version="1",
            fingerprint=fingerprint,
            reason_code=reason_code,
        )

    required_mentions = max(_ENV_ENUMERATING_THRESHOLD, -(-len(known_keys) * 3 // 10))
    enumerating = {
        doc_path: mentions
        for doc_path, mentions in doc_mentions.items()
        if len(mentions) >= required_mentions
    }
    for key in sorted(added):
        for doc_path in sorted(enumerating):
            if key in enumerating[doc_path]:
                continue
            findings.append(
                _finding(
                    kind="config_key_undocumented",
                    key=key,
                    reason=(
                        f"new configuration key {key!r} is missing from {doc_path}, "
                        "which enumerates configuration keys"
                    ),
                    reason_code="omission.config_key",
                    disposition=FindingDisposition.UNRESOLVED,
                    code_line=added[key],
                    doc_path=doc_path,
                    doc_line=1,
                    doc_hash=doc_hashes[doc_path],
                )
            )
    for key in sorted(removed):
        for doc_path, mentions in sorted(doc_mentions.items()):
            if key not in mentions:
                continue
            findings.append(
                _finding(
                    kind="config_key_removed",
                    key=key,
                    reason=(
                        f"documentation references configuration key {key!r}, "
                        "which was removed from .env.example"
                    ),
                    reason_code="detected",
                    disposition=FindingDisposition.DETECTED,
                    code_line=1,
                    doc_path=doc_path,
                    doc_line=mentions[key],
                    doc_hash=doc_hashes[doc_path],
                )
            )
    return findings


def _source_roots_for(paths: list[str], roots: list[str]) -> list[str]:
    return [
        root
        for root in sorted({PurePosixPath(root).as_posix() for root in roots})
        if any(PurePosixPath(path).is_relative_to(PurePosixPath(root)) for path in paths)
    ]


def _path_truth(path: str, config: TruthConfig) -> TruthKind:
    rules: tuple[tuple[TruthKind, list[str]], ...] = (
        ("code_derived", config.code_derived),
        ("design", config.design),
        ("contract", config.contract),
    )
    matches = [kind for kind, patterns in rules if matches_any(path, patterns)]
    return matches[0] if len(matches) == 1 else "unknown"


def _claim_truth(claim: DocClaim, config: TruthConfig) -> TruthKind:
    if claim.explicit_truth is not None:
        return claim.explicit_truth
    rules: tuple[tuple[TruthKind, list[str]], ...] = (
        ("code_derived", config.code_derived),
        ("design", config.design),
        ("contract", config.contract),
    )
    matches = [kind for kind, patterns in rules if matches_any(claim.anchor.path, patterns)]
    return matches[0] if len(matches) == 1 else "unknown"


def _looks_like_fqn(value: str) -> bool:
    parts = value.split(".")
    return len(parts) >= 2 and all(part.isidentifier() for part in parts)


def _apply_truth_policy(finding: DriftFinding, truth: TruthKind) -> DriftFinding:
    if truth == "code_derived":
        return finding
    disposition = (
        FindingDisposition.NEEDS_APPROVAL
        if truth in {"design", "contract"}
        else FindingDisposition.UNRESOLVED
    )
    return finding.model_copy(
        update={
            "disposition": disposition,
            "truth_source": "unknown" if truth == "unknown" else "human",
            "reason": f"{finding.reason}; truth is {truth}",
            "reason_code": (
                "truth_requires_approval" if truth in {"design", "contract"} else "unknown_truth"
            ),
        }
    )


def _ambiguity_findings(
    facts: list[CodeFact],
    claims: list[DocClaim],
    *,
    repository_id: str,
) -> list[DriftFinding]:
    facts_by_symbol = {fact.symbol_id: fact for fact in facts}
    findings: list[DriftFinding] = []
    signature_claims = [claim for claim in claims if claim.kind == "signature"]
    for symbol_id, grouped_claims in ambiguous_exact_claims(
        facts,
        signature_claims,
    ).items():
        fact = facts_by_symbol[symbol_id]
        first_claim = grouped_claims[0]
        code_evidence = EvidenceAnchor(
            path=fact.path,
            line=fact.line,
            source_hash=fact.source_hash,
            start_byte=(fact.signature_anchor.start_byte if fact.signature_anchor else 0),
            end_byte=(fact.signature_anchor.end_byte if fact.signature_anchor else 0),
        )
        doc_evidence = EvidenceAnchor(
            path=first_claim.anchor.path,
            line=first_claim.anchor.line,
            source_hash=first_claim.anchor.source_hash,
            start_byte=first_claim.anchor.start_byte,
            end_byte=first_claim.anchor.end_byte,
        )
        old_value = {"type": "ambiguous_claims", "count": len(grouped_claims)}
        new_value = {"type": "code_signature", "value": fact.signature}
        fingerprint = finding_fingerprint(
            repository_id=repository_id,
            symbol_identity=fact.symbol_identity or fact.symbol_id,
            kind="unsupported",
            component_id="symbol",
            old_value=old_value,
            new_value=new_value,
            code_evidence=code_evidence,
            doc_evidence=doc_evidence,
            detector_id=StructuralDetector.id,
            detector_version=StructuralDetector.version,
        )
        findings.append(
            DriftFinding(
                id=f"finding_{fingerprint}",
                symbol_id=symbol_id,
                disposition=FindingDisposition.UNRESOLVED,
                truth_source="unknown",
                code_evidence=code_evidence,
                doc_evidence=doc_evidence,
                reason=f"{len(grouped_claims)} exact-FQN claims are ambiguous",
                kind="unsupported",
                component_id="symbol",
                old_value=old_value,
                new_value=new_value,
                detector_id=StructuralDetector.id,
                detector_version=StructuralDetector.version,
                fingerprint=fingerprint,
                reason_code="ambiguity.claim",
            )
        )
    return findings


def _extraction_findings(
    facts: list[CodeFact],
    claims: list[DocClaim],
    *,
    repository_id: str,
) -> list[DriftFinding]:
    facts_by_symbol = {fact.symbol_id: fact for fact in facts}
    findings: list[DriftFinding] = []
    for claim in claims:
        if claim.extraction_error is None or claim.symbol_id not in facts_by_symbol:
            continue
        fact = facts_by_symbol[claim.symbol_id]
        code_evidence = EvidenceAnchor(
            path=fact.path,
            line=fact.line,
            source_hash=fact.source_hash,
            start_byte=(fact.signature_anchor.start_byte if fact.signature_anchor else 0),
            end_byte=(fact.signature_anchor.end_byte if fact.signature_anchor else 0),
        )
        doc_evidence = EvidenceAnchor(
            path=claim.anchor.path,
            line=claim.anchor.line,
            source_hash=claim.anchor.source_hash,
            start_byte=claim.anchor.start_byte,
            end_byte=claim.anchor.end_byte,
        )
        old_value = {"type": "unsupported_claim", "value": claim.signature}
        new_value = {"type": "code_signature", "value": fact.signature}
        fingerprint = finding_fingerprint(
            repository_id=repository_id,
            symbol_identity=fact.symbol_identity or fact.symbol_id,
            kind="unsupported",
            component_id="signature",
            old_value=old_value,
            new_value=new_value,
            code_evidence=code_evidence,
            doc_evidence=doc_evidence,
            detector_id=StructuralDetector.id,
            detector_version=StructuralDetector.version,
        )
        findings.append(
            DriftFinding(
                id=f"finding_{fingerprint}",
                symbol_id=claim.symbol_id,
                disposition=FindingDisposition.UNRESOLVED,
                truth_source="unknown",
                code_evidence=code_evidence,
                doc_evidence=doc_evidence,
                reason=f"claim extraction failed: {claim.extraction_error}",
                kind="unsupported",
                component_id="signature",
                old_value=old_value,
                new_value=new_value,
                detector_id=StructuralDetector.id,
                detector_version=StructuralDetector.version,
                fingerprint=fingerprint,
                reason_code="unsupported.markdown_claim",
            )
        )
    return findings


def _fact_ambiguity_findings(
    facts: list[CodeFact],
    claims: list[DocClaim],
    *,
    repository_id: str,
) -> list[DriftFinding]:
    claims_by_symbol = {claim.symbol_id: claim for claim in claims}
    findings: list[DriftFinding] = []
    for symbol_id, grouped_facts in ambiguous_exact_facts(facts, claims).items():
        first_fact = grouped_facts[0]
        claim = claims_by_symbol[symbol_id]
        code_evidence = EvidenceAnchor(
            path=first_fact.path,
            line=first_fact.line,
            source_hash=first_fact.source_hash,
            start_byte=(
                first_fact.signature_anchor.start_byte if first_fact.signature_anchor else 0
            ),
            end_byte=(first_fact.signature_anchor.end_byte if first_fact.signature_anchor else 0),
        )
        doc_evidence = EvidenceAnchor(
            path=claim.anchor.path,
            line=claim.anchor.line,
            source_hash=claim.anchor.source_hash,
            start_byte=claim.anchor.start_byte,
            end_byte=claim.anchor.end_byte,
        )
        old_value = {"type": "document_signature", "value": claim.signature}
        new_value = {"type": "ambiguous_facts", "count": len(grouped_facts)}
        fingerprint = finding_fingerprint(
            repository_id=repository_id,
            symbol_identity=first_fact.symbol_identity or first_fact.symbol_id,
            kind="unsupported",
            component_id="symbol",
            old_value=old_value,
            new_value=new_value,
            code_evidence=code_evidence,
            doc_evidence=doc_evidence,
            detector_id=StructuralDetector.id,
            detector_version=StructuralDetector.version,
        )
        findings.append(
            DriftFinding(
                id=f"finding_{fingerprint}",
                symbol_id=symbol_id,
                disposition=FindingDisposition.UNRESOLVED,
                truth_source="unknown",
                code_evidence=code_evidence,
                doc_evidence=doc_evidence,
                reason=f"{len(grouped_facts)} exact-FQN code facts are ambiguous",
                kind="unsupported",
                component_id="symbol",
                old_value=old_value,
                new_value=new_value,
                detector_id=StructuralDetector.id,
                detector_version=StructuralDetector.version,
                fingerprint=fingerprint,
                reason_code="ambiguity.symbol",
            )
        )
    return findings


def _code_facts_are_current(
    repo: Path,
    state: AgentState,
    excluded_paths: set[str] | None = None,
) -> bool:
    excluded = excluded_paths or set()
    try:
        return all(
            sha256_file(repository_path_without_symlinks(repo, fact.path)) == fact.source_hash
            for fact in state.get("facts", [])
            if fact.path not in excluded
        )
    except (OSError, UnsafeInputPathError):
        return False


def _snapshot_inputs_are_current(
    repo: Path,
    config: DriftConfig,
    snapshot: WorkspaceSnapshot,
    excluded_paths: set[str],
) -> bool:
    resolver = GitScopeResolver(repo, config.project)
    if resolver.head_revision() != snapshot.head_revision:
        return False
    if snapshot.validation_input_hashes:
        try:
            current_validation_inputs = validation_input_manifest(repo)
        except OSError:
            return False
        expected_validation_inputs = {
            path: value
            for path, value in snapshot.validation_input_hashes.items()
            if path not in excluded_paths
        }
        current_validation_inputs = {
            path: value
            for path, value in current_validation_inputs.items()
            if path not in excluded_paths
        }
        if current_validation_inputs != expected_validation_inputs:
            return False
    for path, expected_hash in snapshot.input_file_hashes.items():
        if path in excluded_paths:
            continue
        try:
            input_path = repository_path_without_symlinks(
                repo,
                path,
                allow_missing=expected_hash == MISSING_INPUT_HASH,
            )
            if expected_hash == MISSING_INPUT_HASH:
                if input_path.exists() or input_path.is_symlink():
                    return False
                continue
            if sha256_file(input_path) != expected_hash:
                return False
        except (OSError, UnsafeInputPathError):
            return False
    return True


def _scope_paths_are_current(
    repo: Path,
    config: DriftConfig,
    expected_paths: list[str],
    excluded_paths: set[str],
    *,
    expected_head: str,
    baseline_revision: str,
) -> bool:
    try:
        resolver = GitScopeResolver(
            repo,
            config.project,
            baseline_revision=baseline_revision,
            observed_head_revision=expected_head,
        )
        if not resolver.head_is_current():
            return False
        current = set(resolver.changed_paths())
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    return current - excluded_paths == set(expected_paths) - excluded_paths


def _git_changes_are_current(
    repo: Path,
    config: DriftConfig,
    expected_head: str,
    baseline_revision: str,
    expected_changes: list[GitChange],
    excluded_paths: set[str],
) -> bool:
    """Compare the complete Git status/rename sample, not only its path set."""

    def retained(changes: list[GitChange]) -> tuple[tuple[str, str | None, str | None], ...]:
        return tuple(
            (change.status, change.old_path, change.new_path)
            for change in changes
            if not (
                {path for path in (change.old_path, change.new_path) if path is not None}
                & excluded_paths
            )
        )

    try:
        resolver = GitScopeResolver(
            repo,
            config.project,
            baseline_revision=baseline_revision,
            observed_head_revision=expected_head,
        )
        if not resolver.head_is_current():
            return False
        current = resolver.changes()
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    return retained(current) == retained(expected_changes)


def _state_git_changes_are_current(
    repo: Path,
    state: AgentState,
    excluded_paths: set[str],
) -> bool:
    context: dict[str, Any] = cast(dict[str, Any], state.get("runtime_context", {}))
    expected_head = context.get("head_revision")
    baseline_revision = context.get("baseline_revision")
    expected_changes = context.get("git_changes")
    if (
        not isinstance(expected_head, str)
        or not isinstance(baseline_revision, str)
        or not isinstance(expected_changes, list)
    ):
        return False
    if not all(isinstance(change, GitChange) for change in expected_changes):
        return False
    return _git_changes_are_current(
        repo,
        state["config"],
        expected_head,
        baseline_revision,
        expected_changes,
        excluded_paths,
    )


def _state_scope_paths_are_current(
    repo: Path,
    state: AgentState,
    excluded_paths: set[str],
) -> bool:
    context: dict[str, Any] = cast(dict[str, Any], state.get("runtime_context", {}))
    expected_head = context.get("head_revision")
    baseline_revision = context.get("baseline_revision")
    if not isinstance(expected_head, str) or not isinstance(baseline_revision, str):
        return False
    return _scope_paths_are_current(
        repo,
        state["config"],
        state["changed_paths"],
        excluded_paths,
        expected_head=expected_head,
        baseline_revision=baseline_revision,
    )


def _attempt_rollback(
    transaction: WorkspaceTransaction,
) -> tuple[BaseException | None, list[PatchAttempt]]:
    try:
        transaction.rollback()
    except BaseException as error:
        return error, transaction.residual_attempts()
    return None, []


def _residual_current_hash(repo: Path, relative_path: str) -> str:
    """Describe residual bytes without letting an unsafe target erase the report."""

    try:
        path = repository_path_without_symlinks(
            repo,
            relative_path,
            allow_missing=True,
        )
    except (OSError, UnsafeInputPathError):
        return "unavailable"
    if not path.exists():
        return MISSING_INPUT_HASH
    try:
        return sha256_file(path)
    except OSError:
        return "unavailable"


def _global_snapshot_changed_state(state: AgentState) -> AgentState:
    findings = [
        finding
        if finding.disposition is FindingDisposition.FIXED
        else finding.model_copy(
            update={
                "disposition": FindingDisposition.UNRESOLVED,
                "reason_code": "global_snapshot_changed",
                "reason": (
                    finding.reason
                    if finding.reason_code == "global_snapshot_changed"
                    else f"{finding.reason}; global_snapshot_changed"
                ),
            }
        )
        for finding in state.get("findings", [])
    ]
    return {**state, "stale": True, "findings": findings}


def _runtime_failure(error: BaseException) -> ValidationResult:
    reason_code = getattr(error, "reason_code", "")
    configuration = isinstance(reason_code, str) and reason_code.startswith("config.")
    return ValidationResult(
        finding_ids=[],
        attempt_id="run",
        check="config" if configuration else "runtime",
        required=True,
        status=ValidationStatus.UNAVAILABLE,
        summary=f"{reason_code}: {error}" if configuration else f"{type(error).__name__}: {error}",
    )


def _rollback_failure(error: BaseException) -> ValidationResult:
    return ValidationResult(
        finding_ids=[],
        attempt_id="rollback",
        check="rollback",
        required=True,
        status=ValidationStatus.UNAVAILABLE,
        summary=f"{type(error).__name__}: {error}",
    )


def _failed_rollback_state(
    state: AgentState,
    validation: list[ValidationResult],
    error: BaseException,
    residual_attempts: list[PatchAttempt],
) -> AgentState:
    residual_finding_ids = {
        finding_id for attempt in residual_attempts for finding_id in attempt.finding_ids
    }
    findings = [
        finding.model_copy(
            update={
                "disposition": FindingDisposition.UNRESOLVED,
                "reason_code": "rollback_unavailable",
                "reason": f"{finding.reason}; rollback_unavailable",
            }
        )
        if finding.id in residual_finding_ids
        else finding
        for finding in state.get("findings", [])
    ]
    return {
        **state,
        "failed": True,
        "findings": findings,
        "validation": [*validation, _rollback_failure(error)],
        "residual_attempts": residual_attempts,
    }


def _rename_alignment_map(
    before_facts: list[CodeFact],
    current_facts: list[CodeFact],
    path_renames: list[tuple[str, str]],
) -> dict[str, str]:
    result: dict[str, str] = {}

    def declaration_shape(fact: CodeFact) -> tuple[object, ...]:
        return (
            tuple(
                (
                    parameter.kind,
                    parameter.annotation,
                    parameter.default,
                    parameter.required,
                )
                for parameter in fact.parameters
            ),
            fact.return_annotation,
        )

    for old_path, new_path in path_renames:
        old = [fact for fact in before_facts if fact.path == old_path]
        new = [fact for fact in current_facts if fact.path == new_path]
        structural: dict[str, list[CodeFact]] = {}
        for old_fact in old:
            structural[old_fact.symbol_id] = [
                new_fact
                for new_fact in new
                if (
                    old_fact.category == new_fact.category
                    and old_fact.owner == new_fact.owner
                    and old_fact.is_async == new_fact.is_async
                    and bool(old_fact.body_fingerprint)
                    and old_fact.body_fingerprint == new_fact.body_fingerprint
                )
            ]
        used_new: set[str] = set()
        exact_pairs: list[tuple[CodeFact, CodeFact]] = []
        for old_fact in old:
            exact = [
                candidate
                for candidate in structural[old_fact.symbol_id]
                if declaration_shape(candidate) == declaration_shape(old_fact)
            ]
            if len(exact) == 1:
                exact_pairs.append((old_fact, exact[0]))
        exact_target_counts = Counter(candidate.symbol_id for _, candidate in exact_pairs)
        for old_fact, candidate in exact_pairs:
            if exact_target_counts[candidate.symbol_id] == 1:
                result[old_fact.symbol_id] = candidate.symbol_id
                used_new.add(candidate.symbol_id)
        remaining_old = [fact for fact in old if fact.symbol_id not in result]
        remaining_candidates = {
            fact.symbol_id: [
                candidate
                for candidate in structural[fact.symbol_id]
                if candidate.symbol_id not in used_new
            ]
            for fact in remaining_old
        }
        candidate_counts = Counter(
            candidate.symbol_id
            for candidates in remaining_candidates.values()
            for candidate in candidates
        )
        for old_fact in remaining_old:
            candidates = remaining_candidates[old_fact.symbol_id]
            if len(candidates) == 1 and candidate_counts[candidates[0].symbol_id] == 1:
                result[old_fact.symbol_id] = candidates[0].symbol_id
                used_new.add(candidates[0].symbol_id)
    return result


def _validation_finding_key(finding: DriftFinding) -> tuple[str, ...]:
    """Logical finding identity for before/after validation multisets.

    File hashes and byte offsets legitimately change when an independent edit
    in the same file is retained, so validation compares the semantic finding
    material while still preserving duplicate counts.
    """

    return (
        finding.symbol_id,
        finding.kind,
        finding.component_id,
        memory_canonical_json(finding.old_value),
        memory_canonical_json(finding.new_value),
        finding.detector_id,
        finding.detector_version,
    )


def _finding_delta_is_clean(
    *,
    baseline: list[DriftFinding],
    remaining: list[DriftFinding],
    targets: list[DriftFinding],
) -> bool:
    """Require every target to disappear and forbid new active findings."""

    before = Counter(_validation_finding_key(finding) for finding in baseline)
    after = Counter(_validation_finding_key(finding) for finding in remaining)
    target = Counter(_validation_finding_key(finding) for finding in targets)
    allowed_after = before - target
    return not (after - allowed_after)


def _finding_delta_failures(
    *,
    baseline: list[DriftFinding],
    remaining: list[DriftFinding],
    targets: list[DriftFinding],
) -> list[DriftFinding]:
    """Return target remnants and findings exceeding the pre-repair baseline."""

    before = Counter(_validation_finding_key(finding) for finding in baseline)
    target = Counter(_validation_finding_key(finding) for finding in targets)
    allowed_after = before - target
    observed: Counter[tuple[str, ...]] = Counter()
    failures: list[DriftFinding] = []
    for finding in sorted(remaining, key=finding_sort_key):
        key = _validation_finding_key(finding)
        observed[key] += 1
        if observed[key] > allowed_after[key]:
            failures.append(finding)
    return failures


def _semantic_symbols(findings: list[DriftFinding]) -> set[str]:
    symbols = {finding.symbol_id for finding in findings}
    for finding in findings:
        for value in (finding.old_value, finding.new_value):
            if isinstance(value, str) and _looks_like_fqn(value):
                symbols.add(value)
    return symbols


def _expand_rename_symbols(
    symbols: set[str],
    rename_map: dict[str, str],
) -> set[str]:
    expanded = set(symbols)
    while True:
        previous = set(expanded)
        for old_symbol, new_symbol in rename_map.items():
            if old_symbol in expanded or new_symbol in expanded:
                expanded.update((old_symbol, new_symbol))
        if expanded == previous:
            return expanded


def _exclude_run_suppressions(
    state: AgentState,
    findings: list[DriftFinding],
) -> list[DriftFinding]:
    """Keep evidence-time decisions effective throughout the same transaction."""

    context: dict[str, Any] = cast(dict[str, Any], state.get("runtime_context", {}))
    suppressed = [
        finding
        for finding in context.get("suppressed_findings", [])
        if isinstance(finding, DriftFinding)
    ]
    remaining = Counter(_validation_finding_key(finding) for finding in suppressed)
    result: list[DriftFinding] = []
    for finding in sorted(findings, key=finding_sort_key):
        key = _validation_finding_key(finding)
        if remaining[key] > 0:
            remaining[key] -= 1
            continue
        result.append(finding)
    return result


def _unsupported_symbol_findings(
    current_symbols: list[UnsupportedSymbolFact],
    before_symbols: list[UnsupportedSymbolFact],
    claims: list[DocClaim],
    *,
    repository_id: str,
) -> list[DriftFinding]:
    """Report changed class references without attempting class repair.

    Classes remain outside the Stage 2 repair matrix.  We still retain their
    exact AST identity so a unique removed class, or a unique removed/added
    class pair in one changed file, is visible as a conservative finding.
    """

    findings: list[DriftFinding] = []
    before_names_by_path: dict[str, set[str]] = {}
    for symbol in before_symbols:
        before_names_by_path.setdefault(symbol.path, set()).add(symbol.symbol_identity.name)

    for claim in claims:
        if claim.kind not in {"signature", "symbol_reference"}:
            continue
        old_matches = [symbol for symbol in before_symbols if symbol.symbol_id == claim.symbol_id]
        current_matches = [
            symbol for symbol in current_symbols if symbol.symbol_id == claim.symbol_id
        ]
        if not old_matches and not current_matches:
            claim_name = claim.symbol_id.rsplit(".", 1)[-1]
            old_matches = [
                symbol for symbol in before_symbols if symbol.symbol_identity.name == claim_name
            ]
            current_matches = [
                symbol for symbol in current_symbols if symbol.symbol_identity.name == claim_name
            ]
            if len(old_matches) > 1 or len(current_matches) > 1:
                continue
        if len(current_matches) == 1:
            evidence_symbol = current_matches[0]
            new_value: Any = {
                "type": "unsupported_symbol",
                "symbol_id": evidence_symbol.symbol_id,
            }
        elif len(current_matches) > 1 or len(old_matches) != 1:
            continue
        else:
            old_symbol = old_matches[0]
            # A same-file class replacement is useful evidence, but remains
            # unsupported and is never treated as an automatic alignment.
            added = [
                symbol
                for symbol in current_symbols
                if symbol.path == old_symbol.path
                and symbol.symbol_identity.name
                not in before_names_by_path.get(old_symbol.path, set())
                and symbol.symbol_identity.category == old_symbol.symbol_identity.category
            ]
            if len(added) == 1:
                evidence_symbol = added[0]
                prefix, _, _ = claim.symbol_id.rpartition(".")
                new_value = f"{prefix}.{evidence_symbol.symbol_identity.name}"
            else:
                evidence_symbol = old_symbol
                new_value = MISSING_SYMBOL

        code_anchor = evidence_symbol.name_anchor
        doc_anchor = claim.value_anchor or claim.anchor
        code_evidence = EvidenceAnchor(
            path=evidence_symbol.path,
            line=evidence_symbol.line,
            source_hash=evidence_symbol.source_hash,
            start_byte=code_anchor.start_byte,
            end_byte=code_anchor.end_byte,
        )
        doc_evidence = EvidenceAnchor(
            path=doc_anchor.path,
            line=doc_anchor.line,
            source_hash=doc_anchor.source_hash,
            start_byte=doc_anchor.start_byte,
            end_byte=doc_anchor.end_byte,
        )
        fingerprint = finding_fingerprint(
            repository_id=repository_id,
            symbol_identity=evidence_symbol.symbol_identity,
            kind="unsupported",
            component_id="symbol",
            old_value=claim.symbol_id,
            new_value=new_value,
            code_evidence=code_evidence,
            doc_evidence=doc_evidence,
            detector_id=StructuralDetector.id,
            detector_version=StructuralDetector.version,
        )
        findings.append(
            DriftFinding(
                id=f"finding_{fingerprint}",
                symbol_id=evidence_symbol.symbol_id,
                symbol_identity=evidence_symbol.symbol_identity,
                disposition=FindingDisposition.UNRESOLVED,
                truth_source="unknown",
                code_evidence=code_evidence,
                doc_evidence=doc_evidence,
                reason="class symbols are outside the Stage 2 support matrix",
                kind="unsupported",
                component_id="symbol",
                old_value=claim.symbol_id,
                new_value=new_value,
                detector_id=StructuralDetector.id,
                detector_version=StructuralDetector.version,
                fingerprint=fingerprint,
                reason_code="unsupported.symbol_kind",
            )
        )
    return sorted(findings, key=finding_sort_key)


def _unsupported_provider_findings(
    issues: list[ProviderIssue],
    facts: list[CodeFact],
    before_facts: list[CodeFact],
    claims: list[DocClaim],
    *,
    repository_id: str,
) -> list[DriftFinding]:
    """Turn symbol-scoped provider rejections into stable V2 findings."""

    findings: dict[str, DriftFinding] = {}
    available_facts = [*facts, *before_facts]
    for issue in issues:
        matching_claims = sorted(
            (claim for claim in claims if claim.symbol_id == issue.symbol_id),
            key=lambda claim: (claim.anchor.path, claim.anchor.start_byte, claim.id),
        )
        claim = matching_claims[0] if matching_claims else None
        fact = next(
            (candidate for candidate in available_facts if candidate.symbol_id == issue.symbol_id),
            None,
        )
        identity = issue.symbol_identity or (fact.symbol_identity if fact is not None else None)
        if identity is None:
            continue
        code_anchor = fact.signature_anchor if fact is not None else None
        code_evidence = EvidenceAnchor(
            path=issue.path,
            line=issue.line,
            source_hash=issue.source_hash,
            start_byte=0 if code_anchor is None else code_anchor.start_byte,
            end_byte=0 if code_anchor is None else code_anchor.end_byte,
        )
        if claim is None:
            doc_evidence = EvidenceAnchor(
                path=issue.path,
                line=issue.line,
                source_hash=issue.source_hash,
                start_byte=0,
                end_byte=0,
            )
            old_value: object = {"type": "missing_claim"}
        else:
            doc_anchor = claim.value_anchor or claim.anchor
            doc_evidence = EvidenceAnchor(
                path=doc_anchor.path,
                line=doc_anchor.line,
                source_hash=doc_anchor.source_hash,
                start_byte=doc_anchor.start_byte,
                end_byte=doc_anchor.end_byte,
            )
            old_value = (
                {"type": "document_signature", "value": claim.signature}
                if len(matching_claims) == 1
                else {"type": "ambiguous_claims", "count": len(matching_claims)}
            )
        new_value = {"type": "unsupported_symbol", "reason": issue.reason_code}
        fingerprint = finding_fingerprint(
            repository_id=repository_id,
            symbol_identity=identity,
            kind="unsupported",
            component_id="symbol",
            old_value=old_value,
            new_value=new_value,
            code_evidence=code_evidence,
            doc_evidence=doc_evidence,
            detector_id=StructuralDetector.id,
            detector_version=StructuralDetector.version,
        )
        findings[fingerprint] = DriftFinding(
            id=f"finding_{fingerprint}",
            symbol_id=issue.symbol_id,
            disposition=FindingDisposition.UNRESOLVED,
            truth_source="unknown",
            code_evidence=code_evidence,
            doc_evidence=doc_evidence,
            reason=issue.message,
            kind="unsupported",
            component_id="symbol",
            old_value=old_value,
            new_value=new_value,
            detector_id=StructuralDetector.id,
            detector_version=StructuralDetector.version,
            fingerprint=fingerprint,
            reason_code=issue.reason_code,
        )
    return sorted(findings.values(), key=finding_sort_key)


def _symbol_kind_transition_findings(
    facts: list[CodeFact],
    before_facts: list[CodeFact],
    claims: list[DocClaim],
    *,
    repository_id: str,
) -> list[DriftFinding]:
    """Reject sync/async and supported-decorator identity transitions."""

    current_by_symbol = {fact.symbol_id: fact for fact in facts}
    before_by_symbol = {fact.symbol_id: fact for fact in before_facts}
    findings: list[DriftFinding] = []
    for claim in claims:
        current = current_by_symbol.get(claim.symbol_id)
        before = before_by_symbol.get(claim.symbol_id)
        if current is None or before is None:
            continue
        old_value = {
            "type": "symbol_kind",
            "category": before.category,
            "async": before.is_async,
            "decorator": before.decorator_kind,
        }
        new_value = {
            "type": "symbol_kind",
            "category": current.category,
            "async": current.is_async,
            "decorator": current.decorator_kind,
        }
        if old_value == new_value:
            continue
        code_anchor = current.signature_anchor
        doc_anchor = claim.value_anchor or claim.anchor
        code_evidence = EvidenceAnchor(
            path=current.path,
            line=current.line,
            source_hash=current.source_hash,
            start_byte=0 if code_anchor is None else code_anchor.start_byte,
            end_byte=0 if code_anchor is None else code_anchor.end_byte,
        )
        doc_evidence = EvidenceAnchor(
            path=doc_anchor.path,
            line=doc_anchor.line,
            source_hash=doc_anchor.source_hash,
            start_byte=doc_anchor.start_byte,
            end_byte=doc_anchor.end_byte,
        )
        identity = current.symbol_identity or current.symbol_id
        fingerprint = finding_fingerprint(
            repository_id=repository_id,
            symbol_identity=identity,
            kind="unsupported",
            component_id="symbol_kind",
            old_value=old_value,
            new_value=new_value,
            code_evidence=code_evidence,
            doc_evidence=doc_evidence,
            detector_id=StructuralDetector.id,
            detector_version=StructuralDetector.version,
        )
        findings.append(
            DriftFinding(
                id=f"finding_{fingerprint}",
                symbol_id=current.symbol_id,
                disposition=FindingDisposition.UNRESOLVED,
                truth_source="unknown",
                code_evidence=code_evidence,
                doc_evidence=doc_evidence,
                reason="symbol async/decorator category changed",
                kind="unsupported",
                component_id="symbol_kind",
                old_value=old_value,
                new_value=new_value,
                detector_id=StructuralDetector.id,
                detector_version=StructuralDetector.version,
                fingerprint=fingerprint,
                reason_code="unsupported.symbol_kind",
            )
        )
    return sorted(findings, key=finding_sort_key)


def _docstring_issue_findings(
    issues: list[ProviderIssue],
    facts: list[CodeFact],
    *,
    repository_id: str,
) -> list[DriftFinding]:
    """Surface isolated Google-docstring grammar failures conservatively."""

    facts_by_symbol = {fact.symbol_id: fact for fact in facts}
    findings: dict[str, DriftFinding] = {}
    for issue in issues:
        fact = facts_by_symbol.get(issue.symbol_id)
        if fact is None or fact.docstring_anchor is None:
            continue
        code_anchor = fact.signature_anchor
        doc_anchor = fact.docstring_anchor
        code_evidence = EvidenceAnchor(
            path=fact.path,
            line=fact.line,
            source_hash=fact.source_hash,
            start_byte=0 if code_anchor is None else code_anchor.start_byte,
            end_byte=0 if code_anchor is None else code_anchor.end_byte,
        )
        doc_evidence = EvidenceAnchor(
            path=doc_anchor.path,
            line=issue.line,
            source_hash=doc_anchor.source_hash,
            start_byte=doc_anchor.start_byte,
            end_byte=doc_anchor.end_byte,
        )
        old_value = {"type": "unsupported_docstring", "reason": issue.reason_code}
        new_value = {"type": "code_signature", "value": fact.signature}
        fingerprint = finding_fingerprint(
            repository_id=repository_id,
            symbol_identity=fact.symbol_identity or fact.symbol_id,
            kind="unsupported",
            component_id="docstring",
            old_value=old_value,
            new_value=new_value,
            code_evidence=code_evidence,
            doc_evidence=doc_evidence,
            detector_id=StructuralDetector.id,
            detector_version=StructuralDetector.version,
        )
        findings[fingerprint] = DriftFinding(
            id=f"finding_{fingerprint}",
            symbol_id=fact.symbol_id,
            disposition=FindingDisposition.UNRESOLVED,
            truth_source="unknown",
            code_evidence=code_evidence,
            doc_evidence=doc_evidence,
            reason=issue.message,
            kind="unsupported",
            component_id="docstring",
            old_value=old_value,
            new_value=new_value,
            detector_id=StructuralDetector.id,
            detector_version=StructuralDetector.version,
            fingerprint=fingerprint,
            reason_code=issue.reason_code,
        )
    return sorted(findings.values(), key=finding_sort_key)


def _finding_validity(
    finding: DriftFinding,
    repository_id: str,
) -> FindingValidityKey:
    return FindingValidityKey(
        repository_id=repository_id,
        symbol_id=finding.symbol_id,
        kind=finding.kind,
        component_id=finding.component_id,
        normalized_old=memory_canonical_json(finding.old_value),
        normalized_new=memory_canonical_json(finding.new_value),
        code_evidence_hash=finding.code_evidence.source_hash,
        doc_evidence_hash=finding.doc_evidence.source_hash,
        detector_id=finding.detector_id,
        detector_version=finding.detector_version,
        fingerprint=finding.fingerprint or finding.id,
    )


class AgentRuntime:
    def __init__(
        self,
        *,
        model_transport: ModelTransport | None = None,
    ) -> None:
        self.fact_provider = PythonFactProvider()
        self.ts_fact_provider = TypeScriptFactProvider()
        self.section_claim_provider = SectionClaimProvider()
        self.section_detector = SectionSemanticDetector()
        self.claim_provider = MarkdownClaimProvider()
        self.docstring_provider = GoogleDocstringClaimProvider()
        self.detector = StructuralDetector()
        self.executable_provider = ConfiguredExecutableExampleProvider()
        self.executable_detector = ExecutableExampleDetector()
        self.semantic_claim_provider = SemanticReturnClaimProvider()
        self.semantic_detector = ConstantReturnSemanticDetector()
        self.patcher = SignaturePatcher()
        self.planner = RepairPlanner()
        self.validation_runner = ValidationCommandRunner()
        self.model_transport = model_transport
        self.model_client: ModelClient | None = None
        self.budget_ledger: BudgetLedger | None = None
        self.committed_bundle: VerifiedRepairBundle | None = None
        self.identities: IdentitySet | None = None
        self.store: SQLiteStateStore | None = None
        self.run_service: RunService | None = None
        self.run_id: str | None = None
        self.initial_lock_observation: LockObservation | None = None

    def _semantic_model_client(self) -> ModelClient:
        """Create the budgeted client only for an eligible semantic repair."""

        if self.model_client is not None:
            return self.model_client
        if self.budget_ledger is None:
            raise RuntimeError("run budget was not initialized")
        transport = self.model_transport
        if transport is None:
            # Process environment is an explicit adapter boundary.  This does
            # not load the target repository's .env file.
            transport = OpenRouterTransport(OpenRouterSettings.from_environment())
        self.model_client = ModelClient(transport, self.budget_ledger)
        return self.model_client
    def _deadline_state(self, state: AgentState) -> AgentState:
        if state.get("budget_exhausted", False):
            return state
        if self.budget_ledger is None:
            raise RuntimeError("run budget was not initialized")
        try:
            self.budget_ledger.check_deadline()
        except BudgetExhausted as error:
            findings = [
                finding
                if finding.disposition is FindingDisposition.FIXED
                else finding.model_copy(
                    update={
                        "disposition": FindingDisposition.UNRESOLVED,
                        "reason_code": "budget_exhausted",
                        "reason": f"{finding.reason}; budget_exhausted",
                    }
                )
                for finding in state.get("findings", [])
            ]
            validation = [
                *state.get("validation", []),
                ValidationResult(
                    finding_ids=[finding.id for finding in findings],
                    attempt_id="run_budget",
                    check="run_deadline",
                    required=True,
                    status=ValidationStatus.UNAVAILABLE,
                    summary=f"budget_exhausted: {error.resource}",
                ),
            ]
            return {
                **state,
                "budget_exhausted": True,
                "findings": findings,
                "validation": validation,
            }
        return state

    def scope_node(self, state: AgentState) -> AgentState:
        request = state["request"]
        config, config_hash = load_config_with_hash(request.repo_path)
        resolver = GitScopeResolver(
            request.repo_path,
            config.project,
            since=request.scope.revision if request.scope.kind == "since" else None,
        )
        identities = resolve_identities(request.repo_path)
        state_path = resolve_state_path(
            request.repo_path,
            request.state_dir,
            identities=identities,
        )
        store = SQLiteStateStore(state_path)
        store.register_identities(identities)
        run_service = RunService(store)
        run_id = f"run_{uuid.uuid4().hex}"
        run_service.start(
            RunRecord(
                run_id=run_id,
                repository_id=identities.repository.digest,
                workspace_id=identities.workspace.digest,
                mode=request.mode.value,
                request={
                    "mode": request.mode.value,
                    "repo_path": str(request.repo_path),
                    "scope": request.scope.model_dump(mode="json", exclude_none=True),
                    "apply_policy": request.apply_policy,
                    "budgets": request.budgets.model_dump(mode="json"),
                    "state_dir": (None if request.state_dir is None else str(request.state_dir)),
                    "lock_timeout_seconds": request.lock_timeout_seconds,
                    "semantic_analysis": request.semantic_analysis,
                    "semantic_repair": request.semantic_repair,
                },
            )
        )
        manual_state_revision = store.manual_state_revision(identities.repository.digest)
        self.identities = identities
        self.store = store
        self.run_service = run_service
        self.run_id = run_id
        self.initial_lock_observation = probe_workspace_lock(identities.workspace.digest)
        git_changes = resolver.changes()
        changed = resolver.changed_paths()
        head_config = resolver.head_bytes("drift-agent.toml")
        config_changed = head_config is None or sha256_bytes(head_config) != config_hash
        path_renames = [
            (change.old_path, change.new_path)
            for change in git_changes
            if change.status == "R" and change.old_path is not None and change.new_path is not None
        ]
        for path in changed:
            repository_path_without_symlinks(
                request.repo_path,
                path,
                allow_missing=True,
            )
        return {
            "request": request,
            "config": config,
            "config_hash": config_hash,
            "changed_paths": changed,
            "repository_id": identities.repository.digest,
            "workspace_id": identities.workspace.digest,
            "runtime_context": {
                "git_changes": git_changes,
                "path_renames": path_renames,
                "state_path": state_path,
                "run_id": run_id,
                "manual_state_revision": manual_state_revision,
                "head_revision": resolver.head_revision(),
                "baseline_revision": resolver.baseline_revision(),
                "resolved_revision": resolver.resolved_revision(),
                "config_changed": config_changed,
            },
        }

    def evidence_node(self, state: AgentState) -> AgentState:
        request = state["request"]
        config = state["config"]
        changed = state["changed_paths"]
        context: dict[str, Any] = cast(dict[str, Any], state.get("runtime_context", {}))
        git_changes = context.get("git_changes", [])
        changed_python = {
            path
            for change in git_changes
            for path in (change.old_path, change.new_path)
            if path is not None and path.endswith(".py")
        } | {path for path in changed if path.endswith(".py")}
        changed_ts = {
            path
            for change in git_changes
            for path in (change.old_path, change.new_path)
            if path is not None and source_language(path) == "typescript"
        } | {path for path in changed if source_language(path) == "typescript"}
        changed_source = changed_python | changed_ts
        changed_docs = {path for path in changed if path.endswith(".md")}
        doc_paths = _documents(
            request.repo_path,
            config.project.docs_roots,
            config.project.include,
            config.project.exclude,
        )
        semantic_enabled = request.semantic_analysis or request.semantic_repair
        scanned_doc_hashes = (
            {
                path: sha256_file(repository_path_without_symlinks(request.repo_path, path))
                for path in doc_paths
            }
            if semantic_enabled
            else {}
        )
        all_claims = self.claim_provider.collect(request.repo_path, doc_paths)
        all_semantic_claims = (
            self.semantic_claim_provider.collect(
                request.repo_path,
                doc_paths,
                all_claims,
            )
            if semantic_enabled
            else []
        )
        changed_claims = [claim for claim in all_claims if claim.anchor.path in changed_docs]
        changed_semantic_claims = (
            all_semantic_claims
            if semantic_enabled and bool(context.get("config_changed", False))
            else [claim for claim in all_semantic_claims if claim.anchor.path in changed_docs]
        )
        # Single-segment references resolve only against TypeScript exports, so
        # they must not widen the Python scan gate (a prose backtick in a doc
        # edit would otherwise force a full-repo Python scan and, in semantic
        # mode, pull unrelated files into the staleness snapshot).
        changed_dotted_claims = [claim for claim in changed_claims if not claim.single_segment]
        python_paths = (
            _python_sources(
                request.repo_path,
                config.project.source_roots,
                config.project.include,
                config.project.exclude,
            )
            if changed_python or changed_dotted_claims or changed_semantic_claims
            else []
        )
        scanned_python_hashes = (
            {
                path: sha256_file(repository_path_without_symlinks(request.repo_path, path))
                for path in python_paths
            }
            if semantic_enabled
            else {}
        )
        ts_paths = (
            _typescript_sources(
                request.repo_path,
                config.project.source_roots,
                config.project.include,
                config.project.exclude,
            )
            if changed_ts or changed_claims or changed_semantic_claims
            else []
        )
        all_facts = self.fact_provider.collect(
            repo_path=request.repo_path,
            source_roots=_source_roots_for(
                python_paths,
                config.project.source_roots,
            ),
            changed_paths=python_paths,
        )
        current_provider_issues = list(self.fact_provider.issues)
        current_unsupported_symbols = list(self.fact_provider.unsupported_symbols)
        all_facts = [
            *all_facts,
            *self.ts_fact_provider.collect(
                repo_path=request.repo_path,
                source_roots=_source_roots_for(ts_paths, config.project.source_roots),
                changed_paths=ts_paths,
            ),
        ]
        current_provider_issues.extend(self.ts_fact_provider.issues)
        current_unsupported_symbols.extend(self.ts_fact_provider.unsupported_symbols)
        resolver = GitScopeResolver(
            request.repo_path,
            config.project,
            baseline_revision=str(context["baseline_revision"]),
            observed_head_revision=str(context["head_revision"]),
        )
        before_facts: list[CodeFact] = []
        before_unsupported_symbols: list[UnsupportedSymbolFact] = []
        for relative_path in sorted(changed_source):
            raw = resolver.head_bytes(relative_path)
            if raw is None:
                continue
            roots = [
                root
                for root in config.project.source_roots
                if PurePosixPath(relative_path).is_relative_to(PurePosixPath(root))
            ]
            if not roots:
                continue
            source_root = max(roots, key=len)
            provider = (
                self.ts_fact_provider
                if source_language(relative_path) == "typescript"
                else self.fact_provider
            )
            unsupported_start = len(provider.unsupported_symbols)
            before_facts.extend(
                provider.collect_bytes(
                    repo_path=request.repo_path,
                    source_root=source_root,
                    relative_path=relative_path,
                    raw=raw,
                    source_version="head",
                )
            )
            before_unsupported_symbols.extend(provider.unsupported_symbols[unsupported_start:])

        all_claims = _resolve_single_segment_claims(all_claims, [*all_facts, *before_facts])
        changed_claims = [claim for claim in all_claims if claim.anchor.path in changed_docs]

        affected_symbols = {claim.symbol_id for claim in changed_claims}
        affected_symbols.update(claim.symbol_id for claim in changed_semantic_claims)
        affected_symbols.update(fact.symbol_id for fact in all_facts if fact.path in changed_source)
        affected_symbols.update(fact.symbol_id for fact in before_facts)
        current_provider_issues = [
            issue
            for issue in current_provider_issues
            if issue.path in changed_source or issue.symbol_id in affected_symbols
        ]
        current_unsupported_symbols = [
            symbol
            for symbol in current_unsupported_symbols
            if symbol.path in changed_source or symbol.symbol_id in affected_symbols
        ]
        affected_symbols.update(
            issue.symbol_id for issue in current_provider_issues if issue.symbol_id
        )
        affected_symbols.update(
            symbol.symbol_id
            for symbol in [
                *current_unsupported_symbols,
                *before_unsupported_symbols,
            ]
        )
        affected_unsupported_symbols = {
            symbol.symbol_id
            for symbol in [
                *current_unsupported_symbols,
                *before_unsupported_symbols,
            ]
        }
        supported_symbol_ids = {fact.symbol_id for fact in all_facts}
        scoped_unsupported = [
            *current_unsupported_symbols,
            *before_unsupported_symbols,
        ]
        bridged_unsupported_claims = {
            claim.symbol_id
            for claim in all_claims
            if claim.symbol_id not in supported_symbol_ids
            and len(
                {
                    symbol.symbol_id
                    for symbol in scoped_unsupported
                    if symbol.symbol_identity.name == claim.symbol_id.rsplit(".", 1)[-1]
                }
            )
            == 1
        }
        affected_symbols.update(bridged_unsupported_claims)
        validated_alias_map: dict[str, str] = {}
        alias_invalidations: list[str] = []
        if self.store is not None and self.run_id is not None:
            alias_service = AliasService(self.store)
            for alias in alias_service.list(
                state.get("repository_id", ""),
                include_revoked=False,
            ):
                alias_claims = [
                    claim for claim in all_claims if claim.symbol_id == alias.evidence.old_symbol_id
                ]
                if not alias_claims:
                    continue
                targets = [
                    fact for fact in all_facts if fact.symbol_id == alias.evidence.new_symbol_id
                ]
                valid_claims = 0
                for claim in alias_claims:
                    matched_alias = alias_service.match(
                        request.repo_path,
                        AliasValidationContext(
                            repository_id=state.get("repository_id", ""),
                            old_symbol_id=alias.evidence.old_symbol_id,
                            new_symbol_id=alias.evidence.new_symbol_id,
                            old_evidence_hash=alias.evidence.old_evidence_hash,
                            new_evidence_hash=(targets[0].source_hash if len(targets) == 1 else ""),
                            doc_evidence_hash=claim.anchor.source_hash,
                            aligner_id="structural.alignment",
                            aligner_version="1",
                        ),
                        run_id=self.run_id,
                    )
                    alias_invalidations.extend(matched_alias.invalidation_reasons)
                    if matched_alias.alias is not None and len(targets) == 1:
                        valid_claims += 1
                # Alias evidence is symbol-wide in the detector.  It may only
                # enter closure when every current claim for that old symbol
                # validates against the same unique current target.
                if valid_claims == len(alias_claims):
                    validated_alias_map[alias.evidence.old_symbol_id] = alias.evidence.new_symbol_id
        while True:
            expanded = set(affected_symbols)
            expanded.update(
                claim.symbol_id for claim in all_claims if claim.symbol_id in affected_symbols
            )
            expanded.update(
                claim.symbol_id
                for claim in all_semantic_claims
                if claim.symbol_id in affected_symbols
            )
            expanded.update(
                fact.symbol_id for fact in all_facts if fact.symbol_id in affected_symbols
            )
            for old_symbol, new_symbol in validated_alias_map.items():
                if old_symbol in affected_symbols or new_symbol in affected_symbols:
                    expanded.update((old_symbol, new_symbol))
            if expanded == affected_symbols:
                break
            affected_symbols = expanded
        facts = [fact for fact in all_facts if fact.symbol_id in affected_symbols]
        claims = [
            claim
            for claim in all_claims
            if claim.symbol_id in affected_symbols or claim.symbol_id in bridged_unsupported_claims
        ]
        semantic_claims = [
            claim for claim in all_semantic_claims if claim.symbol_id in affected_symbols
        ]

        rename_map = _rename_alignment_map(
            before_facts,
            facts,
            context.get("path_renames", []),
        )
        manual_alias_symbols = {
            old_symbol for old_symbol in validated_alias_map if old_symbol not in rename_map
        }
        for old_symbol, new_symbol in validated_alias_map.items():
            rename_map.setdefault(old_symbol, new_symbol)
        facts = [fact for fact in all_facts if fact.symbol_id in affected_symbols]
        claims = [
            claim
            for claim in all_claims
            if claim.symbol_id in affected_symbols or claim.symbol_id in bridged_unsupported_claims
        ]
        semantic_claims = [
            claim for claim in all_semantic_claims if claim.symbol_id in affected_symbols
        ]
        issue_symbol_ids = {issue.symbol_id for issue in current_provider_issues if issue.symbol_id}
        detectable_claims = [
            claim
            for claim in claims
            if claim.symbol_id not in issue_symbol_ids
            and claim.symbol_id not in affected_unsupported_symbols
            and claim.symbol_id not in bridged_unsupported_claims
        ]
        symbol_findings, symbol_alignments = self.detector.detect_symbols(
            current_facts=facts,
            before_facts=before_facts,
            claims=detectable_claims,
            rename_map=rename_map,
            manual_aliases=manual_alias_symbols,
            repository_id=state.get("repository_id", ""),
        )
        transition_findings = _symbol_kind_transition_findings(
            facts,
            before_facts,
            detectable_claims,
            repository_id=state.get("repository_id", ""),
        )
        transition_symbols = {finding.symbol_id for finding in transition_findings}
        alignments = [
            alignment
            for alignment in symbol_alignments
            if alignment.claim.kind == "signature"
            and alignment.claim.extraction_error is None
            and alignment.fact.symbol_id not in transition_symbols
        ]
        findings = self.detector.detect(
            alignments,
            repository_id=state.get("repository_id", ""),
        )
        findings.extend(symbol_findings)
        findings.extend(transition_findings)
        findings.extend(
            _env_key_findings(
                request.repo_path,
                resolver,
                doc_paths,
                project=config.project,
                repository_id=state.get("repository_id", ""),
            )
        )
        if request.semantic_analysis:
            section_doc_paths = [
                *doc_paths,
                *[
                    path
                    for path in _root_markdown_paths(request.repo_path, config.project)
                    if path not in doc_paths
                ],
            ]
            section_claims = self.section_claim_provider.collect(
                request.repo_path, section_doc_paths
            )
            resolved_sections = _resolve_sections(
                request.repo_path,
                section_claims,
                all_facts,
                changed_source=changed_source,
                changed_docs=changed_docs,
                changed_ranges=resolver.changed_line_ranges(),
            )
            if resolved_sections:
                try:
                    section_client = self._semantic_model_client()
                except ModelConfigurationError:
                    section_client = None
                if section_client is not None:
                    section_findings, _section_calls = self.section_detector.detect(
                        resolved_sections,
                        section_client,
                        repository_id=state.get("repository_id", ""),
                        max_calls=request.budgets.max_model_calls_per_run,
                    )
                    findings.extend(section_findings)
        findings.extend(
            _unsupported_provider_findings(
                current_provider_issues,
                facts,
                before_facts,
                claims,
                repository_id=state.get("repository_id", ""),
            )
        )
        findings.extend(
            _unsupported_symbol_findings(
                current_unsupported_symbols,
                before_unsupported_symbols,
                [
                    claim
                    for claim in claims
                    if claim.symbol_id in affected_unsupported_symbols
                    or claim.symbol_id in bridged_unsupported_claims
                ],
                repository_id=state.get("repository_id", ""),
            )
        )

        docstring_facts = [
            fact
            for fact in facts
            if fact.source_version == "worktree" and fact.language == "python"
        ]
        docstring_claims = self.docstring_provider.collect(
            request.repo_path,
            docstring_facts,
        )
        docstring_issues = list(self.docstring_provider.issues)
        blocked_docstring_symbols = {issue.symbol_id for issue in docstring_issues}
        findings.extend(
            self.detector.detect_docstrings(
                [
                    fact
                    for fact in docstring_facts
                    if fact.symbol_id not in blocked_docstring_symbols
                ],
                [
                    claim
                    for claim in docstring_claims
                    if claim.symbol_id not in blocked_docstring_symbols
                ],
                repository_id=state.get("repository_id", ""),
            )
        )
        findings.extend(
            _docstring_issue_findings(
                docstring_issues,
                docstring_facts,
                repository_id=state.get("repository_id", ""),
            )
        )

        semantic_alignment = align_semantic_returns(facts, semantic_claims)
        findings.extend(
            self.semantic_detector.detect(
                list(semantic_alignment.alignments),
                repository_id=state.get("repository_id", ""),
            )
        )
        semantic_validation = [
            ValidationResult(
                finding_ids=[],
                attempt_id=issue.id,
                check="semantic_alignment",
                required=True,
                status=ValidationStatus.UNAVAILABLE,
                summary=f"{issue.reason_code}: {issue.summary}",
            )
            for issue in semantic_alignment.issues
        ]

        claim_index = {
            (claim.symbol_id, claim.anchor.path, claim.anchor.start_byte): claim
            for claim in [*claims, *docstring_claims, *semantic_claims]
        }
        truth_applied: list[DriftFinding] = []
        for finding in findings:
            if finding.disposition is not FindingDisposition.DETECTED:
                truth_applied.append(finding)
                continue
            matching_claim = claim_index.get(
                (
                    finding.symbol_id,
                    finding.doc_evidence.path,
                    finding.doc_evidence.start_byte,
                )
            )
            if matching_claim is None:
                same_anchor_claims = [
                    claim
                    for claim in [*claims, *docstring_claims, *semantic_claims]
                    if claim.anchor.path == finding.doc_evidence.path
                    and claim.anchor.start_byte == finding.doc_evidence.start_byte
                ]
                matching_claim = same_anchor_claims[0] if len(same_anchor_claims) == 1 else None
            if matching_claim is None:
                matching_claim = next(
                    (
                        claim
                        for claim in [*claims, *docstring_claims, *semantic_claims]
                        if claim.anchor.path == finding.doc_evidence.path
                        and claim.symbol_id in {finding.symbol_id, str(finding.old_value)}
                    ),
                    None,
                )
            if matching_claim is not None:
                truth = _claim_truth(matching_claim, config.truth)
            elif finding.detector_id == "structural.config_keys":
                # Config-key findings have no doc claim; classify by doc path.
                truth = _path_truth(finding.doc_evidence.path, config.truth)
            else:
                truth = "unknown"
            truth_applied.append(_apply_truth_policy(finding, truth))
        findings = truth_applied
        repository_id = state.get("repository_id", "")
        findings.extend(_ambiguity_findings(facts, claims, repository_id=repository_id))
        findings.extend(_fact_ambiguity_findings(facts, claims, repository_id=repository_id))
        findings.extend(_extraction_findings(facts, claims, repository_id=repository_id))
        validation_target_hashes: dict[str, str] = {}
        validation_targets: list[str] = []
        for source in config.validation.commands:
            try:
                compiled = self.validation_runner.compile(source)
            except CommandCompileError:
                continue
            for target in compiled.targets:
                try:
                    target_path = repository_path_without_symlinks(
                        request.repo_path,
                        target,
                        allow_missing=True,
                    )
                except UnsafeInputPathError:
                    continue
                if not target_path.exists():
                    validation_target_hashes[target] = MISSING_INPUT_HASH
                    validation_targets.append(target)
                elif target_path.is_file():
                    validation_target_hashes[target] = sha256_file(target_path)
                    validation_targets.append(target)
        validation_input_hashes: dict[str, str] = {}
        validation_manifest_unstable = False
        if config.validation.commands:
            try:
                validation_input_hashes = validation_input_manifest(request.repo_path)
            except OSError:
                validation_manifest_unstable = True
        expected_hash_entries = [
            ("drift-agent.toml", state["config_hash"]),
            *scanned_doc_hashes.items(),
            *scanned_python_hashes.items(),
            *((fact.path, fact.source_hash) for fact in facts),
            *(
                (issue.path, issue.source_hash)
                for issue in current_provider_issues
                if issue.source_hash
            ),
            *((symbol.path, symbol.source_hash) for symbol in current_unsupported_symbols),
            *(
                (claim.anchor.path, claim.anchor.source_hash)
                for claim in [*claims, *docstring_claims, *semantic_claims]
            ),
            *validation_target_hashes.items(),
            *validation_input_hashes.items(),
        ]
        expected_hashes: dict[str, str] = {}
        evidence_hash_conflict = False
        for path, digest in expected_hash_entries:
            existing = expected_hashes.get(path)
            if existing is not None and existing != digest:
                evidence_hash_conflict = True
                continue
            expected_hashes[path] = digest
        snapshot_paths = [
            "drift-agent.toml",
            *changed,
            *scanned_doc_hashes,
            *scanned_python_hashes,
            *(fact.path for fact in facts),
            *(issue.path for issue in current_provider_issues),
            *(symbol.path for symbol in current_unsupported_symbols),
            *(claim.anchor.path for claim in claims),
            *(claim.anchor.path for claim in docstring_claims),
            *(claim.anchor.path for claim in semantic_claims),
            *validation_targets,
            *validation_input_hashes,
        ]
        sampling_stale = validation_manifest_unstable or evidence_hash_conflict
        try:
            snapshot = resolver.snapshot(
                snapshot_paths,
                expected_hashes=expected_hashes,
            )
        except SnapshotChangedError as error:
            snapshot = error.snapshot
            sampling_stale = True
            findings = [
                finding.model_copy(
                    update={
                        "disposition": FindingDisposition.UNRESOLVED,
                        "reason_code": "global_snapshot_changed",
                        "reason": f"{finding.reason}; global_snapshot_changed",
                    }
                )
                if finding.disposition is not FindingDisposition.FIXED
                else finding
                for finding in findings
            ]
        snapshot = snapshot.model_copy(update={"validation_input_hashes": validation_input_hashes})
        if config.validation.commands and not sampling_stale:
            try:
                manifest_still_current = (
                    validation_input_manifest(request.repo_path) == validation_input_hashes
                )
            except OSError:
                manifest_still_current = False
            if not manifest_still_current:
                sampling_stale = True
                findings = [
                    finding.model_copy(
                        update={
                            "disposition": FindingDisposition.UNRESOLVED,
                            "reason_code": "global_snapshot_changed",
                            "reason": f"{finding.reason}; global_snapshot_changed",
                        }
                    )
                    if finding.disposition is not FindingDisposition.FIXED
                    else finding
                    for finding in findings
                ]
        if set(resolver.changed_paths()) != set(changed) or not _git_changes_are_current(
            request.repo_path,
            config,
            str(context["head_revision"]),
            str(context["baseline_revision"]),
            git_changes,
            set(),
        ):
            sampling_stale = True
            findings = [
                finding.model_copy(
                    update={
                        "disposition": FindingDisposition.UNRESOLVED,
                        "reason_code": "global_snapshot_changed",
                        "reason": f"{finding.reason}; global_snapshot_changed",
                    }
                )
                if finding.disposition is not FindingDisposition.FIXED
                else finding
                for finding in findings
            ]
        check_state: AgentState = {
            **state,
            "snapshot": snapshot,
            "findings": findings,
            "validation": [*state.get("validation", []), *semantic_validation],
            "validation_incomplete": (
                state.get("validation_incomplete", False) or bool(semantic_validation)
            ),
            "stale": sampling_stale or state.get("stale", False),
        }
        if check_state.get("stale", False):
            check_state = _global_snapshot_changed_state(check_state)
        check_state = self._deadline_state(check_state)
        initial_observation = self.initial_lock_observation
        if (
            request.mode is RunMode.CHECK
            and initial_observation is not None
            and initial_observation.active_writer
            and not check_state.get("stale", False)
        ):
            check_state = _global_snapshot_changed_state(check_state)
        else:
            check_state = self._run_check_executable_detection(check_state)
        if not check_state.get("stale", False) and not check_state.get("failed", False):
            check_inputs_current = (
                _snapshot_inputs_are_current(
                    request.repo_path,
                    config,
                    snapshot,
                    set(),
                )
                and _scope_paths_are_current(
                    request.repo_path,
                    config,
                    changed,
                    set(),
                    expected_head=str(context["head_revision"]),
                    baseline_revision=str(context["baseline_revision"]),
                )
                and _git_changes_are_current(
                    request.repo_path,
                    config,
                    str(context["head_revision"]),
                    str(context["baseline_revision"]),
                    git_changes,
                    set(),
                )
            )
            if not check_inputs_current:
                check_state = _global_snapshot_changed_state(check_state)
        findings = check_state.get("findings", [])
        sampling_stale = check_state.get("stale", False)
        check_validation = check_state.get("validation", [])
        run_service = self.run_service
        store = self.store
        run_id = self.run_id
        if run_service is None or store is None or run_id is None:
            raise RuntimeError("persistent run context was not initialized")
        run_service.event(
            run_id,
            "snapshot_captured",
            {"snapshot": snapshot.model_dump(mode="json")},
        )
        run_service.event(
            run_id,
            "facts_collected",
            {
                "facts": len(facts),
                "claims": len(claims) + len(docstring_claims) + len(semantic_claims),
                "semantic_claims": len(semantic_claims),
                "semantic_issues": len(semantic_validation),
                "executable_checks": sum(
                    result.check.startswith("check_") for result in check_validation
                ),
            },
        )
        for finding in findings:
            store.record_finding(
                PersistedFinding(
                    run_id=run_id,
                    finding_id=finding.id,
                    validity=_finding_validity(
                        finding,
                        state.get("repository_id", ""),
                    ),
                )
            )
        for alignment in symbol_alignments:
            if alignment.method != "git_rename" or alignment.old_symbol_id is None:
                continue
            old_fact = next(
                (fact for fact in before_facts if fact.symbol_id == alignment.old_symbol_id),
                None,
            )
            if old_fact is None:
                continue
            old_blob = resolver.head_blob(old_fact.path)
            if old_blob is None:
                continue
            evidence = AlignmentEvidenceKey(
                repository_id=state.get("repository_id", ""),
                old_symbol_id=alignment.old_symbol_id,
                new_symbol_id=alignment.fact.symbol_id,
                confirmation_commit=snapshot.head_revision,
                old_blob_id=old_blob,
                old_evidence_hash=old_fact.source_hash,
                new_evidence_hash=alignment.fact.source_hash,
                doc_evidence_hash=alignment.claim.anchor.source_hash,
                aligner_id="structural.alignment",
                aligner_version="1",
            )
            alignment_id = f"alignment_{uuid.uuid5(uuid.NAMESPACE_URL, evidence.digest)}"
            store.record_alignment(
                PersistedAlignment(
                    run_id=run_id,
                    alignment_id=alignment_id,
                    evidence=evidence,
                )
            )
        run_service.event(
            run_id,
            "findings_detected",
            {"count": len(findings)},
        )

        decision_service = DecisionService(store)
        active_findings: list[DriftFinding] = []
        suppressions: list[SuppressionRecord] = []
        suppressed_findings: list[DriftFinding] = []
        memory_events: list[BundleMemoryEvent] = []
        memory_events.extend(
            BundleMemoryEvent(
                kind="alias_invalidated",
                record_id="",
                reason=reason,
            )
            for reason in alias_invalidations
        )
        for finding in findings:
            matched = decision_service.match(
                _finding_validity(finding, state.get("repository_id", "")),
                run_id=run_id,
            )
            if matched.decision is None:
                active_findings.append(finding)
                memory_events.extend(
                    BundleMemoryEvent(
                        kind="decision_invalidated",
                        record_id="",
                        reason=reason,
                    )
                    for reason in matched.invalidation_reasons
                )
                continue
            decision = matched.decision
            suppressed_findings.append(finding)
            suppressions.append(
                SuppressionRecord(
                    decision_id=decision.decision_id,
                    finding_id=finding.id,
                    action=decision.action,
                    reason=decision.reason,
                    actor=decision.actor,
                    confirmation="human_confirmed",
                    evidence_key=decision.validity.digest,
                    created_at=decision.created_at,
                    revoked_at=decision.revoked_at,
                )
            )
        findings = active_findings
        run_service.event(
            run_id,
            "decisions_applied",
            {"suppressed": len(suppressions)},
        )
        next_state: AgentState = {
            **check_state,
            "stale": sampling_stale or state.get("stale", False),
            "facts": facts,
            "before_facts": before_facts,
            "claims": claims,
            "semantic_claims": semantic_claims,
            "semantic_alignments": list(semantic_alignment.alignments),
            "docstring_claims": docstring_claims,
            "alignments": alignments,
            "findings": sorted(findings, key=finding_sort_key),
            "snapshot": snapshot,
            "runtime_context": {
                **context,
                "rename_map": rename_map,
                "symbol_alignments": symbol_alignments,
                "suppressions": suppressions,
                "suppressed_findings": suppressed_findings,
                "memory_events": memory_events,
                "validation_targets": validation_targets,
            },
        }
        return self._deadline_state(next_state)

    def plan_node(self, state: AgentState) -> AgentState:
        repairable = sorted(
            (
                finding
                for finding in state["findings"]
                if finding.disposition is FindingDisposition.DETECTED
            ),
            key=lambda finding: (finding.symbol_id, finding.id),
        )
        context: dict[str, Any] = cast(dict[str, Any], state.get("runtime_context", {}))
        all_alignments = [
            *state.get("alignments", []),
            *context.get("symbol_alignments", []),
        ]
        docstring_claims = state.get("docstring_claims", [])
        semantic_alignments = state.get("semantic_alignments", [])
        facts_by_symbol = {fact.symbol_id: fact for fact in state.get("facts", [])}
        attempts: list[PatchAttempt] = []
        semantic_candidates: list[SemanticRepairCandidate] = []
        unplanned: set[str] = set()
        unplanned_reasons: dict[str, str] = {}
        signature_kinds = {
            "parameter_added",
            "parameter_removed",
            "parameter_order_changed",
            "parameter_kind_changed",
            "parameter_annotation_changed",
            "parameter_requiredness_changed",
            "parameter_default_changed",
            "return_annotation_changed",
        }
        signature_alignments: dict[tuple[str, str, int], Alignment] = {}
        for alignment in all_alignments:
            if alignment.claim.kind != "signature":
                continue
            key = (
                alignment.fact.symbol_id,
                alignment.claim.anchor.path,
                alignment.claim.anchor.start_byte,
            )
            signature_alignments.setdefault(key, alignment)
        safe_signature_keys: set[tuple[str, str, int]] = set()
        for key, alignment in signature_alignments.items():
            all_component_ids = {
                finding.id
                for finding in self.detector.detect(
                    [alignment],
                    repository_id=state.get("repository_id", ""),
                )
                if finding.kind in signature_kinds
            }
            active_component_ids = {
                finding.id
                for finding in state["findings"]
                if finding.disposition is FindingDisposition.DETECTED
                and finding.kind in signature_kinds
                and (
                    finding.symbol_id,
                    finding.doc_evidence.path,
                    finding.doc_evidence.start_byte,
                )
                == key
            }
            if all_component_ids and all_component_ids == active_component_ids:
                safe_signature_keys.add(key)
        for finding in repairable:
            semantic_candidate: SemanticRepairCandidate | None = None
            signature_alignment = signature_alignments.get(
                (
                    finding.symbol_id,
                    finding.doc_evidence.path,
                    finding.doc_evidence.start_byte,
                )
            )
            matching_alignment = next(
                (
                    alignment
                    for alignment in all_alignments
                    if alignment.fact.symbol_id == finding.symbol_id
                    and alignment.claim.anchor.path == finding.doc_evidence.path
                ),
                None,
            )
            proposed: list[PatchAttempt] = []
            if finding.kind in signature_kinds and signature_alignment is not None:
                signature_key = (
                    signature_alignment.fact.symbol_id,
                    signature_alignment.claim.anchor.path,
                    signature_alignment.claim.anchor.start_byte,
                )
                if signature_key in safe_signature_keys:
                    proposed = [
                        self.patcher.propose(
                            state["request"].repo_path,
                            signature_alignment,
                            finding,
                        )
                    ]
            elif finding.kind.startswith("symbol_reference_"):
                symbol_alignment = next(
                    (
                        alignment
                        for alignment in all_alignments
                        if alignment.claim.anchor.path == finding.doc_evidence.path
                        and alignment.claim.symbol_id == str(finding.old_value)
                    ),
                    matching_alignment,
                )
                if symbol_alignment is not None:
                    proposed = propose_symbol_patch(
                        state["request"].repo_path,
                        symbol_alignment.claim,
                        finding,
                    )
            elif finding.kind.startswith("docstring_"):
                claim = next(
                    (
                        item
                        for item in docstring_claims
                        if item.symbol_id == finding.symbol_id
                        and item.kind
                        == (
                            "docstring_return"
                            if finding.kind == "docstring_return_changed"
                            else "docstring_parameter"
                        )
                        and item.component_id == finding.component_id
                    ),
                    None,
                )
                fact = facts_by_symbol.get(finding.symbol_id)
                if claim is not None and fact is not None:
                    proposed = propose_docstring_patch(
                        state["request"].repo_path,
                        Alignment(fact=fact, claim=claim),
                        finding,
                    )
            elif finding.type == "semantic_drift" and state["request"].semantic_repair:
                try:
                    semantic_candidate = SemanticRepairCandidate.from_unique_alignment(
                        semantic_alignments,
                        finding,
                    )
                except SemanticRepairBoundaryError:
                    unplanned_reasons[finding.id] = "semantic_alignment_unavailable"
            if proposed:
                attempts.extend(proposed)
            elif semantic_candidate is not None:
                semantic_candidates.append(semantic_candidate)
            else:
                unplanned.add(finding.id)

        findings = [
            finding.model_copy(
                update={
                    "disposition": FindingDisposition.UNRESOLVED,
                    "reason_code": unplanned_reasons.get(
                        finding.id,
                        "unsupported.literal",
                    ),
                    "reason": (
                        f"{finding.reason}; "
                        f"{unplanned_reasons.get(finding.id, 'no deterministic repair anchor')}"
                    ),
                }
            )
            if finding.id in unplanned
            else finding
            for finding in state["findings"]
        ]
        plan = self.planner.plan(findings, attempts)
        if self.run_service is not None and self.run_id is not None:
            self.run_service.event(
                self.run_id,
                "repair_planned",
                {"groups": len(plan.groups) + len(semantic_candidates)},
            )
        return {
            **state,
            "findings": findings,
            "attempts": [attempt for group in plan.groups for attempt in group.attempts],
            "runtime_context": {
                **context,
                "repair_plan": plan,
                "semantic_repair_candidates": semantic_candidates,
            },
        }

    def _final_closure_findings(
        self,
        state: AgentState,
        groups: list[RepairGroup],
    ) -> list[DriftFinding]:
        """Re-extract and redetect every symbol touched by retained groups."""

        repo = state["request"].repo_path
        config = state["config"]
        context: dict[str, Any] = cast(dict[str, Any], state.get("runtime_context", {}))
        target_findings = {finding_id for group in groups for finding_id in group.finding_ids}
        touched = [finding for finding in state["findings"] if finding.id in target_findings]
        symbols = {finding.symbol_id for finding in touched}
        for finding in touched:
            for value in (finding.old_value, finding.new_value):
                if isinstance(value, str) and _looks_like_fqn(value):
                    symbols.add(value)
        rename_map = cast(dict[str, str], context.get("rename_map", {}))
        for old_symbol, new_symbol in rename_map.items():
            if old_symbol in symbols or new_symbol in symbols:
                symbols.update((old_symbol, new_symbol))

        doc_paths = _documents(
            repo,
            config.project.docs_roots,
            config.project.include,
            config.project.exclude,
        )
        all_claims = self.claim_provider.collect(repo, doc_paths)
        semantic_targets = [finding for finding in touched if finding.type == "semantic_drift"]
        all_semantic_claims = (
            self.semantic_claim_provider.collect(repo, doc_paths, all_claims)
            if semantic_targets
            else []
        )
        while True:
            expanded = set(symbols)
            expanded.update(claim.symbol_id for claim in all_claims if claim.symbol_id in symbols)
            expanded.update(
                claim.symbol_id for claim in all_semantic_claims if claim.symbol_id in symbols
            )
            if expanded == symbols:
                break
            symbols = expanded

        python_paths = _python_sources(
            repo,
            config.project.source_roots,
            config.project.include,
            config.project.exclude,
        )
        ts_paths = _typescript_sources(
            repo,
            config.project.source_roots,
            config.project.include,
            config.project.exclude,
        )
        all_facts = self.fact_provider.collect(
            repo_path=repo,
            source_roots=_source_roots_for(python_paths, config.project.source_roots),
            changed_paths=python_paths,
        )
        current_issues = list(self.fact_provider.issues)
        current_unsupported = list(self.fact_provider.unsupported_symbols)
        all_facts = [
            *all_facts,
            *self.ts_fact_provider.collect(
                repo_path=repo,
                source_roots=_source_roots_for(ts_paths, config.project.source_roots),
                changed_paths=ts_paths,
            ),
        ]
        current_issues.extend(self.ts_fact_provider.issues)
        current_unsupported.extend(self.ts_fact_provider.unsupported_symbols)
        all_claims = _resolve_single_segment_claims(all_claims, all_facts)
        facts = [fact for fact in all_facts if fact.symbol_id in symbols]
        claims = [claim for claim in all_claims if claim.symbol_id in symbols]
        semantic_claims = [claim for claim in all_semantic_claims if claim.symbol_id in symbols]

        resolver = GitScopeResolver(
            repo,
            config.project,
            baseline_revision=str(context["baseline_revision"]),
            observed_head_revision=str(context["head_revision"]),
        )
        changed_source = {
            path
            for change in context.get("git_changes", [])
            for path in (change.old_path, change.new_path)
            if path is not None and source_language(path) is not None
        }
        before_facts: list[CodeFact] = []
        before_unsupported: list[UnsupportedSymbolFact] = []
        for relative_path in sorted(changed_source):
            raw = resolver.head_bytes(relative_path)
            if raw is None:
                continue
            roots = [
                root
                for root in config.project.source_roots
                if PurePosixPath(relative_path).is_relative_to(PurePosixPath(root))
            ]
            if not roots:
                continue
            provider = (
                self.ts_fact_provider
                if source_language(relative_path) == "typescript"
                else self.fact_provider
            )
            unsupported_start = len(provider.unsupported_symbols)
            before_facts.extend(
                provider.collect_bytes(
                    repo_path=repo,
                    source_root=max(roots, key=len),
                    relative_path=relative_path,
                    raw=raw,
                    source_version="head",
                )
            )
            before_unsupported.extend(provider.unsupported_symbols[unsupported_start:])
        before_facts = [fact for fact in before_facts if fact.symbol_id in symbols]
        before_unsupported = [fact for fact in before_unsupported if fact.symbol_id in symbols]
        unsupported_symbols = {
            item.symbol_id
            for item in [*current_unsupported, *before_unsupported]
            if item.symbol_id in symbols
        }
        issue_symbols = {issue.symbol_id for issue in current_issues if issue.symbol_id in symbols}
        detectable_claims = [
            claim
            for claim in claims
            if claim.symbol_id not in issue_symbols and claim.symbol_id not in unsupported_symbols
        ]
        symbol_findings, symbol_alignments = self.detector.detect_symbols(
            current_facts=facts,
            before_facts=before_facts,
            claims=detectable_claims,
            rename_map=rename_map,
            repository_id=state.get("repository_id", ""),
        )
        transition_findings = _symbol_kind_transition_findings(
            facts,
            before_facts,
            detectable_claims,
            repository_id=state.get("repository_id", ""),
        )
        transition_symbols = {finding.symbol_id for finding in transition_findings}
        alignments = [
            alignment
            for alignment in symbol_alignments
            if alignment.claim.kind == "signature"
            and alignment.claim.extraction_error is None
            and alignment.fact.symbol_id not in transition_symbols
        ]
        findings = [
            *self.detector.detect(
                alignments,
                repository_id=state.get("repository_id", ""),
            ),
            *symbol_findings,
            *transition_findings,
            *_env_key_findings(
                repo,
                resolver,
                doc_paths,
                project=config.project,
                repository_id=state.get("repository_id", ""),
            ),
            *_unsupported_provider_findings(
                [issue for issue in current_issues if issue.symbol_id in symbols],
                facts,
                before_facts,
                claims,
                repository_id=state.get("repository_id", ""),
            ),
            *_unsupported_symbol_findings(
                [item for item in current_unsupported if item.symbol_id in symbols],
                before_unsupported,
                claims,
                repository_id=state.get("repository_id", ""),
            ),
            *_ambiguity_findings(
                facts,
                claims,
                repository_id=state.get("repository_id", ""),
            ),
            *_fact_ambiguity_findings(
                facts,
                claims,
                repository_id=state.get("repository_id", ""),
            ),
            *_extraction_findings(
                facts,
                claims,
                repository_id=state.get("repository_id", ""),
            ),
        ]

        docstring_facts = [
            fact for fact in facts if fact.symbol_id in symbols and fact.language == "python"
        ]
        docstring_claims = self.docstring_provider.collect(repo, docstring_facts)
        docstring_issues = list(self.docstring_provider.issues)
        blocked_docstrings = {issue.symbol_id for issue in docstring_issues}
        findings.extend(
            self.detector.detect_docstrings(
                [fact for fact in docstring_facts if fact.symbol_id not in blocked_docstrings],
                [claim for claim in docstring_claims if claim.symbol_id not in blocked_docstrings],
                repository_id=state.get("repository_id", ""),
            )
        )
        findings.extend(
            _docstring_issue_findings(
                docstring_issues,
                docstring_facts,
                repository_id=state.get("repository_id", ""),
            )
        )

        if semantic_targets:
            semantic_result = align_semantic_returns(facts, semantic_claims)
            semantic_findings = self.semantic_detector.detect(
                list(semantic_result.alignments),
                repository_id=state.get("repository_id", ""),
            )
            aligned_targets = {
                (
                    alignment.fact.symbol_id,
                    alignment.claim.anchor.path,
                    alignment.claim.component_id,
                )
                for alignment in semantic_result.alignments
            }
            # A semantic target is not closed merely because the patched text
            # stopped matching the tiny claim grammar.  The exact local claim
            # must remain uniquely aligned after every retained edit.
            for target in semantic_targets:
                target_key = (
                    target.symbol_id,
                    target.doc_evidence.path,
                    target.component_id,
                )
                if target_key not in aligned_targets:
                    semantic_findings.append(target)
            findings.extend(semantic_findings)

        final_claims = [*claims, *docstring_claims, *semantic_claims]
        truth_applied: list[DriftFinding] = []
        for finding in findings:
            if finding.disposition is not FindingDisposition.DETECTED:
                truth_applied.append(finding)
                continue
            matching = next(
                (
                    claim
                    for claim in final_claims
                    if claim.symbol_id in {finding.symbol_id, str(finding.old_value)}
                    and claim.anchor.path == finding.doc_evidence.path
                ),
                None,
            )
            truth_applied.append(
                _apply_truth_policy(
                    finding,
                    _claim_truth(matching, config.truth) if matching is not None else "unknown",
                )
            )
        if self.store is not None:
            decision_service = DecisionService(self.store)
            truth_applied = [
                finding
                for finding in truth_applied
                if decision_service.match(
                    _finding_validity(finding, state.get("repository_id", "")),
                    run_id=self.run_id,
                    audit_mismatches=False,
                ).decision
                is None
            ]
        return _exclude_run_suppressions(state, truth_applied)

    def _run_check_executable_detection(self, state: AgentState) -> AgentState:
        """Run configured examples as read-only check evidence.

        A runner-confirmed test failure becomes a broken-example finding.
        Configuration, environment, timeout, and budget failures never
        masquerade as drift; they make the check incomplete instead.
        """

        if (
            state["request"].mode is not RunMode.CHECK
            or not state["config"].validation.commands
            or state.get("stale", False)
            or state.get("failed", False)
            or state.get("budget_exhausted", False)
        ):
            return state
        if self.budget_ledger is None:
            raise RuntimeError("run budget was not initialized")

        collection = self.executable_provider.collect(
            state["request"].repo_path,
            state["config"].validation.commands,
            snapshot=state["snapshot"],
            config_hash=state["config_hash"],
            compiler=self.validation_runner.compile,
        )
        findings = list(state.get("findings", []))
        validation = list(state.get("validation", []))
        validation_incomplete = state.get("validation_incomplete", False)

        def budget_state(resource: str, attempt_id: str) -> AgentState:
            exhausted_findings = [
                finding
                if finding.disposition is FindingDisposition.FIXED
                else finding.model_copy(
                    update={
                        "disposition": FindingDisposition.UNRESOLVED,
                        "reason_code": "budget_exhausted",
                        "reason": f"{finding.reason}; budget_exhausted",
                    }
                )
                for finding in findings
            ]
            return {
                **state,
                "findings": exhausted_findings,
                "validation": [
                    *validation,
                    ValidationResult(
                        finding_ids=[finding.id for finding in exhausted_findings],
                        attempt_id=attempt_id,
                        check="check_validation_budget",
                        required=True,
                        status=ValidationStatus.UNAVAILABLE,
                        summary=f"budget_exhausted: {resource}",
                    ),
                ],
                "validation_incomplete": True,
                "budget_exhausted": True,
            }

        for entry in collection.entries:
            if isinstance(entry, ExecutableExampleIssue):
                validation.append(
                    ValidationResult(
                        finding_ids=[],
                        attempt_id=entry.attempt_id,
                        check="check_validation_command",
                        required=True,
                        status=ValidationStatus.UNAVAILABLE,
                        summary=entry.summary,
                    )
                )
                validation_incomplete = True
                continue
            if not isinstance(entry, ExecutableExample):
                raise TypeError("unexpected executable example provider entry")
            try:
                self.budget_ledger.reserve_validation_command()
            except BudgetExhausted as error:
                return budget_state(error.resource, entry.attempt_id)
            remaining_seconds = self.budget_ledger.remaining_seconds()
            if remaining_seconds <= 0:
                return budget_state("timeout_seconds", entry.attempt_id)
            try:
                result = self.validation_runner.run(
                    state["request"].repo_path,
                    entry.command,
                    finding_ids=[],
                    attempt_id=entry.attempt_id,
                    timeout_seconds=remaining_seconds,
                    network=state["config"].validation.network,
                    expected_input_hashes=state["snapshot"].validation_input_hashes,
                ).model_copy(update={"check": f"check_{entry.kind}"})
            except ValidationInputChangedError as error:
                validation.append(
                    ValidationResult(
                        finding_ids=[],
                        attempt_id=entry.attempt_id,
                        check="check_target_snapshot",
                        required=True,
                        status=ValidationStatus.UNAVAILABLE,
                        summary=f"global_snapshot_changed: {error}",
                    )
                )
                stale_state = _global_snapshot_changed_state(
                    {
                        **state,
                        "findings": findings,
                        "validation": validation,
                        "validation_incomplete": True,
                    }
                )
                return {**stale_state, "validation": validation}
            try:
                self.budget_ledger.check_deadline()
            except BudgetExhausted as error:
                validation.append(
                    result.model_copy(
                        update={
                            "status": ValidationStatus.UNAVAILABLE,
                            "summary": f"budget_exhausted: {error.resource}",
                        }
                    )
                )
                findings = [
                    finding
                    if finding.disposition is FindingDisposition.FIXED
                    else finding.model_copy(
                        update={
                            "disposition": FindingDisposition.UNRESOLVED,
                            "reason_code": "budget_exhausted",
                            "reason": f"{finding.reason}; budget_exhausted",
                        }
                    )
                    for finding in findings
                ]
                return {
                    **state,
                    "findings": findings,
                    "validation": validation,
                    "validation_incomplete": True,
                    "budget_exhausted": True,
                }
            finding = self.executable_detector.detect(
                entry,
                result,
                repository_id=state.get("repository_id", ""),
            )
            if finding is not None:
                findings.append(finding)
                result = result.model_copy(update={"finding_ids": [finding.id]})
            elif result.status is ValidationStatus.UNAVAILABLE:
                validation_incomplete = True
            validation.append(result)

        return {
            **state,
            "findings": sorted(findings, key=finding_sort_key),
            "validation": validation,
            "validation_incomplete": validation_incomplete,
        }

    def _preflight_configured_validation(
        self,
        state: AgentState,
        group: RepairGroup,
    ) -> tuple[ValidationResult | None, str | None]:
        commands = state["config"].validation.commands
        if not commands:
            return None, None
        if self.budget_ledger is None:
            raise RuntimeError("run budget was not initialized")
        try:
            for source in commands:
                self.validation_runner.compile(source)
        except CommandCompileError as error:
            return (
                ValidationResult(
                    finding_ids=group.finding_ids,
                    attempt_id=group.attempts[0].id,
                    check="group_validation_command",
                    required=True,
                    status=ValidationStatus.UNAVAILABLE,
                    summary=f"validation_unavailable: {error}",
                ),
                "validation_unavailable",
            )
        try:
            # A retained group must still have enough capacity for the mandatory
            # final-snapshot pass.  Keep that capacity available before writing.
            self.budget_ledger.ensure_validation_capacity(len(commands) * 2)
        except BudgetExhausted as error:
            return (
                ValidationResult(
                    finding_ids=group.finding_ids,
                    attempt_id=group.attempts[0].id,
                    check="group_validation_budget",
                    required=True,
                    status=ValidationStatus.UNAVAILABLE,
                    summary=f"budget_exhausted: {error.resource}",
                ),
                "budget_exhausted",
            )
        return None, None

    def _run_configured_validation(
        self,
        state: AgentState,
        *,
        finding_ids: list[str],
        attempt_id: str,
        phase: Literal["group", "final"],
        mutable_paths: set[str],
    ) -> tuple[list[ValidationResult], str | None]:
        """Run the repository's allowlisted required commands within run budgets."""

        commands = state["config"].validation.commands
        if not commands:
            return [], None
        if self.budget_ledger is None:
            raise RuntimeError("run budget was not initialized")

        results: list[ValidationResult] = []
        for source in commands:
            try:
                compiled = self.validation_runner.compile(source)
            except CommandCompileError as error:
                results.append(
                    ValidationResult(
                        finding_ids=finding_ids,
                        attempt_id=attempt_id,
                        check=f"{phase}_validation_command",
                        required=True,
                        status=ValidationStatus.UNAVAILABLE,
                        summary=f"validation_unavailable: {error}",
                    )
                )
                return results, "validation_unavailable"

            try:
                self.budget_ledger.reserve_validation_command()
            except BudgetExhausted as error:
                results.append(
                    ValidationResult(
                        finding_ids=finding_ids,
                        attempt_id=attempt_id,
                        check=f"{phase}_validation_budget",
                        required=True,
                        status=ValidationStatus.UNAVAILABLE,
                        summary=f"budget_exhausted: {error.resource}",
                    )
                )
                return results, "budget_exhausted"

            remaining_seconds = self.budget_ledger.remaining_seconds()
            if remaining_seconds <= 0:
                results.append(
                    ValidationResult(
                        finding_ids=finding_ids,
                        attempt_id=attempt_id,
                        check=f"{phase}_validation_budget",
                        required=True,
                        status=ValidationStatus.UNAVAILABLE,
                        summary="budget_exhausted: timeout_seconds",
                    )
                )
                return results, "budget_exhausted"
            expected_input_hashes = dict(state["snapshot"].validation_input_hashes)
            if expected_input_hashes:
                current_input_hashes = validation_input_manifest(state["request"].repo_path)
                differing_paths = {
                    path
                    for path in current_input_hashes.keys() | expected_input_hashes.keys()
                    if current_input_hashes.get(path) != expected_input_hashes.get(path)
                }
                unexpected_changes = differing_paths - mutable_paths
                if unexpected_changes:
                    raise ValidationInputChangedError(sorted(unexpected_changes)[0])
                for path in mutable_paths:
                    if path in current_input_hashes:
                        expected_input_hashes[path] = current_input_hashes[path]
                    else:
                        expected_input_hashes.pop(path, None)
            result = self.validation_runner.run(
                state["request"].repo_path,
                compiled,
                finding_ids=finding_ids,
                attempt_id=attempt_id,
                timeout_seconds=remaining_seconds,
                network=state["config"].validation.network,
                expected_input_hashes=expected_input_hashes,
            ).model_copy(update={"check": f"{phase}_{compiled.kind}"})
            try:
                self.budget_ledger.check_deadline()
            except BudgetExhausted as error:
                results.append(
                    result.model_copy(
                        update={
                            "status": ValidationStatus.UNAVAILABLE,
                            "summary": f"budget_exhausted: {error.resource}",
                        }
                    )
                )
                return results, "budget_exhausted"
            results.append(result)
            if result.status is ValidationStatus.FAILED:
                return results, "validation_failed"
            if result.status is not ValidationStatus.PASSED:
                return results, "validation_unavailable"
        return results, None

    def _validate_group(
        self,
        state: AgentState,
        group: RepairGroup,
        transaction: WorkspaceTransaction,
    ) -> ValidationResult:
        repo = state["request"].repo_path
        findings_by_id = {finding.id: finding for finding in state["findings"]}
        group_findings = [findings_by_id[item] for item in group.finding_ids]
        attempt = group.attempts[0]
        kinds = {finding.kind for finding in group_findings}
        if any(finding.type == "semantic_drift" for finding in group_findings):
            if not all(finding.type == "semantic_drift" for finding in group_findings):
                return ValidationResult(
                    finding_ids=group.finding_ids,
                    attempt_id=attempt.id,
                    check="semantic_redetect",
                    required=True,
                    status=ValidationStatus.FAILED,
                    summary="semantic repair group mixed incompatible finding types",
                )
            declarations = self.claim_provider.collect(repo, [attempt.path])
            semantic_claims = self.semantic_claim_provider.collect(
                repo,
                [attempt.path],
                declarations,
            )
            semantic_result = align_semantic_returns(
                state.get("facts", []),
                semantic_claims,
            )
            target_keys = {
                (
                    finding.symbol_id,
                    finding.doc_evidence.path,
                    finding.component_id,
                )
                for finding in group_findings
            }
            aligned_keys = {
                (
                    alignment.fact.symbol_id,
                    alignment.claim.anchor.path,
                    alignment.claim.component_id,
                )
                for alignment in semantic_result.alignments
            }
            uniquely_realigned = target_keys <= aligned_keys
            redetected = self.semantic_detector.detect(
                list(semantic_result.alignments),
                repository_id=state.get("repository_id", ""),
            )
            redetected = _exclude_run_suppressions(state, redetected)
            baseline = [
                finding
                for finding in state["findings"]
                if finding.type == "semantic_drift" and finding.doc_evidence.path == attempt.path
            ]
            passed = uniquely_realigned and _finding_delta_is_clean(
                baseline=baseline,
                remaining=redetected,
                targets=group_findings,
            )
            return ValidationResult(
                finding_ids=group.finding_ids,
                attempt_id=attempt.id,
                check="semantic_redetect",
                required=True,
                status=ValidationStatus.PASSED if passed else ValidationStatus.FAILED,
                summary=(
                    "semantic drift removed and claim remained uniquely aligned"
                    if passed
                    else "semantic drift remains, is ambiguous, or introduced a new finding"
                ),
            )
        if any(kind.startswith("docstring_") for kind in kinds):
            original = transaction.original_bytes(attempt.path)
            current = repository_path_without_symlinks(repo, attempt.path).read_bytes()
            if original is None or not docstring_ast_unchanged(original, current):
                return ValidationResult(
                    finding_ids=group.finding_ids,
                    attempt_id=attempt.id,
                    check="docstring_ast_guard",
                    required=True,
                    status=ValidationStatus.FAILED,
                    summary="docstring patch changed executable Python structure",
                )
            roots = _source_roots_for([attempt.path], state["config"].project.source_roots)
            reparsed_facts = self.fact_provider.collect(
                repo_path=repo,
                source_roots=roots,
                changed_paths=[attempt.path],
            )
            reparsed_claims = self.docstring_provider.collect(repo, reparsed_facts)
            reparsed_issues = list(self.docstring_provider.issues)
            blocked_symbols = {issue.symbol_id for issue in reparsed_issues}
            remaining = self.detector.detect_docstrings(
                [fact for fact in reparsed_facts if fact.symbol_id not in blocked_symbols],
                [claim for claim in reparsed_claims if claim.symbol_id not in blocked_symbols],
                repository_id=state.get("repository_id", ""),
            )
            remaining.extend(
                _docstring_issue_findings(
                    reparsed_issues,
                    reparsed_facts,
                    repository_id=state.get("repository_id", ""),
                )
            )
            target_symbols = {finding.symbol_id for finding in group_findings}
            scoped_remaining = _exclude_run_suppressions(
                state,
                [finding for finding in remaining if finding.symbol_id in target_symbols],
            )
            baseline = [
                finding
                for finding in state["findings"]
                if finding.symbol_id in target_symbols
                and (finding.kind.startswith("docstring_") or finding.component_id == "docstring")
            ]
            failed = not _finding_delta_is_clean(
                baseline=baseline,
                remaining=scoped_remaining,
                targets=group_findings,
            )
            return ValidationResult(
                finding_ids=group.finding_ids,
                attempt_id=attempt.id,
                check="docstring_redetect",
                required=True,
                status=(ValidationStatus.FAILED if failed else ValidationStatus.PASSED),
                summary=(
                    "docstring drift remains or a new finding appeared"
                    if failed
                    else "docstring drift removed and AST guard passed"
                ),
            )
        if any(kind.startswith("symbol_reference_") for kind in kinds):
            reparsed = self.claim_provider.collect(repo, [attempt.path])
            remaining_symbols = {claim.symbol_id for claim in reparsed}
            passed = True
            for finding in group_findings:
                if finding.kind == "symbol_reference_deleted":
                    passed = passed and str(finding.old_value) not in remaining_symbols
                else:
                    passed = passed and str(finding.new_value) in remaining_symbols
            symbol_findings, symbol_alignments = self.detector.detect_symbols(
                current_facts=state["facts"],
                before_facts=state.get("before_facts", []),
                claims=reparsed,
                repository_id=state.get("repository_id", ""),
            )
            signature_alignments = [
                alignment
                for alignment in symbol_alignments
                if alignment.method == "exact_fqn"
                and alignment.claim.kind == "signature"
                and alignment.claim.extraction_error is None
            ]
            redetected = [
                *symbol_findings,
                *self.detector.detect(
                    signature_alignments,
                    repository_id=state.get("repository_id", ""),
                ),
            ]
            redetected = _exclude_run_suppressions(state, redetected)
            baseline = [
                finding
                for finding in state["findings"]
                if finding.doc_evidence.path == attempt.path
                and not finding.kind.startswith("docstring_")
                and finding.component_id != "docstring"
            ]
            passed = passed and _finding_delta_is_clean(
                baseline=baseline,
                remaining=redetected,
                targets=group_findings,
            )
            return ValidationResult(
                finding_ids=group.finding_ids,
                attempt_id=attempt.id,
                check="symbol_redetect",
                required=True,
                status=ValidationStatus.PASSED if passed else ValidationStatus.FAILED,
                summary="symbol drift removed" if passed else "symbol drift remains",
            )

        all_reparsed_claims = self.claim_provider.collect(repo, [attempt.path])
        reparsed_claims = [
            claim
            for claim in all_reparsed_claims
            if claim.extraction_error is None and claim.kind == "signature"
        ]
        reparsed_alignments = align_exact(state["facts"], reparsed_claims)
        symbols = {finding.symbol_id for finding in group_findings}
        attempt_alignments = [
            alignment for alignment in reparsed_alignments if alignment.fact.symbol_id in symbols
        ]
        if {alignment.fact.symbol_id for alignment in attempt_alignments} != symbols:
            return ValidationResult(
                finding_ids=group.finding_ids,
                attempt_id=attempt.id,
                check="drift_redetect",
                required=True,
                status=ValidationStatus.FAILED,
                summary="repaired claim could not be uniquely re-aligned",
            )
        detector_result = self.detector.validate(attempt_alignments, attempt)
        if detector_result.status is not ValidationStatus.PASSED:
            return detector_result
        redetected = [
            *self.detector.detect(
                reparsed_alignments,
                repository_id=state.get("repository_id", ""),
            ),
            *_ambiguity_findings(
                state["facts"],
                all_reparsed_claims,
                repository_id=state.get("repository_id", ""),
            ),
            *_fact_ambiguity_findings(
                state["facts"],
                all_reparsed_claims,
                repository_id=state.get("repository_id", ""),
            ),
            *_extraction_findings(
                state["facts"],
                all_reparsed_claims,
                repository_id=state.get("repository_id", ""),
            ),
        ]
        redetected = _exclude_run_suppressions(state, redetected)
        baseline = [
            finding
            for finding in state["findings"]
            if finding.doc_evidence.path == attempt.path
            and not finding.kind.startswith("docstring_")
            and finding.component_id != "docstring"
        ]
        passed = _finding_delta_is_clean(
            baseline=baseline,
            remaining=redetected,
            targets=group_findings,
        )
        return ValidationResult(
            finding_ids=group.finding_ids,
            attempt_id=attempt.id,
            check="drift_redetect",
            required=True,
            status=ValidationStatus.PASSED if passed else ValidationStatus.FAILED,
            summary=(
                "signature drift removed without new findings"
                if passed
                else "signature drift remains or a new finding appeared"
            ),
        )

    def apply_validate_node(self, state: AgentState) -> AgentState:
        repo = state["request"].repo_path
        transaction = WorkspaceTransaction(repo)
        validation: list[ValidationResult] = []
        context: dict[str, Any] = cast(dict[str, Any], state.get("runtime_context", {}))
        plan = context.get("repair_plan")
        groups: list[RepairGroup] = [] if plan is None else list(plan.groups)
        semantic_candidates = cast(
            list[SemanticRepairCandidate],
            context.get("semantic_repair_candidates", []),
        )
        planned_group_ids = [
            *(candidate.group_id for candidate in semantic_candidates),
            *(group.id for group in groups),
        ]
        findings_by_id = {finding.id: finding for finding in state["findings"]}
        retained_attempts: list[PatchAttempt] = []
        retained_groups: list[RepairGroup] = []
        outcomes: list[RepairGroupOutcome] = []
        rolled_back_group_ids: set[str] = set()
        patched_paths: set[str] = set()
        lock: WorkspaceLock | None = None

        def rollback_retained_for_budget(error: BudgetExhausted) -> BaseException | None:
            nonlocal outcomes, patched_paths, retained_attempts, retained_groups, state
            affected_groups = list(retained_groups)
            if not affected_groups:
                return None
            affected_group_ids = {group.id for group in affected_groups}
            try:
                transaction.rollback_attempts(
                    {attempt.id for group in affected_groups for attempt in group.attempts}
                )
                rolled_back_group_ids.update(affected_group_ids)
            except BaseException as rollback_error:
                return rollback_error
            finding_ids = sorted(
                {finding_id for group in affected_groups for finding_id in group.finding_ids}
            )
            validation.append(
                ValidationResult(
                    finding_ids=finding_ids,
                    attempt_id="publish_budget",
                    check="publish_deadline",
                    required=True,
                    status=ValidationStatus.UNAVAILABLE,
                    summary=f"budget_exhausted: {error.resource}",
                )
            )
            for finding_id in finding_ids:
                finding = findings_by_id[finding_id]
                findings_by_id[finding_id] = finding.model_copy(
                    update={
                        "disposition": FindingDisposition.UNRESOLVED,
                        "reason_code": "budget_exhausted",
                        "reason": f"{finding.reason}; budget_exhausted",
                    }
                )
            outcomes = [
                outcome.model_copy(
                    update={
                        "disposition": FindingDisposition.UNRESOLVED,
                        "reason_code": "budget_exhausted",
                    }
                )
                if outcome.id in affected_group_ids
                else outcome
                for outcome in outcomes
            ]
            state = {**state, "budget_exhausted": True}
            retained_groups = []
            retained_attempts = []
            patched_paths = set()
            return None

        def record_publication_aborted(
            reason_code: str,
            *,
            rollback_failed: bool = False,
        ) -> None:
            if self.run_service is None or self.run_id is None:
                return
            self.run_service.event(
                self.run_id,
                "publication_aborted",
                {
                    "reason_code": reason_code,
                    "rollback_failed": rollback_failed,
                },
            )

        def apply_semantic_group(group: RepairGroup) -> str | None:
            """Apply and validate one already-bounded semantic patch attempt.

            A non-None result is a stable failure reason.  Any applied bytes
            are rolled back before that reason is returned.
            """

            preflight_result, preflight_reason = self._preflight_configured_validation(
                state,
                group,
            )
            if preflight_reason is not None:
                if preflight_result is not None:
                    validation.append(preflight_result)
                return preflight_reason
            if self.budget_ledger is None:
                raise RuntimeError("run budget was not initialized")
            try:
                self.budget_ledger.reserve_patch_attempt(group.finding_ids)
            except BudgetExhausted as error:
                validation.append(
                    ValidationResult(
                        finding_ids=group.finding_ids,
                        attempt_id=group.attempts[0].id,
                        check="patch_attempt_budget",
                        required=True,
                        status=ValidationStatus.UNAVAILABLE,
                        summary=f"budget_exhausted: {error.resource}",
                    )
                )
                return "budget_exhausted"

            applied_ids: set[str] = set()
            try:
                for attempt in sorted(
                    group.attempts,
                    key=lambda item: (item.path, -item.start_byte, item.id),
                ):
                    transaction.apply(attempt)
                    applied_ids.add(attempt.id)
            except StaleWorkspaceError:
                if applied_ids:
                    transaction.rollback_attempts(applied_ids)
                    rolled_back_group_ids.add(group.id)
                return "precondition_changed"

            result = self._validate_group(state, group, transaction)
            validation.append(result)
            failure_reason: str | None
            if result.status is ValidationStatus.PASSED:
                command_results, failure_reason = self._run_configured_validation(
                    state,
                    finding_ids=group.finding_ids,
                    attempt_id=group.attempts[0].id,
                    phase="group",
                    mutable_paths={
                        attempt.path for attempt in [*retained_attempts, *group.attempts]
                    },
                )
                validation.extend(command_results)
            elif result.status is ValidationStatus.UNAVAILABLE:
                failure_reason = "validation_unavailable"
            else:
                failure_reason = "validation_failed"
            if failure_reason is None:
                return None
            transaction.rollback_attempts({attempt.id for attempt in group.attempts})
            rolled_back_group_ids.add(group.id)
            return failure_reason

        def semantic_model_reason(error: ModelClientError) -> str:
            if error.reason_code == "invalid_structured_output":
                return "model_schema_invalid"
            if error.reason_code == "accounting_incomplete" or (
                isinstance(error.usage, ModelTokenUsage)
                and not isinstance(error.usage, ModelCallUsage)
            ):
                return "accounting_incomplete"
            return "model_unavailable"

        def record_semantic_model_stop(
            candidate: SemanticRepairCandidate,
            reason_code: str,
        ) -> None:
            validation.append(
                ValidationResult(
                    finding_ids=[candidate.finding_id],
                    attempt_id=f"model_{candidate.group_id}",
                    check="semantic_model_proposal",
                    required=True,
                    status=ValidationStatus.UNAVAILABLE,
                    summary=reason_code,
                )
            )

        def stop_semantic_candidate(
            candidate: SemanticRepairCandidate,
            reason_code: str,
        ) -> None:
            nonlocal state
            finding = findings_by_id[candidate.finding_id]
            findings_by_id[candidate.finding_id] = finding.model_copy(
                update={
                    "disposition": FindingDisposition.UNRESOLVED,
                    "reason_code": reason_code,
                    "reason": f"{finding.reason}; {reason_code}",
                }
            )
            outcomes.append(
                RepairGroupOutcome(
                    id=candidate.group_id,
                    finding_ids=[candidate.finding_id],
                    disposition=FindingDisposition.UNRESOLVED,
                    reason_code=reason_code,
                    path=candidate.literal_anchor.path,
                    start_byte=candidate.literal_anchor.start_byte,
                    end_byte=candidate.literal_anchor.end_byte,
                )
            )
            if reason_code == "budget_exhausted":
                state = {**state, "budget_exhausted": True}

        def retain_semantic_group(group: RepairGroup) -> None:
            for finding_id in group.finding_ids:
                finding = findings_by_id[finding_id]
                findings_by_id[finding_id] = finding.model_copy(
                    update={
                        "disposition": FindingDisposition.FIXED,
                        "reason_code": "validated",
                    }
                )
            retained_attempts.extend(group.attempts)
            retained_groups.append(group)
            outcomes.append(
                RepairGroupOutcome(
                    id=group.id,
                    finding_ids=group.finding_ids,
                    disposition=FindingDisposition.FIXED,
                    reason_code="validated",
                    path=group.path,
                    start_byte=group.start_byte,
                    end_byte=group.end_byte,
                )
            )

        try:
            if self.identities is None or self.run_id is None:
                raise RuntimeError("repair lock identity was not initialized")
            if self.budget_ledger is None:
                raise RuntimeError("run budget was not initialized")
            lock = WorkspaceLock.from_identities(
                self.identities,
                run_id=self.run_id,
                timeout_seconds=min(
                    state["request"].lock_timeout_seconds,
                    self.budget_ledger.remaining_seconds(),
                ),
            )
            try:
                owner = lock.acquire()
            except LockTimeoutError as error:
                if self.budget_ledger.remaining_seconds() == 0:
                    raise BudgetExhausted("timeout_seconds") from error
                raise
            if self.run_service is not None:
                self.run_service.event(
                    self.run_id,
                    "lock_acquired",
                    {"generation": owner.generation},
                )
            try:
                self.budget_ledger.check_deadline()
            except BudgetExhausted as error:
                for candidate in semantic_candidates:
                    finding = findings_by_id[candidate.finding_id]
                    findings_by_id[candidate.finding_id] = finding.model_copy(
                        update={
                            "disposition": FindingDisposition.UNRESOLVED,
                            "reason_code": "budget_exhausted",
                            "reason": f"{finding.reason}; budget_exhausted",
                        }
                    )
                    outcomes.append(
                        RepairGroupOutcome(
                            id=candidate.group_id,
                            finding_ids=[candidate.finding_id],
                            disposition=FindingDisposition.UNRESOLVED,
                            reason_code="budget_exhausted",
                            path=candidate.literal_anchor.path,
                            start_byte=candidate.literal_anchor.start_byte,
                            end_byte=candidate.literal_anchor.end_byte,
                        )
                    )
                for group in groups:
                    for finding_id in group.finding_ids:
                        finding = findings_by_id[finding_id]
                        findings_by_id[finding_id] = finding.model_copy(
                            update={
                                "disposition": FindingDisposition.UNRESOLVED,
                                "reason_code": "budget_exhausted",
                                "reason": f"{finding.reason}; budget_exhausted",
                            }
                        )
                    outcomes.append(
                        RepairGroupOutcome(
                            id=group.id,
                            finding_ids=group.finding_ids,
                            disposition=FindingDisposition.UNRESOLVED,
                            reason_code="budget_exhausted",
                            path=group.path,
                            start_byte=group.start_byte,
                            end_byte=group.end_byte,
                        )
                    )
                return {
                    **state,
                    "budget_exhausted": True,
                    "findings": [findings_by_id[finding.id] for finding in state["findings"]],
                    "attempts": [],
                    "validation": [
                        ValidationResult(
                            finding_ids=[
                                *(candidate.finding_id for candidate in semantic_candidates),
                                *(
                                    finding_id
                                    for group in groups
                                    for finding_id in group.finding_ids
                                ),
                            ],
                            attempt_id="run_budget",
                            check="run_deadline",
                            required=True,
                            status=ValidationStatus.UNAVAILABLE,
                            summary=f"budget_exhausted: {error.resource}",
                        )
                    ],
                    "runtime_context": {
                        **context,
                        "repair_outcomes": outcomes,
                    },
                }
            if not (
                _snapshot_inputs_are_current(
                    repo,
                    state["config"],
                    state["snapshot"],
                    set(),
                )
                and _state_scope_paths_are_current(repo, state, set())
                and _state_git_changes_are_current(repo, state, set())
            ):
                return {
                    **_global_snapshot_changed_state(state),
                    "validation": validation,
                }

            for candidate in sorted(
                semantic_candidates,
                key=lambda item: (
                    item.literal_anchor.path,
                    item.literal_anchor.start_byte,
                    item.group_id,
                ),
            ):
                if self.run_service is not None and self.run_id is not None:
                    self.run_service.event(
                        self.run_id,
                        "group_started",
                        {"group_id": candidate.group_id},
                    )
                if state.get("budget_exhausted", False):
                    record_semantic_model_stop(candidate, "budget_exhausted")
                    stop_semantic_candidate(candidate, "budget_exhausted")
                    continue
                try:
                    client = self._semantic_model_client()
                    if self.budget_ledger is None:
                        raise RuntimeError("run budget was not initialized")
                    session = SemanticRepairSession(
                        client,
                        candidate,
                        timeout_seconds=max(
                            0.001,
                            self.budget_ledger.remaining_seconds(),
                        ),
                    )
                except ModelConfigurationError:
                    record_semantic_model_stop(candidate, "model_not_configured")
                    stop_semantic_candidate(candidate, "model_not_configured")
                    continue

                selected_profile: Literal["fast", "strong"] = "fast"
                try:
                    proposal = session.propose("fast")
                except BudgetExhausted:
                    record_semantic_model_stop(candidate, "budget_exhausted")
                    stop_semantic_candidate(candidate, "budget_exhausted")
                    continue
                except ModelClientError as error:
                    reason_code = semantic_model_reason(error)
                    record_semantic_model_stop(candidate, reason_code)
                    stop_semantic_candidate(candidate, reason_code)
                    continue

                if proposal.decision == "abstain":
                    record_semantic_model_stop(candidate, "model_abstained")
                    stop_semantic_candidate(candidate, "model_abstained")
                    continue
                if proposal.confidence == "low":
                    selected_profile = "strong"
                    try:
                        proposal = session.propose("strong")
                    except BudgetExhausted:
                        record_semantic_model_stop(candidate, "budget_exhausted")
                        stop_semantic_candidate(candidate, "budget_exhausted")
                        continue
                    except ModelClientError as error:
                        reason_code = semantic_model_reason(error)
                        record_semantic_model_stop(candidate, reason_code)
                        stop_semantic_candidate(candidate, reason_code)
                        continue
                    if proposal.decision == "abstain":
                        record_semantic_model_stop(candidate, "model_abstained")
                        stop_semantic_candidate(candidate, "model_abstained")
                        continue
                    if proposal.confidence != "high":
                        record_semantic_model_stop(candidate, "model_low_confidence")
                        stop_semantic_candidate(candidate, "model_low_confidence")
                        continue

                try:
                    first_attempt = build_semantic_attempt(
                        repo,
                        candidate,
                        proposal,
                        1,
                        transaction_base=transaction.original_bytes(candidate.literal_anchor.path),
                    )
                except SemanticRepairBoundaryError as error:
                    reason_code = (
                        error.reason_code
                        if error.reason_code
                        in {
                            "model_abstained",
                            "model_low_confidence",
                            "precondition_changed",
                        }
                        else "model_output_unsafe"
                    )
                    record_semantic_model_stop(candidate, reason_code)
                    stop_semantic_candidate(candidate, reason_code)
                    continue
                semantic_group = RepairGroup(
                    id=candidate.group_id,
                    finding_ids=[candidate.finding_id],
                    attempts=[first_attempt],
                )
                first_failure = apply_semantic_group(semantic_group)
                if first_failure is None:
                    retain_semantic_group(semantic_group)
                    continue

                if first_failure == "validation_failed" and selected_profile == "fast":
                    if self.budget_ledger is None:
                        raise RuntimeError("run budget was not initialized")
                    try:
                        self.budget_ledger.ensure_patch_attempt_capacity(candidate.finding_id)
                    except BudgetExhausted:
                        record_semantic_model_stop(candidate, "budget_exhausted")
                        stop_semantic_candidate(candidate, "budget_exhausted")
                        continue
                    try:
                        strong_proposal = session.propose("strong")
                    except BudgetExhausted:
                        record_semantic_model_stop(candidate, "budget_exhausted")
                        stop_semantic_candidate(candidate, "budget_exhausted")
                        continue
                    except ModelClientError as error:
                        reason_code = semantic_model_reason(error)
                        record_semantic_model_stop(candidate, reason_code)
                        stop_semantic_candidate(candidate, reason_code)
                        continue
                    if strong_proposal.decision == "abstain":
                        record_semantic_model_stop(candidate, "model_abstained")
                        stop_semantic_candidate(candidate, "model_abstained")
                        continue
                    if strong_proposal.confidence != "high":
                        record_semantic_model_stop(candidate, "model_low_confidence")
                        stop_semantic_candidate(candidate, "model_low_confidence")
                        continue
                    try:
                        second_attempt = build_semantic_attempt(
                            repo,
                            candidate,
                            strong_proposal,
                            2,
                            transaction_base=transaction.original_bytes(
                                candidate.literal_anchor.path
                            ),
                        )
                    except SemanticRepairBoundaryError as error:
                        reason_code = (
                            "precondition_changed"
                            if error.reason_code == "precondition_changed"
                            else "model_output_unsafe"
                        )
                        record_semantic_model_stop(candidate, reason_code)
                        stop_semantic_candidate(candidate, reason_code)
                        continue
                    retry_group = RepairGroup(
                        id=candidate.group_id,
                        finding_ids=[candidate.finding_id],
                        attempts=[second_attempt],
                    )
                    retry_failure = apply_semantic_group(retry_group)
                    if retry_failure is None:
                        retain_semantic_group(retry_group)
                        continue
                    first_failure = (
                        "semantic_validation_failed"
                        if retry_failure == "validation_failed"
                        else retry_failure
                    )
                elif first_failure == "validation_failed":
                    first_failure = "semantic_validation_failed"

                stop_semantic_candidate(candidate, first_failure)

            for group in groups:
                if self.run_service is not None and self.run_id is not None:
                    self.run_service.event(
                        self.run_id,
                        "group_started",
                        {"group_id": group.id},
                    )
                if state.get("budget_exhausted", False):
                    for finding_id in group.finding_ids:
                        finding = findings_by_id[finding_id]
                        findings_by_id[finding_id] = finding.model_copy(
                            update={
                                "disposition": FindingDisposition.UNRESOLVED,
                                "reason_code": "budget_exhausted",
                                "reason": f"{finding.reason}; budget_exhausted",
                            }
                        )
                    outcomes.append(
                        RepairGroupOutcome(
                            id=group.id,
                            finding_ids=group.finding_ids,
                            disposition=FindingDisposition.UNRESOLVED,
                            reason_code="budget_exhausted",
                            path=group.path,
                            start_byte=group.start_byte,
                            end_byte=group.end_byte,
                        )
                    )
                    continue
                if group.conflict_key:
                    reason_code = f"conflict.{group.conflict_key}"
                    for finding_id in group.finding_ids:
                        finding = findings_by_id[finding_id]
                        findings_by_id[finding_id] = finding.model_copy(
                            update={
                                "disposition": FindingDisposition.UNRESOLVED,
                                "reason_code": reason_code,
                                "reason": f"{finding.reason}; {reason_code}",
                            }
                        )
                    outcomes.append(
                        RepairGroupOutcome(
                            id=group.id,
                            finding_ids=group.finding_ids,
                            disposition=FindingDisposition.UNRESOLVED,
                            reason_code=reason_code,
                            path=group.path,
                            start_byte=group.start_byte,
                            end_byte=group.end_byte,
                        )
                    )
                    continue
                preflight_result, preflight_reason = self._preflight_configured_validation(
                    state, group
                )
                if preflight_reason is not None:
                    if preflight_reason == "budget_exhausted":
                        state = {**state, "budget_exhausted": True}
                    if preflight_result is not None:
                        validation.append(preflight_result)
                    for finding_id in group.finding_ids:
                        finding = findings_by_id[finding_id]
                        findings_by_id[finding_id] = finding.model_copy(
                            update={
                                "disposition": FindingDisposition.UNRESOLVED,
                                "reason_code": preflight_reason,
                                "reason": f"{finding.reason}; {preflight_reason}",
                            }
                        )
                    outcomes.append(
                        RepairGroupOutcome(
                            id=group.id,
                            finding_ids=group.finding_ids,
                            disposition=FindingDisposition.UNRESOLVED,
                            reason_code=preflight_reason,
                            path=group.path,
                            start_byte=group.start_byte,
                            end_byte=group.end_byte,
                        )
                    )
                    continue
                if self.budget_ledger is None:
                    raise RuntimeError("run budget was not initialized")
                try:
                    self.budget_ledger.reserve_patch_attempt(group.finding_ids)
                except BudgetExhausted as error:
                    state = {**state, "budget_exhausted": True}
                    for finding_id in group.finding_ids:
                        finding = findings_by_id[finding_id]
                        findings_by_id[finding_id] = finding.model_copy(
                            update={
                                "disposition": FindingDisposition.UNRESOLVED,
                                "reason_code": "budget_exhausted",
                                "reason": f"{finding.reason}; budget_exhausted",
                            }
                        )
                    validation.append(
                        ValidationResult(
                            finding_ids=group.finding_ids,
                            attempt_id=group.attempts[0].id,
                            check="patch_attempt_budget",
                            required=True,
                            status=ValidationStatus.UNAVAILABLE,
                            summary=f"budget_exhausted: {error.resource}",
                        )
                    )
                    outcomes.append(
                        RepairGroupOutcome(
                            id=group.id,
                            finding_ids=group.finding_ids,
                            disposition=FindingDisposition.UNRESOLVED,
                            reason_code="budget_exhausted",
                            path=group.path,
                            start_byte=group.start_byte,
                            end_byte=group.end_byte,
                        )
                    )
                    continue
                applied_ids: set[str] = set()
                try:
                    for attempt in sorted(
                        group.attempts,
                        key=lambda item: (item.path, -item.start_byte, item.id),
                    ):
                        transaction.apply(attempt)
                        applied_ids.add(attempt.id)
                except StaleWorkspaceError:
                    if applied_ids:
                        transaction.rollback_attempts(applied_ids)
                        rolled_back_group_ids.add(group.id)
                    for finding_id in group.finding_ids:
                        finding = findings_by_id[finding_id]
                        findings_by_id[finding_id] = finding.model_copy(
                            update={
                                "disposition": FindingDisposition.UNRESOLVED,
                                "reason_code": "precondition_changed",
                                "reason": f"{finding.reason}; precondition_changed",
                            }
                        )
                    outcomes.append(
                        RepairGroupOutcome(
                            id=group.id,
                            finding_ids=group.finding_ids,
                            disposition=FindingDisposition.UNRESOLVED,
                            reason_code="precondition_changed",
                            path=group.path,
                            start_byte=group.start_byte,
                            end_byte=group.end_byte,
                        )
                    )
                    continue

                result = self._validate_group(state, group, transaction)
                validation.append(result)
                failure_reason: str | None
                if result.status is ValidationStatus.PASSED:
                    command_results, failure_reason = self._run_configured_validation(
                        state,
                        finding_ids=group.finding_ids,
                        attempt_id=group.attempts[0].id,
                        phase="group",
                        mutable_paths={
                            attempt.path for attempt in [*retained_attempts, *group.attempts]
                        },
                    )
                    validation.extend(command_results)
                elif result.status is ValidationStatus.UNAVAILABLE:
                    failure_reason = "validation_unavailable"
                else:
                    failure_reason = "validation_failed"
                if failure_reason is None:
                    for finding_id in group.finding_ids:
                        finding = findings_by_id[finding_id]
                        findings_by_id[finding_id] = finding.model_copy(
                            update={
                                "disposition": FindingDisposition.FIXED,
                                "reason_code": "validated",
                            }
                        )
                    retained_attempts.extend(group.attempts)
                    retained_groups.append(group)
                    outcomes.append(
                        RepairGroupOutcome(
                            id=group.id,
                            finding_ids=group.finding_ids,
                            disposition=FindingDisposition.FIXED,
                            reason_code="validated",
                            path=group.path,
                            start_byte=group.start_byte,
                            end_byte=group.end_byte,
                        )
                    )
                    continue

                if failure_reason == "budget_exhausted":
                    state = {**state, "budget_exhausted": True}

                try:
                    transaction.rollback_attempts({attempt.id for attempt in group.attempts})
                    rolled_back_group_ids.add(group.id)
                except BaseException as group_rollback_error:
                    return _failed_rollback_state(
                        {**state, "findings": list(findings_by_id.values())},
                        validation,
                        group_rollback_error,
                        transaction.residual_attempts(),
                    )
                for finding_id in group.finding_ids:
                    finding = findings_by_id[finding_id]
                    findings_by_id[finding_id] = finding.model_copy(
                        update={
                            "disposition": FindingDisposition.UNRESOLVED,
                            "reason_code": failure_reason,
                            "reason": f"{finding.reason}; {failure_reason}",
                        }
                    )
                outcomes.append(
                    RepairGroupOutcome(
                        id=group.id,
                        finding_ids=group.finding_ids,
                        disposition=FindingDisposition.UNRESOLVED,
                        reason_code=failure_reason,
                        path=group.path,
                        start_byte=group.start_byte,
                        end_byte=group.end_byte,
                    )
                )

            # Revalidate the retained set after all local writes.  Remove the
            # deterministic failing set together, then repeat until the
            # remaining groups reach a fixed point.
            while retained_groups:
                failing: list[RepairGroup] = []
                for group in retained_groups:
                    result = self._validate_group(
                        {**state, "findings": list(findings_by_id.values())},
                        group,
                        transaction,
                    ).model_copy(update={"check": "final_group_redetect"})
                    validation.append(result)
                    if result.status is not ValidationStatus.PASSED:
                        failing.append(group)
                if not failing:
                    break
                failing_ids = {group.id for group in failing}
                try:
                    transaction.rollback_attempts(
                        {attempt.id for group in failing for attempt in group.attempts}
                    )
                    rolled_back_group_ids.update(failing_ids)
                except BaseException as final_rollback_error:
                    return _failed_rollback_state(
                        {**state, "findings": list(findings_by_id.values())},
                        validation,
                        final_rollback_error,
                        transaction.residual_attempts(),
                    )
                for group in failing:
                    for finding_id in group.finding_ids:
                        finding = findings_by_id[finding_id]
                        findings_by_id[finding_id] = finding.model_copy(
                            update={
                                "disposition": FindingDisposition.UNRESOLVED,
                                "reason_code": "final_validation_failed",
                                "reason": f"{finding.reason}; final_validation_failed",
                            }
                        )
                outcomes = [
                    outcome.model_copy(
                        update={
                            "disposition": FindingDisposition.UNRESOLVED,
                            "reason_code": "final_validation_failed",
                        }
                    )
                    if outcome.id in failing_ids
                    else outcome
                    for outcome in outcomes
                ]
                retained_groups = [
                    group for group in retained_groups if group.id not in failing_ids
                ]
                retained_attempts = [
                    attempt for attempt in retained_attempts if attempt.group_id not in failing_ids
                ]

            closure_iteration = 0
            while retained_groups:
                closure_iteration += 1
                closure_findings = self._final_closure_findings(
                    {**state, "findings": list(findings_by_id.values())},
                    retained_groups,
                )
                closure_targets = [
                    findings_by_id[finding_id]
                    for group in retained_groups
                    for finding_id in group.finding_ids
                ]
                rename_map = cast(dict[str, str], context.get("rename_map", {}))
                closure_symbols = _expand_rename_symbols(
                    _semantic_symbols(closure_targets),
                    rename_map,
                )
                closure_baseline = [
                    finding for finding in state["findings"] if finding.symbol_id in closure_symbols
                ]
                closure_failures = _finding_delta_failures(
                    baseline=closure_baseline,
                    remaining=closure_findings,
                    targets=closure_targets,
                )
                closure_clean = not closure_failures
                validation.append(
                    ValidationResult(
                        finding_ids=[finding.id for finding in closure_targets],
                        attempt_id=f"final_closure_{closure_iteration}",
                        check="final_closure_redetect",
                        required=True,
                        status=(
                            ValidationStatus.PASSED if closure_clean else ValidationStatus.FAILED
                        ),
                        summary=(
                            "final closure has no target or newly active findings"
                            if closure_clean
                            else "final closure contains target or newly active findings"
                        ),
                    )
                )
                if closure_clean:
                    break

                failure_symbols = _expand_rename_symbols(
                    _semantic_symbols(closure_failures),
                    rename_map,
                )
                failure_paths = {
                    path
                    for finding in closure_failures
                    for path in (
                        finding.code_evidence.path,
                        finding.doc_evidence.path,
                    )
                }
                failing_groups = []
                for group in retained_groups:
                    group_findings = [
                        findings_by_id[finding_id] for finding_id in group.finding_ids
                    ]
                    group_symbols = _expand_rename_symbols(
                        _semantic_symbols(group_findings),
                        rename_map,
                    )
                    group_paths = {attempt.path for attempt in group.attempts}
                    if group_symbols & failure_symbols or group_paths & failure_paths:
                        failing_groups.append(group)
                if not failing_groups:
                    # The delta is real but cannot be assigned more narrowly.
                    failing_groups = list(retained_groups)
                failing_ids = {group.id for group in failing_groups}
                try:
                    transaction.rollback_attempts(
                        {attempt.id for group in failing_groups for attempt in group.attempts}
                    )
                    rolled_back_group_ids.update(failing_ids)
                except BaseException as final_closure_rollback_error:
                    return _failed_rollback_state(
                        {**state, "findings": list(findings_by_id.values())},
                        validation,
                        final_closure_rollback_error,
                        transaction.residual_attempts(),
                    )
                for group in failing_groups:
                    for finding_id in group.finding_ids:
                        current_finding = findings_by_id[finding_id]
                        findings_by_id[finding_id] = current_finding.model_copy(
                            update={
                                "disposition": FindingDisposition.UNRESOLVED,
                                "reason_code": "final_validation_failed",
                                "reason": (f"{current_finding.reason}; final_validation_failed"),
                            }
                        )
                outcomes = [
                    outcome.model_copy(
                        update={
                            "disposition": FindingDisposition.UNRESOLVED,
                            "reason_code": "final_validation_failed",
                        }
                    )
                    if outcome.id in failing_ids
                    else outcome
                    for outcome in outcomes
                ]
                retained_groups = [
                    group for group in retained_groups if group.id not in failing_ids
                ]
                retained_attempts = [
                    attempt for attempt in retained_attempts if attempt.group_id not in failing_ids
                ]

            if retained_groups:
                final_finding_ids = sorted(
                    {finding_id for group in retained_groups for finding_id in group.finding_ids}
                )
                if self.budget_ledger is None:
                    raise RuntimeError("run budget was not initialized")
                if state.get("budget_exhausted", False) and state["config"].validation.commands:
                    final_command_results = [
                        ValidationResult(
                            finding_ids=final_finding_ids,
                            attempt_id="final_configured_validation",
                            check="final_validation_budget",
                            required=True,
                            status=ValidationStatus.UNAVAILABLE,
                            summary="budget_exhausted: run budget already exhausted",
                        )
                    ]
                    final_command_reason: str | None = "budget_exhausted"
                else:
                    try:
                        self.budget_ledger.check_deadline()
                    except BudgetExhausted as error:
                        final_command_results = [
                            ValidationResult(
                                finding_ids=final_finding_ids,
                                attempt_id="final_configured_validation",
                                check="final_validation_budget",
                                required=True,
                                status=ValidationStatus.UNAVAILABLE,
                                summary=f"budget_exhausted: {error.resource}",
                            )
                        ]
                        final_command_reason = "budget_exhausted"
                    else:
                        final_command_results, final_command_reason = (
                            self._run_configured_validation(
                                {**state, "findings": list(findings_by_id.values())},
                                finding_ids=final_finding_ids,
                                attempt_id="final_configured_validation",
                                phase="final",
                                mutable_paths={attempt.path for attempt in retained_attempts},
                            )
                        )
                validation.extend(final_command_results)
                if final_command_reason is not None:
                    if final_command_reason == "budget_exhausted":
                        state = {**state, "budget_exhausted": True}
                    affected_groups = list(retained_groups)
                    affected_group_ids = {group.id for group in affected_groups}
                    outcome_reason = (
                        "final_validation_failed"
                        if final_command_reason == "validation_failed"
                        else final_command_reason
                    )
                    try:
                        transaction.rollback_attempts(
                            {attempt.id for group in affected_groups for attempt in group.attempts}
                        )
                        rolled_back_group_ids.update(affected_group_ids)
                    except BaseException as command_rollback_error:
                        return _failed_rollback_state(
                            {**state, "findings": list(findings_by_id.values())},
                            validation,
                            command_rollback_error,
                            transaction.residual_attempts(),
                        )
                    for group in affected_groups:
                        for finding_id in group.finding_ids:
                            finding = findings_by_id[finding_id]
                            findings_by_id[finding_id] = finding.model_copy(
                                update={
                                    "disposition": FindingDisposition.UNRESOLVED,
                                    "reason_code": outcome_reason,
                                    "reason": f"{finding.reason}; {outcome_reason}",
                                }
                            )
                    outcomes = [
                        outcome.model_copy(
                            update={
                                "disposition": FindingDisposition.UNRESOLVED,
                                "reason_code": outcome_reason,
                            }
                        )
                        if outcome.id in affected_group_ids
                        else outcome
                        for outcome in outcomes
                    ]
                    retained_groups = []
                    retained_attempts = []

            patched_paths = {attempt.path for attempt in retained_attempts}
            inputs_current = (
                _snapshot_inputs_are_current(
                    repo,
                    state["config"],
                    state["snapshot"],
                    patched_paths,
                )
                and _code_facts_are_current(repo, state, patched_paths)
                and _state_scope_paths_are_current(
                    repo,
                    state,
                    patched_paths,
                )
                and _state_git_changes_are_current(repo, state, patched_paths)
            )
            if not inputs_current:
                rollback_problem, residual_attempts = _attempt_rollback(transaction)
                if rollback_problem is not None:
                    return _failed_rollback_state(
                        state,
                        validation,
                        rollback_problem,
                        residual_attempts,
                    )
                return {
                    **_global_snapshot_changed_state(state),
                    "validation": validation,
                }

            inputs_still_current = (
                _snapshot_inputs_are_current(
                    repo,
                    state["config"],
                    state["snapshot"],
                    patched_paths,
                )
                and _code_facts_are_current(repo, state, patched_paths)
                and _state_scope_paths_are_current(
                    repo,
                    state,
                    patched_paths,
                )
                and _state_git_changes_are_current(repo, state, patched_paths)
            )
            if not inputs_still_current:
                rollback_problem, residual_attempts = _attempt_rollback(transaction)
                if rollback_problem is not None:
                    return _failed_rollback_state(
                        state,
                        validation,
                        rollback_problem,
                        residual_attempts,
                    )
                return {
                    **_global_snapshot_changed_state(state),
                    "validation": validation,
                }
            transaction.preflight_commit()
            if retained_groups:
                try:
                    self.budget_ledger.check_deadline()
                except BudgetExhausted as error:
                    deadline_rollback_error = rollback_retained_for_budget(error)
                    if deadline_rollback_error is not None:
                        return _failed_rollback_state(
                            {**state, "findings": list(findings_by_id.values())},
                            validation,
                            deadline_rollback_error,
                            transaction.residual_attempts(),
                        )

            ordered_findings = [findings_by_id[finding.id] for finding in state["findings"]]
            next_state: AgentState = {
                **state,
                "findings": ordered_findings,
                "attempts": retained_attempts,
                "validation": validation,
                "runtime_context": {
                    **context,
                    "repair_outcomes": outcomes,
                },
            }
            if self.run_service is not None and self.run_id is not None:
                outcomes_by_id = {outcome.id: outcome for outcome in outcomes}
                for group_id in planned_group_ids:
                    outcome = outcomes_by_id[group_id]
                    if outcome.disposition is FindingDisposition.FIXED:
                        terminal: Literal[
                            "group_retained",
                            "group_rolled_back",
                            "group_skipped",
                        ] = "group_retained"
                    elif group_id in rolled_back_group_ids:
                        terminal = "group_rolled_back"
                    else:
                        terminal = "group_skipped"
                    self.run_service.event(
                        self.run_id,
                        terminal,
                        {"group_id": group_id, "reason": outcome.reason_code},
                    )
                if next_state.get("budget_exhausted", False):
                    self.run_service.event(
                        self.run_id,
                        "budget_exhausted",
                        {"reason_code": "budget_exhausted"},
                    )
                self.run_service.event(
                    self.run_id,
                    "final_validation_completed",
                    {"retained_groups": len(retained_groups)},
                )
            bundle = self._build_bundle(next_state)
            publish_inputs_current = (
                _snapshot_inputs_are_current(
                    repo,
                    state["config"],
                    state["snapshot"],
                    patched_paths,
                )
                and _code_facts_are_current(repo, state, patched_paths)
                and _state_scope_paths_are_current(
                    repo,
                    state,
                    patched_paths,
                )
                and _state_git_changes_are_current(repo, state, patched_paths)
            )
            if not publish_inputs_current:
                rollback_problem, residual_attempts = _attempt_rollback(transaction)
                record_publication_aborted(
                    "global_snapshot_changed",
                    rollback_failed=rollback_problem is not None,
                )
                if rollback_problem is not None:
                    return _failed_rollback_state(
                        state,
                        validation,
                        rollback_problem,
                        residual_attempts,
                    )
                return {
                    **_global_snapshot_changed_state(state),
                    "validation": validation,
                }
            transaction.preflight_commit()
            if retained_groups:
                try:
                    self.budget_ledger.check_deadline()
                except BudgetExhausted as error:
                    deadline_rollback_error = rollback_retained_for_budget(error)
                    record_publication_aborted(
                        "budget_exhausted",
                        rollback_failed=deadline_rollback_error is not None,
                    )
                    if deadline_rollback_error is not None:
                        return _failed_rollback_state(
                            {**state, "findings": list(findings_by_id.values())},
                            validation,
                            deadline_rollback_error,
                            transaction.residual_attempts(),
                        )
                    ordered_findings = [findings_by_id[finding.id] for finding in state["findings"]]
                    next_state = {
                        **state,
                        "findings": ordered_findings,
                        "attempts": retained_attempts,
                        "validation": validation,
                        "runtime_context": {
                            **context,
                            "repair_outcomes": outcomes,
                        },
                    }
                    bundle = self._build_bundle(next_state)
            if self.run_service is not None and self.run_id is not None:
                try:
                    self.run_service.finish(
                        self.run_id,
                        RunCompletion(
                            status=bundle.status.value,
                            finding_count=len(bundle.findings),
                            suppressed_count=len(bundle.suppressed_findings),
                            fixed_count=sum(
                                finding.disposition is FindingDisposition.FIXED
                                for finding in bundle.findings
                            ),
                            model_calls=bundle.usage.model_calls,
                            snapshot=bundle.snapshot.model_dump(mode="json"),
                            usage=bundle.usage.model_dump(mode="json"),
                        ),
                        expected_manual_state_revision=int(context["manual_state_revision"]),
                    )
                except ManualStateRevisionChangedError:
                    rollback_problem, residual_attempts = _attempt_rollback(transaction)
                    record_publication_aborted(
                        "global_snapshot_changed",
                        rollback_failed=rollback_problem is not None,
                    )
                    if rollback_problem is not None:
                        return _failed_rollback_state(
                            state,
                            validation,
                            rollback_problem,
                            residual_attempts,
                        )
                    return {
                        **_global_snapshot_changed_state(state),
                        "validation": validation,
                    }
            try:
                self.committed_bundle = bundle
                transaction.finish_commit()
            except BaseException:
                if self.committed_bundle is bundle:
                    return {**next_state, "bundle": bundle}
                raise
            return {**next_state, "bundle": bundle}
        except BaseException as error:
            rollback_problem, residual_attempts = _attempt_rollback(transaction)
            if rollback_problem is not None:
                return _failed_rollback_state(
                    state,
                    [*validation, _runtime_failure(error)],
                    rollback_problem,
                    residual_attempts,
                )
            if not isinstance(error, Exception):
                raise
            if isinstance(error, BudgetExhausted):
                budget_findings = [
                    finding
                    if finding.disposition is FindingDisposition.FIXED
                    else finding.model_copy(
                        update={
                            "disposition": FindingDisposition.UNRESOLVED,
                            "reason_code": "budget_exhausted",
                            "reason": f"{finding.reason}; budget_exhausted",
                        }
                    )
                    for finding in state.get("findings", [])
                ]
                return {
                    **state,
                    "budget_exhausted": True,
                    "findings": budget_findings,
                    "attempts": [],
                    "validation": [
                        *validation,
                        ValidationResult(
                            finding_ids=[finding.id for finding in budget_findings],
                            attempt_id="run_budget",
                            check="run_deadline",
                            required=True,
                            status=ValidationStatus.UNAVAILABLE,
                            summary=f"budget_exhausted: {error.resource}",
                        ),
                    ],
                }
            if isinstance(error, ValidationInputChangedError):
                return {
                    **_global_snapshot_changed_state(state),
                    "validation": [
                        *validation,
                        ValidationResult(
                            finding_ids=[finding.id for finding in state.get("findings", [])],
                            attempt_id="validation_input_snapshot",
                            check="validation_input_snapshot",
                            required=True,
                            status=ValidationStatus.UNAVAILABLE,
                            summary=f"global_snapshot_changed: {error}",
                        ),
                    ],
                }
            if (
                not _snapshot_inputs_are_current(
                    repo,
                    state["config"],
                    state["snapshot"],
                    set(),
                )
                or not _code_facts_are_current(
                    repo,
                    state,
                )
                or not _state_scope_paths_are_current(
                    repo,
                    state,
                    set(),
                )
                or not _state_git_changes_are_current(repo, state, set())
            ):
                return {
                    **_global_snapshot_changed_state(state),
                    "validation": validation,
                }
            reason_code = getattr(error, "reason_code", "")
            failed_findings = state.get("findings", [])
            if isinstance(reason_code, str) and reason_code:
                failed_findings = [
                    finding
                    if finding.disposition is FindingDisposition.FIXED
                    else finding.model_copy(
                        update={
                            "disposition": FindingDisposition.UNRESOLVED,
                            "reason_code": reason_code,
                            "reason": f"{finding.reason}; {reason_code}",
                        }
                    )
                    for finding in failed_findings
                ]
            return {
                **state,
                "failed": True,
                "findings": failed_findings,
                "validation": [*validation, _runtime_failure(error)],
            }
        finally:
            if lock is not None:
                lock.release()

    def _build_bundle(self, state: AgentState) -> VerifiedRepairBundle:
        findings = state.get("findings", [])
        status = aggregate_status(
            state["request"].mode,
            [finding.disposition for finding in findings],
            stale=state.get("stale", False),
            failed=state.get("failed", False),
        )
        if not state.get("stale", False) and not state.get("failed", False):
            if state.get("budget_exhausted", False) or state.get("validation_incomplete", False):
                status = (
                    RunStatus.PARTIAL
                    if any(finding.disposition is FindingDisposition.FIXED for finding in findings)
                    and any(
                        finding.disposition is not FindingDisposition.FIXED for finding in findings
                    )
                    else RunStatus.UNRESOLVED
                )
            elif state["request"].mode is RunMode.CHECK and any(
                result.required
                and result.status is ValidationStatus.FAILED
                and result.check.startswith("check_")
                for result in state.get("validation", [])
            ):
                # A required executable failure remains non-clean even if a
                # Memory decision suppresses its lossy legacy finding envelope.
                status = RunStatus.DRIFT_FOUND
        attempts = state.get("attempts", [])
        residual_attempts = state.get("residual_attempts", [])
        verified_applied = any(
            finding.disposition is FindingDisposition.FIXED for finding in findings
        )
        change_attempts = [] if residual_attempts else (attempts if verified_applied else [])
        applied = bool(change_attempts)
        approval_required = [
            ApprovalRequest(
                id=f"approval_{uuid.uuid5(uuid.NAMESPACE_URL, finding.id)}",
                finding_id=finding.id,
                reason=finding.reason,
                input_file_hashes={
                    path: state["snapshot"].input_file_hashes[path]
                    for path in {
                        "drift-agent.toml",
                        finding.code_evidence.path,
                        finding.doc_evidence.path,
                    }
                    if path in state["snapshot"].input_file_hashes
                },
                suggested_validation=["rerun drift-agent check after the approved change"],
            )
            for finding in findings
            if finding.disposition is FindingDisposition.NEEDS_APPROVAL
        ]
        bundle_context: dict[str, Any] = cast(dict[str, Any], state.get("runtime_context", {}))
        budget_usage = (
            self.budget_ledger.usage_snapshot() if self.budget_ledger is not None else Usage()
        )
        validation_results = state.get("validation", [])
        return VerifiedRepairBundle(
            status=status,
            run_id=bundle_context.get(
                "run_id",
                f"run_{uuid.uuid4()}",
            ),
            snapshot=state["snapshot"],
            scope=state.get("changed_paths", []),
            findings=findings,
            changes=ChangeSet(
                applied=applied,
                files=sorted({attempt.path for attempt in change_attempts}),
                patch="".join(attempt.unified_diff for attempt in change_attempts),
            ),
            validation=validation_results,
            approval_required=approval_required,
            usage=budget_usage.model_copy(
                update={
                    "tool_calls": budget_usage.tool_calls + 4 + len(validation_results),
                }
            ),
            repository_id=state.get("repository_id", ""),
            workspace_id=state.get("workspace_id", ""),
            suppressed_findings=bundle_context.get(
                "suppressions",
                [],
            ),
            memory_events=bundle_context.get(
                "memory_events",
                [],
            ),
            repair_groups=bundle_context.get(
                "repair_outcomes",
                [],
            ),
            residual_changes=[
                ResidualChange(
                    path=attempt.path,
                    start_byte=attempt.start_byte,
                    end_byte=attempt.end_byte,
                    current_hash=_residual_current_hash(
                        state["request"].repo_path,
                        attempt.path,
                    ),
                )
                for attempt in residual_attempts
            ],
            semantic_analysis=(
                state["request"].semantic_analysis or state["request"].semantic_repair
            ),
        )

    def finalize_node(self, state: AgentState) -> AgentState:
        if "bundle" in state:
            return state
        state = self._deadline_state(state)
        if (
            state["request"].mode.value == "repair"
            and not state.get("budget_exhausted", False)
            and not state.get("failed", False)
            and not state.get("stale", False)
            and self.run_service is not None
            and self.run_id is not None
            and self.identities is not None
            and all(
                event.kind != "repair_planned" for event in self.run_service.events(self.run_id)
            )
        ):
            self.run_service.event(self.run_id, "repair_planned", {"groups": 0})
            lock = WorkspaceLock.from_identities(
                self.identities,
                run_id=self.run_id,
                timeout_seconds=min(
                    state["request"].lock_timeout_seconds,
                    (
                        self.budget_ledger.remaining_seconds()
                        if self.budget_ledger is not None
                        else state["request"].lock_timeout_seconds
                    ),
                ),
            )
            try:
                try:
                    owner = lock.acquire()
                except LockTimeoutError:
                    if self.budget_ledger is None or self.budget_ledger.remaining_seconds() > 0:
                        raise
                    state = self._deadline_state(state)
                else:
                    self.run_service.event(
                        self.run_id,
                        "lock_acquired",
                        {"generation": owner.generation},
                    )
                    self.run_service.event(
                        self.run_id,
                        "final_validation_completed",
                        {"retained_groups": 0},
                    )
            finally:
                lock.release()
        if state["request"].mode.value == "check" and state.get("workspace_id"):
            final_observation = probe_workspace_lock(state["workspace_id"])
            initial_observation = self.initial_lock_observation
            if (
                final_observation.active_writer
                or (initial_observation is not None and initial_observation.active_writer)
                or (
                    initial_observation is not None
                    and initial_observation.generation != final_observation.generation
                )
            ):
                state = _global_snapshot_changed_state(state)
        if not state.get("failed", False) and not (
            _snapshot_inputs_are_current(
                state["request"].repo_path,
                state["config"],
                state["snapshot"],
                set(),
            )
            and _state_scope_paths_are_current(
                state["request"].repo_path,
                state,
                set(),
            )
            and _state_git_changes_are_current(
                state["request"].repo_path,
                state,
                set(),
            )
        ):
            state = _global_snapshot_changed_state(state)
        context: dict[str, Any] = cast(dict[str, Any], state.get("runtime_context", {}))
        initial_manual_revision = context.get("manual_state_revision")
        if (
            not state.get("failed", False)
            and self.store is not None
            and isinstance(initial_manual_revision, int)
            and self.store.manual_state_revision(state.get("repository_id", ""))
            != initial_manual_revision
        ):
            state = _global_snapshot_changed_state(state)
        state = self._deadline_state(state)
        if (
            state.get("budget_exhausted", False)
            and self.run_service is not None
            and self.run_id is not None
            and all(
                event.kind not in {"budget_exhausted", "final_validation_completed"}
                for event in self.run_service.events(self.run_id)
            )
        ):
            self.run_service.event(
                self.run_id,
                "budget_exhausted",
                {"reason_code": "budget_exhausted"},
            )
        bundle = self._build_bundle(state)
        if self.run_service is not None and self.run_id is not None:
            persisted = self.store.run(self.run_id) if self.store is not None else None
            if persisted is not None and not persisted.completed:
                completion = RunCompletion(
                    status=bundle.status.value,
                    finding_count=len(bundle.findings),
                    suppressed_count=len(bundle.suppressed_findings),
                    fixed_count=sum(
                        finding.disposition is FindingDisposition.FIXED
                        for finding in bundle.findings
                    ),
                    model_calls=bundle.usage.model_calls,
                    snapshot=bundle.snapshot.model_dump(mode="json"),
                    usage=bundle.usage.model_dump(mode="json"),
                )
                expected_revision = (
                    initial_manual_revision
                    if isinstance(initial_manual_revision, int)
                    and not state.get("stale", False)
                    and not state.get("failed", False)
                    else None
                )
                try:
                    self.run_service.finish(
                        self.run_id,
                        completion,
                        expected_manual_state_revision=expected_revision,
                    )
                except ManualStateRevisionChangedError:
                    state = _global_snapshot_changed_state(state)
                    bundle = self._build_bundle(state)
                    self.run_service.finish(
                        self.run_id,
                        RunCompletion(
                            status=bundle.status.value,
                            finding_count=len(bundle.findings),
                            suppressed_count=len(bundle.suppressed_findings),
                            fixed_count=sum(
                                finding.disposition is FindingDisposition.FIXED
                                for finding in bundle.findings
                            ),
                            model_calls=bundle.usage.model_calls,
                            snapshot=bundle.snapshot.model_dump(mode="json"),
                            usage=bundle.usage.model_dump(mode="json"),
                        ),
                    )
        return {**state, "bundle": bundle}


def _failed_bundle(request: RunRequest, error: Exception) -> VerifiedRepairBundle:
    return VerifiedRepairBundle(
        status=RunStatus.FAILED,
        run_id=f"run_{uuid.uuid4()}",
        snapshot=WorkspaceSnapshot(
            head_revision="unknown",
            workspace_fingerprint=sha256_bytes(str(request.repo_path.resolve()).encode()),
            input_file_hashes={},
        ),
        scope=[],
        findings=[],
        changes=ChangeSet(),
        validation=[_runtime_failure(error)],
        semantic_analysis=request.semantic_analysis or request.semantic_repair,
    )


def run(
    request: RunRequest,
    *,
    runtime: AgentRuntime | None = None,
) -> VerifiedRepairBundle:
    initial: AgentState = {"request": request}
    active_runtime = runtime or AgentRuntime()
    active_runtime.committed_bundle = None
    active_runtime.budget_ledger = BudgetLedger(request.budgets)
    active_runtime.model_client = None
    try:
        result = run_pipeline(active_runtime, initial)
        bundle = result["bundle"]
    except BaseException as error:
        if active_runtime.committed_bundle is not None:
            bundle = active_runtime.committed_bundle
        elif isinstance(error, Exception):
            bundle = _failed_bundle(request, error)
            budget_usage = active_runtime.budget_ledger.usage_snapshot()
            bundle = bundle.model_copy(
                update={
                    "usage": budget_usage.model_copy(
                        update={
                            "tool_calls": budget_usage.tool_calls + len(bundle.validation),
                        }
                    )
                }
            )
            if active_runtime.identities is not None:
                bundle = bundle.model_copy(
                    update={
                        "run_id": active_runtime.run_id or bundle.run_id,
                        "repository_id": active_runtime.identities.repository.digest,
                        "workspace_id": active_runtime.identities.workspace.digest,
                    }
                )
            if (
                active_runtime.store is not None
                and active_runtime.run_service is not None
                and active_runtime.run_id is not None
            ):
                try:
                    persisted = active_runtime.store.run(active_runtime.run_id)
                    if persisted is not None and not persisted.completed:
                        active_runtime.run_service.finish(
                            active_runtime.run_id,
                            RunCompletion(
                                status="failed",
                                finding_count=0,
                                suppressed_count=0,
                                fixed_count=0,
                                model_calls=bundle.usage.model_calls,
                                snapshot=bundle.snapshot.model_dump(mode="json"),
                                usage=bundle.usage.model_dump(mode="json"),
                                event_payload={"error": f"{type(error).__name__}: {error}"},
                            ),
                        )
                except Exception:
                    pass
        else:
            raise
    return bundle

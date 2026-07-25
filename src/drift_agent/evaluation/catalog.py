from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path, PurePosixPath

from pydantic import JsonValue, ValidationError

from drift_agent.evaluation.models import (
    CaseManifest,
    CatalogAudit,
    CatalogManifest,
    Disposition,
    MatchingKey,
    Operation,
    ProjectFamily,
    Provenance,
    SymbolIdentity,
)

MAX_CASE_FILES = 16
MAX_CASE_BYTES = 64 * 1024
MAX_DATASET_FILES = 64
MAX_DATASET_BYTES = 256 * 1024

FROZEN_CASE_IDS = (
    "click.parameter-default.v1",
    "click.multi-group-partial.v1",
    "click.conflict.v1",
    "httpx.responseclosed-streamclosed.v1",
    "pydantic.apply-validators-field-name.v1",
    "pydantic.google-returns.v1",
    "rich.iteration-speed-column.v1",
    "rich.incomplete-declaration-reject.v1",
)

REQUIRED_COVERAGE_TAGS = frozenset(
    {
        "parameter",
        "default",
        "rename",
        "delete",
        "google_args",
        "google_returns",
        "same_file_multi",
        "cross_file_multi",
        "partial",
        "conflict",
        "conservative_rejection",
    }
)

# Filled from the frozen manifest bytes. A manifest/oracle change requires a new
# dataset version, not an in-place hash update.
FROZEN_MANIFEST_SHA256: dict[str, str] = {
    "click.parameter-default.v1": (
        "a3f09ea0256ac655227c0d7590890cce487de28eedffa482a5f4872a9c799ede"
    ),
    "click.multi-group-partial.v1": (
        "a864b05ada9b022967b20741f8dda1f3e4990dde7f69ccd49f79dd66a9d92a3f"
    ),
    "click.conflict.v1": ("5b5e83861851680bcba1face8c7b272cd066f74d483a6aecf8d5dc24670564c7"),
    "httpx.responseclosed-streamclosed.v1": (
        "e82d31dbc77a47660592cc3f530692d2c3e347678a6905ba3cd040dee20b2e6b"
    ),
    "pydantic.apply-validators-field-name.v1": (
        "a782c2e32bee6b2d64006a60000ba8ba2c3c726a2bd6b505081a1a45e52d9c31"
    ),
    "pydantic.google-returns.v1": (
        "0014b28033405b49d80902bb1dd5d263978dece5ea594170aae397b1a1638be1"
    ),
    "rich.iteration-speed-column.v1": (
        "65acb28ee254d5d4ef132549e4b55245cc6283ddfa46a3696b39f95489c00feb"
    ),
    "rich.incomplete-declaration-reject.v1": (
        "1a51028b663d88fb38db4dd75622488bafa4ed010bff9b9ed7ced423a607d00d"
    ),
}


@dataclass(frozen=True)
class FrozenCaseContract:
    project_family: ProjectFamily
    provenance: Provenance
    operation: Operation
    coverage_tags: tuple[str, ...]
    status: str
    finding_multiset: tuple[MatchingKey, ...]
    dispositions: tuple[Disposition, ...]
    reason_codes: tuple[str, ...]
    changed_paths: tuple[str, ...]


class CatalogAuditError(ValueError):
    """Raised before replay when any frozen catalog assertion fails."""

    def __init__(self, errors: Iterable[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("structural-v1 catalog audit failed:\n- " + "\n- ".join(self.errors))


def _symbol(
    module: str,
    name: str,
    category: str = "module_function",
    *,
    owner: str | None = None,
) -> SymbolIdentity:
    return SymbolIdentity.model_validate(
        {
            "module": module,
            "owner": owner,
            "name": name,
            "category": category,
        }
    )


def _key(
    symbol: SymbolIdentity,
    kind: str,
    component: str,
    old_value: JsonValue,
    new_value: JsonValue,
    code_path: str,
    doc_path: str,
) -> MatchingKey:
    return MatchingKey(
        symbol_identity=symbol,
        kind=kind,
        component=component,
        old_value=old_value,
        new_value=new_value,
        code_path=code_path,
        doc_path=doc_path,
        detector_id="structural.signature",
        detector_version="2",
    )


def _synthetic_provenance() -> Provenance:
    return Provenance(
        kind="project_authored",
        repository="project://doc-code-drift-agent",
        code_revision="structural-v1",
        doc_revision="structural-v1",
        source_urls=(),
        license_spdx="LicenseRef-Project-Authored",
        copied_bytes=0,
    )


FROZEN_CASES: dict[str, FrozenCaseContract] = {
    "click.parameter-default.v1": FrozenCaseContract(
        project_family="click",
        provenance=_synthetic_provenance(),
        operation="repair",
        coverage_tags=("parameter", "default", "same_file"),
        status="fixed",
        finding_multiset=(
            _key(
                _symbol("click_eval.api", "echo"),
                "parameter_default_changed",
                "color",
                "Constant(value=False)",
                "Constant(value=True)",
                "src/click_eval/api.py",
                "docs/api.md",
            ),
        ),
        dispositions=("fixed",),
        reason_codes=("validated",),
        changed_paths=("docs/api.md",),
    ),
    "click.multi-group-partial.v1": FrozenCaseContract(
        project_family="click",
        provenance=_synthetic_provenance(),
        operation="repair",
        coverage_tags=(
            "rename",
            "delete",
            "same_file_multi",
            "cross_file_multi",
            "partial",
        ),
        status="partial",
        finding_multiset=(
            _key(
                _symbol("click_eval.current", "publish"),
                "symbol_reference_renamed",
                "symbol",
                "click_eval.legacy.deploy",
                "click_eval.current.publish",
                "src/click_eval/current.py",
                "docs/commands.md",
            ),
            _key(
                _symbol("click_eval.legacy", "retired"),
                "symbol_reference_deleted",
                "symbol",
                "click_eval.legacy.retired",
                {"type": "missing_symbol"},
                "src/click_eval/legacy.py",
                "docs/commands.md",
            ),
            _key(
                _symbol("click_eval.design", "preview"),
                "parameter_default_changed",
                "format",
                "Constant(value='plain')",
                "Constant(value='rich')",
                "src/click_eval/design.py",
                "docs/design.md",
            ),
        ),
        dispositions=("fixed", "fixed", "needs_approval"),
        reason_codes=("validated", "validated", "truth_requires_approval"),
        changed_paths=("docs/commands.md",),
    ),
    "click.conflict.v1": FrozenCaseContract(
        project_family="click",
        provenance=_synthetic_provenance(),
        operation="repair",
        coverage_tags=("parameter", "default", "rename", "conflict"),
        status="unresolved",
        finding_multiset=(
            _key(
                _symbol("click_eval.drawing", "draw"),
                "parameter_default_changed",
                "color",
                "Constant(value='blue')",
                "Constant(value='red')",
                "src/click_eval/drawing.py",
                "docs/conflict.md",
            ),
            _key(
                _symbol("click_eval.drawing", "draw"),
                "parameter_annotation_changed",
                "color",
                "Name(id='str', ctx=Load())",
                "Name(id='Color', ctx=Load())",
                "src/click_eval/drawing.py",
                "docs/conflict.md",
            ),
            _key(
                _symbol("click_eval.drawing", "draw"),
                "symbol_reference_renamed",
                "symbol",
                "click_eval.conflict.paint",
                "click_eval.drawing.draw",
                "src/click_eval/drawing.py",
                "docs/conflict.md",
            ),
        ),
        dispositions=("unresolved", "unresolved", "unresolved"),
        reason_codes=(
            "conflict.replacement",
            "conflict.replacement",
            "conflict.replacement",
        ),
        changed_paths=(),
    ),
    "httpx.responseclosed-streamclosed.v1": FrozenCaseContract(
        project_family="httpx",
        provenance=Provenance(
            kind="historical",
            repository="https://github.com/encode/httpx",
            code_revision="9b8f5af7596ab2208375a4d26b5b585d51b82b01",
            doc_revision="7d3a5347a9717169c00c73b71ba7c560e9a04443",
            source_urls=(
                "https://github.com/encode/httpx/commit/9b8f5af7596ab2208375a4d26b5b585d51b82b01",
                "https://github.com/encode/httpx/commit/7d3a5347a9717169c00c73b71ba7c560e9a04443",
            ),
            license_spdx="BSD-3-Clause",
            copied_bytes=0,
        ),
        operation="repair",
        coverage_tags=("rename", "historical", "conservative_rejection"),
        status="unresolved",
        finding_multiset=(
            _key(
                _symbol(
                    "httpx._exceptions",
                    "StreamClosed",
                    "exception_class",
                ),
                "unsupported",
                "symbol",
                "httpx.ResponseClosed",
                "httpx.StreamClosed",
                "src/httpx/_exceptions.py",
                "docs/exceptions.md",
            ),
        ),
        dispositions=("unresolved",),
        reason_codes=("unsupported.symbol_kind",),
        changed_paths=(),
    ),
    "pydantic.apply-validators-field-name.v1": FrozenCaseContract(
        project_family="pydantic",
        provenance=Provenance(
            kind="historical",
            repository="https://github.com/pydantic/pydantic",
            code_revision="080c741ecf4e113b9c7487de16ffbba5182f03bf",
            doc_revision="080c741ecf4e113b9c7487de16ffbba5182f03bf",
            source_urls=(
                "https://github.com/pydantic/pydantic/commit/080c741ecf4e113b9c7487de16ffbba5182f03bf",
            ),
            license_spdx="MIT",
            copied_bytes=0,
        ),
        operation="repair",
        coverage_tags=("parameter", "delete", "google_args", "historical"),
        status="fixed",
        finding_multiset=(
            _key(
                _symbol("pydantic_eval.generate_schema", "apply_validators"),
                "docstring_parameter_changed",
                "field_name",
                None,
                {"type": "missing_parameter"},
                "src/pydantic_eval/generate_schema.py",
                "src/pydantic_eval/generate_schema.py",
            ),
        ),
        dispositions=("fixed",),
        reason_codes=("validated",),
        changed_paths=("src/pydantic_eval/generate_schema.py",),
    ),
    "pydantic.google-returns.v1": FrozenCaseContract(
        project_family="pydantic",
        provenance=_synthetic_provenance(),
        operation="repair",
        coverage_tags=("google_returns", "docstring_ast_guard"),
        status="fixed",
        finding_multiset=(
            _key(
                _symbol("pydantic_eval.api", "model_name"),
                "docstring_return_changed",
                "return",
                "Name(id='bytes', ctx=Load())",
                "Name(id='str', ctx=Load())",
                "src/pydantic_eval/api.py",
                "src/pydantic_eval/api.py",
            ),
        ),
        dispositions=("fixed",),
        reason_codes=("validated",),
        changed_paths=("src/pydantic_eval/api.py",),
    ),
    "rich.iteration-speed-column.v1": FrozenCaseContract(
        project_family="rich",
        provenance=Provenance(
            kind="historical",
            repository="https://github.com/Textualize/rich",
            code_revision="669b5006b3bbfe6fb023d76cda62c59773141cf5",
            doc_revision="669b5006b3bbfe6fb023d76cda62c59773141cf5",
            source_urls=(
                "https://github.com/Textualize/rich/commit/669b5006b3bbfe6fb023d76cda62c59773141cf5",
            ),
            license_spdx="MIT",
            copied_bytes=0,
        ),
        operation="repair",
        coverage_tags=("delete", "historical", "conservative_rejection"),
        status="unresolved",
        finding_multiset=(
            _key(
                _symbol("rich.progress", "IterationSpeedColumn", "class"),
                "unsupported",
                "symbol",
                "rich.progress.IterationSpeedColumn",
                {"type": "missing_symbol"},
                "src/rich/progress.py",
                "docs/progress.md",
            ),
        ),
        dispositions=("unresolved",),
        reason_codes=("unsupported.symbol_kind",),
        changed_paths=(),
    ),
    "rich.incomplete-declaration-reject.v1": FrozenCaseContract(
        project_family="rich",
        provenance=_synthetic_provenance(),
        operation="repair",
        coverage_tags=("delete", "conservative_rejection"),
        status="unresolved",
        finding_multiset=(
            _key(
                _symbol("rich_eval.api", "legacy_render"),
                "symbol_reference_deleted",
                "symbol",
                "rich_eval.api.legacy_render",
                {"type": "missing_symbol"},
                "src/rich_eval/api.py",
                "docs/guide.md",
            ),
        ),
        dispositions=("unresolved",),
        reason_codes=("unsupported.incomplete_declaration",),
        changed_paths=(),
    ),
}


def default_dataset_root() -> Path:
    """Return the installed or source-checkout structural-v1 dataset root."""

    resource_root = resources.files("drift_agent.evaluation").joinpath("data", "structural", "v1")
    if isinstance(resource_root, Path) and resource_root.is_dir():
        return resource_root

    root = Path(__file__).resolve().parents[3] / "evals/datasets/structural/v1"
    if not root.is_dir():
        raise FileNotFoundError(
            "structural-v1 dataset is not available in the installed distribution "
            "or source checkout; pass an explicit dataset_root"
        )
    return root


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CatalogAuditError((f"{path}: cannot read JSON: {error}",)) from error


def load_case_manifest(path: Path) -> CaseManifest:
    try:
        return CaseManifest.model_validate(_read_json(path))
    except ValidationError as error:
        raise CatalogAuditError((f"{path}: invalid case manifest: {error}",)) from error


def load_catalog_manifest(dataset_root: Path) -> CatalogManifest:
    path = dataset_root / "catalog.json"
    try:
        return CatalogManifest.model_validate(_read_json(path))
    except ValidationError as error:
        raise CatalogAuditError((f"{path}: invalid catalog manifest: {error}",)) from error


def _same_provenance_except_copied_bytes(
    actual: Provenance,
    frozen: Provenance,
) -> bool:
    actual_data = actual.model_dump(mode="json", exclude={"copied_bytes"})
    frozen_data = frozen.model_dump(mode="json", exclude={"copied_bytes"})
    return actual_data == frozen_data


def _audit_frozen_contract(manifest: CaseManifest, errors: list[str]) -> None:
    frozen = FROZEN_CASES[manifest.case_id]
    prefix = manifest.case_id
    if manifest.project_family != frozen.project_family:
        errors.append(f"{prefix}: project_family differs from frozen catalog")
    if not _same_provenance_except_copied_bytes(manifest.provenance, frozen.provenance):
        errors.append(f"{prefix}: provenance/license/revisions differ from frozen catalog")
    if manifest.operation != frozen.operation:
        errors.append(f"{prefix}: operation differs from frozen catalog")
    if manifest.coverage_tags != frozen.coverage_tags:
        errors.append(f"{prefix}: coverage tags differ from frozen catalog")
    if manifest.expected.status != frozen.status:
        errors.append(f"{prefix}: status oracle differs from frozen catalog")
    if manifest.expected.finding_multiset != frozen.finding_multiset:
        errors.append(f"{prefix}: normalized finding oracle differs from frozen catalog")
    if manifest.expected.dispositions != frozen.dispositions:
        errors.append(f"{prefix}: disposition oracle differs from frozen catalog")
    if manifest.expected.reason_codes != frozen.reason_codes:
        errors.append(f"{prefix}: reason-code oracle differs from frozen catalog")
    actual_changed_paths = tuple(change.path for change in manifest.expected.changed_bytes)
    if actual_changed_paths != frozen.changed_paths:
        errors.append(f"{prefix}: mutation oracle differs from frozen catalog")


def _audited_fixture_files(
    case_root: Path,
    manifest: CaseManifest,
    errors: list[str],
) -> tuple[int, int, int]:
    listed_paths = {fixture.path for fixture in manifest.files}
    discovered_paths: set[str] = set()
    for path in case_root.rglob("*"):
        if path.is_symlink():
            errors.append(f"{manifest.case_id}: symlink fixture is forbidden: {path}")
            continue
        if path.is_file() and path.name != "manifest.json":
            discovered_paths.add(path.relative_to(case_root).as_posix())
    if discovered_paths != listed_paths:
        missing = sorted(listed_paths - discovered_paths)
        unlisted = sorted(discovered_paths - listed_paths)
        if missing:
            errors.append(f"{manifest.case_id}: missing fixture files: {missing}")
        if unlisted:
            errors.append(f"{manifest.case_id}: unlisted fixture files: {unlisted}")

    total_bytes = 0
    copied_bytes = 0
    for fixture in manifest.files:
        path = case_root / PurePosixPath(fixture.path)
        try:
            raw = path.read_bytes()
        except OSError as error:
            errors.append(f"{manifest.case_id}: cannot read {fixture.path}: {error}")
            continue
        total_bytes += len(raw)
        if fixture.origin == "upstream":
            copied_bytes += len(raw)
        if len(raw) != fixture.size_bytes:
            errors.append(
                f"{manifest.case_id}: {fixture.path} size is {len(raw)}, "
                f"manifest says {fixture.size_bytes}"
            )
        digest = _sha256(raw)
        if digest != fixture.sha256:
            errors.append(
                f"{manifest.case_id}: {fixture.path} sha256 is {digest}, "
                f"manifest says {fixture.sha256}"
            )

    if len(manifest.files) > MAX_CASE_FILES:
        errors.append(f"{manifest.case_id}: {len(manifest.files)} files exceeds {MAX_CASE_FILES}")
    if total_bytes > MAX_CASE_BYTES:
        errors.append(f"{manifest.case_id}: {total_bytes} bytes exceeds {MAX_CASE_BYTES}")
    if manifest.provenance.kind == "project_authored" and copied_bytes != 0:
        errors.append(f"{manifest.case_id}: synthetic copied bytes must be zero")
    if manifest.provenance.copied_bytes != copied_bytes:
        errors.append(
            f"{manifest.case_id}: copied_bytes is {copied_bytes}, "
            f"manifest says {manifest.provenance.copied_bytes}"
        )
    return len(manifest.files), total_bytes, copied_bytes


def _audit_changed_bytes(manifest: CaseManifest, errors: list[str]) -> None:
    base = {fixture.target_path: fixture for fixture in manifest.files if fixture.role == "base"}
    current = dict(base)
    for rename in manifest.workspace.renames:
        moved = current.pop(rename.old_path, None)
        if moved is None:
            errors.append(
                f"{manifest.case_id}: rename source is absent from base: {rename.old_path}"
            )
        else:
            current[rename.new_path] = moved
    for path in manifest.workspace.deleted_paths:
        if current.pop(path, None) is None:
            errors.append(f"{manifest.case_id}: deleted path is absent from base: {path}")
    current.update(
        {fixture.target_path: fixture for fixture in manifest.files if fixture.role == "current"}
    )
    expected = {
        fixture.target_path: fixture for fixture in manifest.files if fixture.role == "expected"
    }
    changed_paths = {change.path for change in manifest.expected.changed_bytes}
    if set(expected) != changed_paths:
        errors.append(f"{manifest.case_id}: expected fixture targets must equal changed-byte paths")
    for change in manifest.expected.changed_bytes:
        before = current.get(change.path)
        after = expected.get(change.path)
        if before is None or after is None:
            continue
        expected_before = (
            before.sha256,
            before.size_bytes,
            "0644",
        )
        expected_after = (
            after.sha256,
            after.size_bytes,
            "0644",
        )
        actual_before = (
            change.before_sha256,
            change.before_size_bytes,
            change.before_mode,
        )
        actual_after = (
            change.after_sha256,
            change.after_size_bytes,
            change.after_mode,
        )
        if actual_before != expected_before:
            errors.append(f"{manifest.case_id}: before-byte oracle mismatch for {change.path}")
        if actual_after != expected_after:
            errors.append(f"{manifest.case_id}: after-byte oracle mismatch for {change.path}")


def _perform_audit(
    dataset_root: Path,
) -> tuple[CatalogAudit, tuple[CaseManifest, ...]]:
    root = dataset_root.resolve()
    catalog = load_catalog_manifest(root)
    errors: list[str] = []
    catalog_ids = tuple(entry.case_id for entry in catalog.cases)
    if catalog_ids != FROZEN_CASE_IDS:
        errors.append(
            f"catalog case IDs/order differ: expected={FROZEN_CASE_IDS}, actual={catalog_ids}"
        )
    if len({entry.case_id for entry in catalog.cases}) != len(catalog.cases):
        errors.append("catalog case IDs must be unique")
    manifest_paths = tuple(entry.manifest for entry in catalog.cases)
    if len(set(manifest_paths)) != len(manifest_paths):
        errors.append("catalog manifest paths must be unique")

    discovered_manifests = {
        path.relative_to(root).as_posix() for path in root.glob("*/manifest.json") if path.is_file()
    }
    if discovered_manifests != set(manifest_paths):
        errors.append("catalog entries must list every and only case manifest")

    manifests: list[CaseManifest] = []
    total_files = 0
    total_bytes = 0
    copied_bytes = 0
    coverage: set[str] = set()
    for entry in catalog.cases:
        manifest_path = root / PurePosixPath(entry.manifest)
        try:
            raw_manifest = manifest_path.read_bytes()
        except OSError as error:
            errors.append(f"{entry.case_id}: cannot read manifest: {error}")
            continue
        digest = _sha256(raw_manifest)
        if digest != entry.sha256:
            errors.append(
                f"{entry.case_id}: catalog manifest hash is {digest}, says {entry.sha256}"
            )
        frozen_digest = FROZEN_MANIFEST_SHA256.get(entry.case_id, "")
        if frozen_digest and digest != frozen_digest:
            errors.append(f"{entry.case_id}: frozen manifest hash changed")
        try:
            manifest = CaseManifest.model_validate_json(raw_manifest)
        except ValidationError as error:
            errors.append(f"{entry.case_id}: invalid case manifest: {error}")
            continue
        manifests.append(manifest)
        if manifest.case_id != entry.case_id:
            errors.append(f"{entry.case_id}: manifest case_id mismatch")
            continue
        if manifest.case_id not in FROZEN_CASES:
            errors.append(f"{manifest.case_id}: case is not in the frozen matrix")
            continue
        _audit_frozen_contract(manifest, errors)
        files, size, copied = _audited_fixture_files(
            manifest_path.parent,
            manifest,
            errors,
        )
        _audit_changed_bytes(manifest, errors)
        total_files += files
        total_bytes += size
        copied_bytes += copied
        coverage.update(manifest.coverage_tags)

    if total_files > MAX_DATASET_FILES:
        errors.append(f"dataset file count {total_files} exceeds {MAX_DATASET_FILES}")
    if total_bytes > MAX_DATASET_BYTES:
        errors.append(f"dataset bytes {total_bytes} exceeds {MAX_DATASET_BYTES}")
    missing_coverage = sorted(REQUIRED_COVERAGE_TAGS - coverage)
    if missing_coverage:
        errors.append(f"dataset is missing required coverage tags: {missing_coverage}")
    if errors:
        raise CatalogAuditError(errors)
    return (
        CatalogAudit(
            dataset_id="structural-v1",
            case_ids=FROZEN_CASE_IDS,
            fixture_files=total_files,
            fixture_bytes=total_bytes,
            copied_bytes=copied_bytes,
            coverage_tags=tuple(sorted(coverage)),
        ),
        tuple(manifests),
    )


def audit_catalog(dataset_root: Path | None = None) -> CatalogAudit:
    """Audit IDs, oracle, provenance, license, hashes, sizes, and caps."""

    root = dataset_root if dataset_root is not None else default_dataset_root()
    audit, _ = _perform_audit(root)
    return audit


def load_catalog(dataset_root: Path | None = None) -> tuple[CaseManifest, ...]:
    """Load the frozen manifests only after the complete preflight audit passes."""

    root = dataset_root if dataset_root is not None else default_dataset_root()
    _, manifests = _perform_audit(root)
    return manifests

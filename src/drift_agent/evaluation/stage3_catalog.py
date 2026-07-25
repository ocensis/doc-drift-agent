from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from importlib import resources
from pathlib import Path, PurePosixPath

from pydantic import ValidationError

from drift_agent.evaluation.stage3_models import (
    Stage3CaseManifest,
    Stage3CatalogAudit,
    Stage3CatalogManifest,
)

MAX_STAGE3_CASE_FILES = 16
MAX_STAGE3_CASE_BYTES = 64 * 1024
MAX_STAGE3_DATASET_FILES = 64
MAX_STAGE3_DATASET_BYTES = 256 * 1024

FROZEN_STAGE3_CASE_IDS = (
    "executable.doctest-pass.v1",
    "executable.doctest-fail.v1",
    "executable.pytest-pass.v1",
    "executable.pytest-fail.v1",
    "executable.timeout.v1",
    "executable.unavailable.v1",
    "executable.budget-exhaustion.v1",
    "semantic.fast-success.v1",
    "semantic.strong-success.v1",
    "semantic.two-failures-abstain.v1",
)

REQUIRED_STAGE3_COVERAGE_TAGS = frozenset(
    {
        "abstention",
        "budget_exhaustion",
        "doctest",
        "executable",
        "failing",
        "fast_success",
        "passing",
        "pytest",
        "semantic",
        "strong_escalation",
        "timeout",
        "unavailable",
    }
)

# Populated only after the complete dataset matrix is frozen. Changing any
# fixture, oracle, route, or usage response requires a new dataset version.
FROZEN_STAGE3_MANIFEST_SHA256: dict[str, str] = {
    "executable.doctest-pass.v1": (
        "7cc0a8d892866966e291e5c826113b3b8288b6c4fd153a41b0ef8225864c7247"
    ),
    "executable.doctest-fail.v1": (
        "1640ebb307d47b89a799c9f006cd98b6d16fd198414f3a4c55aa994a898fc1ef"
    ),
    "executable.pytest-pass.v1": (
        "5608490af215c97cdb2f34058ccabaefbbcc480f48becec1061d9ed465bbb06f"
    ),
    "executable.pytest-fail.v1": (
        "b4ef00918769f0e7100d8d61fadd4f608e61864d153263d14197c17dedb69c8d"
    ),
    "executable.timeout.v1": ("b38af6360dc9f080489c7ca6542eebb905b814740688dbd42523e525cbb76f1d"),
    "executable.unavailable.v1": (
        "41d785236257405ebb363fceb739be24d9da673d18daaabca7a4be2690c3bc8e"
    ),
    "executable.budget-exhaustion.v1": (
        "d507d5ebbb173b964b9f2255c22058b275cf9cc7cbefed180d1a530242eb723a"
    ),
    "semantic.fast-success.v1": (
        "4c0b891c42e1d33f7c275858cf58fc7c16bd4e57c359a986c8e7c25ecc6764a3"
    ),
    "semantic.strong-success.v1": (
        "04ac15067fb6b35756fbd4487884563751b6327ebc73a8cb5321dbd2bc334237"
    ),
    "semantic.two-failures-abstain.v1": (
        "f2a8b727432b5db3b6065f7ad0756ba9db1d55894c2362da829cc5c7c5c06c39"
    ),
}


class Stage3CatalogAuditError(ValueError):
    def __init__(self, errors: Iterable[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("stage3-v1 catalog audit failed:\n- " + "\n- ".join(self.errors))


def default_stage3_dataset_root() -> Path:
    resource_root = resources.files("drift_agent.evaluation").joinpath(
        "data",
        "stage3",
        "v1",
    )
    if isinstance(resource_root, Path) and resource_root.is_dir():
        return resource_root
    root = Path(__file__).resolve().parents[3] / "evals/datasets/stage3/v1"
    if not root.is_dir():
        raise FileNotFoundError(
            "stage3-v1 dataset is not available in the installed distribution "
            "or source checkout; pass an explicit dataset_root"
        )
    return root


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise Stage3CatalogAuditError((f"{path}: cannot read JSON: {error}",)) from error


def load_stage3_case_manifest(path: Path) -> Stage3CaseManifest:
    try:
        return Stage3CaseManifest.model_validate(_read_json(path))
    except ValidationError as error:
        raise Stage3CatalogAuditError((f"{path}: invalid case manifest: {error}",)) from error


def load_stage3_catalog_manifest(dataset_root: Path) -> Stage3CatalogManifest:
    path = dataset_root / "catalog.json"
    try:
        return Stage3CatalogManifest.model_validate(_read_json(path))
    except ValidationError as error:
        raise Stage3CatalogAuditError((f"{path}: invalid catalog manifest: {error}",)) from error


def _audit_fixture_files(
    case_root: Path,
    manifest: Stage3CaseManifest,
    errors: list[str],
) -> tuple[int, int]:
    listed_paths = {fixture.path for fixture in manifest.files}
    discovered_paths: set[str] = set()
    for path in case_root.rglob("*"):
        if path.is_symlink():
            errors.append(f"{manifest.case_id}: symlink fixture is forbidden: {path}")
            continue
        if path.is_file() and path.name != "manifest.json":
            discovered_paths.add(path.relative_to(case_root).as_posix())
    if discovered_paths != listed_paths:
        errors.append(f"{manifest.case_id}: listed fixture files differ from discovered files")

    total_bytes = 0
    for fixture in manifest.files:
        path = case_root / PurePosixPath(fixture.path)
        try:
            raw = path.read_bytes()
        except OSError as error:
            errors.append(f"{manifest.case_id}: cannot read {fixture.path}: {error}")
            continue
        total_bytes += len(raw)
        if fixture.origin != "project_authored":
            errors.append(f"{manifest.case_id}: stage3-v1 fixtures must be project-authored")
        if len(raw) != fixture.size_bytes:
            errors.append(f"{manifest.case_id}: size mismatch for {fixture.path}")
        if _sha256(raw) != fixture.sha256:
            errors.append(f"{manifest.case_id}: sha256 mismatch for {fixture.path}")
    if len(manifest.files) > MAX_STAGE3_CASE_FILES:
        errors.append(f"{manifest.case_id}: fixture file cap exceeded")
    if total_bytes > MAX_STAGE3_CASE_BYTES:
        errors.append(f"{manifest.case_id}: fixture byte cap exceeded")
    return len(manifest.files), total_bytes


def _audit_changed_bytes(manifest: Stage3CaseManifest, errors: list[str]) -> None:
    base = {fixture.target_path: fixture for fixture in manifest.files if fixture.role == "base"}
    current = dict(base)
    for rename in manifest.workspace.renames:
        moved = current.pop(rename.old_path, None)
        if moved is None:
            errors.append(f"{manifest.case_id}: rename source absent: {rename.old_path}")
        else:
            current[rename.new_path] = moved
    for path in manifest.workspace.deleted_paths:
        if current.pop(path, None) is None:
            errors.append(f"{manifest.case_id}: deleted path absent: {path}")
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
        if (
            change.before_sha256,
            change.before_size_bytes,
            change.before_mode,
        ) != (before.sha256, before.size_bytes, "0644"):
            errors.append(f"{manifest.case_id}: before-byte oracle mismatch for {change.path}")
        if (
            change.after_sha256,
            change.after_size_bytes,
            change.after_mode,
        ) != (after.sha256, after.size_bytes, "0644"):
            errors.append(f"{manifest.case_id}: after-byte oracle mismatch for {change.path}")


def _perform_stage3_audit(
    dataset_root: Path,
) -> tuple[Stage3CatalogAudit, tuple[Stage3CaseManifest, ...]]:
    root = dataset_root.resolve()
    catalog = load_stage3_catalog_manifest(root)
    errors: list[str] = []
    catalog_ids = tuple(entry.case_id for entry in catalog.cases)
    if catalog_ids != FROZEN_STAGE3_CASE_IDS:
        errors.append(
            "catalog case IDs/order differ: "
            f"expected={FROZEN_STAGE3_CASE_IDS}, actual={catalog_ids}"
        )
    if len(set(catalog_ids)) != len(catalog_ids):
        errors.append("catalog case IDs must be unique")
    manifest_paths = tuple(entry.manifest for entry in catalog.cases)
    if len(set(manifest_paths)) != len(manifest_paths):
        errors.append("catalog manifest paths must be unique")
    discovered = {
        path.relative_to(root).as_posix() for path in root.glob("*/manifest.json") if path.is_file()
    }
    if discovered != set(manifest_paths):
        errors.append("catalog entries must list every and only case manifest")

    manifests: list[Stage3CaseManifest] = []
    total_files = 0
    total_bytes = 0
    coverage: set[str] = set()
    for entry in catalog.cases:
        manifest_path = root / PurePosixPath(entry.manifest)
        try:
            raw = manifest_path.read_bytes()
        except OSError as error:
            errors.append(f"{entry.case_id}: cannot read manifest: {error}")
            continue
        digest = _sha256(raw)
        if digest != entry.sha256:
            errors.append(f"{entry.case_id}: catalog manifest hash mismatch")
        if digest != FROZEN_STAGE3_MANIFEST_SHA256.get(entry.case_id):
            errors.append(f"{entry.case_id}: frozen manifest hash changed")
        try:
            manifest = Stage3CaseManifest.model_validate_json(raw)
        except ValidationError as error:
            errors.append(f"{entry.case_id}: invalid case manifest: {error}")
            continue
        manifests.append(manifest)
        if manifest.case_id != entry.case_id:
            errors.append(f"{entry.case_id}: manifest case_id mismatch")
            continue
        files, size = _audit_fixture_files(manifest_path.parent, manifest, errors)
        _audit_changed_bytes(manifest, errors)
        total_files += files
        total_bytes += size
        coverage.update(manifest.coverage_tags)
    if total_files > MAX_STAGE3_DATASET_FILES:
        errors.append("stage3-v1 dataset file cap exceeded")
    if total_bytes > MAX_STAGE3_DATASET_BYTES:
        errors.append("stage3-v1 dataset byte cap exceeded")
    missing_coverage = sorted(REQUIRED_STAGE3_COVERAGE_TAGS - coverage)
    if missing_coverage:
        errors.append(f"dataset is missing required coverage tags: {missing_coverage}")
    if errors:
        raise Stage3CatalogAuditError(errors)
    return (
        Stage3CatalogAudit(
            dataset_id="stage3-v1",
            case_ids=FROZEN_STAGE3_CASE_IDS,
            fixture_files=total_files,
            fixture_bytes=total_bytes,
            coverage_tags=tuple(sorted(coverage)),
        ),
        tuple(manifests),
    )


def audit_stage3_catalog(dataset_root: Path | None = None) -> Stage3CatalogAudit:
    root = dataset_root if dataset_root is not None else default_stage3_dataset_root()
    audit, _ = _perform_stage3_audit(root)
    return audit


def load_stage3_catalog(
    dataset_root: Path | None = None,
) -> tuple[Stage3CaseManifest, ...]:
    root = dataset_root if dataset_root is not None else default_stage3_dataset_root()
    _, manifests = _perform_stage3_audit(root)
    return manifests


__all__ = [
    "FROZEN_STAGE3_CASE_IDS",
    "FROZEN_STAGE3_MANIFEST_SHA256",
    "MAX_STAGE3_CASE_BYTES",
    "MAX_STAGE3_CASE_FILES",
    "MAX_STAGE3_DATASET_BYTES",
    "MAX_STAGE3_DATASET_FILES",
    "REQUIRED_STAGE3_COVERAGE_TAGS",
    "Stage3CatalogAuditError",
    "audit_stage3_catalog",
    "default_stage3_dataset_root",
    "load_stage3_case_manifest",
    "load_stage3_catalog",
    "load_stage3_catalog_manifest",
]

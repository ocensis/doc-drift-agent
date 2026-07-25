from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections import Counter
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from drift_agent.evaluation import (
    FROZEN_CASE_IDS,
    MAX_CASE_BYTES,
    MAX_CASE_FILES,
    MAX_DATASET_BYTES,
    MAX_DATASET_FILES,
    CatalogAuditError,
    audit_catalog,
    default_dataset_root,
    load_catalog,
)
from drift_agent.evaluation.models import FixtureFile, Provenance

_REQUIRED_COVERAGE = {
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


def test_frozen_catalog_audit_covers_matrix_provenance_license_and_caps() -> None:
    dataset_root = default_dataset_root()

    audit = audit_catalog(dataset_root)
    manifests = load_catalog(dataset_root)

    assert audit.dataset_id == "structural-v1"
    assert audit.case_ids == FROZEN_CASE_IDS
    assert len(manifests) == len(FROZEN_CASE_IDS) == 8
    assert tuple(manifest.case_id for manifest in manifests) == FROZEN_CASE_IDS
    assert Counter(manifest.project_family for manifest in manifests) == {
        "click": 3,
        "httpx": 1,
        "pydantic": 2,
        "rich": 2,
    }
    assert _REQUIRED_COVERAGE <= set(audit.coverage_tags)
    assert audit.fixture_files == sum(len(manifest.files) for manifest in manifests)
    assert audit.fixture_bytes == sum(
        fixture.size_bytes for manifest in manifests for fixture in manifest.files
    )
    assert audit.copied_bytes == sum(manifest.provenance.copied_bytes for manifest in manifests)
    assert audit.fixture_files <= MAX_DATASET_FILES
    assert audit.fixture_bytes <= MAX_DATASET_BYTES

    for manifest in manifests:
        assert manifest.operation == "repair"
        assert manifest.model_calls == 0
        assert manifest.offline is True
        assert 0 < len(manifest.files) <= MAX_CASE_FILES
        assert sum(fixture.size_bytes for fixture in manifest.files) <= MAX_CASE_BYTES
        assert manifest.provenance.license_spdx != "NOASSERTION"
        upstream_bytes = sum(
            fixture.size_bytes for fixture in manifest.files if fixture.origin == "upstream"
        )
        assert manifest.provenance.copied_bytes == upstream_bytes
        if manifest.provenance.kind == "project_authored":
            assert manifest.provenance.license_spdx == "LicenseRef-Project-Authored"
            assert manifest.provenance.copied_bytes == 0
            assert all(fixture.origin == "project_authored" for fixture in manifest.files)
        else:
            expected_license = {
                "httpx": "BSD-3-Clause",
                "pydantic": "MIT",
                "rich": "MIT",
            }[manifest.project_family]
            assert manifest.provenance.license_spdx == expected_license
            assert manifest.provenance.source_urls
            assert all(url.startswith("https://") for url in manifest.provenance.source_urls)
            assert re.fullmatch(r"[0-9a-f]{40}", manifest.provenance.code_revision)
            assert re.fullmatch(r"[0-9a-f]{40}", manifest.provenance.doc_revision)

    by_id = {manifest.case_id: manifest for manifest in manifests}
    click_multi = by_id["click.multi-group-partial.v1"]
    assert {
        "rename",
        "delete",
        "same_file_multi",
        "cross_file_multi",
        "partial",
    } <= set(click_multi.coverage_tags)
    assert click_multi.expected.status == "partial"
    assert click_multi.expected.dispositions == ("fixed", "fixed", "needs_approval")
    assert click_multi.expected.reason_codes == (
        "validated",
        "validated",
        "truth_requires_approval",
    )

    for case_id in (
        "httpx.responseclosed-streamclosed.v1",
        "rich.iteration-speed-column.v1",
    ):
        historical_class_case = by_id[case_id]
        assert "conservative_rejection" in historical_class_case.coverage_tags
        assert historical_class_case.expected.status == "unresolved"
        assert historical_class_case.expected.dispositions == ("unresolved",)
        assert historical_class_case.expected.reason_codes == ("unsupported.symbol_kind",)
        assert historical_class_case.expected.changed_bytes == ()


@pytest.mark.parametrize(
    ("payload", "message"),
    (
        (
            {
                "kind": "project_authored",
                "repository": "project://fixture",
                "code_revision": "structural-v1",
                "doc_revision": "structural-v1",
                "source_urls": (),
                "license_spdx": "MIT",
                "copied_bytes": 0,
            },
            "synthetic fixtures require project-authored license",
        ),
        (
            {
                "kind": "project_authored",
                "repository": "project://fixture",
                "code_revision": "structural-v1",
                "doc_revision": "structural-v1",
                "source_urls": (),
                "license_spdx": "LicenseRef-Project-Authored",
                "copied_bytes": 1,
            },
            "synthetic fixtures must declare copied_bytes=0",
        ),
        (
            {
                "kind": "historical",
                "repository": "https://example.test/project",
                "code_revision": "a" * 40,
                "doc_revision": "b" * 40,
                "source_urls": ("https://example.test/commit/a",),
                "license_spdx": "NOASSERTION",
                "copied_bytes": 1,
            },
            "evaluation fixtures require an asserted SPDX license",
        ),
        (
            {
                "kind": "historical",
                "repository": "https://example.test/project",
                "code_revision": "a" * 40,
                "doc_revision": "b" * 40,
                "source_urls": ("http://example.test/commit/a",),
                "license_spdx": "MIT",
                "copied_bytes": 1,
            },
            "historical source URLs must use HTTPS",
        ),
        (
            {
                "kind": "historical",
                "repository": "https://example.test/project",
                "code_revision": "A" * 40,
                "doc_revision": "b" * 40,
                "source_urls": ("https://example.test/commit/a",),
                "license_spdx": "MIT",
                "copied_bytes": 1,
            },
            "historical revisions must be full lowercase Git SHAs",
        ),
    ),
)
def test_provenance_model_rejects_unlicensed_or_unfixed_sources(
    payload: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        Provenance.model_validate(payload)


def test_fixture_model_rejects_repository_escape() -> None:
    with pytest.raises(ValidationError, match="fixture paths must be relative"):
        FixtureFile(
            path="../outside.py",
            target_path="src/project/api.py",
            role="base",
            origin="project_authored",
            sha256="a" * 64,
            size_bytes=1,
        )


def _read_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast(dict[str, Any], value)


def _write_json_object(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _tamper_dataset(dataset_root: Path, tamper: str) -> None:
    if tamper == "catalog_case_id":
        path = dataset_root / "catalog.json"
        catalog = _read_json_object(path)
        cases = cast(list[dict[str, Any]], catalog["cases"])
        cases[0]["case_id"] = "click.parameter-default.tampered"
        _write_json_object(path, catalog)
        return

    if tamper in {"source_revision", "spdx_license", "copied_bytes"}:
        path = dataset_root / "httpx.responseclosed-streamclosed.v1/manifest.json"
        manifest = _read_json_object(path)
        provenance = cast(dict[str, Any], manifest["provenance"])
        if tamper == "source_revision":
            provenance["code_revision"] = "0" * 40
        elif tamper == "spdx_license":
            provenance["license_spdx"] = "MIT"
        else:
            provenance["copied_bytes"] = cast(int, provenance["copied_bytes"]) + 1
        _write_json_object(path, manifest)
        return

    assert tamper == "case_caps"
    case_root = dataset_root / "click.parameter-default.v1"
    path = case_root / "manifest.json"
    manifest = _read_json_object(path)
    files = cast(list[dict[str, Any]], manifest["files"])
    extra_count = MAX_CASE_FILES - len(files) + 1
    for index in range(extra_count):
        raw = b"x" * (MAX_CASE_BYTES + 1) if index == 0 else b"x"
        relative_path = f"base/cap-{index}.bin"
        fixture_path = case_root / relative_path
        fixture_path.write_bytes(raw)
        files.append(
            {
                "path": relative_path,
                "target_path": f"cap-{index}.bin",
                "role": "base",
                "origin": "project_authored",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size_bytes": len(raw),
            }
        )
    _write_json_object(path, manifest)


@pytest.mark.parametrize(
    ("tamper", "expected_errors"),
    (
        ("catalog_case_id", ("catalog case IDs/order differ",)),
        (
            "source_revision",
            ("provenance/license/revisions differ from frozen catalog",),
        ),
        (
            "spdx_license",
            ("provenance/license/revisions differ from frozen catalog",),
        ),
        ("copied_bytes", ("copied_bytes is",)),
        ("case_caps", ("files exceeds 16", "bytes exceeds 65536")),
    ),
)
def test_tampered_catalog_or_manifest_is_rejected_before_replay(
    tmp_path: Path,
    tamper: str,
    expected_errors: tuple[str, ...],
) -> None:
    dataset_root = tmp_path / tamper
    shutil.copytree(default_dataset_root(), dataset_root)
    _tamper_dataset(dataset_root, tamper)

    with pytest.raises(CatalogAuditError) as error:
        audit_catalog(dataset_root)

    details = "\n".join(error.value.errors)
    for expected in expected_errors:
        assert expected in details

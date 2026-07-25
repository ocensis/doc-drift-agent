from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, cast

import pytest

from drift_agent.evaluation.stage3_catalog import (
    FROZEN_STAGE3_CASE_IDS,
    MAX_STAGE3_CASE_BYTES,
    MAX_STAGE3_CASE_FILES,
    MAX_STAGE3_DATASET_BYTES,
    MAX_STAGE3_DATASET_FILES,
    REQUIRED_STAGE3_COVERAGE_TAGS,
    Stage3CatalogAuditError,
    audit_stage3_catalog,
    default_stage3_dataset_root,
    load_stage3_catalog,
)


def test_stage3_catalog_freezes_matrix_hashes_caps_and_routing_oracles() -> None:
    audit = audit_stage3_catalog()
    manifests = load_stage3_catalog()

    assert audit.dataset_id == "stage3-v1"
    assert audit.case_ids == FROZEN_STAGE3_CASE_IDS
    assert len(manifests) == len(FROZEN_STAGE3_CASE_IDS) == 10
    assert audit.fixture_files <= MAX_STAGE3_DATASET_FILES
    assert audit.fixture_bytes <= MAX_STAGE3_DATASET_BYTES
    assert REQUIRED_STAGE3_COVERAGE_TAGS <= set(audit.coverage_tags)
    assert tuple(manifest.case_id for manifest in manifests) == FROZEN_STAGE3_CASE_IDS

    for manifest in manifests:
        assert manifest.offline is True
        assert 0 < len(manifest.files) <= MAX_STAGE3_CASE_FILES
        assert sum(item.size_bytes for item in manifest.files) <= MAX_STAGE3_CASE_BYTES
        assert manifest.provenance.kind == "project_authored"
        assert manifest.provenance.license_spdx == "LicenseRef-Project-Authored"
        assert all(item.origin == "project_authored" for item in manifest.files)
        if manifest.case_kind == "executable":
            assert manifest.model_script == ()
            assert manifest.expected.accounting.model_calls == 0
        else:
            assert manifest.semantic_repair is True
            assert manifest.model_script

    by_id = {manifest.case_id: manifest for manifest in manifests}
    assert by_id["semantic.fast-success.v1"].expected.accounting.patch_attempts == 1
    assert by_id["semantic.strong-success.v1"].expected.accounting.patch_attempts == 2
    assert (
        by_id["semantic.two-failures-abstain.v1"].expected.accounting.repair_outcome == "abstained"
    )


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast(dict[str, Any], value)


@pytest.mark.parametrize("tamper", ["fixture", "manifest", "unlisted"])
def test_stage3_catalog_rejects_tampering_before_replay(
    tmp_path: Path,
    tamper: str,
) -> None:
    dataset = tmp_path / "stage3-v1"
    shutil.copytree(default_stage3_dataset_root(), dataset)
    case = dataset / "semantic.fast-success.v1"
    if tamper == "fixture":
        path = case / "base/docs/api.md"
        path.write_text(path.read_text(encoding="utf-8") + "tampered\n", encoding="utf-8")
    elif tamper == "manifest":
        path = case / "manifest.json"
        payload = _read_object(path)
        payload["coverage_tags"] = ["semantic", "tampered"]
        path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    else:
        (case / "base/unlisted.txt").write_text("unlisted\n", encoding="utf-8")

    with pytest.raises(Stage3CatalogAuditError):
        audit_stage3_catalog(dataset)

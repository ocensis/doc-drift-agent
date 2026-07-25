from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from drift_agent.domain.enums import RunMode
from drift_agent.domain.models import RunRequest, ScopeSpec


def test_scope_spec_accepts_changed_and_since_contracts() -> None:
    changed = ScopeSpec()
    since = ScopeSpec(kind="since", revision="main~2")

    assert changed.kind == "changed"
    assert changed.revision is None
    assert changed.model_dump(mode="json", exclude_none=True) == {"kind": "changed"}
    assert since.model_dump(mode="json", exclude_none=True) == {
        "kind": "since",
        "revision": "main~2",
    }
    assert changed.model_dump(mode="json") == {"kind": "changed"}


def test_scope_spec_json_schema_is_tagged_and_matches_default_serialization() -> None:
    schema = ScopeSpec.model_json_schema()

    assert schema["oneOf"][0] == {
        "type": "object",
        "properties": {"kind": {"const": "changed", "default": "changed", "type": "string"}},
        "additionalProperties": False,
    }
    assert schema["oneOf"][1]["required"] == ["kind", "revision"]
    assert schema["oneOf"][1]["additionalProperties"] is False

    request = RunRequest(mode=RunMode.CHECK, repo_path=Path("."))
    assert request.model_dump(mode="json")["scope"] == {"kind": "changed"}
    assert RunRequest.model_json_schema()["$defs"]["ScopeSpec"] == schema


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "changed", "revision": "HEAD~1"},
        {"kind": "changed", "revision": None},
        {"kind": "since"},
        {"kind": "since", "revision": ""},
        {"kind": "since", "revision": " \t "},
        {"kind": "since", "revision": "HEAD\nmain"},
        {"kind": "since", "revision": "HEAD\x7f"},
        {"kind": "since", "revision": "--verify"},
        {"kind": "since", "revision": b"HEAD"},
        {"kind": "since", "revision": "a" * 1025},
        {"kind": "future"},
        {"kind": "changed", "unexpected": True},
    ],
    ids=[
        "changed-with-revision",
        "changed-with-null-revision",
        "since-without-revision",
        "since-empty-revision",
        "since-blank-revision",
        "since-control-character",
        "since-delete-character",
        "since-option-like-revision",
        "since-non-string-revision",
        "since-overlong-revision",
        "unknown-kind",
        "extra-field",
    ],
)
def test_scope_spec_rejects_invalid_kind_revision_combinations(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        ScopeSpec.model_validate(payload)

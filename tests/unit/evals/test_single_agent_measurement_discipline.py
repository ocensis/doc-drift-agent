"""The eval harness must not silently bound what an Agent may deliver.

A size cap anywhere between the model and the score turns a truncated answer
into a low recall number, which is indistinguishable from a model that simply
did not find the drift.  These tests pin the "no caps" property that the
tool-portfolio measurements depend on.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

SINGLE_AGENT_DIR = Path(__file__).resolve().parents[3] / "evals" / "field" / "single_agent"
sys.path.insert(0, str(SINGLE_AGENT_DIR))

from _runner import (  # noqa: E402
    BASE_TOOLS,
    AgentContext,
    EvalSubmission,
    SingleAgentRunner,
    UnboundedRepoToolbox,
    generic_extra_tools,
)


def _context(repo: Path, revision: str = "frozen-revision") -> AgentContext:
    return AgentContext(
        repo_path=repo,
        baseline_revision=revision,
        head_revision=revision,
    )


def test_submit_schema_has_no_string_or_collection_size_caps(tmp_path: Path) -> None:
    schema = EvalSubmission.model_json_schema()
    encoded_schema = json.dumps(schema, sort_keys=True)

    assert "maxLength" not in encoded_schema
    assert "maxItems" not in encoded_schema

    definitions = SingleAgentRunner.tool_definitions(
        BASE_TOOLS,
        generic_extra_tools(_context(tmp_path)),
    )
    submit = next(item for item in definitions if item["function"]["name"] == "submit")
    encoded_tool_schema = json.dumps(submit["function"]["parameters"], sort_keys=True)
    assert "maxLength" not in encoded_tool_schema
    assert "maxItems" not in encoded_tool_schema

    formerly_oversized = {
        "doc": "d" * 513,
        "line": 1,
        "quote": "q" * 201,
        "why": "w" * 801,
        "code_evidence": "e" * 401,
        "confidence": "high",
    }
    validated = EvalSubmission.model_validate(
        {"findings": [formerly_oversized for _index in range(51)]}
    )
    assert len(validated.findings) == 51
    assert EvalSubmission.model_validate({"findings": []}).findings == []
    with pytest.raises(ValidationError):
        EvalSubmission.model_validate({})


def test_unbounded_repo_tools_return_complete_content_and_keep_path_jail(
    tmp_path: Path,
) -> None:
    long_tail = "x" * 500
    source_lines = [f"MATCH-{index}-{long_tail}" for index in range(1, 502)]
    (tmp_path / "large.txt").write_text("\n".join(source_lines) + "\n", encoding="utf-8")
    for index in range(205):
        (tmp_path / f"entry-{index:03}.txt").write_text("other\n", encoding="utf-8")
    toolbox = UnboundedRepoToolbox(tmp_path)

    read = toolbox.read_file("large.txt")
    grep = toolbox.grep(r"^MATCH-", glob="**/*.txt")
    listing = toolbox.list_dir(".")

    assert "501: MATCH-501-" in read
    assert len(read) > 200_000
    assert len(grep.splitlines()) == 501
    assert f"large.txt:501: MATCH-501-{long_tail}" in grep
    assert "entry-204.txt" in listing
    assert "[truncated" not in read + grep + listing

from __future__ import annotations

import dataclasses
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

SINGLE_AGENT_DIR = Path(__file__).resolve().parents[3] / "evals" / "field" / "single_agent"
sys.path.insert(0, str(SINGLE_AGENT_DIR))

import _portfolio_brief as portfolio  # noqa: E402
import alignment_map_agent  # noqa: E402
import brief_diff_agent  # noqa: E402
import brief_diff_doc_map_agent  # noqa: E402
import change_seed_agent  # noqa: E402
import doc_map_agent  # noqa: E402
import portfolio_control_agent  # noqa: E402
from _runner import (  # noqa: E402
    BASE_TOOLS,
    PORTFOLIO_SYSTEM_PROMPT,
    TOOL_PORTFOLIO_PROTOCOL_VERSION,
    AgentContext,
    SingleAgentRunner,
)


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, AgentContext]:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "docs").mkdir()
    (repo / "drift-agent.toml").write_text(
        """\
[project]
source_roots = ["src"]
docs_roots = ["docs"]
include = ["**/*.ts", "**/*.tsx", "**/*.py", "**/*.md", ".env.example"]
exclude = []

[truth]
code_derived = []
design = []
contract = []

[validation]
commands = []
network = false
""",
        encoding="utf-8",
    )
    (repo / "src" / "service.ts").write_text(
        "export function oldService(value: string): string { return value; }\n",
        encoding="utf-8",
    )
    (repo / "docs" / "architecture.md").write_text(
        "# Architecture\n\n## Old service\n\n### Flow\nText.\n",
        encoding="utf-8",
    )
    (repo / "docs" / "operations.md").write_text(
        "# Operations\n\n## Configuration\nText.\n",
        encoding="utf-8",
    )
    (repo / ".env.example").write_text("OLD_FLAG=true\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "brief@example.com")
    _git(repo, "config", "user.name", "Brief")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "baseline")
    baseline = _git(repo, "rev-parse", "HEAD")

    (repo / "src" / "service.ts").write_text(
        """\
export interface ServiceOptions { enabled: boolean; }
export function newService(options: ServiceOptions): string {
  return options.enabled ? 'new' : 'off';
}
export const MODEL_PROVIDER = 'streamlake';
""",
        encoding="utf-8",
    )
    (repo / "docs" / "architecture.md").write_text(
        (
            "# Architecture\n\n## New service\n\n### Flow\n"
            "Text tracks oldService, newService, NEW_FLAG, and src/service.ts.\n"
        ),
        encoding="utf-8",
    )
    (repo / "docs" / "new.md").write_text(
        "# New doc\n\n## Capability\nText.\n",
        encoding="utf-8",
    )
    (repo / ".env.example").write_text("NEW_FLAG=true\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "head")
    head = _git(repo, "rev-parse", "HEAD")
    return repo, AgentContext(repo_path=repo, baseline_revision=baseline, head_revision=head)


def _handler(runtime: Any) -> Any:
    return runtime.extra_tools[portfolio.AUDIT_BRIEF_TOOL][1]


def _component(text: str, name: str) -> str:
    marker = f"# Component: {name}\n"
    tail = text.split(marker, 1)[1]
    # The renderer inserts one structural newline before the next component;
    # it is not part of either cached component body.
    return tail.split("\n# Component: ", 1)[0].removesuffix("\n")


def _forbidden(*_arguments: Any, **_keywords: Any) -> Any:
    raise AssertionError("a forbidden profile dependency was invoked")


def test_profile_specs_are_frozen_and_declare_exact_union() -> None:
    assert portfolio.PORTFOLIO_PROTOCOL_VERSION == "single-agent-tool-portfolio-v2"
    assert portfolio.BRIEF_DIFF_DOC_MAP_PROFILE.components == (
        *portfolio.BRIEF_DIFF_PROFILE.components,
        *portfolio.DOC_MAP_PROFILE.components,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        portfolio.BRIEF_DIFF_PROFILE.profile_id = "changed"  # type: ignore[misc]


def test_agents_share_portfolio_protocol_prompt_menu_and_audit_schema(tmp_path: Path) -> None:
    _repo_path, context = _repo(tmp_path)
    agents = (
        brief_diff_agent.AGENT,
        doc_map_agent.AGENT,
        change_seed_agent.AGENT,
        alignment_map_agent.AGENT,
        brief_diff_doc_map_agent.AGENT,
    )
    control_runtime = portfolio_control_agent.AGENT.prepare(context)
    control_definitions = SingleAgentRunner.tool_definitions(
        BASE_TOOLS,
        control_runtime.extra_tools,
    )
    audit_schemas = []
    for agent in agents:
        assert agent.protocol_version == TOOL_PORTFOLIO_PROTOCOL_VERSION
        assert agent.system_prompt == PORTFOLIO_SYSTEM_PROMPT
        assert agent.tools == (*BASE_TOOLS, portfolio.AUDIT_BRIEF_TOOL)
        runtime = agent.prepare(context)
        definitions = SingleAgentRunner.tool_definitions(agent.tools, runtime.extra_tools)
        assert definitions[: len(BASE_TOOLS)] == control_definitions
        audit_schemas.append(definitions[-1])
    assert audit_schemas == [portfolio.AUDIT_BRIEF_DEFINITION] * len(agents)
    parameters = audit_schemas[0]["function"]["parameters"]
    assert parameters == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }


def test_runtime_is_lazy_replayable_and_preserves_paged_control_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _repo_path, context = _repo(tmp_path)
    original = portfolio._BUILDERS["brief_diff"]
    calls = 0

    def counted(inner_context: AgentContext) -> Any:
        nonlocal calls
        calls += 1
        return original(inner_context)

    monkeypatch.setitem(portfolio._BUILDERS, "brief_diff", counted)
    runtime = portfolio.portfolio_runtime(context, portfolio.BRIEF_DIFF_PROFILE)

    assert calls == 0
    assert runtime.metadata["generic_profile_id"] == "paged_generic"
    assert runtime.metadata["transport_pagination"]["whole_result_limit"] is None
    assert runtime.metadata["page_queries"] == []
    first = _handler(runtime)({})
    second = _handler(runtime)({})

    assert calls == 1
    assert first == second
    assert runtime.metadata["audit_brief_calls"] == 2
    assert runtime.metadata["audit_brief_cache_hits"] == 1
    assert len(runtime.metadata["audit_brief_handler_seconds"]) == 2
    assert runtime.metadata["component_chars"]["brief_diff"] == len(_component(first, "brief_diff"))
    assert runtime.metadata["brief_chars"] == len(first)
    assert runtime.metadata["portfolio_setup_seconds"] >= 0


def test_brief_diff_has_no_doc_seed_alignment_or_fact_dependency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _repo_path, context = _repo(tmp_path)
    monkeypatch.setattr(portfolio.application, "_documents", _forbidden)
    monkeypatch.setattr(portfolio.application, "_root_markdown_paths", _forbidden)
    monkeypatch.setattr(portfolio, "PythonFactProvider", _forbidden)
    monkeypatch.setattr(portfolio, "TypeScriptFactProvider", _forbidden)
    monkeypatch.setitem(portfolio._BUILDERS, "doc_map", _forbidden)
    monkeypatch.setitem(portfolio._BUILDERS, "change_seed", _forbidden)

    runtime = portfolio.portfolio_runtime(context, portfolio.BRIEF_DIFF_PROFILE)
    output = _handler(runtime)({})
    component = runtime.metadata["component_metadata"]["brief_diff"]

    assert "src/service.ts" in output
    assert "Hunk map" in output
    assert "git_diff" in output
    assert "Selection accounting" in output
    assert component["hard_character_truncation"] is False
    assert component["raw_diff_chars"] > 0
    assert component["brief_chars"] <= component["char_limit"]
    assert "alignment" not in json.dumps(component).lower()


def test_doc_map_invokes_no_seed_alignment_fact_or_change_resolver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _repo_path, context = _repo(tmp_path)
    monkeypatch.setattr(portfolio, "GitScopeResolver", _forbidden)
    monkeypatch.setattr(portfolio, "PythonFactProvider", _forbidden)
    monkeypatch.setattr(portfolio, "TypeScriptFactProvider", _forbidden)
    monkeypatch.setitem(portfolio._BUILDERS, "brief_diff", _forbidden)
    monkeypatch.setitem(portfolio._BUILDERS, "change_seed", _forbidden)

    runtime = portfolio.portfolio_runtime(context, portfolio.DOC_MAP_PROFILE)
    output = _handler(runtime)({})
    component = runtime.metadata["component_metadata"]["doc_map"]

    assert "docs/architecture.md" in output
    assert "# Architecture" in output
    assert "## New service" in output
    assert "line counts" in output
    assert "aligned sites" not in output
    assert component["alignment_sites_collected"] == 0
    assert component["facts_collected"] == 0
    assert component["hard_character_truncation"] is False


def test_change_seed_avoids_full_diff_docs_and_product_seed_caps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _repo_path, context = _repo(tmp_path)
    monkeypatch.setattr(portfolio.application, "_documents", _forbidden)
    monkeypatch.setattr(portfolio.application, "_root_markdown_paths", _forbidden)
    monkeypatch.setitem(portfolio._BUILDERS, "brief_diff", _forbidden)
    monkeypatch.setitem(portfolio._BUILDERS, "doc_map", _forbidden)
    monkeypatch.setattr(portfolio, "_git_text", _forbidden)

    runtime = portfolio.portfolio_runtime(context, portfolio.CHANGE_SEED_PROFILE)
    output = _handler(runtime)({})
    component = runtime.metadata["component_metadata"]["change_seed"]

    assert "src/service.ts" in output
    assert "OLD_FLAG" in output and "NEW_FLAG" in output
    assert "Newly added docs" in output and "docs/new.md" in output
    assert "Inherited product-internal truncation: none" in output
    assert component["full_diff_collected"] is False
    assert component["documents_read"] == 0
    assert component["alignments_collected"] == 0
    assert component["inherited_product_internal_truncation"] == {
        "build_seed_called": False,
        "diff_char_cap_inherited": False,
        "added_doc_char_cap_inherited": False,
        "symbol_list_cap_inherited": False,
    }


def test_alignment_map_returns_only_complete_literal_doc_line_leads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _repo_path, context = _repo(tmp_path)
    monkeypatch.setitem(portfolio._BUILDERS, "change_seed", _forbidden)

    runtime = portfolio.portfolio_runtime(context, portfolio.ALIGNMENT_MAP_PROFILE)
    output = _handler(runtime)({})
    component = runtime.metadata["component_metadata"]["alignment_map"]

    assert "docs/architecture.md:L6" in output
    assert "deleted-symbol:oldService" in output
    assert "added-symbol:newService" in output
    assert "env-added:NEW_FLAG" in output
    assert "changed-path:src/service.ts" in output
    assert "ServiceOptions" not in output
    assert "Text tracks oldService" not in output
    assert "::" not in output
    assert component["alignment_sites"] == 1
    assert component["documents_read"] == 3
    assert component["hard_character_truncation"] is False
    assert component["overflow_behavior"] == "fail_closed_no_partial_output"
    assert component["unmatched_signals_exposed"] is False
    assert component["document_text_returned"] is False
    assert component["full_diff_collected"] is False
    assert component["change_seed_component_called"] is False
    assert component["finalizer_gate_or_store_used"] is False
    assert len(output) <= portfolio.PROFILE_BRIEF_TARGET_CHARS


def test_alignment_map_fails_closed_instead_of_returning_partial_sites(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _repo_path, context = _repo(tmp_path)
    monkeypatch.setattr(portfolio, "_ALIGNMENT_MAP_CHAR_LIMIT", 1)
    runtime = portfolio.portfolio_runtime(context, portfolio.ALIGNMENT_MAP_PROFILE)

    with pytest.raises(ValueError, match="no partial alignment map was returned"):
        _handler(runtime)({})

    assert runtime.metadata["brief_chars"] is None
    assert runtime.metadata["component_chars"] == {}


def test_union_reuses_exact_component_bytes_and_stays_in_one_call_target(
    tmp_path: Path,
) -> None:
    _repo_path, context = _repo(tmp_path)
    diff_runtime = portfolio.portfolio_runtime(context, portfolio.BRIEF_DIFF_PROFILE)
    map_runtime = portfolio.portfolio_runtime(context, portfolio.DOC_MAP_PROFILE)
    union_runtime = portfolio.portfolio_runtime(
        context,
        portfolio.BRIEF_DIFF_DOC_MAP_PROFILE,
    )
    diff_output = _handler(diff_runtime)({})
    map_output = _handler(map_runtime)({})
    union_output = _handler(union_runtime)({})

    assert _component(union_output, "brief_diff") == _component(diff_output, "brief_diff")
    assert _component(union_output, "doc_map") == _component(map_output, "doc_map")
    assert union_runtime.metadata["component_sha256"] == {
        "brief_diff": hashlib.sha256(
            _component(diff_output, "brief_diff").encode("utf-8")
        ).hexdigest(),
        "doc_map": hashlib.sha256(_component(map_output, "doc_map").encode("utf-8")).hexdigest(),
    }
    assert len(union_output) <= portfolio.PROFILE_BRIEF_TARGET_CHARS
    assert union_runtime.metadata["brief_target_exceeded"] is False


def test_dependency_hashes_cover_generic_and_component_dependencies(tmp_path: Path) -> None:
    _repo_path, context = _repo(tmp_path)
    runtime = portfolio.portfolio_runtime(context, portfolio.BRIEF_DIFF_DOC_MAP_PROFILE)
    expected = json.dumps(
        runtime.metadata["dependencies"],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    assert (
        runtime.metadata["dependency_sha256"]
        == hashlib.sha256(expected.encode("utf-8")).hexdigest()
    )
    assert runtime.metadata["brief_component_dependencies"] == list(
        portfolio.BRIEF_DIFF_DOC_MAP_PROFILE.dependencies
    )

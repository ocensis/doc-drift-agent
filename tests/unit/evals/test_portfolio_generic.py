from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

SINGLE_AGENT_DIR = Path(__file__).resolve().parents[3] / "evals" / "field" / "single_agent"
sys.path.insert(0, str(SINGLE_AGENT_DIR))

import paged_generic_agent  # noqa: E402
import portfolio_control_agent  # noqa: E402
from _portfolio_generic import paged_generic_runtime  # noqa: E402
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
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "portfolio@example.com")
    _git(repo, "config", "user.name", "Portfolio")
    (repo / "large.txt").write_text(
        "\n".join(f"MATCH line {index} " + "x" * 180 for index in range(1, 121)) + "\n",
        encoding="utf-8",
    )
    (repo / "service.ts").write_text(
        "export function before(): string { return 'before'; }\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "baseline")
    baseline = _git(repo, "rev-parse", "HEAD")
    (repo / "service.ts").write_text(
        "export function after(): string { return 'after'; }\n" + "const tail = 'z';\n" * 400,
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "head")
    head = _git(repo, "rev-parse", "HEAD")
    return repo, AgentContext(repo_path=repo, baseline_revision=baseline, head_revision=head)


def _all_pages(handler: Any, first_arguments: dict[str, Any]) -> list[str]:
    outputs: list[str] = []
    arguments = dict(first_arguments)
    while True:
        output = handler(arguments)
        outputs.append(output)
        header = json.loads(output.splitlines()[0])
        cursor = header["next_cursor"]
        if cursor is None:
            return outputs
        arguments = {"cursor": cursor}


def _body(output: str) -> str:
    return output.partition("\n")[2]


def _assert_page_envelopes(outputs: list[str], *, kind: str, logical_items: int) -> None:
    for index, output in enumerate(outputs, start=1):
        header = json.loads(output.partition("\n")[0])
        body = _body(output)
        assert header["kind"] == kind
        assert header["page"] == index
        assert header["pages"] == len(outputs)
        assert header["logical_items"] == logical_items
        assert header["total_logical_items"] == logical_items
        assert header["returned_chars"] == len(body)
        assert header["returned_items"] >= 0
        if header["returned_items"]:
            assert header["item_start"] is not None
            assert header["item_end"] is not None
            assert header["item_start"] <= header["item_end"]
        else:
            assert header["item_start"] is None
            assert header["item_end"] is None
        assert header["has_more"] is (index < len(outputs))
        assert (header["next_cursor"] is not None) is header["has_more"]


def test_portfolio_control_and_paged_agent_share_protocol_and_prompt() -> None:
    control = portfolio_control_agent.AGENT
    paged = paged_generic_agent.PAGED_GENERIC_AGENT

    assert control.protocol_version == paged.protocol_version == TOOL_PORTFOLIO_PROTOCOL_VERSION
    assert control.system_prompt == paged.system_prompt == PORTFOLIO_SYSTEM_PROMPT
    assert control.tools == paged.tools == BASE_TOOLS
    assert "turn limit" not in PORTFOLIO_SYSTEM_PROMPT.lower()
    assert "graph_context" in PORTFOLIO_SYSTEM_PROMPT
    assert "audit_brief" in PORTFOLIO_SYSTEM_PROMPT


def test_paged_tools_preserve_complete_results_through_cursors(tmp_path: Path) -> None:
    repo_path, context = _repo(tmp_path)
    runtime = paged_generic_runtime(context)

    read_pages = _all_pages(
        runtime.extra_tools["read_file"][1],
        {"path": "large.txt", "max_chars": 4_000},
    )
    grep_pages = _all_pages(
        runtime.extra_tools["grep"][1],
        {"pattern": "MATCH", "glob": "**/*.txt", "max_chars": 4_000},
    )
    diff_pages = _all_pages(
        runtime.extra_tools["git_diff"][1],
        {"path": "service.ts", "context": 8, "max_chars": 4_000},
    )

    assert len(read_pages) > 1
    assert len(grep_pages) > 1
    assert len(diff_pages) > 1
    assert all(len(page) <= 4_000 for page in [*read_pages, *grep_pages, *diff_pages])
    expected_read = [
        f"{number}: {line}"
        for number, line in enumerate(
            (repo_path / "large.txt").read_text(encoding="utf-8").splitlines(),
            start=1,
        )
    ]
    expected_grep = [f"large.txt:{line}" for line in expected_read]
    expected_diff = _git(
        repo_path,
        "diff",
        "--no-ext-diff",
        "--unified=8",
        context.baseline_revision,
        "--",
        "service.ts",
    ).splitlines()
    assert [line for page in read_pages for line in _body(page).splitlines()] == expected_read
    assert [line for page in grep_pages for line in _body(page).splitlines()] == expected_grep
    assert [line for page in diff_pages for line in _body(page).splitlines()] == expected_diff
    _assert_page_envelopes(read_pages, kind="read_file", logical_items=len(expected_read))
    _assert_page_envelopes(grep_pages, kind="grep", logical_items=len(expected_grep))
    _assert_page_envelopes(diff_pages, kind="git_diff", logical_items=len(expected_diff))
    assert all(query["complete_result_resumable"] for query in runtime.metadata["page_queries"])
    assert runtime.metadata["transport_pagination"]["whole_result_limit"] is None
    assert runtime.metadata["transport_pagination"]["cursor_bound_to_tool_kind"] is True
    assert len(runtime.metadata["page_calls"]) == len(read_pages) + len(grep_pages) + len(
        diff_pages
    )
    assert all(call["body_sha256"] for call in runtime.metadata["page_calls"])
    assert all(
        call["page_envelope"]["returned_chars"] == len(_body(output))
        for call, output in zip(
            runtime.metadata["handler_calls"],
            [*read_pages, *grep_pages, *diff_pages],
            strict=True,
        )
    )


def test_cursor_is_bound_to_the_tool_that_created_it(tmp_path: Path) -> None:
    _repo_path, context = _repo(tmp_path)
    runtime = paged_generic_runtime(context)
    read_output = runtime.extra_tools["read_file"][1]({"path": "large.txt", "max_chars": 4_000})
    cursor = json.loads(read_output.partition("\n")[0])["next_cursor"]
    assert cursor is not None

    runtime.extra_tools["read_file"][1]({"path": "large.txt", "max_chars": 8_000})
    resumed = runtime.extra_tools["read_file"][1]({"cursor": cursor})
    assert json.loads(resumed.partition("\n")[0])["page"] == 2

    with pytest.raises(ValueError, match="cursor belongs to read_file, not grep"):
        runtime.extra_tools["grep"][1]({"cursor": cursor})

    rejected = runtime.metadata["handler_calls"][-1]
    assert rejected["tool"] == "grep"
    assert rejected["error"] is True
    assert rejected["error_type"] == "ValueError"


def test_list_dir_and_changed_files_are_fully_resumable(tmp_path: Path) -> None:
    repo_path, initial_context = _repo(tmp_path)
    catalog = repo_path / "catalog"
    catalog.mkdir()
    for index in range(180):
        name = f"entry_{index:04d}_{'x' * 40}.txt"
        (catalog / name).write_text(f"entry {index}\n", encoding="utf-8")
    _git(repo_path, "add", "-A")
    _git(repo_path, "commit", "-q", "-m", "large catalog")
    context = AgentContext(
        repo_path=repo_path,
        baseline_revision=initial_context.baseline_revision,
        head_revision=_git(repo_path, "rev-parse", "HEAD"),
    )
    runtime = paged_generic_runtime(context)

    list_pages = _all_pages(
        runtime.extra_tools["list_dir"][1],
        {"path": "catalog", "max_chars": 4_000},
    )
    changed_pages = _all_pages(
        runtime.extra_tools["git_changed_files"][1],
        {"max_chars": 4_000},
    )
    expected_list = [child.name for child in sorted(catalog.iterdir())]
    expected_changed = _git(
        repo_path,
        "diff",
        "--name-status",
        context.baseline_revision,
        "--",
    ).splitlines()

    assert len(list_pages) > 1
    assert len(changed_pages) > 1
    assert [line for page in list_pages for line in _body(page).splitlines()] == expected_list
    assert [line for page in changed_pages for line in _body(page).splitlines()] == expected_changed
    _assert_page_envelopes(list_pages, kind="list_dir", logical_items=len(expected_list))
    _assert_page_envelopes(
        changed_pages,
        kind="git_changed_files",
        logical_items=len(expected_changed),
    )


def test_compact_diff_index_lists_every_file_and_hunk_without_full_diff(tmp_path: Path) -> None:
    _repo_path, context = _repo(tmp_path)
    runtime = paged_generic_runtime(context)
    output = runtime.extra_tools["git_diff"][1]({"max_chars": 16_000})

    assert "FILE service.ts" in output
    assert "@@" in output
    assert "export function after" in output
    assert "call git_diff with path" in output
    assert output.count("const tail = 'z';") < 400


def test_paged_base_schema_is_explicit_and_cursor_resumable(tmp_path: Path) -> None:
    _repo_path, context = _repo(tmp_path)
    runtime = paged_generic_runtime(context)
    definitions = SingleAgentRunner.tool_definitions(BASE_TOOLS, runtime.extra_tools)
    by_name = {item["function"]["name"]: item["function"] for item in definitions}

    assert list(by_name) == list(BASE_TOOLS)
    for name in (
        "read_file",
        "grep",
        "list_dir",
        "git_changed_files",
        "git_diff",
        "git_show",
    ):
        properties = by_name[name]["parameters"]["properties"]
        assert "cursor" in properties
        assert "max_chars" in properties
    assert by_name["git_diff"]["parameters"]["properties"]["context"]["maximum"] == 40

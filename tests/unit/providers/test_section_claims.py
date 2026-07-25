from pathlib import Path

from drift_agent.hashing import sha256_bytes
from drift_agent.providers.section_claims import SectionClaim, SectionClaimProvider


def _write(tmp_path: Path, name: str, text: str) -> bytes:
    raw = text.encode("utf-8")
    (tmp_path / name).write_bytes(raw)
    return raw


def _collect(tmp_path: Path, doc_paths: list[str]) -> list[SectionClaim]:
    return SectionClaimProvider().collect(tmp_path, doc_paths)


def test_chinese_doc_sections_have_exact_byte_anchors(tmp_path: Path) -> None:
    preamble = "前言不属于任何章节。\n"
    text = (
        f"{preamble}"
        "# 架构总览\n"
        "系统由 `DriftPlanner` 调度,入口在 src/drift_agent/cli.py 中。\n"
        "## 数据流\n"
        "文档与代码的对齐依赖模型判断。\n"
        "#### 深层标题不拆分\n"
        "仍属于数据流章节。\n"
    )
    raw = _write(tmp_path, "arch.md", text)

    claims = _collect(tmp_path, ["arch.md"])

    assert [claim.heading for claim in claims] == ["架构总览", "数据流"]
    for claim in claims:
        assert raw[claim.start_byte : claim.end_byte].decode("utf-8") == claim.text
        assert claim.path == "arch.md"
        assert claim.source_hash == sha256_bytes(raw)
    first, second = claims
    assert first.line == 2
    assert first.start_byte == len(preamble.encode("utf-8"))
    assert first.text.startswith("# 架构总览\n")
    assert first.end_byte == second.start_byte
    assert second.line == 4
    assert second.end_byte == len(raw)
    assert "#### 深层标题不拆分" in second.text


def test_crlf_documents_keep_byte_accurate_anchors(tmp_path: Path) -> None:
    text = "# 第一章\r\n正文一。\r\n# 第二章\r\n正文二。\r\n"
    raw = _write(tmp_path, "crlf.md", text)

    claims = _collect(tmp_path, ["crlf.md"])

    assert [claim.heading for claim in claims] == ["第一章", "第二章"]
    assert [claim.line for claim in claims] == [1, 3]
    for claim in claims:
        assert raw[claim.start_byte : claim.end_byte].decode("utf-8") == claim.text
    assert claims[0].end_byte == claims[1].start_byte
    assert claims[1].end_byte == len(raw)


def test_fenced_code_blocks_protect_heading_like_lines(tmp_path: Path) -> None:
    text = (
        "# 配置说明\n"
        "```bash\n"
        "# 这不是标题\n"
        "## 也不是\n"
        "```\n"
        "~~~text\n"
        "# tilde 栅栏内的假标题\n"
        "~~~\n"
        "结束。\n"
        "# 下一章\n"
        "内容。\n"
    )
    _write(tmp_path, "config.md", text)

    claims = _collect(tmp_path, ["config.md"])

    assert [claim.heading for claim in claims] == ["配置说明", "下一章"]
    assert "# 这不是标题" in claims[0].text
    assert "# tilde 栅栏内的假标题" in claims[0].text


def test_unclosed_fence_protects_to_end_of_file(tmp_path: Path) -> None:
    text = "# 章节\n内容。\n```\n# 假标题\n"
    raw = _write(tmp_path, "open.md", text)

    claims = _collect(tmp_path, ["open.md"])

    assert [claim.heading for claim in claims] == ["章节"]
    assert claims[0].end_byte == len(raw)
    assert "# 假标题" in claims[0].text


def test_token_extraction_keeps_code_anchors_and_drops_prose(tmp_path: Path) -> None:
    text = (
        "# 锚点\n"
        "调度器 `DriftPlanner` 会读取 `agent.budget.BudgetLedger`,前端配置见 `config/app.tsx`,\n"
        "入口在 src/drift_agent/application.py。短的 `ab`、`不是标识符` 和 `two words` 都忽略,\n"
        "普通词 planner 不算。`DriftPlanner` 重复只记一次,`src/drift_agent/cli.py` 是路径。\n"
    )
    _write(tmp_path, "tokens.md", text)

    claims = _collect(tmp_path, ["tokens.md"])

    assert len(claims) == 1
    assert claims[0].tokens == [
        "DriftPlanner",
        "agent.budget.BudgetLedger",
        "config/app.tsx",
        "src/drift_agent/application.py",
        "src/drift_agent/cli.py",
    ]


def test_bare_source_path_sheds_trailing_sentence_punctuation(tmp_path: Path) -> None:
    text = "# 路径\n模块位于 src/drift_agent/normalization.py.\n"
    _write(tmp_path, "path.md", text)

    claims = _collect(tmp_path, ["path.md"])

    assert claims[0].tokens == ["src/drift_agent/normalization.py"]


def test_tokens_are_capped_at_forty_per_section(tmp_path: Path) -> None:
    spans = " ".join(f"`token{index:02d}`" for index in range(50))
    _write(tmp_path, "cap.md", f"# 大量锚点\n{spans}\n")

    claims = _collect(tmp_path, ["cap.md"])

    assert len(claims) == 1
    assert claims[0].tokens == [f"token{index:02d}" for index in range(40)]


def test_sections_without_content_are_skipped(tmp_path: Path) -> None:
    text = "# 空章节\n\n# 有内容\n正文。\n# 结尾空\n"
    _write(tmp_path, "blank.md", text)

    claims = _collect(tmp_path, ["blank.md"])

    assert [claim.heading for claim in claims] == ["有内容"]


def test_preamble_before_first_heading_is_not_a_section(tmp_path: Path) -> None:
    text = "只有前言,没有标题。\n换行也一样。\n"
    _write(tmp_path, "preamble.md", text)

    assert _collect(tmp_path, ["preamble.md"]) == []


def test_multi_doc_ordering_is_sorted_by_path_then_position(tmp_path: Path) -> None:
    _write(tmp_path, "b.md", "# B一\n正文。\n# B二\n正文。\n")
    _write(tmp_path, "a.md", "# A一\n正文。\n")

    claims = _collect(tmp_path, ["b.md", "a.md"])

    assert [(claim.path, claim.heading) for claim in claims] == [
        ("a.md", "A一"),
        ("b.md", "B一"),
        ("b.md", "B二"),
    ]
    assert claims == _collect(tmp_path, ["a.md", "b.md"])


def test_unreadable_documents_are_skipped_silently(tmp_path: Path) -> None:
    _write(tmp_path, "good.md", "# 章节\n正文。\n")
    (tmp_path / "binary.md").write_bytes(b"\xff\xfe# not utf-8\n")

    claims = _collect(tmp_path, ["missing.md", "binary.md", "good.md"])

    assert [claim.path for claim in claims] == ["good.md"]

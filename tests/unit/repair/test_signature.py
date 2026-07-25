from pathlib import Path

import pytest

from drift_agent.detectors.structural import StructuralDetector
from drift_agent.providers.markdown_claims import MarkdownClaimProvider
from drift_agent.providers.python_facts import PythonFactProvider
from drift_agent.repair.signature import SignaturePatcher


def test_propose_replaces_only_the_anchored_markdown_stub(tmp_path: Path) -> None:
    package = tmp_path / "src/click_demo"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "api.py").write_text(
        "def echo(message: str, color: bool = True) -> None: ...\n",
        encoding="utf-8",
    )
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "api.md").write_text(
        "### `click_demo.api.echo`\n\n```python\ndef echo(message: str) -> None: ...\n```\n",
        encoding="utf-8",
    )
    fact = PythonFactProvider().collect(
        repo_path=tmp_path,
        source_roots=["src"],
        changed_paths=["src/click_demo/api.py"],
    )[0]
    claim = MarkdownClaimProvider().collect(tmp_path, ["docs/api.md"])[0]
    from drift_agent.domain.models import Alignment

    alignment = Alignment(fact=fact, claim=claim)
    finding = StructuralDetector().detect([alignment])[0]

    attempt = SignaturePatcher().propose(tmp_path, alignment, finding)

    assert attempt.path == "docs/api.md"
    assert attempt.replacement_text == ("def echo(message: str, color: bool = True) -> None: ...")
    assert "color: bool = True" in attempt.unified_diff


def test_propose_rejects_target_replaced_by_symlink_after_extraction(
    tmp_path: Path,
) -> None:
    package = tmp_path / "src/click_demo"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "api.py").write_text(
        "def echo(message: str, color: bool = True) -> None: ...\n",
        encoding="utf-8",
    )
    docs = tmp_path / "docs"
    docs.mkdir()
    path = docs / "api.md"
    path.write_text(
        "### `click_demo.api.echo`\n```python\ndef echo(message: str) -> None: ...\n```\n",
        encoding="utf-8",
    )
    fact = PythonFactProvider().collect(
        repo_path=tmp_path,
        source_roots=["src"],
        changed_paths=["src/click_demo/api.py"],
    )[0]
    claim = MarkdownClaimProvider().collect(tmp_path, ["docs/api.md"])[0]
    from drift_agent.domain.models import Alignment

    alignment = Alignment(fact=fact, claim=claim)
    finding = StructuralDetector().detect([alignment])[0]
    target = tmp_path / "notes/api.md"
    target.parent.mkdir()
    path.replace(target)
    path.symlink_to(target)

    with pytest.raises(RuntimeError, match="symlink"):
        SignaturePatcher().propose(tmp_path, alignment, finding)


def test_propose_preserves_crlf_in_the_fenced_stub(tmp_path: Path) -> None:
    package = tmp_path / "src/click_demo"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "api.py").write_text(
        "def echo(message: str, color: bool = True) -> None: ...\n",
        encoding="utf-8",
    )
    docs = tmp_path / "docs"
    docs.mkdir()
    path = docs / "api.md"
    path.write_bytes(
        b"### `click_demo.api.echo`\r\n```python\r\ndef echo(message: str) -> None: ...\r\n```\r\n"
    )
    fact = PythonFactProvider().collect(
        repo_path=tmp_path,
        source_roots=["src"],
        changed_paths=["src/click_demo/api.py"],
    )[0]
    claim = MarkdownClaimProvider().collect(tmp_path, ["docs/api.md"])[0]
    from drift_agent.domain.models import Alignment

    alignment = Alignment(fact=fact, claim=claim)
    finding = StructuralDetector().detect([alignment])[0]

    attempt = SignaturePatcher().propose(tmp_path, alignment, finding)

    original = path.read_bytes()
    proposed = (
        original[: attempt.start_byte]
        + attempt.replacement_text.encode("utf-8")
        + original[attempt.end_byte :]
    )
    assert attempt.replacement_text.endswith("...")
    assert proposed == (
        b"### `click_demo.api.echo`\r\n"
        b"```python\r\n"
        b"def echo(message: str, color: bool = True) -> None: ...\r\n"
        b"```\r\n"
    )


def test_propose_uses_byte_offsets_after_a_non_ascii_prefix(tmp_path: Path) -> None:
    package = tmp_path / "src/click_demo"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "api.py").write_text(
        "def echo(message: str, color: bool = True) -> None: ...\n",
        encoding="utf-8",
    )
    docs = tmp_path / "docs"
    docs.mkdir()
    path = docs / "api.md"
    original = (
        "# 概览\n\n"
        "### `click_demo.api.echo`\n\n"
        "```python\n"
        "def echo(message: str) -> None: ...\n"
        "```\n"
    ).encode()
    path.write_bytes(original)
    fact = PythonFactProvider().collect(
        repo_path=tmp_path,
        source_roots=["src"],
        changed_paths=["src/click_demo/api.py"],
    )[0]
    claim = MarkdownClaimProvider().collect(tmp_path, ["docs/api.md"])[0]
    from drift_agent.domain.models import Alignment

    alignment = Alignment(fact=fact, claim=claim)
    finding = StructuralDetector().detect([alignment])[0]

    attempt = SignaturePatcher().propose(tmp_path, alignment, finding)

    proposed = (
        original[: attempt.start_byte]
        + attempt.replacement_text.encode()
        + original[attempt.end_byte :]
    )
    assert proposed.startswith("# 概览".encode())
    assert proposed.endswith(b"```\n")
    assert "-def echo(message: str)" in attempt.unified_diff
    assert "+def echo(message: str, color: bool = True)" in attempt.unified_diff


def test_propose_separates_diff_records_for_an_eof_stub_without_newline(
    tmp_path: Path,
) -> None:
    package = tmp_path / "src/click_demo"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "api.py").write_text(
        "def echo(message: str, color: bool = True) -> None: ...\n",
        encoding="utf-8",
    )
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "api.md").write_bytes(
        b"### `click_demo.api.echo`\n\n```python\ndef echo(message: str) -> None: ..."
    )
    fact = PythonFactProvider().collect(
        repo_path=tmp_path,
        source_roots=["src"],
        changed_paths=["src/click_demo/api.py"],
    )[0]
    claim = MarkdownClaimProvider().collect(tmp_path, ["docs/api.md"])[0]
    from drift_agent.domain.models import Alignment

    alignment = Alignment(fact=fact, claim=claim)
    finding = StructuralDetector().detect([alignment])[0]

    attempt = SignaturePatcher().propose(tmp_path, alignment, finding)

    assert attempt.replacement_text == ("def echo(message: str, color: bool = True) -> None: ...")
    assert attempt.unified_diff.endswith(
        "-def echo(message: str) -> None: ...\n"
        "\\ No newline at end of file\n"
        "+def echo(message: str, color: bool = True) -> None: ...\n"
        "\\ No newline at end of file\n"
    )

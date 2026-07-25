import subprocess
from pathlib import Path

import pytest


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def drift_repo(tmp_path: Path) -> Path:
    package = tmp_path / "src/click_demo"
    docs = tmp_path / "docs"
    package.mkdir(parents=True)
    docs.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    api = package / "api.py"
    api.write_text("def echo(message: str) -> None: ...\n", encoding="utf-8")
    (docs / "api.md").write_text(
        "### `click_demo.api.echo`\n\n```python\ndef echo(message: str) -> None: ...\n```\n",
        encoding="utf-8",
    )
    (tmp_path / "drift-agent.toml").write_text(
        """\
[project]
source_roots = ["src"]
docs_roots = ["docs"]
include = ["src/**/*.py", "docs/**/*.md"]
exclude = []

[truth]
code_derived = ["docs/**"]
design = []
contract = []

[validation]
commands = []
network = false
""",
        encoding="utf-8",
    )
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    api.write_text(
        "def echo(message: str, color: bool = True) -> None: ...\n",
        encoding="utf-8",
    )
    return tmp_path

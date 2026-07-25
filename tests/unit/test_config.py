from pathlib import Path

import pytest

from drift_agent.config import (
    ConfigurationError,
    ScaffoldError,
    load_config,
    load_config_with_hash,
    scaffold_config,
)
from drift_agent.hashing import sha256_file


def test_load_config_reads_roots_patterns_truth_and_validation(tmp_path: Path) -> None:
    (tmp_path / "drift-agent.toml").write_text(
        """
[project]
source_roots = ["src"]
docs_roots = ["docs"]
include = ["src/**/*.py", "docs/**/*.md"]
exclude = ["**/generated/**"]

[truth]
code_derived = ["docs/api/**"]
design = []
contract = []

[validation]
commands = []
network = false
""".strip(),
        encoding="utf-8",
    )

    config = load_config(tmp_path)

    assert config.project.source_roots == ["src"]
    assert config.truth.code_derived == ["docs/api/**"]
    assert config.validation.network is False
    loaded, source_hash = load_config_with_hash(tmp_path)
    assert loaded == config
    assert source_hash == sha256_file(tmp_path / "drift-agent.toml")


def test_load_config_rejects_absolute_or_parent_paths(tmp_path: Path) -> None:
    (tmp_path / "drift-agent.toml").write_text(
        """
[project]
source_roots = ["../src"]
docs_roots = ["docs"]
include = ["**/*.py"]
exclude = []
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError) as excinfo:
        load_config(tmp_path)

    assert excinfo.value.reason_code == "config.invalid"
    assert "drift-agent init" in str(excinfo.value)


def test_missing_config_raises_actionable_configuration_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError) as excinfo:
        load_config(tmp_path)

    assert excinfo.value.reason_code == "config.missing"
    assert "drift-agent init" in str(excinfo.value)


def test_malformed_toml_raises_invalid_configuration_error(tmp_path: Path) -> None:
    (tmp_path / "drift-agent.toml").write_text("[project\n", encoding="utf-8")

    with pytest.raises(ConfigurationError) as excinfo:
        load_config(tmp_path)

    assert excinfo.value.reason_code == "config.invalid"
    assert "drift-agent init" in str(excinfo.value)


def test_unreadable_config_raises_configuration_error(tmp_path: Path) -> None:
    (tmp_path / "drift-agent.toml").mkdir()

    with pytest.raises(ConfigurationError) as excinfo:
        load_config(tmp_path)

    assert excinfo.value.reason_code == "config.unreadable"
    assert "drift-agent init" in str(excinfo.value)


def test_scaffold_config_infers_src_layout_and_round_trips(tmp_path: Path) -> None:
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "docs").mkdir()

    config_path, config = scaffold_config(tmp_path)

    assert config_path == tmp_path / "drift-agent.toml"
    assert config.project.source_roots == ["src"]
    assert config.project.docs_roots == ["docs"]
    assert config.project.include == ["src/**/*.py", "docs/**/*.md"]
    assert config.truth.code_derived == []
    assert config.validation.commands == []
    assert load_config(tmp_path) == config


def test_scaffold_config_anchors_top_level_packages_at_repo_root(tmp_path: Path) -> None:
    for name in ("alpha", "tests", ".hidden"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "plain").mkdir()

    _, config = scaffold_config(tmp_path)

    assert config.project.source_roots == ["."]
    assert config.project.docs_roots == []
    assert config.project.include == ["alpha/**/*.py"]
    assert load_config(tmp_path) == config


def test_scaffold_config_fails_closed_for_unsupported_layouts(tmp_path: Path) -> None:
    (tmp_path / "script.py").write_text("x = 1\n", encoding="utf-8")

    with pytest.raises(ScaffoldError) as excinfo:
        scaffold_config(tmp_path)

    assert "could not infer a supported source layout" in str(excinfo.value)
    assert not (tmp_path / "drift-agent.toml").exists()


def test_scaffold_config_anchors_src_package_at_repo_root(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "mod.py").write_text("", encoding="utf-8")

    _, config = scaffold_config(tmp_path)

    assert config.project.source_roots == ["."]
    assert config.project.include == ["src/**/*.py"]
    assert load_config(tmp_path) == config


def test_scaffold_config_skips_symlinked_candidates(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    (outside / "beta").mkdir(parents=True)
    (outside / "beta" / "__init__.py").write_text("", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").symlink_to(outside, target_is_directory=True)
    (repo / "beta").symlink_to(outside / "beta", target_is_directory=True)
    (repo / "docs").symlink_to(outside, target_is_directory=True)
    (repo / "alpha").mkdir()
    (repo / "alpha" / "__init__.py").write_text("", encoding="utf-8")

    _, config = scaffold_config(repo)

    assert config.project.source_roots == ["."]
    assert config.project.include == ["alpha/**/*.py"]
    assert config.project.docs_roots == []


def test_scaffold_config_infers_typescript_src_layout(tmp_path: Path) -> None:
    (tmp_path / "tsconfig.json").write_text("{}", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "index.ts").write_text("export const x = 1;\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()

    _, config = scaffold_config(tmp_path)

    assert config.project.source_roots == ["src"]
    assert config.project.docs_roots == ["docs"]
    assert config.project.include == ["src/**/*.ts", "src/**/*.tsx", "docs/**/*.md"]
    assert config.project.exclude == ["**/node_modules/**", "**/dist/**"]
    assert config.truth.code_derived == []
    assert load_config(tmp_path) == config


def test_scaffold_config_infers_typescript_root_level_dirs_via_package_json(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.ts").write_text("export const a = 1;\n", encoding="utf-8")
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "util.tsx").write_text("export const b = 2;\n", encoding="utf-8")

    _, config = scaffold_config(tmp_path)

    assert config.project.source_roots == ["app", "lib"]
    assert config.project.docs_roots == []
    assert config.project.include == [
        "app/**/*.ts",
        "app/**/*.tsx",
        "lib/**/*.ts",
        "lib/**/*.tsx",
    ]
    assert config.project.exclude == ["**/node_modules/**", "**/dist/**"]
    assert load_config(tmp_path) == config


def test_scaffold_config_typescript_scan_skips_node_modules_and_dist(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "node_modules" / "dep").mkdir(parents=True)
    (tmp_path / "node_modules" / "dep" / "index.ts").write_text("", encoding="utf-8")
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "bundle.ts").write_text("", encoding="utf-8")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.ts").write_text("export const a = 1;\n", encoding="utf-8")

    _, config = scaffold_config(tmp_path)

    assert config.project.source_roots == ["app"]
    assert config.project.include == ["app/**/*.ts", "app/**/*.tsx"]


def test_scaffold_config_typescript_requires_a_root_manifest(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "index.ts").write_text("export const x = 1;\n", encoding="utf-8")

    with pytest.raises(ScaffoldError) as excinfo:
        scaffold_config(tmp_path)

    assert "could not infer a supported source layout" in str(excinfo.value)
    assert not (tmp_path / "drift-agent.toml").exists()


def test_scaffold_config_manifest_without_typescript_sources_still_raises(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "index.js").write_text("module.exports = {};\n", encoding="utf-8")

    with pytest.raises(ScaffoldError) as excinfo:
        scaffold_config(tmp_path)

    assert "could not infer a supported source layout" in str(excinfo.value)
    assert not (tmp_path / "drift-agent.toml").exists()


def test_scaffold_config_python_layout_wins_over_typescript(tmp_path: Path) -> None:
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "tsconfig.json").write_text("{}", encoding="utf-8")
    (tmp_path / "src" / "index.ts").write_text("export const x = 1;\n", encoding="utf-8")

    _, config = scaffold_config(tmp_path)

    assert config.project.source_roots == ["src"]
    assert config.project.include == ["src/**/*.py"]
    assert config.project.exclude == ["**/generated/**", "**/.venv/**"]
    assert load_config(tmp_path) == config


def test_scaffold_config_refuses_to_overwrite(tmp_path: Path) -> None:
    (tmp_path / "drift-agent.toml").write_text("", encoding="utf-8")

    with pytest.raises(ScaffoldError) as excinfo:
        scaffold_config(tmp_path)

    assert "already exists" in str(excinfo.value)


def test_scaffold_config_refuses_dangling_symlink_without_writing_target(tmp_path: Path) -> None:
    target = tmp_path / "outside" / "target.toml"
    repo = tmp_path / "repo"
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "drift-agent.toml").symlink_to(target)

    with pytest.raises(ScaffoldError) as excinfo:
        scaffold_config(repo)

    assert "already exists" in str(excinfo.value)
    assert not target.exists()
    assert (repo / "drift-agent.toml").is_symlink()

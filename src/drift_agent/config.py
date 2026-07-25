import json
import tomllib
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from drift_agent.hashing import sha256_bytes
from drift_agent.languages import SOURCE_SUFFIXES


class ConfigurationError(RuntimeError):
    """Raised when drift-agent.toml is missing, unreadable, or invalid."""

    def __init__(self, message: str, *, reason_code: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class ScaffoldError(RuntimeError):
    """Raised when a starter drift-agent.toml cannot be scaffolded."""

    reason_code = "config.scaffold"


def _is_safe_relative(value: str) -> bool:
    path = PurePosixPath(value)
    return not path.is_absolute() and ".." not in path.parts


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectConfig(ConfigModel):
    source_roots: list[str]
    docs_roots: list[str]
    include: list[str]
    exclude: list[str] = Field(default_factory=list)

    @field_validator("source_roots", "docs_roots", "include", "exclude")
    @classmethod
    def validate_relative_paths(cls, values: list[str]) -> list[str]:
        if any(not _is_safe_relative(value) for value in values):
            raise ValueError("paths and patterns must stay inside the repository")
        return values


class TruthConfig(ConfigModel):
    code_derived: list[str] = Field(default_factory=list)
    design: list[str] = Field(default_factory=list)
    contract: list[str] = Field(default_factory=list)

    @field_validator("code_derived", "design", "contract")
    @classmethod
    def validate_relative_patterns(cls, values: list[str]) -> list[str]:
        if any(not _is_safe_relative(value) for value in values):
            raise ValueError("truth patterns must stay inside the repository")
        return values


class ValidationConfig(ConfigModel):
    commands: list[str] = Field(default_factory=list)
    network: bool = False


class DriftConfig(ConfigModel):
    project: ProjectConfig
    truth: TruthConfig = Field(default_factory=TruthConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)


def load_config_with_hash(repo_path: Path) -> tuple[DriftConfig, str]:
    config_path = repo_path / "drift-agent.toml"
    try:
        raw = config_path.read_bytes()
    except FileNotFoundError:
        raise ConfigurationError(
            f"drift-agent.toml not found in {repo_path}; run"
            f" `drift-agent init --repo {repo_path}` to scaffold a starter configuration,"
            " then review the [truth] sections before rerunning",
            reason_code="config.missing",
        ) from None
    except OSError as error:
        raise ConfigurationError(
            f"drift-agent.toml in {repo_path} could not be read: {error}; fix the file, or"
            f" delete it and rerun `drift-agent init --repo {repo_path}`",
            reason_code="config.unreadable",
        ) from error
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise ConfigurationError(
            f"drift-agent.toml in {repo_path} is not valid TOML: {error}; fix it, or delete"
            f" it and rerun `drift-agent init --repo {repo_path}`",
            reason_code="config.invalid",
        ) from error
    try:
        config = DriftConfig.model_validate(data)
    except ValidationError as error:
        raise ConfigurationError(
            f"drift-agent.toml in {repo_path} failed validation: {error}; fix it, or delete"
            f" it and rerun `drift-agent init --repo {repo_path}`",
            reason_code="config.invalid",
        ) from error
    return config, sha256_bytes(raw)


def load_config(repo_path: Path) -> DriftConfig:
    config, _ = load_config_with_hash(repo_path)
    return config


_SCAFFOLD_SKIP_DIRS = frozenset({"build", "dist", "docs", "examples", "node_modules", "tests"})

# Excludes name each language's build/tooling artifact directories. They stay split so a
# Python-only scaffold keeps emitting exactly the earlier bytes.
_PYTHON_EXCLUDE = ["**/generated/**", "**/.venv/**"]
_TYPESCRIPT_EXCLUDE = ["**/node_modules/**", "**/dist/**"]

# A TypeScript layout is only inferred when one of these manifests sits at the repo root.
_TYPESCRIPT_MANIFESTS = ("tsconfig.json", "package.json")
# Sourced from the language table so a new TypeScript suffix flows through automatically.
_TYPESCRIPT_SUFFIXES = frozenset(
    suffix for suffix, language in SOURCE_SUFFIXES.items() if language == "typescript"
)
# Never descend into these when scanning a directory for TypeScript sources.
_TYPESCRIPT_SCAN_SKIP_DIRS = frozenset({"node_modules", "dist", "build"})

_SCAFFOLD_TEMPLATE = """\
# drift-agent configuration, scaffolded by `drift-agent init`.
# Review every section before relying on check or repair results.

[project]
# Roots the agent scans for public Python API and documentation.
source_roots = {source_roots}
docs_roots = {docs_roots}
include = {include}
exclude = {exclude}

[truth]
# Classify documentation globs before enabling repair.
# code_derived: docs the agent may rewrite from code evidence.
# design: human intent; never edited by the agent.
# contract: frozen external contracts; never edited by the agent.
code_derived = []
design = []
contract = []

[validation]
# Optional required oracles. Only doctest/pytest commands with exactly one
# explicit repo-relative target are supported, for example:
#   "python -m doctest docs/api.md"
#   "python -m pytest tests/test_api.py -q"
commands = []
network = false
"""


def _toml_strings(values: list[str]) -> str:
    return "[" + ", ".join(json.dumps(value) for value in values) + "]"


def _package_directories(root: Path) -> list[str]:
    return sorted(
        entry.name
        for entry in root.iterdir()
        if entry.is_dir()
        and not entry.is_symlink()
        and not entry.name.startswith(".")
        and entry.name not in _SCAFFOLD_SKIP_DIRS
        and (entry / "__init__.py").is_file()
    )


def _has_typescript_sources(root: Path) -> bool:
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if not entry.name.startswith(".") and entry.name not in _TYPESCRIPT_SCAN_SKIP_DIRS:
                    stack.append(entry)
            elif entry.suffix in _TYPESCRIPT_SUFFIXES:
                return True
    return False


def _typescript_directories(root: Path) -> list[str]:
    return sorted(
        entry.name
        for entry in root.iterdir()
        if entry.is_dir()
        and not entry.is_symlink()
        and not entry.name.startswith(".")
        and entry.name not in _SCAFFOLD_SKIP_DIRS
        and _has_typescript_sources(entry)
    )


def _typescript_globs(roots: list[str]) -> list[str]:
    return [f"{root}/**/*{suffix}" for root in roots for suffix in sorted(_TYPESCRIPT_SUFFIXES)]


def _python_layout(repo_path: Path) -> tuple[list[str], list[str]] | None:
    source = repo_path / "src"
    if (
        source.is_dir()
        and not source.is_symlink()
        and not (source / "__init__.py").is_file()
        and _package_directories(source)
    ):
        return ["src"], ["src/**/*.py"]
    packages = _package_directories(repo_path)
    if packages:
        return ["."], [f"{name}/**/*.py" for name in packages]
    return None


def _typescript_layout(repo_path: Path) -> tuple[list[str], list[str]] | None:
    if not any(
        (repo_path / manifest).is_file() and not (repo_path / manifest).is_symlink()
        for manifest in _TYPESCRIPT_MANIFESTS
    ):
        return None
    source = repo_path / "src"
    if source.is_dir() and not source.is_symlink() and _has_typescript_sources(source):
        return ["src"], _typescript_globs(["src"])
    directories = _typescript_directories(repo_path)
    if directories:
        return directories, _typescript_globs(directories)
    return None


def _inferred_layout(repo_path: Path) -> tuple[list[str], list[str], list[str]]:
    # Python keeps absolute priority: when a Python layout is inferable the roots, globs,
    # and excludes stay byte-identical to earlier releases. TypeScript is only consulted
    # when Python inference would otherwise fail, so a mixed repo yields the Python layout
    # rather than a merged one (see task note).
    python = _python_layout(repo_path)
    if python is not None:
        source_roots, include = python
        return source_roots, include, _PYTHON_EXCLUDE
    typescript = _typescript_layout(repo_path)
    if typescript is not None:
        source_roots, include = typescript
        return source_roots, include, _TYPESCRIPT_EXCLUDE
    raise ScaffoldError(
        f"could not infer a supported source layout for {repo_path}: expected Python"
        " packages under src/ or top-level packages with __init__.py, or a TypeScript"
        " project (tsconfig.json or package.json) with .ts sources; write drift-agent.toml"
        " by hand for other layouts"
    )


def scaffold_config(repo_path: Path) -> tuple[Path, DriftConfig]:
    config_path = repo_path / "drift-agent.toml"
    if config_path.is_symlink() or config_path.exists():
        raise ScaffoldError(
            f"drift-agent.toml already exists in {repo_path} (or is a symlink); delete it"
            " first to rescaffold"
        )
    source_roots, include, exclude = _inferred_layout(repo_path)
    docs = repo_path / "docs"
    docs_roots = ["docs"] if docs.is_dir() and not docs.is_symlink() else []
    include = [*include, *(f"{root}/**/*.md" for root in docs_roots)]
    text = _SCAFFOLD_TEMPLATE.format(
        source_roots=_toml_strings(source_roots),
        docs_roots=_toml_strings(docs_roots),
        include=_toml_strings(include),
        exclude=_toml_strings(exclude),
    )
    try:
        config = DriftConfig.model_validate(tomllib.loads(text))
    except (tomllib.TOMLDecodeError, ValidationError) as error:
        raise ScaffoldError(
            f"scaffolded configuration for {repo_path} is invalid: {error}"
        ) from error
    try:
        with config_path.open("x", encoding="utf-8") as handle:
            handle.write(text)
    except FileExistsError:
        raise ScaffoldError(
            f"drift-agent.toml already exists in {repo_path} (or is a symlink); delete it"
            " first to rescaffold"
        ) from None
    return config_path, config

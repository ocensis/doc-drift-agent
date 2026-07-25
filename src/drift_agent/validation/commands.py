from __future__ import annotations

import math
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal, Protocol, cast

from drift_agent.domain.enums import ValidationStatus
from drift_agent.domain.models import ValidationResult
from drift_agent.hashing import sha256_file

CommandKind = Literal["doctest", "pytest"]

_SHELL_CONTROL_CHARACTERS = frozenset(";&|<>`$\n\r\0")
_TRUNCATION_MARKER = "\n...[truncated]"
_VALIDATION_IGNORED_NAMES = frozenset(
    {
        ".env",
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
    }
)
_MODEL_PROVIDER_ENVIRONMENT_NAMES = (
    "ANTHROPIC_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "COHERE_API_KEY",
    "DEEPSEEK_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GROQ_API_KEY",
    "MISTRAL_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "OPENROUTER_BASE_URL",
    "OPENROUTER_DATA_COLLECTION",
    "OPENROUTER_FAST_MODEL",
    "OPENROUTER_MODEL",
    "OPENROUTER_PROVIDER",
    "OPENROUTER_STRONG_MODEL",
    "OPENROUTER_TIMEOUT_SECONDS",
    "TOGETHER_API_KEY",
    "XAI_API_KEY",
)
_PYTEST_FLAGS = frozenset(
    {
        "-q",
        "-qq",
        "-v",
        "-vv",
        "-x",
        "--disable-warnings",
        "--strict-config",
        "--strict-markers",
    }
)
_PYTEST_FLAG_PREFIXES = ("--maxfail=", "--tb=")
_DOCTEST_FLAGS = frozenset({"-v"})
_VALIDATION_BOOTSTRAP = """\
from __future__ import annotations

import sys
from pathlib import Path


kind = sys.argv[1]
workspace = Path(sys.argv[2])
arguments = sys.argv[3:]

# Import the allowlisted runner before repository paths become importable.  In
# particular, a repository-local pytest.py/doctest.py cannot shadow it.
if kind == "doctest":
    import doctest
elif kind == "pytest":
    import pytest
else:
    raise SystemExit("validation module is not allowlisted")

sys.path.insert(0, str(workspace))
source_root = workspace / "src"
if source_root.is_dir():
    sys.path.insert(0, str(source_root))

if kind == "doctest":
    sys.argv = ["doctest", *arguments]
    raise SystemExit(doctest._test())
raise SystemExit(pytest.main(arguments))
"""
_NETWORK_GUARD = """\
import socket as _socket


def _drift_agent_network_disabled(*_args, **_kwargs):
    raise OSError("network disabled by drift-agent")


for _name in (
    "create_connection",
    "create_server",
    "getaddrinfo",
    "gethostbyaddr",
    "gethostbyname",
    "gethostbyname_ex",
):
    if hasattr(_socket, _name):
        setattr(_socket, _name, _drift_agent_network_disabled)

for _name in (
    "accept",
    "bind",
    "connect",
    "connect_ex",
    "listen",
    "recvfrom",
    "recvfrom_into",
    "sendmsg",
    "sendto",
):
    if hasattr(_socket.socket, _name):
        setattr(_socket.socket, _name, _drift_agent_network_disabled)
"""


class CommandCompileError(ValueError):
    """Raised when a configured validation command crosses the command boundary."""


class ValidationInputChangedError(RuntimeError):
    """Raised when the disposable copy does not match captured input evidence."""

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(f"validation input changed before execution: {path}")


@dataclass(frozen=True, slots=True)
class CompiledValidationCommand:
    """A shell-free validation invocation normalized to this Python runtime."""

    source: str
    kind: CommandKind
    argv: tuple[str, ...]

    @property
    def targets(self) -> tuple[str, ...]:
        return _validation_targets(self.kind, self.argv[3:])


class ProcessRunner(Protocol):
    """Injectable subprocess boundary used by :class:`ValidationCommandRunner`."""

    def __call__(
        self,
        argv: Sequence[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]: ...


def _unsafe_path(value: str) -> bool:
    candidate = value.split("::", 1)[0]
    if not candidate:
        return False
    posix = PurePosixPath(candidate)
    windows = PureWindowsPath(candidate)
    return (
        posix.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or bool(windows.root)
        or ".." in posix.parts
        or ".." in windows.parts
    )


def _validate_argument(argument: str) -> None:
    if any(character in _SHELL_CONTROL_CHARACTERS for character in argument):
        raise CommandCompileError("validation command contains a shell control character")
    if argument.startswith("@"):
        raise CommandCompileError("validation command argument files are not allowed")
    candidate = argument.partition("=")[2] if argument.startswith("-") else argument
    if candidate and _unsafe_path(candidate):
        raise CommandCompileError("validation command paths must stay inside the repository")


def _validation_targets(kind: CommandKind, arguments: Sequence[str]) -> tuple[str, ...]:
    targets: list[str] = []
    for argument in arguments:
        _validate_argument(argument)
        if argument.startswith("-"):
            allowed = (
                argument in _DOCTEST_FLAGS
                if kind == "doctest"
                else argument in _PYTEST_FLAGS or argument.startswith(_PYTEST_FLAG_PREFIXES)
            )
            if not allowed:
                raise CommandCompileError(f"{kind} validation flag is not allowlisted: {argument}")
            continue
        target = argument.split("::", 1)[0]
        suffixes = {".md", ".py", ".rst", ".txt"} if kind == "doctest" else {".py"}
        if PurePosixPath(target).suffix not in suffixes:
            raise CommandCompileError(
                f"{kind} validation requires an explicit repository-local file target"
            )
        targets.append(target)
    if not targets:
        raise CommandCompileError("validation command requires a repository-local target")
    return tuple(targets)


def compile_validation_command(source: str) -> CompiledValidationCommand:
    """Compile one allowlisted command without invoking a shell.

    Only stdlib doctest and pytest are accepted.  The configured executable is
    intentionally not retained: every accepted form runs with ``sys.executable``
    so an untrusted PATH cannot select a different interpreter.
    """

    if not source.strip():
        raise CommandCompileError("validation command must not be empty")
    if any(character in _SHELL_CONTROL_CHARACTERS for character in source):
        raise CommandCompileError("validation command contains a shell control character")
    try:
        tokens = shlex.split(source, posix=True)
    except ValueError as error:
        raise CommandCompileError(f"validation command could not be parsed: {error}") from error

    kind: CommandKind
    arguments: list[str]
    if len(tokens) >= 3 and tokens[:2] == ["python", "-m"]:
        module = tokens[2]
        if module not in {"doctest", "pytest"}:
            raise CommandCompileError("python validation module is not allowlisted")
        kind = cast(CommandKind, module)
        arguments = tokens[3:]
    elif tokens[:1] == ["pytest"]:
        kind = "pytest"
        arguments = tokens[1:]
    else:
        raise CommandCompileError(
            "validation command must use python -m doctest, python -m pytest, or pytest"
        )

    _validation_targets(kind, arguments)
    return CompiledValidationCommand(
        source=source,
        kind=kind,
        argv=(sys.executable, "-m", kind, *arguments),
    )


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _bounded_summary(prefix: str, detail: str, limit: int) -> str:
    normalized_detail = detail.strip()
    summary = prefix if not normalized_detail else f"{prefix}\n{normalized_detail}"
    if len(summary) <= limit:
        return summary
    if limit <= len(_TRUNCATION_MARKER):
        return _TRUNCATION_MARKER[:limit]
    return f"{summary[: limit - len(_TRUNCATION_MARKER)]}{_TRUNCATION_MARKER}"


def _pytest_argv(command: CompiledValidationCommand) -> tuple[str, ...]:
    if command.kind != "pytest":
        return command.argv
    return (*command.argv[:3], "-p", "no:cacheprovider", *command.argv[3:])


def _execution_argv(
    command: CompiledValidationCommand,
    *,
    bootstrap_path: Path,
    workspace: Path,
) -> tuple[str, ...]:
    logical_argv = _pytest_argv(command)
    return (
        sys.executable,
        "-P",
        str(bootstrap_path),
        command.kind,
        str(workspace),
        *logical_argv[3:],
    )


def _ignored_validation_name(name: str) -> bool:
    return name in _VALIDATION_IGNORED_NAMES or name.startswith(".env")


def _copy_validation_workspace(source: Path, destination: Path) -> None:
    def ignore(directory: str, names: list[str]) -> set[str]:
        excluded = {name for name in names if _ignored_validation_name(name)}
        excluded.update(name for name in names if (Path(directory) / name).is_symlink())
        return excluded

    shutil.copytree(source, destination, symlinks=True, ignore=ignore)


def validation_input_manifest(repo_path: Path) -> dict[str, str]:
    """Hash every regular repository file that the disposable copy can expose."""

    manifest: dict[str, str] = {}
    for directory, names, filenames in os.walk(
        repo_path,
        topdown=True,
        followlinks=False,
    ):
        root = Path(directory)
        names[:] = [
            name
            for name in sorted(names)
            if not _ignored_validation_name(name) and not (root / name).is_symlink()
        ]
        for name in sorted(filenames):
            if _ignored_validation_name(name):
                continue
            path = root / name
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(repo_path).as_posix()
            manifest[relative] = sha256_file(path)
    return dict(sorted(manifest.items()))


def _unsafe_target(repo_path: Path, target: str) -> str | None:
    current = repo_path
    for component in PurePosixPath(target).parts:
        current /= component
        if current.is_symlink():
            return f"validation target contains a symlink: {target}"
    if not current.is_file():
        return f"validation target is unavailable: {target}"
    return None


class ValidationCommandRunner:
    """Run an allowlisted validation command in a disposable local environment."""

    def __init__(
        self,
        *,
        process_runner: ProcessRunner | None = None,
        max_summary_chars: int = 2_000,
    ) -> None:
        if max_summary_chars <= 0:
            raise ValueError("max_summary_chars must be positive")
        self._process_runner = process_runner or cast(ProcessRunner, subprocess.run)
        self._max_summary_chars = max_summary_chars

    @staticmethod
    def compile(source: str) -> CompiledValidationCommand:
        return compile_validation_command(source)

    def _environment(
        self,
        root: Path,
        workspace: Path,
        *,
        network: bool,
    ) -> dict[str, str]:
        home = root / "home"
        cache = root / "cache"
        temporary = root / "tmp"
        pycache = root / "pycache"
        for path in (home, cache, temporary, pycache):
            path.mkdir()

        environment = {
            name: os.environ[name]
            for name in ("SYSTEMROOT", "SystemRoot", "WINDIR")
            if name in os.environ
        }
        environment.update(
            {
                "HOME": str(home),
                "USERPROFILE": str(home),
                "XDG_CACHE_HOME": str(cache),
                "TMPDIR": str(temporary),
                "TMP": str(temporary),
                "TEMP": str(temporary),
                "PYTHONPYCACHEPREFIX": str(pycache),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTEST_ADDOPTS": "",
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                "PYTEST_PLUGINS": "",
                "PYTHONNOUSERSITE": "1",
                "PYTHONSAFEPATH": "1",
                "PYTHONUTF8": "1",
                "PYTHONHASHSEED": "0",
                "PATH": str(Path(sys.executable).resolve().parent),
                "LANG": "C",
                "LC_ALL": "C",
                "TZ": "UTC",
                "PWD": str(workspace),
                "OLDPWD": str(workspace),
            }
        )
        for name in (
            "PYTHONBREAKPOINT",
            "PYTHONHOME",
            "PYTHONINSPECT",
            "PYTHONPATH",
            "PYTHONSTARTUP",
            "PYTHONUSERBASE",
            "VIRTUAL_ENV",
        ):
            environment.pop(name, None)
        for name in _MODEL_PROVIDER_ENVIRONMENT_NAMES:
            environment.pop(name, None)
        if not network:
            guard = root / "network-guard"
            guard.mkdir()
            (guard / "sitecustomize.py").write_text(_NETWORK_GUARD, encoding="utf-8")
            environment["PYTHONPATH"] = str(guard)
        return environment

    def run(
        self,
        repo_path: Path,
        command: str | CompiledValidationCommand,
        *,
        finding_ids: Sequence[str],
        attempt_id: str,
        required: bool = True,
        timeout_seconds: float = 30.0,
        network: bool = False,
        expected_input_hashes: Mapping[str, str] | None = None,
    ) -> ValidationResult:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        if isinstance(command, str):
            compiled = compile_validation_command(command)
        else:
            compiled = compile_validation_command(command.source)
            if compiled != command:
                raise CommandCompileError("compiled validation command failed revalidation")
        started = time.monotonic()
        if find_spec(compiled.kind) is None:
            return ValidationResult(
                finding_ids=list(finding_ids),
                attempt_id=attempt_id,
                check=compiled.kind,
                required=required,
                status=ValidationStatus.UNAVAILABLE,
                summary=f"{compiled.kind} unavailable: Python module is not installed",
            )
        for target in _validation_targets(compiled.kind, compiled.argv[3:]):
            problem = _unsafe_target(repo_path, target)
            if problem is not None:
                return ValidationResult(
                    finding_ids=list(finding_ids),
                    attempt_id=attempt_id,
                    check=compiled.kind,
                    required=required,
                    status=ValidationStatus.UNAVAILABLE,
                    summary=problem,
                )
        with tempfile.TemporaryDirectory(prefix="drift-agent-validation-") as temporary_name:
            temporary_root = Path(temporary_name)
            validation_workspace = temporary_root / "workspace"
            try:
                _copy_validation_workspace(repo_path, validation_workspace)
            except OSError as error:
                return ValidationResult(
                    finding_ids=list(finding_ids),
                    attempt_id=attempt_id,
                    check=compiled.kind,
                    required=required,
                    status=ValidationStatus.UNAVAILABLE,
                    summary=_bounded_summary(
                        f"{compiled.kind} unavailable: workspace isolation failed",
                        f"{type(error).__name__}: {error}",
                        self._max_summary_chars,
                    ),
                )
            if expected_input_hashes is not None:
                copied_manifest = validation_input_manifest(validation_workspace)
                expected_manifest = dict(expected_input_hashes)
                if copied_manifest != expected_manifest:
                    differing_path = next(
                        path
                        for path in sorted(copied_manifest.keys() | expected_manifest.keys())
                        if copied_manifest.get(path) != expected_manifest.get(path)
                    )
                    raise ValidationInputChangedError(differing_path)
            elapsed = time.monotonic() - started
            remaining_seconds = timeout_seconds - elapsed
            if remaining_seconds <= 0:
                return ValidationResult(
                    finding_ids=list(finding_ids),
                    attempt_id=attempt_id,
                    check=compiled.kind,
                    required=required,
                    status=ValidationStatus.UNAVAILABLE,
                    summary=f"{compiled.kind} unavailable: timed out during workspace isolation",
                    duration_ms=int(elapsed * 1_000),
                )
            bootstrap_path = temporary_root / "validation_bootstrap.py"
            bootstrap_path.write_text(_VALIDATION_BOOTSTRAP, encoding="utf-8")
            argv = _execution_argv(
                compiled,
                bootstrap_path=bootstrap_path,
                workspace=validation_workspace,
            )
            environment = self._environment(
                temporary_root,
                validation_workspace,
                network=network,
            )
            try:
                completed = self._process_runner(
                    argv,
                    cwd=validation_workspace,
                    check=False,
                    capture_output=True,
                    text=True,
                    shell=False,
                    timeout=remaining_seconds,
                    env=environment,
                )
            except subprocess.TimeoutExpired as error:
                duration_ms = int((time.monotonic() - started) * 1_000)
                detail = "\n".join(
                    item for item in (_text(error.stdout), _text(error.stderr)) if item
                )
                return ValidationResult(
                    finding_ids=list(finding_ids),
                    attempt_id=attempt_id,
                    check=compiled.kind,
                    required=required,
                    status=ValidationStatus.UNAVAILABLE,
                    summary=_bounded_summary(
                        f"{compiled.kind} unavailable: timed out after {timeout_seconds:g}s",
                        detail,
                        self._max_summary_chars,
                    ),
                    duration_ms=duration_ms,
                )
            except OSError as error:
                duration_ms = int((time.monotonic() - started) * 1_000)
                return ValidationResult(
                    finding_ids=list(finding_ids),
                    attempt_id=attempt_id,
                    check=compiled.kind,
                    required=required,
                    status=ValidationStatus.UNAVAILABLE,
                    summary=_bounded_summary(
                        f"{compiled.kind} unavailable",
                        f"{type(error).__name__}: {error}",
                        self._max_summary_chars,
                    ),
                    duration_ms=duration_ms,
                )

        duration_ms = int((time.monotonic() - started) * 1_000)
        if completed.returncode == 0:
            status = ValidationStatus.PASSED
        elif completed.returncode == 1:
            # Both allowlisted runners reserve exit 1 for confirmed test/example
            # failures. Other codes are infrastructure, usage, or interruption.
            status = ValidationStatus.FAILED
        else:
            status = ValidationStatus.UNAVAILABLE
        detail = "\n".join(item for item in (completed.stdout, completed.stderr) if item)
        return ValidationResult(
            finding_ids=list(finding_ids),
            attempt_id=attempt_id,
            check=compiled.kind,
            required=required,
            status=status,
            summary=_bounded_summary(
                f"{compiled.kind} {status.value} (exit {completed.returncode})",
                detail,
                self._max_summary_chars,
            ),
            duration_ms=duration_ms,
        )


__all__ = [
    "CommandCompileError",
    "CompiledValidationCommand",
    "ProcessRunner",
    "ValidationCommandRunner",
    "ValidationInputChangedError",
    "compile_validation_command",
    "validation_input_manifest",
]

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import shlex
import shutil
import stat
import subprocess
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_WHEEL_NAME = "doc_code_drift_agent-0.1.0-py3-none-any.whl"
_DIST_INFO = "doc_code_drift_agent-0.1.0.dist-info"
_DETERMINISTIC_ZIP_TIME = (1980, 1, 1, 0, 0, 0)


class BenchmarkRuntimeError(RuntimeError):
    """Raised when a benchmark subject namespace cannot be built safely."""


@dataclass(frozen=True, slots=True)
class ExecutableIdentity:
    path: Path
    version: str
    sha256: str


@dataclass(frozen=True, slots=True)
class SlimRuntime:
    wheel: Path
    wheel_sha256: str
    executable: Path
    member_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CodexPermissionConfig:
    path: Path
    profile_name: str
    sha256: str


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def identify_executable(path: Path, *version_args: str) -> ExecutableIdentity:
    executable = path.expanduser().absolute()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise BenchmarkRuntimeError(f"benchmark executable is not runnable: {executable}")
    search_path = os.pathsep.join(
        dict.fromkeys(
            (
                os.fspath(executable.parent),
                "/opt/homebrew/bin",
                "/usr/local/bin",
                "/usr/bin",
                "/bin",
            )
        )
    )
    try:
        completed = subprocess.run(
            [os.fspath(executable), *version_args],
            check=True,
            capture_output=True,
            text=True,
            shell=False,
            timeout=15,
            env={"PATH": search_path, "LANG": "C", "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise BenchmarkRuntimeError(
            f"cannot identify benchmark executable: {executable}"
        ) from error
    version = (completed.stdout or completed.stderr).strip()
    if not version or "\n" in version or "\r" in version:
        raise BenchmarkRuntimeError(f"unexpected executable version output: {executable}")
    return ExecutableIdentity(
        path=executable,
        version=version,
        sha256=sha256_file(executable),
    )


def resolve_codex_executable(explicit: Path | None = None) -> ExecutableIdentity:
    candidate = os.fspath(explicit) if explicit is not None else shutil.which("codex")
    if candidate is None:
        raise BenchmarkRuntimeError("Codex CLI is not on PATH; pass --codex-binary")
    return identify_executable(Path(candidate), "--version")


def _wheel_hash(raw: bytes) -> str:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b"=")
    return f"sha256={encoded.decode('ascii')}"


def _zip_info(name: str, *, executable: bool = False) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, _DETERMINISTIC_ZIP_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    mode = 0o755 if executable else 0o644
    info.external_attr = (stat.S_IFREG | mode) << 16
    info.create_system = 3
    return info


def _slim_members(package_root: Path) -> dict[str, bytes]:
    members: dict[str, bytes] = {}
    for path in sorted(package_root.rglob("*")):
        relative = path.relative_to(package_root)
        if (
            not path.is_file()
            or "__pycache__" in relative.parts
            or relative.parts[0] == "evaluation"
            or path.suffix not in {".py", ".typed"}
        ):
            continue
        name = PurePosixPath("drift_agent", *relative.parts).as_posix()
        members[name] = path.read_bytes()
    if "drift_agent/__init__.py" not in members or "drift_agent/cli.py" not in members:
        raise BenchmarkRuntimeError("slim runtime source is missing package entry points")
    return members


def _metadata_members() -> dict[str, bytes]:
    return {
        f"{_DIST_INFO}/METADATA": (
            b"Metadata-Version: 2.3\n"
            b"Name: doc-code-drift-agent\n"
            b"Version: 0.1.0\n"
            b"Requires-Python: >=3.11\n"
            b"\n"
        ),
        f"{_DIST_INFO}/WHEEL": (
            b"Wheel-Version: 1.0\n"
            b"Generator: drift-agent-benchmark-runtime-v1\n"
            b"Root-Is-Purelib: true\n"
            b"Tag: py3-none-any\n"
            b"\n"
        ),
        f"{_DIST_INFO}/entry_points.txt": (
            b"[console_scripts]\ndrift-agent = drift_agent.cli:app\n"
        ),
    }


def _record_bytes(members: dict[str, bytes]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for name in sorted(members):
        raw = members[name]
        writer.writerow((name, _wheel_hash(raw), len(raw)))
    writer.writerow((f"{_DIST_INFO}/RECORD", "", ""))
    return output.getvalue().encode("utf-8")


def build_slim_runtime(
    *,
    source_root: Path,
    destination: Path,
    python_executable: Path,
) -> SlimRuntime:
    """Build a deterministic, oracle-free wheel and a private CLI launcher."""

    package_root = source_root.resolve() / "src" / "drift_agent"
    if not package_root.is_dir():
        raise BenchmarkRuntimeError(f"drift_agent source package is absent: {package_root}")
    destination.mkdir(parents=True, exist_ok=True)
    wheel = destination / _WHEEL_NAME
    members = {**_slim_members(package_root), **_metadata_members()}
    members[f"{_DIST_INFO}/RECORD"] = _record_bytes(members)
    with zipfile.ZipFile(wheel, "w") as archive:
        for name in sorted(members):
            archive.writestr(_zip_info(name), members[name])

    with zipfile.ZipFile(wheel) as archive:
        names = tuple(sorted(archive.namelist()))
        corrupt = archive.testzip()
    if corrupt is not None:
        raise BenchmarkRuntimeError(f"slim wheel integrity failure: {corrupt}")
    if any(
        name.startswith("drift_agent/evaluation/")
        or "/datasets/" in name
        or name.startswith("tests/")
        for name in names
    ):
        raise BenchmarkRuntimeError("slim wheel contains evaluation or test material")

    python = python_executable.expanduser().absolute()
    if not python.is_file() or not os.access(python, os.X_OK):
        raise BenchmarkRuntimeError(f"Python runtime is not runnable: {python}")
    launcher = destination / "drift-agent"
    launcher.write_text(
        "#!/bin/sh\n"
        f"export PYTHONPATH={shlex.quote(os.fspath(wheel))}\n"
        "export DRIFT_AGENT_SUBJECT_RUNTIME=1\n"
        f"exec {shlex.quote(os.fspath(python))} -c "
        f'{shlex.quote("from drift_agent.cli import app; app()")} "$@"\n',
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    return SlimRuntime(
        wheel=wheel,
        wheel_sha256=sha256_file(wheel),
        executable=launcher,
        member_names=names,
    )


def copy_isolated_codex_auth(*, source_home: Path, destination_home: Path) -> Path:
    """Copy only Codex's auth channel, never personal config, rules, or sessions."""

    source = source_home.expanduser().resolve() / "auth.json"
    if not source.is_file():
        raise BenchmarkRuntimeError(
            "Codex auth.json is unavailable; log in or provide a benchmark API key"
        )
    destination_home.mkdir(parents=True, exist_ok=False, mode=0o700)
    destination = destination_home / "auth.json"
    shutil.copyfile(source, destination)
    destination.chmod(0o600)
    return destination


def _toml_key(value: str) -> str:
    """Render a path/profile key as a TOML basic string."""

    return json.dumps(value, ensure_ascii=False)


def interpreter_runtime_roots(
    neutral: Path,
    neutral_python: Path,
) -> tuple[Path, ...]:
    """Return read-only roots needed to start the copied virtualenv Python.

    On Homebrew macOS, ``venv --copies`` copies the executable but the Mach-O
    binary still loads ``Python.framework`` from the pinned Cellar directory.
    ``pyvenv.cfg`` records that base interpreter location without executing any
    code from the environment.
    """

    roots = [neutral_python.resolve().parent.parent]
    configuration = neutral / "pyvenv.cfg"
    if configuration.is_file() and not configuration.is_symlink():
        for line in configuration.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == "home":
                base_bin = Path(value.strip()).expanduser()
                if not base_bin.is_absolute():
                    raise BenchmarkRuntimeError("neutral Python home must be absolute")
                base_root = base_bin.resolve().parent
                if not base_root.is_dir():
                    raise BenchmarkRuntimeError("neutral Python runtime root is unavailable")
                roots.append(base_root)
                break
    return tuple(dict.fromkeys(roots))


def build_codex_permission_config(
    *,
    codex_home: Path,
    neutral_toolchain_root: Path,
    ephemeral_root: Path,
    denied_roots: Iterable[Path],
    toolchain_read_roots: Iterable[Path] = (),
    profile_name: str = "benchmark",
) -> CodexPermissionConfig:
    """Create the only user config visible to the isolated Codex parent.

    Permission profiles apply to commands spawned by Codex, not to the Codex
    parent itself.  This lets the parent read its isolated auth channel while
    model-proposed commands remain unable to read that channel or any trusted
    benchmark namespace.
    """

    home = codex_home.expanduser().resolve()
    neutral = neutral_toolchain_root.expanduser().resolve()
    ephemeral = ephemeral_root.expanduser().resolve()
    if not home.is_dir() or home.is_symlink():
        raise BenchmarkRuntimeError("isolated Codex home must be a real directory")
    if not (home / "auth.json").is_file():
        raise BenchmarkRuntimeError("isolated Codex auth must exist before permissions")
    if not neutral.is_dir() or neutral.is_symlink():
        raise BenchmarkRuntimeError("neutral toolchain root must be a real directory")
    neutral_python = neutral / "bin" / "python"
    if not neutral_python.is_file():
        raise BenchmarkRuntimeError("neutral toolchain is missing Python")
    interpreter_roots = interpreter_runtime_roots(neutral, neutral_python)
    ephemeral.mkdir(parents=True, exist_ok=True, mode=0o700)
    if ephemeral.is_symlink():
        raise BenchmarkRuntimeError("Codex ephemeral root may not be a symlink")
    if not profile_name or not profile_name.replace("-", "_").isalnum():
        raise BenchmarkRuntimeError("invalid Codex permission profile name")

    denied = tuple(
        dict.fromkeys(path.expanduser().resolve() for path in (*tuple(denied_roots), home))
    )
    rules: dict[str, str] = {
        ":root": "deny",
        ":minimal": "read",
    }
    for root in denied:
        rules[os.fspath(root)] = "deny"
    rules[os.fspath(neutral)] = "read"
    for interpreter_root in interpreter_roots:
        rules[os.fspath(interpreter_root)] = "read"
    for root in toolchain_read_roots:
        read_only = root.expanduser().resolve()
        if not read_only.is_dir() or read_only.is_symlink():
            raise BenchmarkRuntimeError("toolchain read root must be a real directory")
        rules[os.fspath(read_only)] = "read"
    rules[os.fspath(ephemeral)] = "write"

    lines = [
        f"default_permissions = {_toml_key(profile_name)}",
        "",
        f"[permissions.{profile_name}.filesystem]",
    ]
    lines.extend(f"{_toml_key(path)} = {_toml_key(access)}" for path, access in rules.items())
    lines.extend(
        (
            "",
            f'[permissions.{profile_name}.filesystem.":workspace_roots"]',
            '"." = "write"',
            '".git" = "read"',
            "",
            f"[permissions.{profile_name}.network]",
            "enabled = false",
            "",
        )
    )
    path = home / "config.toml"
    path.write_text("\n".join(lines), encoding="utf-8")
    path.chmod(0o600)
    return CodexPermissionConfig(
        path=path,
        profile_name=profile_name,
        sha256=sha256_file(path),
    )


def codex_auth_sensitive_values(auth_path: Path) -> tuple[bytes, ...]:
    """Extract credential-shaped JSON leaves for stream leakage detection."""

    try:
        document = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise BenchmarkRuntimeError("isolated Codex auth is not valid JSON") from error
    protected: set[bytes] = set()

    def visit(value: object, key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                if isinstance(child_key, str):
                    visit(child, child_key.lower())
        elif isinstance(value, list):
            for child in value:
                visit(child, key)
        elif (
            isinstance(value, str)
            and len(value) >= 8
            and any(marker in key for marker in ("token", "secret", "api_key", "apikey"))
        ):
            protected.add(value.encode("utf-8"))

    visit(document)
    if not protected:
        raise BenchmarkRuntimeError("Codex auth JSON contains no protected credential values")
    return tuple(sorted(protected, key=lambda item: (-len(item), item)))


def _sandbox_literal(path: Path) -> str:
    value = os.fspath(path.expanduser().resolve())
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise BenchmarkRuntimeError("sandbox paths may not contain control characters")
    return value.replace("\\", "\\\\").replace('"', '\\"')


def build_sandboxed_codex_launcher(
    *,
    destination: Path,
    codex_executable: Path,
    denied_roots: Iterable[Path],
) -> Path:
    """Wrap Codex in a macOS seatbelt that hides supervisor and oracle roots."""

    sandbox = Path("/usr/bin/sandbox-exec")
    if not sandbox.is_file():
        raise BenchmarkRuntimeError("sandbox-exec is required for the live local benchmark")
    destination.mkdir(parents=True, exist_ok=True)
    roots = tuple(dict.fromkeys(path.expanduser().resolve() for path in denied_roots))
    if not roots:
        raise BenchmarkRuntimeError("Codex sandbox requires at least one denied root")
    clauses = ["(version 1)", "(allow default)"]
    for root in roots:
        literal = _sandbox_literal(root)
        clauses.append(f'(deny file-read* (subpath "{literal}"))')
        clauses.append(f'(deny file-write* (subpath "{literal}"))')
    profile = destination / "codex-seatbelt.sb"
    profile.write_text("\n".join(clauses) + "\n", encoding="utf-8")
    profile.chmod(0o600)

    codex = codex_executable.expanduser().resolve(strict=True)
    launcher = destination / "codex-benchmark"
    launcher.write_text(
        "#!/bin/sh\n"
        f"exec {shlex.quote(os.fspath(sandbox))} -f "
        f"{shlex.quote(os.fspath(profile))} "
        f'{shlex.quote(os.fspath(codex))} "$@"\n',
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    return launcher


__all__ = [
    "BenchmarkRuntimeError",
    "CodexPermissionConfig",
    "ExecutableIdentity",
    "SlimRuntime",
    "build_codex_permission_config",
    "build_sandboxed_codex_launcher",
    "build_slim_runtime",
    "canonical_json_bytes",
    "codex_auth_sensitive_values",
    "copy_isolated_codex_auth",
    "identify_executable",
    "interpreter_runtime_roots",
    "resolve_codex_executable",
    "sha256_file",
]

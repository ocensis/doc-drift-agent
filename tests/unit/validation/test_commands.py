from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from drift_agent.domain.enums import ValidationStatus
from drift_agent.validation import commands as commands_module
from drift_agent.validation.commands import (
    CommandCompileError,
    CompiledValidationCommand,
    ValidationCommandRunner,
    ValidationInputChangedError,
    compile_validation_command,
    validation_input_manifest,
)


@pytest.mark.parametrize(
    ("source", "kind", "arguments"),
    [
        ("python -m doctest docs/api.md", "doctest", ("docs/api.md",)),
        (
            "python -m pytest tests/test_api.py::test_echo -q",
            "pytest",
            ("tests/test_api.py::test_echo", "-q"),
        ),
        (
            "pytest 'tests/test api.py::test_echo'",
            "pytest",
            ("tests/test api.py::test_echo",),
        ),
    ],
)
def test_compile_normalizes_allowlisted_commands_to_current_python(
    source: str,
    kind: str,
    arguments: tuple[str, ...],
) -> None:
    compiled = compile_validation_command(source)

    assert compiled == CompiledValidationCommand(
        source=source,
        kind=kind,  # type: ignore[arg-type]
        argv=(sys.executable, "-m", kind, *arguments),
    )
    assert compiled.targets == tuple(
        argument.split("::", 1)[0] for argument in arguments if not argument.startswith("-")
    )


@pytest.mark.parametrize(
    "source",
    [
        "",
        "python",
        "python -c 'print(1)'",
        "python -m pip list",
        "python3 -m pytest tests/test_api.py",
        "/usr/bin/python -m pytest tests/test_api.py",
        "bash -lc pytest",
        "pytest",
        "python -m doctest",
        "pytest; echo unsafe",
        "pytest tests/test_api.py | cat",
        "pytest $HOME/test_api.py",
        "pytest `whoami`",
        "pytest /tmp/test_api.py",
        "pytest D:tests/test_api.py",
        r"pytest '\tests\test_api.py'",
        "pytest ../test_api.py",
        "pytest tests/../../test_api.py",
        "pytest --rootdir=/tmp tests/test_api.py",
        "pytest -p unsafe tests/test_api.py",
        "pytest --pyargs package",
        "pytest tests",
        "pytest @config/pytest-args.txt",
        "pytest 'unterminated",
    ],
)
def test_compile_rejects_non_allowlisted_or_unsafe_commands(source: str) -> None:
    with pytest.raises(CommandCompileError):
        compile_validation_command(source)


class _RecordingProcess:
    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        error: BaseException | None = None,
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.error = error
        self.argv: tuple[str, ...] = ()
        self.kwargs: dict[str, Any] = {}
        self.environment_paths: dict[str, Path] = {}
        self.sitecustomize = ""
        self.workspace_name = ""
        self.workspace_target_exists = False

    def __call__(
        self,
        argv: Sequence[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        self.argv = tuple(argv)
        self.kwargs = kwargs
        workspace = Path(kwargs["cwd"])
        self.workspace_name = workspace.name
        self.workspace_target_exists = (workspace / "tests/test_api.py").is_file()
        environment = kwargs["env"]
        assert isinstance(environment, Mapping)
        for name in (
            "HOME",
            "XDG_CACHE_HOME",
            "TMPDIR",
            "PYTHONPYCACHEPREFIX",
        ):
            value = environment[name]
            assert isinstance(value, str)
            path = Path(value)
            assert path.is_dir()
            self.environment_paths[name] = path
        python_path = environment["PYTHONPATH"]
        assert isinstance(python_path, str)
        guard_path = Path(python_path.split(os.pathsep)[0])
        sitecustomize = guard_path / "sitecustomize.py"
        assert sitecustomize.is_file()
        self.sitecustomize = sitecustomize.read_text(encoding="utf-8")
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(
            list(argv),
            self.returncode,
            stdout=self.stdout,
            stderr=self.stderr,
        )


def test_runner_uses_shell_false_isolated_environment_and_bounded_summary(
    tmp_path: Path,
) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_api.py").write_text(
        "def test_echo() -> None:\n    assert True\n",
        encoding="utf-8",
    )
    process = _RecordingProcess(stdout="x" * 500)
    runner = ValidationCommandRunner(process_runner=process, max_summary_chars=160)

    result = runner.run(
        tmp_path,
        "pytest tests/test_api.py::test_echo",
        finding_ids=["finding_1"],
        attempt_id="attempt_1",
        timeout_seconds=3.5,
        network=False,
    )

    assert result.status is ValidationStatus.PASSED
    assert result.check == "pytest"
    assert result.finding_ids == ["finding_1"]
    assert result.attempt_id == "attempt_1"
    assert len(result.summary) <= 160
    assert "truncated" in result.summary
    assert process.argv[:2] == (sys.executable, "-P")
    assert Path(process.argv[2]).name == "validation_bootstrap.py"
    assert process.argv[3] == "pytest"
    assert Path(process.argv[4]).name == "workspace"
    assert process.argv[5:] == (
        "-p",
        "no:cacheprovider",
        "tests/test_api.py::test_echo",
    )
    assert process.kwargs["cwd"] != tmp_path
    assert process.workspace_name == "workspace"
    assert process.workspace_target_exists is True
    assert process.kwargs["shell"] is False
    assert process.kwargs["check"] is False
    assert process.kwargs["capture_output"] is True
    assert process.kwargs["text"] is True
    assert 0 < process.kwargs["timeout"] <= 3.5
    environment = process.kwargs["env"]
    assert environment["PYTHONDONTWRITEBYTECODE"] == "1"
    assert environment["PYTEST_ADDOPTS"] == ""
    assert environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert environment["PYTHONSAFEPATH"] == "1"
    assert Path(environment["PWD"]).name == "workspace"
    assert environment["OLDPWD"] == environment["PWD"]
    assert "network disabled by drift-agent" in process.sitecustomize
    assert all(not path.exists() for path in process.environment_paths.values())


@pytest.mark.parametrize("network", [False, True])
def test_validation_environment_never_inherits_model_provider_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    network: bool,
) -> None:
    provider_names = (
        "OPENROUTER_API_KEY",
        "OPENROUTER_MODEL",
        "OPENROUTER_PROVIDER",
        "OPENROUTER_DATA_COLLECTION",
        "OPENROUTER_BASE_URL",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GH_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "DATABASE_URL",
        "HTTPS_PROXY",
    )
    for name in provider_names:
        monkeypatch.setenv(name, f"sensitive-{name}")
    root = tmp_path / ("network" if network else "offline")
    root.mkdir()
    workspace = root / "workspace"
    workspace.mkdir()

    environment = ValidationCommandRunner()._environment(
        root,
        workspace,
        network=network,
    )

    assert all(name not in environment for name in provider_names)
    assert environment["PATH"] == str(Path(sys.executable).resolve().parent)


def test_validation_copy_and_manifest_exclude_all_dotenv_prefixes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    for name in (".env", ".env.local", ".envrc", ".env-dev", ".env_secret"):
        (repo / name).write_text("OPENROUTER_API_KEY=secret\n", encoding="utf-8")
    dotenv_directory = repo / ".env-secrets"
    dotenv_directory.mkdir()
    (dotenv_directory / "provider-key").write_text("secret\n", encoding="utf-8")
    safe_directory = repo / "config"
    safe_directory.mkdir()
    (safe_directory / ".envrc").write_text("ANOTHER_SECRET=value\n", encoding="utf-8")
    (safe_directory / "settings.toml").write_text("safe = true\n", encoding="utf-8")
    (repo / "safe.py").write_text("VALUE = 1\n", encoding="utf-8")

    manifest = validation_input_manifest(repo)
    copied = tmp_path / "copied"
    commands_module._copy_validation_workspace(repo, copied)

    assert set(manifest) == {"config/settings.toml", "safe.py"}
    assert {
        path.relative_to(copied).as_posix() for path in copied.rglob("*") if path.is_file()
    } == {"config/settings.toml", "safe.py"}
    assert all(
        not part.startswith(".env")
        for path in copied.rglob("*")
        for part in path.relative_to(copied).parts
    )


@pytest.mark.parametrize(
    ("command", "returncode", "expected"),
    [
        ("python -m doctest docs/api.md", 0, ValidationStatus.PASSED),
        ("python -m doctest docs/api.md", 1, ValidationStatus.FAILED),
        ("python -m doctest docs/api.md", 2, ValidationStatus.UNAVAILABLE),
        ("python -m doctest docs/api.md", -9, ValidationStatus.UNAVAILABLE),
        ("python -m pytest docs/api.py", 1, ValidationStatus.FAILED),
        ("python -m pytest docs/api.py", 2, ValidationStatus.UNAVAILABLE),
        ("python -m pytest docs/api.py", 3, ValidationStatus.UNAVAILABLE),
        ("python -m pytest docs/api.py", 4, ValidationStatus.UNAVAILABLE),
        ("python -m pytest docs/api.py", 5, ValidationStatus.UNAVAILABLE),
    ],
)
def test_runner_maps_process_exit_status(
    tmp_path: Path,
    command: str,
    returncode: int,
    expected: ValidationStatus,
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "api.md").write_text("Example documentation.\n", encoding="utf-8")
    (docs / "api.py").write_text("def test_example() -> None: ...\n", encoding="utf-8")
    runner = ValidationCommandRunner(
        process_runner=_RecordingProcess(returncode=returncode, stderr="details")
    )

    result = runner.run(
        tmp_path,
        command,
        finding_ids=[],
        attempt_id="validation_1",
    )

    assert result.status is expected
    assert "details" in result.summary


def test_runner_revalidates_injected_compiled_commands(tmp_path: Path) -> None:
    forged = CompiledValidationCommand(
        source="pytest tests/test_api.py",
        kind="pytest",
        argv=("/bin/sh", "-c", "unsafe"),
    )

    with pytest.raises(CommandCompileError):
        ValidationCommandRunner(process_runner=_RecordingProcess()).run(
            tmp_path,
            forged,
            finding_ids=[],
            attempt_id="attempt_forged",
        )


@pytest.mark.parametrize(
    "error",
    [
        subprocess.TimeoutExpired(["python"], 0.01, output="partial"),
        OSError("executable unavailable"),
    ],
)
def test_runner_maps_timeout_and_os_errors_to_unavailable(
    tmp_path: Path,
    error: BaseException,
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "api.md").write_text("Example documentation.\n", encoding="utf-8")
    runner = ValidationCommandRunner(process_runner=_RecordingProcess(error=error))

    result = runner.run(
        tmp_path,
        "python -m doctest docs/api.md",
        finding_ids=["finding_1"],
        attempt_id="attempt_1",
        timeout_seconds=0.01,
    )

    assert result.status is ValidationStatus.UNAVAILABLE
    assert result.required is True


def test_runner_reports_missing_python_module_as_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_api.py").write_text(
        "def test_echo() -> None:\n    assert True\n",
        encoding="utf-8",
    )
    process = _RecordingProcess()
    monkeypatch.setattr(commands_module, "find_spec", lambda _module: None)

    result = ValidationCommandRunner(process_runner=process).run(
        tmp_path,
        "pytest tests/test_api.py",
        finding_ids=[],
        attempt_id="attempt_missing_module",
    )

    assert result.status is ValidationStatus.UNAVAILABLE
    assert "not installed" in result.summary
    assert process.argv == ()


def test_runner_rejects_a_symlinked_validation_target(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.py"
    outside.write_text("def test_external() -> None:\n    assert True\n", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_api.py").symlink_to(outside)
    process = _RecordingProcess()

    result = ValidationCommandRunner(process_runner=process).run(
        tmp_path,
        "pytest tests/test_api.py",
        finding_ids=[],
        attempt_id="attempt_symlink",
    )

    assert result.status is ValidationStatus.UNAVAILABLE
    assert "symlink" in result.summary
    assert process.argv == ()


def test_validator_side_effects_stay_in_disposable_workspace(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "write.txt").write_text(
        """\
>>> from pathlib import Path
>>> Path("pollution.txt").write_text("x")
1
""",
        encoding="utf-8",
    )

    result = ValidationCommandRunner().run(
        tmp_path,
        "python -m doctest docs/write.txt",
        finding_ids=[],
        attempt_id="attempt_isolation",
    )

    assert result.status is ValidationStatus.PASSED, result.summary
    assert not (tmp_path / "pollution.txt").exists()


def test_inherited_working_directory_cannot_point_back_to_source_repo(
    tmp_path: Path,
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "pwd.txt").write_text(
        """\
>>> import os
>>> from pathlib import Path
>>> Path(os.environ["PWD"]).name
'workspace'
>>> Path(os.environ["PWD"], "pwd-pollution.txt").write_text("x")
1
""",
        encoding="utf-8",
    )

    result = ValidationCommandRunner().run(
        tmp_path,
        "python -m doctest docs/pwd.txt",
        finding_ids=[],
        attempt_id="attempt_pwd_isolation",
    )

    assert result.status is ValidationStatus.PASSED, result.summary
    assert not (tmp_path / "pwd-pollution.txt").exists()


def test_repository_modules_cannot_shadow_the_validation_runtime(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_api.py").write_text(
        "def test_echo() -> None:\n    assert True\n",
        encoding="utf-8",
    )
    (tmp_path / "pytest.py").write_text(
        'raise RuntimeError("repository pytest.py was imported")\n',
        encoding="utf-8",
    )
    (tmp_path / "sitecustomize.py").write_text(
        'raise RuntimeError("repository sitecustomize.py was imported")\n',
        encoding="utf-8",
    )

    result = ValidationCommandRunner().run(
        tmp_path,
        "pytest tests/test_api.py -q",
        finding_ids=[],
        attempt_id="attempt_shadow",
        network=False,
    )

    assert result.status is ValidationStatus.PASSED, result.summary


def test_network_false_sitecustomize_blocks_socket_connect(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "network.txt").write_text(
        """\
>>> import socket
>>> try:
...     socket.create_connection(("127.0.0.1", 9), timeout=0.01)
... except OSError as error:
...     print(error)
network disabled by drift-agent
""",
        encoding="utf-8",
    )

    result = ValidationCommandRunner().run(
        tmp_path,
        "python -m doctest docs/network.txt",
        finding_ids=["finding_network"],
        attempt_id="attempt_network",
        network=False,
    )

    assert result.status is ValidationStatus.PASSED, result.summary
    assert not list(tmp_path.rglob("__pycache__"))


def test_expected_input_manifest_is_checked_in_disposable_copy(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "example.md").write_text(">>> 1 + 1\n2\n", encoding="utf-8")
    dependency = tmp_path / "helper.py"
    dependency.write_text("VALUE = 2\n", encoding="utf-8")

    class MustNotRun:
        def __call__(self, *_: object, **__: object) -> subprocess.CompletedProcess[str]:
            raise AssertionError("a mismatched validation copy must not execute")

    runner = ValidationCommandRunner(process_runner=MustNotRun())
    manifest = validation_input_manifest(tmp_path)
    dependency.write_text("VALUE = 3\n", encoding="utf-8")

    with pytest.raises(ValidationInputChangedError, match=r"helper\.py"):
        runner.run(
            tmp_path,
            "python -m doctest docs/example.md",
            finding_ids=[],
            attempt_id="check-example",
            expected_input_hashes=manifest,
        )

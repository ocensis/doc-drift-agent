from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from drift_agent.evaluation.benchmark_runtime import (
    BenchmarkRuntimeError,
    build_codex_permission_config,
    build_sandboxed_codex_launcher,
    build_slim_runtime,
    canonical_json_bytes,
    codex_auth_sensitive_values,
    copy_isolated_codex_auth,
)

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def test_canonical_json_is_stable_and_rejects_nan() -> None:
    assert canonical_json_bytes({"z": 1, "a": "值"}) == b'{"a":"\xe5\x80\xbc","z":1}'
    with pytest.raises(ValueError, match="Out of range"):
        canonical_json_bytes({"value": float("nan")})


def test_slim_runtime_excludes_all_evaluation_material(tmp_path: Path) -> None:
    runtime = build_slim_runtime(
        source_root=_REPOSITORY_ROOT,
        destination=tmp_path / "runtime",
        python_executable=Path(sys.executable),
    )

    with zipfile.ZipFile(runtime.wheel) as archive:
        assert archive.testzip() is None
        assert not any(name.startswith("drift_agent/evaluation/") for name in archive.namelist())
        assert not any("dataset" in name for name in archive.namelist())

    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import importlib.util, drift_agent; "
                "assert importlib.util.find_spec('drift_agent.evaluation') is None"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        shell=False,
        cwd=tmp_path,
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": os.fspath(runtime.wheel),
            "PYTHONNOUSERSITE": "1",
        },
    )
    assert probe.returncode == 0, probe.stderr

    help_result = subprocess.run(
        [os.fspath(runtime.executable), "--help"],
        check=False,
        capture_output=True,
        text=True,
        shell=False,
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert help_result.returncode == 0
    assert "check" in help_result.stdout
    assert "benchmark" not in help_result.stdout


def test_isolated_codex_home_copies_only_auth(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "auth.json").write_text(
        '{"mode":"chatgpt","tokens":{"access_token":"secret-value"}}',
        encoding="utf-8",
    )
    (source / "config.toml").write_text("model = 'forbidden'", encoding="utf-8")
    destination = tmp_path / "isolated"

    copied = copy_isolated_codex_auth(source_home=source, destination_home=destination)

    assert json.loads(copied.read_text(encoding="utf-8")) == {
        "mode": "chatgpt",
        "tokens": {"access_token": "secret-value"},
    }
    assert sorted(path.name for path in destination.iterdir()) == ["auth.json"]
    assert copied.stat().st_mode & 0o777 == 0o600
    assert codex_auth_sensitive_values(copied) == (b"secret-value",)


def test_missing_codex_auth_fails_closed(tmp_path: Path) -> None:
    source = tmp_path / "empty"
    source.mkdir()
    with pytest.raises(BenchmarkRuntimeError, match=r"auth\.json"):
        copy_isolated_codex_auth(
            source_home=source,
            destination_home=tmp_path / "destination",
        )


def test_codex_permission_config_is_default_deny_and_hides_auth(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text("{}", encoding="utf-8")
    neutral = tmp_path / "neutral"
    (neutral / "bin").mkdir(parents=True)
    (neutral / "bin" / "python").symlink_to(sys.executable)
    base_runtime = tmp_path / "python-runtime"
    (base_runtime / "bin").mkdir(parents=True)
    (neutral / "pyvenv.cfg").write_text(
        f"home = {base_runtime / 'bin'}\n",
        encoding="utf-8",
    )
    ephemeral = tmp_path / "ephemeral"
    trusted = tmp_path / "trusted"
    git_runtime = tmp_path / "git-runtime"
    git_runtime.mkdir()

    config = build_codex_permission_config(
        codex_home=codex_home,
        neutral_toolchain_root=neutral,
        ephemeral_root=ephemeral,
        denied_roots=(trusted,),
        toolchain_read_roots=(git_runtime,),
    )

    raw = config.path.read_text(encoding="utf-8")
    assert 'default_permissions = "benchmark"' in raw
    assert '":root" = "deny"' in raw
    assert f'"{codex_home.resolve()}" = "deny"' in raw
    assert f'"{trusted.resolve()}" = "deny"' in raw
    assert f'"{neutral.resolve()}" = "read"' in raw
    assert f'"{base_runtime.resolve()}" = "read"' in raw
    assert f'"{git_runtime.resolve()}" = "read"' in raw
    assert f'"{ephemeral.resolve()}" = "write"' in raw
    assert '":slash_tmp"' not in raw
    assert '":tmpdir"' not in raw
    assert "[permissions.benchmark.network]\nenabled = false" in raw
    assert config.path.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(not Path("/usr/bin/sandbox-exec").is_file(), reason="requires macOS")
def test_codex_launcher_denies_each_trusted_root(tmp_path: Path) -> None:
    fake_codex = tmp_path / "codex"
    fake_codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_codex.chmod(0o755)
    first = tmp_path / "trusted one"
    second = tmp_path / "oracle"
    launcher = build_sandboxed_codex_launcher(
        destination=tmp_path / "runtime",
        codex_executable=fake_codex,
        denied_roots=(first, second),
    )

    profile = (launcher.parent / "codex-seatbelt.sb").read_text(encoding="utf-8")
    assert profile.count("deny file-read") == 2
    assert profile.count("deny file-write") == 2
    assert os.fspath(first.resolve()) in profile
    assert os.fspath(second.resolve()) in profile

from __future__ import annotations

import ast
import hashlib
import os
import re
import secrets
import shutil
import stat
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, TypeAlias, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from drift_agent.evaluation.benchmark_models import (
    BenchmarkTaskV1,
    NeutralFindingKeyV1,
    NeutralOracleProjectionV1,
    NeutralValueV1,
    canonical_json_bytes,
    canonical_sha256,
    neutral_finding_encoding_sha256,
    sha256_prefixed,
)
from drift_agent.evaluation.catalog import (
    FROZEN_CASE_IDS,
    FROZEN_MANIFEST_SHA256,
    default_dataset_root,
    load_catalog,
)
from drift_agent.evaluation.models import CaseManifest, MatchingKey
from drift_agent.evaluation.stage3_catalog import (
    FROZEN_STAGE3_CASE_IDS,
    FROZEN_STAGE3_MANIFEST_SHA256,
    default_stage3_dataset_root,
    load_stage3_catalog,
)
from drift_agent.evaluation.stage3_models import Stage3CaseManifest
from drift_agent.evaluation.stage4_models import ComparisonChangedBytes

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
GitObjectId = Annotated[str, Field(pattern=r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")]

BenchmarkDatasetId = Literal["structural-v1", "stage3-v1"]
BenchmarkLayer = Literal["structural", "executable", "semantic"]
BenchmarkCaseClass = Literal["portable", "control"]
BenchmarkManifest: TypeAlias = CaseManifest | Stage3CaseManifest

STRUCTURAL_PORTABLE_CASE_IDS = FROZEN_CASE_IDS
EXECUTABLE_PORTABLE_CASE_IDS = FROZEN_STAGE3_CASE_IDS[:4]
PORTABLE_CASE_IDS = STRUCTURAL_PORTABLE_CASE_IDS + EXECUTABLE_PORTABLE_CASE_IDS
CONTROL_CASE_IDS = FROZEN_STAGE3_CASE_IDS[4:]

_OPAQUE_ID = re.compile(r"^[0-9a-f]{32}$")
_FIXED_GIT_DATE = "2000-01-01T00:00:00+00:00"
_GIT_OVERRIDES = (
    "-c",
    "commit.gpgSign=false",
    "-c",
    "tag.gpgSign=false",
    "-c",
    f"core.hooksPath={os.devnull}",
    "-c",
    "core.autocrlf=false",
    "-c",
    "core.eol=lf",
)
_GIT_EXECUTABLE = "/usr/bin/git"
_GIT_LOCAL_CONFIG = (
    ("user.name", "benchmark-v1"),
    ("user.email", "benchmark-v1@example.invalid"),
    ("commit.gpgSign", "false"),
    ("tag.gpgSign", "false"),
    ("core.hooksPath", os.devnull),
    ("core.autocrlf", "false"),
    ("core.eol", "lf"),
)


class BenchmarkCaseError(ValueError):
    """Raised when a frozen case cannot enter the portable benchmark boundary."""


class RepositorySnapshotError(RuntimeError):
    """Raised when repository state cannot be represented canonically."""


class _CaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _canonical_path(value: str, *, label: str = "repository path") -> str:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or value != path.as_posix()
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{label} must be a canonical repo-relative POSIX path")
    return value


class RepositoryEntryStateV1(_CaseModel):
    kind: Literal["file", "symlink"]
    mode: str = Field(pattern=r"^[0-7]{4}$")
    size_bytes: int = Field(ge=0)
    sha256: Sha256


class CanonicalWorktreeEntryV1(RepositoryEntryStateV1):
    path: str = Field(min_length=1, max_length=500)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _canonical_path(value)

    @property
    def state(self) -> RepositoryEntryStateV1:
        return RepositoryEntryStateV1(
            kind=self.kind,
            mode=self.mode,
            size_bytes=self.size_bytes,
            sha256=self.sha256,
        )


class CanonicalIndexEntryV1(_CaseModel):
    path: str = Field(min_length=1, max_length=500)
    stage: int = Field(ge=0, le=3)
    mode: str = Field(pattern=r"^[0-7]{6}$")
    blob_oid: GitObjectId
    intent_to_add: bool
    skip_worktree: bool
    assume_unchanged: bool

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _canonical_path(value, label="index path")


class CanonicalGitRefV1(_CaseModel):
    name: str = Field(min_length=1, max_length=500)
    object_type: str = Field(min_length=1, max_length=50)
    object_id: GitObjectId

    @field_validator("name", "object_type")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if value != value.strip() or any(
            ord(character) < 32 or ord(character) == 127 for character in value
        ):
            raise ValueError("Git ref fields must be bounded printable text")
        return value


class CanonicalGitConfigEntryV1(_CaseModel):
    key: str = Field(min_length=1, max_length=500)
    value: str = Field(max_length=4_096)

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        if value != value.strip() or any(
            ord(character) < 32 or ord(character) == 127 for character in value
        ):
            raise ValueError("Git config keys must be bounded printable text")
        return value

    @field_validator("value")
    @classmethod
    def validate_value(cls, value: str) -> str:
        if "\0" in value:
            raise ValueError("Git config values may not contain NUL")
        return value


class CanonicalGitInternalEntryV1(RepositoryEntryStateV1):
    path: str = Field(min_length=1, max_length=500)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _canonical_path(value, label="Git-internal path")


class CanonicalGitMetadataV1(_CaseModel):
    """Semantic Git safety state kept outside the exact paired snapshot digest."""

    schema_version: Literal[1] = 1
    head_oid: GitObjectId
    head_ref: str | None = Field(default=None, min_length=1, max_length=500)
    refs: tuple[CanonicalGitRefV1, ...]
    config: tuple[CanonicalGitConfigEntryV1, ...]
    hooks: tuple[CanonicalGitInternalEntryV1, ...]

    @field_validator("head_ref")
    @classmethod
    def validate_head_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value != value.strip() or any(
            ord(character) < 32 or ord(character) == 127 for character in value
        ):
            raise ValueError("HEAD symbolic ref must be bounded printable text")
        return value

    @model_validator(mode="after")
    def validate_canonical_order(self) -> CanonicalGitMetadataV1:
        ref_keys = tuple((entry.name, entry.object_type, entry.object_id) for entry in self.refs)
        if ref_keys != tuple(sorted(ref_keys)) or len(set(ref_keys)) != len(ref_keys):
            raise ValueError("Git refs must be unique and sorted")
        config_keys = tuple((entry.key, entry.value) for entry in self.config)
        if config_keys != tuple(sorted(config_keys)):
            raise ValueError("Git config entries must be sorted")
        hook_paths = tuple(entry.path for entry in self.hooks)
        if hook_paths != tuple(sorted(hook_paths)) or len(set(hook_paths)) != len(hook_paths):
            raise ValueError("Git hook entries must be unique and path-sorted")
        return self


class CanonicalRepositorySnapshotV1(_CaseModel):
    schema_version: Literal[1] = 1
    worktree_entries: tuple[CanonicalWorktreeEntryV1, ...]
    head_tree: GitObjectId
    index_entries: tuple[CanonicalIndexEntryV1, ...]
    status_records: tuple[str, ...]

    @model_validator(mode="after")
    def validate_canonical_order(self) -> CanonicalRepositorySnapshotV1:
        worktree_keys = tuple(entry.path for entry in self.worktree_entries)
        if worktree_keys != tuple(sorted(worktree_keys)) or len(set(worktree_keys)) != len(
            worktree_keys
        ):
            raise ValueError("worktree entries must be unique and path-sorted")
        index_keys = tuple((entry.path, entry.stage) for entry in self.index_entries)
        if index_keys != tuple(sorted(index_keys)) or len(set(index_keys)) != len(index_keys):
            raise ValueError("index entries must be unique and sorted by path/stage")
        if any("\0" in record for record in self.status_records):
            raise ValueError("status records may not retain NUL separators")
        return self


class EffectiveChangedBytesV1(_CaseModel):
    path: str = Field(min_length=1, max_length=500)
    before: RepositoryEntryStateV1 | None
    after: RepositoryEntryStateV1 | None

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _canonical_path(value, label="changed-byte path")

    @model_validator(mode="after")
    def validate_actual_change(self) -> EffectiveChangedBytesV1:
        if self.before is None and self.after is None:
            raise ValueError("effective changed bytes require a before or after entry")
        if self.before == self.after:
            raise ValueError("effective changed bytes must describe an actual change")
        return self

    def to_comparison_change(self) -> ComparisonChangedBytes:
        return ComparisonChangedBytes(
            path=self.path,
            before_sha256=None if self.before is None else self.before.sha256,
            after_sha256=None if self.after is None else self.after.sha256,
        )


class CanonicalScopePathV1(_CaseModel):
    path: str = Field(min_length=1, max_length=500)
    old_path: str | None = Field(default=None, min_length=1, max_length=500)
    change_kind: Literal["added", "deleted", "modified", "renamed", "status_only"]
    before: RepositoryEntryStateV1 | None
    after: RepositoryEntryStateV1 | None
    index_status: str = Field(min_length=1, max_length=1)
    worktree_status: str = Field(min_length=1, max_length=1)
    staged: bool
    unstaged: bool
    untracked: bool

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _canonical_path(value, label="scope path")

    @field_validator("old_path")
    @classmethod
    def validate_old_path(cls, value: str | None) -> str | None:
        return None if value is None else _canonical_path(value, label="scope old path")

    @model_validator(mode="after")
    def validate_kind(self) -> CanonicalScopePathV1:
        if (self.change_kind == "renamed") != (self.old_path is not None):
            raise ValueError("only renamed scope entries carry old_path")
        if self.untracked != (self.index_status == "?" and self.worktree_status == "?"):
            raise ValueError("untracked scope state must use the ?? status")
        if self.staged != (self.index_status not in {".", "?"}):
            raise ValueError("staged flag must match index status")
        if self.unstaged != (self.worktree_status not in {".", "?"}):
            raise ValueError("unstaged flag must match worktree status")
        return self


class CanonicalScopeV1(_CaseModel):
    schema_version: Literal[1] = 1
    paths: tuple[CanonicalScopePathV1, ...]
    explicit_deleted_paths: tuple[str, ...]
    explicit_staged_paths: tuple[str, ...]
    status_records: tuple[str, ...]

    @field_validator("explicit_deleted_paths", "explicit_staged_paths")
    @classmethod
    def validate_path_sequence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_canonical_path(path) for path in value)
        if len(set(normalized)) != len(normalized):
            raise ValueError("explicit scope paths must be unique")
        if normalized != tuple(sorted(normalized)):
            raise ValueError("explicit scope paths must be sorted")
        return normalized

    @model_validator(mode="after")
    def validate_path_order(self) -> CanonicalScopeV1:
        keys = tuple((entry.path, entry.old_path or "") for entry in self.paths)
        if keys != tuple(sorted(keys)) or len(set(keys)) != len(keys):
            raise ValueError("scope paths must be unique and sorted")
        return self


@dataclass(frozen=True)
class BenchmarkCase:
    dataset_id: BenchmarkDatasetId
    case_id: str
    case_class: BenchmarkCaseClass
    layer: BenchmarkLayer
    operation: Literal["check", "repair"]
    manifest_sha256: str
    case_root: Path
    manifest: BenchmarkManifest = field(repr=False)


@dataclass(frozen=True)
class PreparedBenchmarkCase:
    """Supervisor-owned prepared input; hidden_oracle is never subject-visible."""

    case: BenchmarkCase = field(repr=False)
    repo_path: Path
    task: BenchmarkTaskV1
    baseline_snapshot: CanonicalRepositorySnapshotV1
    prepared_snapshot: CanonicalRepositorySnapshotV1
    baseline_git_metadata: CanonicalGitMetadataV1
    prepared_git_metadata: CanonicalGitMetadataV1
    scope: CanonicalScopeV1
    snapshot_digest: str
    task_digest: str
    scope_digest: str
    hidden_oracle: NeutralOracleProjectionV1 = field(repr=False)

    @property
    def dataset_id(self) -> BenchmarkDatasetId:
        return self.case.dataset_id

    @property
    def case_id(self) -> str:
        return self.case.case_id

    @property
    def case_manifest_sha256(self) -> str:
        return self.case.manifest_sha256

    def subject_contract(self) -> dict[str, object]:
        """Return only the case-neutral data an adapter may expose to a subject."""

        return {
            "repo_path": str(self.repo_path),
            "task": self.task.model_dump(mode="json"),
            "snapshot_digest": self.snapshot_digest,
            "task_digest": self.task_digest,
            "scope_digest": self.scope_digest,
        }


@dataclass(frozen=True)
class _StatusState:
    index: str
    worktree: str
    untracked: bool = False
    old_path: str | None = None


def canonical_digest(value: BaseModel | Mapping[str, object]) -> str:
    return sha256_prefixed(value)


def _git_environment() -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(
        {
            "GIT_ALLOW_PROTOCOL": "file",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "benchmark-v1",
            "GIT_AUTHOR_EMAIL": "benchmark-v1@example.invalid",
            "GIT_AUTHOR_DATE": _FIXED_GIT_DATE,
            "GIT_COMMITTER_NAME": "benchmark-v1",
            "GIT_COMMITTER_EMAIL": "benchmark-v1@example.invalid",
            "GIT_COMMITTER_DATE": _FIXED_GIT_DATE,
            "LC_ALL": "C",
            "LANG": "C",
            "TZ": "UTC",
        }
    )
    return environment


def _git_bytes(repo: Path, *args: str) -> bytes:
    try:
        completed = subprocess.run(
            [_GIT_EXECUTABLE, *_GIT_OVERRIDES, *args],
            cwd=repo,
            check=True,
            capture_output=True,
            shell=False,
            env=_git_environment(),
        )
    except subprocess.CalledProcessError as error:
        stderr = error.stderr.decode("utf-8", errors="replace").strip()
        raise RepositorySnapshotError(f"git {' '.join(args)} failed: {stderr}") from error
    return completed.stdout


def _git_text(repo: Path, *args: str) -> str:
    try:
        return _git_bytes(repo, *args).decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise RepositorySnapshotError("git output is not valid UTF-8") from error


def _git_optional_text(repo: Path, *args: str) -> str | None:
    completed = subprocess.run(
        [_GIT_EXECUTABLE, *_GIT_OVERRIDES, *args],
        cwd=repo,
        check=False,
        capture_output=True,
        shell=False,
        env=_git_environment(),
    )
    if completed.returncode == 1:
        return None
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RepositorySnapshotError(f"git {' '.join(args)} failed: {stderr}")
    try:
        return completed.stdout.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise RepositorySnapshotError("git output is not valid UTF-8") from error


def _initialize_baseline_repository(repo: Path) -> None:
    _git_bytes(repo, "init", "-q")
    for key, value in _GIT_LOCAL_CONFIG:
        _git_bytes(repo, "config", "--local", key, value)
    _git_bytes(repo, "add", "-A", "--", ".")
    _git_bytes(repo, "commit", "--no-verify", "--no-gpg-sign", "-qm", "baseline")
    if _git_text(repo, "remote"):
        raise RepositorySnapshotError("prepared benchmark repositories may not have remotes")


def _copy_fixture(source: Path, destination: Path) -> None:
    if any(parent.is_symlink() for parent in destination.parents):
        raise BenchmarkCaseError("fixture destination contains a symlink component")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if any(parent.is_symlink() for parent in destination.parents):
        raise BenchmarkCaseError("fixture destination contains a symlink component")
    shutil.copyfile(source, destination)
    destination.chmod(0o644)


def _decode_repo_path(raw: bytes, *, label: str) -> str:
    try:
        return _canonical_path(raw.decode("utf-8"), label=label)
    except UnicodeDecodeError as error:
        raise RepositorySnapshotError(f"{label} is not valid UTF-8") from error
    except ValueError as error:
        raise RepositorySnapshotError(str(error)) from error


def _worktree_entries(repo: Path) -> tuple[CanonicalWorktreeEntryV1, ...]:
    entries: list[CanonicalWorktreeEntryV1] = []
    for path in sorted(repo.rglob("*")):
        relative = path.relative_to(repo)
        if (
            not relative.parts
            or relative.parts[0] == ".git"
            or (path.is_dir() and not path.is_symlink())
        ):
            continue
        try:
            relative_path = _canonical_path(relative.as_posix())
        except ValueError as error:
            raise RepositorySnapshotError(str(error)) from error
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            kind: Literal["file", "symlink"] = "symlink"
            raw = os.fsencode(os.readlink(path))
        elif stat.S_ISREG(metadata.st_mode):
            kind = "file"
            raw = path.read_bytes()
        else:
            raise RepositorySnapshotError(f"unsupported repository entry kind: {relative_path}")
        entries.append(
            CanonicalWorktreeEntryV1(
                path=relative_path,
                kind=kind,
                mode=f"{stat.S_IMODE(metadata.st_mode):04o}",
                size_bytes=len(raw),
                sha256=hashlib.sha256(raw).hexdigest(),
            )
        )
    return tuple(entries)


def _stage_index_records(repo: Path) -> list[tuple[str, int, str, str]]:
    raw = _git_bytes(repo, "ls-files", "--stage", "-z")
    records: list[tuple[str, int, str, str]] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, oid, stage = metadata.decode("ascii").split(" ", 2)
            path = _decode_repo_path(raw_path, label="index path")
            records.append((path, int(stage), mode, oid))
        except (UnicodeDecodeError, ValueError) as error:
            raise RepositorySnapshotError("cannot parse canonical Git index projection") from error
    return records


def _index_flags(repo: Path, records: Sequence[tuple[str, int, str, str]]) -> tuple[int, ...]:
    raw = _git_bytes(repo, "ls-files", "--debug", "-z")
    flags: list[int] = []
    cursor = 0
    for index, (path, _stage, _mode, _oid) in enumerate(records):
        marker = path.encode("utf-8") + b"\0"
        if not raw.startswith(marker, cursor):
            raise RepositorySnapshotError("Git index/debug projections disagree on path order")
        block_start = cursor + len(marker)
        if index + 1 < len(records):
            next_marker = records[index + 1][0].encode("utf-8") + b"\0"
            block_end = raw.find(next_marker, block_start)
            if block_end < 0:
                raise RepositorySnapshotError("Git index debug projection is truncated")
        else:
            block_end = len(raw)
        match = re.search(rb"(?:^|\t)flags: ([0-9a-fA-F]+)\n?$", raw[block_start:block_end])
        if match is None:
            raise RepositorySnapshotError("Git index debug projection lacks flags")
        flags.append(int(match.group(1), 16))
        cursor = block_end
    if cursor != len(raw):
        raise RepositorySnapshotError("Git index debug projection has trailing bytes")
    return tuple(flags)


def _index_entries(repo: Path) -> tuple[CanonicalIndexEntryV1, ...]:
    records = _stage_index_records(repo)
    flags = _index_flags(repo, records)
    entries = [
        CanonicalIndexEntryV1(
            path=path,
            stage=stage,
            mode=mode,
            blob_oid=oid,
            intent_to_add=bool(raw_flags & 0x20000000),
            skip_worktree=bool(raw_flags & 0x40000000),
            assume_unchanged=bool(raw_flags & 0x00008000),
        )
        for (path, stage, mode, oid), raw_flags in zip(records, flags, strict=True)
    ]
    return tuple(sorted(entries, key=lambda entry: (entry.path, entry.stage)))


def _status_records(repo: Path) -> tuple[str, ...]:
    raw = _git_bytes(repo, "status", "--porcelain=v2", "-z", "--untracked-files=all")
    records: list[str] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            records.append(record.decode("utf-8"))
        except UnicodeDecodeError as error:
            raise RepositorySnapshotError("Git status contains a non-UTF-8 path") from error
    return tuple(records)


def _resolved_git_repo(repo_path: Path) -> Path:
    repo = repo_path.resolve()
    if not repo.is_absolute() or not repo.is_dir() or repo_path.is_symlink():
        raise RepositorySnapshotError("snapshot target must be a real absolute repository")
    if not (repo / ".git").is_dir():
        raise RepositorySnapshotError("snapshot target is not a Git worktree")
    return repo


def capture_repository_snapshot(repo_path: Path) -> CanonicalRepositorySnapshotV1:
    repo = _resolved_git_repo(repo_path)
    head_tree = _git_text(repo, "rev-parse", "HEAD^{tree}")
    return CanonicalRepositorySnapshotV1(
        worktree_entries=_worktree_entries(repo),
        head_tree=head_tree,
        index_entries=_index_entries(repo),
        status_records=_status_records(repo),
    )


def _git_refs(repo: Path) -> tuple[CanonicalGitRefV1, ...]:
    output = _git_text(
        repo,
        "for-each-ref",
        "--format=%(refname)\t%(objecttype)\t%(objectname)",
    )
    refs: list[CanonicalGitRefV1] = []
    for line in output.splitlines():
        if not line:
            continue
        fields = line.split("\t")
        if len(fields) != 3:
            raise RepositorySnapshotError("cannot parse canonical Git ref projection")
        refs.append(
            CanonicalGitRefV1(
                name=fields[0],
                object_type=fields[1],
                object_id=fields[2],
            )
        )
    return tuple(sorted(refs, key=lambda item: (item.name, item.object_type, item.object_id)))


def _git_config(repo: Path) -> tuple[CanonicalGitConfigEntryV1, ...]:
    raw = _git_bytes(repo, "config", "--local", "--null", "--list")
    entries: list[CanonicalGitConfigEntryV1] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            raw_key, raw_value = record.split(b"\n", 1)
            key = raw_key.decode("utf-8")
            value = raw_value.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as error:
            raise RepositorySnapshotError("cannot parse canonical Git config projection") from error
        entries.append(CanonicalGitConfigEntryV1(key=key, value=value))
    return tuple(sorted(entries, key=lambda item: (item.key, item.value)))


def _git_hooks(repo: Path) -> tuple[CanonicalGitInternalEntryV1, ...]:
    hooks_root = repo / ".git" / "hooks"
    if not hooks_root.exists():
        return ()
    hooks: list[CanonicalGitInternalEntryV1] = []
    for path in sorted(hooks_root.rglob("*")):
        if path.is_dir() and not path.is_symlink():
            continue
        relative = _canonical_path(
            (Path("hooks") / path.relative_to(hooks_root)).as_posix(),
            label="Git hook path",
        )
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            kind: Literal["file", "symlink"] = "symlink"
            raw = os.fsencode(os.readlink(path))
        elif stat.S_ISREG(metadata.st_mode):
            kind = "file"
            raw = path.read_bytes()
        else:
            raise RepositorySnapshotError(f"unsupported Git hook entry kind: {relative}")
        hooks.append(
            CanonicalGitInternalEntryV1(
                path=relative,
                kind=kind,
                mode=f"{stat.S_IMODE(metadata.st_mode):04o}",
                size_bytes=len(raw),
                sha256=hashlib.sha256(raw).hexdigest(),
            )
        )
    return tuple(hooks)


def capture_git_metadata(repo_path: Path) -> CanonicalGitMetadataV1:
    """Capture refs/config/hooks for safety checks without changing the pair digest."""

    repo = _resolved_git_repo(repo_path)
    return CanonicalGitMetadataV1(
        head_oid=_git_text(repo, "rev-parse", "HEAD"),
        head_ref=_git_optional_text(repo, "symbolic-ref", "--quiet", "HEAD"),
        refs=_git_refs(repo),
        config=_git_config(repo),
        hooks=_git_hooks(repo),
    )


def git_metadata_sha256(metadata: CanonicalGitMetadataV1) -> str:
    """Return the unprefixed canonical digest used by raw-evidence receipts."""

    return canonical_sha256(metadata)


def effective_changed_bytes(
    before: CanonicalRepositorySnapshotV1,
    after: CanonicalRepositorySnapshotV1,
) -> tuple[EffectiveChangedBytesV1, ...]:
    before_entries = {entry.path: entry.state for entry in before.worktree_entries}
    after_entries = {entry.path: entry.state for entry in after.worktree_entries}
    return tuple(
        EffectiveChangedBytesV1(
            path=path,
            before=before_entries.get(path),
            after=after_entries.get(path),
        )
        for path in sorted(before_entries.keys() | after_entries.keys())
        if before_entries.get(path) != after_entries.get(path)
    )


def _parse_status_records(records: Sequence[str]) -> dict[str, _StatusState]:
    parsed: dict[str, _StatusState] = {}
    index = 0
    while index < len(records):
        record = records[index]
        prefix = record[:1]
        old_path: str | None = None
        if prefix == "1":
            fields = record.split(" ", 8)
            if len(fields) != 9:
                raise RepositorySnapshotError("invalid ordinary porcelain-v2 status record")
            xy, path = fields[1], fields[8]
        elif prefix == "2":
            fields = record.split(" ", 9)
            if len(fields) != 10 or index + 1 >= len(records):
                raise RepositorySnapshotError("invalid rename porcelain-v2 status record")
            xy, path = fields[1], fields[9]
            index += 1
            old_path = records[index]
        elif prefix == "u":
            fields = record.split(" ", 10)
            if len(fields) != 11:
                raise RepositorySnapshotError("invalid unmerged porcelain-v2 status record")
            xy, path = fields[1], fields[10]
        elif prefix == "?":
            path = record[2:]
            xy = "??"
        elif prefix == "!":
            index += 1
            continue
        else:
            raise RepositorySnapshotError("unknown porcelain-v2 status record")
        path = _canonical_path(path, label="status path")
        if old_path is not None:
            old_path = _canonical_path(old_path, label="status old path")
        if len(xy) != 2 or path in parsed:
            raise RepositorySnapshotError("duplicate or invalid porcelain-v2 status path")
        parsed[path] = _StatusState(
            index=xy[0],
            worktree=xy[1],
            untracked=xy == "??",
            old_path=old_path,
        )
        index += 1
    return parsed


def _scope_status(status: _StatusState | None) -> tuple[str, str, bool, bool, bool]:
    if status is None:
        return ".", ".", False, False, False
    staged = status.index not in {".", "?"}
    unstaged = status.worktree not in {".", "?"}
    return status.index, status.worktree, staged, unstaged, status.untracked


def build_canonical_scope(
    baseline: CanonicalRepositorySnapshotV1,
    prepared: CanonicalRepositorySnapshotV1,
    manifest: BenchmarkManifest,
) -> CanonicalScopeV1:
    baseline_entries = {entry.path: entry.state for entry in baseline.worktree_entries}
    prepared_entries = {entry.path: entry.state for entry in prepared.worktree_entries}
    status = _parse_status_records(prepared.status_records)
    effective = {change.path: change for change in effective_changed_bytes(baseline, prepared)}
    paths: list[CanonicalScopePathV1] = []
    consumed: set[str] = set()

    for rename in manifest.workspace.renames:
        rename_status = status.get(rename.new_path) or status.get(rename.old_path)
        index_status, worktree_status, staged, unstaged, untracked = _scope_status(rename_status)
        paths.append(
            CanonicalScopePathV1(
                path=rename.new_path,
                old_path=rename.old_path,
                change_kind="renamed",
                before=baseline_entries.get(rename.old_path),
                after=prepared_entries.get(rename.new_path),
                index_status=index_status,
                worktree_status=worktree_status,
                staged=staged,
                unstaged=unstaged,
                untracked=untracked,
            )
        )
        consumed.update((rename.old_path, rename.new_path))

    for deleted_path in manifest.workspace.deleted_paths:
        if deleted_path in consumed:
            continue
        path_status = status.get(deleted_path)
        index_status, worktree_status, staged, unstaged, untracked = _scope_status(path_status)
        paths.append(
            CanonicalScopePathV1(
                path=deleted_path,
                change_kind="deleted",
                before=baseline_entries.get(deleted_path),
                after=prepared_entries.get(deleted_path),
                index_status=index_status,
                worktree_status=worktree_status,
                staged=staged,
                unstaged=unstaged,
                untracked=untracked,
            )
        )
        consumed.add(deleted_path)

    for path in sorted((effective.keys() | status.keys()) - consumed):
        change = effective.get(path)
        before = None if change is None else change.before
        after = None if change is None else change.after
        if change is None:
            kind: Literal["added", "deleted", "modified", "renamed", "status_only"] = "status_only"
        elif before is None:
            kind = "added"
        elif after is None:
            kind = "deleted"
        else:
            kind = "modified"
        path_status = status.get(path)
        index_status, worktree_status, staged, unstaged, untracked = _scope_status(path_status)
        paths.append(
            CanonicalScopePathV1(
                path=path,
                change_kind=kind,
                before=before,
                after=after,
                index_status=index_status,
                worktree_status=worktree_status,
                staged=staged,
                unstaged=unstaged,
                untracked=untracked,
            )
        )

    return CanonicalScopeV1(
        paths=tuple(sorted(paths, key=lambda item: (item.path, item.old_path or ""))),
        explicit_deleted_paths=tuple(sorted(manifest.workspace.deleted_paths)),
        explicit_staged_paths=tuple(sorted(manifest.workspace.staged_paths)),
        status_records=prepared.status_records,
    )


def load_benchmark_cases(
    *,
    structural_root: Path | None = None,
    stage3_root: Path | None = None,
) -> tuple[BenchmarkCase, ...]:
    structural_dataset = (
        default_dataset_root() if structural_root is None else structural_root.resolve()
    )
    stage3_dataset = default_stage3_dataset_root() if stage3_root is None else stage3_root.resolve()
    cases: list[BenchmarkCase] = []
    for structural_manifest in load_catalog(structural_dataset):
        cases.append(
            BenchmarkCase(
                dataset_id="structural-v1",
                case_id=structural_manifest.case_id,
                case_class="portable",
                layer="structural",
                operation=structural_manifest.operation,
                manifest_sha256=FROZEN_MANIFEST_SHA256[structural_manifest.case_id],
                case_root=structural_dataset / structural_manifest.case_id,
                manifest=structural_manifest,
            )
        )
    for stage3_manifest in load_stage3_catalog(stage3_dataset):
        cases.append(
            BenchmarkCase(
                dataset_id="stage3-v1",
                case_id=stage3_manifest.case_id,
                case_class=(
                    "portable" if stage3_manifest.case_id in PORTABLE_CASE_IDS else "control"
                ),
                layer=("executable" if stage3_manifest.case_kind == "executable" else "semantic"),
                operation=stage3_manifest.operation,
                manifest_sha256=FROZEN_STAGE3_MANIFEST_SHA256[stage3_manifest.case_id],
                case_root=stage3_dataset / stage3_manifest.case_id,
                manifest=stage3_manifest,
            )
        )
    portable = tuple(case.case_id for case in cases if case.case_class == "portable")
    controls = tuple(case.case_id for case in cases if case.case_class == "control")
    if portable != PORTABLE_CASE_IDS or controls != CONTROL_CASE_IDS:
        raise BenchmarkCaseError(
            "frozen benchmark selection is not exactly 12 portable + 6 control"
        )
    return tuple(cases)


def load_benchmark_case(
    case_id: str,
    *,
    structural_root: Path | None = None,
    stage3_root: Path | None = None,
) -> BenchmarkCase:
    matches = [
        case
        for case in load_benchmark_cases(
            structural_root=structural_root,
            stage3_root=stage3_root,
        )
        if case.case_id == case_id
    ]
    if len(matches) != 1:
        raise KeyError(f"unknown frozen benchmark case: {case_id}")
    return matches[0]


def _symbol_fqn(key: MatchingKey) -> str:
    symbol = key.symbol_identity
    components = [symbol.module]
    if symbol.owner is not None:
        components.append(symbol.owner)
    components.append(symbol.name)
    return ".".join(components)


def _python_literal(value: object) -> NeutralValueV1:
    if isinstance(value, str) and value.startswith("Constant(value=") and value.endswith(")"):
        try:
            value = ast.literal_eval(value[len("Constant(value=") : -1])
        except (SyntaxError, ValueError) as error:
            raise BenchmarkCaseError(
                f"unsupported frozen Python literal encoding: {value}"
            ) from error
    if value is None or isinstance(value, (bool, int, str)):
        return NeutralValueV1(kind="python_literal", value=value)
    raise BenchmarkCaseError(f"unsupported frozen Python literal: {value!r}")


def _python_annotation(value: object) -> NeutralValueV1:
    if not isinstance(value, str):
        raise BenchmarkCaseError(f"unsupported frozen annotation encoding: {value!r}")
    match = re.fullmatch(r"Name\(id=(['\"])([A-Za-z_]\w*)\1, ctx=Load\(\)\)", value)
    if match is None:
        raise BenchmarkCaseError(f"unsupported frozen annotation encoding: {value}")
    return NeutralValueV1(kind="python_annotation", value=match.group(2))


def _missing() -> NeutralValueV1:
    return NeutralValueV1(kind="missing", value=None)


def _present() -> NeutralValueV1:
    return NeutralValueV1(kind="present", value=None)


def _symbol_value(value: object) -> NeutralValueV1:
    if not isinstance(value, str):
        raise BenchmarkCaseError(f"unsupported frozen symbol encoding: {value!r}")
    return NeutralValueV1(kind="symbol_fqn", value=value)


def _is_missing(value: object) -> bool:
    return isinstance(value, Mapping) and value.get("type") in {
        "missing_parameter",
        "missing_symbol",
    }


def _neutral_key(key: MatchingKey) -> NeutralFindingKeyV1:
    common = {
        "code_path": key.code_path,
        "doc_path": key.doc_path,
        "symbol_fqn": _symbol_fqn(key),
    }
    if key.kind == "parameter_default_changed":
        return NeutralFindingKeyV1(
            **common,
            finding_family="parameter_default_changed",
            component_kind="parameter",
            component_name=key.component,
            old_value=_python_literal(key.old_value),
            new_value=_python_literal(key.new_value),
        )
    if key.kind == "parameter_annotation_changed":
        return NeutralFindingKeyV1(
            **common,
            finding_family="parameter_annotation_changed",
            component_kind="parameter",
            component_name=key.component,
            old_value=_python_annotation(key.old_value),
            new_value=_python_annotation(key.new_value),
        )
    if key.kind == "docstring_parameter_changed":
        return NeutralFindingKeyV1(
            **common,
            finding_family="google_arg_changed",
            component_kind="parameter",
            component_name=key.component,
            old_value=_present() if key.old_value is None else _python_literal(key.old_value),
            new_value=_missing() if _is_missing(key.new_value) else _python_literal(key.new_value),
        )
    if key.kind == "docstring_return_changed":
        return NeutralFindingKeyV1(
            **common,
            finding_family="google_returns_changed",
            component_kind="return",
            component_name=None,
            old_value=_python_annotation(key.old_value),
            new_value=_python_annotation(key.new_value),
        )
    if key.kind == "symbol_reference_renamed":
        return NeutralFindingKeyV1(
            **common,
            finding_family="symbol_renamed",
            component_kind="symbol",
            component_name=None,
            old_value=_symbol_value(key.old_value),
            new_value=_symbol_value(key.new_value),
        )
    if key.kind == "symbol_reference_deleted":
        return NeutralFindingKeyV1(
            **common,
            finding_family="symbol_deleted",
            component_kind="symbol",
            component_name=None,
            old_value=_symbol_value(key.old_value),
            new_value=_missing(),
        )
    if key.kind == "unsupported":
        new_value = _missing() if _is_missing(key.new_value) else _symbol_value(key.new_value)
        return NeutralFindingKeyV1(
            **common,
            finding_family="ambiguous_or_unsupported",
            component_kind="unsupported",
            component_name=None,
            old_value=_symbol_value(key.old_value),
            new_value=new_value,
        )
    if key.kind == "broken_example":
        component_kind = key.component.split(":", 1)[0]
        if component_kind not in {"doctest", "pytest"}:
            raise BenchmarkCaseError(f"unsupported executable component: {key.component}")
        return NeutralFindingKeyV1(
            code_path=key.code_path,
            doc_path=key.doc_path,
            symbol_fqn=None,
            finding_family="broken_example",
            component_kind=cast(Literal["doctest", "pytest"], component_kind),
            component_name=None,
            old_value=NeutralValueV1(kind="validation_status", value="passed"),
            new_value=NeutralValueV1(kind="validation_status", value="failed"),
        )
    raise BenchmarkCaseError(f"portable neutral projection is undefined for {key.kind}")


def project_neutral_oracle(case: BenchmarkCase) -> NeutralOracleProjectionV1:
    if case.case_class != "portable":
        raise BenchmarkCaseError(
            "control cases use the private Stage 3 scorer, not a neutral oracle"
        )
    findings = tuple(_neutral_key(key) for key in case.manifest.expected.finding_multiset)
    identities = tuple(canonical_json_bytes(finding) for finding in findings)
    if len(set(identities)) != len(identities):
        raise BenchmarkCaseError(f"neutral oracle projection collides for {case.case_id}")
    expected_changes = tuple(
        ComparisonChangedBytes(
            path=change.path,
            before_sha256=change.before_sha256,
            after_sha256=change.after_sha256,
        )
        for change in case.manifest.expected.changed_bytes
    )
    return NeutralOracleProjectionV1(
        encoding_sha256=neutral_finding_encoding_sha256(),
        dataset_id=case.dataset_id,
        case_id=case.case_id,
        case_manifest_sha256=case.manifest_sha256,
        operation=case.operation,
        expected_status=case.manifest.expected.status,
        findings=findings,
        expected_changed_bytes=expected_changes,
    )


def prepare_benchmark_case(
    case_id: str,
    workspace_root: Path,
    *,
    opaque_id: str | None = None,
    structural_root: Path | None = None,
    stage3_root: Path | None = None,
) -> PreparedBenchmarkCase:
    case = load_benchmark_case(
        case_id,
        structural_root=structural_root,
        stage3_root=stage3_root,
    )
    if case.case_class != "portable":
        raise BenchmarkCaseError("control cases must run through the isolated control runner")
    workspace = workspace_root.resolve()
    if workspace_root.is_symlink() or case.case_id in workspace.as_posix():
        raise BenchmarkCaseError("subject-visible workspace must be opaque and non-symlinked")
    workspace.mkdir(parents=True, exist_ok=True)
    opaque = secrets.token_hex(16) if opaque_id is None else opaque_id
    if _OPAQUE_ID.fullmatch(opaque) is None:
        raise BenchmarkCaseError("opaque repo id must be exactly 32 lowercase hexadecimal chars")
    repo = workspace / f"repo-{opaque}"
    if case.case_id in repo.as_posix():
        raise BenchmarkCaseError("subject repo path leaks the trusted case id")
    repo.mkdir(parents=False, exist_ok=False)

    for fixture in case.manifest.files:
        if fixture.role == "base":
            _copy_fixture(case.case_root / fixture.path, repo / fixture.target_path)
    _initialize_baseline_repository(repo)
    baseline = capture_repository_snapshot(repo)
    baseline_git_metadata = capture_git_metadata(repo)
    if baseline.status_records:
        raise RepositorySnapshotError("baseline commit must have a clean worktree and index")

    for rename in case.manifest.workspace.renames:
        source = repo / rename.old_path
        destination = repo / rename.new_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.replace(destination)
    for relative in case.manifest.workspace.deleted_paths:
        path = repo / relative
        if os.path.lexists(path):
            path.unlink()
    for fixture in case.manifest.files:
        if fixture.role == "current":
            _copy_fixture(case.case_root / fixture.path, repo / fixture.target_path)
    if case.manifest.workspace.staged_paths:
        _git_bytes(repo, "add", "-A", "--", *case.manifest.workspace.staged_paths)

    prepared = capture_repository_snapshot(repo)
    prepared_git_metadata = capture_git_metadata(repo)
    task = BenchmarkTaskV1(operation=case.operation)
    scope = build_canonical_scope(baseline, prepared, case.manifest)
    return PreparedBenchmarkCase(
        case=case,
        repo_path=repo,
        task=task,
        baseline_snapshot=baseline,
        prepared_snapshot=prepared,
        baseline_git_metadata=baseline_git_metadata,
        prepared_git_metadata=prepared_git_metadata,
        scope=scope,
        snapshot_digest=canonical_digest(prepared),
        task_digest=canonical_digest(task),
        scope_digest=canonical_digest(scope),
        hidden_oracle=project_neutral_oracle(case),
    )


__all__ = [
    "CONTROL_CASE_IDS",
    "EXECUTABLE_PORTABLE_CASE_IDS",
    "PORTABLE_CASE_IDS",
    "STRUCTURAL_PORTABLE_CASE_IDS",
    "BenchmarkCase",
    "BenchmarkCaseClass",
    "BenchmarkCaseError",
    "CanonicalGitConfigEntryV1",
    "CanonicalGitInternalEntryV1",
    "CanonicalGitMetadataV1",
    "CanonicalGitRefV1",
    "CanonicalIndexEntryV1",
    "CanonicalRepositorySnapshotV1",
    "CanonicalScopePathV1",
    "CanonicalScopeV1",
    "CanonicalWorktreeEntryV1",
    "EffectiveChangedBytesV1",
    "PreparedBenchmarkCase",
    "RepositoryEntryStateV1",
    "RepositorySnapshotError",
    "build_canonical_scope",
    "canonical_digest",
    "canonical_json_bytes",
    "capture_git_metadata",
    "capture_repository_snapshot",
    "effective_changed_bytes",
    "git_metadata_sha256",
    "load_benchmark_case",
    "load_benchmark_cases",
    "prepare_benchmark_case",
    "project_neutral_oracle",
]

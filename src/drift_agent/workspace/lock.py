from __future__ import annotations

import fcntl
import hashlib
import importlib
import json
import math
import os
import socket
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

from drift_agent.workspace.identity import IdentitySet

LOCK_SCHEMA_VERSION = 1
DEFAULT_LOCK_TIMEOUT_SECONDS = 5.0
_POLL_INTERVAL_SECONDS = 0.05
_PROCESS_START_TOKENS: dict[int, str] = {}


class WorkspaceLockError(RuntimeError):
    reason_code = "workspace_lock"


class LockTimeoutError(WorkspaceLockError):
    reason_code = "lock_timeout"


class LockMetadataError(WorkspaceLockError):
    reason_code = "lock_metadata"


class LockOwnershipError(WorkspaceLockError):
    reason_code = "lock_ownership"


class MonotonicClock(Protocol):
    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class SystemMonotonicClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


@dataclass(frozen=True, slots=True)
class LockOwner:
    schema_version: int
    repository_id: str
    workspace_id: str
    run_id: str
    pid: int
    hostname: str
    process_start_token: str
    generation: int
    acquired_at: str


@dataclass(frozen=True, slots=True)
class LockObservation:
    generation: int
    active_writer: bool
    owner: LockOwner | None = None


def _process_start_token() -> str:
    """Return a stable token per live process, including after ``fork``."""

    pid = os.getpid()
    token = _PROCESS_START_TOKENS.get(pid)
    if token is None:
        token = hashlib.sha256(f"{pid}:{time.time_ns()}:{time.monotonic_ns()}".encode()).hexdigest()
        _PROCESS_START_TOKENS[pid] = token
    return token


def resolve_lock_root(runtime_root: Path | None = None) -> Path:
    """Resolve the fixed runtime namespace without creating it."""

    if runtime_root is not None:
        base = runtime_root.expanduser().resolve(strict=False)
    else:
        try:
            platform_runtime_path = cast(
                Callable[[str], str | Path],
                importlib.import_module("platformdirs").user_runtime_path,
            )
        except ModuleNotFoundError:
            xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
            base = (
                Path(xdg_runtime) / "drift-agent"
                if xdg_runtime
                else Path(tempfile.gettempdir()) / "drift-agent-runtime"
            )
        else:
            base = Path(platform_runtime_path("drift-agent"))
    return base / "locks-v1"


def _validate_key(value: str, field_name: str) -> str:
    if not value or value in {".", ".."}:
        raise ValueError(f"{field_name} must not be empty")
    if any(
        character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
        for character in value
    ):
        raise ValueError(f"{field_name} contains unsafe path characters")
    return value


def _owner_from_object(value: object) -> LockOwner | None:
    if not isinstance(value, dict):
        return None
    required_string_fields = (
        "repository_id",
        "workspace_id",
        "run_id",
        "hostname",
        "process_start_token",
        "acquired_at",
    )
    if any(not isinstance(value.get(field), str) for field in required_string_fields):
        return None
    schema_version = value.get("schema_version")
    pid = value.get("pid")
    generation = value.get("generation")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != LOCK_SCHEMA_VERSION
        or not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 1
    ):
        return None
    return LockOwner(
        schema_version=schema_version,
        repository_id=cast(str, value["repository_id"]),
        workspace_id=cast(str, value["workspace_id"]),
        run_id=cast(str, value["run_id"]),
        pid=pid,
        hostname=cast(str, value["hostname"]),
        process_start_token=cast(str, value["process_start_token"]),
        generation=generation,
        acquired_at=cast(str, value["acquired_at"]),
    )


def _read_owner(owner_path: Path) -> LockOwner | None:
    try:
        raw = owner_path.read_bytes()
        decoded = cast(object, json.loads(raw.decode("utf-8")))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return _owner_from_object(decoded)


def _write_owner(owner_path: Path, owner: LockOwner) -> None:
    payload = json.dumps(
        asdict(owner),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=owner_path.parent,
        prefix=f".{owner_path.name}.",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        # Keep lock diagnostics independent from WorkspaceTransaction's
        # os.replace fault-injection hook.  The files share one directory, so
        # POSIX rename still publishes the sidecar atomically.
        os.rename(temporary, owner_path)
    except OSError as error:
        raise LockMetadataError("could not publish workspace lock owner metadata") from error
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _probe_paths(lock_path: Path, owner_path: Path) -> LockObservation:
    """Probe OS ownership without creating files or changing the sidecar."""

    owner_before = _read_owner(owner_path)
    generation_before = owner_before.generation if owner_before is not None else 0
    try:
        descriptor = os.open(lock_path, os.O_RDWR | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        # The sample point is the failed open.  A writer that creates the lock
        # immediately afterwards must remain visible as a generation change at
        # the second sample, rather than being folded into this observation.
        return LockObservation(
            generation=generation_before,
            active_writer=False,
            owner=owner_before,
        )
    except OSError:
        # An unprobeable existing lock path cannot be treated as available.
        return LockObservation(
            generation=generation_before,
            active_writer=True,
            owner=owner_before,
        )

    active_writer = False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            active_writer = True
        else:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)
    owner_after = _read_owner(owner_path) if active_writer else owner_before
    generation_after = owner_after.generation if owner_after is not None else 0
    return LockObservation(
        generation=(
            max(generation_before, generation_after) if active_writer else generation_before
        ),
        active_writer=active_writer,
        owner=owner_after or owner_before,
    )


def probe_workspace_lock(
    workspace_id: str,
    *,
    runtime_root: Path | None = None,
) -> LockObservation:
    safe_workspace_id = _validate_key(workspace_id, "workspace_id")
    lock_root = resolve_lock_root(runtime_root)
    return _probe_paths(
        lock_root / f"{safe_workspace_id}.lock",
        lock_root / f"{safe_workspace_id}.owner.json",
    )


class WorkspaceLock:
    """Repair-only OS advisory lock keyed by writable workspace identity."""

    def __init__(
        self,
        *,
        repository_id: str,
        workspace_id: str,
        run_id: str,
        timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
        runtime_root: Path | None = None,
        clock: MonotonicClock | None = None,
    ) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds < 0:
            raise ValueError("lock timeout must be finite and non-negative")
        self.repository_id = _validate_key(repository_id, "repository_id")
        self.workspace_id = _validate_key(workspace_id, "workspace_id")
        self.run_id = _validate_key(run_id, "run_id")
        self.timeout_seconds = timeout_seconds
        self.lock_root = resolve_lock_root(runtime_root)
        self.lock_path = self.lock_root / f"{self.workspace_id}.lock"
        self.owner_path = self.lock_root / f"{self.workspace_id}.owner.json"
        self.clock = clock or SystemMonotonicClock()
        self._descriptor: int | None = None
        self._owner: LockOwner | None = None

    @classmethod
    def from_identities(
        cls,
        identities: IdentitySet,
        *,
        run_id: str,
        timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
        runtime_root: Path | None = None,
        clock: MonotonicClock | None = None,
    ) -> WorkspaceLock:
        return cls(
            repository_id=identities.repository.digest,
            workspace_id=identities.workspace.digest,
            run_id=run_id,
            timeout_seconds=timeout_seconds,
            runtime_root=runtime_root,
            clock=clock,
        )

    @property
    def acquired(self) -> bool:
        return self._descriptor is not None

    @property
    def owner(self) -> LockOwner | None:
        return self._owner

    def probe(self) -> LockObservation:
        return _probe_paths(self.lock_path, self.owner_path)

    def acquire(self) -> LockOwner:
        if self._descriptor is not None:
            raise LockOwnershipError("workspace lock is already held by this object")
        try:
            self.lock_root.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise LockMetadataError("could not create workspace lock root") from error
        if not self.lock_root.is_dir():
            raise LockMetadataError("workspace lock root is not a directory")
        try:
            descriptor = os.open(
                self.lock_path,
                os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
        except OSError as error:
            raise LockMetadataError("could not open workspace OS lock") from error
        try:
            os.fchmod(descriptor, 0o600)
        except OSError as error:
            os.close(descriptor)
            raise LockMetadataError("could not secure workspace OS lock") from error

        deadline = self.clock.monotonic() + self.timeout_seconds
        try:
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as error:
                    now = self.clock.monotonic()
                    if now >= deadline:
                        raise LockTimeoutError(
                            f"workspace lock timed out after {self.timeout_seconds:.3f}s"
                        ) from error
                    self.clock.sleep(min(_POLL_INTERVAL_SECONDS, deadline - now))

            previous_owner = _read_owner(self.owner_path)
            generation = (previous_owner.generation if previous_owner is not None else 0) + 1
            owner = LockOwner(
                schema_version=LOCK_SCHEMA_VERSION,
                repository_id=self.repository_id,
                workspace_id=self.workspace_id,
                run_id=self.run_id,
                pid=os.getpid(),
                hostname=socket.gethostname(),
                process_start_token=_process_start_token(),
                generation=generation,
                acquired_at=datetime.now(UTC).isoformat(),
            )
            _write_owner(self.owner_path, owner)
        except BaseException:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(descriptor)
            raise

        self._descriptor = descriptor
        self._owner = owner
        return owner

    def release(self) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            return
        self._descriptor = None
        self._owner = None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def __enter__(self) -> LockOwner:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        self.release()

    def close(self) -> None:
        self.release()


WorkspaceWriteLock = WorkspaceLock

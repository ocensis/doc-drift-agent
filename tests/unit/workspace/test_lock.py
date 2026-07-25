from __future__ import annotations

from pathlib import Path

import pytest

from drift_agent.workspace.lock import (
    LockTimeoutError,
    WorkspaceLock,
    probe_workspace_lock,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _lock(
    runtime_root: Path,
    run_id: str,
    *,
    timeout: float = 5.0,
    clock: FakeClock | None = None,
) -> WorkspaceLock:
    return WorkspaceLock(
        repository_id="repository-a",
        workspace_id="workspace-a",
        run_id=run_id,
        timeout_seconds=timeout,
        runtime_root=runtime_root,
        clock=clock,
    )


def test_probe_is_read_only_and_stale_owner_advances_generation(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"

    before = probe_workspace_lock("workspace-a", runtime_root=runtime_root)

    assert before.active_writer is False
    assert before.generation == 0
    assert before.owner is None
    assert not runtime_root.exists()

    first = _lock(runtime_root, "run-one")
    first_owner = first.acquire()
    try:
        active = first.probe()
        assert active.active_writer is True
        assert active.owner == first_owner
        assert active.generation == 1
    finally:
        first.release()

    stale = first.probe()
    assert stale.active_writer is False
    assert stale.owner == first_owner
    assert stale.generation == 1

    second = _lock(runtime_root, "run-two")
    second_owner = second.acquire()
    try:
        assert second_owner.generation == 2
        assert second_owner.workspace_id == "workspace-a"
        assert second_owner.run_id == "run-two"
    finally:
        second.release()


def test_contention_obeys_exact_monotonic_timeout_without_target_writes(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    holder = _lock(runtime_root, "holder")
    holder.acquire()
    clock = FakeClock()
    contender = _lock(runtime_root, "contender", clock=clock)

    try:
        with pytest.raises(LockTimeoutError, match=r"5\.000s"):
            contender.acquire()
    finally:
        holder.release()

    assert clock.now == pytest.approx(5.0)
    assert sum(clock.sleeps) == pytest.approx(5.0)
    assert contender.acquired is False


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_context_manager_releases_after_catchable_base_exception(
    tmp_path: Path,
    interrupt: type[BaseException],
) -> None:
    runtime_root = tmp_path / "runtime"
    first = _lock(runtime_root, "interrupted")

    with pytest.raises(interrupt):
        with first:
            raise interrupt

    assert first.acquired is False
    successor = _lock(runtime_root, "successor", timeout=0.0)
    owner = successor.acquire()
    try:
        assert owner.run_id == "successor"
    finally:
        successor.release()

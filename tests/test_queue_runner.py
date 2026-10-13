import asyncio
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from caf_control_api.storage import ClaimedRun
from caf_agent_runtime.windows_job import ProcessTreeCleanupError
from caf_agent_runtime import CodexRuntime, CodingTask
from caf_orchestration_worker.queue_runner import CleanupTimeout, LostLeaseError, dispatch_once
from caf_orchestration_worker.bounded_worker import run_one_synthetic
from caf_orchestration_worker.synthetic_daemon import serve


def claim():
    return ClaimedRun(uuid4(), uuid4(), uuid4(), "caf-sample-app", "a" * 40,
                      "small task", ["test passes"], "codex", 1, 1)


class FakeStore:
    def __init__(self):
        self.run = claim()
        self.queued = True
        self.cancelled = False
        self.valid = True
        self.completed = None
        self.quarantines = 0
        self.renewals = 0

    def quarantine_expired(self):
        self.quarantines += 1
        return []

    def claim_queued(self, owner, lease_seconds=60):
        if not self.queued:
            return None
        self.queued = False
        return self.run

    def claim_control(self, run_id, owner):
        return self.valid, self.cancelled

    def renew_lease(self, run_id, owner, lease_seconds=60):
        self.renewals += 1
        return self.valid

    def complete_claim(self, run_id, owner, status, artifact):
        if not self.valid or (self.cancelled and status != "cancelled"):
            return False
        self.completed = status, artifact
        return True


def test_success_ack_is_idempotent_at_claim_boundary():
    store = FakeStore()
    calls = []

    async def execute(run):
        calls.append(run.id)
        return {"status": "succeeded", "run_id": str(run.id)}

    artifact = asyncio.run(dispatch_once(store, "worker-12345", execute, tick_seconds=.01))
    assert store.completed == ("succeeded", artifact)
    assert calls == [store.run.id]
    assert asyncio.run(dispatch_once(store, "worker-12345", execute, tick_seconds=.01)) is None
    assert calls == [store.run.id]
    assert store.quarantines == 2


def test_pre_dispatch_cancel_does_not_invoke_executor():
    store = FakeStore()
    store.cancelled = True

    async def execute(_):
        pytest.fail("execution must not start")

    assert asyncio.run(dispatch_once(store, "worker-12345", execute))["reason"] == "requested_before_execution"
    assert store.completed[0] == "cancelled"


def test_running_cancel_stops_executor_before_ack():
    store = FakeStore()
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def execute(_):
        started.set()
        try:
            await asyncio.sleep(5)
        finally:
            cleaned.set()
        return {"status": "succeeded"}

    async def scenario():
        worker = asyncio.create_task(dispatch_once(store, "worker-12345", execute, tick_seconds=.01))
        await started.wait()
        store.cancelled = True
        await worker

    asyncio.run(scenario())
    assert cleaned.is_set()
    assert store.completed[0] == "cancelled"


def test_lost_lease_cancels_executor_and_never_acks():
    store = FakeStore()
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def execute(_):
        started.set()
        try:
            await asyncio.sleep(5)
        finally:
            cleaned.set()

    async def scenario():
        worker = asyncio.create_task(dispatch_once(store, "worker-12345", execute, tick_seconds=.01))
        await started.wait()
        store.valid = False
        with pytest.raises(LostLeaseError):
            await worker

    asyncio.run(scenario())
    assert cleaned.is_set()
    assert store.completed is None


def test_cancel_racing_ack_is_not_reported_as_success():
    store = FakeStore()
    original = store.complete_claim

    def race(run_id, owner, status, artifact):
        if status == "succeeded":
            store.cancelled = True
        return original(run_id, owner, status, artifact)

    store.complete_claim = race

    async def execute(_):
        return {"status": "succeeded"}

    result = asyncio.run(dispatch_once(store, "worker-12345", execute, tick_seconds=.01))
    assert result["reason"] == "requested_before_ack"
    assert store.completed[0] == "cancelled"


def test_stubborn_executor_does_not_ack_cancelled_run():
    store = FakeStore()
    started = asyncio.Event()

    async def execute(_):
        started.set()
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            await asyncio.sleep(.1)
        return {"status": "succeeded"}

    async def scenario():
        worker = asyncio.create_task(dispatch_once(
            store, "worker-12345", execute, tick_seconds=.01, cleanup_seconds=.02,
        ))
        await started.wait()
        store.cancelled = True
        with pytest.raises(CleanupTimeout):
            await worker

    asyncio.run(scenario())
    assert store.completed is None


def test_cleanup_parameters_leave_lease_margin():
    store = FakeStore()

    async def execute(_):
        return {"status": "succeeded"}

    with pytest.raises(ValueError, match="lease margin"):
        asyncio.run(dispatch_once(store, "worker-12345", execute,
                                  lease_seconds=15, tick_seconds=4, cleanup_seconds=9))
    assert store.queued is True


def test_unverified_process_cleanup_leaves_claim_unacknowledged():
    store = FakeStore()

    async def execute(_):
        raise ProcessTreeCleanupError("unverified orphan")

    with pytest.raises(ProcessTreeCleanupError):
        asyncio.run(dispatch_once(store, "worker-12345", execute, tick_seconds=.01))
    assert store.completed is None


def test_one_claim_through_real_process_runtime_without_coding_cli(tmp_path: Path):
    store = FakeStore()
    script = tmp_path / "fixture.py"
    script.write_text("print('fixture complete')\n", encoding="utf-8")
    runtime = CodexRuntime(executable=Path(sys.executable), timeout_seconds=10)
    runtime.command = lambda _: (sys.executable, str(script))  # type: ignore[method-assign]

    async def execute(run):
        result = await runtime.execute(CodingTask(
            str(run.id), str(run.correlation_id), "No coding", "codex",
        ), tmp_path)
        return {"status": result.status.value, "execution_kind": "local-process-fixture"}

    artifact = asyncio.run(dispatch_once(store, "worker-12345", execute, tick_seconds=.01))
    assert artifact == {"status": "succeeded", "execution_kind": "local-process-fixture"}
    assert store.completed == ("succeeded", artifact)
    assert store.renewals >= 1


def test_bounded_synthetic_worker_guards_database_and_persists_artifact(tmp_path: Path):
    store = FakeStore()
    store.database_name = lambda: "caf"
    with pytest.raises(ValueError, match="caf_queue_test_"):
        asyncio.run(run_one_synthetic(store, "worker-12345", tmp_path))  # type: ignore[arg-type]
    assert store.queued
    store.database_name = lambda: "caf_queue_test_unit"
    artifact = asyncio.run(run_one_synthetic(store, "worker-12345", tmp_path))  # type: ignore[arg-type]
    assert artifact["execution_kind"] == "bounded-synthetic-worker"
    assert artifact["status"] == "succeeded"
    assert store.completed == ("succeeded", artifact)


def test_persistent_synthetic_worker_polls_until_stopped(tmp_path: Path):
    store = FakeStore()
    store.queued = False
    store.database_name = lambda: "caf_queue_test_unit"
    heartbeat = tmp_path / "heartbeat"

    async def scenario():
        stop = asyncio.Event()
        worker = asyncio.create_task(serve(
            store, tmp_path, stop, heartbeat, poll_seconds=.01,  # type: ignore[arg-type]
        ))
        for _ in range(100):
            if store.quarantines >= 2:
                break
            await asyncio.sleep(.01)
        stop.set()
        await worker

    asyncio.run(scenario())
    assert store.quarantines >= 2
    assert heartbeat.read_text() == "ready\n"

"""Bounded lease-aware dispatch; executor supplies actual runtime and cleanup."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Protocol
from uuid import UUID

from caf_control_api.storage import ClaimedRun


class QueueStore(Protocol):
    def claim_queued(self, owner: str, lease_seconds: int = 60) -> ClaimedRun | None: ...
    def claim_control(self, run_id: UUID, owner: str) -> tuple[bool, bool]: ...
    def renew_lease(self, run_id: UUID, owner: str, lease_seconds: int = 60) -> bool: ...
    def complete_claim(self, run_id: UUID, owner: str, status: str, artifact: dict) -> bool: ...
    def quarantine_expired(self) -> list[UUID]: ...


Executor = Callable[[ClaimedRun], Awaitable[dict]]


class LostLeaseError(RuntimeError):
    """A worker must stop execution when ownership can no longer be proven."""


class CleanupTimeout(RuntimeError):
    """Execution did not stop in time; leave the Run unacknowledged."""


async def _stop_work(work: asyncio.Task[dict], timeout_seconds: float) -> None:
    if work.done():
        return
    work.cancel()
    done, _ = await asyncio.wait({work}, timeout=timeout_seconds)
    if not done:
        raise CleanupTimeout("executor did not stop after cancellation")
    await asyncio.gather(work, return_exceptions=True)


async def dispatch_once(
    store: QueueStore,
    owner: str,
    execute: Executor,
    *,
    lease_seconds: int = 60,
    tick_seconds: float = 5,
    cleanup_seconds: float = 10,
) -> dict | None:
    """Claim one run, renew while active, cancel on request or lost ownership.

    The executor must terminate all child processes when its task is cancelled.
    Expired runs are quarantined rather than replayed: a CLI might survive a
    worker crash, so retry is unsafe until that process is reconciled.
    """
    if tick_seconds <= 0 or tick_seconds >= lease_seconds / 3:
        raise ValueError("tick_seconds must be positive and below one third of lease")
    if cleanup_seconds <= 0 or tick_seconds + cleanup_seconds + max(2, lease_seconds * .1) >= lease_seconds:
        raise ValueError("tick and cleanup must leave a lease margin")
    await asyncio.to_thread(store.quarantine_expired)
    claim = await asyncio.to_thread(store.claim_queued, owner, lease_seconds)
    if claim is None:
        return None

    work: asyncio.Task[dict] | None = None
    terminal = "failed"
    artifact: dict = {"reason": "worker_failure", "run_id": str(claim.id)}
    try:
        valid, cancelled = await asyncio.to_thread(store.claim_control, claim.id, owner)
        if not valid:
            raise LostLeaseError("claim ownership was lost before execution")
        if cancelled:
            terminal, artifact = "cancelled", {"reason": "requested_before_execution"}
        else:
            work = asyncio.create_task(execute(claim))
            while True:
                done, _ = await asyncio.wait({work}, timeout=tick_seconds)
                valid, cancelled = await asyncio.to_thread(store.claim_control, claim.id, owner)
                if not valid:
                    raise LostLeaseError("claim ownership was lost during execution")
                if cancelled:
                    if not await asyncio.to_thread(store.renew_lease, claim.id, owner, lease_seconds):
                        raise LostLeaseError("lease renewal failed before cancellation")
                    await _stop_work(work, cleanup_seconds)
                    terminal, artifact = "cancelled", {"reason": "requested_during_execution"}
                    break
                if not await asyncio.to_thread(store.renew_lease, claim.id, owner, lease_seconds):
                    raise LostLeaseError("lease renewal failed")
                if done:
                    try:
                        artifact = work.result()
                        json.dumps(artifact)
                        terminal = "succeeded" if artifact.get("status") == "succeeded" else "failed"
                    except Exception as exc:
                        artifact = {"reason": "executor_exception", "error_type": type(exc).__name__}
                        terminal = "failed"
                    break
        if not await asyncio.to_thread(store.complete_claim, claim.id, owner, terminal, artifact):
            valid, cancelled = await asyncio.to_thread(store.claim_control, claim.id, owner)
            if valid and cancelled and terminal != "cancelled":
                terminal, artifact = "cancelled", {"reason": "requested_before_ack"}
                if await asyncio.to_thread(store.complete_claim, claim.id, owner, terminal, artifact):
                    return artifact
            raise LostLeaseError("conditional completion failed")
        return artifact
    finally:
        if work is not None and not work.done():
            await _stop_work(work, cleanup_seconds)

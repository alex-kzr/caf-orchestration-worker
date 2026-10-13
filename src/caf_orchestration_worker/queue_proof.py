"""Explicit, one-run queue-to-synthetic-runtime proof; never launches agent CLIs."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Mapping, Protocol
from uuid import UUID

from caf_agent_runtime import SyntheticOutcome, SyntheticRuntime
from caf_agent_runtime.windows_job import ProcessTreeCleanupError
from caf_control_api.storage import ClaimedRun

from .workflow import RunRequest, run_baseline


class QueueStore(Protocol):
    def database_name(self) -> str: ...
    def claim_queued(self, owner: str, lease_seconds: int = 60) -> ClaimedRun | None: ...
    def complete_claim(self, run_id: UUID, owner: str, status: str, artifact: dict) -> bool: ...


class LostLeaseError(RuntimeError):
    """The worker no longer owns a claimed run."""


async def run_synthetic_claim_once(
    store: QueueStore,
    owner: str,
    allowed_repositories: Mapping[str, Path],
) -> dict | None:
    """Exercise queue handoff without CLI, Git mutation, or a long-running daemon.

    Intended only for short, deterministic proof runs. A real executor needs
    lease renewal, orphan reconciliation and a separately approved CLI path.
    """
    database_name = await asyncio.to_thread(store.database_name)
    if not database_name.startswith("caf_queue_test_"):
        raise ValueError("synthetic queue proof requires a dedicated caf_queue_test_* database")
    claim = await asyncio.to_thread(store.claim_queued, owner)
    if claim is None:
        return None

    workspace = allowed_repositories.get(claim.target_repository)
    if workspace is None or not workspace.is_dir():
        artifact = {
            "execution_kind": "synthetic-queue-proof", "status": "blocked",
            "reason": "repository_not_allowed", "run_id": str(claim.id),
            "correlation_id": str(claim.correlation_id),
        }
        if not await asyncio.to_thread(store.complete_claim, claim.id, owner, "failed", artifact):
            raise LostLeaseError("claim ownership was lost")
        return artifact

    runtime = SyntheticRuntime({
        "review": [SyntheticOutcome(output={"approved": True, "findings": []})],
        "test": [SyntheticOutcome(output={"passed": True, "checks": ["synthetic gate"]})],
    })
    request = RunRequest(
        str(claim.id), str(claim.correlation_id), claim.prompt,
        claim.provider, claim.max_corrections, claim.base_revision,
    )
    try:
        artifact = (await run_baseline(request, runtime, workspace)).to_dict()
    except Exception as exc:
        if isinstance(exc, ProcessTreeCleanupError):
            raise
        artifact = {
            "status": "blocked", "reason": "worker_exception",
            "error_type": type(exc).__name__, "run_id": str(claim.id),
            "correlation_id": str(claim.correlation_id),
        }
    artifact["execution_kind"] = "synthetic-queue-proof"
    artifact = json.loads(json.dumps(artifact))
    terminal = "succeeded" if artifact["status"] == "succeeded" else "failed"
    if not await asyncio.to_thread(store.complete_claim, claim.id, owner, terminal, artifact):
        raise LostLeaseError("claim ownership was lost")
    return artifact

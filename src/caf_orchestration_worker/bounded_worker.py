"""One bounded queue claim through the normal lease-aware dispatcher."""

from __future__ import annotations

import asyncio
from pathlib import Path

from caf_agent_runtime import SyntheticOutcome, SyntheticRuntime
from caf_control_api.storage import ClaimedRun, PostgresStore

from .queue_runner import dispatch_once
from .workflow import RunRequest, run_baseline


async def run_one_synthetic(
    store: PostgresStore, owner: str, workspace: Path,
) -> dict | None:
    """Use only an isolated test database; never consume the shared CAF queue."""
    database_name = await asyncio.to_thread(store.database_name)
    if not database_name.startswith("caf_queue_test_"):
        raise ValueError("synthetic worker requires a caf_queue_test_* database")
    if not workspace.is_dir():
        raise ValueError("workspace must exist")

    async def execute(claim: ClaimedRun) -> dict:
        if claim.target_repository != "caf-sample-app":
            return {"status": "blocked", "reason": "repository_not_allowed"}
        if claim.prompt == "CAF_SYNTHETIC_DELAY_3_SECONDS":
            # Bounded test-only delay makes lease renewal observable in PG.
            await asyncio.sleep(3)
        if claim.prompt == "CAF_SYNTHETIC_DELAY_8_SECONDS":
            # Test-owned worker-crash fixture; never invokes a coding CLI.
            await asyncio.sleep(8)
        runtime = SyntheticRuntime({
            "review": [SyntheticOutcome(output={"approved": True, "findings": []})],
            "test": [SyntheticOutcome(output={"passed": True, "checks": ["synthetic gate"]})],
        })
        request = RunRequest(
            str(claim.id), str(claim.correlation_id), claim.prompt,
            claim.provider, claim.max_corrections, claim.base_revision,
        )
        artifact = (await run_baseline(request, runtime, workspace)).to_dict()
        artifact["execution_kind"] = "bounded-synthetic-worker"
        return artifact

    return await dispatch_once(store, owner, execute, tick_seconds=1)

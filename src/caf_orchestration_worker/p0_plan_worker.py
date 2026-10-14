"""Opt-in one-Run planning diagnostic; no coding runtime is constructed."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from caf_agent_runtime import ClaudeRuntime

from .api_store import WorkerAPIStore
from .p0_flow_worker import run_one_flow_fixture
from .p0_flow_fixture import PLAN_SCHEMA
from .workspace_binding import resolve_sample_workspace


class _NoCodingRuntime:
    """Defense in depth if a downstream coding stage is accidentally scheduled."""

    async def execute(self, *args, **kwargs):
        raise RuntimeError("plan-only entrypoint forbids coding stages")

    async def cancel(self, run_id: str) -> None:
        raise RuntimeError("plan-only entrypoint has no coding process")

    async def get_status(self, run_id: str):
        raise RuntimeError("plan-only entrypoint has no coding process")


def main() -> int:
    binding = resolve_sample_workspace(Path(os.environ["CAF_UMBRELLA_ROOT"]),
                                       "caf-sample-fixture-v1")
    store = WorkerAPIStore(os.environ["CAF_WORKER_API_URL"], os.environ["CAF_WORKER_TOKEN"])
    result = asyncio.run(run_one_flow_fixture(
        store, os.environ["CAF_WORKER_ID"], binding,
        _NoCodingRuntime(), ClaudeRuntime(timeout_seconds=120, tools=(),
                                         structured_schemas={"plan": PLAN_SCHEMA}),
        stop_after_plan=True,
    ))
    print("empty" if result is None else
          f"{result.get('execution_kind')}: {result['status']}")
    return 0 if result is None or result["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Persistent, test-database-only synthetic CAF worker."""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path
from uuid import uuid4

from caf_control_api.storage import PostgresStore

from .bounded_worker import run_one_synthetic


async def serve(
    store: PostgresStore, workspace: Path, stop: asyncio.Event,
    heartbeat: Path, *, poll_seconds: float = 2,
) -> None:
    if not 0 < poll_seconds <= 60:
        raise ValueError("poll_seconds must be between 0 and 60")
    # Verify identity before starting a persistent polling loop.
    if not (await asyncio.to_thread(store.database_name)).startswith("caf_queue_test_"):
        raise ValueError("synthetic daemon requires a caf_queue_test_* database")
    owner = f"worker-{uuid4().hex}"
    while not stop.is_set():
        await run_one_synthetic(store, owner, workspace)
        heartbeat.write_text("ready\n", encoding="utf-8")
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
        except TimeoutError:
            pass


def main() -> None:
    store = PostgresStore(os.environ["CAF_QUEUE_TEST_DATABASE_URL"])
    store.open()
    try:
        async def run() -> None:
            loop = asyncio.get_running_loop()
            stop = asyncio.Event()
            for name in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(name, stop.set)
                except NotImplementedError:
                    pass
            await serve(store, Path("/workspace"), stop,
                        Path("/tmp/caf-synthetic-worker-heartbeat"))

        asyncio.run(run())
    finally:
        store.close()


if __name__ == "__main__":
    main()

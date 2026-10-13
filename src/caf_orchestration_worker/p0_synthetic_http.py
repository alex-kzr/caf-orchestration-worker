"""One opt-in synthetic HTTP claim; no CLI or persistent credential store."""

import asyncio
import os
from pathlib import Path

from .api_store import WorkerAPIStore
from .bounded_worker import run_one_synthetic


def main() -> None:
    store = WorkerAPIStore(os.environ["CAF_WORKER_API_URL"],
                           os.environ["CAF_WORKER_TOKEN"])
    workspace = Path(os.environ["CAF_SYNTHETIC_WORKSPACE"]).resolve(strict=True)
    asyncio.run(run_one_synthetic(store, os.environ["CAF_WORKER_ID"], workspace))


if __name__ == "__main__":
    main()

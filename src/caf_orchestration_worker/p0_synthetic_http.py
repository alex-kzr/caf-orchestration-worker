"""One opt-in synthetic HTTP claim; no CLI or persistent credential store."""

import asyncio
import os
from pathlib import Path

from .api_store import WorkerAPIStore
from .bounded_worker import run_one_synthetic
from .workspace_binding import resolve_sample_workspace


def main() -> None:
    store = WorkerAPIStore(os.environ["CAF_WORKER_API_URL"],
                           os.environ["CAF_WORKER_TOKEN"])
    binding = resolve_sample_workspace(Path(os.environ["CAF_UMBRELLA_ROOT"]),
                                       "caf-synthetic-v1")
    asyncio.run(run_one_synthetic(store, os.environ["CAF_WORKER_ID"],
                                  binding.repository, binding=binding))


if __name__ == "__main__":
    main()

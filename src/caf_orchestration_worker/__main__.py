"""Explicit synthetic baseline demo; no CLI, Git, or CrewAI integration."""

from __future__ import annotations

import argparse
import asyncio
import os
from uuid import uuid4
from pathlib import Path

from caf_agent_runtime import SyntheticOutcome, SyntheticRuntime

from .workflow import RunRequest, run_baseline, write_artifact


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CAF worker experiments")
    subcommands = parser.add_subparsers(dest="command", required=True)
    demo = subcommands.add_parser("synthetic-demo", help="run a deterministic in-process workflow")
    demo.add_argument("--output", type=Path, required=True)
    demo.add_argument("--base-revision", help="record the benchmark starting revision")
    demo.add_argument("--mode", choices=("baseline", "flow"), default="baseline")
    bounded = subcommands.add_parser("queue-synthetic-once", help="claim at most one isolated test Run")
    bounded.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "queue-synthetic-once":
        from caf_control_api.storage import PostgresStore

        from .bounded_worker import run_one_synthetic

        store = PostgresStore(os.environ["CAF_QUEUE_TEST_DATABASE_URL"])
        store.open()
        try:
            artifact = asyncio.run(run_one_synthetic(
                store, f"worker-{uuid4().hex}", args.workspace,
            ))
        finally:
            store.close()
        print("empty" if artifact is None else f"{artifact['execution_kind']}: {artifact['status']}")
        return 0 if artifact is None or artifact["status"] == "succeeded" else 1
    output: Path = args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    runtime = SyntheticRuntime({
        "review": [
            SyntheticOutcome(output={"approved": False, "findings": ["synthetic finding"]}),
            SyntheticOutcome(output={"approved": True, "findings": []}),
        ],
        "test": [SyntheticOutcome(output={"passed": True, "checks": ["synthetic gate"]})],
    })
    request = RunRequest("synthetic-demo", "synthetic-demo-correlation", "Synthetic fixture", "synthetic", base_revision=args.base_revision)
    if args.mode == "flow":
        from .flow_mode import run_flow

        artifact = asyncio.run(run_flow(request, runtime, output.parent))
    else:
        artifact = asyncio.run(run_baseline(request, runtime, output.parent))
    write_artifact(artifact, output)
    print(f"{artifact.status}: {output}")
    return 0 if artifact.status == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())

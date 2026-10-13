"""Explicit synthetic baseline demo; no CLI, Git, or CrewAI integration."""

from __future__ import annotations

import argparse
import asyncio
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
    args = parser.parse_args(argv)
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

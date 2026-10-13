# caf-orchestration-worker

CAF's provider-neutral `asyncio` workflow baseline. The current executable slice uses the published `caf-agent-runtime` package and its deterministic `SyntheticRuntime`; it does **not** edit Git repositories, run Codex/Claude CLI, execute tests against code, or persist state to a database.

## Run the synthetic slice

From an umbrella checkout or a standalone worker clone:

```powershell
uv run --python 3.12 pytest -q
uv run --python 3.12 python -m caf_orchestration_worker synthetic-demo --output $env:TEMP\caf-worker-demo.json --base-revision <fixture-commit>
```

The demo emits a JSON run artifact with correlation ID, stage outcomes, timestamps, ordered runtime events, one rejected review, one correction and a passing synthetic gate. The output path is explicit and should be outside the source tree. The fixture is deterministic in its outcomes and stage order; timestamps vary by run.
`--base-revision` records a supplied starting commit for comparison; this synthetic mode does not check out or verify that commit.

`[tool.uv.sources]` resolves `caf-agent-runtime==0.1.0` from `https://github.com/alex-kzr/caf-agent-runtime` at commit `227e0cc0166fcf3c9bbdc8894ba483065a49836f`. `uv.lock` records the same exact revision. A fresh checkout needs network access to fetch this Git dependency unless uv already has it cached. The umbrella's adjacent runtime submodule remains useful for development, but the worker does not import from or install that sibling directory.

## Contract and boundaries

`run_baseline(request, runtime, workspace)` calls the shared `CodingAgentRuntime` protocol for `plan`, `implement`, `review`, `correct` and `test`. The synthetic runtime performs no filesystem mutation. Each stage gets a unique task ID and the same correlation ID. Review results require a boolean `output.approved`; test results require a boolean `output.passed`. Missing verdicts block the run. A failed review or test can trigger a bounded correction and reevaluation. Runtime failures are recorded as blocked with their failure class.
Correction tasks receive the structured review or test evidence. Explicit quality-gate failures can be corrected; infrastructure, agent and timeout failures block without pretending a code change can repair them. Stage artifacts retain runtime exit code, session ID, changed files, commits and usage when available. Unexpected runtime exceptions become terminal infrastructure records containing only the exception type, because exception text could expose private data.

The JSON artifact is an experiment record, not a durable audit store. The current workflow is sequential: no task DAG, worktrees, integration branch, cancellation propagation, restart recovery, API transport, CLI execution, CrewAI mode or real quality gate has been implemented here. The target lifecycle and evaluation criteria remain in the umbrella `SPEC.md` and `docs/`.

## Next validation steps

1. Run the same request and fixture through an actual CrewAI Flow with a pinned, tested version and a verified model configuration. No CrewAI package or API key is required for the current baseline.
2. Add real, separately verified CLI adapters, workspace isolation and Git integration at the runtime boundary.
3. Compare Flow and baseline on identical worktrees, code tests and metrics; persist artifacts/events durably before claiming recovery.

**CrewAI availability check (2026-10-13):** `importlib.util.find_spec("crewai")` returned `None` in both the system Python and the worker's Python 3.12 uv environment. A key-free Flow path has therefore not been exercised or claimed; integration remains pending installation and a tested configuration.

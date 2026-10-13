# caf-orchestration-worker

CAF's provider-neutral `asyncio` baseline (Mode C) and CrewAI Flow-to-runtime experiment (Mode B). Both use the published `caf-agent-runtime` package and its deterministic `SyntheticRuntime`; neither edits Git repositories, runs Codex/Claude CLI, or executes tests against code. A separate explicit queue proof can claim one PostgreSQL Run, pass it through the synthetic baseline and persist a labeled artifact; it is not a daemon or a real coding run.

## Run the synthetic slice

From an umbrella checkout or a standalone worker clone:

```powershell
uv run --python 3.12 pytest -q
uv run --python 3.12 python -m caf_orchestration_worker synthetic-demo --output $env:TEMP\caf-worker-demo.json --base-revision <fixture-commit>
$env:PYTHONIOENCODING = 'utf-8' # Windows PowerShell: CrewAI prints Unicode status symbols
uv run --extra flow --python 3.12 pytest -q
uv run --extra flow --python 3.12 python -m caf_orchestration_worker synthetic-demo --mode flow --output $env:TEMP\caf-flow-demo.json --base-revision <fixture-commit>
```

The demos emit the same JSON run artifact shape with correlation ID, stage outcomes, timestamps, ordered runtime events, one rejected review, one correction and a passing synthetic gate. `mode` distinguishes `asyncio-baseline` and `crewai-flow`. The output path is explicit and should be outside the source tree. The fixture is deterministic in its outcomes and stage order; timestamps vary by run.
`--base-revision` records a supplied starting commit for comparison; this synthetic mode does not check out or verify that commit.

`[tool.uv.sources]` resolves `caf-agent-runtime==0.1.0` at commit `6d6f78489f2ffa31f91ec172d275d14ab1c56c15` and `caf-control-api==0.1.0` at commit `39eae5d125707c67cdd60743c4d342fc984e79dd` from their published Git repositories. `uv.lock` records both exact revisions. A fresh checkout needs network access to fetch these Git dependencies unless uv has them cached. The umbrella's adjacent submodules remain useful for development, but the worker does not import from sibling directories. Host Codex runs require an explicit native executable path; neither the synthetic demo nor queue proof constructs a host CLI adapter.

## Contract and boundaries

`run_baseline(request, runtime, workspace)` calls the shared `CodingAgentRuntime` protocol for `plan`, `implement`, `review`, `correct` and `test`. The synthetic runtime performs no filesystem mutation. Each stage gets a unique task ID and the same correlation ID. Review results require a boolean `output.approved`; test results require a boolean `output.passed`. Missing verdicts block the run. A failed review or test can trigger a bounded correction and reevaluation. Runtime failures are recorded as blocked with their failure class.
`run_flow` uses CrewAI 1.15.27 `Flow`, `@start`, `@listen`, and `kickoff_async` to sequence plan, implementation and evaluation. It invokes the same `CodingAgentRuntime` and shared `WorkflowSession` policy as the baseline. The bounded review/correction/test loop remains within the evaluation listener, so this experiment does **not** yet test CrewAI routers, Crew delegation, Flow persistence or restart recovery. No CrewAI Agent or LLM is constructed. The `flow` extra isolates CrewAI's substantial dependency set from baseline-only installations.
Correction tasks receive the structured review or test evidence. Explicit quality-gate failures can be corrected; infrastructure, agent and timeout failures block without pretending a code change can repair them. Stage artifacts retain runtime exit code, session ID, changed files, commits and usage when available. Unexpected runtime exceptions become terminal infrastructure records containing only the exception type, because exception text could expose private data.

The standalone JSON artifact is an experiment record, not a durable audit store. The baseline/Flow workflow is sequential: it has no task DAG, worktrees, integration branch, cancellation propagation, restart recovery, API transport, CLI execution, CrewAI Agent mode or real quality gate. The separate queue runner below tests cancellation of an in-process executor task; it does not prove CLI process-tree cleanup. The target lifecycle and evaluation criteria remain in the umbrella `SPEC.md` and `docs/`.

## Explicit queue-to-synthetic proof

`run_synthetic_claim_once(store, owner, allowed_repositories)` accepts an opened `PostgresStore` or compatible queue store. Before claiming anything, it checks the **actual connected database name** and refuses to run unless it starts with `caf_queue_test_`; it cannot consume the shared `caf` queue. It claims at most one queued Run, checks the repository slug against the caller's mapping to an existing directory, runs only `SyntheticRuntime` through the baseline policy, labels the artifact `execution_kind: synthetic-queue-proof`, and conditionally persists `succeeded` or `failed` under the current lease owner. A lost lease raises `LostLeaseError`; an unknown repository is recorded as failed without runtime invocation. It does not verify a Git revision or modify a workspace. There is no polling daemon, heartbeat, orphan reconciliation, real CLI invocation or automatic startup in Compose. Synthetic success is not code acceptance.

Unit tests cover identity propagation, stage order, allowlist rejection and lost ownership. The opt-in `tests/test_queue_bridge_live.py` ran in a dedicated temporary `caf_queue_test_*` database and verified queue claim, worker handoff, synthetic stages and persisted JSON artifact against PostgreSQL. It checks the actual connected database before writing, deletes its own Task/Run, and requires an external harness to create/drop the isolated database. No task was run from the shared CAF queue.

## Lease-aware dispatch boundary

`queue_runner.dispatch_once` is the next, executor-neutral lifecycle slice. It quarantines expired claims without replay, claims at most one Run, polls the owner/cancel signal, renews the lease, cancels its executor task on request or lost ownership, and conditionally acknowledges one terminal artifact. Cleanup has a bounded wait and a lease margin; an unresponsive executor leaves the Run unacknowledged. Unit tests cover duplicate polling, cancellation before and during execution, lost ownership, ACK races and a stubborn executor. A dedicated PostgreSQL test covers one real claim/ACK and the empty second poll. This boundary is not wired into Compose or a host CLI process. Its executor must prove forced process-tree termination before it can run real coding tasks. An expired Run is marked `failed/orphan_unverified`, not automatically retried; the orphan may still exist.

## Next validation steps

1. Expand Mode B into explicit CrewAI branches and measure whether that improves the same fixture relative to baseline; test persistence and restart behavior separately.
2. Add real, separately verified CLI adapters, workspace isolation and Git integration at the runtime boundary.
3. Compare Flow and baseline on identical worktrees, code tests and metrics; persist artifacts/events durably before claiming recovery.

**Verified on 2026-10-13:** CrewAI `1.15.27` was installed as an optional extra on Python 3.12. The synthetic Flow parity tests passed with `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` and `GOOGLE_API_KEY` removed from their process environment. This proves the tested Flow path can run without those model credentials; it makes no claim about Mode A or Flow steps that construct CrewAI Agents. API-model cost in this synthetic scenario was not measured. CrewAI's [Flow docs](https://docs.crewai.com/en/concepts/flows) specify async listeners and `kickoff_async`; the [1.15.27 release](https://pypi.org/project/crewai/1.15.27/) supplies the pinned package.
In the tested Windows PowerShell session, CrewAI's console event handler emitted encoding errors for its Unicode status symbols with the default encoding; `PYTHONIOENCODING=utf-8` removed those errors. The workflow completed in both cases. This is a console behavior observation, not a readiness or deployment claim.

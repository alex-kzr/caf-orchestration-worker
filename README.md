# caf-orchestration-worker

CAF's provider-neutral `asyncio` baseline (Mode C) and CrewAI Flow-to-runtime experiment (Mode B). Their default demos use the pinned `caf-agent-runtime` package and its deterministic `SyntheticRuntime`; those demos do not edit Git repositories or run Codex/Claude. Separately, opt-in P0 fixture entrypoints use the sample repository, a scoped HTTP grant and real host CLI adapters. The Flow fixture has one measured plan-only Run; the new explicit-schema path has offline coverage only.

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

`[tool.uv.sources]` resolves `caf-agent-runtime==0.1.0` at commit `c25a190729140953f77c02d80b0d5abb60577863` and `caf-control-api==0.1.0` at commit `e46c34d8a49affcaab6f61c27178f99c4661345d` from their published Git repositories. `uv.lock` records both exact revisions. A fresh checkout needs network access to fetch these Git dependencies unless uv has them cached. The umbrella's adjacent submodules remain useful for development, but the worker does not import from sibling directories. Host Codex runs require an explicit native executable path; neither the synthetic demo nor queue proof constructs a host CLI adapter.

## Contract and boundaries

`run_baseline(request, runtime, workspace)` calls the shared `CodingAgentRuntime` protocol for `plan`, `implement`, `review`, `correct` and `test`. The synthetic runtime performs no filesystem mutation. Each stage gets a unique task ID and the same correlation ID. Review results require a boolean `output.approved`; test results require a boolean `output.passed`. Missing verdicts block the run. A failed review or test can trigger a bounded correction and reevaluation. Runtime failures are recorded as blocked with their failure class.
`run_flow` uses CrewAI 1.15.27 `Flow`, `@start`, `@listen`, and `kickoff_async` to sequence plan, implementation and evaluation. It invokes the same `CodingAgentRuntime` and shared `WorkflowSession` policy as the baseline. The bounded review/correction/test loop remains within the evaluation listener, so this experiment does **not** yet test CrewAI routers, Crew delegation, Flow persistence or restart recovery. No CrewAI Agent or LLM is constructed. The `flow` extra isolates CrewAI's substantial dependency set from baseline-only installations.
Correction tasks receive the structured review or test evidence. Explicit quality-gate failures can be corrected; infrastructure, agent and timeout failures block without pretending a code change can repair them. Stage artifacts retain runtime exit code, session ID, changed files, commits and usage when available. Unexpected runtime exceptions become terminal infrastructure records containing only the exception type, because exception text could expose private data.

The standalone JSON artifact is an experiment record, not a durable audit store. The baseline/Flow workflow is sequential: it has no task DAG, worktrees, integration branch, cancellation propagation, restart recovery, API transport, CLI execution, CrewAI Agent mode or real quality gate. The separate queue runner below tests cancellation of an in-process executor task; it does not prove CLI process-tree cleanup. The target lifecycle and evaluation criteria remain in the umbrella `SPEC.md` and `docs/`.

## Explicit queue-to-synthetic proof

`run_synthetic_claim_once(store, owner, allowed_repositories)` accepts an opened `PostgresStore` or compatible queue store. Before claiming anything, it checks the **actual connected database name** and refuses to run unless it starts with `caf_queue_test_`; it cannot consume the shared `caf` queue. It claims at most one queued Run, checks the repository slug against the caller's mapping to an existing directory, runs only `SyntheticRuntime` through the baseline policy, labels the artifact `execution_kind: synthetic-queue-proof`, and conditionally persists `succeeded` or `failed` under the current lease owner. A lost lease raises `LostLeaseError`; an unknown repository is recorded as failed without runtime invocation. It does not verify a Git revision or modify a workspace. The separate `p0-test-daemon` Compose profile now runs a persistent synthetic polling loop with heartbeat and expired-lease quarantine against dedicated test PostgreSQL. This one-shot function still does not invoke a real CLI. Synthetic success is not code acceptance.

Unit tests cover identity propagation, stage order, allowlist rejection and lost ownership. The opt-in `tests/test_queue_bridge_live.py` ran in a dedicated temporary `caf_queue_test_*` database and verified queue claim, worker handoff, synthetic stages and persisted JSON artifact against PostgreSQL. It checks the actual connected database before writing, deletes its own Task/Run, and requires an external harness to create/drop the isolated database. No task was run from the shared CAF queue.

## Lease-aware dispatch boundary

`queue_runner.dispatch_once` is the next, executor-neutral lifecycle slice. It quarantines expired claims without replay, claims at most one Run, polls the owner/cancel signal, renews the lease, cancels its executor task on request or lost ownership, and conditionally acknowledges one terminal artifact. Cleanup has a bounded wait and a lease margin; an unresponsive executor or `ProcessTreeCleanupError` leaves the Run unacknowledged. Unit tests cover duplicate polling, cancellation before and during execution, lost ownership, ACK races, a stubborn executor and unverified process cleanup. One bounded test passes a fake queue claim through dispatch into the actual process runtime using a local Python fixture; it does not use the coding CLIs or PostgreSQL. A separate dedicated PostgreSQL test covers one real claim/ACK and the empty second poll. This boundary is not wired into Compose or a host CLI process. Its executor must prove forced process-tree termination before it can run real coding tasks. An expired Run is marked `failed/orphan_unverified`, not automatically retried; the orphan may still exist.

`python -m caf_orchestration_worker queue-synthetic-once --workspace /workspace`
is a one-claim container entrypoint built on that same dispatcher. It verifies
the **connected** database starts with `caf_queue_test_` before claiming, allows
only the sample repository slug, uses `SyntheticRuntime`, and stores a labeled
artifact. The umbrella's `p0-test` Compose profile runs it as a one-shot CAF
service against a separately created test database. It cannot run coding CLIs
and must not be treated as the production coding daemon.

`python -m caf_orchestration_worker.p0_code_worker` is an opt-in, single-Run
host experiment for the discount-code fixture. It requires
`CAF_UMBRELLA_ROOT`, `CAF_WORKER_API_URL`, `CAF_WORKER_TOKEN`,
`CAF_WORKER_ID` and `CAF_CODEX_EXECUTABLE`. The entrypoint no longer falls
back to direct PostgreSQL. It maps `caf-sample-fixture-v1` to the exact
`repositories/caf-sample-app` checkout under the supplied CAF umbrella,
rejects redirected checkouts and temporary roots, and creates a private
`caf-p0-*` worktree directory under the host temporary directory for each
invocation. It checks the connected test database,
exact fixture prompt, sample base SHA and clean checkout before claiming. It
creates an external worktree, permits only the fixture
implementation file, invokes
Codex once, runs both deterministic acceptance suites, asks Claude for a
structured no-tools review, then stores the diff, verdict, gate results and
local commit SHA in the Run artifact. The commit stays on a local branch; no
push or merge occurs. An unverified process-tree cleanup leaves the claim
unacknowledged and preserves the worktree for investigation. This fixture path
is not a general production worker or a persistent daemon.

### One-Run Flow fixture

`python -m caf_orchestration_worker.p0_flow_worker` is a separate opt-in
single-Run Flow fixture. It uses the same exact sample workspace and isolated
`caf_queue_test_*` database boundary. Within one claimed Run, CrewAI Flow
sequences a structured no-tools Claude plan, Codex implementation, structured
no-tools Claude review, at most `max_corrections` Codex corrections with renewed
review, and deterministic acceptance suites. The plan steps are passed to the
implementation stage. Only the sample implementation file may change; a
successful result records a local commit and bounded stage evidence. Offline
fake-CLI tests exercised a rejected review followed by one correction and
passing gates inside a single Flow Run, plus zero-correction rejection and
pre-claim scope checks. One approved real-CLI/PostgreSQL Run reached the plan
stage but ended `failed/plan_agent`; no code, review, tests or real correction
were executed. See the umbrella's [live Flow report](../../docs/experiments/2026-10-14-one-run-flow-gate.md).
The plan parser now accepts plain JSON or one JSON code fence; whether that
caused the live failure is unknown. This entrypoint does not activate a
persistent worker or broaden the HTTP grant.

The Flow fixture now stores a small `diagnostic` object for plan and review
stages: phase, correlation ID, fixed category, provider status/failure class,
exit code, captured response byte count, input format and
parser outcome. Categories distinguish process exit, timeout, authentication
or limit signal, transport signal, empty output, invalid JSON and invalid
schema. These are classifications of observed CLI metadata, not a copy of
stdout/stderr or a claim that all provider errors are recognized. The parser
accepts only a complete JSON object or one complete JSON code fence; extra
prose and extra schema fields fail closed. The response byte count measures
the runtime's redacted, capped capture, not the provider's original output.
Synthetic offline fixtures cover both supported formats and negative cases.
For plan operations, the fixture now also supplies the same strict schema to
Claude through `--json-schema`; it reads the `structured_output` envelope field
and still validates the result locally. The safe diagnostic adds a response
source and a fixed schema error code/keyword, path limited to `steps`, type
names, and an unknown-field count. Provider keys, values and response text do
not enter that diagnostic. This new CLI combination is offline tested only.

### Plan-only Flow diagnostic

`python -m caf_orchestration_worker.p0_plan_worker` is an opt-in, one-Run
diagnostic for the exact sample fixture. It requires `CAF_UMBRELLA_ROOT`,
`CAF_WORKER_API_URL`, `CAF_WORKER_TOKEN` and `CAF_WORKER_ID`, and accepts only a
claim bound to provider `claude`, the pinned sample base and prompt, and zero
corrections in an isolated `caf_queue_test_*` database. It creates a temporary
worktree outside the submodule, calls Claude with tools disabled for the
structured plan, and terminates the Flow after that stage. This entrypoint
does not construct a Codex runtime. A guard rejects any accidental downstream
stage before a provider call or local test gate, and the result is blocked if
any additional stage was recorded or
the worktree changed. The terminal artifact includes a bounded
`terminal_diagnostic` with `plan_valid`, `plan_failed`, `stage_violation` or
`workspace_modified` and safe category/parser fields. It contains no diff,
commit, review or gate fields. Success means that the plan parsed; it is not
P0 acceptance or a claim about implementation quality. Offline tests cover
both terminal outcomes and the absence of downstream calls. This entrypoint
completed one real Claude CLI/PostgreSQL Run before the explicit schema was
added. That Run failed during `plan` with category `schema` and parser outcome
`invalid_schema`; it ran no downstream stage. The category does not identify
the rejected field or value. See the [plan-only gate](../../docs/experiments/2026-10-14-plan-only-schema-gate.md).

For an envelope rejected by the runtime, the plan parser now reports
`not_run`, and the terminal diagnostic carries only the allowlisted envelope
reason and type categories. An exit-zero provider result with a rejected
envelope is categorized as `envelope`, not `empty_output`. Offline fixture
tests cover this path; the previous live response was not retained. See the
[envelope diagnostic gate](../../docs/experiments/2026-10-14-envelope-diagnostic-gate.md).
The worker lockfile pins runtime commit `1aac05c60db738dd51f95644490fba11e6e6c269`;
a fresh Git-backed install requires that commit to be published first.
The worker allowlists the runtime's bounded root-array shape fields when an
array is rejected. It does not interpret an array as a successful plan.

The earlier direct-database experiment used
`CAF_REVIEW_SOURCE_RUN=<UUID>` to reapply a tested diff from a failed Run.
The current one-Run HTTP grant cannot read a different source Run, so that
continuation is unavailable through the new entrypoint and explicitly fails
closed before a foreign read.
No automatic replay is enabled.

`WorkerAPIStore` is a narrow alternative queue transport for a host worker.
It accepts only an explicit `http://127.0.0.1:<port>` control API, disables
proxies and redirects, uses a distinct scoped `CAF_WORKER_TOKEN` and never sends the token
in a URL. The host fixture requires this transport and verifies the connected
database begins with `caf_queue_test_` before any claim. One approved temporary token was used for
a single synthetic HTTP Run and then removed; no persistent host API worker
is started by normal commands.

The API grant is now bound to one Run/task, repository, workspace ID, base
revision, provider, worker identity, exact test database and an expiry no
later than one hour after configuration. The fixture accepts HTTP claims only
for workspace ID `caf-sample-fixture-v1`; its repository is derived from the
CAF umbrella path and its private worktree root from the host temp directory. CLI, Git and acceptance test
subprocesses receive a minimal environment containing OS lookup and standard
provider config paths, not the worker token or model API keys. Real CLI login
has not been retested after this environment change. The new
`p0_synthetic_http` entrypoint is an opt-in **one-shot synthetic** HTTP client
for workspace ID `caf-synthetic-v1`; it requires `CAF_UMBRELLA_ROOT` and maps
the ID to the same checked CAF sample checkout. It invokes no coding CLI.

An offline Windows test used a local dummy-token HTTP API, a separate worker
process and a disposable Python child in the real runtime adapter. Killing
only the worker stopped the child through Job Object closure; after test-only
lease expiry, the API-side state was quarantined and no claim replay occurred.
The API state in that test was in-memory, not PostgreSQL; the earlier live
test-database daemon proof covers a separate persistence boundary.

`caf_orchestration_worker.synthetic_daemon` is a persistent **test-only**
polling loop. The umbrella's `p0-test-daemon` profile gives it an isolated
test PostgreSQL database, no CLI credentials, one claim at a time and a
heartbeat healthcheck. An exact prompt `CAF_SYNTHETIC_DELAY_3_SECONDS` adds
only a three-second synthetic delay for observing lease renewal.

## Next validation steps

1. Expand Mode B into explicit CrewAI branches and measure whether that improves the same fixture relative to baseline; test persistence and restart behavior separately.
2. Add real, separately verified CLI adapters, workspace isolation and Git integration at the runtime boundary.
3. Compare Flow and baseline on identical worktrees, code tests and metrics; persist artifacts/events durably before claiming recovery.

**Verified on 2026-10-13:** CrewAI `1.15.27` was installed as an optional extra on Python 3.12. The synthetic Flow parity tests passed with `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` and `GOOGLE_API_KEY` removed from their process environment. This proves the tested Flow path can run without those model credentials; it makes no claim about Mode A or Flow steps that construct CrewAI Agents. API-model cost in this synthetic scenario was not measured. CrewAI's [Flow docs](https://docs.crewai.com/en/concepts/flows) specify async listeners and `kickoff_async`; the [1.15.27 release](https://pypi.org/project/crewai/1.15.27/) supplies the pinned package.
In the tested Windows PowerShell session, CrewAI's console event handler emitted encoding errors for its Unicode status symbols with the default encoding; `PYTHONIOENCODING=utf-8` removed those errors. The workflow completed in both cases. This is a console behavior observation, not a readiness or deployment claim.

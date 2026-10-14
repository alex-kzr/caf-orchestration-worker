"""Opt-in one-Run host CrewAI Flow fixture over the exact worker HTTP grant.

This is a bounded P0 entrypoint, not an always-on production worker.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from caf_agent_runtime import ClaudeRuntime, CodexRuntime, CodingAgentRuntime
from caf_agent_runtime.windows_job import ProcessTreeCleanupError
from caf_control_api.storage import ClaimedRun

from .api_store import WorkerAPIStore
from .flow_mode import run_flow, run_plan_only_flow
from .p0_code_worker import ALLOWED_FILE, BASE_REVISION, FIXTURE_PROMPT, _git
from .p0_flow_fixture import FIXTURE_PROVIDERS, PLAN_SCHEMA, FixtureStageRuntime
from .queue_runner import dispatch_once
from .workflow import RunRequest
from .workspace_binding import WorkspaceBinding, resolve_sample_workspace


async def run_one_flow_fixture(
    store: WorkerAPIStore, owner: str, binding: WorkspaceBinding,
    codex: CodingAgentRuntime, claude: CodingAgentRuntime,
    *, stop_after_plan: bool = False,
) -> dict | None:
    """Claim one sample Run; optionally terminate after the diagnostic plan."""
    if (binding.workspace_id != "caf-sample-fixture-v1"
            or binding.repository_slug != "caf-sample-app"
            or binding.base_revision != BASE_REVISION
            or binding.worktree_root is None):
        raise ValueError("Flow fixture requires the exact CAF sample binding")
    database = await asyncio.to_thread(store.database_name)
    if not database.startswith("caf_queue_test_"):
        raise ValueError("Flow fixture requires an isolated test database")
    repository = binding.repository
    umbrella_root = repository.parent.parent.resolve(strict=True)
    temp_root = binding.worktree_root.resolve(strict=True)
    if (binding.worktree_root.is_symlink() or binding.worktree_root.is_junction()
            or temp_root.is_relative_to(umbrella_root)
            or _git(repository, "rev-parse", "HEAD") != BASE_REVISION
            or _git(repository, "status", "--porcelain")):
        raise ValueError("Flow fixture requires clean sample base and external worktree root")
    private_root = Path(tempfile.mkdtemp(prefix="caf-p0-flow-", dir=temp_root))
    if (private_root.resolve(strict=True).parent != temp_root
            or private_root.is_relative_to(umbrella_root)):
        raise ValueError("private Flow worktree root was redirected")

    async def execute(claim: ClaimedRun) -> dict:
        if (claim.workspace_id != binding.workspace_id
                or claim.target_repository != binding.repository_slug
                or claim.base_revision != binding.base_revision
                or claim.provider != ("claude" if stop_after_plan else "codex")
                or claim.prompt != FIXTURE_PROMPT
                or (claim.max_corrections != 0 if stop_after_plan
                    else not 0 <= claim.max_corrections <= 3)):
            return {"status": "blocked", "reason": "fixture_claim_mismatch"}
        branch = ("caf-plan-probe-" if stop_after_plan else "caf-flow-experiment-") + claim.id.hex[:12]
        workspace = private_root / str(claim.id)
        _git(repository, "worktree", "add", "-b", branch, str(workspace), BASE_REVISION)
        cleanup_unverified = False
        try:
            runtime = FixtureStageRuntime(codex, claude)
            request = RunRequest(str(claim.id), str(claim.correlation_id),
                                 claim.prompt, claim.provider, claim.max_corrections,
                                 claim.base_revision)
            flow = await (run_plan_only_flow if stop_after_plan else run_flow)(
                request, runtime, workspace, FIXTURE_PROVIDERS)
            artifact: dict = {
                "execution_kind": ("p0-queued-plan-only-fixture" if stop_after_plan
                                   else "p0-queued-flow-fixture"),
                "status": "blocked" if flow.status != "succeeded" else "succeeded",
                "workflow_status": flow.status,
                "reason": flow.reason,
                "run_id": str(claim.id),
                "correlation_id": str(claim.correlation_id),
                "base_revision": BASE_REVISION,
                "branch": branch,
                "iterations": flow.iterations,
                "started_at": flow.started_at,
                "finished_at": flow.ended_at,
                "stages": [{"operation": stage.operation, "status": stage.status,
                            "failure_class": stage.failure_class,
                            "diagnostic": (stage.output.get("diagnostic")
                                           if stage.operation in ("plan", "review") else None),
                            "started_at": stage.started_at, "ended_at": stage.ended_at}
                           for stage in flow.stages],
            }
            if stop_after_plan:
                plan = flow.stages[0] if flow.stages else None
                diagnostic = plan.output.get("diagnostic") if plan is not None else None
                artifact["terminal_diagnostic"] = {
                    "phase": "plan", "result": ("plan_valid" if flow.status == "succeeded"
                                                else "plan_failed"),
                    "category": diagnostic.get("category") if isinstance(diagnostic, dict) else None,
                    "parser_outcome": (diagnostic.get("parser_outcome")
                                       if isinstance(diagnostic, dict) else None),
                    "response_source": (diagnostic.get("response_source")
                                        if isinstance(diagnostic, dict) else None),
                    "schema_error": (diagnostic.get("schema_error")
                                     if isinstance(diagnostic, dict) else None),
                    "stage_operations": [stage.operation for stage in flow.stages],
                }
                if artifact["terminal_diagnostic"]["stage_operations"] != ["plan"]:
                    artifact["status"] = "blocked"
                    artifact["reason"] = "plan_only_stage_violation"
                    artifact["terminal_diagnostic"]["result"] = "stage_violation"
                if _git(workspace, "status", "--porcelain"):
                    artifact["status"] = "blocked"
                    artifact["reason"] = "plan_only_workspace_modified"
                    artifact["terminal_diagnostic"]["result"] = "workspace_modified"
                return artifact
            reviews = [stage.output for stage in flow.stages if stage.operation == "review"]
            gates = [stage.output.get("gates") for stage in flow.stages if stage.operation == "test"]
            if reviews:
                artifact["review"] = reviews[-1]
            if gates:
                artifact["gates"] = gates[-1]
            if flow.status != "succeeded":
                return artifact
            diff = runtime._changed_diff(workspace)
            artifact["diff"] = diff
            _git(workspace, "add", "--", ALLOWED_FILE)
            _git(workspace, "commit", "-m", "Implement CAF sample through one Flow Run")
            artifact["commit"] = _git(workspace, "rev-parse", "HEAD")
            return artifact
        except ProcessTreeCleanupError:
            cleanup_unverified = True
            raise
        finally:
            if not cleanup_unverified:
                _git(repository, "worktree", "remove", "--force", str(workspace))

    try:
        return await dispatch_once(store, owner, execute,
                                   lease_seconds=600, tick_seconds=5, cleanup_seconds=15)
    finally:
        try:
            private_root.rmdir()
        except OSError:
            pass  # Preserve anything whose cleanup was not verified.


def main() -> int:
    binding = resolve_sample_workspace(Path(os.environ["CAF_UMBRELLA_ROOT"]),
                                       "caf-sample-fixture-v1")
    codex_executable = Path(os.environ["CAF_CODEX_EXECUTABLE"])
    if not codex_executable.is_absolute() or not codex_executable.is_file():
        raise ValueError("native Codex executable must exist at an absolute path")
    store = WorkerAPIStore(os.environ["CAF_WORKER_API_URL"], os.environ["CAF_WORKER_TOKEN"])
    result = asyncio.run(run_one_flow_fixture(
        store, os.environ["CAF_WORKER_ID"], binding,
        CodexRuntime(executable=codex_executable, timeout_seconds=180,
                     sandbox="workspace-write"),
        ClaudeRuntime(timeout_seconds=120, tools=(),
                      structured_schemas={"plan": PLAN_SCHEMA}),
    ))
    print("empty" if result is None else f"{result.get('execution_kind')}: {result['status']}")
    return 0 if result is None or result["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())

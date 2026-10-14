"""Bounded, opt-in real CLI fixture through one isolated CAF queue Run.

This is an experiment entrypoint, not an always-on production worker.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
from pathlib import Path
from uuid import UUID

from caf_agent_runtime import AgentStatus, ClaudeRuntime, CodexRuntime, CodingTask, child_environment
from caf_agent_runtime.windows_job import ProcessTreeCleanupError
from caf_control_api.storage import ClaimedRun, PostgresStore

from .api_store import WorkerAPIStore
from .queue_runner import dispatch_once
from .workspace_binding import WorkspaceBinding, SAMPLE_BASE_REVISION, resolve_sample_workspace


BASE_REVISION = SAMPLE_BASE_REVISION
ALLOWED_FILE = "src/caf_sample/orders.py"
FIXTURE_PROMPT = (
    "Add an optional discount_code: str | None = None argument to "
    "caf_sample.quote_order. Support SAVE10 in any letter case. Deduct 10% "
    "of merchandise subtotal, rounded to nearest whole cent with halves "
    "rounded up. Compute shipping from original subtotal. Unknown, empty, "
    "or whitespace-only codes raise ValueError. None preserves behavior. "
    "Return discount in Quote.discount_cents and final total. Keep item "
    "validation and existing behavior. Do not edit acceptance tests."
)


def _review_verdict(value: object) -> dict | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if stripped.startswith("```json") and stripped.endswith("```"):
        stripped = stripped[7:-3].strip()
    elif stripped.startswith("```") and stripped.endswith("```"):
        stripped = stripped[3:-3].strip()
    try:
        verdict = json.loads(stripped)
    except json.JSONDecodeError:
        start, end = stripped.find("{"), stripped.rfind("}")
        if start < 0 or end < start:
            return None
        try:
            verdict = json.loads(stripped[start:end + 1])
        except json.JSONDecodeError:
            return None
    if (not isinstance(verdict, dict)
            or not isinstance(verdict.get("approved"), bool)
            or not isinstance(verdict.get("findings"), list)
            or any(not isinstance(item, str) for item in verdict["findings"])):
        return None
    return {"approved": verdict["approved"], "findings": verdict["findings"]}


def _git(repository: Path, *args: str, timeout: int = 20) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *args], check=True,
        capture_output=True, text=True, timeout=timeout, env=child_environment(),
    ).stdout.rstrip("\r\n")


def _gate(workspace: Path, filename: str) -> dict:
    env = {**child_environment(), "PYTHONPATH": str(workspace / "src"),
           "PYTHONDONTWRITEBYTECODE": "1"}
    completed = subprocess.run(
        ["python", "-m", "unittest", "discover", "-s", "tests", "-p", filename],
        cwd=workspace, env=env, capture_output=True, text=True, timeout=30,
    )
    return {"suite": filename, "passed": completed.returncode == 0,
            "exit_code": completed.returncode}


async def run_one_code_fixture(
    store: PostgresStore | WorkerAPIStore, owner: str, binding: WorkspaceBinding,
    codex_executable: Path,
) -> dict | None:
    """Claim once from a test DB and persist diff, gates and Claude verdict."""
    if (binding.workspace_id != "caf-sample-fixture-v1"
            or binding.repository_slug != "caf-sample-app"
            or binding.base_revision != BASE_REVISION
            or binding.worktree_root is None):
        raise ValueError("code fixture requires the exact CAF sample binding")
    repository, worktree_root = binding.repository, binding.worktree_root
    database_name = await asyncio.to_thread(store.database_name)
    if not database_name.startswith("caf_queue_test_"):
        raise ValueError("code fixture requires a caf_queue_test_* database")
    umbrella_root = repository.parent.parent.resolve(strict=True)
    resolved_temp_root = worktree_root.resolve(strict=True)
    if (worktree_root.is_symlink() or worktree_root.is_junction()
            or not resolved_temp_root.is_dir()
            or resolved_temp_root.is_relative_to(umbrella_root)):
        raise ValueError("worktrees must be outside the CAF checkout")
    if _git(repository, "rev-parse", "HEAD") != BASE_REVISION:
        raise ValueError("sample repository must be checked out at the fixture base")
    if _git(repository, "status", "--porcelain"):
        raise ValueError("sample repository must be clean")
    if not codex_executable.is_absolute() or not codex_executable.is_file():
        raise ValueError("Codex native executable must exist at an absolute path")
    private_root = Path(tempfile.mkdtemp(prefix="caf-p0-", dir=resolved_temp_root))
    if (private_root.is_symlink() or private_root.is_junction()
            or private_root.resolve(strict=True).parent != resolved_temp_root
            or private_root.is_relative_to(umbrella_root)
            or umbrella_root.is_relative_to(private_root)):
        raise ValueError("private worktree root was redirected")

    async def execute(claim: ClaimedRun) -> dict:
        if claim.workspace_id != binding.workspace_id:
            return {"status": "blocked", "reason": "workspace_scope_mismatch"}
        if (claim.target_repository != binding.repository_slug or claim.provider != "codex"
                or claim.base_revision != binding.base_revision or claim.max_corrections != 0):
            return {"status": "blocked", "reason": "fixture_claim_mismatch"}
        if not (claim.prompt == FIXTURE_PROMPT
                or claim.prompt.startswith("CAF_REVIEW_SOURCE_RUN=")):
            return {"status": "blocked", "reason": "fixture_prompt_mismatch"}
        branch = f"caf-queue-experiment-{claim.id.hex[:12]}"
        workspace = private_root / str(claim.id)
        source_run_id: UUID | None = None
        source_diff: str | None = None
        if claim.prompt.startswith("CAF_REVIEW_SOURCE_RUN="):
            if isinstance(store, WorkerAPIStore):
                return {"status": "blocked", "reason": "source_run_out_of_scope"}
            source_run_id = UUID(claim.prompt.removeprefix("CAF_REVIEW_SOURCE_RUN="))
            source = await asyncio.to_thread(store.get_run, source_run_id)
            source_artifact = source.artifact if source is not None else None
            if (source is None or source.status != "failed"
                    or not isinstance(source_artifact, dict)
                    or source_artifact.get("execution_kind") != "p0-queued-code-fixture"
                    or source_artifact.get("base_revision") != BASE_REVISION
                    or not isinstance(source_artifact.get("diff"), str)
                    or not 0 < len(source_artifact["diff"].encode("utf-8")) <= 30_000):
                return {"status": "blocked", "reason": "invalid_source_run"}
            source_diff = source_artifact["diff"]
        _git(repository, "worktree", "add", "-b", branch, str(workspace), BASE_REVISION)
        cleanup_unverified = False
        try:
            artifact: dict = {
                "execution_kind": "p0-queued-code-fixture", "status": "blocked",
                "run_id": str(claim.id), "correlation_id": str(claim.correlation_id),
                "base_revision": BASE_REVISION, "branch": branch,
            }
            if source_diff is not None:
                applied = subprocess.run(
                    ["git", "-C", str(workspace), "apply", "--", "-"],
                    input=source_diff + "\n", capture_output=True, text=True,
                    timeout=20, env=child_environment(),
                )
                if applied.returncode != 0:
                    artifact["reason"] = "source_diff_failed_to_apply"
                    return artifact
                artifact["source_run_id"] = str(source_run_id)
                artifact["implementation"] = {"status": "reused_verified_diff"}
            else:
                codex = CodexRuntime(executable=codex_executable, timeout_seconds=180,
                                     sandbox="workspace-write")
                implemented = await codex.execute(CodingTask(
                    f"{claim.id}:implement", str(claim.correlation_id),
                    claim.prompt + "\nEdit only src/caf_sample/orders.py. "
                    "Do not modify acceptance tests, commit, push, or access other repositories.",
                    "codex", "implement",
                ), workspace)
                artifact["implementation"] = {"status": implemented.status.value,
                                              "exit_code": implemented.exit_code}
                if implemented.status != AgentStatus.SUCCEEDED:
                    artifact["reason"] = "implementation_failed"
                    return artifact
            changed = _git(workspace, "status", "--porcelain").splitlines()
            artifact["changed_files"] = changed
            if changed != [f" M {ALLOWED_FILE}"]:
                artifact["reason"] = "unexpected_changed_files"
                artifact["changed_count"] = len(changed)
                return artifact
            diff = _git(workspace, "diff", "--", ALLOWED_FILE)
            if not diff or len(diff.encode("utf-8")) > 30_000:
                artifact["reason"] = "missing_or_oversized_diff"
                return artifact
            artifact["diff"] = diff
            gates = [_gate(workspace, name) for name in
                     ("test_baseline.py", "test_discount_code.py")]
            artifact["gates"] = gates
            if not all(gate["passed"] for gate in gates):
                artifact["reason"] = "acceptance_failed"
                return artifact
            reviewer = ClaudeRuntime(timeout_seconds=120, tools=())
            reviewed = await reviewer.execute(CodingTask(
                f"{claim.id}:review", str(claim.correlation_id),
                "Review this CAF sample implementation diff. Requirements: "
                "quote_order gains optional discount_code: str | None = None; "
                "SAVE10 is case insensitive, deducts 10% of merchandise subtotal "
                "with nearest-cent half-up rounding; shipping is calculated from "
                "the original subtotal; unknown, empty, or whitespace-only codes "
                "raise ValueError; None preserves behavior; Quote.discount_cents "
                "and total reflect discount; item validation remains unchanged. "
                "The unchanged baseline and discount-code acceptance suites both "
                "passed. Do not use tools or edit files. "
                "Your entire reply must be one JSON object in this exact schema: "
                "{\"approved\":true,\"findings\":[]}. Set approved false and provide "
                "specific findings if the diff fails the task.\n\n" + diff,
                "claude", "review",
            ), workspace)
            artifact["review_status"] = reviewed.status.value
            if reviewed.status != AgentStatus.SUCCEEDED:
                artifact["reason"] = "review_failed"
                return artifact
            review_text = reviewed.output.get("final_text")
            artifact["review_text"] = review_text
            verdict = _review_verdict(review_text)
            if verdict is None:
                artifact["reason"] = "invalid_review_output"
                return artifact
            artifact["review"] = verdict
            if not verdict["approved"] or verdict["findings"]:
                artifact["reason"] = "review_rejected"
                return artifact
            _git(workspace, "add", "--", ALLOWED_FILE)
            _git(workspace, "commit", "-m", "Implement queued CAF discount fixture")
            artifact["commit"] = _git(workspace, "rev-parse", "HEAD")
            artifact["status"] = "succeeded"
            return artifact
        except ProcessTreeCleanupError:
            cleanup_unverified = True
            raise
        finally:
            if not cleanup_unverified:
                _git(repository, "worktree", "remove", "--force", str(workspace))

    try:
        return await dispatch_once(store, owner, execute, lease_seconds=600,
                                   tick_seconds=5, cleanup_seconds=15)
    finally:
        try:
            private_root.rmdir()  # Preserve a nonempty root if cleanup was unverified.
        except OSError:
            pass


def main() -> int:
    """Require an exact HTTP grant and CAF workspace binding for this fixture."""
    binding = resolve_sample_workspace(Path(os.environ["CAF_UMBRELLA_ROOT"]),
                                       "caf-sample-fixture-v1")
    store = WorkerAPIStore(os.environ["CAF_WORKER_API_URL"], os.environ["CAF_WORKER_TOKEN"])
    result = asyncio.run(run_one_code_fixture(
        store, os.environ["CAF_WORKER_ID"], binding,
        Path(os.environ["CAF_CODEX_EXECUTABLE"]),
    ))
    print("empty" if result is None else f"{result.get('execution_kind')}: {result['status']}")
    return 0 if result is None or result["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())

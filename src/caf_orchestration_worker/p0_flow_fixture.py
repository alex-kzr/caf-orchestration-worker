"""Stage adapter for the bounded CAF sample Flow; no CLI is launched on import."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from caf_agent_runtime import (
    AgentResult, AgentStatus, CodingAgentRuntime, CodingTask, FailureClass,
)

from .p0_code_worker import ALLOWED_FILE, FIXTURE_PROMPT, _gate, _git, _review_verdict


FIXTURE_PROVIDERS = {
    "plan": "claude",
    "implement": "codex",
    "review": "claude",
    "correct": "codex",
    "test": "codex",  # The test stage is deterministic; no CLI is launched.
}


def _plan_steps(value: object) -> list[str] | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if stripped.startswith("```json") and stripped.endswith("```"):
        stripped = stripped[7:-3].strip()
    elif stripped.startswith("```") and stripped.endswith("```"):
        stripped = stripped[3:-3].strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    steps = parsed.get("steps") if isinstance(parsed, dict) else None
    if (not isinstance(steps, list) or not 1 <= len(steps) <= 8
            or any(not isinstance(step, str) or not 1 <= len(step.strip()) <= 300
                   for step in steps)):
        return None
    return [step.strip() for step in steps]


class FixtureStageRuntime:
    """Route Flow roles to external CLIs, with deterministic gates and narrow Git scope.

    The sample-specific worker supplies a checked external worktree and exact
    one-Run transport. Tests can inject fake runtimes; no provider is called
    until a Flow stage executes.
    """

    def __init__(self, codex: CodingAgentRuntime, claude: CodingAgentRuntime) -> None:
        self.codex = codex
        self.claude = claude
        self._delegated: dict[str, CodingAgentRuntime] = {}
        self._statuses: dict[str, AgentStatus] = {}

    def _changed_diff(self, workspace: Path) -> str:
        changed = _git(workspace, "status", "--porcelain").splitlines()
        if changed != [f" M {ALLOWED_FILE}"]:
            raise ValueError("fixture has missing or unexpected changed files")
        diff = _git(workspace, "diff", "--", ALLOWED_FILE)
        if not diff or len(diff.encode("utf-8")) > 30_000:
            raise ValueError("fixture diff missing or oversized")
        return diff

    async def execute(self, task: CodingTask, workspace: Path,
                      session_id: str | None = None) -> AgentResult:
        if session_id is not None:
            raise ValueError("session resume is not verified")
        if task.operation not in FIXTURE_PROVIDERS or task.provider != FIXTURE_PROVIDERS[task.operation]:
            raise ValueError("fixture operation/provider mismatch")
        if task.operation == "test":
            gates = [_gate(workspace, name) for name in ("test_baseline.py", "test_discount_code.py")]
            passed = all(gate["passed"] for gate in gates)
            status = AgentStatus.SUCCEEDED if passed else AgentStatus.FAILED
            self._statuses[task.run_id] = status
            return AgentResult(
                task.run_id, task.correlation_id, status,
                "deterministic acceptance gates passed" if passed else "deterministic acceptance gates failed",
                (), None if passed else FailureClass.QUALITY_GATE,
                output={"passed": passed, "gates": gates},
            )
        if task.operation in ("implement", "correct"):
            self._delegated[task.run_id] = self.codex
            result = await self.codex.execute(replace(task, prompt=(
                task.prompt + "\nEdit only src/caf_sample/orders.py. Do not modify acceptance tests, "
                "commit, push, or access other repositories.")), workspace)
            if result.status == AgentStatus.SUCCEEDED:
                self._changed_diff(workspace)
            self._statuses[task.run_id] = result.status
            return result
        self._delegated[task.run_id] = self.claude
        if task.operation == "plan":
            prompt = ("Plan this CAF sample coding task without tools or file edits. "
                      "Reply only with a JSON object containing 1-8 short steps in a 'steps' array. "
                      "Include implementation and acceptance checks. Task: " + FIXTURE_PROMPT)
            result = await self.claude.execute(replace(task, prompt=prompt), workspace)
            if result.status == AgentStatus.SUCCEEDED:
                steps = _plan_steps(result.output.get("final_text"))
                if steps is None:
                    result = replace(result, status=AgentStatus.FAILED,
                                     failure_class=FailureClass.AGENT,
                                     summary="invalid structured plan", output={})
                else:
                    result = replace(result, output={"steps": steps})
        else:
            diff = self._changed_diff(workspace)
            prompt = (
                "Review this CAF sample implementation diff without tools or edits. "
                "Requirements: " + FIXTURE_PROMPT + "\nThe unchanged acceptance suites run "
                "as deterministic gates after review. Reply only with one JSON object: "
                '{"approved":true,"findings":[]}; set approved false and give specific '
                "findings if the diff is wrong.\n\n" + diff
            )
            result = await self.claude.execute(replace(task, prompt=prompt), workspace)
            if result.status == AgentStatus.SUCCEEDED:
                verdict = _review_verdict(result.output.get("final_text"))
                if (verdict is None or len(verdict["findings"]) > 20
                        or any(len(item) > 2000 for item in verdict["findings"])):
                    result = replace(result, status=AgentStatus.FAILED,
                                     failure_class=FailureClass.AGENT,
                                     summary="invalid structured review", output={})
                else:
                    result = replace(result, output=verdict)
        self._statuses[task.run_id] = result.status
        return result

    async def cancel(self, run_id: str) -> None:
        delegated = self._delegated.get(run_id)
        if delegated is not None:
            await delegated.cancel(run_id)
        self._statuses[run_id] = AgentStatus.CANCELLED

    async def get_status(self, run_id: str) -> AgentStatus:
        delegated = self._delegated.get(run_id)
        if delegated is not None and run_id not in self._statuses:
            return await delegated.get_status(run_id)
        return self._statuses.get(run_id, AgentStatus.FAILED)

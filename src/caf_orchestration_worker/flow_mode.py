"""CrewAI Flow mode B: Flow listeners dispatch the shared runtime stages."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from caf_agent_runtime import AgentResult, AgentStatus, CodingAgentRuntime, CodingTask
from crewai.flow.flow import Flow, listen, start

from .workflow import RunArtifact, RunRequest, WorkflowSession


class CodingFlow(Flow):
    """No CrewAI Agent or LLM is constructed; listeners call the runtime protocol."""

    def __init__(self, session: WorkflowSession) -> None:
        super().__init__()
        self.session = session

    @start()
    async def plan(self) -> str:
        await self.session.plan()
        return self.session.artifact.status

    @listen(plan)
    async def implement(self, _previous: str) -> str:
        await self.session.implement()
        return self.session.artifact.status

    @listen(implement)
    async def review_and_gate(self, _previous: str) -> RunArtifact:
        return await self.session.evaluate()


class PlanOnlyFlow(Flow):
    """A terminal diagnostic Flow with no implementation/evaluation listeners."""

    def __init__(self, session: WorkflowSession) -> None:
        super().__init__()
        self.session = session

    @start()
    async def plan(self) -> RunArtifact:
        await self.session.plan()
        if self.session.artifact.status == "running":
            self.session.finish("succeeded", "plan_only_complete")
        return self.session.artifact


class PlanOnlyRuntime:
    """Reject any non-plan stage before it can reach a CLI or local gate."""

    def __init__(self, runtime: CodingAgentRuntime) -> None:
        self.runtime = runtime

    async def execute(self, task: CodingTask, workspace: Path,
                      session_id: str | None = None) -> AgentResult:
        if task.operation != "plan":
            raise RuntimeError("plan-only Flow forbids downstream stages")
        return await self.runtime.execute(task, workspace, session_id)

    async def cancel(self, run_id: str) -> None:
        await self.runtime.cancel(run_id)

    async def get_status(self, run_id: str) -> AgentStatus:
        return await self.runtime.get_status(run_id)


async def run_flow(request: RunRequest, runtime: CodingAgentRuntime, workspace: Path,
                   provider_by_operation: Mapping[str, str] | None = None) -> RunArtifact:
    """Run the same policy as the baseline through real CrewAI Flow listeners."""
    session = WorkflowSession(request, runtime, workspace, "crewai-flow", provider_by_operation)
    flow = CodingFlow(session)
    await flow.kickoff_async()
    return session.artifact


async def run_plan_only_flow(request: RunRequest, runtime: CodingAgentRuntime,
                             workspace: Path,
                             provider_by_operation: Mapping[str, str] | None = None) -> RunArtifact:
    """Run only the plan start node and leave an explicit terminal artifact."""
    session = WorkflowSession(request, PlanOnlyRuntime(runtime), workspace, "crewai-flow-plan-only",
                              provider_by_operation)
    flow = PlanOnlyFlow(session)
    await flow.kickoff_async()
    return session.artifact

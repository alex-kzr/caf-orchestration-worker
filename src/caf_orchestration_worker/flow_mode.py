"""CrewAI Flow mode B: Flow listeners dispatch the shared runtime stages."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from caf_agent_runtime import CodingAgentRuntime
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


async def run_flow(request: RunRequest, runtime: CodingAgentRuntime, workspace: Path,
                   provider_by_operation: Mapping[str, str] | None = None) -> RunArtifact:
    """Run the same policy as the baseline through real CrewAI Flow listeners."""
    session = WorkflowSession(request, runtime, workspace, "crewai-flow", provider_by_operation)
    flow = CodingFlow(session)
    await flow.kickoff_async()
    return session.artifact

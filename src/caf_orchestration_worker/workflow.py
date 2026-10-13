"""Sequential asyncio workflow; runtime execution belongs to caf-agent-runtime."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from caf_agent_runtime import AgentStatus, CodingAgentRuntime, CodingTask


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class RunRequest:
    run_id: str
    correlation_id: str
    prompt: str
    provider: str
    max_corrections: int = 1
    base_revision: str | None = None

    def __post_init__(self) -> None:
        if not all((self.run_id.strip(), self.correlation_id.strip(), self.prompt.strip(), self.provider.strip())):
            raise ValueError("run_id, correlation_id, prompt, and provider are required")
        if self.max_corrections < 0:
            raise ValueError("max_corrections must be nonnegative")
        if self.base_revision is not None and not self.base_revision.strip():
            raise ValueError("base_revision must not be blank")


@dataclass(frozen=True, slots=True)
class StageArtifact:
    operation: str
    task_id: str
    status: str
    failure_class: str | None
    summary: str
    output: dict[str, Any]
    started_at: str
    ended_at: str
    events: tuple[dict[str, Any], ...] = ()
    exit_code: int | None = None
    session_id: str | None = None
    changed_files: tuple[str, ...] = ()
    commits: tuple[str, ...] = ()
    usage: dict[str, Any] | None = None


@dataclass(slots=True)
class RunArtifact:
    run_id: str
    correlation_id: str
    mode: str
    status: str
    reason: str | None
    iterations: int
    started_at: str
    base_revision: str | None = None
    ended_at: str | None = None
    stages: list[StageArtifact] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


async def run_baseline(
    request: RunRequest, runtime: CodingAgentRuntime, workspace: Path
) -> RunArtifact:
    """Execute the P0 synthetic stage contract without claiming Git or CLI effects."""
    if not workspace.is_dir():
        raise ValueError("workspace must be an existing directory")
    artifact = RunArtifact(request.run_id, request.correlation_id, "asyncio-baseline", "running", None, 0, _now(), request.base_revision)

    async def stage(operation: str, prompt: str | None = None) -> StageArtifact:
        task_id = f"{request.run_id}:{len(artifact.stages) + 1}:{operation}"
        task = CodingTask(task_id, request.correlation_id, prompt or request.prompt, request.provider, operation)
        started = _now()
        try:
            result = await runtime.execute(task, workspace)
            if result.run_id != task_id or result.correlation_id != request.correlation_id:
                raise ValueError("runtime returned mismatched identity")
            output = dict(result.output)
            usage = dict(result.usage) if result.usage is not None else None
            events = tuple(asdict(event) for event in result.events)
            json.dumps((output, usage, events))  # Persisted artifacts must be JSON-compatible.
            item = StageArtifact(
                operation, task_id, result.status.value,
                result.failure_class.value if result.failure_class else None,
                result.summary, output, started, _now(), events,
                result.exit_code, result.session_id, result.changed_files, result.commits, usage,
            )
        except Exception as exc:
            # Exception details may contain CLI output, paths or credentials.
            item = StageArtifact(operation, task_id, "failed", "infrastructure", type(exc).__name__, {}, started, _now())
        artifact.stages.append(item)
        return item

    def finish(status: str, reason: str | None = None) -> RunArtifact:
        artifact.status = status
        artifact.reason = reason
        artifact.ended_at = _now()
        return artifact

    for operation in ("plan", "implement"):
        item = await stage(operation)
        if item.status != AgentStatus.SUCCEEDED.value:
            return finish("blocked", f"{operation}_{item.failure_class or item.status}")

    while True:
        review = await stage("review")
        if review.status != AgentStatus.SUCCEEDED.value:
            return finish("blocked", f"review_{review.failure_class or review.status}")
        approved = review.output.get("approved")
        if not isinstance(approved, bool):
            return finish("blocked", "invalid_review_output")
        if approved:
            test = await stage("test")
            if test.status != AgentStatus.SUCCEEDED.value:
                if test.failure_class == "quality_gate":
                    failure = "test_failed"
                else:
                    return finish("blocked", f"test_{test.failure_class or test.status}")
            elif not isinstance(test.output.get("passed"), bool):
                return finish("blocked", "invalid_test_output")
            elif test.output["passed"]:
                return finish("succeeded")
            else:
                failure = "test_failed"
        else:
            failure = "review_rejected"
        if artifact.iterations >= request.max_corrections:
            return finish("blocked", failure)
        evidence = review.output if failure == "review_rejected" else test.output
        correction_prompt = f"{request.prompt}\n\nCorrect the {failure} using this structured evidence: {json.dumps(evidence)}"
        correction = await stage("correct", correction_prompt)
        artifact.iterations += 1
        if correction.status != AgentStatus.SUCCEEDED.value:
            return finish("blocked", f"correct_{correction.failure_class or correction.status}")


def write_artifact(artifact: RunArtifact, path: Path) -> None:
    """Write one JSON report to an explicitly provided output path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(artifact.to_dict(), indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)

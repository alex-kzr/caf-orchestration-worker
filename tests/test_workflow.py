import asyncio
from pathlib import Path

import pytest

from caf_agent_runtime import SyntheticOutcome, SyntheticRuntime
from caf_agent_runtime import AgentStatus, FailureClass
from caf_agent_runtime.windows_job import ProcessTreeCleanupError
from caf_orchestration_worker import RunRequest, run_baseline, write_artifact


def test_unverified_process_cleanup_escapes_workflow(tmp_path: Path) -> None:
    class BrokenRuntime:
        async def execute(self, *_):
            raise ProcessTreeCleanupError("fixture")

    with pytest.raises(ProcessTreeCleanupError):
        asyncio.run(run_baseline(
            RunRequest("run-cleanup", "corr-cleanup", "No coding", "synthetic"),
            BrokenRuntime(), tmp_path,  # type: ignore[arg-type]
        ))


def test_approved_run_records_all_stages(tmp_path: Path) -> None:
    runtime = SyntheticRuntime({"review": [SyntheticOutcome(output={"approved": True, "findings": []})], "test": [SyntheticOutcome(output={"passed": True, "checks": ["synthetic"]})]})
    request = RunRequest("run-1", "corr-1", "Change the sample", "synthetic")
    artifact = asyncio.run(run_baseline(request, runtime, tmp_path))
    assert artifact.status == "succeeded"
    assert [stage.operation for stage in artifact.stages] == ["plan", "implement", "review", "test"]
    assert artifact.iterations == 0
    assert artifact.to_dict()["correlation_id"] == "corr-1"


def test_rejected_review_causes_one_correction_then_retest(tmp_path: Path) -> None:
    runtime = SyntheticRuntime({"review": [SyntheticOutcome(output={"approved": False, "findings": ["fix issue"]}), SyntheticOutcome(output={"approved": True, "findings": []})], "test": [SyntheticOutcome(output={"passed": True, "checks": ["synthetic"]})]})
    request = RunRequest("run-2", "corr-2", "Change the sample", "synthetic", max_corrections=1)
    artifact = asyncio.run(run_baseline(request, runtime, tmp_path))
    assert artifact.status == "succeeded"
    assert artifact.iterations == 1
    assert [stage.operation for stage in artifact.stages] == ["plan", "implement", "review", "correct", "review", "test"]
    assert artifact.stages[2].output["findings"] == ["fix issue"]


def test_repeated_rejection_blocks_at_limit(tmp_path: Path) -> None:
    runtime = SyntheticRuntime({"review": [SyntheticOutcome(output={"approved": False, "findings": ["still bad"]})]})
    request = RunRequest("run-3", "corr-3", "Change", "synthetic", max_corrections=1)
    artifact = asyncio.run(run_baseline(request, runtime, tmp_path))
    assert artifact.status == "blocked"
    assert artifact.reason == "review_rejected"
    assert [stage.operation for stage in artifact.stages] == ["plan", "implement", "review", "correct", "review"]


def test_missing_review_verdict_blocks_without_guessing(tmp_path: Path) -> None:
    runtime = SyntheticRuntime({"review": [SyntheticOutcome(output={})]})
    request = RunRequest("run-4", "corr-4", "Change", "synthetic")
    artifact = asyncio.run(run_baseline(request, runtime, tmp_path))
    assert artifact.status == "blocked"
    assert artifact.reason == "invalid_review_output"


def test_failed_test_causes_correction_and_retest(tmp_path: Path) -> None:
    runtime = SyntheticRuntime({
        "review": [SyntheticOutcome(output={"approved": True})],
        "test": [SyntheticOutcome(output={"passed": False}), SyntheticOutcome(output={"passed": True})],
    })
    request = RunRequest("run-5", "corr-5", "Change", "synthetic", max_corrections=1)
    artifact = asyncio.run(run_baseline(request, runtime, tmp_path))
    assert artifact.status == "succeeded"
    assert [stage.operation for stage in artifact.stages] == [
        "plan", "implement", "review", "test", "correct", "review", "test"
    ]


def test_write_artifact_preserves_structured_events(tmp_path: Path) -> None:
    import json

    runtime = SyntheticRuntime({
        "review": [SyntheticOutcome(output={"approved": True})],
        "test": [SyntheticOutcome(output={"passed": True})],
    })
    artifact = asyncio.run(run_baseline(RunRequest("run-6", "corr-6", "Change", "synthetic"), runtime, tmp_path))
    output = tmp_path / "artifact.json"
    write_artifact(artifact, output)
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["status"] == "succeeded"
    assert saved["stages"][0]["events"][0]["correlation_id"] == "corr-6"
    assert not output.with_suffix(".json.tmp").exists()


def test_cli_demo_writes_a_successful_synthetic_report(tmp_path: Path) -> None:
    import json

    from caf_orchestration_worker.__main__ import main

    output = tmp_path / "demo.json"
    assert main(["synthetic-demo", "--output", str(output), "--base-revision", "b9761bad"]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["mode"] == "asyncio-baseline"
    assert report["status"] == "succeeded"
    assert report["iterations"] == 1
    assert report["base_revision"] == "b9761bad"


def test_fixture_base_revision_is_recorded_in_artifact(tmp_path: Path) -> None:
    runtime = SyntheticRuntime({
        "review": [SyntheticOutcome(output={"approved": True})],
        "test": [SyntheticOutcome(output={"passed": True})],
    })
    request = RunRequest("run-7", "corr-7", "Change", "synthetic", base_revision="b9761bad")
    artifact = asyncio.run(run_baseline(request, runtime, tmp_path))
    assert artifact.to_dict()["base_revision"] == "b9761bad"


def test_correction_receives_structured_review_findings(tmp_path: Path) -> None:
    class RecordingRuntime(SyntheticRuntime):
        def __init__(self) -> None:
            super().__init__({
                "review": [SyntheticOutcome(output={"approved": False, "findings": ["Handle empty input"]}), SyntheticOutcome(output={"approved": True})],
                "test": [SyntheticOutcome(output={"passed": True})],
            })
            self.tasks = []

        async def execute(self, task, workspace, session_id=None):
            self.tasks.append(task)
            return await super().execute(task, workspace, session_id)

    runtime = RecordingRuntime()
    artifact = asyncio.run(run_baseline(RunRequest("run-8", "corr-8", "Change", "synthetic"), runtime, tmp_path))
    assert artifact.status == "succeeded"
    correction = next(task for task in runtime.tasks if task.operation == "correct")
    assert "Handle empty input" in correction.prompt


def test_runtime_exception_becomes_blocked_artifact(tmp_path: Path) -> None:
    class BrokenRuntime(SyntheticRuntime):
        async def execute(self, task, workspace, session_id=None):
            if task.operation == "implement":
                raise OSError("private diagnostic")
            return await super().execute(task, workspace, session_id)

    artifact = asyncio.run(run_baseline(RunRequest("run-9", "corr-9", "Change", "synthetic"), BrokenRuntime(), tmp_path))
    assert artifact.status == "blocked"
    assert artifact.reason == "implement_infrastructure"
    assert artifact.stages[-1].failure_class == "infrastructure"
    assert "private diagnostic" not in str(artifact.to_dict())


def test_test_runtime_failure_blocks_without_correction(tmp_path: Path) -> None:
    runtime = SyntheticRuntime({
        "review": [SyntheticOutcome(output={"approved": True})],
        "test": [SyntheticOutcome(status=AgentStatus.FAILED, failure_class=FailureClass.INFRASTRUCTURE)],
    })
    artifact = asyncio.run(run_baseline(RunRequest("run-10", "corr-10", "Change", "synthetic"), runtime, tmp_path))
    assert artifact.status == "blocked"
    assert artifact.reason == "test_infrastructure"
    assert [stage.operation for stage in artifact.stages] == ["plan", "implement", "review", "test"]


def test_quality_gate_failure_causes_bounded_correction(tmp_path: Path) -> None:
    runtime = SyntheticRuntime({
        "review": [SyntheticOutcome(output={"approved": True})],
        "test": [
            SyntheticOutcome(status=AgentStatus.FAILED, failure_class=FailureClass.QUALITY_GATE, output={"passed": False, "checks": ["unit"]}),
            SyntheticOutcome(output={"passed": True}),
        ],
    })
    artifact = asyncio.run(run_baseline(RunRequest("run-12", "corr-12", "Change", "synthetic"), runtime, tmp_path))
    assert artifact.status == "succeeded"
    assert artifact.iterations == 1
    assert [stage.operation for stage in artifact.stages] == ["plan", "implement", "review", "test", "correct", "review", "test"]


def test_stage_retains_runtime_result_metadata(tmp_path: Path) -> None:
    from dataclasses import replace

    class MetadataRuntime(SyntheticRuntime):
        async def execute(self, task, workspace, session_id=None):
            result = await super().execute(task, workspace, session_id)
            if task.operation == "plan":
                return replace(result, exit_code=0, session_id="session-1", changed_files=("sample.py",), commits=("abc123",), usage={"tokens": 12})
            return result

    runtime = MetadataRuntime({"review": [SyntheticOutcome(output={"approved": True})], "test": [SyntheticOutcome(output={"passed": True})]})
    artifact = asyncio.run(run_baseline(RunRequest("run-11", "corr-11", "Change", "synthetic"), runtime, tmp_path))
    assert artifact.stages[0].exit_code == 0
    assert artifact.stages[0].session_id == "session-1"
    assert artifact.stages[0].changed_files == ("sample.py",)
    assert artifact.stages[0].commits == ("abc123",)
    assert artifact.stages[0].usage == {"tokens": 12}

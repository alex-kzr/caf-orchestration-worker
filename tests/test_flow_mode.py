import asyncio
from pathlib import Path

import pytest

pytest.importorskip("crewai.flow.flow")

from caf_agent_runtime import AgentStatus, FailureClass, SyntheticOutcome, SyntheticRuntime
from caf_orchestration_worker import RunRequest, run_baseline
from caf_orchestration_worker.flow_mode import run_flow


def _runtime() -> SyntheticRuntime:
    return SyntheticRuntime({
        "review": [
            SyntheticOutcome(output={"approved": False, "findings": ["fix the edge case"]}),
            SyntheticOutcome(output={"approved": True, "findings": []}),
        ],
        "test": [SyntheticOutcome(output={"passed": True, "checks": ["synthetic"]})],
    })


def _comparable(artifact):
    data = artifact.to_dict()
    return (
        data["run_id"], data["correlation_id"], data["status"], data["reason"],
        data["iterations"], data["base_revision"],
        [(stage["operation"], stage["status"], stage["failure_class"], stage["output"])
         for stage in data["stages"]],
    )


def test_flow_matches_baseline_on_bounded_correction_without_model_keys(tmp_path: Path, monkeypatch) -> None:
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    request = RunRequest("compare-1", "corr-1", "Change sample", "synthetic", base_revision="e12f4d8")
    baseline = asyncio.run(run_baseline(request, _runtime(), tmp_path))
    flow = asyncio.run(run_flow(request, _runtime(), tmp_path))
    assert flow.mode == "crewai-flow"
    assert _comparable(flow) == _comparable(baseline)


def test_flow_matches_baseline_when_review_limit_blocks(tmp_path: Path) -> None:
    scenarios = {"review": [SyntheticOutcome(output={"approved": False, "findings": ["still bad"]})]}
    request = RunRequest("compare-2", "corr-2", "Change sample", "synthetic", max_corrections=1)
    baseline = asyncio.run(run_baseline(request, SyntheticRuntime(scenarios), tmp_path))
    flow = asyncio.run(run_flow(request, SyntheticRuntime(scenarios), tmp_path))
    assert flow.status == "blocked"
    assert _comparable(flow) == _comparable(baseline)


def test_flow_matches_baseline_on_test_infrastructure_failure(tmp_path: Path) -> None:
    scenarios = {
        "review": [SyntheticOutcome(output={"approved": True})],
        "test": [SyntheticOutcome(status=AgentStatus.FAILED, failure_class=FailureClass.INFRASTRUCTURE)],
    }
    request = RunRequest("compare-3", "corr-3", "Change sample", "synthetic")
    baseline = asyncio.run(run_baseline(request, SyntheticRuntime(scenarios), tmp_path))
    flow = asyncio.run(run_flow(request, SyntheticRuntime(scenarios), tmp_path))
    assert flow.reason == "test_infrastructure"
    assert _comparable(flow) == _comparable(baseline)


def test_flow_cli_writes_mode_b_artifact(tmp_path: Path) -> None:
    import json

    from caf_orchestration_worker.__main__ import main

    output = tmp_path / "flow.json"
    assert main(["synthetic-demo", "--mode", "flow", "--output", str(output)]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["mode"] == "crewai-flow"
    assert [stage["operation"] for stage in report["stages"]] == [
        "plan", "implement", "review", "correct", "review", "test"
    ]

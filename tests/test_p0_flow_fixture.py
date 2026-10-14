"""Offline, no-model checks of the real Flow's role routing and correction gate."""

import asyncio
from pathlib import Path
import subprocess
from uuid import uuid4

import pytest

pytest.importorskip("crewai.flow.flow")

from caf_agent_runtime import AgentResult, AgentStatus
from caf_control_api.storage import ClaimedRun
from caf_orchestration_worker.flow_mode import run_flow
from caf_orchestration_worker import p0_flow_worker
from caf_orchestration_worker.p0_flow_fixture import FIXTURE_PROVIDERS, FixtureStageRuntime, _plan_steps
from caf_orchestration_worker.p0_code_worker import FIXTURE_PROMPT
from caf_orchestration_worker.workflow import RunRequest
from caf_orchestration_worker.workspace_binding import WorkspaceBinding


def fixture_repo(path: Path) -> None:
    (path / "src/caf_sample").mkdir(parents=True)
    (path / "tests").mkdir()
    (path / "src/caf_sample/__init__.py").write_text("", encoding="utf-8")
    (path / "src/caf_sample/orders.py").write_text("VALUE = 0\n", encoding="utf-8")
    (path / "tests/test_baseline.py").write_text(
        "import unittest\nclass Baseline(unittest.TestCase):\n"
        "    def test_import(self):\n        import caf_sample.orders\n",
        encoding="utf-8",
    )
    (path / "tests/test_discount_code.py").write_text(
        "import unittest\nfrom caf_sample.orders import VALUE\n"
        "class Discount(unittest.TestCase):\n"
        "    def test_value(self):\n        self.assertEqual(VALUE, 2)\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.name=CAF Test",
                    "-c", "user.email=caf-test@example.invalid", "commit", "-qm", "base"], check=True)


def test_structured_plan_accepts_one_json_fence_but_rejects_extra_prose() -> None:
    assert _plan_steps('```json\n{"steps":["edit sample","run tests"]}\n```') == [
        "edit sample", "run tests"
    ]
    assert _plan_steps('Here is the plan: {"steps":["edit sample"]}') is None


class FakeCLI:
    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.calls = []
        self.reviews = 0

    async def execute(self, task, workspace, session_id=None):
        assert task.provider == self.provider and session_id is None
        self.calls.append(task)
        output = {}
        if task.operation == "plan":
            output = {"final_text": '{"steps":["edit the sample","run acceptance checks"]}'}
        elif task.operation == "implement":
            (workspace / "src/caf_sample/orders.py").write_text("VALUE = 1\n", encoding="utf-8")
        elif task.operation == "correct":
            assert "Fix VALUE" in task.prompt
            (workspace / "src/caf_sample/orders.py").write_text("VALUE = 2\n", encoding="utf-8")
        elif task.operation == "review":
            self.reviews += 1
            output = {"final_text": ('{"approved":false,"findings":["Fix VALUE"]}'
                                     if self.reviews == 1 else '{"approved":true,"findings":[]}')}
        return AgentResult(task.run_id, task.correlation_id, AgentStatus.SUCCEEDED,
                           "offline fixture", (), output=output)

    async def cancel(self, run_id):
        return None

    async def get_status(self, run_id):
        return AgentStatus.SUCCEEDED


def test_actual_flow_routes_two_providers_and_corrects_in_one_run(tmp_path: Path, monkeypatch) -> None:
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    fixture_repo(tmp_path)
    codex, claude = FakeCLI("codex"), FakeCLI("claude")
    request = RunRequest("flow-one", "corr-one", FIXTURE_PROMPT, "codex", max_corrections=1)
    artifact = asyncio.run(run_flow(request, FixtureStageRuntime(codex, claude),
                                    tmp_path, FIXTURE_PROVIDERS))
    assert artifact.status == "succeeded" and artifact.iterations == 1
    assert [stage.operation for stage in artifact.stages] == [
        "plan", "implement", "review", "correct", "review", "test"
    ]
    assert [task.operation for task in codex.calls] == ["implement", "correct"]
    assert 'Plan steps: ["edit the sample", "run acceptance checks"]' in codex.calls[0].prompt
    assert [task.operation for task in claude.calls] == ["plan", "review", "review"]
    assert artifact.stages[-1].output["gates"] == [
        {"suite": "test_baseline.py", "passed": True, "exit_code": 0},
        {"suite": "test_discount_code.py", "passed": True, "exit_code": 0},
    ]
    assert all(stage.task_id.startswith("flow-one:") for stage in artifact.stages)


def test_review_rejection_blocks_at_zero_corrections(tmp_path: Path) -> None:
    fixture_repo(tmp_path)
    codex, claude = FakeCLI("codex"), FakeCLI("claude")
    artifact = asyncio.run(run_flow(
        RunRequest("flow-zero", "corr-zero", FIXTURE_PROMPT, "codex", max_corrections=0),
        FixtureStageRuntime(codex, claude), tmp_path, FIXTURE_PROVIDERS,
    ))
    assert artifact.status == "blocked" and artifact.reason == "review_rejected"
    assert [task.operation for task in codex.calls] == ["implement"]
    assert [stage.operation for stage in artifact.stages] == ["plan", "implement", "review"]


def test_host_flow_worker_rejects_shared_database_before_claim(tmp_path: Path) -> None:
    class Store:
        def database_name(self):
            return "caf"

    binding = WorkspaceBinding("caf-sample-fixture-v1", "caf-sample-app", tmp_path,
                               p0_flow_worker.BASE_REVISION, tmp_path.parent)
    with pytest.raises(ValueError, match="isolated test database"):
        asyncio.run(p0_flow_worker.run_one_flow_fixture(
            Store(), "worker-caf-test", binding, FakeCLI("codex"), FakeCLI("claude")
        ))


def test_host_flow_worker_rejects_unscoped_prompt_before_clis(tmp_path: Path, monkeypatch) -> None:
    class Store:
        def __init__(self):
            self.done = None

        def database_name(self):
            return "caf_queue_test_unit"

        def quarantine_expired(self):
            return []

        def claim_queued(self, owner, lease_seconds=60):
            if self.done is not None:
                return None
            return ClaimedRun(uuid4(), uuid4(), uuid4(), "caf-sample-app",
                              p0_flow_worker.BASE_REVISION, "unscoped prompt", [],
                              "codex", 1, 1, "caf-sample-fixture-v1")

        def claim_control(self, run_id, owner):
            return True, False

        def renew_lease(self, run_id, owner, lease_seconds=60):
            return True

        def complete_claim(self, run_id, owner, status, artifact):
            self.done = status, artifact
            return True

    root = tmp_path / "caf" / "repositories" / "caf-sample-app"
    root.mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    monkeypatch.setattr(p0_flow_worker, "_git", lambda _, *args:
                        p0_flow_worker.BASE_REVISION if args == ("rev-parse", "HEAD") else "")
    store = Store()
    artifact = asyncio.run(p0_flow_worker.run_one_flow_fixture(
        store, "worker-caf-test", WorkspaceBinding(
            "caf-sample-fixture-v1", "caf-sample-app", root,
            p0_flow_worker.BASE_REVISION, external,
        ), FakeCLI("codex"), FakeCLI("claude"),
    ))
    assert artifact == {"status": "blocked", "reason": "fixture_claim_mismatch"}
    assert store.done == ("failed", artifact)

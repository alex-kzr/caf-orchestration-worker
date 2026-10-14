"""Offline, no-model checks of the real Flow's role routing and correction gate."""

import asyncio
import json
from pathlib import Path
import subprocess
from uuid import uuid4

import pytest

pytest.importorskip("crewai.flow.flow")

from caf_agent_runtime import AgentResult, AgentStatus, CodingTask, FailureClass
from caf_control_api.storage import ClaimedRun
from caf_orchestration_worker.flow_mode import PlanOnlyRuntime, run_flow, run_plan_only_flow
from caf_orchestration_worker import p0_flow_worker
from caf_orchestration_worker.p0_flow_fixture import (
    FIXTURE_PROVIDERS, PLAN_SCHEMA, FixtureStageRuntime, _parse_plan,
    _parse_plan_detailed, _parse_review,
    _plan_steps, _safe_diagnostic,
)
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


@pytest.mark.parametrize(
    "case", json.loads((Path(__file__).parent / "fixtures/claude_plan_text_cases.json")
                       .read_text(encoding="utf-8")),
    ids=lambda case: case["name"],
)
def test_supported_claude_result_text_formats_and_negative_cases(case: dict) -> None:
    steps, outcome, fmt = _parse_plan(case["text"])
    assert (outcome, fmt) == (case["outcome"], case["format"])
    assert (steps is not None) == (outcome == "valid")


@pytest.mark.parametrize("payload,keyword,code,path,expected,actual,count", [
    ('[]', "type", "root_type", "$", "object", "array", None),
    ('{}', "required", "missing_steps", "$.steps", "array", "missing", 0),
    ('{"private_token":"SECRET"}', "required", "missing_steps", "$.steps", "array", "missing", 1),
    ('{"steps":"SECRET"}', "type", "steps_type", "$.steps", "array", "string", 0),
    ('{"steps":[]}', "minItems", "too_few_steps", "$.steps", "array", "array", 0),
    ('{"steps":[1]}', "type", "step_type", "$.steps[]", "string", "number", 0),
    ('{"steps":["  "]}', "minLength", "empty_step", "$.steps[]", "string", "string", 0),
    ('{"steps":["ok"],"private_token":"SECRET"}', "additionalProperties",
     "unknown_fields", "$", "object", "object", 1),
    ('{"steps":["ok"],"steps":["SECRET"]}', None, "duplicate_property",
     "$", "object", "object", None),
])
def test_plan_schema_errors_are_fixed_identifiers_only(
    payload: str, keyword: str | None, code: str, path: str,
    expected: str, actual: str, count: int | None,
) -> None:
    steps, outcome, _, issue = _parse_plan_detailed(payload)
    assert steps is None and outcome == "invalid_schema"
    assert issue == {"schema_keyword": keyword, "error_code": code,
                     "path": path, "expected_type": expected,
                     "actual_type": actual, "unknown_field_count": count}
    assert "SECRET" not in json.dumps(issue)
    assert "private_token" not in json.dumps(issue)


def test_plan_cli_schema_matches_local_cardinality_and_extra_field_policy() -> None:
    assert PLAN_SCHEMA["required"] == ["steps"]
    assert PLAN_SCHEMA["additionalProperties"] is False
    steps = PLAN_SCHEMA["properties"]["steps"]
    assert (steps["minItems"], steps["maxItems"]) == (1, 8)
    assert (steps["items"]["minLength"], steps["items"]["maxLength"]) == (1, 300)
    assert _parse_plan('{"steps":[' + ",".join('"x"' for _ in range(9)) + ']}')[0] is None
    assert _parse_plan('{"steps":["' + "x" * 301 + '"]}')[0] is None


@pytest.mark.parametrize("value,outcome", [
    ('{"approved":true,"findings":[]}', "valid"),
    ('```json\n{"approved":false,"findings":["fix logic"]}\n```', "valid"),
    ('Review: {"approved":true,"findings":[]}', "invalid_json"),
    ('{"approved":"yes","findings":[]}', "invalid_schema"),
    ('{"approved":true,"findings":[],"extra":1}', "invalid_schema"),
])
def test_flow_review_requires_complete_structured_verdict(value: str, outcome: str) -> None:
    verdict, actual, _ = _parse_review(value)
    assert actual == outcome
    assert (verdict is not None) == (outcome == "valid")


@pytest.mark.parametrize("status,failure,exit_code,output,outcome,category", [
    (AgentStatus.FAILED, FailureClass.AGENT, 7, {}, "not_run", "process_exit"),
    (AgentStatus.FAILED, FailureClass.INFRASTRUCTURE, 0, {}, "not_run", "unexpected_error"),
    (AgentStatus.TIMED_OUT, FailureClass.TIMEOUT, None, {}, "not_run", "timeout"),
    (AgentStatus.FAILED, FailureClass.INFRASTRUCTURE, 1,
     {"diagnostic_category": "authentication"}, "not_run", "auth_limit"),
    (AgentStatus.FAILED, FailureClass.AGENT, 1,
     {"stderr_tail": "429 rate limit reached SECRET"}, "not_run", "auth_limit"),
    (AgentStatus.FAILED, FailureClass.INFRASTRUCTURE, 1,
     {"diagnostic_category": "network"}, "not_run", "transport"),
    (AgentStatus.SUCCEEDED, None, 0, {"final_text": ""}, "empty_output", "empty_output"),
    (AgentStatus.SUCCEEDED, None, 0, {"final_text": "SECRET{"}, "invalid_json", "json"),
    (AgentStatus.SUCCEEDED, None, 0, {"final_text": '{"steps":[]}'}, "invalid_schema", "schema"),
])
def test_safe_stage_diagnostic_uses_only_structured_metadata(
    status, failure, exit_code, output, outcome, category,
) -> None:
    task = CodingTask("one:plan", "corr-safe", "PRIVATE PROMPT", "claude", "plan")
    result = AgentResult(task.run_id, task.correlation_id, status, "PRIVATE SUMMARY",
                         (), failure, exit_code=exit_code, output=output)
    diagnostic = _safe_diagnostic(task, result, outcome, "plain_json")
    assert diagnostic["category"] == category
    assert diagnostic["phase"] == "plan"
    assert diagnostic["correlation_id"] == "corr-safe"
    assert diagnostic["parser_outcome"] == outcome
    assert diagnostic["captured_response_bytes"] == (
        len(output["final_text"].encode("utf-8")) if "final_text" in output else None
    )
    encoded = json.dumps(diagnostic)
    assert "SECRET" not in encoded and "PRIVATE" not in encoded


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


@pytest.mark.parametrize("final_text,expected", [
    (None, "empty_output"),
    ("some prose", "json"),
    ('{"steps":[]}', "schema"),
])
def test_flow_plan_failure_records_safe_parser_category(
    tmp_path: Path, final_text: str | None, expected: str,
) -> None:
    fixture_repo(tmp_path)

    class PlanCLI(FakeCLI):
        async def execute(self, task, workspace, session_id=None):
            self.calls.append(task)
            return AgentResult(task.run_id, task.correlation_id, AgentStatus.SUCCEEDED,
                               "provider text withheld", (), output={"final_text": final_text})

    codex, claude = FakeCLI("codex"), PlanCLI("claude")
    artifact = asyncio.run(run_flow(
        RunRequest("plan-fails", "corr-safe", FIXTURE_PROMPT, "codex"),
        FixtureStageRuntime(codex, claude), tmp_path, FIXTURE_PROVIDERS,
    ))
    assert artifact.status == "blocked" and artifact.reason == "plan_agent"
    assert not codex.calls and len(artifact.stages) == 1
    diagnostic = artifact.stages[0].output["diagnostic"]
    assert diagnostic["category"] == expected
    assert diagnostic["provider_status"] == "succeeded"
    if final_text == '{"steps":[]}':
        assert diagnostic["schema_error"]["error_code"] == "too_few_steps"
        assert diagnostic["schema_error"]["path"] == "$.steps"
    assert "final_text" not in artifact.stages[0].output


@pytest.mark.parametrize("error,category", [
    (ConnectionError("PRIVATE transport address"), "transport"),
    (TimeoutError("PRIVATE timing detail"), "timeout"),
])
def test_plan_runtime_exception_uses_safe_category(
    tmp_path: Path, error: Exception, category: str,
) -> None:
    class RaisingCLI(FakeCLI):
        async def execute(self, task, workspace, session_id=None):
            raise error

    task = CodingTask("one:plan", "corr-safe", "PRIVATE PROMPT", "claude", "plan")
    result = asyncio.run(FixtureStageRuntime(FakeCLI("codex"), RaisingCLI("claude"))
                         .execute(task, tmp_path))
    assert result.status != AgentStatus.SUCCEEDED
    assert result.output["diagnostic"]["category"] == category
    assert "PRIVATE" not in json.dumps(result.output)


def test_plan_nonzero_cli_exit_stays_distinct_from_parser_failure(tmp_path: Path) -> None:
    class ExitingCLI(FakeCLI):
        async def execute(self, task, workspace, session_id=None):
            return AgentResult(task.run_id, task.correlation_id, AgentStatus.FAILED,
                               "provider text withheld", (), FailureClass.AGENT,
                               exit_code=7,
                               output={"final_text": "PRIVATE TRANSCRIPT",
                                       "stderr_tail": "PRIVATE ERROR"})

    task = CodingTask("one:plan", "corr-safe", "PRIVATE PROMPT", "claude", "plan")
    result = asyncio.run(FixtureStageRuntime(FakeCLI("codex"), ExitingCLI("claude"))
                         .execute(task, tmp_path))
    diagnostic = result.output["diagnostic"]
    assert diagnostic["category"] == "process_exit"
    assert diagnostic["exit_code"] == 7
    assert diagnostic["parser_outcome"] == "not_run"
    assert "PRIVATE" not in json.dumps(result.output)


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


def test_plan_only_success_is_terminal_without_writer_or_review(tmp_path: Path) -> None:
    fixture_repo(tmp_path)
    codex, claude = FakeCLI("codex"), FakeCLI("claude")
    artifact = asyncio.run(run_plan_only_flow(
        RunRequest("plan-one", "corr-plan", FIXTURE_PROMPT, "claude", max_corrections=0),
        FixtureStageRuntime(codex, claude), tmp_path, FIXTURE_PROVIDERS,
    ))
    assert (artifact.status, artifact.reason) == ("succeeded", "plan_only_complete")
    assert artifact.ended_at is not None and artifact.iterations == 0
    assert [stage.operation for stage in artifact.stages] == ["plan"]
    assert artifact.stages[0].output["diagnostic"]["category"] == "ok"
    assert not codex.calls
    assert [task.operation for task in claude.calls] == ["plan"]
    assert subprocess.run(["git", "-C", str(tmp_path), "status", "--porcelain"],
                          capture_output=True, text=True, check=True).stdout == ""


def test_plan_only_failure_is_terminal_without_writer_or_review(tmp_path: Path) -> None:
    fixture_repo(tmp_path)

    class BadPlan(FakeCLI):
        async def execute(self, task, workspace, session_id=None):
            self.calls.append(task)
            return AgentResult(task.run_id, task.correlation_id, AgentStatus.SUCCEEDED,
                               "safe", (), output={"final_text": "invalid plan"})

    codex, claude = FakeCLI("codex"), BadPlan("claude")
    artifact = asyncio.run(run_plan_only_flow(
        RunRequest("plan-bad", "corr-plan", FIXTURE_PROMPT, "claude", max_corrections=0),
        FixtureStageRuntime(codex, claude), tmp_path, FIXTURE_PROVIDERS,
    ))
    assert (artifact.status, artifact.reason) == ("blocked", "plan_agent")
    assert artifact.ended_at is not None and artifact.iterations == 0
    assert [stage.operation for stage in artifact.stages] == ["plan"]
    assert artifact.stages[0].output["diagnostic"]["category"] == "json"
    assert not codex.calls and [task.operation for task in claude.calls] == ["plan"]


@pytest.mark.parametrize("operation", ["implement", "review", "correct", "test"])
def test_plan_only_runtime_rejects_downstream_before_dispatch(
    tmp_path: Path, operation: str,
) -> None:
    codex, claude = FakeCLI("codex"), FakeCLI("claude")
    guarded = PlanOnlyRuntime(FixtureStageRuntime(codex, claude))
    task = CodingTask("guard:1", "corr-guard", FIXTURE_PROMPT,
                      FIXTURE_PROVIDERS[operation], operation)
    with pytest.raises(RuntimeError, match="forbids downstream stages"):
        asyncio.run(guarded.execute(task, tmp_path))
    assert not codex.calls and not claude.calls


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


def test_one_claim_persists_safe_plan_diagnostic_without_provider_text(
    tmp_path: Path, monkeypatch,
) -> None:
    root = tmp_path / "caf" / "repositories" / "caf-sample-app"
    root.mkdir(parents=True)
    fixture_repo(root)
    base = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    monkeypatch.setattr(p0_flow_worker, "BASE_REVISION", base)
    external = tmp_path / "external"
    external.mkdir()
    claim = ClaimedRun(uuid4(), uuid4(), uuid4(), "caf-sample-app", base,
                       FIXTURE_PROMPT, ["gates pass"], "codex", 1, 1,
                       "caf-sample-fixture-v1")

    class Store:
        completed = None

        def database_name(self):
            return "caf_queue_test_offline"

        def quarantine_expired(self):
            return []

        def claim_queued(self, owner, lease_seconds=60):
            return claim

        def claim_control(self, run_id, owner):
            return True, False

        def renew_lease(self, run_id, owner, lease_seconds=60):
            return True

        def complete_claim(self, run_id, owner, status, artifact):
            self.completed = status, artifact
            return True

    class InvalidPlan(FakeCLI):
        async def execute(self, task, workspace, session_id=None):
            self.calls.append(task)
            return AgentResult(task.run_id, task.correlation_id, AgentStatus.SUCCEEDED,
                               "provider text withheld", (),
                               output={"final_text": "private prose {\"steps\":[\"x\"]}"})

    store, codex, claude = Store(), FakeCLI("codex"), InvalidPlan("claude")
    artifact = asyncio.run(p0_flow_worker.run_one_flow_fixture(
        store, "worker-caf-test", WorkspaceBinding(
            "caf-sample-fixture-v1", "caf-sample-app", root, base, external,
        ), codex, claude,
    ))
    assert store.completed == ("failed", artifact)
    assert artifact["reason"] == "plan_agent" and artifact["iterations"] == 0
    assert artifact["stages"][0]["diagnostic"] == {
        "phase": "plan", "correlation_id": str(claim.correlation_id),
        "category": "json", "provider_status": "succeeded",
        "runtime_failure_class": None, "exit_code": None,
        "captured_response_bytes": len('private prose {"steps":["x"]}'.encode()),
        "input_format": "plain_json",
        "parser_outcome": "invalid_json",
        "response_source": None, "envelope": None, "schema_error": None,
    }
    assert "private prose" not in json.dumps(artifact)
    assert not codex.calls


@pytest.mark.parametrize("plan_text,expected_status,expected_result", [
    ('{"steps":["inspect sample","check gates"]}', "succeeded", "plan_valid"),
    ("unstructured response", "failed", "plan_failed"),
    ('{"steps":["ok"],"private_token":"SECRET"}', "failed", "plan_failed"),
    ("REJECTED_ENVELOPE", "failed", "plan_failed"),
    ("REJECTED_SURROGATE", "failed", "plan_failed"),
    ("REJECTED_ARRAY", "failed", "plan_failed"),
])
def test_plan_only_claim_has_one_terminal_stage_and_no_coding_artifacts(
    tmp_path: Path, monkeypatch, plan_text: str,
    expected_status: str, expected_result: str,
) -> None:
    from caf_orchestration_worker.p0_plan_worker import _NoCodingRuntime

    root = tmp_path / "caf" / "repositories" / "caf-sample-app"
    root.mkdir(parents=True)
    fixture_repo(root)
    base = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    monkeypatch.setattr(p0_flow_worker, "BASE_REVISION", base)
    external = tmp_path / "external"
    external.mkdir()
    claim = ClaimedRun(uuid4(), uuid4(), uuid4(), "caf-sample-app", base,
                       FIXTURE_PROMPT, ["structured plan"], "claude", 0, 1,
                       "caf-sample-fixture-v1")

    class Store:
        completed = None

        def database_name(self):
            return "caf_queue_test_offline"

        def quarantine_expired(self):
            return []

        def claim_queued(self, owner, lease_seconds=60):
            return claim

        def claim_control(self, run_id, owner):
            return True, False

        def renew_lease(self, run_id, owner, lease_seconds=60):
            return True

        def complete_claim(self, run_id, owner, status, artifact):
            self.completed = status, artifact
            return True

    class PlanCLI(FakeCLI):
        async def execute(self, task, workspace, session_id=None):
            self.calls.append(task)
            assert task.operation == "plan" and task.provider == "claude"
            if plan_text in ("REJECTED_ENVELOPE", "REJECTED_SURROGATE", "REJECTED_ARRAY"):
                surrogate = plan_text == "REJECTED_SURROGATE"
                array = plan_text == "REJECTED_ARRAY"
                return AgentResult(task.run_id, task.correlation_id, AgentStatus.FAILED,
                                   "safe", (), FailureClass.AGENT, exit_code=0,
                                   output={"final_text": None,
                                           "response_source": "rejected_envelope",
                                           "envelope_diagnostic": {
                                               "reason": ("root_type" if array else
                                                          "malformed_json" if surrogate
                                                          else "multiple_json_values"),
                                               "framing": ("single_json" if array else
                                                           "invalid_json" if surrogate
                                                           else "multiple_json_values"),
                                               "result_type": "string",
                                               "root_type": "array" if array else None,
                                               "array_length_capped": 2 if array else None,
                                               "array_result_count_capped": 1 if array else None,
                                               "array_result_position": "last" if array else None,
                                               "array_error_count_capped": 90000,
                                               "private_provider_field": "SECRET\\ud800"}})
            return AgentResult(task.run_id, task.correlation_id, AgentStatus.SUCCEEDED,
                               "safe", (), output={"final_text": plan_text,
                                                   "response_source": "structured_output"})

    store, claude = Store(), PlanCLI("claude")
    artifact = asyncio.run(p0_flow_worker.run_one_flow_fixture(
        store, "worker-caf-test", WorkspaceBinding(
            "caf-sample-fixture-v1", "caf-sample-app", root, base, external,
        ), _NoCodingRuntime(), claude, stop_after_plan=True,
    ))
    assert store.completed == (expected_status, artifact)
    assert artifact["execution_kind"] == "p0-queued-plan-only-fixture"
    assert artifact["terminal_diagnostic"]["result"] == expected_result
    assert artifact["terminal_diagnostic"]["response_source"] == (
        "rejected_envelope" if plan_text in (
            "REJECTED_ENVELOPE", "REJECTED_SURROGATE", "REJECTED_ARRAY")
        else "structured_output")
    assert artifact["terminal_diagnostic"]["stage_operations"] == ["plan"]
    if plan_text in ("REJECTED_ENVELOPE", "REJECTED_SURROGATE", "REJECTED_ARRAY"):
        assert artifact["terminal_diagnostic"]["category"] == "envelope"
        assert artifact["terminal_diagnostic"]["parser_outcome"] == "not_run"
        assert artifact["terminal_diagnostic"]["envelope"]["reason"] == (
            "root_type" if plan_text == "REJECTED_ARRAY" else
            "malformed_json" if plan_text == "REJECTED_SURROGATE"
            else "multiple_json_values")
        if plan_text == "REJECTED_ARRAY":
            assert artifact["terminal_diagnostic"]["envelope"]["array_result_count_capped"] == 1
            assert artifact["terminal_diagnostic"]["envelope"]["array_result_position"] == "last"
        assert artifact["terminal_diagnostic"]["envelope"]["array_error_count_capped"] is None
        assert "private_provider_field" not in json.dumps(artifact)
        assert "SECRET" not in json.dumps(artifact)
        assert "\\ud800" not in json.dumps(artifact)
    if "private_token" in plan_text:
        issue = artifact["terminal_diagnostic"]["schema_error"]
        assert (issue["schema_keyword"], issue["error_code"],
                issue["unknown_field_count"]) == ("additionalProperties", "unknown_fields", 1)
        assert "private_token" not in json.dumps(artifact)
        assert "SECRET" not in json.dumps(artifact)
    assert [stage["operation"] for stage in artifact["stages"]] == ["plan"]
    assert len(claude.calls) == 1
    assert all(field not in artifact for field in ("diff", "commit", "review", "gates"))
    assert subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                          capture_output=True, text=True, check=True).stdout == ""
    assert len(subprocess.run(["git", "-C", str(root), "worktree", "list", "--porcelain"],
                              capture_output=True, text=True, check=True).stdout
               .split("worktree ")) == 2

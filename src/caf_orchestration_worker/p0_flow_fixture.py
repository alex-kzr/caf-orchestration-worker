"""Stage adapter for the bounded CAF sample Flow; no CLI is launched on import."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

from caf_agent_runtime import (
    AgentResult, AgentStatus, CodingAgentRuntime, CodingTask, FailureClass,
)
from caf_agent_runtime.windows_job import ProcessTreeCleanupError

from .p0_code_worker import ALLOWED_FILE, FIXTURE_PROMPT, _gate, _git


FIXTURE_PROVIDERS = {
    "plan": "claude",
    "implement": "codex",
    "review": "claude",
    "correct": "codex",
    "test": "codex",  # The test stage is deterministic; no CLI is launched.
}

PLAN_SCHEMA = {
    "type": "object",
    "properties": {"steps": {"type": "array", "minItems": 1, "maxItems": 8,
                             "items": {"type": "string", "minLength": 1,
                                       "maxLength": 300}}},
    "required": ["steps"], "additionalProperties": False,
}


_FENCE = re.compile(r"\A```(?:json)?[ \t]*\r?\n(?P<body>.*?)\r?\n```\Z", re.I | re.S)
_AUTH_LIMIT = re.compile(r"(?i)(?:\brate\s+limit\b|\bquota\b|\b429\b|\bauth(?:entication)?\b|\blogin\b)")


class _DuplicateField(ValueError):
    pass


def _json_type(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    return "number"


def _schema_issue(keyword: str | None, code: str, path: str,
                  expected: str, actual: str,
                  unknown_count: int | None = 0) -> dict:
    """Only fixed schema identifiers and type names; never provider keys or values."""
    return {"schema_keyword": keyword, "error_code": code, "path": path,
            "expected_type": expected, "actual_type": actual,
            "unknown_field_count": unknown_count}


def _unique_fields(pairs: list[tuple[str, object]]) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateField
        value[key] = item
    return value


def _json_object(value: object) -> tuple[dict | None, str, str, dict | None]:
    """Parse only a complete object or a single complete JSON fence."""
    if not isinstance(value, str) or not value.strip():
        return None, "empty_output", "none", None
    stripped = value.strip()
    fmt = "plain_json"
    if stripped.startswith("```"):
        match = _FENCE.fullmatch(stripped)
        if match is None:
            return None, "invalid_json", "unsupported", None
        body = match.group("body")
        if re.search(r"(?m)^```", body):
            return None, "invalid_json", "unsupported", None
        stripped = body.strip()
        fmt = "fenced_json"
    try:
        parsed = json.loads(stripped, object_pairs_hook=_unique_fields)
    except json.JSONDecodeError:
        return None, "invalid_json", fmt, None
    except _DuplicateField:
        return None, "invalid_schema", fmt, _schema_issue(
            None, "duplicate_property", "$", "object", "object", None)
    if not isinstance(parsed, dict):
        return None, "invalid_schema", fmt, _schema_issue(
            "type", "root_type", "$", "object", _json_type(parsed), None)
    return parsed, "valid_json", fmt, None


def _parse_plan_detailed(value: object) -> tuple[list[str] | None, str, str, dict | None]:
    parsed, outcome, fmt, issue = _json_object(value)
    if parsed is None:
        return None, outcome, fmt, issue
    extras = len(set(parsed) - {"steps"})
    if "steps" not in parsed:
        return None, "invalid_schema", fmt, _schema_issue(
            "required", "missing_steps", "$.steps", "array", "missing", extras)
    if extras:
        return None, "invalid_schema", fmt, _schema_issue(
            "additionalProperties", "unknown_fields", "$", "object", "object", extras)
    steps = parsed["steps"]
    if not isinstance(steps, list):
        return None, "invalid_schema", fmt, _schema_issue(
            "type", "steps_type", "$.steps", "array", _json_type(steps))
    if not steps:
        return None, "invalid_schema", fmt, _schema_issue(
            "minItems", "too_few_steps", "$.steps", "array", "array")
    if len(steps) > 8:
        return None, "invalid_schema", fmt, _schema_issue(
            "maxItems", "too_many_steps", "$.steps", "array", "array")
    for step in steps:
        if not isinstance(step, str):
            return None, "invalid_schema", fmt, _schema_issue(
                "type", "step_type", "$.steps[]", "string", _json_type(step))
        if not step.strip():
            return None, "invalid_schema", fmt, _schema_issue(
                "minLength", "empty_step", "$.steps[]", "string", "string")
        if len(step) > 300 or len(step.strip()) > 300:
            return None, "invalid_schema", fmt, _schema_issue(
                "maxLength", "long_step", "$.steps[]", "string", "string")
    return [step.strip() for step in steps], "valid", fmt, None


def _parse_plan(value: object) -> tuple[list[str] | None, str, str]:
    steps, outcome, fmt, _ = _parse_plan_detailed(value)
    return steps, outcome, fmt


def _plan_steps(value: object) -> list[str] | None:
    return _parse_plan(value)[0]


def _parse_review(value: object) -> tuple[dict | None, str, str]:
    parsed, outcome, fmt, _ = _json_object(value)
    if parsed is None:
        return None, outcome, fmt
    findings = parsed.get("findings")
    if (set(parsed) != {"approved", "findings"}
            or not isinstance(parsed.get("approved"), bool)
            or not isinstance(findings, list) or len(findings) > 20
            or any(not isinstance(item, str) or len(item) > 2000 for item in findings)):
        return None, "invalid_schema", fmt
    return parsed, "valid", fmt


_ENVELOPE_REASONS = frozenset({
    "ok", "oversized_envelope", "empty_stdout", "invalid_utf8", "duplicate_keys",
    "trailing_data", "multiple_json_values", "malformed_json", "root_type",
    "provider_error", "invalid_error_flag", "unexpected_type_tag",
    "unexpected_subtype", "unexpected_status", "missing_structured_output",
    "structured_output_type", "unsupported_schema_root",
    "oversized_structured_output", "sensitive_structured_output",
})
_ENVELOPE_FRAMING = frozenset({
    "single_json", "oversized", "empty", "invalid_utf8", "duplicate_keys",
    "invalid_json", "trailing_data", "multiple_json_values",
})
_ENVELOPE_TYPES = frozenset({
    "missing", "null", "boolean", "object", "array", "string", "number",
})


def _safe_envelope(value: object) -> dict | None:
    """Rebuild runtime metadata from fixed sets; discard unknown provider data."""
    if not isinstance(value, dict):
        return None

    def allowed(name: str, choices: frozenset[str]) -> str | None:
        item = value.get(name)
        return item if type(item) is str and item in choices else None

    count = value.get("stdout_bytes_capped")
    return {
        "reason": allowed("reason", _ENVELOPE_REASONS),
        "framing": allowed("framing", _ENVELOPE_FRAMING),
        "root_type": allowed("root_type", _ENVELOPE_TYPES),
        "type_tag": allowed("type_tag", frozenset({"missing", "result", "other", "non_string"})),
        "subtype_tag": allowed("subtype_tag", frozenset({"missing", "success", "other", "non_string"})),
        "status_tag": allowed("status_tag", frozenset({"missing", "success", "error", "other", "non_string"})),
        "is_error": allowed("is_error", frozenset({"missing", "true", "false", "non_boolean"})),
        "result_type": allowed("result_type", _ENVELOPE_TYPES),
        "structured_output_type": allowed("structured_output_type", _ENVELOPE_TYPES),
        "nested_structured_output_type": allowed("nested_structured_output_type", _ENVELOPE_TYPES),
        "stdout_bytes_capped": (count if type(count) is int and 0 <= count <= 65537 else None),
    }


def _safe_diagnostic(task: CodingTask, result: AgentResult, outcome: str,
                     fmt: str, schema_issue: dict | None = None) -> dict:
    """A fixed-shape record; never copy stdout, stderr, prompts or CLI events."""
    output = result.output if isinstance(result.output, dict) else {}
    response = output.get("final_text")
    stderr = output.get("stderr_tail")
    runtime_category = output.get("diagnostic_category")
    envelope = _safe_envelope(output.get("envelope_diagnostic"))
    if result.status == AgentStatus.TIMED_OUT:
        category = "timeout"
    elif result.status == AgentStatus.CANCELLED:
        category = "cancelled"
    elif envelope is not None and envelope["reason"] not in (None, "ok") \
            and result.exit_code == 0:
        category = "envelope"
    elif runtime_category == "authentication" or (
            result.status == AgentStatus.FAILED and isinstance(stderr, str)
            and _AUTH_LIMIT.search(stderr)):
        category = "auth_limit"
    elif runtime_category == "network":
        category = "transport"
    elif runtime_category == "orphan_process":
        category = "process_cleanup"
    elif result.status == AgentStatus.FAILED and type(result.exit_code) is int and result.exit_code != 0:
        category = "process_exit"
    elif result.status == AgentStatus.FAILED:
        category = "unexpected_error"
    else:
        category = {"empty_output": "empty_output", "invalid_json": "json",
                    "invalid_schema": "schema", "valid": "ok"}.get(outcome, "unexpected_error")
    source = output.get("response_source")
    return {
        "phase": task.operation, "correlation_id": task.correlation_id,
        "category": category, "provider_status": result.status.value,
        "runtime_failure_class": result.failure_class.value if result.failure_class else None,
        "exit_code": result.exit_code if type(result.exit_code) is int else None,
        "captured_response_bytes": len(response.encode("utf-8")) if isinstance(response, str) else None,
        "input_format": fmt, "parser_outcome": outcome,
        "response_source": (source if source in {
            "none", "result_text", "structured_output", "missing_structured_output",
            "invalid_envelope", "oversized_envelope", "oversized_structured_output",
            "sensitive_structured_output", "rejected_envelope"}
                            else None),
        "envelope": envelope,
        "schema_error": schema_issue,
    }


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

    async def _call_claude(self, task: CodingTask, workspace: Path,
                           prompt: str) -> AgentResult:
        try:
            return await self.claude.execute(replace(task, prompt=prompt), workspace)
        except ProcessTreeCleanupError:
            raise  # An unverified child must not be acknowledged as stopped.
        except TimeoutError:
            return AgentResult(task.run_id, task.correlation_id, AgentStatus.TIMED_OUT,
                               "Claude stage timed out", (), FailureClass.TIMEOUT, output={})
        except Exception as exc:
            # Exception text may include paths, arguments or provider output.
            category = "network" if isinstance(exc, ConnectionError) else None
            return AgentResult(task.run_id, task.correlation_id, AgentStatus.FAILED,
                               "Claude stage raised " + type(exc).__name__, (),
                               FailureClass.INFRASTRUCTURE,
                               output={"diagnostic_category": category})

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
            result = await self._call_claude(task, workspace, prompt)
            envelope = _safe_envelope(result.output.get("envelope_diagnostic"))
            if envelope is not None and envelope["reason"] not in (None, "ok"):
                diagnostic = _safe_diagnostic(task, result, "not_run", "none")
                result = replace(result, status=AgentStatus.FAILED,
                                 failure_class=result.failure_class or FailureClass.AGENT,
                                 summary="structured envelope rejected",
                                 output={"diagnostic": diagnostic})
            elif result.status == AgentStatus.SUCCEEDED:
                steps, outcome, fmt, issue = _parse_plan_detailed(result.output.get("final_text"))
                diagnostic = _safe_diagnostic(task, result, outcome, fmt, issue)
                if steps is None:
                    result = replace(result, status=AgentStatus.FAILED,
                                     failure_class=FailureClass.AGENT,
                                     summary="invalid structured plan",
                                     output={"diagnostic": diagnostic})
                else:
                    result = replace(result, output={"steps": steps, "diagnostic": diagnostic})
            else:
                result = replace(result, output={"diagnostic":
                                 _safe_diagnostic(task, result, "not_run", "none")})
        else:
            diff = self._changed_diff(workspace)
            prompt = (
                "Review this CAF sample implementation diff without tools or edits. "
                "Requirements: " + FIXTURE_PROMPT + "\nThe unchanged acceptance suites run "
                "as deterministic gates after review. Reply only with one JSON object: "
                '{"approved":true,"findings":[]}; set approved false and give specific '
                "findings if the diff is wrong.\n\n" + diff
            )
            result = await self._call_claude(task, workspace, prompt)
            if result.status == AgentStatus.SUCCEEDED:
                verdict, outcome, fmt = _parse_review(result.output.get("final_text"))
                diagnostic = _safe_diagnostic(task, result, outcome, fmt)
                if verdict is None:
                    result = replace(result, status=AgentStatus.FAILED,
                                     failure_class=FailureClass.AGENT,
                                     summary="invalid structured review",
                                     output={"diagnostic": diagnostic})
                else:
                    result = replace(result, output={**verdict, "diagnostic": diagnostic})
            else:
                result = replace(result, output={"diagnostic":
                                 _safe_diagnostic(task, result, "not_run", "none")})
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

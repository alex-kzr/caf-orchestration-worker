"""Narrow authenticated loopback queue transport for a host worker."""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID

from caf_control_api.models import RunStatus
from caf_control_api.storage import ClaimedRun


class WorkerTransportError(RuntimeError):
    """The queue transport failed; no response body or credential is retained."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


class WorkerAPIStore:
    """Synchronous QueueStore over the existing localhost control API token."""

    def __init__(self, base_url: str, token: str) -> None:
        parsed = urlsplit(base_url)
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment
                or parsed.port is None):
            raise ValueError("worker API must be an explicit 127.0.0.1 HTTP port")
        if len(token) < 32:
            raise ValueError("scoped CAF worker token is required")
        self._base = f"http://127.0.0.1:{parsed.port}"
        self._token = token
        self._opener = build_opener(ProxyHandler({}), _NoRedirect)

    def _call(self, method: str, path: str, payload: dict | None = None):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            self._base + path, data=body, method=method,
            headers={"Authorization": f"Bearer {self._token}",
                     "Content-Type": "application/json"},
        )
        try:
            with self._opener.open(request, timeout=5) as response:
                return json.load(response)
        except HTTPError as exc:
            raise WorkerTransportError(f"queue API returned HTTP {exc.code}") from None
        except (URLError, OSError, ValueError, json.JSONDecodeError):
            raise WorkerTransportError("queue API unavailable or invalid response") from None

    def database_name(self) -> str:
        value = self._call("GET", "/v1/worker/database")
        if not isinstance(value, str):
            raise WorkerTransportError("invalid database identity response")
        return value

    def quarantine_expired(self) -> list[UUID]:
        return [UUID(item) for item in self._call("POST", "/v1/worker/quarantine-expired")]

    def claim_queued(self, owner: str, lease_seconds: int = 60) -> ClaimedRun | None:
        value = self._call("POST", "/v1/worker/claim",
                           {"owner": owner, "lease_seconds": lease_seconds})
        if value is None:
            return None
        try:
            return ClaimedRun(
                UUID(value["id"]), UUID(value["task_id"]), UUID(value["correlation_id"]),
                value["target_repository"], value["base_revision"], value["prompt"],
                value["acceptance_criteria"], value["provider"],
                value["max_corrections"], value["attempt"], value["workspace_id"],
            )
        except (KeyError, TypeError, ValueError):
            raise WorkerTransportError("invalid queue claim response") from None

    def claim_control(self, run_id: UUID, owner: str) -> tuple[bool, bool]:
        value = self._call("POST", f"/v1/worker/runs/{run_id}/control", {"owner": owner})
        if not isinstance(value, dict) or not all(
            isinstance(value.get(key), bool) for key in ("valid", "cancelled")
        ):
            raise WorkerTransportError("invalid lease control response")
        return value["valid"], value["cancelled"]

    def renew_lease(self, run_id: UUID, owner: str, lease_seconds: int = 60) -> bool:
        value = self._call("POST", f"/v1/worker/runs/{run_id}/renew",
                           {"owner": owner, "lease_seconds": lease_seconds})
        if not isinstance(value, dict) or not isinstance(value.get("renewed"), bool):
            raise WorkerTransportError("invalid lease renewal response")
        return value["renewed"]

    def complete_claim(self, run_id: UUID, owner: str, status: str, artifact: dict) -> bool:
        value = self._call("POST", f"/v1/worker/runs/{run_id}/complete",
                           {"owner": owner, "status": status, "artifact": artifact})
        if not isinstance(value, dict) or not isinstance(value.get("completed"), bool):
            raise WorkerTransportError("invalid completion response")
        return value["completed"]

    def get_run(self, run_id: UUID) -> RunStatus | None:
        try:
            value = self._call("GET", f"/v1/worker/runs/{run_id}")
        except WorkerTransportError as exc:
            if str(exc) == "queue API returned HTTP 404":
                return None
            raise
        try:
            return RunStatus.model_validate(value)
        except ValueError:
            raise WorkerTransportError("invalid Run response") from None

import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.error import URLError
from uuid import uuid4

import pytest

from caf_orchestration_worker.api_store import _NoRedirect, WorkerAPIStore, WorkerTransportError


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def test_worker_transport_is_loopback_only_and_keeps_token_in_header():
    for url in ("http://example.com:8123", "https://127.0.0.1:8123",
                "http://127.0.0.1:8123/path", "http://127.0.0.1:8123@evil.test"):
        with pytest.raises(ValueError):
            WorkerAPIStore(url, "x" * 32)
    store = WorkerAPIStore("http://127.0.0.1:8123", "x" * 32)
    seen = []

    class Opener:
        def open(self, request, timeout):
            seen.append((request.full_url, request.get_header("Authorization"), timeout))
            return Response(json.dumps("caf_queue_test_unit").encode())

    store._opener = Opener()
    assert store.database_name() == "caf_queue_test_unit"
    assert seen == [("http://127.0.0.1:8123/v1/worker/database", "Bearer " + "x" * 32, 5)]
    assert store._opener is not None
    assert _NoRedirect().redirect_request(None, None, 302, "Moved", {},
                                          "http://example.invalid") is None


def test_worker_transport_parses_claim_and_fails_closed_on_bad_response():
    store = WorkerAPIStore("http://127.0.0.1:8123", "x" * 32)
    run_id, task_id, correlation_id = uuid4(), uuid4(), uuid4()
    claim = {"id": str(run_id), "task_id": str(task_id),
             "correlation_id": str(correlation_id), "target_repository": "caf-sample-app",
             "workspace_id": "caf-sample-fixture-v1",
             "base_revision": "a" * 40, "prompt": "fixture", "acceptance_criteria": ["pass"],
             "provider": "codex", "max_corrections": 0, "attempt": 1}

    class Opener:
        def __init__(self, payload):
            self.payload = payload

        def open(self, request, timeout):
            return Response(json.dumps(self.payload).encode())

    store._opener = Opener(claim)
    parsed = store.claim_queued("worker-12345")
    assert parsed.id == run_id and parsed.workspace_id == "caf-sample-fixture-v1"
    store._opener = Opener({"invalid": True})
    with pytest.raises(WorkerTransportError, match="invalid queue claim"):
        store.claim_queued("worker-12345")
    assert store._opener is not None


def test_transport_failure_hides_credential_and_fails_closed():
    token = "secret-worker-token-" + "x" * 32
    store = WorkerAPIStore("http://127.0.0.1:8123", token)

    class Opener:
        def open(self, request, timeout):
            raise URLError("internal path or token should not escape")

    store._opener = Opener()
    with pytest.raises(WorkerTransportError) as caught:
        store.database_name()
    assert token not in str(caught.value)
    assert "internal path" not in str(caught.value)


def test_loopback_request_ignores_environment_proxy(monkeypatch):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append((self.path, self.headers.get("Authorization")))
            body = b'"caf_queue_test_proxy"'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    try:
        store = WorkerAPIStore(f"http://127.0.0.1:{server.server_port}", "x" * 32)
        assert store.database_name() == "caf_queue_test_proxy"
        assert received == [("/v1/worker/database", "Bearer " + "x" * 32)]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

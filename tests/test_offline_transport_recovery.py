"""Real loopback transport and disposable process tree, with no provider CLI."""

import ctypes
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock, Thread
from uuid import uuid4

import pytest
import uvicorn

from caf_agent_runtime import child_environment
from caf_control_api.app import Settings, create_app
from caf_control_api.storage import ClaimedRun
from caf_control_api.worker_grant import WorkerGrant
from caf_orchestration_worker.api_store import WorkerAPIStore


def _alive(pid: int) -> bool:
    kernel = ctypes.windll.kernel32
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    exit_code = ctypes.c_ulong()
    try:
        return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                    and exit_code.value == 259)
    finally:
        kernel.CloseHandle(handle)


class DurableFakeStore:
    """API-side state survives disposal of the separate worker process."""

    def __init__(self, grant: WorkerGrant):
        self.grant = grant
        self.correlation_id = uuid4()
        self.state = "queued"
        self.owner = None
        self.lease_until = 0.0
        self.journal = []
        self.lock = Lock()

    def database_name(self):
        return self.grant.database

    def run_in_scope(self, grant):
        return grant == self.grant

    def quarantine_expired_scoped(self, grant):
        with self.lock:
            if (grant == self.grant and self.state == "running"
                    and self.owner == grant.identity and self.lease_until <= time.monotonic()):
                self.state = "failed"
                self.owner = None
                self.journal.append("quarantined")
                return [grant.run_id]
            return []

    def claim_scoped(self, grant, lease_seconds=60):
        with self.lock:
            if grant != self.grant or self.state != "queued":
                return None
            self.state = "running"
            self.owner = grant.identity
            self.lease_until = time.monotonic() + lease_seconds
            self.journal.append("claimed")
            return ClaimedRun(grant.run_id, grant.task_id, self.correlation_id,
                              grant.repository, grant.base_revision, "offline process fixture",
                              ["No CLI"], grant.provider, 0, 1, grant.workspace_id)

    def claim_control(self, run_id, owner):
        with self.lock:
            return (run_id == self.grant.run_id and self.state == "running"
                    and owner == self.owner and self.lease_until > time.monotonic(), False)

    def renew_lease(self, run_id, owner, lease_seconds=60):
        with self.lock:
            if (run_id != self.grant.run_id or self.state != "running"
                    or owner != self.owner or self.lease_until <= time.monotonic()):
                return False
            self.lease_until = time.monotonic() + lease_seconds
            return True

    def complete_claim(self, run_id, owner, status, artifact):
        with self.lock:
            if (run_id != self.grant.run_id or self.state != "running"
                    or owner != self.owner or self.lease_until <= time.monotonic()):
                return False
            self.state = status
            self.owner = None
            self.journal.append("completed")
            return True


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object host-exit integration")
def test_worker_exit_quarantines_without_replay_over_http(tmp_path: Path):
    grant = WorkerGrant("offline-dummy-token-" + "x" * 32, "worker-offline-01",
                        "caf_queue_test_offline", uuid4(), uuid4(), "caf-sample-app",
                        "caf-synthetic-v1", "a" * 40, "codex",
                        datetime.now(timezone.utc) + timedelta(minutes=5))
    store = DurableFakeStore(grant)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(Settings("unused", "a" * 32, grant), store),
                                           host="127.0.0.1", port=port,
                                           log_level="critical", access_log=False))
    server_thread = Thread(target=server.run, daemon=True)
    server_thread.start()
    pid_file = tmp_path / "child.txt"
    child_script = tmp_path / "child.py"
    child_script.write_text(
        "import os,sys,time\nfrom pathlib import Path\n"
        "Path(sys.argv[1]).write_text(f'{os.getpid()}|{\"CAF_WORKER_TOKEN\" in os.environ}')\n"
        "time.sleep(60)\n", encoding="utf-8",
    )
    worker_script = tmp_path / "worker.py"
    worker_script.write_text(
        "import asyncio,os,sys\nfrom pathlib import Path\n"
        "from caf_agent_runtime import CodexRuntime,CodingTask\n"
        "from caf_orchestration_worker.api_store import WorkerAPIStore\n"
        "from caf_orchestration_worker.queue_runner import dispatch_once\n"
        "store=WorkerAPIStore(os.environ['CAF_WORKER_API_URL'],os.environ['CAF_WORKER_TOKEN'])\n"
        "runtime=CodexRuntime(executable=Path(sys.executable),timeout_seconds=70)\n"
        "runtime.command=lambda _: (sys.executable,os.environ['CAF_TEST_CHILD_SCRIPT'],"
        "os.environ['CAF_TEST_PID_FILE'])\n"
        "async def execute(run):\n"
        " result=await runtime.execute(CodingTask(str(run.id),str(run.correlation_id),"
        "'offline only','codex'),Path(os.environ['CAF_TEST_WORKSPACE']))\n"
        " return {'status':result.status.value}\n"
        "asyncio.run(dispatch_once(store,os.environ['CAF_WORKER_ID'],execute,"
        "lease_seconds=15,tick_seconds=.1,cleanup_seconds=2))\n",
        encoding="utf-8",
    )
    worker = None
    child_pid = None
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(.05)
        assert server.started
        env = child_environment()
        env.update({
            "CAF_WORKER_API_URL": f"http://127.0.0.1:{port}",
            "CAF_WORKER_TOKEN": grant.token,
            "CAF_WORKER_ID": grant.identity,
            "CAF_TEST_CHILD_SCRIPT": str(child_script),
            "CAF_TEST_PID_FILE": str(pid_file),
            "CAF_TEST_WORKSPACE": str(tmp_path),
        })
        worker = subprocess.Popen([sys.executable, str(worker_script)],
                                  cwd=tmp_path, env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(150):
            if pid_file.exists() and store.state == "running":
                break
            time.sleep(.05)
        assert pid_file.exists() and store.journal == ["claimed"]
        child_pid_text, token_present = pid_file.read_text().split("|")
        child_pid = int(child_pid_text)
        assert token_present == "False"
        assert _alive(child_pid)
        worker.kill()
        worker.wait(timeout=10)
        for _ in range(100):
            if not _alive(child_pid):
                break
            time.sleep(.05)
        assert not _alive(child_pid)
        with store.lock:
            store.lease_until = time.monotonic() - 1
        client = WorkerAPIStore(f"http://127.0.0.1:{port}", grant.token)
        assert client.quarantine_expired() == [grant.run_id]
        assert client.claim_queued(grant.identity, 15) is None
        assert store.state == "failed" and store.journal == ["claimed", "quarantined"]
    finally:
        if worker is not None and worker.poll() is None:
            worker.kill()
            worker.wait(timeout=10)
        if child_pid is not None and _alive(child_pid):
            subprocess.run(["taskkill", "/PID", str(child_pid), "/T", "/F"],
                           env=child_environment(), capture_output=True, timeout=10)
        server.should_exit = True
        server_thread.join(timeout=10)

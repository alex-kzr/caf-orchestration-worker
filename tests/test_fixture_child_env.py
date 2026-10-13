"""Fixture Git and acceptance checks never inherit worker transport secrets."""

from pathlib import Path
from subprocess import CompletedProcess

from caf_orchestration_worker import p0_code_worker


def test_fixture_git_and_gate_receive_minimal_environment(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CAF_WORKER_TOKEN", "dummy-worker-token")
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-model-token")
    seen = []

    def run(*args, **kwargs):
        seen.append(kwargs["env"])
        return CompletedProcess(args, 0, stdout="ok", stderr="")

    monkeypatch.setattr(p0_code_worker.subprocess, "run", run)
    assert p0_code_worker._git(tmp_path, "status", "--porcelain") == "ok"
    assert p0_code_worker._gate(tmp_path, "test_baseline.py")["passed"]
    assert len(seen) == 2
    assert all("CAF_WORKER_TOKEN" not in env and "OPENAI_API_KEY" not in env
               for env in seen)
    assert seen[1]["PYTHONPATH"] == str(tmp_path / "src")

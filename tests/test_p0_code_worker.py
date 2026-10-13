"""Guardrails for the opt-in real-code fixture without invoking coding CLIs."""

import asyncio
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from caf_control_api.storage import ClaimedRun
from caf_orchestration_worker import p0_code_worker
from caf_orchestration_worker.p0_code_worker import _git, _review_verdict


def test_git_preserves_porcelain_status_prefix(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "fixture.txt").write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "fixture.txt"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=CAF Test",
                    "-c", "user.email=caf-test@example.invalid", "commit", "-qm", "base"], check=True)
    (tmp_path / "fixture.txt").write_text("after\n", encoding="utf-8")
    assert _git(tmp_path, "status", "--porcelain") == " M fixture.txt"


def test_review_parser_accepts_json_wrapping_but_rejects_missing_verdict() -> None:
    verdict = {"approved": True, "findings": []}
    assert _review_verdict('{"approved":true,"findings":[]}') == verdict
    assert _review_verdict('```json\n{"approved":true,"findings":[]}\n```') == verdict
    assert _review_verdict('Review:\n{"approved":true,"findings":[]}') == verdict
    assert _review_verdict('The change looks good') is None
    assert _review_verdict('{"approved":"yes","findings":[]}') is None


def test_real_cli_fixture_rejects_arbitrary_prompt_before_launch(tmp_path: Path, monkeypatch) -> None:
    class Store:
        def __init__(self):
            self.claimed = False
            self.completed = None

        def database_name(self):
            return "caf_queue_test_unit"

        def quarantine_expired(self):
            return []

        def claim_queued(self, owner, lease_seconds=60):
            if self.claimed:
                return None
            self.claimed = True
            return ClaimedRun(uuid4(), uuid4(), uuid4(), "caf-sample-app",
                              p0_code_worker.BASE_REVISION, "arbitrary command", [],
                              "codex", 0, 1)

        def claim_control(self, run_id, owner):
            return True, False

        def renew_lease(self, run_id, owner, lease_seconds=60):
            return True

        def complete_claim(self, run_id, owner, status, artifact):
            self.completed = status, artifact
            return True

    repository = tmp_path / "repo"
    repository.mkdir()
    monkeypatch.setattr(p0_code_worker, "_git", lambda _, *args:
                        p0_code_worker.BASE_REVISION if args == ("rev-parse", "HEAD") else "")
    store = Store()
    result = asyncio.run(p0_code_worker.run_one_code_fixture(
        store, "worker-12345", repository, tmp_path / "worktrees",
        Path(sys.executable),  # type: ignore[arg-type]
    ))
    assert result == {"status": "blocked", "reason": "fixture_prompt_mismatch"}
    assert store.completed == ("failed", result)

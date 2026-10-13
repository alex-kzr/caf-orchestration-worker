import asyncio
from uuid import uuid4

import pytest

from caf_control_api.storage import ClaimedRun
from caf_orchestration_worker.queue_proof import LostLeaseError, run_synthetic_claim_once


class FakeQueue:
    def __init__(self, claim, owns=True, database="caf_queue_test_unit"):
        self.claim = claim
        self.owns = owns
        self.database = database
        self.completions = []

    def database_name(self):
        return self.database

    def claim_queued(self, owner, lease_seconds=60):
        result, self.claim = self.claim, None
        return result

    def complete_claim(self, run_id, owner, status, artifact):
        self.completions.append((run_id, owner, status, artifact))
        return self.owns


def sample_claim():
    return ClaimedRun(
        id=uuid4(), task_id=uuid4(), correlation_id=uuid4(),
        target_repository="caf-sample-app", base_revision="a" * 40,
        prompt="Synthetic queue proof", acceptance_criteria=["Artifact persisted"],
        provider="codex", max_corrections=1, attempt=1,
    )


def test_synthetic_queue_handoff_preserves_identity_and_artifact(tmp_path):
    claim = sample_claim()
    store = FakeQueue(claim)
    artifact = asyncio.run(run_synthetic_claim_once(store, "worker-test-1", {"caf-sample-app": tmp_path}))
    assert artifact["execution_kind"] == "synthetic-queue-proof"
    assert artifact["run_id"] == str(claim.id)
    assert artifact["correlation_id"] == str(claim.correlation_id)
    assert artifact["base_revision"] == claim.base_revision
    assert [stage["operation"] for stage in artifact["stages"]] == ["plan", "implement", "review", "test"]
    assert store.completions == [(claim.id, "worker-test-1", "succeeded", artifact)]
    assert asyncio.run(run_synthetic_claim_once(store, "worker-test-1", {"caf-sample-app": tmp_path})) is None


def test_unknown_repository_fails_without_runtime(tmp_path):
    claim = sample_claim()
    store = FakeQueue(claim)
    artifact = asyncio.run(run_synthetic_claim_once(store, "worker-test-2", {"other": tmp_path}))
    assert artifact["reason"] == "repository_not_allowed"
    assert store.completions[0][2] == "failed"
    assert "stages" not in artifact


def test_lost_lease_is_not_reported_as_completion(tmp_path):
    store = FakeQueue(sample_claim(), owns=False)
    with pytest.raises(LostLeaseError):
        asyncio.run(run_synthetic_claim_once(store, "worker-test-3", {"caf-sample-app": tmp_path}))


def test_shared_database_is_rejected_before_claim(tmp_path):
    claim = sample_claim()
    store = FakeQueue(claim, database="caf")
    with pytest.raises(ValueError, match="dedicated"):
        asyncio.run(run_synthetic_claim_once(store, "worker-test-4", {"caf-sample-app": tmp_path}))
    assert store.claim is claim
    assert store.completions == []

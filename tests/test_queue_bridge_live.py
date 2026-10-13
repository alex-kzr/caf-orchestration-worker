"""Opt-in queue → worker → synthetic runtime proof in a dedicated database."""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from caf_control_api.models import CreateTask
from caf_control_api.storage import PostgresStore
from caf_orchestration_worker.queue_proof import run_synthetic_claim_once


@unittest.skipUnless(os.getenv("CAF_QUEUE_TEST_LIVE") == "1", "live database opt-in required")
class LiveQueueBridgeTest(unittest.TestCase):
    def test_one_synthetic_run_is_persisted(self):
        dsn = os.environ["CAF_QUEUE_TEST_DATABASE_URL"]
        expected = urlparse(dsn).path.lstrip("/")
        if not expected.startswith("caf_queue_test_"):
            raise ValueError("a dedicated caf_queue_test_* database is required")
        store = PostgresStore(dsn)
        store.open()
        run = None
        try:
            with store.pool.connection() as conn:
                actual = conn.execute("SELECT current_database()").fetchone()[0]
                self.assertEqual(actual, expected)
                self.assertEqual(conn.execute("SELECT count(*) FROM runs").fetchone()[0], 0)
            task = CreateTask(
                target_repository="caf-sample-app", base_revision="a" * 40,
                prompt="Synthetic queue bridge proof; no source edit",
                acceptance_criteria=["Structured artifact persists"], provider="codex",
            )
            run = store.submit(task, f"bridge-test-{uuid4().hex}")
            with tempfile.TemporaryDirectory(prefix="caf-queue-proof-") as workspace:
                artifact = asyncio.run(run_synthetic_claim_once(
                    store, f"worker-{uuid4().hex}",
                    {"caf-sample-app": Path(workspace)},
                ))
            recorded = store.get_run(run.run_id)
            self.assertEqual(recorded.status, "succeeded")
            self.assertEqual(recorded.attempt, 1)
            self.assertEqual(recorded.artifact, artifact)
            self.assertEqual(artifact["execution_kind"], "synthetic-queue-proof")
            self.assertEqual(artifact["correlation_id"], str(run.correlation_id))
            self.assertEqual(
                [stage["operation"] for stage in artifact["stages"]],
                ["plan", "implement", "review", "test"],
            )
        finally:
            if run is not None:
                with store.pool.connection() as conn, conn.transaction():
                    conn.execute("DELETE FROM runs WHERE id = %s", (run.run_id,))
                    conn.execute("DELETE FROM tasks WHERE id = %s", (run.task_id,))
            store.close()


if __name__ == "__main__":
    unittest.main()

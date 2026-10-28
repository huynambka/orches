"""Claimed connections: CRUD guards, automatic release, API conflict responses."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orches.models import AgentStatus, utc_now
from orches.state import ConnectionUseError, StateStore

from test_connection_tools import make_running_run, start_running_invocation


class ClaimStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = StateStore(Path(self.tmp.name) / "state.sqlite3")
        self.record = self.state.create_connection(name="box", os="linux", cred="secret")
        self.connection_id = self.record["connection_id"]
        _, self.run_id = make_running_run(self.state, Path(self.tmp.name) / "proj")
        self.invocation = start_running_invocation(self.state, self.run_id, "node_" + "a" * 32)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_claimed_connection_rejects_update_and_delete(self) -> None:
        self.state.claim_connection(self.connection_id, self.invocation)
        with self.assertRaises(ConnectionUseError):
            self.state.update_connection(self.connection_id, name="renamed")
        with self.assertRaises(ConnectionUseError):
            self.state.delete_connection(self.connection_id)
        record = self.state.get_connection(self.connection_id)
        self.assertEqual(record["name"], "box")
        self.assertTrue(record["in_use"])

    def test_unclaimed_crud_unchanged(self) -> None:
        updated = self.state.update_connection(self.connection_id, name="renamed")
        self.assertEqual(updated["name"], "renamed")
        self.assertFalse(updated["in_use"])
        self.assertIsNone(updated["used_by"])
        self.assertTrue(self.state.delete_connection(self.connection_id))
        self.assertIsNone(self.state.get_connection(self.connection_id))

    def test_invocation_end_releases_claim(self) -> None:
        for status in (AgentStatus.COMPLETED, AgentStatus.FAILED):
            invocation = start_running_invocation(self.state, self.run_id, f"node_{status.value}")
            self.state.claim_connection(self.connection_id, invocation)
            self.assertTrue(self.state.get_connection(self.connection_id)["in_use"])
            self.state.update_invocation(
                invocation, status=status, finished_at=utc_now(), returncode=0,
                error=None, output_artifact_id=None, stderr_artifact_id=None,
            )
            record = self.state.get_connection(self.connection_id)
            self.assertFalse(record["in_use"])
            self.assertIsNone(record["used_by"])

    def test_terminal_run_releases_claims(self) -> None:
        self.state.claim_connection(self.connection_id, self.invocation)
        self.state.update_run(self.run_id, "blocked", "worker failed")
        self.assertFalse(self.state.get_connection(self.connection_id)["in_use"])

    def test_interrupted_api_run_recovery_releases_claims(self) -> None:
        self.state.claim_connection(self.connection_id, self.invocation)
        self.state.fail_interrupted_api_runs()
        self.assertEqual(self.state.get_run(self.run_id)["status"], "failed")
        self.assertFalse(self.state.get_connection(self.connection_id)["in_use"])

    def test_claim_requires_running_run(self) -> None:
        self.state.update_run(self.run_id, "completed")
        with self.assertRaises(ConnectionUseError) as caught:
            self.state.claim_connection(self.connection_id, self.invocation)
        self.assertEqual(str(caught.exception), "agent invocation is not running")


class ClaimApiTest(unittest.TestCase):
    def setUp(self) -> None:
        try:
            from fastapi.testclient import TestClient
        except ImportError:  # pragma: no cover - optional web extra
            self.skipTest("fastapi test client is not installed")
        from orches.api import create_app
        from orches.config import AppConfig

        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        config = AppConfig(
            root=root, state_db=root / "state.sqlite3", runs_root=root / "runs",
            profiles_db=root / "profiles.sqlite3",
        )
        self.state = StateStore(config.state_db)
        self.record = self.state.create_connection(name="box", os="linux", cred="secret")
        self.client = TestClient(create_app(config))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.addCleanup(self.tmp.cleanup)

    def _claim(self) -> None:
        _, run_id = make_running_run(self.state, Path(self.tmp.name) / "proj")
        invocation = start_running_invocation(self.state, run_id, "node_" + "a" * 32)
        self.state.claim_connection(self.record["connection_id"], invocation)

    def test_connection_listing_reports_usage(self) -> None:
        self._claim()
        payload = self.client.get("/api/connections").json()
        (entry,) = payload["connections"]
        self.assertNotIn("cred", entry)
        self.assertTrue(entry["in_use"])
        self.assertEqual(entry["used_by"]["role"], "node_" + "a" * 32)

    def test_update_and_delete_of_claimed_connection_conflict(self) -> None:
        self._claim()
        connection_id = self.record["connection_id"]
        patched = self.client.patch(f"/api/connections/{connection_id}", json={"name": "x"})
        self.assertEqual(patched.status_code, 409)
        self.assertEqual(patched.json()["detail"], "connection is in use")
        deleted = self.client.delete(f"/api/connections/{connection_id}")
        self.assertEqual(deleted.status_code, 409)

    def test_unclaimed_connection_mutations_succeed(self) -> None:
        connection_id = self.record["connection_id"]
        patched = self.client.patch(f"/api/connections/{connection_id}", json={"name": "x"})
        self.assertEqual(patched.status_code, 200)
        self.assertEqual(patched.json()["name"], "x")
        self.assertFalse(patched.json()["in_use"])
        deleted = self.client.delete(f"/api/connections/{connection_id}")
        self.assertEqual(deleted.status_code, 200)


if __name__ == "__main__":
    unittest.main()

"""Connection address-book host tools with exclusive agent claims."""
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from orches.connection_tools import (
    CONNECTION_TOOL_DEFINITIONS,
    CONNECTION_TOOL_NAMES,
    handle_connection_tool,
    masked_connections,
)
from orches.models import AgentInvocation, AgentStatus, utc_now
from orches.orchestrator import PipelineController
from orches.pipeline_profiles import PipelineProfile, PipelineStep
from orches.state import ConnectionUseError, StateStore


def make_running_run(state: StateStore, directory: Path, name: str = "proj") -> tuple[str, str]:
    directory.mkdir(parents=True, exist_ok=True)
    project_id = state.create_project(project_directory=str(directory), name=name)
    project = state.get_project(project_id)
    run_id, _ = state.reserve_project_run(
        project_id, expected_updated_at=project["updated_at"], profile_json="{}",
    )
    return project_id, run_id


def start_running_invocation(state: StateStore, run_id: str, role: str) -> int:
    return state.start_invocation(
        AgentInvocation(
            run_id=run_id, role=role, model_selector="m", thinking="medium",
            status=AgentStatus.RUNNING, started_at=utc_now(), prompt_version="w1",
        )
    )


class ConnectionToolsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "state.sqlite3"
        self.state = StateStore(self.db_path)
        self.record = self.state.create_connection(
            name="build box", os="linux", ip="10.0.0.5", cred="user:hunter2",
        )
        _, self.run_id = make_running_run(self.state, Path(self.tmp.name) / "proj")
        self.worker_a = start_running_invocation(self.state, self.run_id, "node_" + "a" * 32)
        self.worker_b = start_running_invocation(self.state, self.run_id, "node_" + "b" * 32)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def _call(self, name: str, arguments: dict, invocation_id: int) -> dict:
        return await handle_connection_tool(
            self.state, name, arguments, caller="t", invocation_id=invocation_id,
        )

    async def test_masked_list_never_contains_credentials(self) -> None:
        result = await self._call("read_connections", {}, self.worker_a)
        self.assertEqual(result["connections"], masked_connections(self.state))
        (entry,) = result["connections"]
        self.assertNotIn("cred", entry)
        self.assertTrue(entry["has_cred"])
        self.assertEqual(entry["ip"], "10.0.0.5")
        self.assertFalse(entry["in_use"])
        self.assertIsNone(entry["used_by"])

    async def test_claim_reveal_release_lifecycle(self) -> None:
        connection_id = self.record["connection_id"]
        claimed = await self._call("claim_connection", {"id": connection_id}, self.worker_a)
        self.assertTrue(claimed["in_use"])
        self.assertEqual(claimed["used_by"]["role"], "node_" + "a" * 32)
        self.assertEqual(claimed["used_by"]["run_id"], self.run_id)
        self.assertNotIn("cred", claimed)

        # Live usage is visible to everyone, including a second store on the same file.
        other_store = StateStore(self.db_path)
        (listed,) = masked_connections(other_store)
        self.assertTrue(listed["in_use"])
        self.assertEqual(listed["used_by"]["run_id"], self.run_id)

        # The competitor can neither claim nor reveal.
        blocked = await self._call("claim_connection", {"id": connection_id}, self.worker_b)
        self.assertTrue(blocked["_is_error"])
        self.assertEqual(blocked["error"], "connection is in use")
        peek = await self._call("reveal_connection", {"id": connection_id}, self.worker_b)
        self.assertTrue(peek["_is_error"])
        self.assertEqual(peek["error"], "connection is not claimed by this agent")
        steal = await self._call("release_connection", {"id": connection_id}, self.worker_b)
        self.assertTrue(steal["_is_error"])
        self.assertEqual(steal["error"], "connection is not claimed by this agent")

        # The owner reclaims idempotently and reveals the stored credential.
        again = await self._call("claim_connection", {"id": connection_id}, self.worker_a)
        self.assertTrue(again["in_use"])
        revealed = await self._call("reveal_connection", {"id": connection_id}, self.worker_a)
        self.assertEqual(revealed["cred"], "user:hunter2")
        self.assertEqual(revealed["name"], "build box")

        # Release frees the connection for the competitor.
        released = await self._call("release_connection", {"id": connection_id}, self.worker_a)
        self.assertFalse(released["in_use"])
        self.assertIsNone(released["used_by"])
        self.assertNotIn("cred", released)
        rerelease = await self._call("release_connection", {"id": connection_id}, self.worker_a)
        self.assertTrue(rerelease["_is_error"])
        self.assertEqual(rerelease["error"], "connection is not claimed by this agent")
        taken = await self._call("claim_connection", {"id": connection_id}, self.worker_b)
        self.assertTrue(taken["in_use"])
        revealed_b = await self._call("reveal_connection", {"id": connection_id}, self.worker_b)
        self.assertEqual(revealed_b["cred"], "user:hunter2")

    async def test_reveal_requires_claim(self) -> None:
        result = await self._call(
            "reveal_connection", {"id": self.record["connection_id"]}, self.worker_a,
        )
        self.assertTrue(result["_is_error"])
        self.assertEqual(result["error"], "connection is not claimed by this agent")

    async def test_dead_invocation_cannot_claim(self) -> None:
        self.state.update_invocation(
            self.worker_a, status=AgentStatus.FAILED, finished_at=utc_now(), returncode=1,
            error="boom", output_artifact_id=None, stderr_artifact_id=None,
        )
        result = await self._call(
            "claim_connection", {"id": self.record["connection_id"]}, self.worker_a,
        )
        self.assertTrue(result["_is_error"])
        self.assertEqual(result["error"], "agent invocation is not running")
        unknown_invocation = await self._call(
            "claim_connection", {"id": self.record["connection_id"]}, 10_000,
        )
        self.assertTrue(unknown_invocation["_is_error"])
        self.assertEqual(unknown_invocation["error"], "agent invocation is not running")

    async def test_invalid_arguments_and_unknown_ids_error(self) -> None:
        bad_args = await self._call("read_connections", {"x": 1}, self.worker_a)
        self.assertTrue(bad_args["_is_error"])
        for name in ("claim_connection", "release_connection", "reveal_connection"):
            missing = await self._call(name, {}, self.worker_a)
            self.assertTrue(missing["_is_error"])
            unknown = await self._call(name, {"id": "nope"}, self.worker_a)
            self.assertTrue(unknown["_is_error"])
        self.assertEqual(
            (await self._call("claim_connection", {"id": "nope"}, self.worker_a))["error"],
            "unknown connection id",
        )

    async def test_concurrent_claims_have_one_winner(self) -> None:
        connection_id = self.record["connection_id"]
        barrier = threading.Barrier(2)
        outcomes: list[str] = []
        lock = threading.Lock()

        def contend(invocation_id: int) -> None:
            store = StateStore(self.db_path)
            barrier.wait()
            try:
                store.claim_connection(connection_id, invocation_id)
                outcome = "won"
            except ConnectionUseError as exc:
                outcome = str(exc)
            with lock:
                outcomes.append(outcome)

        threads = [
            threading.Thread(target=contend, args=(invocation,))
            for invocation in (self.worker_a, self.worker_b)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(outcomes), ["connection is in use", "won"])
        (entry,) = masked_connections(self.state)
        self.assertTrue(entry["in_use"])

    async def test_root_controller_dispatches_connection_tools(self) -> None:
        state = self.state
        run_id = self.run_id

        class Owner:
            pass

        owner = Owner()
        owner.state = state
        profile = PipelineProfile(
            id="p", name="P", steps=(PipelineStep(id="node_" + "c" * 32, name="C", model_selector="m"),),
        )
        controller = PipelineController(owner, profile, run_id, {}, {"stages": {}}, None)  # type: ignore[arg-type]
        self.assertTrue(
            {d["name"] for d in controller.tool_definitions()} >= set(CONNECTION_TOOL_NAMES),
        )
        unbound = await controller.handle_tool("read_connections", {})
        self.assertTrue(unbound["_is_error"])

        root_invocation = start_running_invocation(state, run_id, "root")
        controller.bind_root_invocation(root_invocation)
        listed = await controller.handle_tool("read_connections", {})
        self.assertEqual(len(listed["connections"]), 1)
        claimed = await controller.handle_tool(
            "claim_connection", {"id": self.record["connection_id"]},
        )
        self.assertTrue(claimed["in_use"])
        revealed = await controller.handle_tool(
            "reveal_connection", {"id": self.record["connection_id"]},
        )
        self.assertEqual(revealed["cred"], "user:hunter2")

    def test_tool_definitions_are_well_formed(self) -> None:
        self.assertEqual(
            {d["name"] for d in CONNECTION_TOOL_DEFINITIONS}, set(CONNECTION_TOOL_NAMES),
        )
        for definition in CONNECTION_TOOL_DEFINITIONS:
            self.assertIn("name", definition)
            self.assertIn("description", definition)
            self.assertEqual(definition["parameters"]["type"], "object")


if __name__ == "__main__":
    unittest.main()

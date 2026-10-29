"""Resuming a finished run: reusable-node selection, controller seeding, persistence."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from orches.models import AgentInvocation, AgentResult, AgentStatus, utc_now
from orches.orchestrator import PipelineController
from orches.pipeline_profiles import PipelineProfile, PipelineStep
from orches.state import SCHEMA_VERSION, StateStore

from test_auth import install_admin_password, login

A, B, C, D = ("node_" + ch * 32 for ch in "abcd")


def _diamond() -> PipelineProfile:
    """A -> B, A -> C, (B, C) -> D."""
    return PipelineProfile(id="p", name="P", steps=(
        PipelineStep(id=A, name="A", model_selector="m"),
        PipelineStep(id=B, name="B", model_selector="m", depends_on=(A,)),
        PipelineStep(id=C, name="C", model_selector="m", depends_on=(A,)),
        PipelineStep(id=D, name="D", model_selector="m", depends_on=(B, C)),
    ))


class ReusableNodesTest(unittest.TestCase):
    def test_reuses_only_dependency_closed_completed_nodes(self) -> None:
        profile = _diamond()
        self.assertEqual(profile.reusable_nodes({A, B}), {A, B})
        # C completed but its dependency A did not: nothing upstream to trust.
        self.assertEqual(profile.reusable_nodes({C}), set())
        self.assertEqual(profile.reusable_nodes({A, B, C, D}), {A, B, C, D})

    def test_rerun_excludes_the_node_and_every_dependent(self) -> None:
        profile = _diamond()
        self.assertEqual(profile.reusable_nodes({A, B, C, D}, rerun={B}), {A, C})
        self.assertEqual(profile.reusable_nodes({A, B, C, D}, rerun={A}), set())
        with self.assertRaises(ValueError):
            profile.reusable_nodes({A}, rerun={"node_" + "f" * 32})

    def test_unchanged_nodes_ignore_presentation_only_edits(self) -> None:
        snapshot = _diamond()
        edited = PipelineProfile(id="p", name="P renamed", steps=(
            PipelineStep(id=A, name="A moved", model_selector="m", position=(5.0, 5.0)),
            PipelineStep(id=B, name="B", model_selector="other", depends_on=(A,)),
            PipelineStep(id=C, name="C", model_selector="m", depends_on=(A,), thinking="high"),
            PipelineStep(id=D, name="D", model_selector="m", depends_on=(B,)),
        ))
        self.assertEqual(edited.unchanged_nodes(snapshot), {A})
        self.assertEqual(snapshot.unchanged_nodes(snapshot), {A, B, C, D})


class _RejectingMailbox:
    async def handle(self, name: str, arguments: dict[str, Any], invocation_id: int) -> dict[str, Any]:
        raise AssertionError(f"unexpected agent message tool {name}")


class _Runner:
    def __init__(self) -> None:
        self.started: list[str] = []

    async def run_agent(self, step: Any, context: dict[str, Any], run_id: str, **_: Any) -> AgentResult:
        self.started.append(step.id)
        return AgentResult(
            run_id=run_id, role=step.id, status=AgentStatus.COMPLETED,
            output={"text": f"fresh {step.name}", "deps": context["dependency_outputs"]},
            raw_output="", stderr="", error=None, model_selector="m",
            duration_seconds=0.0, artifact_ids=(),
        )


class _Owner:
    def __init__(self) -> None:
        self.runner = _Runner()

    @staticmethod
    def _result_summary(result: AgentResult) -> dict[str, Any]:
        return {"status": result.status.value, "output": result.output}


class SeededControllerTest(unittest.IsolatedAsyncioTestCase):
    async def test_seeded_nodes_are_completed_readable_and_not_rerunnable(self) -> None:
        profile = _diamond()
        report: dict[str, Any] = {"stages": {}}
        owner = _Owner()
        controller = PipelineController(owner, profile, "run2", {}, report, _RejectingMailbox(), {
            A: {"output": {"text": "old A"}, "artifact_ids": ["art-a"]},
            B: {"output": {"text": "old B"}, "artifact_ids": []},
        })
        status = await controller.handle_tool("get_status", {})
        self.assertEqual(status["statuses"][A], "completed")
        self.assertEqual(status["ready"], [C])

        read = await controller.handle_tool("read_agent_output", {"id": A})
        self.assertEqual(read["output"], {"text": "old A"})
        self.assertEqual(read["artifact_ids"], ["art-a"])
        self.assertTrue(read["reused"])
        self.assertTrue(report["stages"][A]["reused"])

        refused = await controller.handle_tool("run_agent", {"id": A, "input_data": {}})
        self.assertTrue(refused.get("_is_error"))
        self.assertIn("previous run", refused["error"])

        self.assertEqual(await controller.handle_tool("run_agent", {"id": C, "input_data": {}}), {"id": C, "status": "running"})
        # D depends on C, which has not finished yet; the host refuses it until then.
        early = await controller.handle_tool("run_agent", {"id": D, "input_data": {}})
        self.assertIn("dependencies are not complete", early["error"])
        await controller.tasks[C]
        ran_c = await controller.handle_tool("read_agent_output", {"id": C})
        self.assertEqual(ran_c["output"]["deps"], {A: {"text": "old A"}})
        await controller.handle_tool("run_agent", {"id": D, "input_data": {}})
        await controller.tasks[D]
        ran_d = await controller.handle_tool("read_agent_output", {"id": D})
        # D sees the carried-over B output and the fresh C output.
        self.assertEqual(ran_d["output"]["deps"][B], {"text": "old B"})
        self.assertEqual(ran_d["output"]["deps"][C]["text"], "fresh C")
        self.assertEqual(owner.runner.started, [C, D])
        self.assertFalse(ran_d.get("reused"))


class PersistenceTest(unittest.TestCase):
    def _run(self, state: StateStore, directory: Path) -> str:
        directory.mkdir(parents=True, exist_ok=True)
        project_id = state.create_project(project_directory=str(directory), name="proj")
        project = state.get_project(project_id)
        run_id, _ = state.reserve_project_run(
            project_id, expected_updated_at=project["updated_at"], profile_json="{}",
        )
        return run_id

    def test_output_json_round_trips_and_carries_over(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = StateStore(root / "state.sqlite3")
            run_id = self._run(state, root / "proj")
            invocation_id = state.start_invocation(AgentInvocation(
                run_id=run_id, role=A, model_selector="m", thinking="low",
                status=AgentStatus.RUNNING, started_at=utc_now(), prompt_version="w1",
            ))
            state.update_invocation(
                invocation_id, status=AgentStatus.COMPLETED, finished_at=utc_now(), returncode=0,
                error=None, output_artifact_id=None, stderr_artifact_id=None,
                output_json=json.dumps({"text": "done"}),
            )
            (row,) = state.list_invocations(run_id)
            self.assertEqual(json.loads(row["output_json"]), {"text": "done"})
            state.update_run(run_id, "failed", "worker B failed")

            project = state.get_project(row and state.get_run(run_id)["project_id"])
            new_run, old = state.reserve_project_run(
                project["project_id"], expected_updated_at=project["updated_at"], profile_json="{}",
            )
            self.assertEqual(old, (run_id,))
            state.carry_over_invocations(new_run, [row])
            (carried,) = state.list_invocations(new_run)
            self.assertEqual(carried["status"], "completed")
            self.assertEqual(carried["prompt_version"], "reused")
            self.assertEqual(carried["thinking"], "low")
            self.assertEqual(json.loads(carried["output_json"]), {"text": "done"})
            with self.assertRaises(KeyError):
                state.carry_over_invocations("missing", [row])

    def test_v2_store_gains_output_json_column(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite3"
            StateStore(path)
            conn = sqlite3.connect(path)
            with conn:
                conn.execute("ALTER TABLE agent_invocations DROP COLUMN output_json")
                conn.execute("PRAGMA user_version=2")
            conn.close()
            StateStore(path)
            conn = sqlite3.connect(path)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(agent_invocations)")}
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            conn.close()
            self.assertIn("output_json", columns)
            self.assertEqual(version, SCHEMA_VERSION)


class _RecordingOrchestrator:
    """Stands in for `build_orchestrator`: records what a run would execute, then completes it."""

    def __init__(self, state: StateStore, artifacts: Any) -> None:
        self.state, self.artifacts = state, artifacts
        self.runs: list[dict[str, Any]] = []

    async def run(self, **kwargs: Any) -> None:
        self.runs.append(kwargs)
        run_id, profile = kwargs["run_id"], kwargs["profile"]
        seeded = set(kwargs.get("completed_outputs") or {})
        for step in profile.steps:
            if step.id in seeded:
                continue
            invocation = self.state.start_invocation(AgentInvocation(
                run_id=run_id, role=step.id, model_selector=step.model_selector, thinking=step.thinking,
                status=AgentStatus.RUNNING, started_at=utc_now(), prompt_version="w1",
            ))
            self.state.update_invocation(
                invocation, status=AgentStatus.COMPLETED, finished_at=utc_now(), returncode=0, error=None,
                output_artifact_id=None, stderr_artifact_id=None, output_json=json.dumps({"text": step.name}),
            )
        self.state.update_run(run_id, "completed")


class ResumeApiTest(unittest.TestCase):
    """Start and Resume both execute the project's saved profile."""

    def setUp(self) -> None:
        try:
            from fastapi.testclient import TestClient
        except ImportError:  # pragma: no cover - optional web extra
            self.skipTest("fastapi test client is not installed")
        from orches.api import create_app
        from orches.artifacts import ArtifactStore
        from orches.config import AppConfig
        from orches.profile_store import ProfileStore
        from orches.project_files import ensure_project_marker

        install_admin_password(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        config = AppConfig(
            root=root, state_db=root / "state.sqlite3", runs_root=root / "runs",
            profiles_db=root / "profiles.sqlite3",
        )
        self.state = StateStore(config.state_db)
        self.profiles = ProfileStore(config.profiles_db)
        self.orchestrator = _RecordingOrchestrator(self.state, ArtifactStore(config.runs_root, self.state))
        patcher = mock.patch("orches.cli.build_orchestrator", return_value=self.orchestrator)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.profile_id = self.profiles.create("P")["id"]
        self._save_profile([("A", "old", ()), ("B", "old", (A,))])
        directory = root / "proj"
        directory.mkdir()
        self.project_id = self.state.create_project(
            project_directory=str(directory), name="proj", target="build it", pipeline_profile=self.profile_id,
        )
        ensure_project_marker(directory, self.project_id)
        self.client = TestClient(create_app(config))
        login(self.client)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def _save_profile(self, steps: list[tuple[str, str, tuple[str, ...]]], profile_id: str | None = None) -> None:
        profile_id = profile_id or self.profile_id
        record = self.profiles.get(profile_id)
        definition = {
            "id": profile_id, "name": record["name"],
            "steps": [
                {"id": {"A": A, "B": B, "C": C}[name], "name": name, "model_selector": model, "prompt": "",
                 "depends_on": list(deps), "position": {"x": 0, "y": 0}, "thinking": "medium"}
                for name, model, deps in steps
            ],
        }
        self.profiles.update(profile_id, definition, record["revision"])

    def _wait_terminal(self, run_id: str) -> dict[str, Any]:
        for _ in range(100):
            run = self.client.get(f"/api/runs/{run_id}").json()
            if run["run"]["status"] != "running":
                return run
            time.sleep(0.02)
        self.fail("run did not finish")

    def _start(self) -> str:
        response = self.client.post(f"/api/projects/{self.project_id}/runs", json={})
        self.assertEqual(response.status_code, 202, response.text)
        run_id = response.json()["run_id"]
        self._wait_terminal(run_id)
        return run_id

    def test_resume_executes_edited_nodes_with_the_saved_profile(self) -> None:
        first = self._start()
        self.assertEqual({step.model_selector for step in self.orchestrator.runs[0]["profile"].steps}, {"old"})

        self._save_profile([("A", "old", ()), ("B", "new", (A,))])
        response = self.client.post(f"/api/runs/{first}/resume", json={})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["reused"], [A])
        second = response.json()["run_id"]
        detail = self._wait_terminal(second)

        launched = self.orchestrator.runs[1]
        self.assertEqual(launched["profile"].step(B).model_selector, "new")
        self.assertEqual(set(launched["completed_outputs"]), {A})
        self.assertEqual(detail["pipeline_profile"]["steps"][1]["model_selector"], "new")
        agents = {item["role"]: item for item in detail["agents"]}
        self.assertTrue(agents[A]["reused"])
        self.assertFalse(agents[B]["reused"])
        self.assertEqual(agents[B]["model_selector"], "new")

    def test_resume_reruns_dependents_of_an_edited_node(self) -> None:
        self._save_profile([("A", "old", ()), ("B", "old", (A,)), ("C", "old", (B,))])
        first = self._start()
        self._save_profile([("A", "new", ()), ("B", "old", (A,)), ("C", "old", (B,))])
        response = self.client.post(f"/api/runs/{first}/resume", json={})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["reused"], [])
        self._wait_terminal(response.json()["run_id"])

    def test_resume_uses_a_newly_selected_profile(self) -> None:
        first = self._start()
        other = self.profiles.create("Q")["id"]
        self._save_profile([("C", "other", ())], profile_id=other)
        self.state.update_project(self.project_id, pipeline_profile=other)
        response = self.client.post(f"/api/runs/{first}/resume", json={})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["reused"], [])
        detail = self._wait_terminal(response.json()["run_id"])
        self.assertEqual(detail["pipeline_profile"]["id"], other)

    def test_resume_requires_a_saved_profile(self) -> None:
        first = self._start()
        self.state.update_project(self.project_id, pipeline_profile=None)
        response = self.client.post(f"/api/runs/{first}/resume", json={})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.state.get_run(first)["status"], "completed")


if __name__ == "__main__":
    unittest.main()

"""Resuming a finished run: reusable-node selection, controller seeding, persistence."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any

from orches.models import AgentInvocation, AgentResult, AgentStatus, utc_now
from orches.orchestrator import PipelineController
from orches.pipeline_profiles import PipelineProfile, PipelineStep
from orches.state import StateStore

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


class _Runner:
    def __init__(self) -> None:
        self.started: list[str] = []

    async def run_agent(self, step: Any, context: dict[str, Any], run_id: str) -> AgentResult:
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
        controller = PipelineController(owner, profile, "run2", {}, report, {
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

        ran_c = await controller.handle_tool("run_agent", {"id": C, "input_data": {}})
        self.assertEqual(ran_c["output"]["deps"], {A: {"text": "old A"}})
        ran_d = await controller.handle_tool("run_agent", {"id": D, "input_data": {}})
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
            self.assertEqual(version, 3)


if __name__ == "__main__":
    unittest.main()

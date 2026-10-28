"""Fail-fast behavior once a worker failure makes the pipeline unwinnable."""
from __future__ import annotations

import asyncio
import unittest
from typing import Any

from orches import orchestrator as orch_mod
from orches.models import AgentResult, AgentStatus
from orches.orchestrator import Orchestrator, PipelineController
from orches.pipeline_profiles import PipelineProfile, PipelineStep


def _profile() -> PipelineProfile:
    step_a = PipelineStep(id="node_" + "a" * 32, name="A", model_selector="m")
    step_b = PipelineStep(id="node_" + "b" * 32, name="B", model_selector="m")
    return PipelineProfile(id="p", name="P", steps=(step_a, step_b))


class _FailingRunner:
    async def run_agent(self, step: Any, context: dict[str, Any], run_id: str) -> AgentResult:
        return AgentResult(
            run_id=run_id, role=step.id, status=AgentStatus.FAILED,
            output=None, raw_output="", stderr="", error="Tool call denied by user: bash",
            model_selector="m", duration_seconds=0.0, artifact_ids=(),
        )


class _Owner:
    def __init__(self) -> None:
        self.runner = _FailingRunner()

    @staticmethod
    def _result_summary(result: AgentResult) -> dict[str, Any]:
        return {"status": result.status.value, "error": result.error}


class FatalControllerTest(unittest.IsolatedAsyncioTestCase):
    async def test_worker_failure_marks_fatal_and_blocks_everything(self) -> None:
        profile = _profile()
        controller = PipelineController(_Owner(), profile, "run1", {}, {"stages": {}})
        node_a, node_b = profile.steps[0].id, profile.steps[1].id

        result = await controller.handle_tool("run_agent", {"id": node_a, "input_data": {}})
        self.assertTrue(result.get("_is_error"))
        self.assertIn("Tool call denied", result["error"])
        self.assertTrue(controller.fatal.is_set())
        self.assertEqual(controller.statuses[node_a], "failed")
        self.assertIn("denied", controller.fatal_error)

        blocked = await controller.handle_tool("run_agent", {"id": node_b, "input_data": {}})
        self.assertTrue(blocked.get("_is_error"))
        self.assertIn("fatally blocked", blocked["error"])
        self.assertEqual(controller.statuses[node_b], "pending")

        finish = await controller.handle_tool("finish_pipeline", {})
        self.assertTrue(finish.get("_is_error"))
        self.assertIn("fatally blocked", finish["error"])
        self.assertFalse(controller.finish_requested)

        status = await controller.handle_tool("get_status", {})
        self.assertEqual(status["fatal_error"], controller.fatal_error)


class _FakeSession:
    def __init__(self, root_future: "asyncio.Future[Any]", abort_ends_root: bool) -> None:
        self._root = root_future
        self._abort_ends_root = abort_ends_root
        self.abort_calls = 0
        self.close_calls = 0

    async def abort(self) -> None:
        self.abort_calls += 1
        if self._abort_ends_root and not self._root.done():
            self._root.set_result(None)

    async def close(self) -> None:
        self.close_calls += 1
        if not self._root.done():
            self._root.set_result(None)


class FatalWatchdogTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._grace = (orch_mod.FATAL_ROOT_GRACE_SECONDS, orch_mod.FATAL_ABORT_GRACE_SECONDS)
        orch_mod.FATAL_ROOT_GRACE_SECONDS = 0.05
        orch_mod.FATAL_ABORT_GRACE_SECONDS = 0.05
        self.orch = Orchestrator(None, None, None, "unused")  # type: ignore[arg-type]

    def tearDown(self) -> None:
        orch_mod.FATAL_ROOT_GRACE_SECONDS, orch_mod.FATAL_ABORT_GRACE_SECONDS = self._grace

    def _controller(self) -> PipelineController:
        return PipelineController(_Owner(), _profile(), "run1", {}, {"stages": {}})

    async def _root_task(self) -> tuple["asyncio.Future[Any]", asyncio.Task[Any]]:
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()

        async def root() -> Any:
            return await future

        return future, asyncio.create_task(root())

    async def test_root_yielding_within_grace_avoids_abort(self) -> None:
        controller = self._controller()
        future, root_task = await self._root_task()
        session = _FakeSession(future, abort_ends_root=True)
        controller._mark_fatal("boom")
        watchdog = asyncio.create_task(self.orch._fatal_watchdog(controller, root_task, [session]))
        future.set_result(None)
        await asyncio.wait_for(watchdog, timeout=2)
        self.assertEqual(session.abort_calls, 0)
        self.assertEqual(session.close_calls, 0)
        await root_task

    async def test_stuck_root_is_aborted(self) -> None:
        controller = self._controller()
        future, root_task = await self._root_task()
        session = _FakeSession(future, abort_ends_root=True)
        controller._mark_fatal("boom")
        await asyncio.wait_for(
            self.orch._fatal_watchdog(controller, root_task, [session]), timeout=2
        )
        self.assertEqual(session.abort_calls, 1)
        self.assertEqual(session.close_calls, 0)
        await root_task

    async def test_abort_resistant_root_is_closed(self) -> None:
        controller = self._controller()
        future, root_task = await self._root_task()
        session = _FakeSession(future, abort_ends_root=False)
        controller._mark_fatal("boom")
        await asyncio.wait_for(
            self.orch._fatal_watchdog(controller, root_task, [session]), timeout=2
        )
        self.assertEqual(session.abort_calls, 1)
        self.assertEqual(session.close_calls, 1)
        await root_task

    async def test_watchdog_waits_for_inflight_workers(self) -> None:
        controller = self._controller()
        future, root_task = await self._root_task()
        session = _FakeSession(future, abort_ends_root=True)
        worker_gate: asyncio.Future[Any] = asyncio.get_running_loop().create_future()

        async def worker() -> dict[str, Any]:
            return await worker_gate

        controller.tasks["node_" + "b" * 32] = asyncio.create_task(worker())
        controller._mark_fatal("boom")
        watchdog = asyncio.create_task(self.orch._fatal_watchdog(controller, root_task, [session]))
        await asyncio.sleep(0.2)
        self.assertEqual(session.abort_calls, 0, "must not abort while a worker is in flight")
        worker_gate.set_result({})
        await asyncio.wait_for(watchdog, timeout=2)
        self.assertEqual(session.abort_calls, 1)
        await root_task


if __name__ == "__main__":
    unittest.main()

"""Cost tracing: per-request ledger, lifetime project/model totals, RPC capture."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from orches.artifacts import ArtifactStore
from orches.agent_runtime import AgentRunner
from orches.costs import UNKNOWN_MODEL, parse_cost_event
from orches.models import AgentInvocation, AgentRole, AgentStatus, RoleConfig, utc_now
from orches.omp import OmpClient
from orches.pipeline_profiles import PipelineStep
from orches.state import StateStore

from test_auth import install_admin_password, login
from test_connection_tools import make_running_run, start_running_invocation

NODE = "node_" + "a" * 32


def _message(ident: str, provider: str | None, model: str | None, cost: Any = 0.0, **usage: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"role": "assistant", "content": [{"type": "text", "text": "hi"}], "stopReason": "stop"}
    if provider is not None:
        body["provider"] = provider
    if model is not None:
        body["model"] = model
    if usage or cost is not None:
        body["usage"] = {**usage, "cost": {"total": cost}}
    return {"type": "message_end", "messageId": ident, "message": body}


class ParserTest(unittest.TestCase):
    def test_assistant_message_usage_is_normalized(self) -> None:
        event = _message("m1", "anthropic", "A", 0.003, input=100, output=20, cacheRead=0, cacheWrite=0, totalTokens=120)
        self.assertEqual(parse_cost_event(event, 7), {
            "event_key": "message:m1", "provider": "anthropic", "model": "A",
            "input_tokens": 100, "output_tokens": 20, "cache_read_tokens": 0,
            "cache_write_tokens": 0, "total_tokens": 120, "cost_usd": 0.003,
        })

    def test_missing_id_uses_index_and_missing_usage_is_unpriced(self) -> None:
        event = {"type": "message_end", "message": {"role": "assistant", "content": []}}
        parsed = parse_cost_event(event, 3)
        self.assertEqual(parsed["event_key"], "message:3")
        self.assertIsNone(parsed["cost_usd"])
        self.assertIsNone(parsed["input_tokens"])

    def test_malformed_values_become_unknown(self) -> None:
        event = _message("m", "p", "m", "free", input=-1, output=True, totalTokens=float("nan"))
        parsed = parse_cost_event(event, 1)
        self.assertIsNone(parsed["cost_usd"])
        self.assertIsNone(parsed["input_tokens"])
        self.assertIsNone(parsed["output_tokens"])
        self.assertIsNone(parsed["total_tokens"])
        for bad in (float("inf"), -0.5, None):
            self.assertIsNone(parse_cost_event(_message("m", "p", "m", bad), 1)["cost_usd"])
        self.assertEqual(parse_cost_event(_message("m", "p", "m", 0), 1)["cost_usd"], 0.0)

    def test_other_events_are_ignored(self) -> None:
        self.assertIsNone(parse_cost_event({"type": "message_update", "message": {"role": "assistant"}}, 1))
        self.assertIsNone(parse_cost_event({"type": "message_end", "message": {"role": "user"}}, 1))
        self.assertIsNone(parse_cost_event({"type": "agent_end", "messages": []}, 1))
        self.assertIsNone(parse_cost_event({"type": "user_steer", "text": "x"}, 1))
        self.assertIsNone(parse_cost_event({"type": "cache_warming_end", "provider": "p", "model": "m", "outcome": "hit"}, 1))

    def test_cache_warming_with_usage_is_a_request(self) -> None:
        event = {"type": "cache_warming_end", "provider": "anthropic", "model": "A", "outcome": "miss",
                 "usage": {"input": 5, "cacheWrite": 5, "cost": {"total": 0.0005}}}
        parsed = parse_cost_event(event, 9)
        self.assertEqual(parsed["event_key"], "warming:9")
        self.assertEqual((parsed["provider"], parsed["model"], parsed["cost_usd"]), ("anthropic", "A", 0.0005))
        self.assertEqual(parsed["cache_write_tokens"], 5)


class LedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = StateStore(self.root / "state.sqlite3")
        self.project_p, self.run_p = make_running_run(self.state, self.root / "p", "P")
        self.project_q, self.run_q = make_running_run(self.state, self.root / "q", "Q")
        self.worker_p = start_running_invocation(self.state, self.run_p, NODE)
        self.worker_q = start_running_invocation(self.state, self.run_q, NODE)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _record(self, invocation: int, event: dict[str, Any], index: int = 1) -> None:
        self.state.record_model_usage(invocation, parse_cost_event(event, index))

    def _seed(self) -> None:
        self._record(self.worker_p, _message("m1", "anthropic", "A", 0.003, input=100, output=20, totalTokens=120))
        self._record(self.worker_p, _message("m2", "anthropic", "B", 0.002, input=10, output=5, totalTokens=15))
        self._record(self.worker_q, _message("m1", "anthropic", "A", 0.007, input=50, output=50, totalTokens=100))

    def test_totals_per_project_and_model(self) -> None:
        self._seed()
        rows = self.state.list_model_costs()
        by_key = {(row["project_name"], row["model"]): row for row in rows}
        self.assertEqual(set(by_key), {("P", "anthropic/A"), ("P", "anthropic/B"), ("Q", "anthropic/A")})
        self.assertAlmostEqual(by_key[("P", "anthropic/A")]["cost_usd"], 0.003)
        self.assertEqual(by_key[("P", "anthropic/A")]["input_tokens"], 100)
        self.assertEqual(by_key[("P", "anthropic/A")]["requests"], 1)
        self.assertEqual(by_key[("P", "anthropic/A")]["unpriced_requests"], 0)
        project_totals = {}
        model_totals = {}
        for row in rows:
            project_totals[row["project_name"]] = project_totals.get(row["project_name"], 0) + row["cost_usd"]
            model_totals[row["model"]] = model_totals.get(row["model"], 0) + row["cost_usd"]
        self.assertAlmostEqual(project_totals["P"], 0.005)
        self.assertAlmostEqual(project_totals["Q"], 0.007)
        self.assertAlmostEqual(model_totals["anthropic/A"], 0.010)
        self.assertAlmostEqual(model_totals["anthropic/B"], 0.002)
        self.assertEqual([row["project_name"] for row in rows], ["P", "P", "Q"])

    def test_zero_cost_differs_from_unpriced(self) -> None:
        self._record(self.worker_p, _message("free", "anthropic", "A", 0, input=1, output=1))
        self._record(self.worker_p, {"type": "message_end", "messageId": "nousage", "message": {"role": "assistant", "content": []}}, 2)
        (row,) = [r for r in self.state.list_model_costs() if r["project_name"] == "P" and r["model"] == "anthropic/A"]
        self.assertEqual(row["cost_usd"], 0.0)
        self.assertEqual(row["requests"], 1)
        (unknown,) = [r for r in self.state.list_model_costs() if r["model"] == "m"]
        self.assertIsNone(unknown["cost_usd"])
        self.assertEqual((unknown["requests"], unknown["unpriced_requests"]), (1, 1))
        self.assertIsNone(unknown["input_tokens"])

    def test_model_falls_back_to_selector_then_unknown(self) -> None:
        self._record(self.worker_p, _message("a", None, None, 0.001))
        self._record(self.worker_p, _message("b", "anthropic", None, 0.001))
        anonymous = self.state.start_invocation(
            AgentInvocation(
                run_id=self.run_p, role="node_" + "b" * 32, model_selector=None, thinking="medium",
                status=AgentStatus.RUNNING, started_at=utc_now(), prompt_version="w1",
            )
        )
        self._record(anonymous, _message("c", None, "A", 0.001))
        models = {row["model"]: row["requests"] for row in self.state.list_model_costs()}
        self.assertEqual(models, {"m": 2, UNKNOWN_MODEL: 1})

    def test_duplicate_event_key_is_recorded_once(self) -> None:
        self._record(self.worker_p, _message("m1", "anthropic", "A", 0.003))
        self._record(self.worker_p, _message("m1", "anthropic", "A", 0.003))
        self._record(self.worker_p, {"type": "message_end", "message": {"role": "assistant", "content": []}}, 5)
        self._record(self.worker_p, {"type": "message_end", "message": {"role": "assistant", "content": []}}, 5)
        requests = {row["model"]: row["requests"] for row in self.state.list_model_costs()}
        self.assertEqual(requests, {"anthropic/A": 1, "m": 1})
        # Same message id on a different invocation is a different request.
        self._record(self.worker_q, _message("m1", "anthropic", "A", 0.003))
        self.assertEqual(sum(r["requests"] for r in self.state.list_model_costs()), 3)

    def test_unknown_invocation_is_rejected(self) -> None:
        with self.assertRaises(KeyError):
            self.state.record_model_usage(999, parse_cost_event(_message("m", "p", "m", 0.1), 1))
        self.assertEqual(self.state.list_model_costs(), [])

    def test_run_agent_costs_only_reports_live_invocations(self) -> None:
        self._seed()
        usage = self.state.run_agent_costs(self.run_p)
        self.assertEqual(set(usage), {self.worker_p})
        self.assertEqual(usage[self.worker_p]["requests"], 2)
        self.assertAlmostEqual(usage[self.worker_p]["cost_usd"], 0.005)
        self.assertEqual(self.state.run_agent_costs("missing"), {})

    def test_ledger_survives_new_run_and_resume_clone(self) -> None:
        self._seed()
        self.state.update_invocation(
            self.worker_p, status=AgentStatus.COMPLETED, finished_at=utc_now(), returncode=0,
            error=None, output_artifact_id=None, stderr_artifact_id=None, output_json='{"text":"x"}',
        )
        self.state.update_run(self.run_p, "completed")
        (old,) = self.state.list_invocations(self.run_p)
        project = self.state.get_project(self.project_p)
        new_run, removed = self.state.reserve_project_run(
            self.project_p, expected_updated_at=project["updated_at"], profile_json="{}",
        )
        self.assertEqual(removed, (self.run_p,))
        self.state.carry_over_invocations(new_run, [old])
        before = self.state.list_model_costs()
        self.assertAlmostEqual(sum(r["cost_usd"] for r in before if r["project_name"] == "P"), 0.005)
        (reused,) = self.state.list_invocations(new_run)
        self.assertEqual(reused["prompt_version"], "reused")
        self.assertEqual(self.state.run_agent_costs(new_run), {})

    def test_project_deletion_drops_its_ledger(self) -> None:
        self._seed()
        self.state.update_run(self.run_p, "failed", "x")
        self.assertTrue(self.state.delete_project(self.project_p))
        names = {row["project_name"] for row in self.state.list_model_costs()}
        self.assertEqual(names, {"Q"})

    def test_v3_store_gains_ledger_and_keeps_invocations(self) -> None:
        path = self.root / "old.sqlite3"
        StateStore(path)
        conn = sqlite3.connect(path)
        with conn:
            conn.execute("DROP TABLE model_cost_events")
            conn.execute("PRAGMA user_version=3")
        conn.close()
        store = StateStore(path)
        _, run_id = make_running_run(store, self.root / "old", "Old")
        invocation = start_running_invocation(store, run_id, NODE)
        store.record_model_usage(invocation, parse_cost_event(_message("m", "p", "m", 0.5), 1))
        self.assertEqual(store.list_model_costs()[0]["cost_usd"], 0.5)
        self.assertEqual(len(store.list_invocations(run_id)), 1)


class CostApiTest(unittest.TestCase):
    def setUp(self) -> None:
        try:
            from fastapi.testclient import TestClient
        except ImportError:  # pragma: no cover - optional web extra
            self.skipTest("fastapi test client is not installed")
        from orches.api import create_app
        from orches.config import AppConfig

        install_admin_password(self)
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        config = AppConfig(
            root=root, state_db=root / "state.sqlite3", runs_root=root / "runs",
            profiles_db=root / "profiles.sqlite3",
        )
        self.state = StateStore(config.state_db)
        self.project, self.run_id = make_running_run(self.state, root / "proj", "P")
        self.worker = start_running_invocation(self.state, self.run_id, NODE)
        self.state.record_model_usage(self.worker, parse_cost_event(_message("m1", "anthropic", "A", 0.003, input=100, output=20), 1))
        self.state.record_model_usage(self.worker, parse_cost_event({"type": "message_end", "message": {"role": "assistant", "content": []}}, 2))
        self.state.update_invocation(
            self.worker, status=AgentStatus.COMPLETED, finished_at=utc_now(), returncode=0,
            error=None, output_artifact_id=None, stderr_artifact_id=None, output_json='{"text":"x"}',
        )
        self.state.carry_over_invocations(self.run_id, [{**self.state.list_invocations(self.run_id)[0], "role": "node_" + "b" * 32}])
        # The API process marks interrupted API runs failed at startup; the ledger is unaffected.
        self.client = TestClient(create_app(config))
        login(self.client)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.addCleanup(self.tmp.cleanup)

    def test_costs_endpoint_lists_project_model_rows(self) -> None:
        payload = self.client.get("/api/costs").json()
        self.assertEqual(len(payload["rows"]), 2)
        row = next(r for r in payload["rows"] if r["model"] == "anthropic/A")
        self.assertEqual(row["project_id"], self.project)
        self.assertEqual(row["project_name"], "P")
        self.assertAlmostEqual(row["cost_usd"], 0.003)
        self.assertEqual((row["requests"], row["unpriced_requests"], row["input_tokens"]), (1, 0, 100))
        unpriced = next(r for r in payload["rows"] if r["model"] == "m")
        self.assertIsNone(unpriced["cost_usd"])
        self.assertEqual(unpriced["unpriced_requests"], 1)

    def test_run_agents_carry_usage_and_reused_is_null(self) -> None:
        for path in (f"/api/runs/{self.run_id}", f"/api/runs/{self.run_id}/agents"):
            agents = {item["role"]: item for item in self.client.get(path).json()["agents"]}
            self.assertEqual(agents[NODE]["usage"]["requests"], 2)
            self.assertEqual(agents[NODE]["usage"]["unpriced_requests"], 1)
            self.assertAlmostEqual(agents[NODE]["usage"]["cost_usd"], 0.003)
            self.assertTrue(agents["node_" + "b" * 32]["reused"])
            self.assertIsNone(agents["node_" + "b" * 32]["usage"])


FAKE_OMP = textwrap.dedent(
    """
    import json, os, sys

    def emit(obj):
        sys.stdout.write(json.dumps(obj) + "\\n"); sys.stdout.flush()

    def message(ident, provider, model, cost, text):
        return {"type": "message_end", "messageId": ident, "message": {
            "role": "assistant", "provider": provider, "model": model,
            "content": [{"type": "text", "text": text}], "stopReason": "stop",
            "usage": {"input": 100, "output": 20, "cacheRead": 0, "cacheWrite": 0,
                      "totalTokens": 120, "cost": {"total": cost}}}}

    emit({"type": "ready", "protocolVersion": 1})
    for line in sys.stdin:
        cmd = json.loads(line)
        kind = cmd.get("type")
        if kind == "prompt":
            emit({"type": "response", "id": cmd.get("id"), "command": kind, "success": True})
            emit(message("m1", "anthropic", "model-a", 0.003, "first"))
            emit(message("m1", "anthropic", "model-a", 0.003, "first again"))
            emit({"type": "cache_warming_end", "phase": "streaming", "provider": "anthropic",
                  "model": "model-a", "outcome": "miss",
                  "usage": {"input": 5, "cacheWrite": 5, "cost": {"total": 0.0005}}})
            emit(message("m2", "openai", "model-b", 0.002, "done"))
            if os.environ.get("FAKE_OMP_MODE") == "drop":
                sys.exit(0)
            emit({"type": "agent_end", "isTerminal": True, "stopReason": "stop",
                  "messages": [message("m2", "openai", "model-b", 0.002, "done")["message"]]})
        elif cmd.get("id") is not None:
            emit({"type": "response", "id": cmd.get("id"), "command": kind, "success": True, "data": {}})
    """
)


class RpcCaptureTest(unittest.IsolatedAsyncioTestCase):
    """Drive the real OmpRpcSession reader with a stdio child that speaks the RPC protocol."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fake = self.root / "fake_omp.py"
        self.fake.write_text(FAKE_OMP, encoding="utf-8")
        self.state = StateStore(self.root / "state.sqlite3")
        self.project, self.run_id = make_running_run(self.state, self.root / "proj", "P")
        self.client = OmpClient(omp_bin=sys.executable)
        real_exec = asyncio.create_subprocess_exec

        async def launch(*args: Any, **kwargs: Any) -> Any:
            return await real_exec(sys.executable, "-u", str(self.fake), **kwargs)

        patcher = mock.patch("orches.omp.asyncio.create_subprocess_exec", side_effect=launch)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def _runner(self) -> AgentRunner:
        return AgentRunner(
            self.client, self.state, ArtifactStore(self.root / "runs", self.state), self.root / "runs",
            {AgentRole.ROOT: RoleConfig(model_selector="anthropic/root", timeout_seconds=20)},
        )

    def _expect_ledger(self, invocation_id: int) -> None:
        other = StateStore(self.root / "state.sqlite3")
        usage = other.run_agent_costs(self.run_id)[invocation_id]
        self.assertEqual(usage["requests"], 3)
        self.assertAlmostEqual(usage["cost_usd"], 0.0055)
        self.assertEqual(usage["input_tokens"], 205)
        rows = {row["model"]: row for row in other.list_model_costs()}
        self.assertEqual(rows["anthropic/model-a"]["requests"], 2)
        self.assertAlmostEqual(rows["anthropic/model-a"]["cost_usd"], 0.0035)
        self.assertAlmostEqual(rows["openai/model-b"]["cost_usd"], 0.002)

    async def test_worker_requests_are_recorded(self) -> None:
        step = PipelineStep(id=NODE, name="A", model_selector="anthropic/worker", thinking="low")
        result = await self._runner().run_agent(step, {"project_directory": str(self.root / "proj")}, self.run_id)
        self.assertEqual(result.status, AgentStatus.COMPLETED, result.error)
        self.assertEqual(result.output, {"text": "done"})
        (invocation,) = self.state.list_invocations(self.run_id)
        self._expect_ledger(invocation["id"])

    async def test_root_requests_are_recorded(self) -> None:
        async def handler(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            return {}

        bound: list[int] = []
        result = await self._runner().run_root(
            {"project_directory": str(self.root / "proj"), "objective": "x"}, self.run_id,
            host_tools=[], host_tool_handler=handler, worker_count=1,
            on_invocation_started=bound.append,
        )
        self.assertEqual(result.status, AgentStatus.COMPLETED, result.error)
        self._expect_ledger(bound[0])

    async def test_completed_requests_survive_a_dropped_session(self) -> None:
        invocation = start_running_invocation(self.state, self.run_id, NODE)
        runner = self._runner()
        with mock.patch.dict(os.environ, {"FAKE_OMP_MODE": "drop"}):
            session = await self.client.start_session(
                "go", timeout=20, usage_event_handler=runner._usage_recorder(invocation),
            )
        await asyncio.wait_for(session._reader_done.wait(), timeout=10)
        await session.close()
        self.assertIsNone(session._agent_end)
        self._expect_ledger(invocation)


if __name__ == "__main__":
    unittest.main()

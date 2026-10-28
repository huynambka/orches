"""Two-way agent inbox: questions and replies between live agents of one run."""
from __future__ import annotations

import asyncio
import sqlite3
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from orches.agent_messages import AGENT_MESSAGE_TOOL_NAMES, AgentMailbox
from orches.agent_runtime import AgentRunner
from orches.artifacts import ArtifactStore
from orches.models import AgentResult, AgentRole, AgentStatus, RoleConfig, utc_now
from orches.omp import OmpClient
from orches.orchestrator import PipelineController
from orches.pipeline_profiles import PipelineProfile, PipelineStep
from orches.state import AgentMessageError, StateStore

from test_connection_tools import make_running_run, start_running_invocation

A, B = ("node_" + ch * 32 for ch in "ab")


def _finish(state: StateStore, invocation_id: int) -> None:
    state.update_invocation(
        invocation_id, status=AgentStatus.COMPLETED, finished_at=utc_now(), returncode=0,
        error=None, output_artifact_id=None, stderr_artifact_id=None,
    )


class MailboxTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root_dir = Path(self.tmp.name)
        self.state = StateStore(self.root_dir / "state.sqlite3")
        self.artifacts = ArtifactStore(self.root_dir / "runs", self.state)
        self.project_id, self.run_id = make_running_run(self.state, self.root_dir / "proj")
        self.root = start_running_invocation(self.state, self.run_id, "root")
        self.a = start_running_invocation(self.state, self.run_id, A)
        self.b = start_running_invocation(self.state, self.run_id, B)
        self.mailbox = AgentMailbox(self.state, self.artifacts, self.run_id)

    async def _call(self, name: str, arguments: dict[str, Any], who: int) -> dict[str, Any]:
        return await self.mailbox.handle(name, arguments, who)

    async def test_question_and_reply_round_trip_between_worker_and_root(self) -> None:
        asked = await self._call("send_agent_message", {"text": "Which format?"}, self.a)  # workers omit `to`
        self.assertEqual(asked["to"], "root")
        self.assertIsNone(asked["reply_to"])

        inbox = await self._call("read_agent_messages", {}, self.root)
        (question,) = inbox["messages"]
        self.assertEqual(question["from"], A)
        self.assertEqual(question["text"], "Which format?")
        self.assertEqual(inbox["next_after_id"], question["id"])

        answered = await self._call("reply_agent_message", {"message_id": question["id"], "text": "JSON"}, self.root)
        self.assertEqual(answered["to"], A)
        self.assertEqual(answered["reply_to"], question["id"])

        reply = await self._call("read_agent_messages", {"after_id": 0}, self.a)
        (message,) = reply["messages"]
        self.assertEqual((message["from"], message["text"], message["reply_to"]), ("root", "JSON", question["id"]))
        # Nothing new after the cursor; the cursor itself is kept.
        again = await self._call("read_agent_messages", {"after_id": reply["next_after_id"]}, self.a)
        self.assertEqual(again, {"messages": [], "next_after_id": reply["next_after_id"]})

    async def test_workers_talk_only_to_the_root(self) -> None:
        await self._call("send_agent_message", {"to": B, "text": "root asks B"}, self.root)
        sideways = await self._call("send_agent_message", {"to": A, "text": "B asks A"}, self.b)
        self.assertIn("only message the root", sideways["error"])
        self.assertEqual([m["from"] for m in (await self._call("read_agent_messages", {}, self.b))["messages"]], ["root"])
        self.assertEqual((await self._call("read_agent_messages", {}, self.a))["messages"], [])
        # The root may not use the worker-style call without a recipient.
        self.assertIn("itself", (await self._call("send_agent_message", {"text": "hi"}, self.root))["error"])

    async def test_denials(self) -> None:
        for arguments, who, fragment in (
            ({"to": "node_missing", "text": "hi"}, self.root, "no running agent"),
            ({"to": A, "text": "hi"}, self.a, "only message the root"),
            ({"text": ""}, self.a, "1 to 4000"),
            ({"text": "x" * 4001}, self.a, "1 to 4000"),
            ({"text": "nul\x00"}, self.a, "1 to 4000"),
            ({"text": "hi", "extra": 1}, self.a, "requires text"),
            ({"to": 5, "text": "hi"}, self.root, "requires text"),
            ({"text": "hi"}, 999_999, "not running"),
        ):
            result = await self._call("send_agent_message", arguments, who)
            self.assertTrue(result.get("_is_error"), result)
            self.assertIn(fragment, result["error"])

        _finish(self.state, self.b)
        gone = await self._call("send_agent_message", {"to": B, "text": "hi"}, self.root)
        self.assertIn("no running agent", gone["error"])

        for arguments, fragment in (
            ({"after_id": -1}, "nonnegative"),
            ({"after_id": True}, "nonnegative"),
            ({"wait_seconds": 11}, "between 0 and 10"),
            ({"wait_seconds": float("nan")}, "between 0 and 10"),
            ({"wait_seconds": True}, "between 0 and 10"),
            ({"cursor": 1}, "accepts only"),
        ):
            result = await self._call("read_agent_messages", arguments, self.root)
            self.assertIn(fragment, result["error"], arguments)
        self.assertIn("unknown", (await self._call("delete_agent_message", {}, self.root))["error"])

    async def test_replies_cannot_be_forged_or_repeated(self) -> None:
        asked = await self._call("send_agent_message", {"to": A, "text": "q"}, self.root)
        forged = await self._call("reply_agent_message", {"message_id": asked["message_id"], "text": "me"}, self.b)
        self.assertIn("addressed to this agent", forged["error"])
        self.assertIn("addressed to this agent", (await self._call("reply_agent_message", {"message_id": 404, "text": "x"}, self.a))["error"])
        self.assertIn("integer message_id", (await self._call("reply_agent_message", {"message_id": "1", "text": "x"}, self.a))["error"])

        ok = await self._call("reply_agent_message", {"message_id": asked["message_id"], "text": "first"}, self.a)
        self.assertEqual(ok["to"], "root")
        duplicate = await self._call("reply_agent_message", {"message_id": asked["message_id"], "text": "second"}, self.a)
        self.assertIn("already answered", duplicate["error"])

        # A reply to an asker that already finished is refused.
        late = await self._call("send_agent_message", {"text": "q2"}, self.b)
        _finish(self.state, self.b)
        self.assertIn("no longer running", (await self._call("reply_agent_message", {"message_id": late["message_id"], "text": "x"}, self.root))["error"])

    async def test_secrets_are_redacted_before_delivery(self) -> None:
        await self._call("send_agent_message", {"to": "root", "text": "use password=hunter2 please"}, self.a)
        (message,) = (await self._call("read_agent_messages", {}, self.root))["messages"]
        self.assertEqual(message["text"], "use password=<REDACTED> please")

    async def test_pagination_cursor(self) -> None:
        for index in range(55):
            await self._call("send_agent_message", {"to": "root", "text": f"m{index}"}, self.a)
        first = await self._call("read_agent_messages", {}, self.root)
        self.assertEqual(len(first["messages"]), 50)
        second = await self._call("read_agent_messages", {"after_id": first["next_after_id"]}, self.root)
        self.assertEqual([m["text"] for m in second["messages"]], [f"m{i}" for i in range(50, 55)])

    async def test_waiting_read_wakes_when_a_message_arrives(self) -> None:
        started = time.monotonic()
        reader = asyncio.create_task(self._call("read_agent_messages", {"wait_seconds": 5}, self.root))
        await asyncio.sleep(0.05)
        self.assertFalse(reader.done())
        await self._call("send_agent_message", {"to": "root", "text": "wake"}, self.a)
        result = await asyncio.wait_for(reader, timeout=2)
        self.assertEqual([m["text"] for m in result["messages"]], ["wake"])
        self.assertLess(time.monotonic() - started, 1.5)

        empty = await self._call("read_agent_messages", {"after_id": result["next_after_id"], "wait_seconds": 0.1}, self.root)
        self.assertEqual(empty["messages"], [])

    def test_messages_do_not_outlive_their_run_or_project(self) -> None:
        self.state.post_agent_message(self.a, "root", "q")
        self._assert_message_rows(1)
        self.state.update_run(self.run_id, "completed")
        with self.assertRaises(AgentMessageError):
            self.state.list_agent_messages(self.root, 0)  # the run is over: nobody is live
        project = self.state.get_project(self.project_id)
        # A new run (what Resume does) replaces the old terminal run and its inbox.
        new_run, _ = self.state.reserve_project_run(self.project_id, expected_updated_at=project["updated_at"], profile_json="{}")
        new_root = start_running_invocation(self.state, new_run, "root")
        self.assertEqual(self.state.list_agent_messages(new_root, 0), [])
        self._assert_message_rows(0)

        start_running_invocation(self.state, new_run, A)
        self.state.post_agent_message(new_root, A, "fresh")
        self._assert_message_rows(1)
        self.state.update_run(new_run, "completed")
        self.assertTrue(self.state.delete_project(self.project_id))
        self._assert_message_rows(0)

    def _assert_message_rows(self, expected: int) -> None:
        conn = sqlite3.connect(self.root_dir / "state.sqlite3")
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_messages").fetchone()[0], expected)
        finally:
            conn.close()


class _TalkingRunner:
    """Worker stand-in that asks the root a question through the mailbox and waits for the reply."""

    def __init__(self, state: StateStore) -> None:
        self.state = state
        self.questions: dict[str, str] = {}

    async def run_agent(self, step: Any, context: dict[str, Any], run_id: str, *, message_handler: Any) -> AgentResult:
        invocation = start_running_invocation(self.state, run_id, step.id)
        asked = await message_handler("send_agent_message", {"text": self.questions[step.id]}, invocation)
        assert not asked.get("_is_error"), asked

        async def drain(cursor: int, wait: float) -> tuple[int, dict[str, Any] | None]:
            inbox = await message_handler("read_agent_messages", {"after_id": cursor, "wait_seconds": wait}, invocation)
            found = None
            for message in inbox["messages"]:
                if message["reply_to"] == asked["message_id"]:
                    found = message
                elif message["reply_to"] is None:  # a question for us: answer it
                    await message_handler("reply_agent_message", {"message_id": message["id"], "text": f"{step.name} says {message['text']}"}, invocation)
            return inbox["next_after_id"], found

        cursor, reply = 0, None
        while reply is None:
            cursor, reply = await drain(cursor, 5)
        await drain(cursor, 0.2)
        _finish(self.state, invocation)
        return AgentResult(
            run_id=run_id, role=step.id, status=AgentStatus.COMPLETED,
            output={"text": f"used {reply['text']}"},
            raw_output="", stderr="", error=None, model_selector="m", duration_seconds=0.0, artifact_ids=(),
        )


class _Owner:
    def __init__(self, state: StateStore, runner: Any) -> None:
        self.state = state
        self.runner = runner

    @staticmethod
    def _result_summary(result: AgentResult) -> dict[str, Any]:
        return {"status": result.status.value, "output": result.output}


class ControllerConversationTest(unittest.IsolatedAsyncioTestCase):
    """Root answers a running worker through the controller's host tools."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root_dir = Path(self.tmp.name)
        self.state = StateStore(root_dir / "state.sqlite3")
        artifacts = ArtifactStore(root_dir / "runs", self.state)
        _, self.run_id = make_running_run(self.state, root_dir / "proj")
        self.runner = _TalkingRunner(self.state)
        self.profile = PipelineProfile(id="p", name="P", steps=(
            PipelineStep(id=A, name="A", model_selector="m"),
            PipelineStep(id=B, name="B", model_selector="m", depends_on=(A,)),
        ))
        self.controller = PipelineController(
            _Owner(self.state, self.runner), self.profile, self.run_id, {}, {"stages": {}},
            AgentMailbox(self.state, artifacts, self.run_id),
        )

    async def test_root_replies_while_the_worker_is_still_running(self) -> None:
        tool = self.controller.handle_tool
        self.assertIn("not registered", (await tool("read_agent_messages", {}))["error"])
        self.controller.bind_root_invocation(start_running_invocation(self.state, self.run_id, "root"))
        self.assertEqual({d["name"] for d in self.controller.tool_definitions()} >= set(AGENT_MESSAGE_TOOL_NAMES), True)

        self.runner.questions = {A: "Which format?", B: "Where to write?"}
        self.assertEqual(await tool("run_agent", {"id": A, "input_data": {}}), {"id": A, "status": "running"})
        self.assertIn("dependencies are not complete", (await tool("run_agent", {"id": B, "input_data": {}}))["error"])
        self.assertIn("must complete", (await tool("finish_pipeline", {}))["error"])

        inbox = await tool("read_agent_messages", {"wait_seconds": 5})
        (question,) = inbox["messages"]
        self.assertEqual((question["from"], question["text"]), (A, "Which format?"))
        self.assertEqual((await tool("get_status", {}))["statuses"][A], "running")
        self.assertIn("not completed", (await tool("read_agent_output", {"id": A}))["error"])

        await tool("reply_agent_message", {"message_id": question["id"], "text": "JSON"})
        # Root asks the still-running worker something and gets an answer back.
        asked = await tool("send_agent_message", {"to": A, "text": "ETA?"})
        answer = await tool("read_agent_messages", {"after_id": inbox["next_after_id"], "wait_seconds": 5})
        (reply,) = answer["messages"]
        self.assertEqual((reply["from"], reply["text"], reply["reply_to"]), (A, "A says ETA?", asked["message_id"]))

        await self.controller.tasks[A]
        output = await tool("read_agent_output", {"id": A})
        self.assertEqual(output["output"]["text"], "used JSON")
        self.assertEqual((await tool("get_status", {}))["ready"], [B])

        await tool("run_agent", {"id": B, "input_data": {}})
        (q2,) = (await tool("read_agent_messages", {"after_id": answer["next_after_id"], "wait_seconds": 5}))["messages"]
        await tool("reply_agent_message", {"message_id": q2["id"], "text": "out/"})
        await self.controller.tasks[B]
        self.assertEqual((await tool("read_agent_output", {"id": B}))["output"]["text"], "used out/")
        self.assertTrue((await tool("finish_pipeline", {}))["finish_requested"])


FAKE_OMP = textwrap.dedent(
    """
    import json, sys

    def emit(obj):
        sys.stdout.write(json.dumps(obj) + "\\n"); sys.stdout.flush()

    def call_host(ident, name, arguments):
        emit({"type": "host_tool_call", "id": ident, "toolName": name, "arguments": arguments})
        for line in sys.stdin:
            frame = json.loads(line)
            if frame.get("type") == "host_tool_result" and frame.get("id") == ident:
                return json.loads(frame["result"]["content"][0]["text"])
            if frame.get("id") is not None:
                emit({"type": "response", "id": frame["id"], "command": frame.get("type"), "success": True})
        raise SystemExit(1)

    advertised = []
    emit({"type": "ready", "protocolVersion": 1})
    for line in sys.stdin:
        cmd = json.loads(line)
        if cmd.get("type") == "set_host_tools":
            advertised = [tool["name"] for tool in cmd["tools"]]
        if cmd.get("id") is not None:
            emit({"type": "response", "id": cmd["id"], "command": cmd.get("type"), "success": True})
        if cmd.get("type") == "prompt":
            asked = call_host("h1", "send_agent_message", {"text": "Which format?"})
            inbox = call_host("h2", "read_agent_messages", {"wait_seconds": 5})
            reply = inbox["messages"][0]
            assert reply["reply_to"] == asked["message_id"]
            text = "format=" + reply["text"] + " tools=" + ",".join(sorted(advertised))
            emit({"type": "message_end", "messageId": "m1", "message": {
                "role": "assistant", "content": [{"type": "text", "text": text}], "stopReason": "stop"}})
            emit({"type": "agent_end", "isTerminal": True, "stopReason": "stop", "messages": []})
            break
    """
)


class RpcWorkerConversationTest(unittest.IsolatedAsyncioTestCase):
    """A real OmpRpcSession worker calls the message host tools over stdio RPC."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root_dir = Path(self.tmp.name)
        fake = self.root_dir / "fake_omp.py"
        fake.write_text(FAKE_OMP, encoding="utf-8")
        self.state = StateStore(self.root_dir / "state.sqlite3")
        self.artifacts = ArtifactStore(self.root_dir / "runs", self.state)
        _, self.run_id = make_running_run(self.state, self.root_dir / "proj")
        self.client = OmpClient(omp_bin=sys.executable)
        real_exec = asyncio.create_subprocess_exec

        async def launch(*args: Any, **kwargs: Any) -> Any:
            self.launch_args = list(args)
            return await real_exec(sys.executable, "-u", str(fake), **kwargs)

        patcher = mock.patch("orches.omp.asyncio.create_subprocess_exec", side_effect=launch)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_worker_question_is_answered_before_it_finishes(self) -> None:
        runner = AgentRunner(
            self.client, self.state, self.artifacts, self.root_dir / "runs",
            {AgentRole.ROOT: RoleConfig(model_selector="anthropic/root", timeout_seconds=20)},
        )
        mailbox = AgentMailbox(self.state, self.artifacts, self.run_id)
        root = start_running_invocation(self.state, self.run_id, "root")
        step = PipelineStep(id=A, name="A", model_selector="anthropic/worker", thinking="low")
        worker = asyncio.create_task(
            runner.run_agent(step, {"project_directory": str(self.root_dir / "proj")}, self.run_id, message_handler=mailbox.handle)
        )

        inbox = await mailbox.handle("read_agent_messages", {"wait_seconds": 10}, root)
        (question,) = inbox["messages"]
        self.assertEqual((question["from"], question["text"]), (A, "Which format?"))
        self.assertFalse(worker.done(), "the worker must still be running while the root answers")
        await mailbox.handle("reply_agent_message", {"message_id": question["id"], "text": "JSON"}, root)

        result = await asyncio.wait_for(worker, timeout=20)
        self.assertEqual(result.status, AgentStatus.COMPLETED, result.error)
        self.assertEqual(
            result.output["text"],
            "format=JSON tools=" + ",".join(sorted([
                "claim_connection", "read_agent_messages", "read_connections", "release_connection",
                "reply_agent_message", "reveal_connection", "send_agent_message",
            ])),
        )
        config_index = self.launch_args.index("--config")
        overlay = Path(self.launch_args[config_index + 1]).read_text(encoding="utf-8")
        for name in AGENT_MESSAGE_TOOL_NAMES:
            self.assertIn(f"{name}: allow", overlay)
        # The finished worker can no longer be addressed.
        gone = await mailbox.handle("send_agent_message", {"to": A, "text": "late"}, root)
        self.assertIn("no running agent", gone["error"])


if __name__ == "__main__":
    unittest.main()

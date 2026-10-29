"""Agent sessions retain native OMP tools alongside Orches host tools."""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from orches.agent_runtime import AgentRunner
from orches.artifacts import ArtifactStore
from orches.connection_tools import CONNECTION_TOOL_DEFINITIONS
from orches.models import AgentRole, AgentStatus, RoleConfig
from orches.omp import OmpClient
from orches.pipeline_profiles import PipelineStep
from orches.state import StateStore
from test_step_thinking import make_running_run

NODE = "node_" + "a" * 32
_FAKE_OMP = textwrap.dedent("""
    import json, sys
    def emit(frame):
        sys.stdout.write(json.dumps(frame) + "\\n")
        sys.stdout.flush()
    emit({"type": "ready", "protocolVersion": 1})
    tools = []
    for line in sys.stdin:
        cmd = json.loads(line)
        kind = cmd.get("type")
        if kind == "set_host_tools":
            tools = [item["name"] for item in cmd["tools"]]
        if cmd.get("id") is not None and kind != "host_tool_result":
            emit({"type": "response", "id": cmd["id"], "command": kind, "success": True})
        if kind == "prompt":
            if "read_connections" not in tools:
                raise RuntimeError("read_connections not registered")
            emit({"type": "host_tool_call", "id": "host-1", "toolName": "read_connections", "arguments": {}})
        if kind == "host_tool_result":
            payload = json.loads(cmd["result"]["content"][0]["text"])
            if cmd["result"].get("isError") or "connections" not in payload:
                raise RuntimeError("host tool did not return connections")
            content = [{"type": "text", "text": "host connections available"}]
            emit({"type": "agent_end", "isTerminal": True, "stopReason": "stop",
                  "messages": [{"role": "assistant", "content": content}]})
            break
""")


class AgentToolLaunchTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "project"
        self.state = StateStore(self.root / "state.sqlite3")
        self.run_id = make_running_run(self.state, self.project)
        self.runs = self.root / "runs"
        fake = self.root / "fake_omp.py"
        fake.write_text(_FAKE_OMP, encoding="utf-8")
        self.client = OmpClient(omp_bin=sys.executable)
        real_launch = asyncio.create_subprocess_exec
        self.calls: list[tuple[str, ...]] = []

        async def launch(*args: str, **kwargs: Any) -> Any:
            self.calls.append(args)
            return await real_launch(sys.executable, "-u", str(fake), **kwargs)

        patcher = mock.patch("orches.omp.asyncio.create_subprocess_exec", side_effect=launch)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.session_timeouts: list[int] = []
        real_start = self.client.start_session

        async def start_session(prompt: str, **kwargs: Any) -> Any:
            self.session_timeouts.append(kwargs["timeout"])
            return await real_start(prompt, **kwargs)

        self.client.start_session = start_session  # type: ignore[method-assign]

    def runner(self, root_policy: str = "yolo") -> AgentRunner:
        return AgentRunner(
            self.client, self.state, ArtifactStore(self.runs, self.state), self.runs,
            {AgentRole.ROOT: RoleConfig(model_selector="root-model", thinking="high",
                                        approval_mode=root_policy, timeout_seconds=20)},
        )

    def assert_native_tools(self, args: tuple[str, ...], approval: str) -> None:
        self.assertEqual(args[args.index("--approval-mode") + 1], approval)
        for disabled in ("--tools", "--no-tools", "--no-context-files", "--no-extensions",
                         "--no-skills", "--no-rules", "--no-lsp", "--no-pty", "--config"):
            self.assertNotIn(disabled, args)

    async def test_worker_exec_approval_with_default_tools_and_host_tools(self) -> None:
        step = PipelineStep(id=NODE, name="A", model_selector="worker-model", thinking="xhigh",
                            timeout_seconds=1234)
        result = await self.runner().run_agent(step, {"project_directory": str(self.project)}, self.run_id)
        self.assertEqual(result.status, AgentStatus.COMPLETED, result.error)
        self.assertEqual(result.output, {"text": "host connections available"})
        (args,) = self.calls
        self.assert_native_tools(args, "yolo")
        self.assertEqual(args[args.index("--model") + 1], "worker-model")
        self.assertEqual(args[args.index("--thinking") + 1], "xhigh")
        self.assertEqual(Path(args[args.index("--cwd") + 1]), self.project.resolve())
        self.assertEqual(Path(args[args.index("--add-dir") + 1]),
                         (self.runs / self.run_id / "agents" / NODE).resolve())
        self.assertFalse((self.runs / self.run_id / "worker-approvals.yml").exists())
        # The node's own timeout bounds its session; nothing is hardcoded.
        self.assertEqual(self.session_timeouts, [1234])

    async def test_root_has_native_tools_and_keeps_configured_policy(self) -> None:
        async def handler(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            self.assertEqual((name, arguments), ("read_connections", {}))
            return {"connections": []}

        for policy in ("yolo", "write"):
            with self.subTest(policy=policy):
                project = self.project if policy == "yolo" else self.root / "project-write"
                run_id = self.run_id if policy == "yolo" else make_running_run(self.state, project)
                result = await self.runner(policy).run_root(
                    {"project_directory": str(project)}, run_id,
                    host_tools=CONNECTION_TOOL_DEFINITIONS, host_tool_handler=handler,
                    worker_count=1,
                    timeout_seconds=7200 if policy == "yolo" else None,
                )
                # Profile pipeline timeout overrides the role default (20s) when set.
                self.assertEqual(self.session_timeouts[-1], 7200 if policy == "yolo" else 20)
                self.assertEqual(result.status, AgentStatus.COMPLETED, result.error)
                self.assertEqual(result.output, {"summary": "host connections available"})
                args = self.calls[-1]
                self.assert_native_tools(args, policy)
                self.assertEqual(args[args.index("--model") + 1], "root-model")
                self.assertEqual(args[args.index("--thinking") + 1], "high")
                self.assertEqual(Path(args[args.index("--cwd") + 1]), project.resolve())
                self.assertEqual(Path(args[args.index("--add-dir") + 1]),
                                 (self.runs / run_id / "agents" / "root").resolve())
                self.assertEqual(Path(args[args.index("--session-dir") + 1]),
                                 (project / ".orches" / "sessions" / run_id / "root").resolve())


if __name__ == "__main__":
    unittest.main()

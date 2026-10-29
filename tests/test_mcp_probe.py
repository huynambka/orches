"""MCP connectivity probe and OMP --cwd launch argument tests."""
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

from orches.omp import OmpClient
from orches.project_files import ensure_project_marker
from orches.state import StateStore

FAKE_PROBE_SUCCESS = textwrap.dedent("""
    import json, sys

    def emit(obj):
        sys.stdout.write(json.dumps(obj) + "\\n"); sys.stdout.flush()

    emit({"type": "ready", "protocolVersion": 1})
    for line in sys.stdin:
        cmd = json.loads(line)
        if cmd.get("type") == "prompt" and "/mcp test" in cmd.get("message", ""):
            name = cmd["message"].split("/mcp test ")[-1].strip()
            emit({"type": "command_output", "text": f'Server "{name}" connected (3 tools).'})
            emit({"type": "response", "id": cmd["id"], "command": "prompt", "success": True, "data": {"agentInvoked": False}})
            break
""")

FAKE_PROBE_FAIL = textwrap.dedent("""
    import json, sys

    def emit(obj):
        sys.stdout.write(json.dumps(obj) + "\\n"); sys.stdout.flush()

    emit({"type": "ready", "protocolVersion": 1})
    for line in sys.stdin:
        cmd = json.loads(line)
        if cmd.get("type") == "prompt":
            name = cmd["message"].split("/mcp test ")[-1].strip()
            emit({"type": "command_output", "text": f'Connection to "{name}" failed: ENOENT'})
            emit({"type": "response", "id": cmd["id"], "command": "prompt", "success": True, "data": {"agentInvoked": False}})
            break
""")

FAKE_PROBE_AGENT_INVOKED = textwrap.dedent("""
    import json, sys

    def emit(obj):
        sys.stdout.write(json.dumps(obj) + "\\n"); sys.stdout.flush()

    emit({"type": "ready", "protocolVersion": 1})
    for line in sys.stdin:
        cmd = json.loads(line)
        if cmd.get("type") == "prompt":
            emit({"type": "response", "id": cmd["id"], "command": "prompt", "success": True, "data": {"agentInvoked": True}})
            break
""")

FAKE_PROBE_HANG = textwrap.dedent("""
    import json, sys, time

    def emit(obj):
        sys.stdout.write(json.dumps(obj) + "\\n"); sys.stdout.flush()

    emit({"type": "ready", "protocolVersion": 1})
    for line in sys.stdin:
        time.sleep(120)
""")


class FakeProbeTest(unittest.IsolatedAsyncioTestCase):
    """Probe with fake OMP children; no real MCP server needed."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.proj = self.root / "proj"
        self.proj.mkdir()
        state = StateStore(self.root / "state.sqlite3")
        pid = state.create_project(project_directory=str(self.proj), name="test")
        ensure_project_marker(self.proj, pid)

    async def _probe(self, script: str, name: str = "fs", timeout: float = 10) -> dict[str, Any]:
        fake = self.root / "fake.py"
        fake.write_text(script, encoding="utf-8")
        client = OmpClient(omp_bin=sys.executable)
        real_exec = asyncio.create_subprocess_exec

        async def launch(*args: Any, **kwargs: Any) -> Any:
            self.launch_args = list(args)
            return await real_exec(sys.executable, "-u", str(fake), **kwargs)

        with mock.patch("orches.omp.asyncio.create_subprocess_exec", side_effect=launch):
            return await client.probe_mcp(self.proj, name, timeout=timeout)

    async def test_success(self) -> None:
        result = await self._probe(FAKE_PROBE_SUCCESS, "fs")
        self.assertEqual(result["status"], "connected")
        self.assertIn("connected (3 tools)", result["detail"])

    async def test_failure(self) -> None:
        result = await self._probe(FAKE_PROBE_FAIL, "bad")
        self.assertEqual(result["status"], "failed")

    async def test_agent_invoked_rejected(self) -> None:
        result = await self._probe(FAKE_PROBE_AGENT_INVOKED, "fs")
        self.assertEqual(result["status"], "failed")
        self.assertIn("model agent", result["detail"])

    async def test_timeout(self) -> None:
        result = await self._probe(FAKE_PROBE_HANG, "fs", timeout=2)
        self.assertEqual(result["status"], "timeout")

    async def test_cwd_argument(self) -> None:
        await self._probe(FAKE_PROBE_SUCCESS, "fs")
        self.assertIn("--cwd", self.launch_args)
        idx = self.launch_args.index("--cwd")
        self.assertEqual(Path(self.launch_args[idx + 1]).resolve(), self.proj.resolve())
        self.assertIn("--no-tools", self.launch_args)


class OmpCwdArgumentTest(unittest.IsolatedAsyncioTestCase):
    """Verify start_session passes --cwd for project_directory."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    async def test_project_directory_becomes_cwd_arg(self) -> None:
        fake = self.root / "fake_session.py"
        fake.write_text(textwrap.dedent("""
            import json, sys
            def emit(obj):
                sys.stdout.write(json.dumps(obj) + "\\n"); sys.stdout.flush()
            emit({"type": "ready", "protocolVersion": 1})
            for line in sys.stdin:
                cmd = json.loads(line)
                if cmd.get("id") is not None:
                    emit({"type": "response", "id": cmd["id"], "command": cmd.get("type"), "success": True})
                if cmd.get("type") == "prompt":
                    emit({"type": "agent_end", "isTerminal": True, "stopReason": "stop", "messages": []})
                    break
        """), encoding="utf-8")
        client = OmpClient(omp_bin=sys.executable)
        real_exec = asyncio.create_subprocess_exec
        captured_args: list[str] = []

        async def launch(*args: Any, **kwargs: Any) -> Any:
            captured_args.extend(args)
            return await real_exec(sys.executable, "-u", str(fake), **kwargs)

        workspace = self.root / "workspace"
        workspace.mkdir()
        project = self.root / "project"
        project.mkdir()

        with mock.patch("orches.omp.asyncio.create_subprocess_exec", side_effect=launch):
            session = await client.start_session(
                "test", cwd=workspace, project_directory=str(project),
                approval_mode="yolo", thinking="off",
            )
            await session.finish()

        self.assertIn("--cwd", captured_args)
        cwd_idx = captured_args.index("--cwd")
        self.assertEqual(Path(captured_args[cwd_idx + 1]).resolve(), project.resolve())
        # Workspace should be --add-dir since it differs from project
        self.assertIn("--add-dir", captured_args)
        add_idx = captured_args.index("--add-dir")
        self.assertEqual(Path(captured_args[add_idx + 1]).resolve(), workspace.resolve())


if __name__ == "__main__":
    unittest.main()

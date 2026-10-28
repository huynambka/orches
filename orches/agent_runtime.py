"""Agent runner that connects profile prompts to interactive OMP sessions."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from .artifacts import ArtifactStore
from .connection_tools import CONNECTION_TOOL_APPROVALS_YML, CONNECTION_TOOL_DEFINITIONS, handle_connection_tool
from .models import AgentInvocation, AgentRole, AgentResult, AgentStatus, RoleConfig, utc_now
from .omp import HostToolHandler, OmpClient, OmpRpcSession
from .project_files import session_root
from .prompts import root_system_prompt, root_user_prompt, worker_system_prompt, worker_user_prompt
from .state import StateStore

# Exec-tier tools (bash, eval, task, ...) are always denied for headless workers
# under approval_mode="write"; don't advertise tools that can never run.
WORKER_TOOLS = ("read", "write", "edit", "glob", "grep", "todo", "web_search")


class ActiveSessionRegistry:
    def __init__(self) -> None:
        self._sessions: dict[tuple[str, int], OmpRpcSession] = {}
        self._locks: dict[tuple[str, int], asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    async def publish(self, run_id: str, invocation_id: int, session: OmpRpcSession) -> None:
        async with self._guard:
            key = (run_id, invocation_id)
            self._sessions[key] = session
            self._locks[key] = asyncio.Lock()

    async def remove(self, run_id: str, invocation_id: int) -> None:
        async with self._guard:
            key = (run_id, invocation_id)
            self._sessions.pop(key, None)
            self._locks.pop(key, None)

    def has(self, run_id: str, invocation_id: int) -> bool:
        return (run_id, invocation_id) in self._sessions

    async def send(self, run_id: str, invocation_id: int, text: str) -> None:
        key = (run_id, invocation_id)
        session = self._sessions.get(key)
        lock = self._locks.get(key)
        if session is None or lock is None:
            raise LookupError("agent session is not active")
        async with lock:
            await session.steer(text)
            await session.record_user_steer(text)

class AgentRunner:
    def __init__(
        self,
        omp: OmpClient | None,
        state: StateStore,
        artifacts: ArtifactStore,
        runs_root: str | Path,
        role_configs: Mapping[AgentRole, RoleConfig],
        max_prompt_bytes: int = 300_000,
        max_output_bytes: int = 2_000_000,
        sessions: ActiveSessionRegistry | None = None,
    ) -> None:
        self.omp = omp
        self.state = state
        self.artifacts = artifacts
        self.runs_root = Path(runs_root)
        self.role_configs = role_configs
        self.max_prompt_bytes = max_prompt_bytes
        self.max_output_bytes = max_output_bytes
        self.sessions = sessions

    async def run_agent(
        self,
        step: Any,
        context: dict[str, Any],
        run_id: str,
    ) -> AgentResult:
        """Run a profile worker node and preserve its plain text output."""
        node_id = step.id
        # Headless workers cannot answer approval prompts; grant file writes inside the
        # bound project and enough time for build-style tasks. Effort comes from the node.
        config = RoleConfig(
            model_selector=step.model_selector, thinking=step.thinking,
            approval_mode="write", timeout_seconds=600,
        )
        started = utc_now()
        started_clock = time.monotonic()
        invocation_id = self.state.start_invocation(
            AgentInvocation(
                run_id=run_id, role=node_id, model_selector=config.model_selector,
                thinking=config.thinking, status=AgentStatus.RUNNING,
                started_at=started, prompt_version="worker-1",
            )
        )
        status = AgentStatus.FAILED
        error: str | None = None
        returncode: int | None = None
        output_artifact_id: str | None = None
        stderr_artifact_id: str | None = None
        raw_output = ""
        final_text = ""
        stderr = ""
        session: OmpRpcSession | None = None
        try:
            raw_dir = self.runs_root / run_id / "raw"
            workspace = self.runs_root / run_id / "agents" / node_id
            workspace.mkdir(parents=True, exist_ok=True)
            prompt = worker_user_prompt(step.name, context)
            if len(prompt.encode("utf-8")) > self.max_prompt_bytes:
                error = f"prompt exceeds {self.max_prompt_bytes} bytes"
            elif self.omp is None:
                error = "omp executable is not configured"
            elif not hasattr(self.omp, "start_session") and hasattr(self.omp, "run"):
                result = await self.omp.run(
                    prompt, append_system_prompt=worker_system_prompt(step.prompt), cwd=workspace,
                    model=config.model_selector, thinking=config.thinking, timeout=config.timeout_seconds,
                    approval_mode=config.approval_mode, max_output_bytes=self.max_output_bytes,
                    stdout_path=raw_dir / f"{node_id}.stdout.jsonl",
                    stderr_path=raw_dir / f"{node_id}.stderr.txt",
                )
                returncode, raw_output = result.returncode, result.raw_output or result.output
                stderr, error, final_text = result.stderr, result.error, result.output
            else:
                stdout_path = raw_dir / f"{node_id}.stdout.jsonl"
                stdout_path.parent.mkdir(parents=True, exist_ok=True)
                caller = f"worker {step.name!r} ({node_id}, run {run_id})"

                async def connection_tools(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
                    return await handle_connection_tool(
                        self.state, name, arguments, caller=caller, invocation_id=invocation_id,
                    )

                approvals = self.runs_root / run_id / "worker-approvals.yml"
                if not approvals.exists():
                    approvals.parent.mkdir(parents=True, exist_ok=True)
                    approvals.write_text(CONNECTION_TOOL_APPROVALS_YML, encoding="utf-8")

                session = await self.omp.start_session(
                    prompt, append_system_prompt=worker_system_prompt(step.prompt),
                    cwd=workspace, project_directory=context.get("project_directory"),
                    session_dir=None, model=config.model_selector, thinking=config.thinking,
                    timeout=config.timeout_seconds, approval_mode=config.approval_mode,
                    max_output_bytes=self.max_output_bytes, stdout_path=stdout_path,
                    tools=list(WORKER_TOOLS),
                    host_tools=CONNECTION_TOOL_DEFINITIONS,
                    host_tool_handler=connection_tools,
                    extra_args=["--config", str(approvals)],
                )
                if self.sessions is not None:
                    await self.sessions.publish(run_id, invocation_id, session)
                result = await session.finish()
                returncode, raw_output = result.returncode, result.raw_output or result.output
                stderr, error, final_text = result.stderr, result.error, result.output
                if stdout_path.exists():
                    raw_output = stdout_path.read_text(encoding="utf-8", errors="replace") or raw_output
                    # The live sink shares the artifact path; remove it before storing the artifact.
                    stdout_path.unlink()
            output_artifact = self.artifacts.write_text(
                run_id, f"raw/{node_id}.stdout.jsonl", raw_output,
                media_type="text/plain",
            )
            stderr_artifact = self.artifacts.write_text(run_id, f"raw/{node_id}.stderr.txt", stderr)
            output_artifact_id, stderr_artifact_id = output_artifact.artifact_id, stderr_artifact.artifact_id
            status = AgentStatus.COMPLETED if not error and returncode in (0, None) else AgentStatus.FAILED
        except asyncio.CancelledError:
            error = "run cancelled"
            raise
        except Exception as exc:
            error, status = str(exc), AgentStatus.FAILED
        finally:
            if self.sessions is not None:
                await self.sessions.remove(run_id, invocation_id)
            if session is not None and session.proc.returncode is None:
                await session.close()
            self.state.update_invocation(
                invocation_id, status=status, finished_at=utc_now(), returncode=returncode,
                error=error, output_artifact_id=output_artifact_id,
                stderr_artifact_id=stderr_artifact_id,
                # Persisted so a later run can resume from this worker's result.
                output_json=json.dumps({"text": final_text}) if status == AgentStatus.COMPLETED else None,
            )
        # Downstream nodes and the root receive the agent's final answer; the full
        # JSONL event log is kept only as the stdout artifact.
        return AgentResult(
            run_id=run_id, role=node_id, status=status, output={"text": final_text},
            raw_output=raw_output, stderr=stderr, error=error,
            model_selector=config.model_selector,
            duration_seconds=time.monotonic() - started_clock,
            artifact_ids=tuple(item for item in (output_artifact_id, stderr_artifact_id) if item),
        )

    async def run_root(
        self,
        context: dict[str, Any],
        run_id: str,
        *,
        host_tools: list[dict[str, Any]],
        host_tool_handler: HostToolHandler,
        worker_count: int,
        root_prompt: str = "",
        model_selector: str | None = None,
        thinking: str | None = None,
        on_session: Callable[[OmpRpcSession], None] | None = None,
        on_invocation_started: Callable[[int], None] | None = None,
    ) -> AgentResult:
        role = AgentRole.ROOT
        config = self.role_configs[role]
        if model_selector:
            config = replace(config, model_selector=model_selector)
        if thinking:
            config = replace(config, thinking=thinking)
        started_clock = time.monotonic()
        invocation_id = self.state.start_invocation(
            AgentInvocation(
                run_id=run_id,
                role=role,
                model_selector=config.model_selector,
                thinking=config.thinking,
                status=AgentStatus.RUNNING,
                started_at=utc_now(),
                prompt_version=config.prompt_version,
            )
        )
        if on_invocation_started is not None:
            on_invocation_started(invocation_id)
        status = AgentStatus.FAILED
        error: str | None = None
        returncode: int | None = None
        output_artifact_id: str | None = None
        stderr_artifact_id: str | None = None
        raw_output = ""
        stderr = ""
        output_text = ""
        session: OmpRpcSession | None = None
        try:
            raw_dir = self.runs_root / run_id / "raw"
            workspace = self.runs_root / run_id / "agents" / role.value
            workspace.mkdir(parents=True, exist_ok=True)
            prompt = root_user_prompt(context)
            system = root_system_prompt(worker_count, root_prompt)
            if len(prompt.encode("utf-8")) > self.max_prompt_bytes:
                error = f"prompt exceeds {self.max_prompt_bytes} bytes"
            elif self.omp is None:
                error = "omp executable is not configured"
            elif not hasattr(self.omp, "start_session"):
                error = "configured OMP runtime does not support RPC host tools"
            else:
                stdout_path = raw_dir / f"{role.value}.stdout.jsonl"
                stdout_path.parent.mkdir(parents=True, exist_ok=True)
                stdout_path.write_text(
                    json.dumps({"type": "stage_input", "text": prompt}, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                project_directory = context.get("project_directory")
                session_dir = None
                if isinstance(project_directory, str) and project_directory:
                    session_dir = session_root(project_directory, run_id) / role.value
                    session_dir.mkdir(parents=True, exist_ok=True)
                session = await self.omp.start_session(
                    prompt,
                    append_system_prompt=system,
                    cwd=workspace,
                    project_directory=None,
                    session_dir=session_dir,
                    model=config.model_selector,
                    thinking=config.thinking,
                    timeout=config.timeout_seconds,
                    approval_mode=config.approval_mode,
                    max_output_bytes=self.max_output_bytes,
                    stdout_path=stdout_path,
                    stderr_path=raw_dir / f"{role.value}.stderr.txt",
                    host_tools=host_tools,
                    host_tool_handler=host_tool_handler,
                    no_builtin_tools=True,
                )
                if on_session is not None:
                    on_session(session)
                if self.sessions is not None:
                    await self.sessions.publish(run_id, invocation_id, session)
                result = await session.finish()
                if stdout_path.exists():
                    raw_output = stdout_path.read_text(encoding="utf-8", errors="replace")
                for sink_path in (stdout_path, raw_dir / f"{role.value}.stderr.txt"):
                    if sink_path.exists():
                        sink_path.unlink()
                returncode = result.returncode
                raw_output = raw_output or result.raw_output or result.output
                output_text = result.output
                stderr = result.stderr
                error = result.error
                output_artifact = self.artifacts.write_text(
                    run_id,
                    f"raw/{role.value}.stdout.jsonl",
                    raw_output,
                    media_type="application/jsonl" if result.events else "text/plain",
                )
                stderr_artifact = self.artifacts.write_text(
                    run_id, f"raw/{role.value}.stderr.txt", stderr
                )
                output_artifact_id = output_artifact.artifact_id
                stderr_artifact_id = stderr_artifact.artifact_id
                status = AgentStatus.COMPLETED if result.ok else AgentStatus.FAILED
        except asyncio.CancelledError:
            error = "run cancelled"
            raise
        except Exception as exc:
            error = str(exc)
            status = AgentStatus.FAILED
        finally:
            if self.sessions is not None:
                await self.sessions.remove(run_id, invocation_id)
            if session is not None and session.proc.returncode is None:
                await session.close()
            self.state.update_invocation(
                invocation_id,
                status=status,
                finished_at=utc_now(),
                returncode=returncode,
                error=error,
                output_artifact_id=output_artifact_id,
                stderr_artifact_id=stderr_artifact_id,
            )
        return AgentResult(
            run_id=run_id,
            role=role,
            status=status,
            output={"summary": output_text} if status == AgentStatus.COMPLETED else None,
            raw_output=raw_output,
            stderr=stderr,
            error=error,
            model_selector=config.model_selector,
            duration_seconds=time.monotonic() - started_clock,
            artifact_ids=tuple(
                item for item in (output_artifact_id, stderr_artifact_id) if item is not None
            ),
        )

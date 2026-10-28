"""Root-controlled execution of user-defined agent pipelines."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Mapping

from .agent_messages import AGENT_MESSAGE_TOOL_NAMES, ROOT_MESSAGE_TOOL_DEFINITIONS, AgentMailbox
from .agent_runtime import AgentRunner
from .connection_tools import CONNECTION_TOOL_DEFINITIONS, CONNECTION_TOOL_NAMES, handle_connection_tool, masked_connections
from .artifacts import ArtifactStore
from .models import AgentRole, AgentStatus
from .pipeline_profiles import PipelineProfile
from .prompts import project_context
from .state import StateStore

# After a worker failure makes the pipeline unwinnable: how long the root gets
# to wrap up on its own, then how long an RPC abort gets before a hard close.
FATAL_ROOT_GRACE_SECONDS = 60.0
FATAL_ABORT_GRACE_SECONDS = 15.0


class PipelineError(RuntimeError):
    """A pipeline transition cannot proceed."""


class PipelineController:
    """Host-owned DAG state machine exposed to the root orchestrator."""

    TOOL_NAMES = (
        "read_profile", "get_status", "read_agent_output", "run_agent", "finish_pipeline",
        *CONNECTION_TOOL_NAMES, *AGENT_MESSAGE_TOOL_NAMES,
    )

    def __init__(
        self, owner: "Orchestrator", profile: PipelineProfile, run_id: str,
        base_context: dict[str, Any], report: dict[str, Any], mailbox: AgentMailbox,
        completed_outputs: Mapping[str, dict[str, Any]] | None = None,
    ) -> None:
        self.owner = owner
        self.profile = profile
        self.run_id = run_id
        self.base_context = base_context
        self.report = report
        self.mailbox = mailbox
        self.statuses = {step.id: "pending" for step in profile.steps}
        self.outputs: dict[str, dict[str, Any]] = {}
        self.results: dict[str, Any] = {}
        self.reused_artifacts: dict[str, list[str]] = {}
        self.tasks: dict[str, asyncio.Task[dict[str, Any]]] = {}
        self.lock = asyncio.Lock()
        self.finish_requested = False
        self.fatal_error: str | None = None
        self.fatal = asyncio.Event()
        self.root_invocation_id: int | None = None
        # Workers carried over from a previous run start completed: the root reads
        # their output but cannot start them again. Seed shape per node:
        # {"output": {...}, "artifact_ids": [...]}.
        for node_id, seed in (completed_outputs or {}).items():
            if node_id not in self.statuses:
                continue
            self.statuses[node_id] = "completed"
            self.outputs[node_id] = dict(seed["output"])
            self.reused_artifacts[node_id] = list(seed.get("artifact_ids", []))
            report["stages"][node_id] = {
                "status": "completed", "reused": True, "output": self.outputs[node_id],
                "artifact_ids": self.reused_artifacts[node_id],
            }

    def bind_root_invocation(self, invocation_id: int) -> None:
        self.root_invocation_id = invocation_id

    @classmethod
    def tool_definitions(cls) -> list[dict[str, Any]]:
        no_args = {"type": "object", "additionalProperties": False, "properties": {}}
        return [
            {"name": "read_profile", "description": "Read the immutable pipeline profile.", "parameters": no_args},
            {"name": "get_status", "description": "Read worker statuses and ready workers.", "parameters": no_args},
            {"name": "read_agent_output", "description": "Read a completed worker output.", "parameters": {
                "type": "object", "additionalProperties": False,
                "properties": {"id": {"type": "string"}}, "required": ["id"],
            }},
            {"name": "run_agent", "description": (
                "Start one ready worker exactly once; returns immediately while it runs. "
                "Poll get_status for completion and read_agent_output for its result."
            ), "parameters": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "input_data": {
                        "type": "object", "additionalProperties": False,
                        "properties": {"guidance": {"type": "string", "maxLength": 2000}},
                        "required": [],
                    },
                },
                "required": ["id", "input_data"],
            }},
            {"name": "finish_pipeline", "description": "Mark the fully completed pipeline.", "parameters": no_args},
            *CONNECTION_TOOL_DEFINITIONS,
            *ROOT_MESSAGE_TOOL_DEFINITIONS,
        ]

    @staticmethod
    def _error(message: str) -> dict[str, Any]:
        return {"_is_error": True, "error": message}

    def _bounded(self, value: dict[str, Any]) -> dict[str, Any]:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
        if len(encoded.encode("utf-8")) <= 64 * 1024:
            return value
        return {"output_preview": str(value.get("output", ""))[:16_000],
                "truncated": True, "artifact_ids": value.get("artifact_ids", [])}

    def _ready(self, node_id: str) -> bool:
        return all(self.statuses.get(dep) == "completed" for dep in self.profile.step(node_id).depends_on)

    def _status_response(self) -> dict[str, Any]:
        return {
            "statuses": dict(self.statuses),
            "ready": [node_id for node_id, status in self.statuses.items()
                      if status == "pending" and self._ready(node_id)],
            "fatal_error": self.fatal_error,
            "finish_requested": self.finish_requested,
        }

    def _mark_fatal(self, message: str) -> None:
        self.fatal_error = self.fatal_error or message
        self.fatal.set()

    async def handle_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in self.TOOL_NAMES:
            return self._error("unknown host tool")
        if not isinstance(arguments, dict):
            return self._error("arguments must be an object")
        if name in {"read_profile", "get_status", "finish_pipeline"} and arguments:
            return self._error("this tool takes no arguments")
        if name in CONNECTION_TOOL_NAMES or name in AGENT_MESSAGE_TOOL_NAMES:
            if self.root_invocation_id is None:
                return self._error("root orchestrator is not registered yet")
            if name in AGENT_MESSAGE_TOOL_NAMES:
                return await self.mailbox.handle(name, arguments, self.root_invocation_id)
            return await handle_connection_tool(
                self.owner.state, name, arguments,
                caller=f"root orchestrator (run {self.run_id})",
                invocation_id=self.root_invocation_id,
            )
        if name == "read_profile":
            return self.profile.to_dict()
        if name == "get_status":
            return self._status_response()
        if name == "read_agent_output":
            if set(arguments) != {"id"} or not isinstance(arguments["id"], str):
                return self._error("read_agent_output requires only a string id")
            node_id = arguments["id"]
            if node_id not in self.statuses:
                return self._error("unknown worker id")
            if self.statuses[node_id] != "completed":
                return self._error("worker output is not completed")
            artifact_ids = (
                list(self.results[node_id].artifact_ids) if node_id in self.results
                else self.reused_artifacts.get(node_id, [])
            )
            return self._bounded({
                "id": node_id, "output": self.outputs[node_id], "artifact_ids": artifact_ids,
                "reused": node_id not in self.results,
            })
        if name == "finish_pipeline":
            async with self.lock:
                if self.fatal_error:
                    return self._error(
                        f"pipeline is fatally blocked: {self.fatal_error}; "
                        "it can never complete — stop and summarize now"
                    )
                if not self.statuses or any(status != "completed" for status in self.statuses.values()):
                    return self._error("all configured workers must complete before finish_pipeline")
                self.finish_requested = True
                return self._status_response()
        if set(arguments) != {"id", "input_data"} or not isinstance(arguments.get("id"), str):
            return self._error("run_agent requires only id and input_data")
        raw_input = arguments["input_data"]
        if not isinstance(raw_input, dict) or set(raw_input) - {"guidance"}:
            return self._error("input_data allows only guidance")
        guidance = raw_input.get("guidance", "")
        if not isinstance(guidance, str) or len(guidance) > 2000:
            return self._error("guidance must be at most 2000 characters")
        node_id = arguments["id"]
        if node_id not in self.statuses:
            return self._error("unknown worker id")
        async with self.lock:
            if self.fatal_error:
                return self._error(
                    f"pipeline is fatally blocked: {self.fatal_error}; "
                    "no new workers may start — stop and summarize now"
                )
            if self.statuses[node_id] != "pending":
                return self._error(
                    f"{node_id} has already been started"
                    if node_id in self.results or node_id in self.tasks
                    else f"{node_id} completed in a previous run; read its output instead of rerunning it"
                )
            if not self._ready(node_id):
                return self._error(f"{node_id} dependencies are not complete")
            self.statuses[node_id] = "running"
            task = asyncio.create_task(self._execute(node_id, guidance), name=f"pipeline-{node_id}")
            self.tasks[node_id] = task
        return {"id": node_id, "status": "running"}

    async def _execute(self, node_id: str, guidance: str) -> None:
        """Run one worker to completion; records its outcome instead of raising."""
        step = self.profile.step(node_id)
        context = dict(self.base_context)
        context["root_guidance"] = guidance
        context["dependency_outputs"] = {
            dependency: self.outputs[dependency]
            for dependency in step.depends_on
        }
        try:
            result = await self.owner.runner.run_agent(
                step, context, self.run_id, message_handler=self.mailbox.handle,
            )
            self.results[node_id] = result
            self.report["stages"][node_id] = self.owner._result_summary(result)
            if result.status != AgentStatus.COMPLETED or result.output is None:
                raise PipelineError(result.error or f"{node_id} failed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            async with self.lock:
                self.statuses[node_id] = "failed"
                self._mark_fatal(str(exc))
            self.report["stages"].setdefault(node_id, {"status": "failed", "error": str(exc)})
            return
        async with self.lock:
            self.outputs[node_id] = result.output
            self.statuses[node_id] = "completed"

    async def cancel(self) -> None:
        current = asyncio.current_task()
        pending = [task for task in self.tasks.values() if task is not current and not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


class Orchestrator:
    def __init__(
        self,
        state: StateStore,
        artifacts: ArtifactStore,
        runner: AgentRunner,
        runs_root: str | Path,
    ) -> None:
        self.state = state
        self.artifacts = artifacts
        self.runner = runner
        self.runs_root = Path(runs_root)

    def dry_run_plan(self, project_id: str, profile: PipelineProfile) -> dict[str, Any]:
        root_config = self.runner.role_configs[AgentRole.ROOT]
        agents = [{"id": AgentRole.ROOT.value, "name": "Root orchestrator",
                   "model_selector": profile.root_model or root_config.model_selector,
                   "thinking": profile.root_thinking or root_config.thinking}]
        agents.extend(
            {"id": step.id, "name": step.name, "model_selector": step.model_selector,
             "depends_on": list(step.depends_on)}
            for step in profile.steps
        )
        return {
            "project_id": project_id,
            "pipeline_profile": profile.to_dict(),
            "agents": agents,
            "state_db": str(self.state.path),
            "runs_root": str(self.runs_root),
        }

    async def run(
        self,
        *,
        project_id: str,
        run_id: str,
        profile: PipelineProfile,
        target: str = "",
        initial_prompt: str = "",
        project_directory: str | Path = "",
        completed_outputs: Mapping[str, dict[str, Any]] | None = None,
        resumed_from: str | None = None,
    ) -> dict[str, Any]:
        if not profile.steps:
            raise ValueError("pipeline profile does not contain any agents")
        reserved = self.state.get_run(run_id)
        if not reserved or reserved["status"] != "running" or reserved["project_id"] != project_id:
            raise ValueError("reserved run does not match the requested project")
        report: dict[str, Any] = {
            "run_id": run_id,
            "project_id": project_id,
            "pipeline_profile": profile.to_dict(),
            "status": "running",
            "stages": {},
        }
        controller: PipelineController | None = None
        root_task: asyncio.Task[Any] | None = None
        watchdog: asyncio.Task[None] | None = None
        try:
            context = project_context(
                target, initial_prompt, str(project_directory),
                connections=masked_connections(self.state),
            )
            if completed_outputs:
                context["resumed_from_run"] = {
                    "run_id": resumed_from,
                    "completed_workers": sorted(node_id for node_id in completed_outputs if node_id in {s.id for s in profile.steps}),
                }
            mailbox = AgentMailbox(self.state, self.artifacts, run_id)
            controller = PipelineController(self, profile, run_id, context, report, mailbox, completed_outputs)
            root_session: list[Any] = [None]
            root_task = asyncio.create_task(
                self.runner.run_root(
                    context, run_id,
                    host_tools=controller.tool_definitions(),
                    host_tool_handler=controller.handle_tool,
                    worker_count=len(profile.steps),
                    root_prompt=profile.root_prompt,
                    model_selector=profile.root_model or None,
                    thinking=profile.root_thinking or None,
                    on_session=lambda s: root_session.__setitem__(0, s),
                    on_invocation_started=controller.bind_root_invocation,
                ),
                name=f"pipeline-root-{run_id}",
            )
            watchdog = asyncio.create_task(
                self._fatal_watchdog(controller, root_task, root_session),
                name=f"pipeline-fatal-watchdog-{run_id}",
            )
            root_result = await root_task
            report["stages"][AgentRole.ROOT.value] = self._result_summary(root_result)
            if controller.fatal_error:
                return await self._finish(run_id, report, "blocked", controller.fatal_error)
            if root_result.status != AgentStatus.COMPLETED:
                return await self._finish(run_id, report, "blocked", root_result.error or "root orchestrator failed")
            if not controller.finish_requested:
                return await self._finish(run_id, report, "blocked", "root orchestrator ended before finish_pipeline")
            return await self._finish(run_id, report, "completed", None)
        except asyncio.CancelledError:
            if root_task is not None and not root_task.done():
                root_task.cancel()
                await asyncio.gather(root_task, return_exceptions=True)
            if controller is not None:
                await controller.cancel()
            self.state.update_run(run_id, "failed", "run cancelled")
            raise
        except Exception as exc:
            if controller is not None:
                await controller.cancel()
            return await self._finish(run_id, report, "blocked", str(exc))
        finally:
            if watchdog is not None:
                watchdog.cancel()
                await asyncio.gather(watchdog, return_exceptions=True)
            if controller is not None:
                await controller.cancel()

    async def _fatal_watchdog(
        self,
        controller: PipelineController,
        root_task: "asyncio.Task[Any]",
        root_session: list[Any],
    ) -> None:
        """End the root session once the pipeline is unwinnable.

        A failed worker means finish_pipeline can never succeed; without this,
        a root that keeps polling only stops at its own timeout (an hour by
        default). Drain in-flight workers (run_agent refuses new ones after
        fatal), give the root a grace window to wrap up, then abort it.
        """
        await controller.fatal.wait()
        while True:
            pending = [task for task in controller.tasks.values() if not task.done()]
            if not pending:
                break
            await asyncio.wait(pending)
        await asyncio.wait({root_task}, timeout=FATAL_ROOT_GRACE_SECONDS)
        if root_task.done():
            return
        session = root_session[0]
        if session is None:
            return
        try:
            await session.abort()
        except Exception:
            pass
        await asyncio.wait({root_task}, timeout=FATAL_ABORT_GRACE_SECONDS)
        if not root_task.done():
            try:
                await session.close()
            except Exception:
                pass

    async def _finish(
        self,
        run_id: str,
        report: dict[str, Any],
        status: str,
        error: str | None,
    ) -> dict[str, Any]:
        report["status"] = status
        if error:
            report["error"] = error
        artifact = self.artifacts.write_text(
            run_id, "report.json", json.dumps(report, indent=2, sort_keys=True), "application/json",
        )
        report["report_artifact_id"] = artifact.artifact_id
        self.state.update_run(run_id, status, error)
        return report

    @staticmethod
    def _result_summary(result: Any) -> dict[str, Any]:
        return {
            "status": result.status.value,
            "model_selector": result.model_selector,
            "duration_seconds": result.duration_seconds,
            "error": result.error,
            "artifact_ids": list(result.artifact_ids),
            "output": result.output,
        }

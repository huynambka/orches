"""Async wrapper around the interactive ``omp --mode rpc`` CLI."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

log = logging.getLogger(__name__)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_MAX_CHUNK_BYTES = 64 * 1024 * 1024


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


@dataclass(slots=True)
class OmpResult:
    output: str
    stderr: str
    returncode: int
    duration_s: float
    events: list[dict[str, Any]] = field(default_factory=list)
    raw_output: str = ""
    error: str | None = None
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and bool(self.output.strip()) and self.error is None


def _extract_text(value: Any) -> str:
    if isinstance(value, str): return value
    if isinstance(value, list): return "".join(_extract_text(item) for item in value)
    if isinstance(value, dict):
        if value.get("type") in {"text", "output_text"} and isinstance(value.get("text"), str): return value["text"]
        if isinstance(value.get("content"), (str, list)): return _extract_text(value["content"])
        if isinstance(value.get("text"), str): return value["text"]
    return ""


def _extract_assistant_text(value: Any) -> str:
    if isinstance(value, dict):
        if value.get("role") == "assistant": return _extract_text(value.get("content", value))
        return ""
    if isinstance(value, list):
        result = ""
        for item in value:
            text = _extract_assistant_text(item)
            if text: result = text
        return result
    return ""


def _parse_json_events(raw: str) -> tuple[list[dict[str, Any]], str, str | None]:
    events: list[dict[str, Any]] = []
    assistant_text = ""
    errors: list[str] = []
    for number, line in enumerate(raw.splitlines(), 1):
        if not line.strip(): continue
        try: event = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"line {number}: {exc.msg}"); continue
        if not isinstance(event, dict): errors.append(f"line {number}: event is not an object"); continue
        events.append(event)
        if event.get("type") == "message_end":
            text = _extract_assistant_text(event.get("message"))
            if text: assistant_text = text
        elif event.get("type") == "agent_end":
            text = _extract_assistant_text(event.get("messages"))
            if text: assistant_text = text
    return events, assistant_text, "; ".join(errors) if errors and not assistant_text else None


def _public_event(event: dict[str, Any]) -> dict[str, Any] | None:
    event_type = event.get("type")
    if event_type in {"ready", "response", "rpc_chunk", "rpc_frame_error", "extension_ui_response", "host_request"}:
        return None
    if event_type == "message_update":
        update = event.get("assistantMessageEvent")
        if isinstance(update, dict) and update.get("type") == "thinking_delta":
            return None
    message = event.get("message")
    if isinstance(message, dict) and message.get("role") == "assistant":
        content = message.get("content")
        if isinstance(content, list):
            message = dict(message)
            message["content"] = [part for part in content if not (isinstance(part, dict) and part.get("type") in {"thinking", "reasoning"})]
            event = dict(event); event["message"] = message
    return event


def _terminal_stop_reason(agent_end: dict[str, Any] | None) -> str | None:
    """Stop reason of a terminal agent_end: top-level if present, else the last message's."""
    if not agent_end:
        return None
    stop = agent_end.get("stopReason")
    if isinstance(stop, str) and stop:
        return stop
    messages = agent_end.get("messages")
    if isinstance(messages, list) and messages and isinstance(messages[-1], dict):
        last = messages[-1].get("stopReason")
        if isinstance(last, str) and last:
            return last
    return None


HostToolHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class OmpRpcSession:
    def __init__(
        self, client: "OmpClient", proc: asyncio.subprocess.Process, *,
        stdout_path: str | Path | None, stderr_path: str | Path | None,
        max_output_bytes: int, timeout: int, host_tool_handler: HostToolHandler | None = None,
    ) -> None:
        self.client, self.proc = client, proc
        self.stdout_path, self.stderr_path = stdout_path, stderr_path
        self.max_output_bytes, self.timeout = max_output_bytes, timeout
        self.started = time.monotonic(); self.events: list[dict[str, Any]] = []
        self._stderr = bytearray(); self._stdout_bytes = 0; self._stderr_bytes = 0
        self._write_lock = asyncio.Lock(); self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._terminal = asyncio.Event(); self._reader_done = asyncio.Event(); self._error: str | None = None
        self._agent_end: dict[str, Any] | None = None; self._last_assistant = ""
        self._ready = asyncio.Event()
        self._host_tool_handler = host_tool_handler
        self._host_tool_tasks: dict[str, asyncio.Task[None]] = {}
        self._host_cleanup_tasks: set[asyncio.Task[None]] = set()
        self._host_responded: set[str] = set()
        self._reader_task = asyncio.create_task(self._read_stdout(), name="omp-rpc-stdout")
        self._stderr_task = asyncio.create_task(self._read_stderr(), name="omp-rpc-stderr")

    async def record_user_steer(self, text: str) -> None:
        event = {"type": "user_steer", "text": text}
        await self._append(self.stdout_path, json.dumps(event, ensure_ascii=False) + "\n")
        self.events.append(event)

    async def record_stage_input(self, text: str) -> None:
        event = {"type": "stage_input", "text": text}
        await self._append(self.stdout_path, json.dumps(event, ensure_ascii=False) + "\n")
        self.events.append(event)

    async def _write_json(self, payload: dict[str, Any]) -> None:
        if self.proc.stdin is None: raise RuntimeError("omp RPC stdin is closed")
        async with self._write_lock:
            self.proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode())
            await self.proc.stdin.drain()

    async def _ack(self, payload: dict[str, Any], timeout: float) -> None:
        ident = str(payload["id"]); loop = asyncio.get_running_loop(); future = loop.create_future(); self._pending[ident] = future
        try:
            await self._write_json(payload)
            response = await asyncio.wait_for(future, timeout=timeout)
            if response.get("ok") is False or response.get("success") is False or response.get("error"):
                raise RuntimeError(str(response.get("error") or "RPC command rejected"))
        finally: self._pending.pop(ident, None)

    async def steer(self, text: str) -> None:
        if not text or "\x00" in text: raise ValueError("steer text must be nonempty and contain no NUL")
        await self._ack({"id": f"steer-{time.monotonic_ns()}", "type": "steer", "message": text}, min(10.0, max(1.0, self.timeout - (time.monotonic() - self.started))))

    async def abort(self) -> None:
        """Abort the active agent run; omp answers with a terminal agent_end."""
        await self._ack({"id": f"abort-{time.monotonic_ns()}", "type": "abort"}, 10.0)
    async def _send_host_tool_result(
        self, ident: str, value: dict[str, Any], *, is_error: bool = False,
    ) -> None:
        if ident in self._host_responded:
            return
        self._host_responded.add(ident)
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
        await self._write_json({
            "type": "host_tool_result",
            "id": ident,
            "result": {
                "content": [{"type": "text", "text": text}],
                "isError": is_error,
            },
        })

    async def _run_host_tool(self, ident: str, name: str, arguments: dict[str, Any]) -> None:
        try:
            if self._host_tool_handler is None:
                await self._send_host_tool_result(
                    ident, {"error": "host tool handler is unavailable"}, is_error=True,
                )
                return
            result = await self._host_tool_handler(name, arguments)
            if not isinstance(result, dict):
                raise TypeError("host tool handler must return an object")
            is_error = bool(result.pop("_is_error", False))
            await self._send_host_tool_result(ident, result, is_error=is_error)
        except asyncio.CancelledError:
            await self._send_host_tool_result(ident, {"error": "host tool call cancelled"}, is_error=True)
            raise
        except Exception as exc:
            await self._send_host_tool_result(ident, {"error": str(exc)}, is_error=True)
        finally:
            self._host_tool_tasks.pop(ident, None)

    async def _finish_host_tool_cancel(self, ident: str, task: asyncio.Task[None]) -> None:
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        if ident not in self._host_responded and self.proc.stdin is not None:
            try:
                await self._send_host_tool_result(
                    ident, {"error": "host tool call cancelled"}, is_error=True,
                )
            except (BrokenPipeError, ConnectionError, RuntimeError):
                pass

    def _dispatch_host_tool_call(self, event: dict[str, Any]) -> None:
        ident = event.get("id")
        name = event.get("toolName")
        arguments = event.get("arguments", {})
        if not isinstance(ident, str) or not ident or not isinstance(name, str) or not isinstance(arguments, dict):
            if isinstance(ident, str) and ident:
                task = asyncio.create_task(
                    self._send_host_tool_result(ident, {"error": "invalid host tool call"}, is_error=True)
                )
                self._host_cleanup_tasks.add(task)
                task.add_done_callback(self._host_cleanup_tasks.discard)
            return
        if ident in self._host_tool_tasks or ident in self._host_responded:
            task = asyncio.create_task(
                self._send_host_tool_result(ident, {"error": "duplicate host tool call id"}, is_error=True)
            )
            self._host_cleanup_tasks.add(task)
            task.add_done_callback(self._host_cleanup_tasks.discard)
            return
        self._host_tool_tasks[ident] = asyncio.create_task(
            self._run_host_tool(ident, name, arguments), name=f"omp-host-tool-{name}-{ident}"
        )

    def _dispatch_host_tool_cancel(self, event: dict[str, Any]) -> None:
        target = event.get("targetId")
        if not isinstance(target, str):
            return
        task = self._host_tool_tasks.get(target)
        if task is None or task.done():
            return
        task.cancel()
        cleanup = asyncio.create_task(self._finish_host_tool_cancel(target, task))
        self._host_cleanup_tasks.add(cleanup)
        cleanup.add_done_callback(self._host_cleanup_tasks.discard)

    def _record_stream_error(self, exc: Exception) -> None:
        """Keep real failures; ignore pipe teardown that follows a terminal agent_end.

        omp may close stdio abruptly right after it reports its terminal result;
        that reset must not turn a finished invocation into a failure.
        """
        if self._terminal.is_set() and isinstance(exc, (ConnectionError, OSError)):
            return
        self._error = self._error or str(exc)

    async def _read_stdout(self) -> None:
        try:
            while self.proc.stdout:
                line = await self.proc.stdout.readline()
                if not line: break
                self._stdout_bytes += len(line)
                if self._stdout_bytes > self.max_output_bytes: self._error = f"output exceeded {self.max_output_bytes} bytes"; break
                try: event = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError): self._error = "malformed RPC JSON"; break
                if not isinstance(event, dict): self._error = "RPC event is not an object"; break
                event_type = event.get("type")
                if event_type in {"ready", "rpc_ready", "session_ready"}:
                    self._ready.set()
                    continue
                ident = event.get("id")
                if event_type in {"response", "ack", "command_ack"} and ident is not None:
                    future = self._pending.get(str(ident))
                    if future and not future.done(): future.set_result(event)
                    continue
                if event_type == "rpc_frame_error": self._error = "RPC frame error"; break
                if event_type == "rpc_chunk":
                    if not self._validate_chunk(event): self._error = "invalid RPC chunk"; break
                    continue
                if event_type in {"host_request", "extension_ui_request", "request"}:
                    if self.proc.stdin: await self._write_json({"type": "extension_ui_response", "id": event.get("id"), "cancelled": True})
                    continue
                if event_type == "host_tool_call":
                    self._dispatch_host_tool_call(event)
                    continue
                if event_type == "host_tool_cancel":
                    self._dispatch_host_tool_cancel(event)
                    continue
                public = _public_event(event)
                if public is None: continue
                self.events.append(public)
                if public.get("type") == "message_end":
                    message = public.get("message")
                    if isinstance(message, dict) and message.get("role") == "assistant": self._last_assistant = _extract_text(message.get("content"))
                if public.get("type") == "agent_end" and public.get("isTerminal", True) is not False:
                    self._agent_end = public; self._terminal.set()
                await self._append(self.stdout_path, json.dumps(public, ensure_ascii=False) + "\n")
        except asyncio.CancelledError: raise
        except Exception as exc: self._record_stream_error(exc)
        finally:
            for future in self._pending.values():
                if not future.done(): future.set_exception(RuntimeError(self._error or "RPC stream ended"))
            self._reader_done.set()

    @staticmethod
    def _validate_chunk(event: dict[str, Any]) -> bool:
        data = event.get("data", event.get("chunk"))
        if not isinstance(data, str):
            return False
        encoded = data.encode("utf-8")
        if len(encoded) > _MAX_CHUNK_BYTES:
            return False
        byte_length = event.get("byteLength")
        if byte_length is not None and byte_length != len(encoded):
            return False
        count, index = event.get("count"), event.get("index")
        return (count is None or isinstance(count, int) and count > 0) and (index is None or isinstance(index, int) and 0 <= index < count)

    async def _read_stderr(self) -> None:
        try:
            while self.proc.stderr:
                data = await self.proc.stderr.read(65536)
                if not data:
                    break
                self._stderr_bytes += len(data)
                if self._stderr_bytes > self.max_output_bytes:
                    self._error = self._error or f"stderr exceeded {self.max_output_bytes} bytes"
                    break
                self._stderr.extend(data)
                await self._append(self.stderr_path, data.decode("utf-8", errors="replace"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._record_stream_error(exc)

    async def _append(self, path: str | Path | None, text: str) -> None:
        if path is None: return
        async with self._write_lock:
            target = Path(path); target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as handle: handle.write(text); handle.flush()

    async def finish(self) -> OmpResult:
        try:
            await asyncio.wait_for(self._terminal.wait(), timeout=max(0.1, self.timeout - (time.monotonic() - self.started)))
        except asyncio.TimeoutError: self._error = self._error or "timeout"
        if self._error is None and self._agent_end is None: self._error = "RPC ended before terminal agent_end"
        await self.close()
        raw = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in self.events)
        _, output, parse_error = _parse_json_events(raw)
        error = self._error or parse_error
        stop = _terminal_stop_reason(self._agent_end)
        if stop in {"error", "aborted"}:
            error = error or ("agent run aborted" if stop == "aborted" else "agent stopped with error")
        rc = self.proc.returncode if self.proc.returncode is not None else 0
        if rc != 0: error = error or f"omp exited with status {rc}"
        return OmpResult(output=output or self._last_assistant, stderr=_strip_ansi(bytes(self._stderr).decode("utf-8", errors="replace")), returncode=rc,
                         duration_s=time.monotonic() - self.started, events=self.events, raw_output=raw, error=error, timed_out=error == "timeout")

    async def close(self) -> None:
        current = asyncio.current_task()
        tasks = [
            task for task in (*self._host_tool_tasks.values(), *self._host_cleanup_tasks)
            if task is not current and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.proc.stdin:
            try: self.proc.stdin.close()
            except Exception: pass
        try: await asyncio.wait_for(self.proc.wait(), timeout=2)
        except (asyncio.TimeoutError, ProcessLookupError): await self.client._terminate(self.proc)
        for task in (self._reader_task, self._stderr_task):
            if task is not current and not task.done(): task.cancel()
        await asyncio.gather(
            *(task for task in (self._reader_task, self._stderr_task) if task is not current),
            return_exceptions=True,
        )


class OmpClient:
    def __init__(self, omp_bin: str = "omp", default_model: str | None = None) -> None:
        resolved = shutil.which(omp_bin)
        if resolved is None: raise FileNotFoundError(f"omp binary not found: {omp_bin!r}")
        self.omp_bin, self.default_model = resolved, default_model

    async def start_session(
        self, prompt: str, *, append_system_prompt: str | None = None,
        cwd: str | Path | None = None, project_directory: str | Path | None = None,
        session_dir: str | Path | None = None, model: str | None = None,
        thinking: str = "medium", tools: list[str] | None = None,
        context_files: list[str | Path] | None = None, timeout: int = 300, approval_mode: str = "always-ask",
        extra_env: Mapping[str, str] | None = None, require_mcp_ready: bool = False,
        max_output_bytes: int = 2_000_000, stdout_path: str | Path | None = None,
        stderr_path: str | Path | None = None, extra_args: list[str] | None = None,
        host_tools: list[dict[str, Any]] | None = None,
        host_tool_handler: HostToolHandler | None = None, no_builtin_tools: bool = False,
    ) -> OmpRpcSession:
        if "\x00" in prompt: raise ValueError("prompt contains NUL byte")
        if max_output_bytes <= 0: raise ValueError("max_output_bytes must be positive")
        if approval_mode not in {"always-ask", "always-ask-cached", "write", "yolo"}: raise ValueError("unsupported approval mode")
        if no_builtin_tools and tools:
            raise ValueError("tools and no_builtin_tools are mutually exclusive")
        args = [self.omp_bin, "--mode", "rpc", "--no-title", "--no-extensions", "--no-skills", "--no-rules", "--no-lsp", "--no-pty", "--approval-mode", approval_mode, "--thinking", thinking]
        if no_builtin_tools:
            args += ["--no-tools", "--no-context-files"]
        selected = model or self.default_model
        if selected: args += ["--model", selected]
        if append_system_prompt: args += ["--append-system-prompt", append_system_prompt]
        if project_directory: args += ["--add-dir", str(Path(project_directory).resolve())]
        if session_dir: args += ["--session-dir", str(Path(session_dir).resolve())]
        if tools: args += ["--tools", ",".join(tools)]
        if context_files:
            for item in context_files: args.append("@" + str(item))
        if extra_args: args.extend(extra_args)
        env = os.environ.copy(); env.update({str(k): str(v) for k, v in (extra_env or {}).items()})
        if require_mcp_ready: env["OMP_MCP_REQUIRE_READY"] = "1"
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
        # Single RPC events (e.g. whole-file write echoes) can far exceed asyncio's
        # 64 KiB default readline limit; size the buffer to the session budget.
        proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                     stdin=asyncio.subprocess.PIPE, cwd=str(cwd) if cwd else None, env=env,
                                                     creationflags=flags, start_new_session=os.name != "nt",
                                                     limit=max(2 ** 16, min(max_output_bytes, 64_000_000)))
        session = OmpRpcSession(
            self, proc, stdout_path=stdout_path, stderr_path=stderr_path,
            max_output_bytes=max_output_bytes, timeout=timeout,
            host_tool_handler=host_tool_handler,
        )
        try:
            await asyncio.wait_for(self._wait_ready(session), timeout=min(30, timeout))
            if host_tools is not None:
                await session._ack(
                    {"id": "host-tools", "type": "set_host_tools", "tools": host_tools},
                    min(10.0, timeout),
                )
            await session._ack({"id": "initial", "type": "prompt", "message": prompt}, min(10.0, timeout))
            return session
        except Exception:
            await session.close(); raise

    async def _wait_ready(self, session: OmpRpcSession) -> None:
        while not session._reader_done.is_set():
            if session._error: raise RuntimeError(session._error)
            if session._ready.is_set(): return
            await asyncio.sleep(0.01)
        raise RuntimeError(session._error or "RPC ended before ready")

    @staticmethod
    async def _terminate(proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None: return
        try:
            if os.name != "nt" and proc.pid:
                os.killpg(proc.pid, signal.SIGTERM); await asyncio.wait_for(proc.wait(), timeout=2)
            else: proc.terminate(); await asyncio.wait_for(proc.wait(), timeout=2)
        except (asyncio.TimeoutError, ProcessLookupError):
            try:
                if os.name != "nt" and proc.pid: os.killpg(proc.pid, signal.SIGKILL)
                else: proc.kill()
            except ProcessLookupError: pass
            await proc.wait()

"""Host tools letting workers ask the root orchestrator questions, and vice versa.

Messages are an asynchronous inbox: sending returns at once with a message id,
the recipient reads its inbox and answers with a reply bound to that id, and
the asker reads the reply later. Workers talk only to the root (they do not
need to know about each other); the root may ask any running worker. The host
binds the caller to its own invocation, so an agent cannot speak as another
one, and replies always go back to the question's author.
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import Any

from .artifacts import ArtifactStore
from .models import AgentRole
from .state import AgentMessageError, StateStore

AGENT_MESSAGE_TOOL_NAMES = ("send_agent_message", "reply_agent_message", "read_agent_messages")

MAX_MESSAGE_CHARS = 4000
MAX_WAIT_SECONDS = 10.0

_REPLY_TOOL: dict[str, Any] = {
    "name": "reply_agent_message",
    "description": (
        "Answer a question from your inbox. `message_id` is the id of the message you "
        "received; the reply goes back to its author. Each question takes one reply."
    ),
    "parameters": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "message_id": {"type": "integer"},
            "text": {"type": "string", "maxLength": MAX_MESSAGE_CHARS},
        },
        "required": ["message_id", "text"],
    },
}

_READ_TOOL: dict[str, Any] = {
    "name": "read_agent_messages",
    "description": (
        "Read questions and replies sent to you, oldest first. Pass `after_id` from the "
        "previous call's next_after_id to get only newer messages. `wait_seconds` "
        "(0-10) blocks briefly until a new message arrives; an empty list means none."
    ),
    "parameters": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "after_id": {"type": "integer", "minimum": 0},
            "wait_seconds": {"type": "number", "minimum": 0, "maximum": MAX_WAIT_SECONDS},
        },
        "required": [],
    },
}

ROOT_MESSAGE_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "send_agent_message",
        "description": (
            "Ask a running worker a question. `to` is the worker node id. Returns the "
            "message id immediately; the answer arrives later in your inbox "
            "(read_agent_messages) with reply_to set to that id."
        ),
        "parameters": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "to": {"type": "string"},
                "text": {"type": "string", "maxLength": MAX_MESSAGE_CHARS},
            },
            "required": ["to", "text"],
        },
    },
    _REPLY_TOOL,
    _READ_TOOL,
]

WORKER_MESSAGE_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "send_agent_message",
        "description": (
            "Ask the root orchestrator a question (a decision, a clarification, a missing "
            "fact). Returns the message id immediately; the answer arrives later in your "
            "inbox (read_agent_messages) with reply_to set to that id."
        ),
        "parameters": {
            "type": "object", "additionalProperties": False,
            "properties": {"text": {"type": "string", "maxLength": MAX_MESSAGE_CHARS}},
            "required": ["text"],
        },
    },
    _REPLY_TOOL,
    _READ_TOOL,
]


def _error(message: str) -> dict[str, Any]:
    return {"_is_error": True, "error": message}


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_text(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    if len(value) > MAX_MESSAGE_CHARS or "\x00" in value:
        return None
    return value


class AgentMailbox:
    """Run-scoped inbox shared by the root and worker sessions of one pipeline run."""

    def __init__(self, state: StateStore, artifacts: ArtifactStore, run_id: str) -> None:
        self.state = state
        self.artifacts = artifacts
        self.run_id = run_id
        self._changed = asyncio.Condition()

    async def handle(self, name: str, arguments: dict[str, Any], invocation_id: int) -> dict[str, Any]:
        """Serve one message tool as the agent bound to `invocation_id`."""
        if not isinstance(arguments, dict):
            return _error("arguments must be an object")
        try:
            if name == "send_agent_message":
                # Workers omit `to`: their only counterpart is the root.
                to = arguments.get("to", AgentRole.ROOT.value)
                if set(arguments) - {"to", "text"} or "text" not in arguments or not isinstance(to, str):
                    return _error("send_agent_message requires text and optionally a string to")
                text = _check_text(arguments["text"])
                if text is None:
                    return _error(f"text must be 1 to {MAX_MESSAGE_CHARS} characters")
                return await self._post(invocation_id, to, text, None)
            if name == "reply_agent_message":
                if set(arguments) != {"message_id", "text"} or not _is_int(arguments["message_id"]):
                    return _error("reply_agent_message requires only an integer message_id and text")
                text = _check_text(arguments["text"])
                if text is None:
                    return _error(f"text must be 1 to {MAX_MESSAGE_CHARS} characters")
                return await self._post(invocation_id, None, text, int(arguments["message_id"]))
            if name == "read_agent_messages":
                if set(arguments) - {"after_id", "wait_seconds"}:
                    return _error("read_agent_messages accepts only after_id and wait_seconds")
                after_id = arguments.get("after_id", 0)
                if not _is_int(after_id) or after_id < 0:
                    return _error("after_id must be a nonnegative integer")
                wait = arguments.get("wait_seconds", 0)
                if isinstance(wait, bool) or not isinstance(wait, (int, float)) or not math.isfinite(wait) \
                        or not 0 <= wait <= MAX_WAIT_SECONDS:
                    return _error(f"wait_seconds must be a number between 0 and {MAX_WAIT_SECONDS:g}")
                return await self._read(invocation_id, int(after_id), float(wait))
            return _error("unknown agent message tool")
        except AgentMessageError as exc:
            return _error(str(exc))

    async def _post(self, sender_id: int, to: str | None, text: str, reply_to: int | None) -> dict[str, Any]:
        safe = self.artifacts.redact_text(text)
        record = self.state.post_agent_message(sender_id, to, safe, reply_to=reply_to)
        async with self._changed:
            self._changed.notify_all()
        return {"message_id": record["id"], "to": record["to"], "reply_to": record["reply_to"]}

    async def _read(self, invocation_id: int, after_id: int, wait: float) -> dict[str, Any]:
        deadline = time.monotonic() + wait
        async with self._changed:
            while True:
                messages = self.state.list_agent_messages(invocation_id, after_id)
                remaining = deadline - time.monotonic()
                if messages or remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(self._changed.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    pass
        return {
            "messages": messages,
            "next_after_id": messages[-1]["id"] if messages else after_id,
        }

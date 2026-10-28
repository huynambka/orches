"""Host tools exposing the connections address book to pipeline agents.

Both the root orchestrator and worker sessions register these; credentials
stay out of prompts and are returned only by an explicit reveal call. A
connection must be claimed by the calling agent before it can be revealed;
the claim is exclusive until the owner releases it or its invocation ends.
"""

from __future__ import annotations

import logging
from typing import Any

from .state import ConnectionUseError, StateStore

log = logging.getLogger(__name__)

CONNECTION_TOOL_NAMES = (
    "read_connections", "claim_connection", "release_connection", "reveal_connection",
)


# Host tools default to the exec approval tier; workers run approval_mode="write",
# which would deny them. This user-policy overlay (passed via --config) allows
# exactly these tools without widening anything else.
CONNECTION_TOOL_APPROVALS_YML = (
    "tools:\n"
    "  approval:\n"
    "    read_connections: allow\n"
    "    claim_connection: allow\n"
    "    release_connection: allow\n"
    "    reveal_connection: allow\n"
)
_NO_ARGS = {"type": "object", "additionalProperties": False, "properties": {}}
_ID_ONLY = {
    "type": "object", "additionalProperties": False,
    "properties": {"id": {"type": "string"}}, "required": ["id"],
}

CONNECTION_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "read_connections",
        "description": (
            "List saved machine connections (name, os, ip; credential masked) with live "
            "usage: in_use and used_by show which agent currently claims each one. "
            "Claim a free connection with claim_connection before using it."
        ),
        "parameters": _NO_ARGS,
    },
    {
        "name": "claim_connection",
        "description": (
            "Register exclusive use of one saved connection for this agent. Fails if "
            "another agent is using it; release_connection frees it again."
        ),
        "parameters": _ID_ONLY,
    },
    {
        "name": "release_connection",
        "description": "Release a connection this agent claimed so other agents can use it.",
        "parameters": _ID_ONLY,
    },
    {
        "name": "reveal_connection",
        "description": (
            "Read one saved connection including its stored credential. Requires that "
            "this agent has claimed the connection first."
        ),
        "parameters": _ID_ONLY,
    },
]


def masked_connection(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "connection_id": record["connection_id"],
        "name": record["name"],
        "os": record["os"],
        "ip": record["ip"],
        "has_cred": record["cred"] is not None,
        "in_use": record["in_use"],
        "used_by": record["used_by"],
    }


def masked_connections(state: StateStore) -> list[dict[str, Any]]:
    return [masked_connection(record) for record in state.list_connections()]


async def handle_connection_tool(
    state: StateStore, name: str, arguments: dict[str, Any], *, caller: str, invocation_id: int,
) -> dict[str, Any]:
    """Serve a connection host tool as one specific agent invocation."""
    if name == "read_connections":
        if arguments:
            return {"_is_error": True, "error": "this tool takes no arguments"}
        return {"connections": masked_connections(state)}
    if name not in CONNECTION_TOOL_NAMES:
        return {"_is_error": True, "error": "unknown connection tool"}
    if set(arguments) != {"id"} or not isinstance(arguments["id"], str):
        return {"_is_error": True, "error": f"{name} requires only a string id"}
    connection_id = arguments["id"]
    try:
        if name == "claim_connection":
            record = state.claim_connection(connection_id, invocation_id)
            log.info("connection %s (%s) claimed by %s", connection_id, record["name"], caller)
            return masked_connection(record)
        if name == "release_connection":
            record = state.release_connection(connection_id, invocation_id)
            log.info("connection %s (%s) released by %s", connection_id, record["name"], caller)
            return masked_connection(record)
        record = state.reveal_claimed_connection(connection_id, invocation_id)
    except ConnectionUseError as exc:
        return {"_is_error": True, "error": str(exc)}
    log.info("connection %s (%s) revealed to %s", record["connection_id"], record["name"], caller)
    return {
        "connection_id": record["connection_id"],
        "name": record["name"],
        "os": record["os"],
        "ip": record["ip"],
        "cred": record["cred"],
    }

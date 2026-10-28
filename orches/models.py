"""Typed contracts shared by the Orches orchestrator and agents."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AgentRole(StrEnum):
    ROOT = "root"


class AgentStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class RoleConfig:
    model_selector: str | None = None
    thinking: str = "medium"
    timeout_seconds: int = 300
    approval_mode: str = "always-ask"
    prompt_version: str = "1"


@dataclass(frozen=True, slots=True)
class AgentInvocation:
    run_id: str
    role: AgentRole | str
    model_selector: str | None
    thinking: str
    status: AgentStatus
    started_at: str
    finished_at: str | None = None
    prompt_version: str = "1"
    returncode: int | None = None
    error: str | None = None
    output_artifact_id: str | None = None
    stderr_artifact_id: str | None = None
    invocation_id: int | None = None


@dataclass(frozen=True, slots=True)
class AgentResult:
    run_id: str
    role: AgentRole | str
    status: AgentStatus
    output: dict[str, Any] | None = None
    raw_output: str = ""
    stderr: str = ""
    error: str | None = None
    model_selector: str | None = None
    duration_seconds: float = 0.0
    artifact_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    artifact_id: str
    run_id: str
    relative_path: str
    sha256: str
    size: int
    media_type: str
    created_at: str


def _jsonable(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if is_dataclass(value):
        return {key: _jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def to_dict(value: Any) -> Any:
    return _jsonable(value)


def dumps(value: Any) -> str:
    return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, indent=2)


def loads_object(text: str) -> dict[str, Any]:
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value

"""Agent role definitions and default runtime policies."""

from __future__ import annotations

from dataclasses import replace
from typing import Mapping

from .models import AgentRole, RoleConfig


DEFAULT_ROLE_CONFIGS: dict[AgentRole, RoleConfig] = {
    AgentRole.ROOT: RoleConfig(
        thinking="high", timeout_seconds=3600, approval_mode="yolo"
    ),
}


def role_configs_with_overrides(
    overrides: Mapping[str, Mapping[str, object]] | None = None,
) -> dict[AgentRole, RoleConfig]:
    result = dict(DEFAULT_ROLE_CONFIGS)
    for raw_role, values in (overrides or {}).items():
        try:
            role = AgentRole(raw_role)
        except ValueError as exc:
            raise ValueError(
                f"unknown role section [roles.{raw_role}]; only [roles.root] is configurable"
            ) from exc
        current = result[role]
        allowed = {"model_selector", "thinking", "timeout_seconds", "approval_mode", "prompt_version"}
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"unknown role config fields for {role.value}: {sorted(unknown)}")
        normalized = dict(values)
        if normalized.get("model_selector") == "":
            normalized["model_selector"] = None
        result[role] = replace(current, **normalized)
    return result

"""Validated visual pipeline profiles: a DAG of user-defined agent nodes."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Mapping

_PROFILE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_NODE_ID = re.compile(r"^node_[0-9a-f]{32}$")
MAX_NODES = 64


def _text(value: Any, label: str, maximum: int, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{label} must be nonempty")
    if len(value) > maximum:
        raise ValueError(f"{label} must be at most {maximum} characters")
    return value


def _position(value: Any) -> tuple[float, float]:
    if value is None:
        return (0.0, 0.0)
    if isinstance(value, (tuple, list)) and len(value) == 2:
        value = {"x": value[0], "y": value[1]}
    if not isinstance(value, Mapping) or set(value) != {"x", "y"}:
        raise ValueError("position must contain x and y")
    result = []
    for axis in ("x", "y"):
        number = value[axis]
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            raise ValueError("position coordinates must be finite numbers")
        number = float(number)
        if not math.isfinite(number) or abs(number) > 10000:
            raise ValueError("position coordinates must be finite and within ±10000")
        result.append(number)
    return (result[0], result[1])


def _position_dict(position: tuple[float, float]) -> dict[str, float]:
    return {"x": position[0], "y": position[1]}


@dataclass(frozen=True, slots=True)
class PipelineStep:
    id: str
    name: str
    model_selector: str
    prompt: str = ""
    depends_on: tuple[str, ...] = ()
    position: tuple[float, float] = (0.0, 0.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "model_selector": self.model_selector,
            "prompt": self.prompt,
            "depends_on": list(self.depends_on),
            "position": _position_dict(self.position),
        }


@dataclass(frozen=True, slots=True)
class PipelineProfile:
    id: str
    name: str
    steps: tuple[PipelineStep, ...] = ()
    root_prompt: str = ""
    # Empty means "use [roles.root] model_selector from orches.toml".
    root_model: str = ""

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "steps": [step.to_dict() for step in self.steps],
        }
        if self.root_prompt:
            result["root_prompt"] = self.root_prompt
        if self.root_model:
            result["root_model"] = self.root_model
        return result

    def step(self, node_id: str) -> PipelineStep:
        return next(step for step in self.steps if step.id == node_id)


def new_node_id() -> str:
    import uuid

    return f"node_{uuid.uuid4().hex}"


def _validate(profile: PipelineProfile) -> PipelineProfile:
    if not _PROFILE_ID.fullmatch(profile.id):
        raise ValueError(f"invalid pipeline profile id: {profile.id!r}")
    _text(profile.name, "profile name", 160, required=True)
    _text(profile.root_prompt, "root_prompt", 10000)
    _text(profile.root_model, "root_model", 256)
    if len(profile.steps) > MAX_NODES:
        raise ValueError(f"profiles may contain at most {MAX_NODES} nodes")
    ids = [step.id for step in profile.steps]
    if len(ids) != len(set(ids)):
        raise ValueError("profile contains duplicate node ids")
    known = set(ids)
    dependencies: dict[str, set[str]] = {}
    for step in profile.steps:
        if not _NODE_ID.fullmatch(step.id):
            raise ValueError(f"invalid node id: {step.id!r}")
        _text(step.name, "node name", 160, required=True)
        _text(step.model_selector, "model_selector", 256, required=True)
        _text(step.prompt, "prompt", 10000)
        _position(step.position)
        dependencies[step.id] = set(step.depends_on)
        if len(step.depends_on) != len(set(step.depends_on)):
            raise ValueError(f"node {step.id!r} contains duplicate dependencies")
        if step.id in dependencies[step.id] or not dependencies[step.id] <= known:
            raise ValueError(f"node {step.id!r} contains an invalid dependency")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visiting:
            raise ValueError("profile contains a dependency cycle")
        if node_id in visited:
            return
        visiting.add(node_id)
        for dependency in dependencies[node_id]:
            visit(dependency)
        visiting.remove(node_id)
        visited.add(node_id)

    for node_id in ids:
        visit(node_id)
    return profile


def profile_from_dict(raw: Mapping[str, Any]) -> PipelineProfile:
    if not isinstance(raw, Mapping):
        raise ValueError("pipeline profile must be an object")
    allowed = {"id", "name", "steps"}
    optional = {"root_prompt", "root_model"}
    if not allowed <= set(raw) or set(raw) - (allowed | optional) or not isinstance(raw["steps"], list):
        raise ValueError("pipeline profile must contain id, name and steps")
    profile_id = raw["id"]
    if not isinstance(profile_id, str):
        raise ValueError("pipeline profile id must be a string")
    root_prompt = _text(raw.get("root_prompt", ""), "root_prompt", 10000)
    root_model = _text(raw.get("root_model", ""), "root_model", 256)
    steps: list[PipelineStep] = []
    for index, item in enumerate(raw["steps"]):
        required = {"id", "name", "model_selector", "prompt", "depends_on", "position"}
        if not isinstance(item, Mapping) or set(item) != required or not isinstance(item["depends_on"], list):
            raise ValueError(f"pipeline profile step {index} has invalid fields")
        if not all(isinstance(value, str) for value in item["depends_on"]):
            raise ValueError(f"pipeline profile step {index} dependencies must be strings")
        if not isinstance(item["id"], str):
            raise ValueError(f"pipeline profile step {index} id must be a string")
        steps.append(PipelineStep(
            item["id"],
            _text(item["name"], "node name", 160, required=True),
            _text(item["model_selector"], "model_selector", 256, required=True),
            _text(item["prompt"], "prompt", 10000),
            tuple(item["depends_on"]),
            _position(item["position"]),
        ))
    return _validate(PipelineProfile(profile_id, _text(raw["name"], "profile name", 160, required=True),
                                     tuple(steps), root_prompt, root_model))


def profile_from_record(raw: Mapping[str, Any]) -> PipelineProfile:
    return profile_from_dict({key: value for key, value in raw.items() if key != "revision"})

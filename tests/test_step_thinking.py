"""Per-agent effort: profile `thinking` validation and worker session wiring."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from orches.artifacts import ArtifactStore
from orches.agent_runtime import AgentRunner
from orches.models import AgentRole, RoleConfig
from orches.omp import OmpResult
from orches.pipeline_profiles import (
    DEFAULT_STEP_TIMEOUT_SECONDS, DEFAULT_THINKING, MAX_TIMEOUT_SECONDS, MIN_TIMEOUT_SECONDS,
    THINKING_LEVELS, PipelineStep, profile_from_dict,
)
from orches.state import StateStore


def make_running_run(state: StateStore, directory: Path) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    project_id = state.create_project(project_directory=str(directory), name="proj")
    project = state.get_project(project_id)
    run_id, _ = state.reserve_project_run(
        project_id, expected_updated_at=project["updated_at"], profile_json="{}",
    )
    return run_id


NODE = "node_" + "a" * 32


def _raw_step(**overrides: Any) -> dict[str, Any]:
    step = {"id": NODE, "name": "A", "model_selector": "m", "prompt": "", "depends_on": [],
            "position": {"x": 0, "y": 0}}
    step.update(overrides)
    return step


def _raw_profile(step: dict[str, Any]) -> dict[str, Any]:
    return {"id": "p", "name": "P", "steps": [step]}


class StepThinkingValidationTest(unittest.TestCase):
    def test_missing_thinking_defaults_and_roundtrips(self) -> None:
        profile = profile_from_dict(_raw_profile(_raw_step()))
        self.assertEqual(profile.steps[0].thinking, DEFAULT_THINKING)
        self.assertEqual(profile.to_dict()["steps"][0]["thinking"], DEFAULT_THINKING)

    def test_every_omp_level_is_accepted(self) -> None:
        for level in THINKING_LEVELS:
            profile = profile_from_dict(_raw_profile(_raw_step(thinking=level)))
            self.assertEqual(profile.steps[0].thinking, level)

    def test_root_thinking_is_optional_and_validated(self) -> None:
        profile = profile_from_dict(_raw_profile(_raw_step()))
        self.assertEqual(profile.root_thinking, "")
        self.assertNotIn("root_thinking", profile.to_dict())
        raw = {**_raw_profile(_raw_step()), "root_thinking": "max"}
        self.assertEqual(profile_from_dict(raw).to_dict()["root_thinking"], "max")
        for bad in ("turbo", None, 1):
            with self.assertRaises(ValueError, msg=repr(bad)):
                profile_from_dict({**_raw_profile(_raw_step()), "root_thinking": bad})

    def test_unknown_levels_are_rejected(self) -> None:
        for bad in ("turbo", "", 3, None, "High"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                profile_from_dict(_raw_profile(_raw_step(thinking=bad)))


class TimeoutValidationTest(unittest.TestCase):
    def test_missing_timeout_defaults_and_roundtrips(self) -> None:
        profile = profile_from_dict(_raw_profile(_raw_step()))
        self.assertEqual(profile.steps[0].timeout_seconds, DEFAULT_STEP_TIMEOUT_SECONDS)
        self.assertEqual(profile.to_dict()["steps"][0]["timeout_seconds"], DEFAULT_STEP_TIMEOUT_SECONDS)

    def test_step_timeout_bounds(self) -> None:
        for ok in (MIN_TIMEOUT_SECONDS, 600, MAX_TIMEOUT_SECONDS):
            profile = profile_from_dict(_raw_profile(_raw_step(timeout_seconds=ok)))
            self.assertEqual(profile.steps[0].timeout_seconds, ok)
        for bad in (MIN_TIMEOUT_SECONDS - 1, MAX_TIMEOUT_SECONDS + 1, 0, -5, True, "600", 1.5, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                profile_from_dict(_raw_profile(_raw_step(timeout_seconds=bad)))

    def test_pipeline_timeout_is_optional_and_validated(self) -> None:
        profile = profile_from_dict(_raw_profile(_raw_step()))
        self.assertEqual(profile.pipeline_timeout_seconds, 0)
        self.assertNotIn("pipeline_timeout_seconds", profile.to_dict())
        raw = {**_raw_profile(_raw_step()), "pipeline_timeout_seconds": 7200}
        self.assertEqual(profile_from_dict(raw).to_dict()["pipeline_timeout_seconds"], 7200)
        for bad in (5, MAX_TIMEOUT_SECONDS + 1, True, "3600", 1.5, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                profile_from_dict({**_raw_profile(_raw_step()), "pipeline_timeout_seconds": bad})


class _RecordingOmp:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def run(self, prompt: str, **kwargs: Any) -> OmpResult:
        self.calls.append(kwargs)
        return OmpResult(output="done", stderr="", returncode=0, duration_s=0.0)


class WorkerThinkingWiringTest(unittest.IsolatedAsyncioTestCase):
    async def test_worker_session_uses_the_step_level(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = StateStore(root / "state.sqlite3")
            run_id = make_running_run(state, root / "proj")
            omp = _RecordingOmp()
            runner = AgentRunner(
                omp, state, ArtifactStore(root / "runs", state), root / "runs",  # type: ignore[arg-type]
                {AgentRole.ROOT: RoleConfig(thinking="high")},
            )
            step = PipelineStep(id=NODE, name="A", model_selector="m", thinking="xhigh")
            result = await runner.run_agent(step, {"project_directory": str(root / "proj")}, run_id)
            self.assertEqual(result.output, {"text": "done"})
            (call,) = omp.calls
            self.assertEqual(call["thinking"], "xhigh")
            (invocation,) = state.list_invocations(run_id)
            self.assertEqual(invocation["thinking"], "xhigh")


if __name__ == "__main__":
    unittest.main()

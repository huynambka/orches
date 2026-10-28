"""Command-line interface for Orches."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from .agent_runtime import ActiveSessionRegistry, AgentRunner
from .artifacts import ArtifactStore
from .config import AppConfig, load_config
from .omp import OmpClient
from .orchestrator import Orchestrator
from .pipeline_profiles import profile_from_record
from .profile_store import ProfileStore
from .project_files import app_root, assert_project_marker, remove_owned_run
from .state import StateStore


def build_orchestrator(
    config: AppConfig, *, require_omp: bool, sessions: ActiveSessionRegistry | None = None,
    project_directory: str | Path | None = None,
) -> Orchestrator:
    state = StateStore(config.state_db, legacy_db=config.legacy_state_db)
    root = app_root(Path(project_directory).resolve()) / "runs" if project_directory else config.runs_root
    artifacts = ArtifactStore(root, state)
    omp = OmpClient(config.omp_bin) if require_omp else None
    runner = AgentRunner(
        omp, state, artifacts, root, config.role_configs,
        max_prompt_bytes=config.max_prompt_bytes, max_output_bytes=config.max_output_bytes,
        sessions=sessions,
    )
    return Orchestrator(state, artifacts, runner, root)


def _print(value: Any, as_json: bool) -> None:
    if as_json or not isinstance(value, str):
        print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    else:
        print(value)


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path)
    parser.add_argument("--json", action="store_true", dest="as_json")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="orches")
    subparsers = parser.add_subparsers(dest="command", required=True)

    api = subparsers.add_parser("api")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8000)
    api.add_argument("--config", type=Path)

    run = subparsers.add_parser("run")
    run_sub = run.add_subparsers(dest="run_command", required=True)
    start = run_sub.add_parser("start")
    start.add_argument("project_id")
    start.add_argument("--dry-run", action="store_true")
    _add_common(start)
    show = run_sub.add_parser("show")
    show.add_argument("run_id")
    _add_common(show)

    projects = subparsers.add_parser("projects")
    projects_sub = projects.add_subparsers(dest="projects_command", required=True)
    listing = projects_sub.add_parser("list")
    _add_common(listing)
    return parser


async def _dispatch(args: argparse.Namespace) -> Any:
    config = load_config(Path.cwd(), args.config)
    if args.command == "api":
        try:
            import uvicorn
            from .api import create_app
        except ImportError as exc:
            raise RuntimeError("API dependencies are not installed; install orches[web]") from exc

        server_config = uvicorn.Config(create_app(config), host=args.host, port=args.port)
        await uvicorn.Server(server_config).serve()
        return {"status": "stopped"}

    state = StateStore(config.state_db, legacy_db=config.legacy_state_db)

    if args.command == "projects":
        return {"projects": state.list_projects()}

    if args.command == "run" and args.run_command == "show":
        run = state.get_run(args.run_id)
        if not run:
            raise ValueError(f"run not found: {args.run_id}")
        return {"run": run, "artifacts": state.list_artifacts(args.run_id)}

    if args.command == "run" and args.run_command == "start":
        project = state.get_project(args.project_id)
        if project is None:
            raise ValueError(f"project not found: {args.project_id}")
        if not project["target"]:
            raise ValueError("set a project objective before starting")
        profile_id = project.get("pipeline_profile")
        if not profile_id:
            raise ValueError("project does not have a pipeline profile selected")
        record = ProfileStore(config.profiles_db).get(profile_id)
        if record is None:
            raise ValueError(f"pipeline profile is not configured: {profile_id}")
        profile = profile_from_record(record)
        if not profile.steps:
            raise ValueError("pipeline profile is an empty draft")
        directory = project["project_directory"]
        assert_project_marker(directory, project["project_id"])
        orchestrator = build_orchestrator(config, require_omp=not args.dry_run, project_directory=directory)
        if args.dry_run:
            return orchestrator.dry_run_plan(project["project_id"], profile)
        run_id, old_ids = state.reserve_project_run(
            project["project_id"], expected_updated_at=project["updated_at"], source="cli",
            profile_json=json.dumps(profile.to_dict(), sort_keys=True),
        )
        for old_id in old_ids:
            remove_owned_run(directory, old_id)
        return await orchestrator.run(
            project_id=project["project_id"], run_id=run_id, profile=profile,
            target=project["target"], initial_prompt=project["initial_prompt"],
            project_directory=directory,
        )

    raise ValueError("unsupported command")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = asyncio.run(_dispatch(args))
    except (ValueError, RuntimeError, FileNotFoundError) as exc:
        print(f"error: {exc}")
        return 1
    except KeyboardInterrupt:
        return 130
    if result is not None:
        _print(result, getattr(args, "as_json", False))
    return 0

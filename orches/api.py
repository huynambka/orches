"""Local monitoring API for Orches projects and runs."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from .agent_runtime import ActiveSessionRegistry
from .artifacts import ArtifactStore
from .auth import AdminSessions, TOKEN_TTL_SECONDS
from .config import AppConfig, load_config
from .models import AgentRole
from .pipeline_profiles import profile_from_dict, profile_from_record
from .profile_store import ProfileConflictError, ProfileNotFoundError, ProfileStore
from .project_files import (
    ProjectDirectoryError, app_root, assert_project_marker, cleanup_orphan_runs,
    ensure_project_marker, read_marker, remove_owned_run, validate_project_directory,
)
from .state import ConnectionUseError, ProjectConflictError, StateStore
from .project_mcp import (
    McpConfigError, McpFileError, McpRevisionError, ProjectMcpService,
    read_project_mcp, safe_summary, validate_name, validate_server,
)

_TERMINAL = {"completed", "failed", "blocked"}
_LOG = logging.getLogger("orches.api")


def _progress(state: StateStore, run_id: str) -> dict[str, int]:
    snapshot = state.get_run_profile(run_id)
    worker_ids = [step.id for step in profile_from_dict(snapshot).steps] if snapshot else []
    statuses = {role: "pending" for role in [AgentRole.ROOT.value, *worker_ids]}
    for invocation in state.list_invocations(run_id):
        if invocation["role"] in statuses:
            statuses[invocation["role"]] = invocation["status"]
    return {"total": len(statuses), "completed": sum(v == "completed" for v in statuses.values()),
            "failed": sum(v == "failed" for v in statuses.values()),
            "blocked": sum(v == "blocked" for v in statuses.values()),
            "running": sum(v == "running" for v in statuses.values())}


def _agent(
    invocation: dict[str, Any],
    sessions: ActiveSessionRegistry | None = None,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    chat_available = (
        invocation["status"] == "running"
        and sessions is not None
        and sessions.has(invocation["run_id"], invocation["id"])
    )
    return {"invocation_id": invocation["id"], "role": invocation["role"],
            "model_selector": invocation["model_selector"], "thinking": invocation["thinking"],
            "status": invocation["status"], "started_at": invocation["started_at"],
            "finished_at": invocation["finished_at"], "returncode": invocation["returncode"],
            "error": invocation["error"], "output_artifact_id": invocation["output_artifact_id"],
            "stderr_artifact_id": invocation["stderr_artifact_id"], "chat_available": chat_available,
            "reused": invocation["prompt_version"] == "reused",
            # Model requests recorded for this invocation; null until one completes
            # (reused workers made none in this run).
            "usage": usage}


def _project_summary(
    state: StateStore, project: dict[str, Any], profile_store: ProfileStore | None = None,
) -> dict[str, Any]:
    current = state.get_current_run(project["project_id"])
    total = 0
    if current is None and profile_store is not None and project.get("pipeline_profile"):
        selected = profile_store.get(project["pipeline_profile"])
        if selected is not None:
            total = 1 + len(selected["steps"])
    progress = _progress(state, current["run_id"]) if current else {
        "total": total, "completed": 0, "failed": 0, "blocked": 0, "running": 0,
    }
    return {
        "project_id": project["project_id"],
        "name": project["name"],
        "status": project["status"],
        "target": project["target"],
        "initial_prompt": project["initial_prompt"],
        "project_directory": project["project_directory"],
        "pipeline_profile": project.get("pipeline_profile"),
        "created_at": project["created_at"],
        "updated_at": project["updated_at"],
        "current_run": current,
        "progress": progress,
    }


def _log_page(
    state: StateStore,
    runs_root: Path,
    run_id: str,
    invocation_id: str,
    stream: str,
    offset: int,
    limit: int,
    redact: Callable[[str], str],
) -> dict[str, Any]:
    if stream not in {"stdout", "stderr"}:
        raise HTTPException(status_code=400, detail="stream must be stdout or stderr")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be non-negative")
    run = state.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    try:
        invocation_number = int(invocation_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=404, detail="invocation not found")
    invocation = next((item for item in state.list_invocations(run_id) if item["id"] == invocation_number), None)
    if invocation is None:
        raise HTTPException(status_code=404, detail="invocation not found")
    role = invocation["role"]
    filename = f"{role}.{'stdout.jsonl' if stream == 'stdout' else 'stderr.txt'}"
    base_root = runs_root.resolve()
    run_root = (base_root / run_id).resolve()
    if run_root != base_root and base_root not in run_root.parents:
        raise HTTPException(status_code=404, detail="log not found")
    target = (run_root / "raw" / filename).resolve()
    if run_root != target and run_root not in target.parents:
        raise HTTPException(status_code=404, detail="log not found")
    terminal = invocation["status"] in _TERMINAL
    if not target.exists():
        return {
            "run_id": run_id, "invocation_id": invocation_number, "stream": stream,
            "lines": [], "next_offset": offset, "eof": terminal,
        }
    try:
        data = target.read_bytes()
    except OSError:
        return {
            "run_id": run_id, "invocation_id": invocation_number, "stream": stream,
            "lines": [], "next_offset": offset, "eof": False,
        }
    available = data[offset:]
    chunks = available.splitlines(keepends=True)
    complete = True
    if chunks and not chunks[-1].endswith((b"\n", b"\r")):
        complete = terminal
        if not terminal:
            chunks = chunks[:-1]
    selected = chunks[:limit]
    consumed = sum(len(chunk) for chunk in selected)
    lines = [
        redact(chunk.decode("utf-8", errors="replace").rstrip("\r\n"))
        for chunk in selected
    ]
    more = len(chunks) > len(selected)
    eof = terminal and not more and (not chunks or complete)
    return {
        "run_id": run_id,
        "invocation_id": invocation_number,
        "stream": stream,
        "lines": lines,
        "next_offset": offset + consumed,
        "eof": eof,
    }


_PROJECT_FIELDS = {"name", "target", "initial_prompt", "project_directory", "pipeline_profile"}
_PROJECT_LIMITS = {"name": 160, "target": 2000, "initial_prompt": 10000}


async def _json_object(request: Request) -> dict[str, Any]:
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(status_code=415, detail="Content-Type must be application/json")
    try:
        payload = await request.json()
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="expected a JSON object")
    return payload


def _settings(payload: dict[str, Any], profiles: dict[str, Any]) -> dict[str, Any]:
    unknown = payload.keys() - _PROJECT_FIELDS
    if unknown:
        raise HTTPException(status_code=400, detail=f"unsupported fields: {', '.join(sorted(unknown))}")
    result: dict[str, Any] = {}
    for field in ("name", "target", "initial_prompt"):
        if field in payload:
            value = payload[field]
            if not isinstance(value, str) or len(value) > _PROJECT_LIMITS[field]:
                raise HTTPException(
                    status_code=400,
                    detail=f"{field} must be text of at most {_PROJECT_LIMITS[field]} characters",
                )
            value = value.strip()
            if field == "name" and not value:
                raise HTTPException(status_code=400, detail="name must be a nonempty string")
            result[field] = value
    if "project_directory" in payload:
        value = payload["project_directory"]
        if not isinstance(value, str) or not value.strip():
            raise HTTPException(status_code=400, detail="project_directory must be an absolute existing folder")
        try:
            result["project_directory"] = str(validate_project_directory(value.strip()))
        except ProjectDirectoryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if "pipeline_profile" in payload:
        value = payload["pipeline_profile"]
        if value is not None and (not isinstance(value, str) or value not in profiles):
            raise HTTPException(status_code=400, detail="pipeline_profile is not configured")
        result["pipeline_profile"] = value
    return result


_LAYOUT_BOUND = 10000.0


def _agent_position(payload: dict[str, Any]) -> tuple[str, float, float]:
    if payload.keys() != {"role", "x", "y"}:
        raise HTTPException(status_code=400, detail="expected exactly role, x and y")
    role = payload["role"]
    if not isinstance(role, str) or (
        role != AgentRole.ROOT.value and not re.fullmatch(r"node_[0-9a-f]{32}", role)
    ):
        raise HTTPException(status_code=400, detail="role must be root or a profile node id")
    coordinates = []
    for axis in ("x", "y"):
        value = payload[axis]
        # bool is an int subclass; a boolean coordinate is a client bug, not a position.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise HTTPException(status_code=400, detail=f"{axis} must be a finite number")
        value = float(value)
        if not math.isfinite(value) or abs(value) > _LAYOUT_BOUND:
            raise HTTPException(
                status_code=400, detail=f"{axis} must be a finite number within ±{int(_LAYOUT_BOUND)}"
            )
        coordinates.append(value)
    return role, coordinates[0], coordinates[1]


_CONNECTION_FIELDS = {"name", "os", "ip", "cred"}
_CONNECTION_LIMITS = {"name": 160, "os": 100, "cred": 4096}


def _connection_settings(payload: dict[str, Any], *, creating: bool) -> dict[str, Any]:
    unknown = payload.keys() - _CONNECTION_FIELDS
    if unknown:
        raise HTTPException(status_code=400, detail=f"unsupported fields: {', '.join(sorted(unknown))}")
    if not creating and not payload:
        raise HTTPException(status_code=400, detail="no fields to update")
    result: dict[str, Any] = {}
    for field in ("name", "os"):
        if field in payload:
            value = payload[field]
            if not isinstance(value, str) or not value.strip() or len(value) > _CONNECTION_LIMITS[field]:
                raise HTTPException(
                    status_code=400,
                    detail=f"{field} must be nonempty text of at most {_CONNECTION_LIMITS[field]} characters",
                )
            result[field] = value.strip()
        elif creating:
            raise HTTPException(status_code=400, detail=f"{field} is required")
    if "ip" in payload:
        value = payload["ip"]
        if value is None or (isinstance(value, str) and not value.strip()):
            result["ip"] = None
        elif isinstance(value, str):
            try:
                result["ip"] = str(ipaddress.ip_address(value.strip()))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="ip must be a valid IPv4 or IPv6 address") from exc
        else:
            raise HTTPException(status_code=400, detail="ip must be a valid IPv4 or IPv6 address")
    if "cred" in payload:
        value = payload["cred"]
        if value is None:
            result["cred"] = None
        elif not isinstance(value, str):
            raise HTTPException(status_code=400, detail="cred must be text or null")
        elif not value:
            # An empty string would silently clear a stored credential; require an explicit null.
            if creating:
                result["cred"] = None
            else:
                raise HTTPException(status_code=400, detail="cred must be nonempty text or null")
        elif len(value) > _CONNECTION_LIMITS["cred"]:
            raise HTTPException(status_code=400, detail="cred must be at most 4096 characters")
        else:
            result["cred"] = value
    return result


def _safe_connection(record: dict[str, Any]) -> dict[str, Any]:
    # Plaintext credentials stay out of list and mutation responses; only the
    # explicit reveal route returns them.
    return {
        "connection_id": record["connection_id"],
        "name": record["name"],
        "os": record["os"],
        "ip": record["ip"],
        "has_cred": record["cred"] is not None,
        "in_use": record["in_use"],
        "used_by": record["used_by"],
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
    }


def create_app(config: AppConfig | None = None) -> FastAPI:
    config = config or load_config(Path.cwd())
    profile_store = ProfileStore(config.profiles_db)
    state = StateStore(config.state_db, legacy_db=config.legacy_state_db)
    artifacts = ArtifactStore(config.runs_root, state)
    sessions = ActiveSessionRegistry()
    active_tasks: set[asyncio.Task[None]] = set()
    run_tasks: dict[str, asyncio.Task[None]] = {}
    cancelled_runs: set[str] = set()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        state.fail_interrupted_api_runs()
        for project_id, old_ids in state.prune_project_history():
            project = state.get_project(project_id)
            if project and project.get("project_directory"):
                for old_id in old_ids:
                    try:
                        remove_owned_run(project["project_directory"], old_id)
                    except ProjectDirectoryError:
                        _LOG.warning("unable to clean old run files for project %s", project_id)
        for project in state.list_projects():
            directory = project.get("project_directory")
            if directory and read_marker(directory) == {"project_id": project["project_id"]}:
                live_run_ids = state.list_project_run_ids(project["project_id"])
                try:
                    cleanup_orphan_runs(directory, live_run_ids)
                except ProjectDirectoryError:
                    _LOG.warning("unable to clean orphan files for project %s", project["project_id"])
        try:
            yield
        finally:
            for task in tuple(active_tasks):
                task.cancel()
            if active_tasks:
                await asyncio.gather(*active_tasks, return_exceptions=True)

    password = os.getenv("ORCHES_ADMIN_PASSWORD", "")
    admin = AdminSessions(password)  # raises WeakPasswordError before the server binds
    # No public schema or docs: the API surface is private behind bearer auth.
    app = FastAPI(title="Orches Monitor", lifespan=lifespan,
                  openapi_url=None, docs_url=None, redoc_url=None)

    auth_exempt = {("GET", "/api/health"), ("HEAD", "/api/health"), ("POST", "/api/auth/login")}

    @app.middleware("http")
    async def _authenticate(request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.method == "OPTIONS" or (request.method, request.url.path) in auth_exempt:
            return await call_next(request)
        if not admin.authenticate(request.headers.get("authorization")):
            return JSONResponse({"detail": "authentication required"}, status_code=401)
        return await call_next(request)

    # CORS is registered after the auth middleware so it wraps it (outermost):
    # 401/429 responses to the configured frontend origin keep CORS headers.
    origin = os.getenv("ORCHES_WEB_ORIGIN", "http://localhost:3000")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[origin],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Accept"],
    )

    no_store = {"Cache-Control": "no-store"}

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "orches", "authentication": "required"}

    @app.post("/api/auth/login")
    async def auth_login(request: Request) -> JSONResponse:
        peer = request.client.host if request.client else "unknown"
        if admin.login_blocked(peer):
            return JSONResponse({"detail": "too many login attempts"}, status_code=429, headers=no_store)
        body = await request.body()
        if len(body) > 4096:
            return JSONResponse({"detail": "request body too large"}, status_code=413, headers=no_store)
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            payload = None
        if (
            not isinstance(payload, dict)
            or set(payload) != {"password"}
            or not isinstance(payload.get("password"), str)
        ):
            return JSONResponse({"detail": "body must contain only a password string"},
                                status_code=400, headers=no_store)
        token = admin.login(payload["password"], peer)
        if token is None:
            return JSONResponse({"detail": "invalid credentials"}, status_code=401, headers=no_store)
        return JSONResponse(
            {"access_token": token, "token_type": "bearer", "expires_in": TOKEN_TTL_SECONDS},
            headers=no_store,
        )

    @app.get("/api/auth/session")
    def auth_session() -> JSONResponse:
        # The auth middleware already validated the bearer token.
        return JSONResponse({"authenticated": True}, headers=no_store)

    @app.post("/api/auth/logout")
    def auth_logout(request: Request) -> JSONResponse:
        admin.revoke(request.headers.get("authorization"))
        return JSONResponse({"logged_out": True}, headers=no_store)

    @app.get("/api/projects")
    def projects() -> dict[str, Any]:
        return {"projects": [_project_summary(state, item, profile_store) for item in state.list_projects()]}

    @app.get("/api/costs")
    def costs() -> dict[str, Any]:
        return {"rows": state.list_model_costs()}

    @app.get("/api/pipeline-profiles")
    def pipeline_profiles() -> dict[str, Any]:
        return {"profiles": profile_store.list()}

    @app.post("/api/pipeline-profiles", status_code=201)
    async def create_pipeline_profile(request: Request) -> dict[str, Any]:
        payload = await _json_object(request)
        if set(payload) != {"name"} or not isinstance(payload["name"], str) or not payload["name"].strip():
            raise HTTPException(status_code=400, detail="body must contain only a nonempty name")
        try:
            return profile_store.create(payload["name"])
        except ProfileConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/pipeline-profiles/{profile_id}")
    def get_pipeline_profile(profile_id: str) -> dict[str, Any]:
        profile = profile_store.get(profile_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="pipeline profile not found")
        return profile

    @app.put("/api/pipeline-profiles/{profile_id}")
    async def update_pipeline_profile(profile_id: str, request: Request) -> dict[str, Any]:
        payload = await _json_object(request)
        if set(payload) - {"root_prompt", "root_model", "root_thinking"} != {"name", "steps", "revision"}:
            raise HTTPException(
                status_code=400,
                detail="body must contain name, steps and revision, and may contain root_prompt, root_model and root_thinking",
            )
        if profile_store.get(profile_id) is None:
            raise HTTPException(status_code=404, detail="pipeline profile not found")
        if not isinstance(payload["revision"], int) or isinstance(payload["revision"], bool):
            raise HTTPException(status_code=400, detail="revision must be an integer")
        definition = {
            "id": profile_id,
            "name": payload["name"],
            "steps": payload["steps"],
            "root_prompt": payload.get("root_prompt", ""),
            "root_model": payload.get("root_model", ""),
            "root_thinking": payload.get("root_thinking", ""),
        }
        try:
            return profile_store.update(profile_id, definition, payload["revision"])
        except ProfileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="pipeline profile not found") from exc
        except ProfileConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/pipeline-profiles/{profile_id}")
    async def delete_pipeline_profile(profile_id: str) -> dict[str, bool]:
        if profile_store.get(profile_id) is None:
            raise HTTPException(status_code=404, detail="pipeline profile not found")
        if any(item.get("pipeline_profile") == profile_id for item in state.list_projects()):
            raise HTTPException(status_code=409, detail="pipeline profile is selected by a project")
        if not profile_store.delete(profile_id):
            raise HTTPException(status_code=404, detail="pipeline profile not found")
        return {"deleted": True}

    @app.get("/api/omp-models")
    async def omp_models() -> dict[str, Any]:
        def load_models() -> list[dict[str, str]]:
            child_env = os.environ.copy()
            child_env.pop("ORCHES_ADMIN_PASSWORD", None)  # never inherit the admin secret
            try:
                process = subprocess.Popen(
                    [config.omp_bin, "models", "--json"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL, env=child_env,
                )
                try:
                    output = process.stdout.read(8 * 1024 * 1024 + 1) if process.stdout else b""
                    process.wait(timeout=10)
                except (TimeoutError, subprocess.TimeoutExpired):
                    process.kill()
                    process.wait()
                    raise RuntimeError("OMP model listing timed out")
                if len(output) > 8 * 1024 * 1024 or process.returncode:
                    raise RuntimeError("OMP model listing failed")
                data = json.loads(output.decode("utf-8"))
                models = data.get("models") if isinstance(data, dict) else None
                if not isinstance(models, list):
                    raise RuntimeError("OMP model listing returned invalid JSON")
                result = []
                for item in models:
                    if not isinstance(item, dict) or item.get("kind") != "chat":
                        continue
                    if not all(isinstance(item.get(key), str) and item[key] for key in ("selector", "name", "provider")):
                        continue
                    result.append({key: item[key] for key in ("selector", "name", "provider")})
                return sorted(result, key=lambda item: item["selector"])
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, RuntimeError) as exc:
                raise RuntimeError(str(exc)) from exc

        try:
            return {"models": await asyncio.to_thread(load_models)}
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/projects", status_code=201)
    async def create_project(request: Request) -> dict[str, Any]:
        payload = await _json_object(request)
        settings = _settings(payload, {item["id"]: item for item in profile_store.list()})
        if "project_directory" not in settings:
            raise HTTPException(status_code=400, detail="project_directory is required")
        if not settings.get("name"):
            raise HTTPException(status_code=400, detail="name is required")
        requested = Path(settings["project_directory"])
        folded = os.path.normcase(str(requested))
        for existing in state.list_projects():
            if os.path.normcase(str(Path(existing["project_directory"]).resolve())) == folded:
                raise HTTPException(status_code=409, detail="project directory is already bound")
        project_id: str | None = None
        try:
            project_id = state.create_project(**settings)
            ensure_project_marker(requested, project_id)
        except ProjectConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ProjectDirectoryError as exc:
            if project_id:
                state.delete_project(project_id)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        project = state.get_project(project_id)
        return _project_summary(state, project, profile_store)

    @app.patch("/api/projects/{project_id}")
    async def update_project(project_id: str, request: Request) -> dict[str, Any]:
        settings = _settings(await _json_object(request), {item["id"]: item for item in profile_store.list()})
        existing = state.get_project(project_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="project not found")
        try:
            project = state.update_project(project_id, **settings)
        except ProjectConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        return _project_summary(state, project, profile_store)

    @app.delete("/api/projects/{project_id}", status_code=200)
    def delete_project(project_id: str) -> dict[str, bool]:
        try:
            deleted = state.delete_project(project_id)
        except ProjectConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not deleted:
            raise HTTPException(status_code=404, detail="project not found")
        return {"deleted": True}

    @app.get("/api/projects/{project_id}")
    def project_detail(project_id: str) -> dict[str, Any]:
        project = state.get_project(project_id)
        if not project:
            raise HTTPException(status_code=404, detail="project not found")
        result = _project_summary(state, project, profile_store)
        result["agent_layout"] = state.get_agent_layout(project_id)
        selected_record = (
            profile_store.get(project["pipeline_profile"]) if project.get("pipeline_profile") else None
        )
        result["pipeline_profile_definition"] = (
            profile_from_record(selected_record).to_dict() if selected_record else None
        )
        return result

    @app.patch("/api/projects/{project_id}/agent-layout")
    async def update_agent_layout(project_id: str, request: Request) -> dict[str, Any]:
        role, x, y = _agent_position(await _json_object(request))
        project = state.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        profile_record = (
            profile_store.get(project["pipeline_profile"]) if project.get("pipeline_profile") else None
        )
        allowed = {AgentRole.ROOT.value}
        if profile_record:
            allowed.update(step["id"] for step in profile_record["steps"])
        if role not in allowed:
            raise HTTPException(status_code=400, detail="role is not part of the selected pipeline profile")
        layout = state.set_agent_position(project_id, role, x, y)
        if layout is None:
            raise HTTPException(status_code=404, detail="project not found")
        return {"agent_layout": layout}

    def _launch_run(
        project: dict[str, Any], profile: Any, run_id: str, orchestrator: Any,
        completed_outputs: dict[str, dict[str, Any]] | None = None, resumed_from: str | None = None,
    ) -> None:
        project_id = project["project_id"]

        async def execute() -> None:
            try:
                await orchestrator.run(
                    project_id=project_id, run_id=run_id, profile=profile,
                    target=project["target"], initial_prompt=project["initial_prompt"],
                    project_directory=project["project_directory"],
                    completed_outputs=completed_outputs, resumed_from=resumed_from,
                )
            except asyncio.CancelledError:
                current = state.get_run(run_id)
                if current and current["status"] == "running":
                    reason = "run cancelled" if run_id in cancelled_runs else "run stopped with API service"
                    state.update_run(run_id, "failed", reason)
                raise
            except Exception:
                _LOG.exception("project run %s failed", run_id)
                current = state.get_run(run_id)
                if current and current["status"] == "running":
                    state.update_run(run_id, "failed", "run failed unexpectedly")

        task = asyncio.create_task(execute(), name=f"project-run-{run_id}")
        active_tasks.add(task)
        run_tasks[run_id] = task

        def _forget(done: asyncio.Task[None]) -> None:
            active_tasks.discard(done)
            run_tasks.pop(run_id, None)
            cancelled_runs.discard(run_id)

        task.add_done_callback(_forget)

    def _project_for_run(project_id: str) -> tuple[dict[str, Any], Any]:
        project = state.get_project(project_id)
        if not project:
            raise HTTPException(status_code=404, detail="project not found")
        try:
            assert_project_marker(project["project_directory"], project_id)
        except ProjectDirectoryError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not project["target"]:
            raise HTTPException(status_code=400, detail="set a project objective before starting")
        try:
            from .cli import build_orchestrator
            orchestrator = build_orchestrator(
                config, require_omp=True, sessions=sessions,
                project_directory=project["project_directory"],
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return project, orchestrator

    def _reserve_run(project: dict[str, Any], profile_json: str) -> str:
        run_id: str | None = None
        try:
            run_id, old_ids = state.reserve_project_run(
                project["project_id"], expected_updated_at=project["updated_at"], source="api",
                profile_json=profile_json,
            )
            for old_id in old_ids:
                remove_owned_run(project["project_directory"], old_id)
        except (ProjectConflictError, KeyError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (ProjectDirectoryError, OSError) as exc:
            if run_id:
                state.update_run(run_id, "failed", f"old run cleanup failed: {exc}")
            raise HTTPException(status_code=503, detail=f"old run cleanup failed: {exc}") from exc
        return run_id

    def _selected_profile(project: dict[str, Any]) -> Any:
        """The project's currently saved profile; what Start and Resume execute."""
        profile_id = project.get("pipeline_profile")
        if not profile_id:
            raise HTTPException(status_code=409, detail="select a pipeline profile before starting")
        selected_record = profile_store.get(profile_id)
        if selected_record is None:
            raise HTTPException(status_code=409, detail=f"pipeline profile is not configured: {profile_id}")
        selected_profile = profile_from_record(selected_record)
        if not selected_profile.steps:
            raise HTTPException(status_code=409, detail="pipeline profile is an empty draft")
        return selected_profile

    @app.post("/api/projects/{project_id}/runs", status_code=202)
    async def start_project_run(project_id: str, request: Request) -> dict[str, str]:
        payload = await _json_object(request)
        if payload:
            raise HTTPException(status_code=400, detail="save project settings before starting a run")
        project, orchestrator = _project_for_run(project_id)
        selected_profile = _selected_profile(project)
        run_id = _reserve_run(project, json.dumps(selected_profile.to_dict(), sort_keys=True))
        _launch_run(project, selected_profile, run_id, orchestrator)
        return {"run_id": run_id, "project_id": project_id, "status": "running"}

    @app.post("/api/runs/{run_id}/resume", status_code=202)
    async def resume_run(run_id: str, request: Request) -> dict[str, Any]:
        """Start a new run of the project's saved profile, reusing completed worker outputs.

        Completed workers are carried over only when their definition (model,
        prompt, effort, dependencies) is unchanged since the finished run; edited
        nodes and their dependents execute again with the saved profile. Body:
        {"rerun": [node_id, ...]} (optional) forces those workers and every
        dependent to execute again as well.
        """
        payload = await _json_object(request)
        if set(payload) - {"rerun"}:
            raise HTTPException(status_code=400, detail="body may contain only rerun")
        rerun_raw = payload.get("rerun", [])
        if not isinstance(rerun_raw, list) or not all(isinstance(item, str) for item in rerun_raw):
            raise HTTPException(status_code=400, detail="rerun must be a list of node ids")
        source = state.get_run(run_id)
        if not source:
            raise HTTPException(status_code=404, detail="run not found")
        if source["status"] == "running":
            raise HTTPException(status_code=409, detail="run is still running; stop it first")
        snapshot = state.get_run_profile(run_id)
        if not snapshot:
            raise HTTPException(status_code=409, detail="run has no pipeline profile snapshot")
        project, orchestrator = _project_for_run(source["project_id"])
        profile = _selected_profile(project)
        unchanged = profile.unchanged_nodes(profile_from_dict(snapshot))
        # Capture everything from the old run before reservation deletes it.
        completed_rows = {
            item["role"]: item for item in state.list_invocations(run_id)
            if item["role"] in unchanged and item["status"] == "completed" and item.get("output_json")
        }
        try:
            reusable = profile.reusable_nodes(set(completed_rows), set(rerun_raw))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        old_root = app_root(project["project_directory"]) / "runs" / run_id / "raw"
        carried: list[dict[str, Any]] = []
        seeds: dict[str, dict[str, Any]] = {}
        for node_id in sorted(reusable):
            row = completed_rows[node_id]
            logs: dict[str, str] = {}
            for suffix in ("stdout.jsonl", "stderr.txt"):
                path = old_root / f"{node_id}.{suffix}"
                if path.is_file():
                    logs[suffix] = path.read_text(encoding="utf-8", errors="replace")
            carried.append({**row, "_logs": logs})
        new_run_id = _reserve_run(project, json.dumps(profile.to_dict(), sort_keys=True))
        for item in carried:
            artifact_ids: dict[str, str | None] = {"output_artifact_id": None, "stderr_artifact_id": None}
            for suffix, key, media in (
                ("stdout.jsonl", "output_artifact_id", "text/plain"),
                ("stderr.txt", "stderr_artifact_id", "text/plain"),
            ):
                if suffix in item["_logs"]:
                    artifact = orchestrator.artifacts.write_text(
                        new_run_id, f"raw/{item['role']}.{suffix}", item["_logs"][suffix], media_type=media,
                    )
                    artifact_ids[key] = artifact.artifact_id
            item.update(artifact_ids)
            seeds[item["role"]] = {
                "output": json.loads(item["output_json"]),
                "artifact_ids": [value for value in artifact_ids.values() if value],
            }
        state.carry_over_invocations(
            new_run_id, [{key: value for key, value in item.items() if key != "_logs"} for item in carried],
        )
        _launch_run(project, profile, new_run_id, orchestrator, completed_outputs=seeds, resumed_from=run_id)
        return {
            "run_id": new_run_id, "project_id": project["project_id"], "status": "running",
            "resumed_from": run_id, "reused": sorted(seeds),
        }

    @app.post("/api/runs/{run_id}/cancel", status_code=202)
    async def cancel_run(run_id: str) -> dict[str, str]:
        run = state.get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="run not found")
        if run["status"] != "running":
            raise HTTPException(status_code=409, detail="run is not running")
        task = run_tasks.get(run_id)
        if task is None or task.done():
            # e.g. a CLI-started run or a run orphaned by an earlier API process.
            raise HTTPException(status_code=409, detail="run is not managed by this API process")
        cancelled_runs.add(run_id)
        task.cancel()
        return {"run_id": run_id, "status": "cancelling"}

    def _run_agents(run_id: str) -> list[dict[str, Any]]:
        usage = state.run_agent_costs(run_id)
        return [_agent(item, sessions, usage.get(item["id"])) for item in state.list_invocations(run_id)]

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: str) -> dict[str, Any]:
        run = state.get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="run not found")
        project = state.get_project(run["project_id"])
        return {
            "run": run,
            "project": _project_summary(state, project, profile_store) if project else None,
            "pipeline_profile": state.get_run_profile(run_id),
            "agents": _run_agents(run_id),
            "artifacts": state.list_artifacts(run_id),
        }

    @app.get("/api/runs/{run_id}/agents")
    def run_agents(run_id: str) -> dict[str, Any]:
        if not state.get_run(run_id):
            raise HTTPException(status_code=404, detail="run not found")
        return {"agents": _run_agents(run_id)}

    @app.post("/api/runs/{run_id}/agents/{invocation_id}/messages", status_code=202)
    async def send_agent_message(run_id: str, invocation_id: str, request: Request) -> dict[str, Any]:
        payload = await _json_object(request)
        if set(payload) != {"text"} or not isinstance(payload.get("text"), str):
            raise HTTPException(status_code=400, detail="body must contain only a string text field")
        text = payload["text"].strip()
        if not text or len(text) > 4000:
            raise HTTPException(status_code=400, detail="text must be 1 to 4000 characters")
        try:
            number = int(invocation_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="invocation not found") from exc
        run = state.get_run(run_id)
        invocation = next((item for item in state.list_invocations(run_id) if item["id"] == number), None)
        if not run or invocation is None:
            raise HTTPException(status_code=404, detail="run or invocation not found")
        if run.get("source") != "api" or run.get("status") != "running" or invocation["status"] != "running" or not sessions.has(run_id, number):
            raise HTTPException(status_code=409, detail="agent is not accepting messages")
        redacted = artifacts.redact_text(text)
        try:
            await asyncio.wait_for(sessions.send(run_id, number, redacted), timeout=10)
        except (LookupError, RuntimeError, asyncio.TimeoutError) as exc:
            raise HTTPException(status_code=503, detail=f"unable to deliver message: {exc}") from exc
        return {"status": "accepted", "run_id": run_id, "invocation_id": number}

    @app.get("/api/runs/{run_id}/agents/{invocation_id}/logs")
    def invocation_logs(
        run_id: str,
        invocation_id: str,
        stream: str = Query("stdout"),
        offset: str = Query("0"),
        limit: int = Query(200),
    ) -> dict[str, Any]:
        try:
            offset_value = int(offset)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="offset must be a non-negative integer") from exc
        if limit < 1:
            limit = 1
        elif limit > 500:
            limit = 500
        run = state.get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="run not found")
        project = state.get_project(run["project_id"])
        if project and project.get("project_directory"):
            try:
                assert_project_marker(project["project_directory"], project["project_id"])
                log_root = app_root(project["project_directory"]) / "runs"
            except ProjectDirectoryError as exc:
                raise HTTPException(status_code=404, detail="log not found") from exc
        else:
            log_root = config.runs_root
        return _log_page(
            state, log_root, run_id, invocation_id, stream, offset_value, limit, artifacts.redact_text,
        )

    @app.get("/api/connections")
    def connections() -> dict[str, Any]:
        return {"connections": [_safe_connection(item) for item in state.list_connections()]}

    @app.post("/api/connections", status_code=201)
    async def create_connection(request: Request) -> dict[str, Any]:
        settings = _connection_settings(await _json_object(request), creating=True)
        record = state.create_connection(
            name=settings["name"], os=settings["os"],
            ip=settings.get("ip"), cred=settings.get("cred"),
        )
        return _safe_connection(record)

    @app.patch("/api/connections/{connection_id}")
    async def update_connection(connection_id: str, request: Request) -> dict[str, Any]:
        settings = _connection_settings(await _json_object(request), creating=False)
        try:
            record = state.update_connection(connection_id, **settings)
        except ConnectionUseError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if record is None:
            raise HTTPException(status_code=404, detail="connection not found")
        return _safe_connection(record)

    @app.delete("/api/connections/{connection_id}")
    def delete_connection(connection_id: str) -> dict[str, bool]:
        try:
            deleted = state.delete_connection(connection_id)
        except ConnectionUseError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not deleted:
            raise HTTPException(status_code=404, detail="connection not found")
        return {"deleted": True}

    @app.get("/api/connections/{connection_id}/cred")
    def reveal_connection_credential(connection_id: str) -> JSONResponse:
        record = state.get_connection(connection_id)
        if record is None:
            raise HTTPException(status_code=404, detail="connection not found")
        return JSONResponse({"cred": record["cred"]}, headers={"Cache-Control": "no-store"})

    # --------------------------------------------------------- MCP catalog
    mcp_service = ProjectMcpService(state)

    @app.get("/api/mcp-servers")
    def mcp_catalog_list() -> dict[str, Any]:
        entries = state.list_mcp_catalog()
        result = []
        for entry in entries:
            config = json.loads(entry["config_json"])
            summary = safe_summary(config)
            result.append({
                "name": entry["name"], "revision": entry["revision"],
                "transport": summary["transport"], "endpoint": summary["endpoint"],
                "created_at": entry["created_at"], "updated_at": entry["updated_at"],
            })
        return {"servers": result}

    @app.post("/api/mcp-servers", status_code=201)
    async def mcp_catalog_create(request: Request) -> dict[str, Any]:
        payload = await _json_object(request)
        name = payload.get("name")
        config = payload.get("config")
        if not isinstance(name, str) or not isinstance(config, dict):
            raise HTTPException(status_code=400, detail="name (string) and config (object) are required")
        try:
            validated = validate_server(validate_name(name), config)
        except McpConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            entry = state.create_mcp_catalog(name, validated)
        except ProjectConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"name": entry["name"], "config": json.loads(entry["config_json"]),
                "revision": entry["revision"], "created_at": entry["created_at"],
                "updated_at": entry["updated_at"]}

    @app.get("/api/mcp-servers/{name}")
    def mcp_catalog_detail(name: str) -> JSONResponse:
        entry = state.get_mcp_catalog(name)
        if entry is None:
            raise HTTPException(status_code=404, detail="catalog entry not found")
        return JSONResponse(
            {"name": entry["name"], "config": json.loads(entry["config_json"]),
             "revision": entry["revision"], "created_at": entry["created_at"],
             "updated_at": entry["updated_at"]},
            headers={"Cache-Control": "no-store"},
        )

    @app.put("/api/mcp-servers/{name}")
    async def mcp_catalog_update(name: str, request: Request) -> dict[str, Any]:
        payload = await _json_object(request)
        config = payload.get("config")
        revision = payload.get("revision")
        if not isinstance(config, dict) or not isinstance(revision, int):
            raise HTTPException(status_code=400, detail="config (object) and revision (integer) are required")
        try:
            validated = validate_server(name, config)
        except McpConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            entry = state.update_mcp_catalog(name, validated, revision)
        except KeyError:
            raise HTTPException(status_code=404, detail="catalog entry not found")
        except ProjectConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"name": entry["name"], "config": json.loads(entry["config_json"]),
                "revision": entry["revision"], "created_at": entry["created_at"],
                "updated_at": entry["updated_at"]}

    @app.delete("/api/mcp-servers/{name}")
    def mcp_catalog_delete(name: str) -> dict[str, bool]:
        if not state.delete_mcp_catalog(name):
            raise HTTPException(status_code=404, detail="catalog entry not found")
        return {"deleted": True}

    # --------------------------------------------------- project MCP servers

    def _project_for_mcp(project_id: str) -> dict[str, Any]:
        project = state.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
        return project

    def _mcp_error(exc: Exception) -> HTTPException:
        if isinstance(exc, McpRevisionError):
            return HTTPException(status_code=409, detail=str(exc))
        if isinstance(exc, McpConfigError):
            return HTTPException(status_code=400, detail=str(exc))
        if isinstance(exc, McpFileError):
            return HTTPException(status_code=409, detail=str(exc))
        if isinstance(exc, ProjectConflictError):
            return HTTPException(status_code=409, detail=str(exc))
        if isinstance(exc, ProjectDirectoryError):
            return HTTPException(status_code=400, detail=str(exc))
        return HTTPException(status_code=500, detail=str(exc))

    @app.get("/api/projects/{project_id}/mcp-servers")
    def project_mcp_list(project_id: str) -> dict[str, Any]:
        project = _project_for_mcp(project_id)
        try:
            return mcp_service.list(project)
        except (McpFileError, ProjectDirectoryError) as exc:
            raise _mcp_error(exc) from exc

    @app.post("/api/projects/{project_id}/mcp-servers", status_code=201)
    async def project_mcp_add(project_id: str, request: Request) -> dict[str, Any]:
        project = _project_for_mcp(project_id)
        payload = await _json_object(request)
        source = payload.get("source")
        if_revision = payload.get("if_revision")
        if not isinstance(if_revision, str):
            raise HTTPException(status_code=400, detail="if_revision (string) is required")
        try:
            if source == "catalog":
                name = payload.get("name")
                if not isinstance(name, str):
                    raise HTTPException(status_code=400, detail="name is required for catalog source")
                return mcp_service.add_from_catalog(project, name, if_revision)
            elif source == "custom":
                name = payload.get("name")
                config = payload.get("config")
                if not isinstance(name, str) or not isinstance(config, dict):
                    raise HTTPException(status_code=400, detail="name and config required for custom source")
                return mcp_service.add_custom(project, name, config, if_revision)
            else:
                raise HTTPException(status_code=400, detail="source must be 'catalog' or 'custom'")
        except (McpConfigError, McpFileError, McpRevisionError, ProjectConflictError, ProjectDirectoryError) as exc:
            raise _mcp_error(exc) from exc

    @app.put("/api/projects/{project_id}/mcp-servers/{name}")
    async def project_mcp_edit(project_id: str, name: str, request: Request) -> dict[str, Any]:
        project = _project_for_mcp(project_id)
        payload = await _json_object(request)
        config = payload.get("config")
        if_revision = payload.get("if_revision")
        if not isinstance(config, dict) or not isinstance(if_revision, str):
            raise HTTPException(status_code=400, detail="config (object) and if_revision (string) required")
        try:
            return mcp_service.edit(project, name, config, if_revision)
        except (McpConfigError, McpFileError, McpRevisionError, ProjectConflictError, ProjectDirectoryError) as exc:
            raise _mcp_error(exc) from exc

    @app.delete("/api/projects/{project_id}/mcp-servers/{name}")
    async def project_mcp_remove(project_id: str, name: str, request: Request) -> dict[str, Any]:
        project = _project_for_mcp(project_id)
        payload = await _json_object(request)
        if_revision = payload.get("if_revision")
        if not isinstance(if_revision, str):
            raise HTTPException(status_code=400, detail="if_revision (string) required")
        try:
            return mcp_service.remove(project, name, if_revision)
        except (McpConfigError, McpFileError, McpRevisionError, ProjectConflictError, ProjectDirectoryError) as exc:
            raise _mcp_error(exc) from exc

    @app.post("/api/projects/{project_id}/mcp-servers/{name}/refresh")
    async def project_mcp_refresh(project_id: str, name: str, request: Request) -> dict[str, Any]:
        project = _project_for_mcp(project_id)
        payload = await _json_object(request)
        if_revision = payload.get("if_revision")
        if not isinstance(if_revision, str):
            raise HTTPException(status_code=400, detail="if_revision (string) required")
        try:
            return mcp_service.refresh(project, name, if_revision)
        except (McpConfigError, McpFileError, McpRevisionError, ProjectConflictError, ProjectDirectoryError) as exc:
            raise _mcp_error(exc) from exc

    from .omp import OmpClient as _OmpClient
    _omp_client: _OmpClient | None = None
    try:
        _omp_client = _OmpClient(config.omp_bin)
    except FileNotFoundError:
        pass

    @app.post("/api/projects/{project_id}/mcp-servers/{name}/check")
    async def project_mcp_check(project_id: str, name: str) -> dict[str, Any]:
        project = _project_for_mcp(project_id)
        try:
            data, _ = read_project_mcp(project)
        except (McpFileError, ProjectDirectoryError) as exc:
            raise _mcp_error(exc) from exc
        servers = data.get("mcpServers", {})
        if name not in servers:
            raise HTTPException(status_code=404, detail=f"server {name!r} not found in project")
        server_config = servers[name]
        if isinstance(server_config, dict) and server_config.get("enabled") is False:
            return {"name": name, "status": "disabled", "checked_at": None, "detail": "server is disabled"}
        if _omp_client is None:
            raise HTTPException(status_code=503, detail="omp binary is not available")
        project_dir = Path(project["project_directory"])
        assert_project_marker(project_dir, project["project_id"])
        return await _omp_client.probe_mcp(project_dir, name)

    return app

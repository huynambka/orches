"""Validation and safe paths for project-owned files."""
from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

MARKER_DIR = ".orches"
MARKER_NAME = "project.json"


class ProjectDirectoryError(ValueError):
    """A project directory is missing, unsafe, or already owned."""


def validate_project_directory(path: str | Path) -> Path:
    if not isinstance(path, (str, Path)) or not str(path).strip():
        raise ProjectDirectoryError("project_directory is required")
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise ProjectDirectoryError("project_directory must be an absolute path")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ProjectDirectoryError("project_directory does not exist") from exc
    if not resolved.is_dir():
        raise ProjectDirectoryError("project_directory must be a directory")
    if not os.access(resolved, os.R_OK | os.W_OK):
        raise ProjectDirectoryError("project_directory must be readable and writable")
    return resolved


def marker_path(project_directory: str | Path) -> Path:
    return Path(project_directory) / MARKER_DIR / MARKER_NAME


def read_marker(project_directory: str | Path) -> dict[str, Any] | None:
    marker = marker_path(project_directory)
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def ensure_project_marker(project_directory: str | Path, project_id: str) -> Path:
    root = validate_project_directory(project_directory)
    app_dir = root / MARKER_DIR
    marker = app_dir / MARKER_NAME
    if app_dir.is_symlink() or marker.is_symlink():
        raise ProjectDirectoryError(f"project {MARKER_DIR} path must not be a symlink")
    if app_dir.exists() and not marker.exists():
        raise ProjectDirectoryError(f"project directory has an unrecognized {MARKER_DIR} marker")
    app_dir.mkdir(parents=True, exist_ok=True)
    existing = read_marker(root)
    if existing is not None and existing.get("project_id") != project_id:
        raise ProjectDirectoryError("project directory is already bound to another project")
    if existing is None and marker.exists():
        raise ProjectDirectoryError(f"project directory has an unrecognized {MARKER_DIR} marker")
    if existing is None:
        temporary = marker.with_name(f".{marker.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps({"project_id": project_id}, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(marker)
    return marker


def assert_project_marker(project_directory: str | Path, project_id: str) -> Path:
    root = validate_project_directory(project_directory)
    marker = read_marker(root)
    if marker is None or marker.get("project_id") != project_id:
        raise ProjectDirectoryError("project directory marker is missing or does not match project")
    return root


def app_root(project_directory: str | Path) -> Path:
    return Path(project_directory) / MARKER_DIR


def run_root(project_directory: str | Path, run_id: str) -> Path:
    if not run_id or Path(run_id).name != run_id or any(c not in "0123456789abcdef" for c in run_id.lower()):
        raise ProjectDirectoryError("invalid run id")
    return app_root(project_directory) / "runs" / run_id


def session_root(project_directory: str | Path, run_id: str) -> Path:
    if not run_id or Path(run_id).name != run_id or any(c not in "0123456789abcdef" for c in run_id.lower()):
        raise ProjectDirectoryError("invalid run id")
    return app_root(project_directory) / "sessions" / run_id


def remove_owned_run(project_directory: str | Path, run_id: str) -> None:
    root = assert_project_marker(project_directory, read_marker(project_directory).get("project_id") if read_marker(project_directory) else "")
    # assert marker above only establishes ownership; run ids and roots are constrained.
    for target in (run_root(root, run_id), session_root(root, run_id)):
        if target.exists() or target.is_symlink():
            if target.is_symlink():
                target.unlink()
            else:
                shutil.rmtree(target)
def cleanup_orphan_runs(project_directory: str | Path, live_run_ids: set[str]) -> None:
    root = assert_project_marker(
        project_directory,
        read_marker(project_directory).get("project_id") if read_marker(project_directory) else "",
    )
    for parent in (app_root(root) / "runs", app_root(root) / "sessions"):
        if not parent.is_dir() or parent.is_symlink():
            continue
        for child in parent.iterdir():
            if child.is_symlink() or not child.is_dir():
                continue
            if child.name not in live_run_ids and all(c in "0123456789abcdef" for c in child.name.lower()):
                shutil.rmtree(child)

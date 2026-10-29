"""MCP server catalog and per-project configuration management.

The central catalog lives in StateStore; the effective project configuration
lives in `<project>/.omp/mcp.json` which OMP discovers at agent runtime.  This
module owns validation, file I/O, and the service that coordinates both.
"""
from __future__ import annotations

import hashlib
import importlib.resources
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Callable

import jsonschema

from .models import utc_now
from .project_files import assert_project_marker
from .state import StateStore

# ---------------------------------------------------------------------------
# Schema and validation
# ---------------------------------------------------------------------------

_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,100}$")
_MAX_ENTRY_BYTES = 64 * 1024
_MAX_FILE_BYTES = 1024 * 1024
_MAX_PROJECT_SERVERS = 64
_MCP_FILE = ".omp/mcp.json"


class McpConfigError(ValueError):
    """Invalid MCP server name or configuration."""


class McpFileError(RuntimeError):
    """The project MCP file is malformed, symlinked, or cannot be safely replaced."""


class McpRevisionError(RuntimeError):
    """The project MCP file changed since the caller last read it."""


def _load_schema() -> dict[str, Any]:
    ref = importlib.resources.files("orches").joinpath("mcp_schema.json")
    return json.loads(ref.read_text(encoding="utf-8"))


_SCHEMA: dict[str, Any] | None = None


def _server_validator() -> jsonschema.Draft202012Validator:
    global _SCHEMA
    if _SCHEMA is None:
        _SCHEMA = _load_schema()
    server_schema = _SCHEMA["$defs"]["serverConfig"]
    # Resolve local $ref within the file
    return jsonschema.Draft202012Validator(server_schema, resolver=jsonschema.RefResolver.from_schema(_SCHEMA))


def validate_name(name: str) -> str:
    """Return a validated server name or raise McpConfigError."""
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise McpConfigError(
            "server name must be 1–100 characters of letters, digits, underscore, dot, or hyphen"
        )
    return name


def validate_server(name: str, config: Any) -> dict[str, Any]:
    """Validate and normalize a single MCP server entry.

    Raises McpConfigError for invalid input.
    """
    validate_name(name)
    if not isinstance(config, dict):
        raise McpConfigError("server config must be a JSON object")
    raw = json.dumps(config, sort_keys=True, separators=(",", ":"))
    if len(raw.encode("utf-8")) > _MAX_ENTRY_BYTES:
        raise McpConfigError(f"server config exceeds {_MAX_ENTRY_BYTES} bytes")

    # Normalize missing stdio type
    config = dict(config)
    if "command" in config and "type" not in config:
        config["type"] = "stdio"

    try:
        _server_validator().validate(config)
    except jsonschema.ValidationError as exc:
        raise McpConfigError(f"invalid server config: {exc.message}") from exc
    return config


def canonical_json(config: dict[str, Any]) -> str:
    return json.dumps(config, sort_keys=True, separators=(",", ":"))


def config_sha256(config: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(config).encode("utf-8")).hexdigest()


def safe_summary(config: dict[str, Any]) -> dict[str, str | None]:
    """Extract non-secret transport summary for list views."""
    transport = config.get("type", "stdio")
    if transport == "stdio":
        cmd = config.get("command", "")
        return {"transport": "stdio", "endpoint": os.path.basename(cmd) if cmd else None}
    url = config.get("url", "")
    # Show only hostname for remote URLs
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        host = parsed.hostname or url
    except Exception:
        host = url
    return {"transport": transport, "endpoint": host}


# ---------------------------------------------------------------------------
# Project file I/O
# ---------------------------------------------------------------------------

def _mcp_path(project_directory: str | Path) -> Path:
    return Path(project_directory) / _MCP_FILE


def _file_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _reject_symlinks(project_directory: Path, mcp_path: Path) -> None:
    omp_dir = project_directory / ".omp"
    if omp_dir.is_symlink():
        raise McpFileError(".omp directory is a symlink")
    if mcp_path.is_symlink():
        raise McpFileError(".omp/mcp.json is a symlink")
    # Ensure resolved path is inside the project
    try:
        resolved = mcp_path.resolve(strict=False)
        if not str(resolved).startswith(str(project_directory.resolve())):
            raise McpFileError(".omp/mcp.json resolves outside the project directory")
    except OSError:
        pass


def read_project_mcp(project: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Read the project's .omp/mcp.json and return (parsed_json, file_revision).

    absent file → ({"mcpServers":{}}, "missing")
    malformed/oversize → raises McpFileError
    """
    project_dir = Path(project["project_directory"])
    assert_project_marker(project_dir, project["project_id"])
    mcp_path = _mcp_path(project_dir)
    _reject_symlinks(project_dir, mcp_path)

    if not mcp_path.exists():
        return {"mcpServers": {}}, "missing"

    raw = mcp_path.read_bytes()
    if len(raw) > _MAX_FILE_BYTES:
        raise McpFileError("project MCP file exceeds 1 MiB")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise McpFileError(f"project MCP file is malformed JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise McpFileError("project MCP file must be a JSON object")
    servers = data.get("mcpServers")
    if servers is not None and not isinstance(servers, dict):
        raise McpFileError("mcpServers must be an object")
    if "mcpServers" not in data:
        data["mcpServers"] = {}
    return data, _file_sha256(raw)


def _write_project_mcp(project_dir: Path, data: dict[str, Any], expected_revision: str) -> str:
    """Atomically replace .omp/mcp.json, returning the new file_revision.

    Raises McpRevisionError if the current file does not match expected_revision.
    Raises McpFileError for symlinks or write failures.
    """
    mcp_path = _mcp_path(project_dir)
    _reject_symlinks(project_dir, mcp_path)

    # Check current revision
    if expected_revision == "missing":
        if mcp_path.exists():
            raise McpRevisionError("expected missing file but it exists (external edit)")
    else:
        if not mcp_path.exists():
            raise McpRevisionError("file was deleted externally")
        current_hash = _file_sha256(mcp_path.read_bytes())
        if current_hash != expected_revision:
            raise McpRevisionError("project MCP file was modified externally")

    new_bytes = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
    omp_dir = project_dir / ".omp"
    omp_dir.mkdir(parents=True, exist_ok=True)
    tmp = mcp_path.with_name(f".mcp.json.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_bytes(new_bytes)
        os.replace(str(tmp), str(mcp_path))
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise McpFileError(f"unable to write project MCP file: {exc}") from exc
    return _file_sha256(new_bytes)


# ---------------------------------------------------------------------------
# Service layer
# ---------------------------------------------------------------------------

class ProjectMcpService:
    """Coordinates catalog snapshots, project file writes, and provenance tracking."""

    def __init__(self, state: StateStore) -> None:
        self.state = state

    def list(self, project: dict[str, Any]) -> dict[str, Any]:
        """Return enriched project MCP server list."""
        data, file_revision = read_project_mcp(project)
        servers_raw = data.get("mcpServers", {})
        sources = self.state.get_project_mcp_sources(project["project_id"])

        servers: list[dict[str, Any]] = []
        for name, config in servers_raw.items():
            source_info = sources.get(name)
            entry: dict[str, Any] = {
                "name": name,
                "config": config,
                "source": "custom",
                "catalog_name": None,
                "source_revision": None,
                "source_missing": False,
                "update_available": False,
                "modified_locally": False,
                "status": "not_checked",
                "last_checked": None,
            }
            if source_info is not None:
                catalog_name = source_info["catalog_name"]
                entry["source"] = "catalog"
                entry["catalog_name"] = catalog_name
                entry["source_revision"] = source_info["source_revision"]
                entry["modified_locally"] = config_sha256(config) != source_info["snapshot_sha256"]
                catalog = self.state.get_mcp_catalog(catalog_name)
                if catalog is None:
                    entry["source_missing"] = True
                else:
                    entry["update_available"] = catalog["revision"] > source_info["source_revision"]
            servers.append(entry)
        return {"file_revision": file_revision, "servers": servers}

    def add_from_catalog(self, project: dict[str, Any], catalog_name: str, if_revision: str) -> dict[str, Any]:
        """Copy a catalog entry into the project file."""
        catalog = self.state.get_mcp_catalog(catalog_name)
        if catalog is None:
            raise McpConfigError(f"catalog entry {catalog_name!r} not found")
        config = json.loads(catalog["config_json"])
        return self._add(project, catalog_name, config, if_revision, catalog_name=catalog_name, catalog_revision=catalog["revision"])

    def add_custom(self, project: dict[str, Any], name: str, config: Any, if_revision: str) -> dict[str, Any]:
        """Add a new custom server entry to the project file."""
        config = validate_server(name, config)
        return self._add(project, name, config, if_revision, catalog_name=None, catalog_revision=None)

    def _add(
        self, project: dict[str, Any], name: str, config: dict[str, Any],
        if_revision: str, *, catalog_name: str | None, catalog_revision: int | None,
    ) -> dict[str, Any]:
        validate_name(name)
        project_dir = Path(project["project_directory"])
        data, current_rev = read_project_mcp(project)
        if current_rev != if_revision:
            raise McpRevisionError("stale file revision")
        servers = data.get("mcpServers", {})
        if name in servers:
            raise McpConfigError(f"server {name!r} already exists in this project")
        if len(servers) >= _MAX_PROJECT_SERVERS:
            raise McpConfigError(f"project already has {_MAX_PROJECT_SERVERS} servers")

        servers[name] = config
        data["mcpServers"] = servers

        source = None
        if catalog_name is not None and catalog_revision is not None:
            source = {
                "catalog_name": catalog_name,
                "source_revision": catalog_revision,
                "snapshot_sha256": config_sha256(config),
            }

        def write_file() -> None:
            nonlocal current_rev
            current_rev = _write_project_mcp(project_dir, data, if_revision)

        self.state.set_project_mcp_source(project["project_id"], name, source, write_file=write_file)
        return self.list(project)

    def edit(self, project: dict[str, Any], name: str, config: Any, if_revision: str) -> dict[str, Any]:
        """Edit an existing server entry in the project file."""
        config = validate_server(name, config)
        project_dir = Path(project["project_directory"])
        data, current_rev = read_project_mcp(project)
        if current_rev != if_revision:
            raise McpRevisionError("stale file revision")
        servers = data.get("mcpServers", {})
        if name not in servers:
            raise McpConfigError(f"server {name!r} not found in this project")

        servers[name] = config
        data["mcpServers"] = servers

        # Preserve existing provenance but recompute modified_locally
        sources = self.state.get_project_mcp_sources(project["project_id"])
        existing_source = sources.get(name)
        source = None
        if existing_source is not None:
            source = {
                "catalog_name": existing_source["catalog_name"],
                "source_revision": existing_source["source_revision"],
                "snapshot_sha256": existing_source["snapshot_sha256"],
            }

        def write_file() -> None:
            _write_project_mcp(project_dir, data, if_revision)

        self.state.set_project_mcp_source(project["project_id"], name, source, write_file=write_file)
        return self.list(project)

    def remove(self, project: dict[str, Any], name: str, if_revision: str) -> dict[str, Any]:
        """Remove a server entry from the project file."""
        project_dir = Path(project["project_directory"])
        data, current_rev = read_project_mcp(project)
        if current_rev != if_revision:
            raise McpRevisionError("stale file revision")
        servers = data.get("mcpServers", {})
        if name not in servers:
            raise McpConfigError(f"server {name!r} not found in this project")

        del servers[name]
        data["mcpServers"] = servers

        def write_file() -> None:
            _write_project_mcp(project_dir, data, if_revision)

        self.state.set_project_mcp_source(project["project_id"], name, None, write_file=write_file)
        return self.list(project)

    def refresh(self, project: dict[str, Any], name: str, if_revision: str) -> dict[str, Any]:
        """Replace a catalog-derived entry with the current catalog version."""
        sources = self.state.get_project_mcp_sources(project["project_id"])
        source_info = sources.get(name)
        if source_info is None:
            raise McpConfigError(f"server {name!r} is not from the catalog")
        catalog = self.state.get_mcp_catalog(source_info["catalog_name"])
        if catalog is None:
            raise McpConfigError(f"catalog entry {source_info['catalog_name']!r} no longer exists")

        project_dir = Path(project["project_directory"])
        data, current_rev = read_project_mcp(project)
        if current_rev != if_revision:
            raise McpRevisionError("stale file revision")
        servers = data.get("mcpServers", {})
        if name not in servers:
            raise McpConfigError(f"server {name!r} not found in this project")

        new_config = json.loads(catalog["config_json"])
        servers[name] = new_config
        data["mcpServers"] = servers

        source = {
            "catalog_name": source_info["catalog_name"],
            "source_revision": catalog["revision"],
            "snapshot_sha256": config_sha256(new_config),
        }

        def write_file() -> None:
            _write_project_mcp(project_dir, data, if_revision)

        self.state.set_project_mcp_source(project["project_id"], name, source, write_file=write_file)
        return self.list(project)

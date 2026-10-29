"""MCP catalog CRUD, project snapshot lifecycle, and file safety tests."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any

from orches.config import AppConfig
from orches.project_files import ensure_project_marker
from orches.project_mcp import (
    McpConfigError, McpFileError, McpRevisionError, ProjectMcpService,
    read_project_mcp, validate_name, validate_server,
)
from orches.state import ProjectConflictError, StateStore

STDIO_SERVER = {"type": "stdio", "command": "npx", "args": ["-y", "@mcp/fs"]}
HTTP_SERVER = {"type": "http", "url": "https://api.example.com/mcp"}


def _make_project(state: StateStore, root: Path, name: str = "test") -> dict[str, Any]:
    proj_dir = root / name
    proj_dir.mkdir(parents=True, exist_ok=True)
    pid = state.create_project(project_directory=str(proj_dir), name=name)
    ensure_project_marker(proj_dir, pid)
    return state.get_project(pid)


class ValidationTest(unittest.TestCase):
    def test_valid_stdio(self) -> None:
        result = validate_server("fs", {"command": "npx", "args": ["-y", "@mcp/fs"]})
        self.assertEqual(result["type"], "stdio")

    def test_valid_http(self) -> None:
        result = validate_server("gh", {"type": "http", "url": "https://github.com/mcp"})
        self.assertEqual(result["type"], "http")

    def test_valid_sse(self) -> None:
        result = validate_server("legacy", {"type": "sse", "url": "https://example.com/sse"})
        self.assertEqual(result["type"], "sse")

    def test_empty_name(self) -> None:
        with self.assertRaises(McpConfigError):
            validate_name("")

    def test_long_name(self) -> None:
        with self.assertRaises(McpConfigError):
            validate_name("a" * 101)

    def test_bad_char_name(self) -> None:
        with self.assertRaises(McpConfigError):
            validate_name("has space")

    def test_missing_command(self) -> None:
        with self.assertRaises(McpConfigError):
            validate_server("x", {"type": "stdio"})

    def test_missing_url(self) -> None:
        with self.assertRaises(McpConfigError):
            validate_server("x", {"type": "http"})

    def test_non_object(self) -> None:
        with self.assertRaises(McpConfigError):
            validate_server("x", "not-an-object")

    def test_var_url_accepted(self) -> None:
        """OMP ${VAR} references must be preserved, not rejected."""
        result = validate_server("env", {"type": "http", "url": "https://${HOST}/mcp"})
        self.assertIn("${HOST}", result["url"])


class CatalogStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = StateStore(Path(self.tmp.name) / "state.sqlite3")

    def test_create_list_get(self) -> None:
        entry = self.state.create_mcp_catalog("fs", STDIO_SERVER)
        self.assertEqual(entry["name"], "fs")
        self.assertEqual(entry["revision"], 1)
        self.assertEqual([e["name"] for e in self.state.list_mcp_catalog()], ["fs"])
        detail = self.state.get_mcp_catalog("fs")
        self.assertIsNotNone(detail)
        self.assertEqual(json.loads(detail["config_json"]), STDIO_SERVER)

    def test_duplicate_create_rejected(self) -> None:
        self.state.create_mcp_catalog("fs", STDIO_SERVER)
        with self.assertRaises(ProjectConflictError):
            self.state.create_mcp_catalog("fs", HTTP_SERVER)

    def test_update_and_stale_revision(self) -> None:
        self.state.create_mcp_catalog("fs", STDIO_SERVER)
        updated = self.state.update_mcp_catalog("fs", HTTP_SERVER, 1)
        self.assertEqual(updated["revision"], 2)
        with self.assertRaises(ProjectConflictError):
            self.state.update_mcp_catalog("fs", STDIO_SERVER, 1)

    def test_update_missing(self) -> None:
        with self.assertRaises(KeyError):
            self.state.update_mcp_catalog("missing", {}, 1)

    def test_delete(self) -> None:
        self.state.create_mcp_catalog("fs", STDIO_SERVER)
        self.assertTrue(self.state.delete_mcp_catalog("fs"))
        self.assertIsNone(self.state.get_mcp_catalog("fs"))
        self.assertFalse(self.state.delete_mcp_catalog("fs"))

    def test_schema_upgrade_from_v5(self) -> None:
        """A v5 database gains the new tables on re-open."""
        db_path = Path(self.tmp.name) / "upgrade.sqlite3"
        conn = sqlite3.connect(db_path)
        conn.executescript("PRAGMA user_version=5; CREATE TABLE IF NOT EXISTS projects (project_id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, target TEXT NOT NULL DEFAULT '', initial_prompt TEXT NOT NULL DEFAULT '', project_directory TEXT NOT NULL, pipeline_profile TEXT);")
        conn.close()
        state = StateStore(db_path)
        # New tables should exist
        state.create_mcp_catalog("test", STDIO_SERVER)
        self.assertEqual(state.get_mcp_catalog("test")["revision"], 1)


class ProjectMcpFileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = StateStore(self.root / "state.sqlite3")
        self.project = _make_project(self.state, self.root)
        self.svc = ProjectMcpService(self.state)

    @property
    def proj_dir(self) -> Path:
        return Path(self.project["project_directory"])

    def test_empty_read(self) -> None:
        data, rev = read_project_mcp(self.project)
        self.assertEqual(data, {"mcpServers": {}})
        self.assertEqual(rev, "missing")

    def test_add_custom_creates_file(self) -> None:
        result = self.svc.add_custom(self.project, "fs", STDIO_SERVER, "missing")
        self.assertEqual(len(result["servers"]), 1)
        self.assertEqual(result["servers"][0]["source"], "custom")
        raw = json.loads((self.proj_dir / ".omp" / "mcp.json").read_text())
        self.assertIn("fs", raw["mcpServers"])

    def test_add_from_catalog_copies_and_tracks(self) -> None:
        self.state.create_mcp_catalog("fs", STDIO_SERVER)
        result = self.svc.add_from_catalog(self.project, "fs", "missing")
        entry = result["servers"][0]
        self.assertEqual(entry["source"], "catalog")
        self.assertEqual(entry["catalog_name"], "fs")
        self.assertFalse(entry["modified_locally"])
        self.assertFalse(entry["update_available"])

    def test_catalog_update_does_not_auto_update_project(self) -> None:
        self.state.create_mcp_catalog("fs", STDIO_SERVER)
        result = self.svc.add_from_catalog(self.project, "fs", "missing")
        rev = result["file_revision"]
        self.state.update_mcp_catalog("fs", HTTP_SERVER, 1)
        result2 = self.svc.list(self.project)
        self.assertTrue(result2["servers"][0]["update_available"])
        self.assertEqual(result2["servers"][0]["config"]["type"], "stdio")

    def test_refresh_replaces_only_target_entry(self) -> None:
        self.state.create_mcp_catalog("fs", STDIO_SERVER)
        result = self.svc.add_from_catalog(self.project, "fs", "missing")
        result = self.svc.add_custom(self.project, "gh", HTTP_SERVER, result["file_revision"])
        self.state.update_mcp_catalog("fs", {"type": "stdio", "command": "npx", "args": ["-y", "@mcp/fs-v3"]}, 1)
        result2 = self.svc.refresh(self.project, "fs", result["file_revision"])
        fs_entry = next(s for s in result2["servers"] if s["name"] == "fs")
        gh_entry = next(s for s in result2["servers"] if s["name"] == "gh")
        self.assertIn("fs-v3", fs_entry["config"]["args"][-1])
        self.assertEqual(gh_entry["config"]["url"], "https://api.example.com/mcp")
        self.assertFalse(fs_entry["update_available"])

    def test_edit_preserves_unrelated_json_keys(self) -> None:
        omp_dir = self.proj_dir / ".omp"
        omp_dir.mkdir(parents=True, exist_ok=True)
        (omp_dir / "mcp.json").write_text(json.dumps({
            "$schema": "https://example.com/schema.json",
            "mcpServers": {"fs": STDIO_SERVER},
            "disabledServers": ["old"],
        }))
        data, rev = read_project_mcp(self.project)
        self.assertIn("$schema", data)
        result = self.svc.edit(self.project, "fs", HTTP_SERVER, rev)
        raw = json.loads((omp_dir / "mcp.json").read_text())
        self.assertEqual(raw["$schema"], "https://example.com/schema.json")
        self.assertEqual(raw["disabledServers"], ["old"])
        self.assertEqual(raw["mcpServers"]["fs"]["type"], "http")

    def test_remove_entry(self) -> None:
        result = self.svc.add_custom(self.project, "fs", STDIO_SERVER, "missing")
        result2 = self.svc.remove(self.project, "fs", result["file_revision"])
        self.assertEqual(result2["servers"], [])

    def test_stale_revision_rejected(self) -> None:
        result = self.svc.add_custom(self.project, "fs", STDIO_SERVER, "missing")
        with self.assertRaises(McpRevisionError):
            self.svc.add_custom(self.project, "gh", HTTP_SERVER, "stale")

    def test_duplicate_name_rejected(self) -> None:
        result = self.svc.add_custom(self.project, "fs", STDIO_SERVER, "missing")
        with self.assertRaises(McpConfigError):
            self.svc.add_custom(self.project, "fs", HTTP_SERVER, result["file_revision"])

    def test_active_run_blocks_mutation(self) -> None:
        from orches.models import utc_now
        p = self.state.get_project(self.project["project_id"])
        run_id, _ = self.state.reserve_project_run(
            p["project_id"], expected_updated_at=p["updated_at"],
            profile_json="{}", source="api",
        )
        with self.assertRaises(ProjectConflictError):
            self.svc.add_custom(self.project, "fs", STDIO_SERVER, "missing")

    def test_deleted_catalog_leaves_project_copy(self) -> None:
        self.state.create_mcp_catalog("fs", STDIO_SERVER)
        result = self.svc.add_from_catalog(self.project, "fs", "missing")
        self.state.delete_mcp_catalog("fs")
        result2 = self.svc.list(self.project)
        entry = result2["servers"][0]
        self.assertEqual(entry["source"], "catalog")
        self.assertTrue(entry["source_missing"])
        self.assertEqual(entry["config"]["type"], "stdio")

    def test_project_deletion_cascades_provenance_not_file(self) -> None:
        self.state.create_mcp_catalog("fs", STDIO_SERVER)
        self.svc.add_from_catalog(self.project, "fs", "missing")
        mcp_path = self.proj_dir / ".omp" / "mcp.json"
        self.assertTrue(mcp_path.exists())
        self.state.delete_project(self.project["project_id"])
        self.assertEqual(self.state.get_project_mcp_sources(self.project["project_id"]), {})
        self.assertTrue(mcp_path.exists())

    def test_malformed_file_rejected(self) -> None:
        omp_dir = self.proj_dir / ".omp"
        omp_dir.mkdir(parents=True, exist_ok=True)
        (omp_dir / "mcp.json").write_text("not json")
        with self.assertRaises(McpFileError):
            read_project_mcp(self.project)

    @unittest.skipIf(os.name == "nt", "symlinks require admin on Windows")
    def test_symlinked_omp_dir_rejected(self) -> None:
        real = self.proj_dir / "real_omp"
        real.mkdir(parents=True, exist_ok=True)
        link = self.proj_dir / ".omp"
        link.symlink_to(real)
        with self.assertRaises(McpFileError):
            read_project_mcp(self.project)

    def test_modified_locally_detection(self) -> None:
        self.state.create_mcp_catalog("fs", STDIO_SERVER)
        result = self.svc.add_from_catalog(self.project, "fs", "missing")
        self.assertFalse(result["servers"][0]["modified_locally"])
        result2 = self.svc.edit(
            self.project, "fs",
            {"type": "stdio", "command": "npx", "args": ["-y", "@mcp/fs-edited"]},
            result["file_revision"],
        )
        self.assertTrue(result2["servers"][0]["modified_locally"])


class ApiCatalogTest(unittest.TestCase):
    """Test the catalog and project MCP API routes end-to-end."""

    def setUp(self) -> None:
        from test_auth import install_admin_password, login
        install_admin_password(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        from orches.api import create_app
        cfg = AppConfig(
            root=root, state_db=root / "state.sqlite3", runs_root=root / "runs",
            profiles_db=root / "profiles.sqlite3", omp_bin="omp",
        )
        app = create_app(cfg)
        from starlette.testclient import TestClient
        self.client = TestClient(app)
        login(self.client)
        self.root = root

    def test_catalog_crud(self) -> None:
        # Create
        resp = self.client.post("/api/mcp-servers", json={"name": "fs", "config": STDIO_SERVER})
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertEqual(data["name"], "fs")
        self.assertEqual(data["revision"], 1)
        # List
        resp = self.client.get("/api/mcp-servers")
        self.assertEqual(len(resp.json()["servers"]), 1)
        self.assertEqual(resp.json()["servers"][0]["transport"], "stdio")
        # Detail
        resp = self.client.get("/api/mcp-servers/fs")
        self.assertEqual(resp.json()["config"]["command"], "npx")
        # Update
        resp = self.client.put("/api/mcp-servers/fs", json={"config": HTTP_SERVER, "revision": 1})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["revision"], 2)
        # Stale update
        resp = self.client.put("/api/mcp-servers/fs", json={"config": HTTP_SERVER, "revision": 1})
        self.assertEqual(resp.status_code, 409)
        # Delete
        resp = self.client.delete("/api/mcp-servers/fs")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["deleted"])
        # 404
        resp = self.client.get("/api/mcp-servers/fs")
        self.assertEqual(resp.status_code, 404)

    def test_project_mcp_lifecycle(self) -> None:
        proj_dir = self.root / "proj"
        proj_dir.mkdir()
        resp = self.client.post("/api/projects", json={
            "name": "test", "project_directory": str(proj_dir),
        })
        self.assertEqual(resp.status_code, 201)
        pid = resp.json()["project_id"]

        # Create catalog entry
        self.client.post("/api/mcp-servers", json={"name": "fs", "config": STDIO_SERVER})
        # List project (empty)
        resp = self.client.get(f"/api/projects/{pid}/mcp-servers")
        self.assertEqual(resp.json()["file_revision"], "missing")
        self.assertEqual(resp.json()["servers"], [])

        # Add from catalog
        resp = self.client.post(f"/api/projects/{pid}/mcp-servers", json={
            "source": "catalog", "name": "fs", "if_revision": "missing",
        })
        self.assertEqual(resp.status_code, 201)
        servers = resp.json()["servers"]
        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0]["source"], "catalog")
        rev = resp.json()["file_revision"]

        # Add custom
        resp = self.client.post(f"/api/projects/{pid}/mcp-servers", json={
            "source": "custom", "name": "gh", "config": HTTP_SERVER, "if_revision": rev,
        })
        self.assertEqual(resp.status_code, 201)
        rev = resp.json()["file_revision"]

        # Edit
        resp = self.client.put(f"/api/projects/{pid}/mcp-servers/gh", json={
            "config": {"type": "http", "url": "https://new.example.com/mcp"},
            "if_revision": rev,
        })
        self.assertEqual(resp.status_code, 200)
        rev = resp.json()["file_revision"]

        # Stale revision
        resp = self.client.put(f"/api/projects/{pid}/mcp-servers/gh", json={
            "config": HTTP_SERVER, "if_revision": "stale",
        })
        self.assertEqual(resp.status_code, 409)

        # Remove
        resp = self.client.request("DELETE", f"/api/projects/{pid}/mcp-servers/gh",
                                    json={"if_revision": rev})
        self.assertEqual(resp.status_code, 200)
        rev = resp.json()["file_revision"]
        self.assertEqual(len(resp.json()["servers"]), 1)

        # Update catalog and refresh
        self.client.put("/api/mcp-servers/fs", json={
            "config": {"type": "stdio", "command": "npx", "args": ["-y", "@mcp/fs-v2"]},
            "revision": 1,
        })
        resp = self.client.get(f"/api/projects/{pid}/mcp-servers")
        self.assertTrue(resp.json()["servers"][0]["update_available"])
        resp = self.client.post(f"/api/projects/{pid}/mcp-servers/fs/refresh", json={"if_revision": rev})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("fs-v2", resp.json()["servers"][0]["config"]["args"][-1])

    def test_invalid_config_rejected(self) -> None:
        resp = self.client.post("/api/mcp-servers", json={"name": "x", "config": {"type": "stdio"}})
        self.assertEqual(resp.status_code, 400)

    def test_bad_name_rejected(self) -> None:
        resp = self.client.post("/api/mcp-servers", json={"name": "has space", "config": STDIO_SERVER})
        self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()

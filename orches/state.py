"""SQLite-backed project, run, invocation and artifact state."""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from .models import (
    AgentInvocation,
    AgentRole,
    AgentStatus,
    ArtifactRef,
    utc_now,
)

_LOG = logging.getLogger("orches.state")

SCHEMA_VERSION = 3
LEGACY_APP_DIR = ".cve-forge"
LEGACY_MARKER = "project.json"


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class StateError(RuntimeError):
    """Base class for persistent state errors."""


class ProjectConflictError(StateError):
    """A project cannot be created, edited or started in its current state."""


class ArtifactConflictError(StateError):
    """An artifact path already exists with different content."""


class ConnectionUseError(StateError):
    """A connection is claimed by a live agent or the caller does not own it."""


class LegacyMigrationError(StateError):
    """A previous installation's data could not be discarded safely."""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    initial_prompt TEXT NOT NULL DEFAULT '',
    project_directory TEXT NOT NULL,
    pipeline_profile TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    error TEXT,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    source TEXT NOT NULL DEFAULT 'api'
);
CREATE INDEX IF NOT EXISTS runs_project_created_idx ON runs(project_id, created_at);
CREATE TABLE IF NOT EXISTS agent_invocations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    model_selector TEXT,
    thinking TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    prompt_version TEXT NOT NULL,
    returncode INTEGER,
    error TEXT,
    output_artifact_id TEXT,
    stderr_artifact_id TEXT,
    output_json TEXT
);
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    relative_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    media_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, relative_path)
);
CREATE TABLE IF NOT EXISTS connections (
    connection_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    os TEXT NOT NULL,
    ip TEXT,
    cred TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS connection_claims (
    connection_id TEXT PRIMARY KEY REFERENCES connections(connection_id) ON DELETE CASCADE,
    invocation_id INTEGER NOT NULL REFERENCES agent_invocations(id) ON DELETE CASCADE,
    claimed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS connection_claims_invocation_idx ON connection_claims(invocation_id);
CREATE TABLE IF NOT EXISTS project_agent_positions (
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    x REAL NOT NULL,
    y REAL NOT NULL,
    PRIMARY KEY(project_id, role)
);
CREATE TABLE IF NOT EXISTS run_pipeline_profiles (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
    profile_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS legacy_cleanup (
    source_db TEXT PRIMARY KEY,
    manifest_json TEXT NOT NULL,
    cleaned INTEGER NOT NULL DEFAULT 0
);
"""

_LEGACY_TABLES = (
    "research_findings", "environment_tools", "leases", "environments",
    "analysis_environments", "run_pipeline_profiles", "project_agent_positions",
    "artifacts", "agent_invocations", "runs", "projects",
)


def _is_legacy_database(conn: sqlite3.Connection) -> bool:
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "projects" not in tables or "runs" not in tables:
        return False
    project_columns = {row["name"] for row in conn.execute("PRAGMA table_info(projects)")}
    run_columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
    return "cve_id" in project_columns and "cve_id" in run_columns


def _legacy_manifest(conn: sqlite3.Connection, *, in_place: bool) -> dict[str, Any]:
    projects = [
        {"project_id": str(row["project_id"]), "project_directory": row["project_directory"]}
        for row in conn.execute("SELECT project_id,project_directory FROM projects")
    ]
    runs = [
        str(row["run_id"]) for row in conn.execute("SELECT run_id FROM runs")
        if _safe_run_id(str(row["run_id"]))
    ]
    return {"projects": projects, "runs": runs, "in_place": in_place}


def _legacy_connections(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "analysis_environments" not in tables:
        return []
    rows = conn.execute(
        "SELECT analysis_environment_id,name,os,ip,cred,created_at,updated_at FROM analysis_environments"
    ).fetchall()
    return [
        {
            "connection_id": str(row["analysis_environment_id"]), "name": row["name"], "os": row["os"],
            "ip": row["ip"], "cred": row["cred"], "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def _safe_run_id(run_id: str) -> bool:
    return bool(run_id) and Path(run_id).name == run_id and all(c in "0123456789abcdef" for c in run_id.lower())


class StateStore:
    def __init__(self, path: str | Path, *, legacy_db: str | Path | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._legacy_db = Path(legacy_db) if legacy_db else None
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, factory=_ClosingConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    # ---------------------------------------------------------------- schema
    def _initialize(self) -> None:
        self._discard_in_place_legacy()
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            # Additive upgrade for v2 stores: CREATE TABLE IF NOT EXISTS keeps the
            # old column set, so add output_json explicitly.
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(agent_invocations)")}
            if "output_json" not in columns:
                conn.execute("ALTER TABLE agent_invocations ADD COLUMN output_json TEXT")
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self._stage_external_legacy()
        self._run_pending_cleanups()

    def _open_legacy(self, path: Path) -> sqlite3.Connection | None:
        if not path.exists() or path.stat().st_size == 0:
            return None
        conn = sqlite3.connect(path, timeout=5, factory=_ClosingConnection)
        conn.row_factory = sqlite3.Row
        try:
            if not _is_legacy_database(conn):
                conn.close()
                return None
        except sqlite3.DatabaseError as exc:
            conn.close()
            raise LegacyMigrationError(f"unable to read legacy database {path}: {exc}") from exc
        return conn

    def _discard_in_place_legacy(self) -> None:
        """Replace a legacy schema in the configured database, keeping connections."""
        conn = self._open_legacy(self.path)
        if conn is None:
            return
        try:
            with conn:
                manifest = _legacy_manifest(conn, in_place=True)
                connections = _legacy_connections(conn)
                conn.execute("BEGIN IMMEDIATE")
                for table in _LEGACY_TABLES:
                    conn.execute(f"DROP TABLE IF EXISTS {table}")
                conn.executescript(_SCHEMA)
                for item in connections:
                    conn.execute(
                        """INSERT OR REPLACE INTO connections(
                            connection_id,name,os,ip,cred,created_at,updated_at
                        ) VALUES(?,?,?,?,?,?,?)""",
                        (item["connection_id"], item["name"], item["os"], item["ip"], item["cred"],
                         item["created_at"], item["updated_at"]),
                    )
                conn.execute(
                    "INSERT OR REPLACE INTO legacy_cleanup(source_db,manifest_json,cleaned) VALUES(?,?,0)",
                    (str(self.path.resolve()), json.dumps(manifest, sort_keys=True)),
                )
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        except sqlite3.DatabaseError as exc:
            raise LegacyMigrationError(f"unable to discard legacy data in {self.path}: {exc}") from exc
        with self._connect() as vacuum:
            vacuum.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            vacuum.execute("VACUUM")

    def _stage_external_legacy(self) -> None:
        """Import connections from a previous installation's default database."""
        legacy_path = self._legacy_db
        if legacy_path is None or legacy_path.resolve() == self.path.resolve():
            return
        conn = self._open_legacy(legacy_path)
        if conn is None:
            return
        try:
            with conn:
                manifest = _legacy_manifest(conn, in_place=False)
                connections = _legacy_connections(conn)
        except sqlite3.DatabaseError as exc:
            raise LegacyMigrationError(f"unable to read legacy database {legacy_path}: {exc}") from exc
        source = str(legacy_path.resolve())
        with self._connect() as target:
            target.execute("BEGIN IMMEDIATE")
            if target.execute("SELECT 1 FROM legacy_cleanup WHERE source_db=?", (source,)).fetchone():
                return
            for item in connections:
                existing = target.execute(
                    "SELECT name,os,ip,cred FROM connections WHERE connection_id=?",
                    (item["connection_id"],),
                ).fetchone()
                if existing is not None:
                    same = all(existing[key] == item[key] for key in ("name", "os", "ip", "cred"))
                    if not same:
                        raise LegacyMigrationError(
                            f"connection {item['connection_id']} already exists with different content"
                        )
                    continue
                target.execute(
                    """INSERT INTO connections(
                        connection_id,name,os,ip,cred,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?)""",
                    (item["connection_id"], item["name"], item["os"], item["ip"], item["cred"],
                     item["created_at"], item["updated_at"]),
                )
            target.execute(
                "INSERT INTO legacy_cleanup(source_db,manifest_json,cleaned) VALUES(?,?,0)",
                (source, json.dumps(manifest, sort_keys=True)),
            )

    def _run_pending_cleanups(self) -> None:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT source_db,manifest_json FROM legacy_cleanup WHERE cleaned=0"
            ).fetchall()
        for row in rows:
            source = str(row["source_db"])
            manifest = json.loads(row["manifest_json"])
            try:
                _discard_legacy_files(manifest, Path(source))
            except LegacyMigrationError as exc:
                _LOG.warning("legacy cleanup pending: %s", exc)
                continue
            with self._connect() as conn:
                conn.execute("DELETE FROM legacy_cleanup WHERE source_db=?", (source,))

    # -------------------------------------------------------------- projects
    def create_project(
        self, *, project_directory: str, name: str, target: str = "",
        initial_prompt: str = "", pipeline_profile: str | None = None,
    ) -> str:
        if not project_directory:
            raise ValueError("project_directory is required")
        name = name.strip()
        if not name:
            raise ValueError("name is required")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            normalized = os.path.normcase(str(Path(project_directory).expanduser().resolve()))
            bound = conn.execute("SELECT project_directory FROM projects").fetchall()
            if any(os.path.normcase(str(Path(row["project_directory"]).resolve())) == normalized for row in bound):
                raise ProjectConflictError("project directory is already bound")
            project_id = uuid.uuid4().hex
            now = utc_now()
            conn.execute(
                """INSERT INTO projects(
                    project_id,name,status,created_at,updated_at,
                    target,initial_prompt,project_directory,pipeline_profile
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (project_id, name, "active", now, now, target, initial_prompt,
                 project_directory, pipeline_profile),
            )
        return project_id

    def update_project(self, project_id: str, **changes: Any) -> dict[str, Any] | None:
        allowed = {"name", "target", "initial_prompt", "project_directory", "pipeline_profile"}
        if not changes.keys() <= allowed:
            raise ValueError(f"unsupported project settings: {sorted(changes.keys() - allowed)}")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
            if not current:
                return None
            if conn.execute(
                "SELECT 1 FROM runs WHERE project_id=? AND status='running' LIMIT 1", (project_id,)
            ).fetchone():
                raise ProjectConflictError("cannot edit a project while its run is active")
            if "project_directory" in changes:
                requested = changes["project_directory"]
                if not requested:
                    raise ValueError("project_directory cannot be empty")
                if current["project_directory"] != requested:
                    raise ProjectConflictError("project directory is already bound and cannot be changed")
            if "name" in changes and not str(changes["name"]).strip():
                raise ValueError("name must be a nonempty string")
            if changes:
                assignments = ", ".join(f"{key}=?" for key in changes)
                conn.execute(
                    f"UPDATE projects SET {assignments},updated_at=? WHERE project_id=?",
                    (*changes.values(), utc_now(), project_id),
                )
            row = conn.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
            return dict(row)

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
        return dict(row) if row else None

    def list_projects(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM projects ORDER BY updated_at DESC").fetchall()
        return [dict(row) for row in rows]

    def get_current_run(self, project_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM runs WHERE project_id=?
                   ORDER BY CASE WHEN status='running' THEN 0 ELSE 1 END,
                            created_at DESC, run_id DESC LIMIT 1""", (project_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_project_run_ids(self, project_id: str) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT run_id FROM runs WHERE project_id=?", (project_id,)).fetchall()
        return {str(row["run_id"]) for row in rows}

    def delete_project(self, project_id: str) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute("SELECT 1 FROM projects WHERE project_id=?", (project_id,)).fetchone():
                return False
            if conn.execute(
                "SELECT 1 FROM runs WHERE project_id=? AND status='running' LIMIT 1", (project_id,)
            ).fetchone():
                raise ProjectConflictError("cannot delete a project while its run is active")
            conn.execute("DELETE FROM projects WHERE project_id=?", (project_id,))
            return True

    def get_agent_layout(self, project_id: str) -> dict[str, dict[str, float]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT role,x,y FROM project_agent_positions WHERE project_id=?", (project_id,)
            ).fetchall()
        return {row["role"]: {"x": float(row["x"]), "y": float(row["y"])} for row in rows}

    def set_agent_position(
        self, project_id: str, role: str, x: float, y: float
    ) -> dict[str, dict[str, float]] | None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute("SELECT 1 FROM projects WHERE project_id=?", (project_id,)).fetchone():
                return None
            conn.execute(
                """INSERT INTO project_agent_positions(project_id,role,x,y) VALUES(?,?,?,?)
                   ON CONFLICT(project_id,role) DO UPDATE SET x=excluded.x,y=excluded.y""",
                (project_id, role, float(x), float(y)),
            )
            rows = conn.execute(
                "SELECT role,x,y FROM project_agent_positions WHERE project_id=?", (project_id,)
            ).fetchall()
            return {row["role"]: {"x": float(row["x"]), "y": float(row["y"])} for row in rows}

    # ----------------------------------------------------------- connections
    _CONNECTION_USAGE_SQL = """
        SELECT c.*, cl.invocation_id AS _claim_invocation_id,
               ai.run_id AS _claim_run_id, ai.role AS _claim_role
        FROM connections c
        LEFT JOIN connection_claims cl ON cl.connection_id = c.connection_id
        LEFT JOIN agent_invocations ai ON ai.id = cl.invocation_id
    """

    @staticmethod
    def _connection_record(row: sqlite3.Row) -> dict[str, Any]:
        record = dict(row)
        invocation_id = record.pop("_claim_invocation_id", None)
        run_id = record.pop("_claim_run_id", None)
        role = record.pop("_claim_role", None)
        record["in_use"] = invocation_id is not None
        record["used_by"] = {"run_id": run_id, "role": role} if invocation_id is not None else None
        return record

    def _connection_row(self, conn: sqlite3.Connection, connection_id: str) -> sqlite3.Row | None:
        return conn.execute(
            f"{self._CONNECTION_USAGE_SQL} WHERE c.connection_id=?", (connection_id,)
        ).fetchone()

    @staticmethod
    def _require_live_invocation(conn: sqlite3.Connection, invocation_id: int) -> None:
        live = conn.execute(
            """SELECT 1 FROM agent_invocations ai JOIN runs r ON r.run_id = ai.run_id
               WHERE ai.id=? AND ai.status='running' AND r.status='running'""",
            (invocation_id,),
        ).fetchone()
        if live is None:
            raise ConnectionUseError("agent invocation is not running")

    def create_connection(
        self, *, name: str, os: str, ip: str | None = None, cred: str | None = None,
    ) -> dict[str, Any]:
        record = {
            "connection_id": uuid.uuid4().hex,
            "name": name,
            "os": os,
            "ip": ip,
            "cred": cred,
            "created_at": utc_now(),
        }
        record["updated_at"] = record["created_at"]
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO connections(
                    connection_id,name,os,ip,cred,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?)""",
                (record["connection_id"], name, os, ip, cred,
                 record["created_at"], record["updated_at"]),
            )
        record["in_use"] = False
        record["used_by"] = None
        return record

    def claim_connection(self, connection_id: str, invocation_id: int) -> dict[str, Any]:
        """Register exclusive use of a connection by one live agent invocation."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._require_live_invocation(conn, invocation_id)
            if self._connection_row(conn, connection_id) is None:
                raise ConnectionUseError("unknown connection id")
            claim = conn.execute(
                "SELECT invocation_id FROM connection_claims WHERE connection_id=?",
                (connection_id,),
            ).fetchone()
            if claim is None:
                conn.execute(
                    "INSERT INTO connection_claims(connection_id,invocation_id,claimed_at) VALUES(?,?,?)",
                    (connection_id, invocation_id, utc_now()),
                )
            elif claim["invocation_id"] != invocation_id:
                raise ConnectionUseError("connection is in use")
            return self._connection_record(self._connection_row(conn, connection_id))

    def release_connection(self, connection_id: str, invocation_id: int) -> dict[str, Any]:
        """Release a claim held by this invocation; only the owner may release."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._require_live_invocation(conn, invocation_id)
            if self._connection_row(conn, connection_id) is None:
                raise ConnectionUseError("unknown connection id")
            claim = conn.execute(
                "SELECT invocation_id FROM connection_claims WHERE connection_id=?",
                (connection_id,),
            ).fetchone()
            if claim is None or claim["invocation_id"] != invocation_id:
                raise ConnectionUseError("connection is not claimed by this agent")
            conn.execute("DELETE FROM connection_claims WHERE connection_id=?", (connection_id,))
            return self._connection_record(self._connection_row(conn, connection_id))

    def reveal_claimed_connection(self, connection_id: str, invocation_id: int) -> dict[str, Any]:
        """Return a connection with its credential, only to the live claim owner."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._require_live_invocation(conn, invocation_id)
            row = self._connection_row(conn, connection_id)
            if row is None:
                raise ConnectionUseError("unknown connection id")
            record = self._connection_record(row)
            if not record["in_use"] or row["_claim_invocation_id"] != invocation_id:
                raise ConnectionUseError("connection is not claimed by this agent")
            return record

    def list_connections(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                f"{self._CONNECTION_USAGE_SQL} ORDER BY c.created_at ASC, c.connection_id ASC"
            ).fetchall()
        return [self._connection_record(row) for row in rows]

    def get_connection(self, connection_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = self._connection_row(conn, connection_id)
        return self._connection_record(row) if row else None

    def update_connection(self, connection_id: str, **changes: Any) -> dict[str, Any] | None:
        allowed = {"name", "os", "ip", "cred"}
        if not changes.keys() <= allowed:
            raise ValueError(f"unsupported connection fields: {sorted(changes.keys() - allowed)}")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = self._connection_row(conn, connection_id)
            if current is None:
                return None
            if current["_claim_invocation_id"] is not None:
                raise ConnectionUseError("connection is in use")
            if changes:
                assignments = ", ".join(f"{key}=?" for key in changes)
                conn.execute(
                    f"UPDATE connections SET {assignments},updated_at=? WHERE connection_id=?",
                    (*changes.values(), utc_now(), connection_id),
                )
            return self._connection_record(self._connection_row(conn, connection_id))

    def delete_connection(self, connection_id: str) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                "SELECT 1 FROM connection_claims WHERE connection_id=?", (connection_id,)
            ).fetchone():
                raise ConnectionUseError("connection is in use")
            cursor = conn.execute("DELETE FROM connections WHERE connection_id=?", (connection_id,))
            return cursor.rowcount == 1

    # ----------------------------------------------------------- invocations
    def list_invocations(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_invocations WHERE run_id=? ORDER BY started_at ASC, id ASC", (run_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def start_invocation(self, invocation: AgentInvocation) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO agent_invocations(
                    run_id,role,model_selector,thinking,status,started_at,finished_at,
                    prompt_version,returncode,error,output_artifact_id,stderr_artifact_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    invocation.run_id,
                    invocation.role.value if isinstance(invocation.role, AgentRole) else invocation.role,
                    invocation.model_selector, invocation.thinking, invocation.status.value,
                    invocation.started_at, invocation.finished_at, invocation.prompt_version,
                    invocation.returncode, invocation.error, invocation.output_artifact_id,
                    invocation.stderr_artifact_id,
                ),
            )
            return int(cursor.lastrowid)

    def update_invocation(
        self,
        invocation_id: int,
        *,
        status: AgentStatus,
        finished_at: str | None,
        returncode: int | None,
        error: str | None,
        output_artifact_id: str | None,
        stderr_artifact_id: str | None,
        output_json: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """UPDATE agent_invocations SET status=?,finished_at=?,returncode=?,error=?,
                   output_artifact_id=?,stderr_artifact_id=?,output_json=? WHERE id=?""",
                (status.value, finished_at, returncode, error, output_artifact_id,
                 stderr_artifact_id, output_json, invocation_id),
            )
            if status != AgentStatus.RUNNING:
                conn.execute(
                    "DELETE FROM connection_claims WHERE invocation_id=?", (invocation_id,)
                )

    def carry_over_invocations(self, run_id: str, invocations: list[dict[str, Any]]) -> None:
        """Record completed workers reused from a previous run of the same snapshot."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone() is None:
                raise KeyError("run not found")
            for item in invocations:
                conn.execute(
                    """INSERT INTO agent_invocations(
                        run_id,role,model_selector,thinking,status,started_at,finished_at,
                        prompt_version,returncode,error,output_artifact_id,stderr_artifact_id,output_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        run_id, item["role"], item["model_selector"], item["thinking"],
                        AgentStatus.COMPLETED.value, item["started_at"], item["finished_at"],
                        "reused", item["returncode"], None, item.get("output_artifact_id"),
                        item.get("stderr_artifact_id"), item.get("output_json"),
                    ),
                )

    # ------------------------------------------------------------------ runs
    def _set_project_after_run(self, conn: sqlite3.Connection, run_id: str, status: str) -> None:
        row = conn.execute("SELECT project_id FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if not row or not row["project_id"]:
            return
        project_id = row["project_id"]
        if status == "running":
            project_status = "active"
        elif status in {"completed", "failed", "blocked"}:
            project_status = status
        else:
            return
        conn.execute(
            "UPDATE projects SET status=?,updated_at=? WHERE project_id=?",
            (project_status, utc_now(), project_id),
        )

    def reserve_project_run(
        self, project_id: str, *, expected_updated_at: str, source: str = "api",
        profile_json: str,
    ) -> tuple[str, tuple[str, ...]]:
        """Reserve the sole current project run and return terminal run ids to clean."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            project = conn.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
            if project is None:
                raise KeyError("project not found")
            if project["updated_at"] != expected_updated_at:
                raise ProjectConflictError("project settings changed; retry start")
            if conn.execute(
                "SELECT 1 FROM runs WHERE project_id=? AND status='running' LIMIT 1", (project_id,)
            ).fetchone():
                raise ProjectConflictError("project already has a running run")
            old_rows = conn.execute(
                "SELECT run_id FROM runs WHERE project_id=? AND status!='running'", (project_id,)
            ).fetchall()
            old_ids = tuple(str(row["run_id"]) for row in old_rows)
            for old_id in old_ids:
                conn.execute("DELETE FROM runs WHERE run_id=?", (old_id,))
            now = utc_now()
            run_id = uuid.uuid4().hex
            conn.execute(
                """INSERT INTO runs(run_id,status,created_at,updated_at,project_id,source)
                   VALUES(?,?,?,?,?,?)""",
                (run_id, "running", now, now, project_id, source),
            )
            conn.execute(
                "INSERT INTO run_pipeline_profiles(run_id,profile_json) VALUES(?,?)",
                (run_id, profile_json),
            )
            self._set_project_after_run(conn, run_id, "running")
            return run_id, old_ids

    def prune_project_history(self) -> list[tuple[str, tuple[str, ...]]]:
        """Drop obsolete terminal project runs and return their ids for file cleanup."""
        removed: list[tuple[str, tuple[str, ...]]] = []
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            projects = conn.execute("SELECT project_id FROM projects").fetchall()
            for project in projects:
                active = conn.execute(
                    "SELECT 1 FROM runs WHERE project_id=? AND status='running' LIMIT 1",
                    (project["project_id"],),
                ).fetchone()
                rows = conn.execute(
                    """SELECT run_id FROM runs WHERE project_id=? AND status!='running'
                       ORDER BY created_at DESC, run_id DESC""",
                    (project["project_id"],),
                ).fetchall()
                keep = 0 if active else 1
                if len(rows) <= keep:
                    continue
                old_ids = tuple(str(row["run_id"]) for row in rows[keep:])
                for old_id in old_ids:
                    conn.execute("DELETE FROM runs WHERE run_id=?", (old_id,))
                removed.append((str(project["project_id"]), old_ids))
        return removed

    @staticmethod
    def _release_run_claims(conn: sqlite3.Connection, run_id: str) -> None:
        conn.execute(
            """DELETE FROM connection_claims WHERE invocation_id IN
               (SELECT id FROM agent_invocations WHERE run_id=?)""",
            (run_id,),
        )

    def fail_interrupted_api_runs(self) -> None:
        """Recover browser-initiated work abandoned by a prior API process."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT run_id FROM runs WHERE source='api' AND status='running'"
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE runs SET status='failed',error=?,updated_at=? WHERE run_id=?",
                    ("API stopped before run completed", utc_now(), row["run_id"]),
                )
                self._release_run_claims(conn, row["run_id"])
                self._set_project_after_run(conn, row["run_id"], "failed")

    def update_run(self, run_id: str, status: str, error: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE runs SET status=?, error=?, updated_at=? WHERE run_id=?",
                (status, error, utc_now(), run_id),
            )
            if status in {"completed", "failed", "blocked"}:
                self._release_run_claims(conn, run_id)
            if status in {"running", "completed", "failed", "blocked"}:
                self._set_project_after_run(conn, run_id, status)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def get_run_profile(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT profile_json FROM run_pipeline_profiles WHERE run_id=?", (run_id,)
            ).fetchone()
        if row is None:
            return None
        value = json.loads(row["profile_json"])
        if not isinstance(value, dict):
            raise ValueError("stored pipeline profile is not an object")
        return value

    # ------------------------------------------------------------- artifacts
    def save_artifact(self, artifact: ArtifactRef) -> None:
        with self._connect() as conn:
            try:
                conn.execute(
                    """INSERT INTO artifacts(
                        artifact_id,run_id,relative_path,sha256,size,media_type,created_at
                    ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        artifact.artifact_id, artifact.run_id, artifact.relative_path,
                        artifact.sha256, artifact.size, artifact.media_type, artifact.created_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                existing = conn.execute(
                    "SELECT sha256 FROM artifacts WHERE run_id=? AND relative_path=?",
                    (artifact.run_id, artifact.relative_path),
                ).fetchone()
                if existing and existing["sha256"] != artifact.sha256:
                    raise ArtifactConflictError(
                        f"artifact path already exists with different content: {artifact.relative_path}"
                    ) from exc
                raise

    def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
        return dict(row) if row else None

    def list_artifacts(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM artifacts WHERE run_id=? ORDER BY relative_path", (run_id,)
            ).fetchall()
        return [dict(row) for row in rows]


def _discard_legacy_files(manifest: dict[str, Any], source_db: Path) -> None:
    """Delete only app-owned files recorded by a previous installation."""
    run_ids = [str(item) for item in manifest.get("runs", []) if _safe_run_id(str(item))]
    for project in manifest.get("projects", []):
        directory = project.get("project_directory")
        project_id = project.get("project_id")
        if not directory or not project_id:
            continue
        root = Path(directory)
        app_dir = root / LEGACY_APP_DIR
        if not app_dir.exists():
            continue
        if app_dir.is_symlink():
            raise LegacyMigrationError(f"legacy app path is a symlink: {app_dir}")
        marker = app_dir / LEGACY_MARKER
        if marker.exists():
            if marker.is_symlink():
                raise LegacyMigrationError(f"legacy marker is a symlink: {marker}")
            try:
                data = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise LegacyMigrationError(f"unreadable legacy marker {marker}: {exc}") from exc
            if not isinstance(data, dict) or data.get("project_id") != project_id:
                raise LegacyMigrationError(f"legacy marker does not match project: {marker}")
            marker.unlink()
        for parent_name in ("runs", "sessions"):
            parent = app_dir / parent_name
            if not parent.is_dir() or parent.is_symlink():
                continue
            for run_id in run_ids:
                target = parent / run_id
                if target.is_symlink():
                    target.unlink()
                elif target.is_dir():
                    shutil.rmtree(target)
            _remove_if_empty(parent)
        _remove_if_empty(app_dir)

    legacy_root = source_db.parent
    legacy_runs = legacy_root / "runs"
    if legacy_runs.is_dir() and not legacy_runs.is_symlink():
        for run_id in run_ids:
            target = legacy_runs / run_id
            if target.is_symlink():
                target.unlink()
            elif target.is_dir():
                shutil.rmtree(target)
        _remove_if_empty(legacy_runs)
    if not manifest.get("in_place") and legacy_root.name == LEGACY_APP_DIR and source_db.exists():
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(source_db) + suffix)
            if candidate.exists() and not candidate.is_symlink():
                candidate.unlink()
        _remove_if_empty(legacy_root)


def _remove_if_empty(path: Path) -> None:
    try:
        if path.is_dir() and not path.is_symlink() and not any(path.iterdir()):
            path.rmdir()
    except OSError:
        pass

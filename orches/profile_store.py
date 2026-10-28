"""User-scoped persistent pipeline profiles."""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Mapping
class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


from .pipeline_profiles import PipelineProfile, profile_from_dict

SCHEMA_VERSION = 1


class ProfileStoreError(RuntimeError):
    pass


class ProfileNotFoundError(ProfileStoreError):
    pass


class ProfileConflictError(ProfileStoreError):
    pass


class ProfileStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, factory=_ClosingConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _initialize(self) -> None:
        purged = False
        try:
            with self._connect() as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS profiles (
                        id TEXT PRIMARY KEY,
                        profile_json TEXT NOT NULL,
                        revision INTEGER NOT NULL
                    );
                    """
                )
                version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                if version >= SCHEMA_VERSION:
                    return
                conn.execute("DROP TABLE IF EXISTS metadata")
                purged = self._discard_legacy_profiles(conn)
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        except (sqlite3.DatabaseError, ValueError, OSError) as exc:
            raise ProfileStoreError(f"unable to initialize profile store {self.path}: {exc}") from exc
        if purged:
            with self._connect() as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.execute("VACUUM")

    @staticmethod
    def _discard_legacy_profiles(conn: sqlite3.Connection) -> bool:
        """Remove profiles written by the previous fixed-role product."""
        removed = False
        for row in conn.execute("SELECT id,profile_json FROM profiles").fetchall():
            try:
                definition = json.loads(row["profile_json"])
            except json.JSONDecodeError as exc:
                raise ValueError(f"profile {row['id']} is not valid JSON: {exc}") from exc
            if not isinstance(definition, dict):
                raise ValueError(f"profile {row['id']} is not an object")
            kind = definition.get("kind")
            if kind is not None and kind != "generic":
                raise ValueError(f"profile {row['id']} has unknown kind {kind!r}")
            if kind != "generic":
                conn.execute("DELETE FROM profiles WHERE id=?", (row["id"],))
                removed = True
                continue
            normalized = {key: value for key, value in definition.items() if key != "kind"}
            conn.execute(
                "UPDATE profiles SET profile_json=? WHERE id=?",
                (json.dumps(normalized, sort_keys=True), row["id"]),
            )
        return removed

    @staticmethod
    def _record(row: sqlite3.Row) -> dict[str, Any]:
        stored = json.loads(row["profile_json"])
        # Rows written by the previous product carried a redundant kind marker.
        definition = {key: value for key, value in stored.items() if key != "kind"}
        result = profile_from_dict(definition).to_dict()
        result["revision"] = int(row["revision"])
        return result

    def list(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT id,profile_json,revision FROM profiles").fetchall()
        records = [self._record(row) for row in rows]
        return sorted(records, key=lambda item: (item.get("name") or item["id"]).casefold(),)

    def get(self, profile_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id,profile_json,revision FROM profiles WHERE id=?", (profile_id,)
            ).fetchone()
        return self._record(row) if row else None

    def get_profile(self, profile_id: str) -> PipelineProfile | None:
        record = self.get(profile_id)
        if not record:
            return None
        return profile_from_dict({key: value for key, value in record.items() if key != "revision"})

    def values(self) -> list[PipelineProfile]:
        return [
            profile_from_dict({key: value for key, value in record.items() if key != "revision"})
            for record in self.list()
        ]

    def create(self, name: str) -> dict[str, Any]:
        profile_id = f"profile_{uuid.uuid4().hex}"
        profile = PipelineProfile(profile_id, name)
        profile_from_dict(profile.to_dict())
        payload = json.dumps(profile.to_dict(), sort_keys=True)
        with self._connect() as conn:
            try:
                conn.execute("INSERT INTO profiles(id,profile_json,revision) VALUES(?,?,1)", (profile_id, payload))
            except sqlite3.IntegrityError as exc:
                raise ProfileConflictError("profile id already exists") from exc
        return self.get(profile_id)  # type: ignore[return-value]

    def update(self, profile_id: str, definition: Mapping[str, Any], revision: int) -> dict[str, Any]:
        current = self.get(profile_id)
        if current is None:
            raise ProfileNotFoundError(profile_id)
        if definition.get("id") != profile_id:
            raise ValueError("profile id is immutable")
        profile = profile_from_dict(definition)
        payload = json.dumps(profile.to_dict(), sort_keys=True)
        with self._connect() as conn:
            result = conn.execute(
                "UPDATE profiles SET profile_json=?,revision=revision+1 WHERE id=? AND revision=?",
                (payload, profile_id, revision),
            )
            if result.rowcount != 1:
                raise ProfileConflictError("profile revision is stale")
        return self.get(profile_id)  # type: ignore[return-value]

    def delete(self, profile_id: str) -> bool:
        with self._connect() as conn:
            result = conn.execute("DELETE FROM profiles WHERE id=?", (profile_id,))
        return result.rowcount == 1

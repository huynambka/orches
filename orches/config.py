"""TOML configuration for Orches."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .roles import role_configs_with_overrides

CONFIG_NAME = "orches.toml"
LEGACY_CONFIG_NAME = "cve-forge.toml"
APP_DIR = ".orches"
LEGACY_APP_DIR = ".cve-forge"
_RUNTIME_KEYS = {
    "state_db", "runs_root", "profiles_db", "max_prompt_bytes", "max_output_bytes",
}


def default_profiles_db() -> Path:
    return Path.home() / ".orches" / "profiles.sqlite3"


@dataclass(frozen=True, slots=True)
class AppConfig:
    root: Path
    state_db: Path
    runs_root: Path
    omp_bin: str = "omp"
    max_prompt_bytes: int = 300_000
    max_output_bytes: int = 64_000_000
    role_configs: dict = None  # type: ignore[assignment]
    profiles_db: Path = field(default_factory=default_profiles_db)
    # Only true for the default state database, so an explicitly configured store is
    # never mixed with files from a previous installation.
    migrate_legacy_default: bool = False

    def __post_init__(self) -> None:
        if self.role_configs is None:
            object.__setattr__(self, "role_configs", role_configs_with_overrides())

    @property
    def legacy_state_db(self) -> Path | None:
        return self.root / LEGACY_APP_DIR / "state.sqlite3" if self.migrate_legacy_default else None


def load_config(root: str | Path, path: str | Path | None = None) -> AppConfig:
    root_path = Path(root).resolve()
    config_path = Path(path).resolve() if path else root_path / CONFIG_NAME
    raw: dict[str, Any] = {}
    if config_path.exists():
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)
    elif path is None and (root_path / LEGACY_CONFIG_NAME).exists():
        raise ValueError(
            f"{LEGACY_CONFIG_NAME} is no longer supported; rename it to {CONFIG_NAME} and remove "
            "CVE-specific role and pipeline_profiles sections"
        )
    unsupported = set(raw) - {"runtime", "omp", "roles"}
    if unsupported:
        raise ValueError(f"unsupported configuration sections: {sorted(unsupported)}")
    runtime = raw.get("runtime", {})
    unknown_runtime = set(runtime) - _RUNTIME_KEYS
    if unknown_runtime:
        raise ValueError(f"unsupported [runtime] keys: {sorted(unknown_runtime)}")
    omp = raw.get("omp", {})
    roles = raw.get("roles", {})
    state_db = Path(runtime.get("state_db", f"{APP_DIR}/state.sqlite3"))
    runs_root = Path(runtime.get("runs_root", f"{APP_DIR}/runs"))
    profiles_db = Path(runtime.get("profiles_db", str(default_profiles_db())))
    if not state_db.is_absolute():
        state_db = root_path / state_db
    if not runs_root.is_absolute():
        runs_root = root_path / runs_root
    if not profiles_db.is_absolute():
        profiles_db = root_path / profiles_db
    return AppConfig(
        root=root_path,
        state_db=state_db,
        runs_root=runs_root,
        omp_bin=str(omp.get("binary", "omp")),
        max_prompt_bytes=int(runtime.get("max_prompt_bytes", 300_000)),
        max_output_bytes=int(runtime.get("max_output_bytes", 64_000_000)),
        role_configs=role_configs_with_overrides(roles),
        profiles_db=profiles_db,
        migrate_legacy_default="state_db" not in runtime,
    )

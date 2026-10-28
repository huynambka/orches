"""Atomic run artifact storage with secret redaction and hashing."""

from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import Path
from typing import Iterable

from .models import ArtifactRef, utc_now
from .state import ArtifactConflictError, StateStore


_DEFAULT_SECRET_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)[^\s,;]+"),
    re.compile(r"(?i)(password\s*[=:]\s*)[^\s,;]+"),
    re.compile(r"-----BEGIN [A-Z ]+ PRIVATE KEY-----.*?-----END [A-Z ]+ PRIVATE KEY-----", re.S),
)


class ArtifactStore:
    def __init__(
        self,
        root: str | Path,
        state: StateStore,
        secret_patterns: Iterable[re.Pattern[str]] = _DEFAULT_SECRET_PATTERNS,
    ) -> None:
        self.root = Path(root).resolve()
        self.state = state
        self.root.mkdir(parents=True, exist_ok=True)
        self.secret_patterns = tuple(secret_patterns)

    def redact_text(self, text: str) -> str:
        result = text
        for pattern in self.secret_patterns:
            result = pattern.sub(lambda match: f"{match.group(1)}<REDACTED>" if match.lastindex else "<REDACTED>", result)
        return result

    def _target(self, run_id: str, relative_path: str) -> tuple[Path, str]:
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("artifact path must stay below the run directory")
        run_root = (self.root / run_id).resolve()
        target = (run_root / relative).resolve()
        if run_root != target and run_root not in target.parents:
            raise ValueError("artifact path escaped run directory")
        return target, target.relative_to(self.root).as_posix()

    def _write(self, run_id: str, relative_path: str, data: bytes, media_type: str) -> ArtifactRef:
        target, stored_path = self._target(run_id, relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(data).hexdigest()
        if target.exists():
            existing = hashlib.sha256(target.read_bytes()).hexdigest()
            if existing != digest:
                raise ArtifactConflictError(f"artifact path already exists: {relative_path}")
        else:
            temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            temporary.write_bytes(data)
            temporary.replace(target)
        artifact = ArtifactRef(
            artifact_id=uuid.uuid4().hex,
            run_id=run_id,
            relative_path=stored_path,
            sha256=digest,
            size=len(data),
            media_type=media_type,
            created_at=utc_now(),
        )
        self.state.save_artifact(artifact)
        return artifact

    def write_text(
        self,
        run_id: str,
        relative_path: str,
        text: str,
        media_type: str = "text/plain",
        redact: bool = True,
    ) -> ArtifactRef:
        safe_text = self.redact_text(text) if redact else text
        return self._write(run_id, relative_path, safe_text.encode("utf-8"), media_type)

    def write_bytes(
        self,
        run_id: str,
        relative_path: str,
        data: bytes,
        media_type: str = "application/octet-stream",
    ) -> ArtifactRef:
        return self._write(run_id, relative_path, data, media_type)

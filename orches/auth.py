"""Single-admin password sessions for the Orches API.

The admin password arrives via the ORCHES_ADMIN_PASSWORD environment variable;
only a salted PBKDF2 verifier is kept in memory. Access tokens are opaque
random strings; the store keeps SHA-256 digests only, so neither the password
nor a usable token can be read back out of the process state.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from typing import Callable

MIN_PASSWORD_CHARS = 16
TOKEN_TTL_SECONDS = 12 * 60 * 60
MAX_SESSIONS = 128
MAX_LOGIN_FAILURES = 5
FAILURE_WINDOW_SECONDS = 60.0
_PBKDF2_ITERATIONS = 310_000


class WeakPasswordError(ValueError):
    """The configured admin password is missing or too short."""


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AdminSessions:
    """In-process bearer-token session store for one admin password."""

    def __init__(self, password: str, clock: Callable[[], float] = time.monotonic) -> None:
        if not isinstance(password, str) or len(password) < MIN_PASSWORD_CHARS:
            raise WeakPasswordError(
                f"ORCHES_ADMIN_PASSWORD must be at least {MIN_PASSWORD_CHARS} characters"
            )
        self._clock = clock
        self._salt = secrets.token_bytes(16)
        self._verifier = self._derive(password)
        self._lock = threading.Lock()
        # token digest -> absolute (monotonic) expiry
        self._sessions: dict[str, float] = {}
        # peer ip -> timestamps of recent failed logins
        self._failures: dict[str, list[float]] = {}

    def _derive(self, password: str) -> bytes:
        return hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), self._salt, _PBKDF2_ITERATIONS
        )

    def _prune(self, now: float) -> None:
        expired = [digest for digest, expiry in self._sessions.items() if expiry <= now]
        for digest in expired:
            del self._sessions[digest]

    def login_blocked(self, peer: str) -> bool:
        """True while the peer is inside the failed-login cooldown window."""
        now = self._clock()
        with self._lock:
            recent = [t for t in self._failures.get(peer, []) if now - t < FAILURE_WINDOW_SECONDS]
            if recent:
                self._failures[peer] = recent
            else:
                self._failures.pop(peer, None)
            return len(recent) >= MAX_LOGIN_FAILURES

    def login(self, password: str, peer: str) -> str | None:
        """Return a new access token, or None for wrong credentials."""
        candidate = self._derive(password if isinstance(password, str) else "")
        now = self._clock()
        if not hmac.compare_digest(candidate, self._verifier):
            with self._lock:
                self._failures.setdefault(peer, []).append(now)
            return None
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._failures.pop(peer, None)
            self._prune(now)
            while len(self._sessions) >= MAX_SESSIONS:
                oldest = min(self._sessions, key=self._sessions.__getitem__)
                del self._sessions[oldest]
            self._sessions[_digest(token)] = now + TOKEN_TTL_SECONDS
        return token

    def authenticate(self, authorization: str | None) -> bool:
        """Validate an exact ``Bearer <token>`` Authorization header value."""
        if not authorization:
            return False
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token or " " in token:
            return False
        now = self._clock()
        with self._lock:
            self._prune(now)
            return _digest(token) in self._sessions

    def revoke(self, authorization: str | None) -> bool:
        """Revoke the session named by the Authorization header, if any."""
        if not authorization:
            return False
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return False
        with self._lock:
            return self._sessions.pop(_digest(token), None) is not None

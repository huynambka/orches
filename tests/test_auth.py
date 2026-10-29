"""Single-admin authentication: session store, API boundary, env scrubbing."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orches.auth import (
    FAILURE_WINDOW_SECONDS, MAX_LOGIN_FAILURES, MAX_SESSIONS, TOKEN_TTL_SECONDS,
    AdminSessions, WeakPasswordError,
)

TEST_PASSWORD = "orches-test-admin-password"


def install_admin_password(case: unittest.TestCase, password: str = TEST_PASSWORD) -> None:
    """Set ORCHES_ADMIN_PASSWORD for the test and restore the old value after."""
    previous = os.environ.get("ORCHES_ADMIN_PASSWORD")

    def restore() -> None:
        if previous is None:
            os.environ.pop("ORCHES_ADMIN_PASSWORD", None)
        else:
            os.environ["ORCHES_ADMIN_PASSWORD"] = previous

    os.environ["ORCHES_ADMIN_PASSWORD"] = password
    case.addCleanup(restore)


def login(client) -> str:
    """Authenticate a TestClient through the real login route, not a bypass."""
    response = client.post("/api/auth/login", json={"password": TEST_PASSWORD})
    assert response.status_code == 200, response.text
    token = response.json()["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return token


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class AdminSessionsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = _Clock()
        self.sessions = AdminSessions(TEST_PASSWORD, clock=self.clock)

    def test_weak_password_rejected(self) -> None:
        for bad in ("", "short", "a" * 15):
            with self.assertRaises(WeakPasswordError):
                AdminSessions(bad)

    def test_login_and_authenticate(self) -> None:
        token = self.sessions.login(TEST_PASSWORD, "1.1.1.1")
        self.assertIsNotNone(token)
        self.assertTrue(self.sessions.authenticate(f"Bearer {token}"))
        self.assertFalse(self.sessions.authenticate(f"bearer-x {token}"))
        self.assertFalse(self.sessions.authenticate(token))  # missing scheme
        self.assertFalse(self.sessions.authenticate(None))
        self.assertFalse(self.sessions.authenticate("Bearer wrong"))

    def test_wrong_password_returns_none(self) -> None:
        self.assertIsNone(self.sessions.login("not-the-password!!", "1.1.1.1"))

    def test_token_expiry(self) -> None:
        token = self.sessions.login(TEST_PASSWORD, "1.1.1.1")
        self.clock.now += TOKEN_TTL_SECONDS - 1
        self.assertTrue(self.sessions.authenticate(f"Bearer {token}"))
        self.clock.now += 2
        self.assertFalse(self.sessions.authenticate(f"Bearer {token}"))

    def test_revoke(self) -> None:
        token = self.sessions.login(TEST_PASSWORD, "1.1.1.1")
        self.assertTrue(self.sessions.revoke(f"Bearer {token}"))
        self.assertFalse(self.sessions.authenticate(f"Bearer {token}"))
        self.assertFalse(self.sessions.revoke(f"Bearer {token}"))

    def test_rate_limit_per_peer(self) -> None:
        for _ in range(MAX_LOGIN_FAILURES):
            self.assertFalse(self.sessions.login_blocked("9.9.9.9"))
            self.sessions.login("wrong-password-wrong", "9.9.9.9")
        self.assertTrue(self.sessions.login_blocked("9.9.9.9"))
        # Another peer is unaffected and can still log in.
        self.assertFalse(self.sessions.login_blocked("8.8.8.8"))
        self.assertIsNotNone(self.sessions.login(TEST_PASSWORD, "8.8.8.8"))
        # The window elapses and the blocked peer recovers.
        self.clock.now += FAILURE_WINDOW_SECONDS + 1
        self.assertFalse(self.sessions.login_blocked("9.9.9.9"))

    def test_success_resets_failures(self) -> None:
        for _ in range(MAX_LOGIN_FAILURES - 1):
            self.sessions.login("wrong-password-wrong", "7.7.7.7")
        self.assertIsNotNone(self.sessions.login(TEST_PASSWORD, "7.7.7.7"))
        self.assertFalse(self.sessions.login_blocked("7.7.7.7"))

    def test_session_cap_evicts_oldest(self) -> None:
        first = self.sessions.login(TEST_PASSWORD, "1.1.1.1")
        for _ in range(MAX_SESSIONS):
            self.clock.now += 1
            self.sessions.login(TEST_PASSWORD, "1.1.1.1")
        self.assertFalse(self.sessions.authenticate(f"Bearer {first}"))


class AuthApiTest(unittest.TestCase):
    def setUp(self) -> None:
        try:
            from fastapi.testclient import TestClient
        except ImportError:  # pragma: no cover - optional web extra
            self.skipTest("fastapi test client is not installed")
        from orches.api import create_app
        from orches.config import AppConfig
        from orches.state import StateStore

        install_admin_password(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.config = AppConfig(
            root=root, state_db=root / "state.sqlite3", runs_root=root / "runs",
            profiles_db=root / "profiles.sqlite3",
        )
        self.state = StateStore(self.config.state_db)
        self.connection = self.state.create_connection(name="box", os="linux", cred="secret")
        self.client = TestClient(create_app(self.config))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_missing_password_fails_startup(self) -> None:
        from orches.api import create_app
        os.environ.pop("ORCHES_ADMIN_PASSWORD", None)
        with self.assertRaises(WeakPasswordError):
            create_app(self.config)
        os.environ["ORCHES_ADMIN_PASSWORD"] = TEST_PASSWORD

    def test_unauthenticated_requests_denied(self) -> None:
        reveal = f"/api/connections/{self.connection['connection_id']}/cred"
        for method, path in [
            ("GET", "/api/projects"),
            ("GET", "/api/costs"),
            ("GET", "/api/connections"),
            ("GET", reveal),
            ("GET", "/api/mcp-servers"),
            ("GET", "/api/auth/session"),
            ("POST", "/api/auth/logout"),
            ("GET", "/openapi.json"),
            ("GET", "/docs"),
        ]:
            response = self.client.request(method, path)
            self.assertEqual(response.status_code, 401, f"{method} {path}: {response.status_code}")
            self.assertEqual(response.json()["detail"], "authentication required")
        created = self.client.post("/api/projects", json={"name": "x", "project_directory": self.tmp.name})
        self.assertEqual(created.status_code, 401)
        self.assertEqual(self.state.list_projects(), [])  # no side effects

    def test_health_is_public_and_identifies_auth(self) -> None:
        payload = self.client.get("/api/health").json()
        self.assertEqual(payload, {"status": "ok", "service": "orches", "authentication": "required"})

    def test_login_contract(self) -> None:
        bad = self.client.post("/api/auth/login", json={"password": "wrong-password!"})
        self.assertEqual(bad.status_code, 401)
        self.assertEqual(bad.json()["detail"], "invalid credentials")
        self.assertEqual(bad.headers.get("cache-control"), "no-store")
        self.assertNotIn("wrong-password!", bad.text)

        malformed = self.client.post("/api/auth/login", json={"password": 5})
        self.assertEqual(malformed.status_code, 400)
        extra = self.client.post("/api/auth/login", json={"password": "x" * 16, "user": "a"})
        self.assertEqual(extra.status_code, 400)
        huge = self.client.post(
            "/api/auth/login", content=b"{" + b" " * 5000 + b"}",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(huge.status_code, 413)

        good = self.client.post("/api/auth/login", json={"password": TEST_PASSWORD})
        self.assertEqual(good.status_code, 200)
        body = good.json()
        self.assertEqual(body["token_type"], "bearer")
        self.assertEqual(body["expires_in"], TOKEN_TTL_SECONDS)
        self.assertGreater(len(body["access_token"]), 30)
        self.assertEqual(good.headers.get("cache-control"), "no-store")

    def test_rate_limit_responds_429(self) -> None:
        for _ in range(MAX_LOGIN_FAILURES):
            self.assertEqual(
                self.client.post("/api/auth/login", json={"password": "wrong-password!"}).status_code,
                401,
            )
        blocked = self.client.post("/api/auth/login", json={"password": TEST_PASSWORD})
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(blocked.json()["detail"], "too many login attempts")

    def test_authenticated_flow_and_logout(self) -> None:
        token = login(self.client)
        self.assertEqual(self.client.get("/api/projects").status_code, 200)
        self.assertEqual(self.client.get("/api/auth/session").json(), {"authenticated": True})
        reveal = self.client.get(f"/api/connections/{self.connection['connection_id']}/cred")
        self.assertEqual(reveal.json(), {"cred": "secret"})
        created = self.client.post(
            "/api/projects", json={"name": "p", "project_directory": self.tmp.name},
        )
        self.assertEqual(created.status_code, 201)

        self.assertEqual(self.client.post("/api/auth/logout").json(), {"logged_out": True})
        self.assertEqual(self.client.get("/api/projects").status_code, 401)
        self.assertEqual(
            self.client.get("/api/projects", headers={"Authorization": f"Bearer {token}"}).status_code,
            401,
        )

    def test_malformed_tokens_denied(self) -> None:
        for header in ("Bearer", "Bearer  ", "Token abc", "Bearer a b", "Bearer " + "x" * 43):
            response = self.client.get("/api/projects", headers={"Authorization": header})
            self.assertEqual(response.status_code, 401, header)

    def test_cors_preflight_for_configured_origin(self) -> None:
        response = self.client.options(
            "/api/projects",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "Authorization",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["access-control-allow-origin"], "http://localhost:3000")
        self.assertIn("authorization", response.headers["access-control-allow-headers"].lower())

        denied = self.client.options(
            "/api/projects",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "GET",
            },
        )
        self.assertNotEqual(denied.status_code, 200)
        self.assertNotIn("access-control-allow-origin", denied.headers)

    def test_unauthenticated_401_carries_cors_header(self) -> None:
        response = self.client.get("/api/projects", headers={"Origin": "http://localhost:3000"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers.get("access-control-allow-origin"), "http://localhost:3000")


class SecretScrubTest(unittest.TestCase):
    """The admin password must never reach child process environments."""

    def setUp(self) -> None:
        install_admin_password(self)

    def test_start_session_env_scrubbed(self) -> None:
        from orches.omp import OmpClient

        client = OmpClient(omp_bin=sys.executable)
        captured: dict = {}

        async def fake_exec(*args, **kwargs):
            captured.update(kwargs)
            raise RuntimeError("stop before spawning")

        with mock.patch("asyncio.create_subprocess_exec", fake_exec):
            with self.assertRaises(RuntimeError):
                asyncio.run(client.start_session("hi", extra_env={"KEEP": "1"}))
        self.assertNotIn("ORCHES_ADMIN_PASSWORD", captured["env"])
        self.assertEqual(captured["env"]["KEEP"], "1")

    def test_probe_mcp_env_scrubbed(self) -> None:
        from orches.omp import OmpClient

        client = OmpClient(omp_bin=sys.executable)
        captured: dict = {}

        async def fake_exec(*args, **kwargs):
            captured.update(kwargs)
            raise RuntimeError("stop before spawning")

        with mock.patch("asyncio.create_subprocess_exec", fake_exec):
            result = asyncio.run(client.probe_mcp(Path(tempfile.gettempdir()), "fs", timeout=5))
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("ORCHES_ADMIN_PASSWORD", captured["env"])

    def test_omp_models_env_scrubbed(self) -> None:
        try:
            from fastapi.testclient import TestClient
        except ImportError:  # pragma: no cover - optional web extra
            self.skipTest("fastapi test client is not installed")
        from orches.api import create_app
        from orches.config import AppConfig

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        config = AppConfig(
            root=root, state_db=root / "state.sqlite3", runs_root=root / "runs",
            profiles_db=root / "profiles.sqlite3",
        )
        captured: dict = {}

        class FakeProcess:
            returncode = 0

            class stdout:  # noqa: D106 - minimal file-like stub
                @staticmethod
                def read(_n: int) -> bytes:
                    return b'{"models": []}'

            @staticmethod
            def wait(timeout: float | None = None) -> int:
                return 0

        def fake_popen(cmd, **kwargs):
            captured.update(kwargs)
            return FakeProcess()

        client = TestClient(create_app(config))
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        login(client)
        with mock.patch("orches.api.subprocess.Popen", fake_popen):
            response = client.get("/api/omp-models")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("env", captured)
        self.assertNotIn("ORCHES_ADMIN_PASSWORD", captured["env"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend import database
from backend.api import app


def _basic_header(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


class ProductionAuthenticationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.previous_db = database.DB_PATH
        database.DB_PATH = Path(cls.temp.name) / "auth-test.db"
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_context.__exit__(None, None, None)
        database.DB_PATH = cls.previous_db
        cls.temp.cleanup()

    @staticmethod
    def _auth_environment() -> dict[str, str]:
        return {
            "OPINION_MONITOR_AUTH_MODE": "basic",
            "OPINION_MONITOR_BASIC_USERS": json.dumps(
                {
                    "analyst": {"password": "研究-pass", "role": "researcher"},
                    "admin": {"password": "core:pass", "role": "core"},
                }
            ),
        }

    def test_01_default_demo_mode_remains_compatible(self):
        with patch.dict(os.environ, {"OPINION_MONITOR_AUTH_MODE": "off"}, clear=False):
            response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["authentication"]["enabled"])

    def test_02_basic_mode_challenges_missing_or_bad_credentials(self):
        with patch.dict(os.environ, self._auth_environment(), clear=False):
            missing = self.client.get("/api/health", headers={"Origin": "http://localhost:5173"})
            invalid = self.client.get("/api/health", headers=_basic_header("analyst", "wrong"))
        self.assertEqual(missing.status_code, 401)
        self.assertIn("Basic", missing.headers.get("www-authenticate", ""))
        self.assertEqual(missing.headers.get("access-control-allow-origin"), "http://localhost:5173")
        self.assertEqual(invalid.status_code, 401)

    def test_03_authenticated_role_cannot_be_elevated_by_request_input(self):
        environment = self._auth_environment()
        analyst = _basic_header("analyst", "研究-pass")
        admin = _basic_header("admin", "core:pass")
        with patch.dict(os.environ, environment, clear=False):
            visible = self.client.get("/api/overview?role=researcher", headers=analyst)
            query_elevation = self.client.get("/api/overview?role=core", headers=analyst)
            body_elevation = self.client.post(
                "/api/knowledge/search",
                headers=analyst,
                json={"query": "来源", "role": "core"},
            )
            implicit_core_write = self.client.post("/api/public-demo/refresh", headers=analyst, json={})
            core_access = self.client.get("/api/overview?role=core", headers=admin)
        self.assertEqual(visible.status_code, 200)
        self.assertEqual(query_elevation.status_code, 403)
        self.assertEqual(body_elevation.status_code, 403)
        self.assertEqual(implicit_core_write.status_code, 403)
        self.assertEqual(core_access.status_code, 200)

    def test_04_inspecting_role_does_not_consume_json_request_body(self):
        with patch.dict(os.environ, self._auth_environment(), clear=False):
            response = self.client.post(
                "/api/knowledge/search",
                headers=_basic_header("analyst", "研究-pass"),
                json={"query": "来源", "role": "researcher", "top_k": 2},
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("results", response.json())

    def test_05_invalid_enabled_configuration_fails_closed(self):
        with patch.dict(
            os.environ,
            {"OPINION_MONITOR_AUTH_MODE": "basic", "OPINION_MONITOR_BASIC_USERS": "not-json"},
            clear=False,
        ):
            response = self.client.get("/api/health", headers=_basic_header("admin", "anything"))
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("not-json", response.text)

    def test_06_researcher_cannot_read_audit_block_details(self):
        environment = self._auth_environment()
        analyst = _basic_header("analyst", "研究-pass")
        admin = _basic_header("admin", "core:pass")
        with patch.dict(os.environ, environment, clear=False):
            created = self.client.post(
                "/api/collection/tasks",
                headers=admin,
                json={
                    "name": "认证审计测试",
                    "dimension": "keyword",
                    "target_value": "audit-boundary",
                    "connector_id": "x",
                    "frequency": "daily",
                    "history_days": 1,
                    "media_types": ["text"],
                    "languages": ["en"],
                    "role": "core",
                },
            )
            researcher_blocks = self.client.get("/api/audit/blocks?role=researcher", headers=analyst)
            core_blocks = self.client.get("/api/audit/blocks?role=core", headers=admin)
        self.assertEqual(created.status_code, 200)
        self.assertTrue(researcher_blocks.json()["details_redacted"])
        self.assertGreater(len(researcher_blocks.json()["blocks"]), 0)
        self.assertTrue(all(block["actor"] == "已隐藏" for block in researcher_blocks.json()["blocks"]))
        self.assertTrue(all(block["object_id"] == "已隐藏" for block in researcher_blocks.json()["blocks"]))
        self.assertTrue(all("仅限核心课题组" in block["detail"] for block in researcher_blocks.json()["blocks"]))
        self.assertFalse(core_blocks.json()["details_redacted"])
        self.assertTrue(any(block["actor"] != "已隐藏" for block in core_blocks.json()["blocks"]))


if __name__ == "__main__":
    unittest.main()

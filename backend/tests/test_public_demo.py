from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from backend import database
from backend.api import app
from backend.ingest import index_integrity
from backend.public_demo import PUBLIC_BATCH_CODE, import_public_demo


class PublicWebDemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.previous_db = database.DB_PATH
        database.DB_PATH = Path(cls.temp.name) / "public-demo.db"
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()
        cls.batch = import_public_demo(db_path=database.DB_PATH)

    @classmethod
    def tearDownClass(cls):
        cls.client_context.__exit__(None, None, None)
        database.DB_PATH = cls.previous_db
        cls.temp.cleanup()

    def test_import_is_indexed_and_idempotent(self):
        again = import_public_demo(db_path=database.DB_PATH)
        self.assertEqual(self.batch["code"], PUBLIC_BATCH_CODE)
        self.assertGreaterEqual(self.batch["record_count"], 27)
        self.assertEqual(again["record_count"], self.batch["record_count"])
        with database.db() as conn:
            batch = conn.execute("SELECT id FROM dataset_batch WHERE code=?", (PUBLIC_BATCH_CODE,)).fetchone()
            self.assertIsNotNone(batch)
            state = index_integrity(conn, int(batch["id"]))
        self.assertTrue(state["complete"], state["issues"])

    def test_status_topics_and_evidence_detail(self):
        status = self.client.get("/api/public-demo/status?role=researcher")
        self.assertEqual(status.status_code, 200)
        self.assertTrue(status.json()["available"])
        self.assertTrue(status.json()["real_public_data"])
        self.assertEqual(status.json()["record_count"], self.batch["record_count"])
        self.assertEqual(status.json()["collection_summary"]["channels_checked"], 12)
        self.assertEqual(len(status.json()["platform_access_observations"]), 12)

        topics = self.client.get("/api/public-demo/topics?role=researcher")
        self.assertEqual(topics.status_code, 200)
        items = {item["slug"]: item for item in topics.json()["items"]}
        self.assertEqual(set(items), {"xiongan", "apec", "xi-overseas"})
        self.assertEqual(items["xiongan"]["count"], 9)
        self.assertEqual(items["apec"]["count"], 9)
        self.assertGreaterEqual(items["xi-overseas"]["count"], 9)

        detail = self.client.get("/api/public-demo/topics/apec?role=researcher")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["count"], 9)
        first = detail.json()["items"][0]
        self.assertTrue(first["record_id"].startswith(f"{PUBLIC_BATCH_CODE}:content:"))
        self.assertTrue(first["original_url"].startswith("https://"))
        self.assertEqual(first["sentiment"], "NOT_RUN")
        self.assertEqual(first["translation_status"], "NOT_CONFIGURED")

        record = self.client.get(f"/api/records/{first['record_id']}?role=researcher")
        self.assertEqual(record.status_code, 200)
        self.assertEqual(record.json()["content"]["demo_label"], "公开网页试采样本")
        self.assertEqual(record.json()["content"]["original_url"], first["original_url"])

        overview = self.client.get("/api/overview?role=researcher").json()
        self.assertEqual(overview["mode"], "PUBLIC_WEB_SAMPLE")
        self.assertEqual(overview["batch"]["code"], PUBLIC_BATCH_CODE)
        self.assertEqual(overview["metrics"]["records"], self.batch["record_count"])

    def test_current_batch_isolation_covers_search_knowledge_alerts_and_reports(self):
        search = self.client.post(
            "/api/knowledge/search",
            json={"query": "习近平 海外 访问", "role": "researcher", "top_k": 6},
        )
        self.assertEqual(search.status_code, 200)
        self.assertGreater(search.json()["result_count"], 0)
        self.assertTrue(all(item["batch_code"] == PUBLIC_BATCH_CODE for item in search.json()["results"]))

        collections = self.client.get("/api/knowledge/collections?role=researcher").json()["items"]
        self.assertEqual(len(collections), 1)
        self.assertTrue(collections[0]["version_id"].startswith("kb-public-web-demo:"))

        alerts = self.client.get("/api/alerts?role=researcher").json()
        self.assertEqual(alerts["mode"], "PUBLIC_WEB_SAMPLE")
        self.assertEqual(alerts["count"], 0)

        report = self.client.post(
            "/api/reports/generate",
            json={"template": "topic", "focus": "APEC 2026", "role": "researcher"},
        ).json()
        self.assertEqual(report["status"], "GENERATED_FROM_PUBLIC_WEB_SAMPLE")
        self.assertTrue(all(item["batch_code"] == PUBLIC_BATCH_CODE for item in report["citations"]))

    def test_refresh_is_core_only(self):
        denied = self.client.post("/api/public-demo/refresh", json={"role": "researcher"})
        self.assertEqual(denied.status_code, 403)
        omitted = self.client.post("/api/public-demo/refresh", json={})
        self.assertEqual(omitted.status_code, 403)
        refreshed = self.client.post("/api/public-demo/refresh", json={"role": "core"})
        self.assertEqual(refreshed.status_code, 200)
        self.assertEqual(refreshed.json()["batch"]["record_count"], self.batch["record_count"])

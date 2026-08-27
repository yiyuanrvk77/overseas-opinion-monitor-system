from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from backend import database
from backend.api import app
from backend.analysis_workflow import _rule_candidate
from backend.public_demo import import_public_demo


def basic(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


class AnalysisWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.previous_db = database.DB_PATH
        database.DB_PATH = Path(cls.temp.name) / "analysis.db"
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()
        cls.batch = import_public_demo(db_path=database.DB_PATH)

    @classmethod
    def tearDownClass(cls):
        cls.client_context.__exit__(None, None, None)
        database.DB_PATH = cls.previous_db
        cls.temp.cleanup()

    def setUp(self):
        with database.db() as conn:
            conn.execute("DELETE FROM human_review")
            conn.execute("DELETE FROM machine_analysis")
            conn.execute("DELETE FROM analysis_run")

    def test_machine_rule_baseline_and_idempotency(self):
        first = self.client.post("/api/analysis/runs", json={"topic": "APEC 2026", "role": "core"})
        self.assertEqual(first.status_code, 200, first.text)
        payload = first.json()
        self.assertEqual(payload["run"]["provider"], "RULE_BASELINE")
        self.assertEqual(payload["run"]["provider_label"], "可审计规则基线")
        self.assertEqual(payload["run"]["model"], "auditable-lexicon-v2")
        self.assertGreater(payload["created_count"], 0)
        second = self.client.post("/api/analysis/runs", json={"topic": "APEC 2026", "role": "core"})
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["created_count"], 0)
        self.assertEqual(second.json()["skipped_count"], payload["created_count"])

    def test_rule_baseline_uses_word_boundaries(self):
        candidate = _rule_candidate({
            "title": "APEC Sees 3.2% Growth in 2026 as Exports Jump, Inflation Accelerates",
            "summary": "The policy unit forecasts growth while warning about energy costs and concentrated growth risks.",
            "evidence_type": "explicit_source_text",
            "source_refs": ["https://example.test/apec"],
        })
        self.assertNotIn("war", candidate["keywords"])
        self.assertEqual(candidate["risk_level"], "LOW")

    def test_permissions_and_server_reviewer(self):
        denied = self.client.post("/api/analysis/runs", json={"role": "researcher"})
        self.assertEqual(denied.status_code, 403)
        created = self.client.post("/api/analysis/runs", json={"topic": "APEC 2026", "role": "core"})
        self.assertEqual(created.status_code, 200, created.text)
        queue = self.client.get("/api/analysis/queue?role=core").json()
        self.assertGreater(len(queue["items"]), 0)
        analysis_id = queue["items"][0]["analysis"]["id"]
        review = self.client.patch(
            f"/api/analysis/{analysis_id}/review",
            json={"decision": "HUMAN_CONFIRMED", "review_note": "已回到公开来源核验", "role": "core", "reviewer": "伪造审核人"},
        )
        self.assertEqual(review.status_code, 200, review.text)
        self.assertEqual(review.json()["review"]["reviewer"], "演示人工研判员")
        duplicate = self.client.patch(
            f"/api/analysis/{analysis_id}/review",
            json={"decision": "HUMAN_CONFIRMED", "review_note": "重复提交", "role": "core"},
        )
        self.assertEqual(duplicate.status_code, 409, duplicate.text)

    def test_all_human_decisions_and_report_exclusion(self):
        self.client.post("/api/analysis/runs", json={"topic": "APEC 2026", "role": "core"})
        queue = self.client.get("/api/analysis/queue?role=core").json()["items"]
        decisions = ["HUMAN_CONFIRMED", "HUMAN_REVISED", "HUMAN_REJECTED", "NEEDS_MORE_EVIDENCE"]
        for item, decision in zip(queue[:4], decisions):
            response = self.client.patch(
                f"/api/analysis/{item['analysis']['id']}/review",
                json={
                    "decision": decision,
                    "review_note": f"已依据公开来源完成{decision}复核",
                    "human_summary": "人工复核记录",
                    "role": "core",
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["workflow"], decision)
        refreshed_queue = self.client.get("/api/analysis/queue?role=core").json()
        self.assertEqual(len(refreshed_queue["items"]), len(queue) - 4)
        self.assertEqual(refreshed_queue["counts"]["verified_count"], 2)
        report = self.client.post("/api/reports/generate", json={"template": "topic", "role": "core"}).json()
        self.assertIn("analysis_workflow", report)
        self.assertGreaterEqual(report["analysis_workflow"]["verified_count"], 1)
        self.assertGreaterEqual(report["analysis_workflow"]["excluded_count"], 2)
        self.assertTrue(any("人工确认或修订" in section["content"] for section in report["sections"]))

    def test_record_detail_analysis_and_audit(self):
        self.client.post("/api/analysis/runs", json={"topic": "APEC 2026", "role": "core"})
        queue = self.client.get("/api/analysis/queue?role=core").json()["items"]
        record_id = queue[0]["analysis"]["record_id"]
        analysis_id = queue[0]["analysis"]["id"]
        review = self.client.patch(
            f"/api/analysis/{analysis_id}/review",
            json={
                "decision": "HUMAN_REVISED",
                "review_note": "已回到原始链接复核并修订",
                "human_summary": "人工修订后的正式摘要",
                "human_sentiment": "NEUTRAL",
                "human_risk_level": "LOW",
                "role": "core",
            },
        )
        self.assertEqual(review.status_code, 200, review.text)
        detail = self.client.get(f"/api/records/{record_id}/analysis?role=core")
        self.assertEqual(detail.status_code, 200, detail.text)
        detail_payload = detail.json()
        self.assertTrue(detail_payload["analyses"])
        self.assertIn("record", detail_payload)
        self.assertTrue(detail_payload["source_refs"])
        self.assertTrue(detail_payload["original_url"])
        self.assertTrue(detail_payload["collected_at"])
        self.assertEqual(detail_payload["current_review"]["decision"], "HUMAN_REVISED")
        self.assertEqual(detail_payload["final_conclusion"]["summary"], "人工修订后的正式摘要")
        self.assertEqual(detail_payload["current_analysis"]["workflow"], "HUMAN_REVISED")
        audit = self.client.get("/api/audit?role=core").json()
        self.assertTrue(audit["chain_verified"])
        self.assertTrue(any(item["action"] == "人工研判决定" for item in audit["items"]))

    def test_revised_decision_requires_revision_fields(self):
        self.client.post("/api/analysis/runs", json={"topic": "APEC 2026", "role": "core"})
        analysis_id = self.client.get("/api/analysis/queue?role=core").json()["items"][0]["analysis"]["id"]
        response = self.client.patch(
            f"/api/analysis/{analysis_id}/review",
            json={"decision": "HUMAN_REVISED", "review_note": "已复核", "role": "core"},
        )
        self.assertEqual(response.status_code, 422, response.text)

    def test_latest_machine_version_controls_queue_detail_and_report(self):
        self.client.post("/api/analysis/runs", json={"topic": "APEC 2026", "role": "core"})
        first_queue = self.client.get("/api/analysis/queue?role=core").json()
        target = first_queue["items"][0]["analysis"]
        confirmed = self.client.patch(
            f"/api/analysis/{target['id']}/review",
            json={"decision": "HUMAN_CONFIRMED", "review_note": "旧版本人工确认", "role": "core"},
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        with database.db() as conn:
            conn.execute(
                "UPDATE machine_analysis SET model=?,engine_version=? WHERE id=?",
                ("auditable-lexicon-v1", "auditable-lexicon-v1:overseas-opinion-analysis-v1", target["id"]),
            )

        rerun = self.client.post("/api/analysis/runs", json={"topic": "APEC 2026", "role": "core"})
        self.assertEqual(rerun.status_code, 200, rerun.text)
        self.assertEqual(rerun.json()["created_count"], 1)

        latest_queue = self.client.get("/api/analysis/queue?role=core").json()
        self.assertEqual(latest_queue["counts"]["machine_count"], first_queue["counts"]["machine_count"])
        latest_target = next(
            item for item in latest_queue["analyses"]
            if item["analysis"]["record_id"] == target["record_id"]
        )
        self.assertEqual(latest_target["analysis"]["model"], "auditable-lexicon-v2")
        self.assertEqual(latest_target["analysis"]["workflow"], "PENDING_HUMAN_REVIEW")

        detail = self.client.get(f"/api/records/{target['record_id']}/analysis?role=core").json()
        self.assertEqual(detail["current_analysis"]["model"], "auditable-lexicon-v2")
        self.assertIsNone(detail["current_review"])
        self.assertIsNone(detail["final_conclusion"])
        self.assertEqual(len(detail["analyses"]), 2)

        report = self.client.post("/api/reports/generate", json={"template": "topic", "role": "core"}).json()
        self.assertEqual(report["verified_count"], 0)


if __name__ == "__main__":
    unittest.main()

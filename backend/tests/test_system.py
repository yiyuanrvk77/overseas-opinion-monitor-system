from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from backend import database
from backend.api import app
from backend.graph import build_graph
from backend.ingest import SNAPSHOT_PATH, SOURCE_ZIP_SHA256, ingest_snapshot
from backend.retrieval import search


EXPECTED_COUNTS = {
    "account": 10,
    "actor": 14,
    "profile_signal": 10,
    "content": 32,
    "event": 15,
    "relationship_layer": 5,
    "business_signal": 8,
    "source": 12,
    "analysis": 9,
    "quality_conflict": 7,
    "production_gap": 12,
}


class ProductSystemTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.previous_db = database.DB_PATH
        database.DB_PATH = Path(cls.temp.name) / "test.db"
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_context.__exit__(None, None, None)
        database.DB_PATH = cls.previous_db
        cls.temp.cleanup()

    def test_01_ingestion_counts_and_checksum(self):
        payload = self.client.get("/api/datasets?role=core").json()["items"][0]
        self.assertEqual(payload["record_count"], 134)
        self.assertEqual(payload["category_counts"], EXPECTED_COUNTS)
        self.assertEqual(payload["checksum"], hashlib.sha256(SNAPSHOT_PATH.read_bytes()).hexdigest())
        self.assertEqual(payload["metadata"]["sourceZipSha256"], SOURCE_ZIP_SHA256)
        self.assertEqual(payload["status"], "TEST_READY")

    def test_02_overview_uses_database_values(self):
        payload = self.client.get("/api/overview?role=core").json()
        self.assertEqual(payload["metrics"]["records"], 134)
        self.assertEqual(payload["metrics"]["knowledge_chunks"], 134)
        self.assertEqual(payload["metrics"]["quality_open"], 19)
        self.assertEqual(payload["metrics"]["connectors_total"], 12)
        self.assertEqual(payload["metrics"]["connectors_ready"], 0)

    def test_03_role_filter_hides_restricted_objects(self):
        core = self.client.get("/api/targets?role=core").json()
        researcher = self.client.get("/api/targets?role=researcher").json()
        self.assertEqual(core["count"], 14)
        self.assertEqual(researcher["count"], 0)
        record_id = core["items"][0]["id"]
        response = self.client.get(f"/api/records/{record_id}?role=researcher")
        self.assertEqual(response.status_code, 404)

    def test_04_hybrid_search_returns_traceable_results(self):
        payload = self.client.post(
            "/api/knowledge/search",
            json={"query": "World Liberty Fi 加密项目", "role": "core", "top_k": 6},
        ).json()
        self.assertGreater(payload["result_count"], 0)
        self.assertEqual(payload["retrieval_mode"], "hybrid_lexical_offline_feature_vector")
        for item in payload["results"]:
            self.assertIn("record_id", item)
            self.assertIn("evidence_type", item)
            self.assertIsInstance(item["source_refs"], list)
            self.assertEqual(len(item["content_hash"]), 64)

    def test_05_all_graph_views_are_referentially_valid(self):
        for view in ("actors", "events", "propagation", "evidence"):
            payload = build_graph(view, role="core")
            node_ids = {node["id"] for node in payload["nodes"]}
            self.assertTrue(payload["directed"])
            self.assertGreater(len(node_ids), 0)
            self.assertTrue(all(edge["source"] in node_ids and edge["target"] in node_ids for edge in payload["edges"]))

    def test_06_connector_task_stays_draft_and_persists(self):
        created = self.client.post(
            "/api/collection/tasks",
            json={
                "name": "涉华关键词采集草稿",
                "dimension": "keyword",
                "target_value": "China policy",
                "connector_id": "x",
                "frequency": "1h",
                "history_days": 90,
                "media_types": ["text"],
                "languages": ["zh", "en"],
                "role": "core",
            },
        )
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["status"], "DRAFT")
        payload = self.client.get("/api/collection?role=core").json()
        self.assertEqual(len(payload["tasks"]), 1)
        self.assertTrue(all(item["status"] == "NOT_CONFIGURED" for item in payload["connectors"]))

    def test_07_alert_action_is_persisted_and_audited(self):
        alerts = self.client.get("/api/alerts?role=core").json()["items"]
        self.assertGreater(len(alerts), 0)
        alert_id = alerts[0]["id"]
        updated = self.client.patch(
            f"/api/alerts/{alert_id}",
            json={"status": "ACKNOWLEDGED", "assignee": "测试审核员", "note": "已核对源文件锚点", "role": "core"},
        )
        self.assertEqual(updated.status_code, 200)
        current = {item["id"]: item for item in self.client.get("/api/alerts?role=core").json()["items"]}
        self.assertEqual(current[alert_id]["status"], "ACKNOWLEDGED")
        audit = self.client.get("/api/audit").json()
        self.assertTrue(audit["chain_verified"])

    def test_08_report_generation_keeps_test_boundary(self):
        payload = self.client.post(
            "/api/reports/generate",
            json={"template": "trace", "focus": "来源与口径冲突", "role": "core"},
        ).json()
        self.assertEqual(payload["status"], "GENERATED_FROM_TEST_BATCH")
        self.assertGreaterEqual(len(payload["sections"]), 4)
        self.assertIn("未调用生成式大模型", payload["notice"])

    def test_09_repeated_ingest_is_idempotent(self):
        first = ingest_snapshot(db_path=database.DB_PATH)
        second = ingest_snapshot(db_path=database.DB_PATH)
        self.assertEqual(first["record_count"], second["record_count"])
        with database.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM dataset_batch").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM source_record").fetchone()[0], 134)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM knowledge_chunk").fetchone()[0], 134)

    def test_10_validation_errors_do_not_mutate_state(self):
        before = len(self.client.get("/api/collection?role=core").json()["tasks"])
        invalid = self.client.post(
            "/api/collection/tasks",
            json={"name": "x", "dimension": "unknown", "target_value": "", "connector_id": "missing"},
        )
        self.assertEqual(invalid.status_code, 422)
        after = len(self.client.get("/api/collection?role=core").json()["tasks"])
        self.assertEqual(before, after)

    def test_11_unknown_roles_and_read_only_mutations_are_rejected(self):
        self.assertEqual(self.client.get("/api/targets?role=typo").status_code, 422)
        self.assertEqual(
            self.client.post(
                "/api/knowledge/search",
                json={"query": "来源", "role": "typo"},
            ).status_code,
            422,
        )
        before = len(self.client.get("/api/collection?role=core").json()["tasks"])
        denied = self.client.post(
            "/api/collection/tasks",
            json={
                "name": "越权任务草稿",
                "dimension": "keyword",
                "target_value": "denied",
                "connector_id": "x",
                "role": "researcher",
            },
        )
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(before, len(self.client.get("/api/collection?role=core").json()["tasks"]))

    def test_12_same_count_snapshot_update_is_reimported(self):
        snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        snapshot["familyMembers"][0]["nameZh"] = "替换数据验证对象"
        replacement = Path(self.temp.name) / "replacement.json"
        replacement.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
        try:
            ingest_snapshot(replacement, database.DB_PATH)
            with database.db() as conn:
                content = json.loads(
                    conn.execute(
                        "SELECT content_json FROM source_record WHERE id LIKE '%:actor:001'"
                    ).fetchone()[0]
                )
            self.assertEqual(content["nameZh"], "替换数据验证对象")
        finally:
            ingest_snapshot(SNAPSHOT_PATH, database.DB_PATH)

    def test_13_incomplete_index_is_detected_and_self_healed(self):
        with database.db() as conn:
            conn.execute("DELETE FROM knowledge_chunk WHERE id=(SELECT id FROM knowledge_chunk LIMIT 1)")
        collection = self.client.get("/api/collection?role=core").json()
        index_step = next(item for item in collection["pipeline"] if item["id"] == "index")
        self.assertEqual(index_step["status"], "INCOMPLETE")
        self.assertEqual(index_step["value"], 133)
        ingest_snapshot(db_path=database.DB_PATH)
        with database.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM knowledge_chunk").fetchone()[0], 134)

    def test_14_irrelevant_query_refuses_and_corrupt_vector_degrades(self):
        irrelevant = search("完全不存在的随机词组甲乙丙丁", role="core", db_path=database.DB_PATH)
        self.assertEqual(irrelevant["result_count"], 0)
        with database.db() as conn:
            conn.execute("UPDATE knowledge_chunk SET vector=? WHERE id=(SELECT id FROM knowledge_chunk LIMIT 1)", (b"broken",))
        degraded = search("来源", role="core", db_path=database.DB_PATH)
        self.assertEqual(degraded["invalid_vector_count"], 1)
        self.assertIn("已降级", degraded["notice"])
        with database.db() as conn:
            conn.execute("DELETE FROM knowledge_chunk WHERE length(vector) != dimensions * 4")
        ingest_snapshot(db_path=database.DB_PATH)

    def test_15_graph_reads_the_normalized_database_version(self):
        record_id = "TEST-ZIP-20260729-001:actor:001"
        with database.db() as conn:
            original = conn.execute("SELECT content_json FROM source_record WHERE id=?", (record_id,)).fetchone()[0]
            changed = json.loads(original)
            changed["nameZh"] = "数据库图谱验证对象"
            conn.execute("UPDATE source_record SET content_json=? WHERE id=?", (json.dumps(changed, ensure_ascii=False), record_id))
        try:
            payload = build_graph("actors", role="core", db_path=database.DB_PATH)
            self.assertTrue(any(node["label"] == "数据库图谱验证对象" for node in payload["nodes"]))
        finally:
            with database.db() as conn:
                conn.execute("UPDATE source_record SET content_json=? WHERE id=?", (original, record_id))

    def test_16_requirement_matrix_excludes_procurement_row(self):
        payload = self.client.get("/api/requirements").json()
        self.assertEqual(len(payload["items"]), 32)
        self.assertFalse(any("采购" in item["feature"] or "报价" in item["feature"] for item in payload["items"]))

    def test_17_empty_snapshot_is_rejected_without_mutation(self):
        empty = Path(self.temp.name) / "empty.json"
        empty.write_text("{}", encoding="utf-8")
        with database.db() as conn:
            before = conn.execute("SELECT COUNT(*) FROM source_record").fetchone()[0]
        with self.assertRaises(ValueError):
            ingest_snapshot(empty, database.DB_PATH)
        with database.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM source_record").fetchone()[0], before)

    def test_18_same_count_wrong_mapping_is_rebuilt(self):
        with database.db() as conn:
            conn.execute("DROP INDEX ux_knowledge_chunk_version_record")
            rows = conn.execute("SELECT id,record_id FROM knowledge_chunk ORDER BY id LIMIT 2").fetchall()
            conn.execute("UPDATE knowledge_chunk SET record_id=? WHERE id=?", (rows[1]["record_id"], rows[0]["id"]))
        ingest_snapshot(db_path=database.DB_PATH)
        with database.db() as conn:
            duplicates = conn.execute(
                "SELECT COUNT(*) FROM (SELECT record_id FROM knowledge_chunk GROUP BY record_id HAVING COUNT(*) != 1)"
            ).fetchone()[0]
            missing = conn.execute(
                """SELECT COUNT(*) FROM source_record sr LEFT JOIN knowledge_chunk kc ON kc.record_id=sr.id
                     WHERE kc.id IS NULL"""
            ).fetchone()[0]
            self.assertEqual((duplicates, missing), (0, 0))
            self.assertIsNotNone(conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name='ux_knowledge_chunk_version_record'"
            ).fetchone())

    def test_19_corrupt_token_metadata_degrades_and_self_heals(self):
        with database.db() as conn:
            conn.execute("UPDATE knowledge_chunk SET tokens_json='not-json' WHERE id=(SELECT id FROM knowledge_chunk LIMIT 1)")
        degraded = search("来源", role="core", db_path=database.DB_PATH)
        self.assertEqual(degraded["invalid_vector_count"], 1)
        ingest_snapshot(db_path=database.DB_PATH)
        with database.db() as conn:
            self.assertTrue(all(json.loads(row[0]) for row in conn.execute("SELECT tokens_json FROM knowledge_chunk LIMIT 5")))

    def test_20_first_layer_payloads_and_operational_details_are_scoped(self):
        self.assertEqual(search("Trump", role="researcher", db_path=database.DB_PATH)["result_count"], 0)
        overview = self.client.get("/api/overview?role=researcher").json()
        self.assertNotIn("account", overview["batch"]["category_counts"])
        self.assertGreater(overview["metrics"]["hidden_restricted"], 0)
        self.assertEqual(self.client.get("/api/collection?role=researcher").json()["tasks"], [])
        audit = self.client.get("/api/audit?role=researcher").json()
        self.assertTrue(audit["details_redacted"])
        self.assertTrue(all(item["object_id"] == "已隐藏" for item in audit["items"]))

    def test_21_graph_uses_persisted_sensitivity_and_real_source_refs(self):
        payload = build_graph("evidence", role="core", db_path=database.DB_PATH)
        account_edges = [edge for edge in payload["edges"] if edge["source"] == "category:account" and edge["relation"] == "DERIVED_FROM"]
        with database.db() as conn:
            expected_refs = {
                ref
                for row in conn.execute("SELECT source_refs_json FROM source_record WHERE category='account'")
                for ref in json.loads(row[0])
            }
        self.assertEqual({edge["source_ref"] for edge in account_edges}, expected_refs)
        record_id = "TEST-ZIP-20260729-001:production_gap:001"
        with database.db() as conn:
            conn.execute("UPDATE source_record SET sensitivity='RESTRICTED' WHERE id=?", (record_id,))
        try:
            researcher = build_graph("evidence", role="researcher", db_path=database.DB_PATH)
            self.assertFalse(any(node.get("metadata", {}).get("_record_id") == record_id for node in researcher["nodes"]))
        finally:
            with database.db() as conn:
                conn.execute("UPDATE source_record SET sensitivity='INTERNAL' WHERE id=?", (record_id,))

    def test_22_replacement_preserves_alert_workflow_and_changes_version_identity(self):
        alert = self.client.get("/api/alerts?role=core").json()["items"][0]
        self.client.patch(
            f"/api/alerts/{alert['id']}",
            json={"status": "RESOLVED", "assignee": "状态保留审核员", "note": "不得被重导入清空", "role": "core"},
        )
        with database.db() as conn:
            original_version = conn.execute("SELECT id FROM knowledge_version").fetchone()[0]
        snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        snapshot["familyMembers"][0]["nameZh"] = "状态保留替换对象"
        replacement = Path(self.temp.name) / "state-preserving.json"
        replacement.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
        ingest_snapshot(replacement, database.DB_PATH)
        with database.db() as conn:
            state = conn.execute("SELECT status,assignee,note FROM alert_case WHERE id=?", (alert["id"],)).fetchone()
            replacement_version = conn.execute("SELECT id FROM knowledge_version").fetchone()[0]
        self.assertEqual(tuple(state), ("RESOLVED", "状态保留审核员", "不得被重导入清空"))
        self.assertNotEqual(original_version, replacement_version)
        ingest_snapshot(SNAPSHOT_PATH, database.DB_PATH)


if __name__ == "__main__":
    unittest.main()

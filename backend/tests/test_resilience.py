from __future__ import annotations

import concurrent.futures
import re
import struct
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from backend import database
from backend.api import app
from backend.ingest import ingest_snapshot
from backend.maintenance import backup_database, verify_database
from backend.retrieval import search


class ResilienceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.previous_db = database.DB_PATH
        database.DB_PATH = Path(cls.temp.name) / "resilience.db"
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_context.__exit__(None, None, None)
        database.DB_PATH = cls.previous_db
        cls.temp.cleanup()

    def test_01_health_reports_integrity_and_index_state(self):
        payload = self.client.get("/api/health").json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["database_integrity"], "ok")
        self.assertTrue(payload["index_complete"])
        self.assertEqual(payload["records"], 134)
        self.assertEqual(payload["knowledge_chunks"], 134)

    def test_02_static_product_and_assets_are_deliverable(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        asset = re.search(r'(?:src|href)="(/assets/[^"]+)"', response.text)
        self.assertIsNotNone(asset)
        self.assertEqual(self.client.get(asset.group(1)).status_code, 200)

    def test_03_concurrent_reads_and_writes_complete_without_lock_errors(self):
        def read(index: int) -> int:
            endpoint = "/api/overview?role=core" if index % 2 else "/api/graph?view=evidence&role=core"
            return self.client.get(endpoint).status_code

        def write(index: int) -> int:
            return self.client.post(
                "/api/collection/tasks",
                json={
                    "name": f"并发任务 {index}",
                    "dimension": "keyword",
                    "target_value": f"resilience-{index}",
                    "connector_id": "x",
                    "role": "core",
                },
            ).status_code

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(read, range(16))) + list(pool.map(write, range(6)))
        self.assertEqual(statuses, [200] * len(statuses))
        self.assertTrue(self.client.get("/api/audit").json()["chain_verified"])

    def test_04_startup_is_idempotent_and_keeps_operational_state(self):
        before = len(self.client.get("/api/collection?role=core").json()["tasks"])
        ingest_snapshot(db_path=database.DB_PATH)
        database.init_db(database.DB_PATH)
        with TestClient(app) as restarted:
            payload = restarted.get("/api/collection?role=core").json()
            self.assertEqual(len(payload["tasks"]), before)
            self.assertEqual(restarted.get("/api/health").json()["status"], "ok")

    def test_05_invalid_snapshot_does_not_mutate_database(self):
        invalid = Path(self.temp.name) / "invalid.json"
        invalid.write_text('{"meta":', encoding="utf-8")
        with database.db() as conn:
            before = conn.execute("SELECT COUNT(*) FROM source_record").fetchone()[0]
        with self.assertRaises(Exception):
            ingest_snapshot(invalid, database.DB_PATH)
        with database.db() as conn:
            after = conn.execute("SELECT COUNT(*) FROM source_record").fetchone()[0]
        self.assertEqual(before, after)

    def test_06_consistent_backup_is_created_and_verified(self):
        target = Path(self.temp.name) / "backup" / "monitor.db"
        result = backup_database(target, database.DB_PATH)
        self.assertTrue(result["ok"])
        self.assertEqual(result["tables"]["source_record"], 134)
        verified = verify_database(target)
        self.assertTrue(verified["ok"])
        self.assertEqual(verified["foreign_key_errors"], 0)

    def test_07_audit_tampering_is_detected(self):
        with database.db() as conn:
            row = conn.execute("SELECT id,detail,trace_hash FROM audit_event ORDER BY id DESC LIMIT 1").fetchone()
            conn.execute("UPDATE audit_event SET detail=? WHERE id=?", ("tampered", row["id"]))
        self.assertFalse(self.client.get("/api/audit").json()["chain_verified"])
        with database.db() as conn:
            conn.execute(
                "UPDATE audit_event SET detail=?,trace_hash=? WHERE id=?",
                (row["detail"], row["trace_hash"], row["id"]),
            )
        self.assertTrue(self.client.get("/api/audit").json()["chain_verified"])

    def test_08_invalid_views_and_oversized_queries_are_rejected(self):
        self.assertEqual(self.client.get("/api/graph?view=unknown&role=core").status_code, 422)
        self.assertEqual(
            self.client.post(
                "/api/knowledge/search",
                json={"query": "x" * 501, "role": "core"},
            ).status_code,
            422,
        )
        self.assertEqual(self.client.get(f"/api/knowledge/search?q={'x' * 501}&role=core").status_code, 422)
        self.assertEqual(
            self.client.post(
                "/api/reports/generate",
                json={"template": "validation", "focus": "x" * 501, "role": "core"},
            ).status_code,
            422,
        )

    def test_09_non_finite_vector_degrades_without_server_error(self):
        vector = struct.pack("<256f", *([float("nan")] * 256))
        with database.db() as conn:
            conn.execute("UPDATE knowledge_chunk SET vector=? WHERE id=(SELECT id FROM knowledge_chunk LIMIT 1)", (vector,))
        payload = search("来源", role="core", db_path=database.DB_PATH)
        self.assertEqual(payload["invalid_vector_count"], 1)
        self.assertIn("已降级", payload["notice"])
        ingest_snapshot(db_path=database.DB_PATH)

    def test_10_audit_checkpoint_detects_truncation(self):
        with database.db() as conn:
            row = dict(conn.execute("SELECT * FROM audit_event ORDER BY id DESC LIMIT 1").fetchone())
            conn.execute("DELETE FROM audit_event WHERE id=?", (row["id"],))
        self.assertFalse(self.client.get("/api/audit?role=core").json()["chain_verified"])
        with database.db() as conn:
            conn.execute(
                """INSERT INTO audit_event(id,event_time,actor,action,object_type,object_id,detail,outcome,trace_hash)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                tuple(row[key] for key in ("id", "event_time", "actor", "action", "object_type", "object_id", "detail", "outcome", "trace_hash")),
            )
        self.assertTrue(self.client.get("/api/audit?role=core").json()["chain_verified"])

    def test_11_concurrent_cold_start_is_serialized(self):
        for index in range(12):
            target = Path(self.temp.name) / f"cold-{index}.db"
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: ingest_snapshot(db_path=target), range(2)))
            self.assertEqual([item["record_count"] for item in results], [134, 134])
            with database.db(target) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM dataset_batch").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM source_record").fetchone()[0], 134)

    def test_12_missing_audit_checkpoint_fails_closed_after_restart(self):
        with database.db() as conn:
            event = dict(conn.execute("SELECT * FROM audit_event ORDER BY id DESC LIMIT 1").fetchone())
            checkpoint = dict(conn.execute("SELECT * FROM audit_checkpoint WHERE id=1").fetchone())
            conn.execute("DELETE FROM audit_event WHERE id=?", (event["id"],))
            conn.execute("DELETE FROM audit_checkpoint WHERE id=1")
        try:
            database.init_db(database.DB_PATH)
            self.assertFalse(self.client.get("/api/audit?role=core").json()["chain_verified"])
        finally:
            with database.db() as conn:
                conn.execute(
                    """INSERT INTO audit_event(id,event_time,actor,action,object_type,object_id,detail,outcome,trace_hash)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    tuple(event[key] for key in ("id", "event_time", "actor", "action", "object_type", "object_id", "detail", "outcome", "trace_hash")),
                )
                conn.execute(
                    "INSERT INTO audit_checkpoint(id,event_count,head_hash,updated_at) VALUES(?,?,?,?)",
                    tuple(checkpoint[key] for key in ("id", "event_count", "head_hash", "updated_at")),
                )
        self.assertTrue(self.client.get("/api/audit?role=core").json()["chain_verified"])

    def test_13_stopword_only_queries_are_refused(self):
        for query in ("the", "a", "is"):
            self.assertEqual(search(query, role="core", db_path=database.DB_PATH)["result_count"], 0)

    def test_14_start_script_checks_native_exit_codes(self):
        script = (Path(__file__).resolve().parents[2] / "start.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("function Invoke-NativeCommand", script)
        self.assertIn("$LASTEXITCODE -ne 0", script)
        self.assertGreaterEqual(script.count("Invoke-NativeCommand"), 5)


if __name__ == "__main__":
    unittest.main()

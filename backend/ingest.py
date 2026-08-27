from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import database as database_module
from .database import (
    ROOT,
    set_active_batch,
    append_audit_block,
    db,
    ensure_knowledge_chunk_constraint,
    init_db,
    json_text,
    now_iso,
    require_audit_appendable,
)
from .features import DIMENSIONS, feature_vector, pack_vector, tokenize


SNAPSHOT_PATH = ROOT / "src" / "zipSnapshot.json"
SOURCE_ZIP_SHA256 = "3dd056dd5d896c9e253d74d5842affeba10f396f6484d591a8500e5f5403f9b4"
BATCH_CODE = "TEST-ZIP-20260729-001"
COLLECTION_ID = "kb-overseas-opinion-test"
VERSION_PREFIX = "kb-overseas-opinion-test:"


CATEGORY_MAP = {
    "accounts": "account",
    "familyMembers": "actor",
    "profileSignals": "profile_signal",
    "tweets": "content",
    "timeline": "event",
    "interactionLayers": "relationship_layer",
    "businessAndPolitics": "business_signal",
    "sources": "source",
    "themeMatrix": "analysis",
    "inconsistencies": "quality_conflict",
    "missingForProduction": "production_gap",
}

DEFAULT_SOURCES = {
    "account": ["trump-family-x-analysis.html"],
    "actor": ["trump-family-report.html", "trump-family-x-analysis.html"],
    "profile_signal": ["trump-family-report.html"],
    "content": ["trump-family-x-analysis.html"],
    "event": ["trump-family-report.html"],
    "relationship_layer": ["trump-interaction-circle.html"],
    "business_signal": ["trump-family-report.html"],
    "source": ["trump-family-report.html"],
    "analysis": ["trump-family-x-analysis.html"],
    "quality_conflict": ["ZIP cross-file review"],
    "production_gap": ["ZIP completeness review"],
}

SENSITIVITY = {
    "account": "RESTRICTED",
    "actor": "RESTRICTED",
    "profile_signal": "RESTRICTED",
    "content": "RESTRICTED",
    "event": "RESTRICTED",
    "relationship_layer": "RESTRICTED",
    "business_signal": "RESTRICTED",
    "analysis": "RESTRICTED",
    "source": "RESTRICTED",
    "quality_conflict": "RESTRICTED",
    "production_gap": "INTERNAL",
}

_INGEST_LOCKS: dict[str, threading.Lock] = {}
_INGEST_LOCKS_GUARD = threading.Lock()


@contextmanager
def _ingest_lock(database_path: Path, timeout: float = 20.0):
    resolved = database_path.resolve()
    lock_key = str(resolved).lower()
    with _INGEST_LOCKS_GUARD:
        thread_lock = _INGEST_LOCKS.setdefault(lock_key, threading.Lock())
    if not thread_lock.acquire(timeout=timeout):
        raise TimeoutError("等待测试数据导入锁超时")
    lock_path = resolved.with_suffix(f"{resolved.suffix}.ingest.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with lock_path.open("a+b") as handle:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            deadline = time.monotonic() + timeout
            if os.name == "nt":
                import msvcrt

                while True:
                    try:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("等待跨进程测试数据导入锁超时")
                        time.sleep(0.05)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                while True:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("等待跨进程测试数据导入锁超时")
                        time.sleep(0.05)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        thread_lock.release()


def _sha(value: str | bytes) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _clean_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value if item]


def _title(category: str, item: Any, index: int) -> str:
    if isinstance(item, str):
        return item
    options = {
        "account": [item.get("nameZh"), item.get("name"), item.get("handle")],
        "actor": [item.get("nameZh"), item.get("name"), item.get("relation")],
        "profile_signal": [item.get("member"), item.get("relation")],
        "content": [" / ".join(_clean_list(item.get("themes"))) or None, item.get("content")],
        "event": [item.get("title")],
        "relationship_layer": [item.get("name")],
        "business_signal": [item.get("title")],
        "source": [item.get("name"), item.get("claim")],
        "analysis": [item.get("member"), "主题画像"],
        "quality_conflict": [item.get("field")],
        "production_gap": [str(item)],
    }
    return next((str(value) for value in options.get(category, []) if value), f"记录 {index + 1}")


def _summary(category: str, item: Any) -> str:
    if isinstance(item, str):
        return item
    if category == "account":
        themes = "、".join(_clean_list(item.get("themes")))
        return "；".join(filter(None, [item.get("role"), item.get("handle"), themes]))
    if category == "actor":
        return "；".join(filter(None, [item.get("relation"), item.get("role")]))
    if category == "profile_signal":
        return str(item.get("summary") or item.get("relation") or "")
    if category == "content":
        return str(item.get("content") or item.get("quotedText") or "源文件仅保留指标，未提供正文")
    if category == "event":
        return str(item.get("summary") or "")
    if category == "relationship_layer":
        members = "、".join(
            str(member.get("handle") or member.get("name") or member)
            if isinstance(member, dict) else str(member)
            for member in (item.get("members") or [])
        )
        return "；".join(filter(None, [item.get("interactionMode"), members, item.get("notes")]))
    if category == "business_signal":
        return "；".join(filter(None, [item.get("metricRaw"), "、".join(_clean_list(item.get("actors"))), "、".join(_clean_list(item.get("claims")))]))
    if category == "source":
        return "；".join(filter(None, [item.get("claim"), item.get("date"), item.get("url")]))
    if category == "analysis":
        return "；".join(filter(None, ["、".join(_clean_list(item.get("topics"))), item.get("tone"), item.get("politicization"), item.get("commercial")]))
    if category == "quality_conflict":
        values = item.get("values")
        values_text = json_text(values) if not isinstance(values, str) else values
        return "；".join(filter(None, [values_text, item.get("impact")]))
    return json_text(item)


def _sources(category: str, item: Any) -> list[str]:
    if isinstance(item, str):
        return DEFAULT_SOURCES[category]
    values: list[str] = []
    for key in ("sourceRefs", "sources"):
        values.extend(_clean_list(item.get(key)))
    if category == "source":
        values.extend(_clean_list(item.get("url")))
        values.extend(_clean_list(item.get("name")))
    return list(dict.fromkeys(values or DEFAULT_SOURCES[category]))


def _evidence(category: str, item: Any) -> str:
    if category == "quality_conflict":
        return "source_conflict"
    if category == "production_gap":
        return "production_gap"
    if isinstance(item, dict) and item.get("evidence"):
        return str(item["evidence"])
    return "source_snapshot"


def _record_text(title: str, summary: str, item: Any) -> str:
    compact = json_text(item)
    return f"{title}\n{summary}\n{compact}"


def _validate_snapshot(snapshot: Any) -> dict:
    if not isinstance(snapshot, dict):
        raise ValueError("测试数据快照必须是 JSON 对象")
    meta = snapshot.get("meta")
    if not isinstance(meta, dict):
        raise ValueError("测试数据快照缺少 meta 对象")
    for key in CATEGORY_MAP:
        value = snapshot.get(key, [])
        if not isinstance(value, list):
            raise ValueError(f"测试数据字段 {key} 必须是数组")
    substantive = sum(
        len(snapshot.get(key, []))
        for key in CATEGORY_MAP
        if key not in {"inconsistencies", "missingForProduction"}
    )
    if substantive == 0:
        raise ValueError("测试数据快照没有可入库的业务记录")
    return snapshot


def _version_identity(meta: dict, snapshot_hash: str) -> tuple[str, str]:
    date_value = str(meta.get("asOf") or "undated").replace("-", ".")
    version = f"v{date_value}+{snapshot_hash[:12]}"
    return f"{VERSION_PREFIX}{version}", version


def index_integrity(conn, batch_id: int | None = None, version_id: str | None = None) -> dict:
    if batch_id is None:
        active = conn.execute(
            "SELECT value FROM workspace_setting WHERE key='active_batch_code'"
        ).fetchone()
        batch = conn.execute(
            "SELECT id FROM dataset_batch WHERE code=?", (active["value"],)
        ).fetchone() if active else None
        if not batch:
            batch = conn.execute(
                "SELECT id FROM dataset_batch ORDER BY updated_at DESC,id DESC LIMIT 1"
            ).fetchone()
        if not batch:
            return {"complete": False, "record_count": 0, "chunk_count": 0, "issues": ["missing_batch"]}
        batch_id = int(batch["id"])
    if version_id is None:
        version = conn.execute(
            "SELECT id FROM knowledge_version WHERE batch_id=? ORDER BY created_at DESC,id DESC LIMIT 1",
            (batch_id,),
        ).fetchone()
        version_id = str(version["id"]) if version else ""

    records = conn.execute(
        "SELECT id,title,summary,content_json,content_hash FROM source_record WHERE batch_id=? ORDER BY id",
        (batch_id,),
    ).fetchall()
    chunks = conn.execute(
        "SELECT id,record_id,text,tokens_json,vector,dimensions FROM knowledge_chunk WHERE version_id=? ORDER BY id",
        (version_id,),
    ).fetchall() if version_id else []
    issues: list[str] = []
    record_map = {row["id"]: row for row in records}
    chunk_records = Counter(str(row["record_id"]) for row in chunks)
    if not records:
        issues.append("empty_record_set")
    if len(chunks) != len(records):
        issues.append("count_mismatch")
    if set(chunk_records) != set(record_map):
        issues.append("record_mapping_mismatch")
    if any(count != 1 for count in chunk_records.values()):
        issues.append("duplicate_record_mapping")

    version_row = conn.execute(
        "SELECT entry_count,chunk_count FROM knowledge_version WHERE id=?",
        (version_id,),
    ).fetchone() if version_id else None
    if not version_row or version_row["entry_count"] != len(records) or version_row["chunk_count"] != len(chunks):
        issues.append("version_count_mismatch")

    for record in records:
        try:
            payload = json.loads(record["content_json"])
        except (TypeError, json.JSONDecodeError):
            issues.append(f"invalid_record_json:{record['id']}")
            continue
        if _sha(record["content_json"]) != record["content_hash"]:
            issues.append(f"content_hash_mismatch:{record['id']}")
        matching = [chunk for chunk in chunks if chunk["record_id"] == record["id"]]
        if len(matching) != 1:
            continue
        chunk = matching[0]
        expected_text = _record_text(record["title"], record["summary"], payload)
        expected_tokens = tokenize(expected_text)
        expected_vector = pack_vector(feature_vector(expected_text))
        try:
            actual_tokens = json.loads(chunk["tokens_json"])
            actual_vector = bytes(chunk["vector"])
            dimensions = int(chunk["dimensions"])
        except (TypeError, ValueError, json.JSONDecodeError):
            issues.append(f"invalid_chunk_metadata:{record['id']}")
            continue
        if chunk["id"] != f"chunk:{record['id']}" or chunk["text"] != expected_text:
            issues.append(f"chunk_text_mismatch:{record['id']}")
        if actual_tokens != expected_tokens:
            issues.append(f"chunk_tokens_mismatch:{record['id']}")
        if dimensions != DIMENSIONS or actual_vector != expected_vector:
            issues.append(f"chunk_vector_mismatch:{record['id']}")

    return {
        "complete": not issues,
        "record_count": len(records),
        "chunk_count": len(chunks),
        "version_id": version_id,
        "issues": issues[:50],
    }


def _audit(conn, actor: str, action: str, object_type: str, object_id: str, detail: str, outcome: str, previous: str = "") -> str:
    event_time = now_iso()
    if not previous:
        require_audit_appendable(conn)
        last = conn.execute("SELECT trace_hash FROM audit_event ORDER BY id DESC LIMIT 1").fetchone()
        previous = last[0] if last else ""
    trace = _sha("|".join([previous, event_time, actor, action, object_type, object_id, detail, outcome]))
    conn.execute(
        """INSERT INTO audit_event
           (event_time,actor,action,object_type,object_id,detail,outcome,trace_hash)
           VALUES(?,?,?,?,?,?,?,?)""",
        (event_time, actor, action, object_type, object_id, detail, outcome, trace),
    )
    event_count = conn.execute("SELECT COUNT(*) FROM audit_event").fetchone()[0]
    conn.execute(
        """INSERT INTO audit_checkpoint(id,event_count,head_hash,updated_at) VALUES(1,?,?,?)
           ON CONFLICT(id) DO UPDATE SET event_count=excluded.event_count,
             head_hash=excluded.head_hash,updated_at=excluded.updated_at""",
        (event_count, trace, event_time),
    )
    append_audit_block(
        conn,
        event_type=action,
        actor=actor,
        object_type=object_type,
        object_id=str(object_id),
        detail=detail,
        outcome=outcome,
        batch_id=BATCH_CODE,
    )
    return trace


def _ensure_alert_cases(conn, snapshot: dict, timestamp: str) -> None:
    for index, item in enumerate(snapshot.get("timeline", []), start=1):
        risk = str(item.get("risk") or "未标注")
        if risk in {"低", "low", "未标注"}:
            continue
        record_id = f"{BATCH_CODE}:event:{index:03d}"
        alert_id = f"ALERT-TEST-{index:03d}"
        conn.execute(
            """INSERT INTO alert_case
               (id,record_id,risk,status,assignee,note,created_at,updated_at)
               VALUES(?,?,?,'PENDING','','',?,?)
               ON CONFLICT(id) DO NOTHING""",
            (alert_id, record_id, risk, timestamp, timestamp),
        )


def _ingest_snapshot_unlocked(snapshot_path: Path | None = None, db_path: Path | None = None) -> dict:
    path = Path(snapshot_path or SNAPSHOT_PATH)
    raw = path.read_bytes()
    snapshot_hash = _sha(raw)
    snapshot = _validate_snapshot(json.loads(raw.decode("utf-8")))
    meta = snapshot["meta"]
    version_id, version_label = _version_identity(meta, snapshot_hash)
    init_db(db_path)
    timestamp = now_iso()
    source_files = meta.get("sourceFiles", [])

    normalized: list[dict] = []
    category_counts: Counter[str] = Counter()
    for source_key, category in CATEGORY_MAP.items():
        for index, item in enumerate(snapshot.get(source_key, [])):
            title = _title(category, item, index)
            summary = _summary(category, item)
            record_id = f"{BATCH_CODE}:{category}:{index + 1:03d}"
            payload = item if isinstance(item, dict) else {"value": item}
            content_hash = _sha(json_text(payload))
            normalized.append({
                "id": record_id,
                "category": category,
                "title": title,
                "summary": summary,
                "payload": payload,
                "evidence": _evidence(category, item),
                "sources": _sources(category, item),
                "sensitivity": SENSITIVITY[category],
                "content_hash": content_hash,
            })
            category_counts[category] += 1

    with db(db_path) as conn:
        # The first imported batch becomes active.  A later public/demo import
        # can explicitly switch the workbench without startup re-ingest
        # silently taking it back.
        if not conn.execute(
            "SELECT 1 FROM workspace_setting WHERE key='active_batch_code'"
        ).fetchone():
            set_active_batch(conn, BATCH_CODE)
        existing = conn.execute(
            "SELECT id,checksum,record_count,metadata_json FROM dataset_batch WHERE code=?",
            (BATCH_CODE,),
        ).fetchone()
        expected_quality = len(snapshot.get("inconsistencies", [])) + len(snapshot.get("missingForProduction", []))
        expected_alerts = sum(
            1
            for item in snapshot.get("timeline", [])
            if str(item.get("risk") or "未标注") not in {"低", "low", "未标注"}
        )
        existing_metadata = json.loads(existing["metadata_json"]) if existing else {}
        existing_counts = {}
        existing_alert_states: list[dict] = []
        if existing:
            current_version = conn.execute(
                "SELECT id FROM knowledge_version WHERE batch_id=? ORDER BY created_at DESC,id DESC LIMIT 1",
                (existing["id"],),
            ).fetchone()
            integrity = index_integrity(
                conn,
                int(existing["id"]),
                str(current_version["id"]) if current_version else version_id,
            )
            existing_counts = {
                "records": conn.execute("SELECT COUNT(*) FROM source_record WHERE batch_id=?", (existing["id"],)).fetchone()[0],
                "chunks": integrity["chunk_count"],
                "quality": conn.execute("SELECT COUNT(*) FROM quality_issue WHERE batch_id=?", (existing["id"],)).fetchone()[0],
                "alerts": conn.execute(
                    """SELECT COUNT(*) FROM alert_case ac
                       JOIN source_record sr ON sr.id=ac.record_id WHERE sr.batch_id=?""",
                    (existing["id"],),
                ).fetchone()[0],
            }
            existing_alert_states = [
                dict(row)
                for row in conn.execute(
                    """SELECT ac.id,ac.status,ac.assignee,ac.note,ac.created_at
                         FROM alert_case ac JOIN source_record sr ON sr.id=ac.record_id
                         WHERE sr.batch_id=?""",
                    (existing["id"],),
                ).fetchall()
            ]
        is_complete = existing and existing_counts == {
            "records": len(normalized),
            "chunks": len(normalized),
            "quality": expected_quality,
            "alerts": expected_alerts,
        } and integrity["complete"] and integrity["version_id"] == version_id
        if (
            existing
            and existing["checksum"] == snapshot_hash
            and existing["record_count"] == len(normalized)
            and existing_metadata.get("snapshotJsonSha256") == snapshot_hash
            and is_complete
        ):
            _ensure_alert_cases(conn, snapshot, timestamp)
            return batch_summary(conn, BATCH_CODE)

        if existing:
            batch_id = existing["id"]
            conn.execute(
                "DELETE FROM alert_case WHERE record_id IN (SELECT id FROM source_record WHERE batch_id=?)",
                (batch_id,),
            )
            conn.execute("DELETE FROM knowledge_version WHERE batch_id=?", (batch_id,))
            conn.execute("DELETE FROM quality_issue WHERE batch_id=?", (batch_id,))
            conn.execute("DELETE FROM source_record WHERE batch_id=?", (batch_id,))
            conn.execute(
                """UPDATE dataset_batch SET name=?,purpose=?,subject=?,source_date=?,status=?,checksum=?,
                   source_files_json=?,record_count=?,category_counts_json=?,metadata_json=?,updated_at=? WHERE id=?""",
                (
                    "海外舆情监测批次 2026-07-29",
                    "汇聚监测对象、舆情信息、知识库与研判线索，支持多维检索与分析",
                    meta.get("subject", "海外舆情公开信息"),
                    meta.get("asOf"), "TEST_READY", snapshot_hash,
                    json_text(source_files), len(normalized), json_text(dict(category_counts)),
                    json_text({**meta, "snapshotJsonSha256": snapshot_hash, "sourceZipSha256": SOURCE_ZIP_SHA256, "versionId": version_id}), timestamp, batch_id,
                ),
            )
        else:
            cursor = conn.execute(
                """INSERT INTO dataset_batch
                   (code,name,purpose,subject,source_date,status,checksum,source_files_json,
                    record_count,category_counts_json,metadata_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    BATCH_CODE, "海外舆情监测批次 2026-07-29",
                    "汇聚监测对象、舆情信息、知识库与研判线索，支持多维检索与分析",
                    meta.get("subject", "海外舆情公开信息"), meta.get("asOf"),
                    "TEST_READY", snapshot_hash, json_text(source_files), len(normalized),
                    json_text(dict(category_counts)), json_text({**meta, "snapshotJsonSha256": snapshot_hash, "sourceZipSha256": SOURCE_ZIP_SHA256, "versionId": version_id}),
                    timestamp, timestamp,
                ),
            )
            batch_id = cursor.lastrowid

        conn.executemany(
            """INSERT INTO source_record
               (id,batch_id,category,title,summary,content_json,evidence_type,source_refs_json,
                sensitivity,content_hash,status,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    item["id"], batch_id, item["category"], item["title"], item["summary"],
                    json_text(item["payload"]), item["evidence"], json_text(item["sources"]),
                    item["sensitivity"], item["content_hash"], "ACTIVE", timestamp,
                )
                for item in normalized
            ],
        )

        conn.execute(
            """INSERT INTO knowledge_collection
               (id,name,description,lifecycle,index_method,classification,owner,updated_at)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET lifecycle=excluded.lifecycle,
                 index_method=excluded.index_method,updated_at=excluded.updated_at""",
            (
                COLLECTION_ID, "海外舆情监测知识库",
                "基于已接入监测数据构建，保留来源、证据性质和质量边界",
                "VALIDATED", "hybrid_lexical_offline_feature_vector", "CONFIDENTIAL",
                "核心课题组", timestamp,
            ),
        )
        conn.execute(
            """INSERT INTO knowledge_version
               (id,collection_id,batch_id,version,status,entry_count,chunk_count,created_at,reviewed_at,notes)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                version_id, COLLECTION_ID, batch_id, version_label, "VALIDATED",
                len(normalized), len(normalized), timestamp, timestamp,
                f"数据版本；快照 SHA-256 {snapshot_hash}；用于海外舆情监测研判",
            ),
        )

        chunks = []
        for item in normalized:
            text = _record_text(item["title"], item["summary"], item["payload"])
            vector = feature_vector(text)
            chunks.append((
                f"chunk:{item['id']}", version_id, item["id"], text,
                json_text(tokenize(text)), pack_vector(vector), DIMENSIONS,
                json_text({"category": item["category"], "evidence": item["evidence"]}),
            ))
        conn.executemany(
            """INSERT INTO knowledge_chunk
               (id,version_id,record_id,text,tokens_json,vector,dimensions,metadata_json)
               VALUES(?,?,?,?,?,?,?,?)""",
            chunks,
        )
        ensure_knowledge_chunk_constraint(conn)

        for index, item in enumerate(snapshot.get("inconsistencies", []), start=1):
            record_id = f"{BATCH_CODE}:quality_conflict:{index:03d}"
            conn.execute(
                """INSERT INTO quality_issue
                   (id,batch_id,issue_type,severity,title,details,status,record_id,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    f"DQ-CONFLICT-{index:03d}", batch_id, "CONSISTENCY", "MEDIUM",
                    str(item.get("field") or f"口径冲突 {index}"), _summary("quality_conflict", item),
                    "OPEN", record_id, timestamp,
                ),
            )
        for index, item in enumerate(snapshot.get("missingForProduction", []), start=1):
            record_id = f"{BATCH_CODE}:production_gap:{index:03d}"
            conn.execute(
                """INSERT INTO quality_issue
                   (id,batch_id,issue_type,severity,title,details,status,record_id,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    f"DQ-GAP-{index:03d}", batch_id, "COMPLETENESS", "HIGH",
                    str(item), "正式数据源接入前需补齐；当前批次保留缺口，不自动推断。",
                    "OPEN", record_id, timestamp,
                ),
            )

        _ensure_alert_cases(conn, snapshot, timestamp)
        for state in existing_alert_states:
            conn.execute(
                """UPDATE alert_case SET status=?,assignee=?,note=?,created_at=?,updated_at=?
                   WHERE id=?""",
                (
                    state["status"], state["assignee"], state["note"],
                    state["created_at"], timestamp, state["id"],
                ),
            )

        trace = _audit(conn, "系统导入器", "登记源批次", "dataset_batch", BATCH_CODE, f"登记 {len(source_files)} 份源文件；快照 {snapshot_hash}", "SUCCESS")
        trace = _audit(conn, "规范化引擎", "分类并保留证据边界", "dataset_batch", BATCH_CODE, f"生成 {len(normalized)} 条、{len(category_counts)} 类记录", "SUCCESS", trace)
        _audit(conn, "知识索引器", "构建混合检索索引", "dataset_batch", BATCH_CODE, f"持久化 {len(chunks)} 个 {DIMENSIONS} 维离线特征向量", "SUCCESS", trace)
        return batch_summary(conn, BATCH_CODE)


def ingest_snapshot(snapshot_path: Path | None = None, db_path: Path | None = None) -> dict:
    database_path = Path(db_path) if db_path is not None else Path(database_module.DB_PATH)
    with _ingest_lock(database_path):
        return _ingest_snapshot_unlocked(snapshot_path, database_path)


def batch_summary(conn, code: str = BATCH_CODE) -> dict:
    row = conn.execute("SELECT * FROM dataset_batch WHERE code=?", (code,)).fetchone()
    if not row:
        return {}
    result = dict(row)
    for key in ("source_files_json", "category_counts_json", "metadata_json"):
        result[key.removesuffix("_json")] = json.loads(result.pop(key))
    result["quality_issue_count"] = conn.execute(
        "SELECT COUNT(*) FROM quality_issue WHERE batch_id=?", (row["id"],)
    ).fetchone()[0]
    return result

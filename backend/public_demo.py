"""Import and query the small public-web snapshot used by the demo.

The snapshot contains metadata and short summaries collected from directly
readable public pages.  It is deliberately kept as a separate batch so the
original test ZIP remains available for regression tests and audit review.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import Counter
from pathlib import Path
from typing import Any

from .database import (
    ROOT,
    append_audit_block,
    db,
    ensure_knowledge_chunk_constraint,
    init_db,
    json_text,
    now_iso,
    require_audit_appendable,
    rows_to_dicts,
    set_active_batch,
)
from .features import DIMENSIONS, feature_vector, pack_vector, tokenize
from .ingest import batch_summary, index_integrity


PUBLIC_DATA_PATH = ROOT / "data" / "public_demo_data.json"
PUBLIC_BATCH_CODE = "PUBLIC-WEB-20260827"
PUBLIC_COLLECTION_ID = "kb-public-web-demo"
PUBLIC_VERSION_PREFIX = "kb-public-web-demo:"
PUBLIC_DEMO_LABEL = "公开网页试采样本"

TOPIC_SLUGS = {
    "xiongan": "雄安新区",
    "apec": "APEC 2026",
    "xi-overseas": "习近平海外活动",
}
TOPIC_TO_SLUG = {value: key for key, value in TOPIC_SLUGS.items()}

_IMPORT_LOCK = threading.Lock()


def _sha(value: str | bytes) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _record_text(title: str, summary: str, payload: dict[str, Any]) -> str:
    return f"{title}\n{summary}\n{json_text(payload)}"


def _topic_slug(topic: str) -> str:
    value = str(topic or "").strip()
    if value in TOPIC_TO_SLUG:
        return TOPIC_TO_SLUG[value]
    return value.lower().strip().replace(" ", "-")


def _resolve_topic(value: str) -> str | None:
    normalized = str(value or "").strip()
    if normalized in TOPIC_TO_SLUG:
        return normalized
    return TOPIC_SLUGS.get(normalized.lower().strip())


def _validate_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("公开网页样本必须是 JSON 对象")
    records = payload.get("records")
    if not isinstance(records, list) or not records:
        raise ValueError("公开网页样本缺少 records 数组")
    seen: set[str] = set()
    required = ("id", "topic", "title", "summary", "original_url")
    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            raise ValueError(f"公开网页样本第 {index} 条不是对象")
        missing = [key for key in required if not str(record.get(key) or "").strip()]
        if missing:
            raise ValueError(f"公开网页样本第 {index} 条缺少字段：{','.join(missing)}")
        record_id = str(record["id"]).strip()
        if record_id in seen:
            raise ValueError(f"公开网页样本存在重复 ID：{record_id}")
        seen.add(record_id)
    return payload


def load_public_payload(path: Path | str | None = None) -> tuple[dict[str, Any], bytes, str]:
    source_path = Path(path or PUBLIC_DATA_PATH)
    if not source_path.exists():
        raise FileNotFoundError(f"公开网页样本文件不存在：{source_path}")
    raw = source_path.read_bytes()
    payload = _validate_payload(json.loads(raw.decode("utf-8")))
    return payload, raw, _sha(raw)


def _normalize_records(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], Counter[str]]:
    collected_at = str(payload.get("collected_at") or now_iso())
    normalized: list[dict[str, Any]] = []
    topics: Counter[str] = Counter()
    for index, source in enumerate(payload["records"], start=1):
        source_id = str(source["id"]).strip()
        topic = str(source.get("topic") or "未分类").strip()
        title = str(source.get("title") or source_id).strip()
        summary = str(source.get("summary") or "").strip()
        url = str(source.get("original_url") or "").strip()
        published_at = str(source.get("published_at") or "").strip()
        # Keep the supplied fields and add explicit provenance/status fields.
        record_payload = {
            **source,
            "demo_label": PUBLIC_DEMO_LABEL,
            "source_url": url,
            "published_time": published_at,
            "collection_time": str(source.get("collected_at") or collected_at),
            "original_text": f"{title}\n{summary}",
            "translation_zh": None,
            "translation_status": "NOT_CONFIGURED",
            "sentiment": "NOT_RUN",
            "stance": "NOT_RUN",
            "risk": "NOT_RUN",
            "themes": [topic],
            "keywords": [topic],
        }
        record_id = f"{PUBLIC_BATCH_CODE}:content:{source_id}"
        normalized.append(
            {
                "id": record_id,
                "source_id": source_id,
                "topic": topic,
                "category": "content",
                "title": title,
                "summary": summary,
                "payload": record_payload,
                "evidence": "source_snapshot",
                "sources": [url],
                "sensitivity": "INTERNAL",
                "content_hash": _sha(json_text(record_payload)),
                "index": index,
            }
        )
        topics[topic] += 1
    return normalized, topics


def _write_audit(conn, action: str, object_id: str, detail: str) -> None:
    require_audit_appendable(conn)
    timestamp = now_iso()
    previous = conn.execute("SELECT trace_hash FROM audit_event ORDER BY id DESC LIMIT 1").fetchone()
    previous_hash = previous[0] if previous else ""
    trace_hash = _sha("|".join([previous_hash, timestamp, "公开网页导入器", action, "dataset_batch", object_id, detail, "SUCCESS"]))
    conn.execute(
        """INSERT INTO audit_event
           (event_time,actor,action,object_type,object_id,detail,outcome,trace_hash)
           VALUES(?,?,?,?,?,?,?,?)""",
        (timestamp, "公开网页导入器", action, "dataset_batch", object_id, detail, "SUCCESS", trace_hash),
    )
    event_count = conn.execute("SELECT COUNT(*) FROM audit_event").fetchone()[0]
    conn.execute(
        """INSERT INTO audit_checkpoint(id,event_count,head_hash,updated_at) VALUES(1,?,?,?)
           ON CONFLICT(id) DO UPDATE SET event_count=excluded.event_count,
             head_hash=excluded.head_hash,updated_at=excluded.updated_at""",
        (event_count, trace_hash, timestamp),
    )
    append_audit_block(
        conn,
        event_type=action,
        actor="公开网页导入器",
        object_type="dataset_batch",
        object_id=object_id,
        detail=detail,
        outcome="SUCCESS",
        batch_id=PUBLIC_BATCH_CODE,
    )


def _import_unlocked(path: Path | str | None, db_path: Path | None) -> dict:
    payload, raw, snapshot_hash = load_public_payload(path)
    normalized, topics = _normalize_records(payload)
    init_db(db_path)
    timestamp = now_iso()
    collected_at = str(payload.get("collected_at") or timestamp)
    source_name = str(Path(path or PUBLIC_DATA_PATH).name)
    source_files = [f"data/{source_name}"]
    metadata = {
        "dataStatus": "PUBLIC_WEB_SAMPLE",
        "demoLabel": PUBLIC_DEMO_LABEL,
        "realData": True,
        "collectionMethod": "公开网页可见标题、短摘要与元数据；不绕过登录、验证码或访问控制",
        "collectedAt": collected_at,
        "scope": str(payload.get("scope") or "公开网页小规模试采"),
        "platformAccessObservations": payload.get("platform_access_observations") or [],
        "collectionRun": payload.get("collection_run") or {},
        "collectionSummary": payload.get("collection_summary") or {},
        "collector": payload.get("collector") or {},
        "recordTopics": dict(topics),
        "snapshotSha256": snapshot_hash,
        "limitations": [
            "社交平台与部分媒体的持续采集需官方 API、RSS 或供应商授权",
            "翻译、情感、立场和风险字段在未配置模型前保持未运行",
            "样本只保存短摘要和来源链接，不复制受版权保护的全文",
        ],
    }
    version_label = f"v{collected_at[:10].replace('-', '.')}+{snapshot_hash[:12]}"
    version_id = f"{PUBLIC_VERSION_PREFIX}{version_label}"

    with db(db_path) as conn:
        existing = conn.execute(
            "SELECT id,checksum,record_count,metadata_json,created_at FROM dataset_batch WHERE code=?",
            (PUBLIC_BATCH_CODE,),
        ).fetchone()
        # A refresh may be idempotent; selecting the reviewed snapshot must
        # still be durable even when no rows need to be rewritten.
        set_active_batch(conn, PUBLIC_BATCH_CODE)
        if existing:
            current_version = conn.execute(
                "SELECT id FROM knowledge_version WHERE batch_id=? ORDER BY created_at DESC,id DESC LIMIT 1",
                (existing["id"],),
            ).fetchone()
            integrity = index_integrity(
                conn,
                int(existing["id"]),
                str(current_version["id"]) if current_version else None,
            )
            current_records = conn.execute(
                "SELECT COUNT(*) FROM source_record WHERE batch_id=?", (existing["id"],)
            ).fetchone()[0]
            if (
                existing["checksum"] == snapshot_hash
                and int(existing["record_count"]) == len(normalized)
                and current_records == len(normalized)
                and integrity["complete"]
                and integrity["version_id"] == version_id
            ):
                return batch_summary(conn, PUBLIC_BATCH_CODE)

            conn.execute("DELETE FROM knowledge_version WHERE batch_id=?", (existing["id"],))
            conn.execute("DELETE FROM quality_issue WHERE batch_id=?", (existing["id"],))
            conn.execute("DELETE FROM source_record WHERE batch_id=?", (existing["id"],))
            batch_id = int(existing["id"])
            conn.execute(
                """UPDATE dataset_batch SET name=?,purpose=?,subject=?,source_date=?,status=?,checksum=?,
                   source_files_json=?,record_count=?,category_counts_json=?,metadata_json=?,updated_at=? WHERE id=?""",
                (
                    "公开网页试采批次 2026-08-27",
                    "从可直接访问的公开网页采集海外舆情 Demo 元数据",
                    "雄安新区、APEC 2026、习近平海外活动",
                    collected_at[:10],
                    "PUBLIC_READY",
                    snapshot_hash,
                    json_text(source_files),
                    len(normalized),
                    json_text({"content": len(normalized)}),
                    json_text(metadata),
                    timestamp,
                    batch_id,
                ),
            )
        else:
            cursor = conn.execute(
                """INSERT INTO dataset_batch
                   (code,name,purpose,subject,source_date,status,checksum,source_files_json,
                    record_count,category_counts_json,metadata_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    PUBLIC_BATCH_CODE,
                    "公开网页试采批次 2026-08-27",
                    "从可直接访问的公开网页采集海外舆情 Demo 元数据",
                    "雄安新区、APEC 2026、习近平海外活动",
                    collected_at[:10],
                    "PUBLIC_READY",
                    snapshot_hash,
                    json_text(source_files),
                    len(normalized),
                    json_text({"content": len(normalized)}),
                    json_text(metadata),
                    timestamp,
                    timestamp,
                ),
            )
            batch_id = int(cursor.lastrowid)
        conn.executemany(
            """INSERT INTO source_record
               (id,batch_id,category,title,summary,content_json,evidence_type,source_refs_json,
                sensitivity,content_hash,status,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    item["id"],
                    batch_id,
                    item["category"],
                    item["title"],
                    item["summary"],
                    json_text(item["payload"]),
                    item["evidence"],
                    json_text(item["sources"]),
                    item["sensitivity"],
                    item["content_hash"],
                    "ACTIVE",
                    timestamp,
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
                PUBLIC_COLLECTION_ID,
                "公开网页海外舆情知识库",
                "保存公开网页试采样本的标题、短摘要、来源链接和证据边界",
                "VALIDATED",
                "hybrid_lexical_offline_feature_vector",
                "INTERNAL",
                "核心课题组",
                timestamp,
            ),
        )
        conn.execute(
            """INSERT INTO knowledge_version
               (id,collection_id,batch_id,version,status,entry_count,chunk_count,created_at,reviewed_at,notes)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                version_id,
                PUBLIC_COLLECTION_ID,
                batch_id,
                version_label,
                "VALIDATED",
                len(normalized),
                len(normalized),
                timestamp,
                timestamp,
                f"公开网页试采；快照 SHA-256 {snapshot_hash}；仅保存短摘要和来源链接",
            ),
        )
        chunks = []
        for item in normalized:
            text = _record_text(item["title"], item["summary"], item["payload"])
            chunks.append(
                (
                    f"chunk:{item['id']}",
                    version_id,
                    item["id"],
                    text,
                    json_text(tokenize(text)),
                    pack_vector(feature_vector(text)),
                    DIMENSIONS,
                    json_text({"category": item["category"], "evidence": item["evidence"], "topic": item["topic"]}),
                )
            )
        conn.executemany(
            """INSERT INTO knowledge_chunk
               (id,version_id,record_id,text,tokens_json,vector,dimensions,metadata_json)
               VALUES(?,?,?,?,?,?,?,?)""",
            chunks,
        )
        ensure_knowledge_chunk_constraint(conn)
        _write_audit(
            conn,
            "登记公开网页试采批次",
            PUBLIC_BATCH_CODE,
            f"登记 {len(normalized)} 条公开网页样本；快照 {snapshot_hash}",
        )
        return batch_summary(conn, PUBLIC_BATCH_CODE)


def import_public_demo(path: Path | str | None = None, db_path: Path | None = None) -> dict:
    """Idempotently import the public snapshot into ``db_path``."""

    with _IMPORT_LOCK:
        return _import_unlocked(path, db_path)


def _allowed_placeholders(allowed: tuple[str, ...] | None) -> tuple[str, tuple[str, ...]]:
    values = tuple(allowed or ("INTERNAL", "CONFIDENTIAL", "RESTRICTED"))
    return ",".join("?" for _ in values), values


def _public_item(row: dict[str, Any], batch_code: str = PUBLIC_BATCH_CODE) -> dict[str, Any]:
    raw = row.get("content") if isinstance(row.get("content"), dict) else {}
    interaction = raw.get("interaction")
    return {
        "record_id": row.get("id"),
        "id": raw.get("id"),
        "topic": raw.get("topic"),
        "topic_slug": _topic_slug(str(raw.get("topic") or "")),
        "platform": raw.get("platform"),
        "title": row.get("title") or raw.get("title"),
        "summary": row.get("summary") or raw.get("summary"),
        "author_or_channel": raw.get("author_or_channel") or "",
        "published_at": raw.get("published_at") or "",
        "collected_at": raw.get("collection_time") or "",
        "country_region": raw.get("country_region") or "",
        "language": raw.get("language") or "",
        "interaction": interaction if isinstance(interaction, dict) else {},
        "original_url": raw.get("original_url") or raw.get("source_url") or "",
        "source_url": raw.get("source_url") or raw.get("original_url") or "",
        "original_text": raw.get("original_text") or "",
        "translation_zh": raw.get("translation_zh"),
        "translation_status": raw.get("translation_status") or "NOT_CONFIGURED",
        "sentiment": raw.get("sentiment") or "NOT_RUN",
        "stance": raw.get("stance") or "NOT_RUN",
        "risk": raw.get("risk") or "NOT_RUN",
        "keywords": raw.get("keywords") or [],
        "themes": raw.get("themes") or [],
        "acquisition_method": raw.get("acquisition_method") or "",
        "access_status": raw.get("access_status") or "",
        "demo_label": raw.get("demo_label") or PUBLIC_DEMO_LABEL,
        "evidence_type": row.get("evidence_type") or "source_snapshot",
        "source_refs": row.get("source_refs") or [],
        "content_hash": row.get("content_hash") or "",
        "sensitivity": row.get("sensitivity") or "INTERNAL",
        "batch_code": batch_code,
    }


def _public_rows(conn, allowed: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    placeholders, values = _allowed_placeholders(allowed)
    rows = conn.execute(
        f"""SELECT sr.*,db.code AS batch_code FROM source_record sr
            JOIN dataset_batch db ON db.id=sr.batch_id
            WHERE db.code=? AND sr.sensitivity IN ({placeholders})
            ORDER BY sr.id""",
        (PUBLIC_BATCH_CODE, *values),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows_to_dicts(rows):
        row["content"] = json.loads(row.pop("content_json"))
        row["source_refs"] = json.loads(row.pop("source_refs_json"))
        result.append(row)
    return result


def public_demo_status(
    db_path: Path | None = None,
    allowed: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    init_db(db_path)
    with db(db_path) as conn:
        batch = batch_summary(conn, PUBLIC_BATCH_CODE)
        rows = _public_rows(conn, allowed)
    if not batch:
        return {
            "available": False,
            "batch": {},
            "record_count": 0,
            "topics": [],
            "platforms": [],
            "demo_label": PUBLIC_DEMO_LABEL,
        }
    topic_counts = Counter(str((row["content"] or {}).get("topic") or "未分类") for row in rows)
    platform_counts = Counter(str((row["content"] or {}).get("platform") or "未标注") for row in rows)
    metadata = batch.get("metadata") or {}
    return {
        "available": True,
        "batch": batch,
        "record_count": len(rows),
        "records": len(rows),
        "topic_count": len(topic_counts),
        "platform_count": len(platform_counts),
        "channel_count": len(metadata.get("platformAccessObservations") or []),
        "topics": [
            {"name": name, "slug": _topic_slug(name), "count": count}
            for name, count in topic_counts.most_common()
        ],
        "platforms": [{"name": name, "count": count} for name, count in platform_counts.most_common()],
        "collected_at": metadata.get("collectedAt") or batch.get("source_date") or "",
        "latest_collected_at": metadata.get("collectedAt") or batch.get("source_date") or "",
        "scope": metadata.get("scope") or "",
        "demo_label": metadata.get("demoLabel") or PUBLIC_DEMO_LABEL,
        "real_public_data": bool(metadata.get("realData")),
        "collection_method": metadata.get("collectionMethod") or "",
        "platform_access_observations": metadata.get("platformAccessObservations") or [],
        "collection_run": metadata.get("collectionRun") or {},
        "collection_summary": metadata.get("collectionSummary") or {},
        "collector": metadata.get("collector") or {},
        "limitations": metadata.get("limitations") or [],
    }


def public_demo_topics(
    db_path: Path | None = None,
    allowed: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    init_db(db_path)
    with db(db_path) as conn:
        rows = _public_rows(conn, allowed)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        raw = row["content"]
        grouped.setdefault(str(raw.get("topic") or "未分类"), []).append(row)
    items: list[dict[str, Any]] = []
    for topic, topic_rows in grouped.items():
        platforms = Counter(str((row["content"] or {}).get("platform") or "未标注") for row in topic_rows)
        countries = Counter(str((row["content"] or {}).get("country_region") or "未标注") for row in topic_rows)
        languages = Counter(str((row["content"] or {}).get("language") or "未标注") for row in topic_rows)
        dates = sorted(
            (str((row["content"] or {}).get("published_at") or "") for row in topic_rows),
            reverse=True,
        )
        collected_dates = sorted(
            (str((row["content"] or {}).get("collection_time") or "") for row in topic_rows),
            reverse=True,
        )
        items.append(
            {
                "name": topic,
                "slug": _topic_slug(topic),
                "count": len(topic_rows),
                "latest_published_at": dates[0] if dates else "",
                "collected_at": collected_dates[0] if collected_dates else "",
                "latest_collected_at": collected_dates[0] if collected_dates else "",
                "keywords": [topic],
                "demo_label": PUBLIC_DEMO_LABEL,
                "platforms": [{"name": name, "count": count} for name, count in platforms.most_common()],
                "countries": [{"name": name, "count": count} for name, count in countries.most_common()],
                "languages": [{"name": name, "count": count} for name, count in languages.most_common()],
                "analysis": {
                    "sentiment": "NOT_RUN",
                    "stance": "NOT_RUN",
                    "risk": "NOT_RUN",
                    "narrative": "当前仅聚合公开标题和短摘要，待人工或正式模型复核。",
                },
                "preview": [
                    _public_item(row)
                    for row in sorted(
                        topic_rows,
                        key=lambda row: str((row["content"] or {}).get("published_at") or ""),
                        reverse=True,
                    )[:3]
                ],
            }
        )
    return sorted(items, key=lambda item: (-int(item["count"]), str(item["name"])))


def public_demo_topic(
    topic: str,
    *,
    db_path: Path | None = None,
    allowed: tuple[str, ...] | None = None,
    query: str = "",
    limit: int = 200,
) -> dict[str, Any] | None:
    resolved = _resolve_topic(topic)
    if not resolved:
        return None
    init_db(db_path)
    with db(db_path) as conn:
        rows = _public_rows(conn, allowed)
    selected = [row for row in rows if str((row["content"] or {}).get("topic") or "") == resolved]
    query_value = str(query or "").strip().lower()
    if query_value:
        selected = [
            row
            for row in selected
            if query_value in " ".join(
                str((row["content"] or {}).get(key) or "")
                for key in ("title", "summary", "platform", "author_or_channel", "country_region")
            ).lower()
        ]
    selected.sort(
        key=lambda row: (
            str((row["content"] or {}).get("published_at") or ""),
            str(row.get("id") or ""),
        ),
        reverse=True,
    )
    topic_items = [_public_item(row) for row in selected[: max(1, min(int(limit), 500))]]
    platforms = Counter(str((item.get("platform") or "未标注")) for item in topic_items)
    countries = Counter(str((item.get("country_region") or "未标注")) for item in topic_items)
    collected_dates = sorted((str(item.get("collected_at") or "") for item in topic_items), reverse=True)
    return {
        "name": resolved,
        "slug": _topic_slug(resolved),
        "count": len(selected),
        "returned_count": len(topic_items),
        "collected_at": collected_dates[0] if collected_dates else "",
        "latest_collected_at": collected_dates[0] if collected_dates else "",
        "keywords": [resolved],
        "demo_label": PUBLIC_DEMO_LABEL,
        "scope": "公开网页与公开 RSS 元数据",
        "query": query,
        "platforms": [{"name": name, "count": count} for name, count in platforms.most_common()],
        "countries": [{"name": name, "count": count} for name, count in countries.most_common()],
        "analysis": {
            "sentiment": "NOT_RUN",
            "stance": "NOT_RUN",
            "risk": "NOT_RUN",
            "narrative": "当前仅聚合公开标题和短摘要，待人工或正式模型复核。",
        },
        "items": topic_items,
    }

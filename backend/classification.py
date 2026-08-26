from __future__ import annotations

import json
from pathlib import Path

from .database import ROOT, db, init_db, now_iso, rows_to_dicts


DATA_PATH = ROOT / "src" / "classification_data.json"

# 层级 -> 敏感级（Excel：第一层最高敏感级，仅少数授权；二三层全所实名开放）
LAYER_SENSITIVITY = {
    "第一层": "最高敏感级",
    "第二层A": "实名开放",
    "第二层B": "实名开放",
    "第三层": "实名开放",
}

ROLE_SENSITIVITY = {
    "core": ("最高敏感级", "实名开放"),
    "researcher": ("实名开放",),
}

_CLASS_TABLES = """
CREATE TABLE monitor_object (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    account TEXT NOT NULL DEFAULT '',
    object_type TEXT NOT NULL,
    layer TEXT NOT NULL,
    sensitivity TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    organization TEXT NOT NULL DEFAULT '',
    influence TEXT NOT NULL DEFAULT '',
    anomaly_flags_json TEXT NOT NULL DEFAULT '[]',
    source_refs_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_monitor_object_layer ON monitor_object(layer);
CREATE TABLE opinion_item (
    id TEXT PRIMARY KEY,
    object_id TEXT NOT NULL REFERENCES monitor_object(id) ON DELETE CASCADE,
    topic TEXT NOT NULL,
    sentiment TEXT NOT NULL,
    risk_grade TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    published_at TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    evidence_type TEXT NOT NULL DEFAULT 'source_snapshot',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_opinion_item_object ON opinion_item(object_id);
CREATE INDEX IF NOT EXISTS idx_opinion_item_topic ON opinion_item(topic);
"""


def _visible_sensitivity(role: str) -> tuple[str, ...]:
    return ROLE_SENSITIVITY.get(role, ROLE_SENSITIVITY["researcher"])


def load_classification(data_path: Path | None = None) -> dict:
    init_db()
    path = Path(data_path or DATA_PATH)
    raw = json.loads(path.read_text(encoding="utf-8"))
    objects = raw.get("objects", [])
    items = raw.get("items", [])
    ts = now_iso()
    with db() as conn:
        conn.execute("DROP TABLE IF EXISTS opinion_item")
        conn.execute("DROP TABLE IF EXISTS monitor_object")
        conn.executescript(_CLASS_TABLES)
        conn.execute("DELETE FROM opinion_item")
        conn.execute("DELETE FROM monitor_object")
        for o in objects:
            conn.execute(
                """INSERT INTO monitor_object
                   (id,name,account,object_type,layer,sensitivity,title,organization,influence,
                    anomaly_flags_json,source_refs_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    o["id"], o["name"], o.get("account", ""), o.get("objectType", ""),
                    o["layer"], o.get("sensitivity", LAYER_SENSITIVITY.get(o["layer"], "实名开放")),
                    o.get("title", ""), o.get("organization", ""), o.get("influence", ""),
                    json.dumps(o.get("anomalyFlags", []), ensure_ascii=False),
                    json.dumps(o.get("sourceRefs", []), ensure_ascii=False), ts,
                ),
            )
        for it in items:
            conn.execute(
                """INSERT INTO opinion_item
                   (id,object_id,topic,sentiment,risk_grade,title,summary,published_at,source,evidence_type,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    it["id"], it["objectId"], it["topic"], it["sentiment"], it["riskGrade"],
                    it["title"], it.get("summary", ""), it.get("publishedAt", ""),
                    it.get("source", ""), it.get("evidence", "source_snapshot"), ts,
                ),
            )
    return {"objects": len(objects), "items": len(items)}


def list_objects(layer: str | None = None, role: str = "researcher") -> list[dict]:
    allowed = _visible_sensitivity(role)
    placeholders = ",".join("?" for _ in allowed)
    sql = f"SELECT * FROM monitor_object WHERE sensitivity IN ({placeholders})"
    args: list[object] = list(allowed)
    if layer:
        sql += " AND layer=?"
        args.append(layer)
    sql += " ORDER BY CASE layer WHEN '第一层' THEN 1 WHEN '第二层A' THEN 2 WHEN '第二层B' THEN 3 ELSE 4 END, name"
    with db() as conn:
        rows = rows_to_dicts(conn.execute(sql, args).fetchall())
    for row in rows:
        row["sourceRefs"] = json.loads(row.pop("source_refs_json"))
        row["anomalyFlags"] = json.loads(row.pop("anomaly_flags_json"))
    return rows


def overview(role: str = "researcher") -> dict:
    allowed = _visible_sensitivity(role)
    placeholders = ",".join("?" for _ in allowed)
    with db() as conn:
        objects = conn.execute(
            f"""SELECT layer,COUNT(*) AS count FROM monitor_object
                  WHERE sensitivity IN ({placeholders}) GROUP BY layer""",
            allowed,
        ).fetchall()
        total_objects = conn.execute(
            f"SELECT COUNT(*) FROM monitor_object WHERE sensitivity IN ({placeholders})",
            allowed,
        ).fetchone()[0]
        items = conn.execute(
            f"""SELECT oi.topic,oi.sentiment,oi.risk_grade
                  FROM opinion_item oi JOIN monitor_object mo ON mo.id=oi.object_id
                  WHERE mo.sensitivity IN ({placeholders})""",
            allowed,
        ).fetchall()
    topics: dict[str, int] = {}
    sentiments: dict[str, int] = {}
    risks: dict[str, int] = {}
    for topic, sentiment, risk in items:
        topics[topic] = topics.get(topic, 0) + 1
        sentiments[sentiment] = sentiments.get(sentiment, 0) + 1
        risks[risk] = risks.get(risk, 0) + 1
    return {
        "object_count": total_objects,
        "layers": {row["layer"]: row["count"] for row in objects},
        "topics": topics,
        "sentiments": sentiments,
        "risk_grades": risks,
        "item_count": sum(topics.values()),
    }


def list_items(
    *,
    role: str = "researcher",
    layer: str | None = None,
    topic: str | None = None,
    sentiment: str | None = None,
    risk_grade: str | None = None,
    limit: int = 200,
) -> list[dict]:
    allowed = _visible_sensitivity(role)
    placeholders = ",".join("?" for _ in allowed)
    sql = f"""SELECT oi.*,mo.name AS object_name,mo.account,mo.layer,mo.sensitivity
                FROM opinion_item oi JOIN monitor_object mo ON mo.id=oi.object_id
                WHERE mo.sensitivity IN ({placeholders})"""
    args: list[object] = list(allowed)
    if layer:
        sql += " AND mo.layer=?"
        args.append(layer)
    if topic:
        sql += " AND oi.topic=?"
        args.append(topic)
    if sentiment:
        sql += " AND oi.sentiment=?"
        args.append(sentiment)
    if risk_grade:
        sql += " AND oi.risk_grade=?"
        args.append(risk_grade)
    sql += " ORDER BY oi.published_at DESC LIMIT ?"
    args.append(limit)
    with db() as conn:
        return rows_to_dicts(conn.execute(sql, args).fetchall())

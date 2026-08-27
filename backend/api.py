from __future__ import annotations

import base64
import binascii
import json
import hashlib
import logging
import logging.handlers
import os
import secrets
import uuid
from collections import Counter
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .database import (
    ROOT,
    active_batch_code,
    append_audit_block,
    audit_checkpoint_matches,
    db,
    now_iso,
    require_audit_appendable,
    rows_to_dicts,
    verify_audit_chain,
)
from .graph import build_graph
from .ingest import BATCH_CODE, COLLECTION_ID, batch_summary, index_integrity, ingest_snapshot
from .public_demo import (
    PUBLIC_BATCH_CODE,
    TOPIC_SLUGS,
    public_demo_status,
    public_demo_topic,
    public_demo_topics,
    import_public_demo,
)
from .retrieval import CATEGORY_LABELS, answer, search
from .classification import list_items as classification_list_items
from .classification import list_objects as classification_list_objects
from .classification import load_classification
from .classification import overview as classification_overview
from .vendor import fetch_and_store, list_collected_items, list_vendors
from .semantic import semantic_engine
from .agent import qwen_agent
from .analysis_workflow import (
    HUMAN_DECISIONS,
    MACHINE_CANDIDATE,
    VERIFIED_DECISIONS,
    analysis_view,
    latest_reviews,
    queue_counts,
    run_machine_analysis,
    verified_report_summary,
)
from .features import pack_vector
from .semantic import MODELS as SEMANTIC_MODELS


RoleName = Literal["core", "researcher"]
GraphView = Literal["actors", "events", "propagation", "evidence"]

AUTH_MODE_ENV = "OPINION_MONITOR_AUTH_MODE"
BASIC_USERS_ENV = "OPINION_MONITOR_BASIC_USERS"
CORE_ONLY_ROUTES = (
    ("POST", "/api/collection/tasks", False),
    ("PATCH", "/api/collection/tasks/", True),
    ("POST", "/api/collection/fetch", False),
    ("POST", "/api/public-demo/refresh", False),
    ("POST", "/api/analysis/runs", False),
    ("PATCH", "/api/analysis/", True),
    ("PATCH", "/api/alerts/", True),
    ("POST", "/api/vectors/rebuild", False),
)


def _setup_logging() -> None:
    log_dir = os.environ.get("OPINION_MONITOR_LOG_DIR", "").strip()
    if not log_dir:
        return
    os.makedirs(log_dir, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        os.path.join(log_dir, "opinion-monitor.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    logging.getLogger("uvicorn.access").addHandler(handler)


def _auth_mode() -> str:
    return os.environ.get(AUTH_MODE_ENV, "off").strip().lower() or "off"


def _basic_users() -> tuple[dict[str, dict[str, str]], str | None]:
    """Load credentials at request time so secret rotation needs no code change."""
    raw_users = os.environ.get(BASIC_USERS_ENV, "").strip()
    if raw_users:
        try:
            parsed = json.loads(raw_users)
        except json.JSONDecodeError:
            return {}, f"{BASIC_USERS_ENV} 不是有效 JSON"
        if not isinstance(parsed, dict) or not parsed:
            return {}, f"{BASIC_USERS_ENV} 必须是非空对象"
        users: dict[str, dict[str, str]] = {}
        for username, entry in parsed.items():
            if not isinstance(username, str) or not username or not isinstance(entry, dict):
                return {}, f"{BASIC_USERS_ENV} 的账号配置无效"
            password = entry.get("password")
            role = entry.get("role", "researcher")
            if not isinstance(password, str) or not password or role not in {"core", "researcher"}:
                return {}, f"{BASIC_USERS_ENV} 的密码或角色配置无效"
            users[username] = {"password": password, "role": role}
        return users, None

    username = os.environ.get("OPINION_MONITOR_BASIC_USERNAME", "").strip()
    password = os.environ.get("OPINION_MONITOR_BASIC_PASSWORD", "")
    role = os.environ.get("OPINION_MONITOR_BASIC_ROLE", "researcher").strip().lower()
    if username and password and role in {"core", "researcher"}:
        return {username: {"password": password, "role": role}}, None
    return {}, (
        "Basic Auth 已启用，但未配置 OPINION_MONITOR_BASIC_USERS，"
        "或未完整配置单账号用户名、密码和角色"
    )


def _basic_identity(authorization: str, users: dict[str, dict[str, str]]) -> tuple[str, str] | None:
    try:
        scheme, encoded = authorization.split(" ", 1)
        if scheme.lower() != "basic":
            return None
        decoded = base64.b64decode(encoded.strip(), validate=True).decode("utf-8")
        username, password = decoded.split(":", 1)
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return None

    identity: tuple[str, str] | None = None
    for configured_username, entry in users.items():
        username_matches = secrets.compare_digest(username.encode("utf-8"), configured_username.encode("utf-8"))
        password_matches = secrets.compare_digest(password.encode("utf-8"), entry["password"].encode("utf-8"))
        if username_matches and password_matches:
            identity = (configured_username, entry["role"])
    return identity


def _core_only_route(method: str, path: str) -> bool:
    return any(
        method == expected_method and (path.startswith(route) if prefix else path == route)
        for expected_method, route, prefix in CORE_ONLY_ROUTES
    )


async def _requested_roles(request: Request) -> set[str]:
    roles = {value.lower() for value in request.query_params.getlist("role")}
    content_type = request.headers.get("content-type", "").lower()
    if request.method in {"POST", "PUT", "PATCH"} and "application/json" in content_type:
        raw_body = await request.body()
        if raw_body:
            try:
                payload = json.loads(raw_body)
            except (json.JSONDecodeError, UnicodeDecodeError):
                payload = None
            if isinstance(payload, dict) and isinstance(payload.get("role"), str):
                roles.add(payload["role"].lower())
    return roles


def _auth_response(status_code: int, detail: str, *, challenge: bool = False) -> JSONResponse:
    headers = {"Cache-Control": "no-store"}
    if challenge:
        headers["WWW-Authenticate"] = 'Basic realm="Opinion Monitor", charset="UTF-8"'
    return JSONResponse(status_code=status_code, content={"detail": detail}, headers=headers)


class RebuildVectorsRequest(BaseModel):
    model: str | None = Field(default=None, max_length=20)


class AgentPlanRequest(BaseModel):
    task: str = Field(min_length=1, max_length=2000)
    context: dict | None = None


class AgentExplainRequest(BaseModel):
    evidence: dict | None = None
    use_llm: bool = True


app = FastAPI(
    title="海外剧情监测系统 API",
    version="1.0.0",
    description="海外剧情监测系统 API，提供监测对象、海外舆情数据、知识库、图谱、研判与报告能力。",
)


@app.middleware("http")
async def production_authentication(request: Request, call_next):
    """Optional production perimeter auth; local demo mode remains unchanged."""
    mode = _auth_mode()
    if mode in {"off", "disabled", "demo"} or request.method == "OPTIONS":
        return await call_next(request)
    if mode != "basic":
        return _auth_response(503, f"不支持的认证模式：{mode}")

    users, config_error = _basic_users()
    if config_error:
        logging.getLogger(__name__).error("Production authentication configuration is invalid: %s", config_error)
        return _auth_response(503, "服务端认证配置无效，请联系管理员")
    identity = _basic_identity(request.headers.get("authorization", ""), users)
    if identity is None:
        return _auth_response(401, "需要有效的服务端账号", challenge=True)

    username, authenticated_role = identity
    requested_roles = await _requested_roles(request)
    if authenticated_role != "core" and (
        "core" in requested_roles or _core_only_route(request.method, request.url.path)
    ):
        return _auth_response(403, "当前账号没有核心课题组权限")
    request.state.authenticated_user = username
    request.state.authenticated_role = authenticated_role
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
_setup_logging()


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    role: RoleName = "researcher"
    top_k: int = Field(default=8, ge=1, le=20)
    category: str | None = Field(default=None, max_length=40)


class ChatRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    role: RoleName = "researcher"
    top_k: int = Field(default=5, ge=1, le=10)


class ReportRequest(BaseModel):
    template: Literal["validation", "person", "event", "topic", "weekly", "monthly", "trace"] = "validation"
    focus: str = Field(default="", max_length=500)
    role: RoleName = "researcher"


class TaskRequest(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    dimension: Literal["person", "account", "keyword", "hashtag"]
    target_value: str = Field(min_length=1, max_length=300)
    connector_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9-]+$")
    frequency: Literal["15m", "30m", "1h", "6h", "daily"] = "1h"
    history_days: int = Field(default=90, ge=1, le=90)
    media_types: list[Literal["text", "image", "video"]] = Field(default_factory=lambda: ["text"], min_length=1, max_length=3)
    languages: list[Literal["zh", "en", "es", "fr", "de", "ja", "ko", "ar", "ru", "pt"]] = Field(default_factory=lambda: ["zh", "en"], min_length=1, max_length=10)
    role: RoleName = "researcher"


class TaskAction(BaseModel):
    status: Literal["DRAFT", "PAUSED", "ARCHIVED"]
    role: RoleName = "researcher"


class AlertAction(BaseModel):
    status: Literal["PENDING", "ACKNOWLEDGED", "RESOLVED"]
    assignee: str = Field(default="", max_length=80)
    note: str = Field(default="", max_length=1000)
    role: RoleName = "researcher"


class VendorFetchRequest(BaseModel):
    dimension: Literal["person", "account", "keyword", "hashtag"] = "keyword"
    target: str = Field(min_length=1, max_length=300)
    platforms: list[str] = Field(default_factory=lambda: ["x"], min_length=1, max_length=12)
    media_types: list[Literal["text", "image", "video"]] = Field(default_factory=lambda: ["text"], min_length=1, max_length=3)
    languages: list[str] = Field(default_factory=lambda: ["zh"], min_length=1, max_length=10)
    vendor: str = "mock"


class PublicDemoRefreshRequest(BaseModel):
    role: RoleName = "researcher"


class AnalysisRunRequest(BaseModel):
    record_ids: list[str] = Field(default_factory=list, max_length=500)
    topic: str = Field(default="", max_length=120)
    role: RoleName = "researcher"


class HumanReviewRequest(BaseModel):
    decision: Literal["HUMAN_CONFIRMED", "HUMAN_REVISED", "HUMAN_REJECTED", "NEEDS_MORE_EVIDENCE"]
    review_note: str = Field(min_length=2, max_length=2000)
    human_sentiment: str = Field(default="", max_length=30)
    human_stance: str = Field(default="", max_length=80)
    human_risk_level: str = Field(default="", max_length=30)
    human_summary: str = Field(default="", max_length=2000)
    evidence_refs: list[str] = Field(default_factory=list, max_length=30)
    role: RoleName = "researcher"


def _json_fields(row: dict, fields: tuple[str, ...]) -> dict:
    for field in fields:
        value = row.pop(f"{field}_json", None)
        if value is not None:
            row[field] = json.loads(value)
    return row


def _allowed(role: str) -> tuple[str, ...]:
    access = {
        "researcher": ("INTERNAL", "CONFIDENTIAL"),
        "core": ("INTERNAL", "CONFIDENTIAL", "RESTRICTED"),
    }
    try:
        return access[role.lower()]
    except (AttributeError, KeyError) as exc:
        raise HTTPException(403, "未知角色，已拒绝访问") from exc


def _require_core(role: str) -> None:
    if role != "core":
        raise HTTPException(403, "该操作仅限核心课题组演示角色")


def _session_identity(request: Request, requested_role: str, *, kind: str) -> tuple[str, str]:
    """Resolve actor and role from auth state; demo mode uses explicit demo identities."""
    authenticated_role = getattr(request.state, "authenticated_role", None)
    authenticated_user = getattr(request.state, "authenticated_user", None)
    if authenticated_role:
        return str(authenticated_user), str(authenticated_role)
    actor = "演示人工研判员" if kind == "review" else "演示分析操作员"
    return actor, requested_role


def _write_audit(conn, actor: str, action: str, object_type: str, object_id: str, detail: str, outcome: str = "SUCCESS") -> None:
    timestamp = now_iso()
    require_audit_appendable(conn)
    previous = conn.execute("SELECT trace_hash FROM audit_event ORDER BY id DESC LIMIT 1").fetchone()
    previous_hash = previous[0] if previous else ""
    trace = hashlib.sha256("|".join([previous_hash, timestamp, actor, action, object_type, object_id, detail, outcome]).encode("utf-8")).hexdigest()
    conn.execute(
        """INSERT INTO audit_event
           (event_time,actor,action,object_type,object_id,detail,outcome,trace_hash)
           VALUES(?,?,?,?,?,?,?,?)""",
        (timestamp, actor, action, object_type, object_id, detail, outcome, trace),
    )
    event_count = conn.execute("SELECT COUNT(*) FROM audit_event").fetchone()[0]
    conn.execute(
        """INSERT INTO audit_checkpoint(id,event_count,head_hash,updated_at) VALUES(1,?,?,?)
           ON CONFLICT(id) DO UPDATE SET event_count=excluded.event_count,
             head_hash=excluded.head_hash,updated_at=excluded.updated_at""",
        (event_count, trace, timestamp),
    )
    append_audit_block(
        conn,
        event_type=action,
        actor=actor,
        object_type=object_type,
        object_id=str(object_id),
        detail=detail,
        outcome=outcome,
        batch_id=active_batch_code(conn) or BATCH_CODE,
    )


def _record_rows(category: str | None = None, role: str = "researcher") -> list[dict]:
    allowed = _allowed(role)
    placeholders = ",".join("?" for _ in allowed)
    with db() as conn:
        current_batch_id = _latest_batch_id(conn)
        if not current_batch_id:
            return []
        sql = f"""SELECT sr.* FROM source_record sr
                  WHERE sr.batch_id=? AND sr.sensitivity IN ({placeholders})"""
        args: list[object] = [current_batch_id, *allowed]
        if category:
            sql += " AND category=?"
            args.append(category)
        sql += " ORDER BY category,id"
        rows = rows_to_dicts(conn.execute(sql, args).fetchall())
    return [_json_fields(row, ("content", "source_refs")) for row in rows]


def _batch_view(conn, code: str = BATCH_CODE, role: str = "researcher") -> dict:
    result = batch_summary(conn, code)
    if not result or role == "core":
        return result
    allowed = _allowed(role)
    placeholders = ",".join("?" for _ in allowed)
    batch_id = result["id"]
    visible_categories = {
        row["category"]: row["count"]
        for row in conn.execute(
            f"""SELECT category,COUNT(*) AS count FROM source_record
                  WHERE batch_id=? AND sensitivity IN ({placeholders}) GROUP BY category""",
            (batch_id, *allowed),
        ).fetchall()
    }
    quality_count = conn.execute(
        f"""SELECT COUNT(*) FROM quality_issue qi JOIN source_record sr ON sr.id=qi.record_id
              WHERE qi.batch_id=? AND sr.sensitivity IN ({placeholders})""",
        (batch_id, *allowed),
    ).fetchone()[0]
    metadata = result.get("metadata", {})
    return {
        **result,
        "subject": "受限监测数据（详细主题已隐藏）",
        "checksum": "",
        "source_files": [],
        "record_count": sum(visible_categories.values()),
        "category_counts": visible_categories,
        "metadata": {"dataStatus": metadata.get("dataStatus", "sample_only"), "redacted": True},
        "quality_issue_count": quality_count,
        "hidden_restricted": result.get("record_count", 0) - sum(visible_categories.values()),
    }


def _latest_batch_code(conn) -> str:
    """Return the most recently updated complete batch for workspace views."""
    return active_batch_code(conn) or BATCH_CODE


def _latest_batch_id(conn) -> int | None:
    """Return the database id for the batch used by the current workbench view."""
    code = active_batch_code(conn)
    row = conn.execute("SELECT id FROM dataset_batch WHERE code=?", (code,)).fetchone() if code else None
    return int(row["id"]) if row else None


@app.on_event("startup")
def startup() -> None:
    ingest_snapshot()
    try:
        load_classification()
    except FileNotFoundError:
        pass


@app.get("/api/health")
def health() -> dict:
    with db() as conn:
        batch = conn.execute("SELECT COUNT(*) FROM dataset_batch").fetchone()[0]
        records = conn.execute("SELECT COUNT(*) FROM source_record").fetchone()[0]
        chunks = conn.execute("SELECT COUNT(*) FROM knowledge_chunk").fetchone()[0]
        integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
        index_state = index_integrity(conn)
        wal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        audit_chain = verify_audit_chain(conn)
    healthy = integrity == "ok" and index_state["complete"] and records > 0 and batch > 0
    return {
        "status": "ok" if healthy else "degraded",
        "ready": healthy,
        "database": "sqlite-local",
        "database_integrity": integrity,
        "journal_mode": wal,
        "index_complete": index_state["complete"],
        "index_issue_count": len(index_state["issues"]),
        "batches": batch,
        "records": records,
        "knowledge_chunks": chunks,
        "models": {"provider": "qwen", "available": bool(os.getenv("DASHSCOPE_API_KEY", "").strip()), "supported": list(SEMANTIC_MODELS.keys())},
        "embedding_store": "sqlite-float32-blob",
        "graph_engine": "networkx",
        "audit_chain": audit_chain,
        "deploy": {"wsgi": "uvicorn-asgi", "worker": "single-process", "db": "sqlite-wal"},
        "authentication": {"mode": _auth_mode(), "enabled": _auth_mode() == "basic"},
        "version": app.version,
    }


@app.get("/api/overview")
def overview(role: RoleName = "researcher") -> dict:
    allowed = _allowed(role)
    placeholders = ",".join("?" for _ in allowed)
    with db() as conn:
        summary = _batch_view(conn, code=_latest_batch_code(conn), role=role)
        current_batch_id = int(summary.get("id", 0) or 0)
        quality = conn.execute(
            f"""SELECT COUNT(*) FROM quality_issue qi JOIN source_record sr ON sr.id=qi.record_id
                  WHERE qi.status='OPEN' AND sr.batch_id=? AND sr.sensitivity IN ({placeholders})""",
            (current_batch_id, *allowed),
        ).fetchone()[0]
        chunks = conn.execute(
            f"""SELECT COUNT(*) FROM knowledge_chunk kc JOIN source_record sr ON sr.id=kc.record_id
                  WHERE sr.batch_id=? AND sr.sensitivity IN ({placeholders})""",
            (current_batch_id, *allowed),
        ).fetchone()[0]
        connectors = conn.execute("SELECT COUNT(*) FROM connector_registry").fetchone()[0]
        ready = conn.execute("SELECT COUNT(*) FROM connector_registry WHERE status='READY'").fetchone()[0]
        evidence = rows_to_dicts(conn.execute(
            f"""SELECT evidence_type,COUNT(*) AS count FROM source_record
                  WHERE batch_id=? AND sensitivity IN ({placeholders}) GROUP BY evidence_type ORDER BY count DESC""",
            (current_batch_id, *allowed),
        ).fetchall())
    categories = summary.get("category_counts", {})
    mode = "PUBLIC_WEB_SAMPLE" if summary.get("code") == PUBLIC_BATCH_CODE else "TEST_DATA"
    return {
        "mode": mode,
        "batch": summary,
        "metrics": {
            "records": summary.get("record_count", 0),
            "categories": len(categories),
            "accounts": categories.get("account", 0),
            "content": categories.get("content", 0),
            "events": categories.get("event", 0),
            "quality_open": quality,
            "knowledge_chunks": chunks,
            "connectors_total": connectors,
            "connectors_ready": ready,
            "hidden_restricted": summary.get("hidden_restricted", 0),
        },
        "evidence_distribution": evidence,
        "notice": "数据已同步更新，以下为当前监测结果。" if mode == "TEST_DATA" else "当前展示公开网页试采样本；翻译、情感、立场和风险字段待正式模型或人工复核。",
    }


@app.get("/api/datasets")
def datasets(role: RoleName = "researcher") -> dict:
    with db() as conn:
        codes = [row["code"] for row in conn.execute("SELECT code FROM dataset_batch ORDER BY created_at DESC").fetchall()]
        items = [_batch_view(conn, code, role) for code in codes]
    return {"items": items}


@app.get("/api/datasets/{code}")
def dataset_detail(code: str, role: RoleName = "researcher") -> dict:
    with db() as conn:
        payload = _batch_view(conn, code, role)
    if not payload:
        raise HTTPException(404, "数据批次不存在")
    return payload


@app.get("/api/datasets/{code}/records")
def dataset_records(
    code: str,
    category: str | None = Query(default=None, max_length=40),
    q: str = Query(default="", max_length=300),
    role: RoleName = "researcher",
    limit: int = Query(default=200, ge=1, le=500),
) -> dict:
    allowed = _allowed(role)
    placeholders = ",".join("?" for _ in allowed)
    sql = f"""SELECT sr.* FROM source_record sr JOIN dataset_batch db ON db.id=sr.batch_id
              WHERE db.code=? AND sr.sensitivity IN ({placeholders})"""
    args: list[object] = [code, *allowed]
    if category:
        sql += " AND sr.category=?"
        args.append(category)
    if q.strip():
        sql += " AND (sr.title LIKE ? OR sr.summary LIKE ?)"
        pattern = f"%{q.strip()}%"
        args.extend([pattern, pattern])
    sql += " ORDER BY sr.category,sr.id LIMIT ?"
    args.append(limit)
    with db() as conn:
        rows = rows_to_dicts(conn.execute(sql, args).fetchall())
    items = [_json_fields(row, ("content", "source_refs")) for row in rows]
    return {"items": items, "count": len(items), "category_labels": CATEGORY_LABELS}


@app.get("/api/records/{record_id}")
def record_detail(record_id: str, role: RoleName = "researcher") -> dict:
    allowed = _allowed(role)
    placeholders = ",".join("?" for _ in allowed)
    with db() as conn:
        row = conn.execute(f"SELECT * FROM source_record WHERE id=? AND sensitivity IN ({placeholders})", (record_id, *allowed)).fetchone()
    if not row:
        raise HTTPException(404, "记录不存在或无权查看")
    return _json_fields(dict(row), ("content", "source_refs"))


@app.get("/api/targets")
def targets(role: RoleName = "researcher") -> dict:
    actors = _record_rows("actor", role)
    accounts = _record_rows("account", role)
    account_by_name = {str(item["content"].get("name", "")).lower(): item for item in accounts}
    with db() as conn:
        current_batch_id = _latest_batch_id(conn)
        source_date = conn.execute("SELECT source_date FROM dataset_batch WHERE id=?", (current_batch_id,)).fetchone() if current_batch_id else None
        total_actors = conn.execute(
            "SELECT COUNT(*) FROM source_record WHERE batch_id=? AND category='actor'", (current_batch_id,)
        ).fetchone()[0] if current_batch_id else 0
    items = []
    for actor in actors:
        raw = actor["content"]
        account = account_by_name.get(str(raw.get("name", "")).lower())
        account_raw = account["content"] if account else {}
        items.append({
            "id": actor["id"],
            "name": raw.get("nameZh") or raw.get("name") or actor["title"],
            "name_en": raw.get("name") or "",
            "relation": raw.get("relation") or "",
            "role": raw.get("role") or "",
            "handle": account_raw.get("handle") or "",
            "followers": account_raw.get("followersRaw") or "未提供",
            "themes": account_raw.get("themes") or [],
            "sensitivity": actor["sensitivity"],
            "evidence": actor["evidence_type"],
            "source_date": source_date[0] if source_date else "",
        })
    return {"items": items, "count": len(items), "hidden_restricted": total_actors - len(items) if role == "researcher" else 0}


@app.get("/api/collection")
def collection(role: RoleName = "researcher") -> dict:
    allowed = _allowed(role)
    placeholders = ",".join("?" for _ in allowed)
    with db() as conn:
        connectors = rows_to_dicts(conn.execute("SELECT * FROM connector_registry ORDER BY channel_type,name").fetchall())
        batch = _batch_view(conn, code=_latest_batch_code(conn), role=role)
        tasks = rows_to_dicts(conn.execute("SELECT * FROM collection_task ORDER BY id DESC").fetchall()) if role == "core" else []
        current_batch_id = int(batch.get("id", 0) or 0)
        chunks = conn.execute(
            f"""SELECT COUNT(*) FROM knowledge_chunk kc JOIN source_record sr ON sr.id=kc.record_id
                  WHERE sr.batch_id=? AND sr.sensitivity IN ({placeholders})""",
            (current_batch_id, *allowed),
        ).fetchone()[0]
        index_state = index_integrity(conn, batch_id=current_batch_id or None)
    for task in tasks:
        task["media_types"] = json.loads(task.pop("media_types_json"))
        task["languages"] = json.loads(task.pop("languages_json"))
    pipeline = [
        {"id": "register", "name": "源文件登记", "status": "COMPLETED", "value": len(batch.get("source_files", [])), "unit": "份文件"},
        {"id": "normalize", "name": "结构化分类", "status": "COMPLETED", "value": batch.get("record_count", 0), "unit": "条记录"},
        {"id": "quality", "name": "质量扫描", "status": "COMPLETED", "value": batch.get("quality_issue_count", 0), "unit": "项待处理"},
        {"id": "index", "name": "知识索引", "status": "COMPLETED" if index_state["complete"] and chunks == batch.get("record_count", 0) else "INCOMPLETE", "value": chunks, "unit": "个向量"},
        {"id": "graph", "name": "关系建图", "status": "COMPLETED", "value": 4, "unit": "个视图"},
    ]
    public_status = public_demo_status(allowed=allowed)
    return {
        "connectors": connectors,
        "batch": batch,
        "pipeline": pipeline,
        "tasks": tasks,
        "public_demo": public_status,
        "notice": "公开网页试采样本已接入；12个平台连接器仍按授权状态展示，不将样本快照伪装为持续在线采集。",
    }


@app.post("/api/collection/tasks")
def create_collection_task(request: TaskRequest) -> dict:
    _require_core(request.role)
    timestamp = now_iso()
    with db() as conn:
        connector = conn.execute("SELECT id,status FROM connector_registry WHERE id=?", (request.connector_id,)).fetchone()
        if not connector:
            raise HTTPException(400, "连接器不存在")
        cursor = conn.execute(
            """INSERT INTO collection_task
               (name,dimension,target_value,connector_id,frequency,history_days,
                media_types_json,languages_json,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                request.name, request.dimension, request.target_value, request.connector_id,
                request.frequency, request.history_days, json.dumps(request.media_types, ensure_ascii=False),
                json.dumps(request.languages, ensure_ascii=False), "DRAFT", timestamp, timestamp,
            ),
        )
        task_id = cursor.lastrowid
        _write_audit(conn, "演示操作员", "创建采集任务草稿", "collection_task", str(task_id), f"{request.dimension}:{request.target_value}")
        row = dict(conn.execute("SELECT * FROM collection_task WHERE id=?", (task_id,)).fetchone())
    row["media_types"] = json.loads(row.pop("media_types_json"))
    row["languages"] = json.loads(row.pop("languages_json"))
    row["execution_notice"] = "连接器尚未配置，任务已保存为草稿，不会伪装为运行中。"
    return row


@app.patch("/api/collection/tasks/{task_id}")
def update_collection_task(task_id: int, request: TaskAction) -> dict:
    _require_core(request.role)
    with db() as conn:
        row = conn.execute("SELECT * FROM collection_task WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(404, "采集任务不存在")
        conn.execute("UPDATE collection_task SET status=?,updated_at=? WHERE id=?", (request.status, now_iso(), task_id))
        _write_audit(conn, "演示操作员", "更新采集任务状态", "collection_task", str(task_id), request.status)
    return {"id": task_id, "status": request.status}


@app.get("/api/collection/vendors")
def collection_vendors() -> dict:
    return {"items": list_vendors()}


@app.post("/api/collection/fetch")
def collection_fetch(request: VendorFetchRequest) -> dict:
    try:
        result = fetch_and_store(
            {
                "dimension": request.dimension,
                "target": request.target,
                "platforms": request.platforms,
                "media_types": request.media_types,
                "languages": request.languages,
            },
            vendor_name=request.vendor,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return result


@app.get("/api/collection/items")
def collection_items(
    platform: str | None = Query(default=None, max_length=20),
    media_type: str | None = Query(default=None, max_length=10),
    limit: int = Query(default=200, ge=1, le=500),
) -> dict:
    items = list_collected_items(platform=platform, media_type=media_type, limit=limit)
    return {"items": items, "count": len(items)}


@app.get("/api/public-demo/status")
def public_demo_status_api(role: RoleName = "researcher") -> dict:
    """Return provenance and availability for the public-web demo batch."""

    return public_demo_status(allowed=_allowed(role))


@app.get("/api/public-demo/topics")
def public_demo_topics_api(role: RoleName = "researcher") -> dict:
    """Return the three configured demo topics with aggregate counters."""

    allowed = _allowed(role)
    items = public_demo_topics(allowed=allowed)
    status = public_demo_status(allowed=allowed)
    return {
        "items": items,
        "count": len(items),
        "topics": items,
        "records": items,
        "status": status,
        # Keep provenance at the top level for clients that do not unwrap the
        # status object before rendering the public-sample notice.
        "platform_access_observations": status.get("platform_access_observations", []),
    }


@app.get("/api/public-demo/topics/{topic:path}")
def public_demo_topic_api(
    topic: str,
    q: str = Query(default="", max_length=300),
    role: RoleName = "researcher",
    limit: int = Query(default=200, ge=1, le=500),
) -> dict:
    result = public_demo_topic(topic, allowed=_allowed(role), query=q, limit=limit)
    if result is None:
        raise HTTPException(404, "重点专题不存在")
    # Keep both names so older and newer workbench builds can consume the same
    # endpoint without a migration window.
    result["records"] = result.get("items", [])
    result["topics"] = [
        {
            "name": result.get("name"),
            "slug": result.get("slug"),
            "count": result.get("count", 0),
        }
    ]
    return result


@app.post("/api/public-demo/refresh")
def public_demo_refresh(request: PublicDemoRefreshRequest) -> dict:
    """Re-import the checked-in public snapshot; only the core role may mutate."""

    _require_core(request.role)
    try:
        batch = import_public_demo()
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(500, f"公开网页样本导入失败：{exc}") from exc
    return {
        "status": "IMPORTED",
        "batch": batch,
        "public_demo": public_demo_status(allowed=_allowed(request.role)),
    }


@app.get("/api/analysis")
def analysis(role: RoleName = "researcher", topic_id: str = "") -> dict:
    records = _record_rows(role=role)
    topics: Counter[str] = Counter()
    evidence: Counter[str] = Counter()
    risks: Counter[str] = Counter()
    for record in records:
        raw = record["content"]
        evidence[record["evidence_type"]] += 1
        if record["category"] in {"account", "content"}:
            topics.update(raw.get("themes") or [])
        elif record["category"] == "analysis":
            topics.update(raw.get("topics") or [])
        if record["category"] == "event" and raw.get("risk"):
            risks[str(raw["risk"])] += 1
    with db() as conn:
        public_mode = _latest_batch_code(conn) == PUBLIC_BATCH_CODE
    with db() as conn:
        topic = TOPIC_SLUGS.get(topic_id, topic_id)
        record_ids = [str(row["id"]) for row in _analysis_records(conn, role=role, topic=topic)]
        workflow_counts = queue_counts(conn, record_ids)
        workflow_report = verified_report_summary(conn, record_ids)
        analysis_rows = rows_to_dicts(conn.execute(
            "SELECT id FROM machine_analysis WHERE record_id IN (" + ",".join("?" for _ in record_ids) + ")",
            record_ids,
        ).fetchall()) if record_ids else []
        reviews = latest_reviews(conn, [str(item["id"]) for item in analysis_rows])
        review_counts = Counter(str(item["decision"]) for item in reviews.values())
        machine_count = sum(workflow_counts.values())
    return {
        "topics": [{"name": name, "count": count} for name, count in topics.most_common(18)],
        "evidence": [{"type": name, "count": count} for name, count in evidence.most_common()],
        "risks": [{"level": name, "count": count} for name, count in risks.most_common()],
        "sentiment": {
            "status": "MACHINE_CANDIDATES" if machine_count else "NOT_RUN",
            "reason": (
                "已生成可审计机器候选，正式结论仍须人工确认；当前未配置经评测的大模型时使用规则基线。"
                if machine_count and public_mode
                else "公开网页样本尚未运行机器分析；本版不把媒体表述自动包装成情感结论。"
                if public_mode
                else "ZIP 未提供人工情感真值；本版不将源文件作者语气判断伪装成模型结论。"
            ),
        },
        "notice": (
            "主题统计来自公开样本的专题标签，需结合原始链接和正式模型或人工复核。"
            if public_mode
            else "主题统计来自源文件标签，仍需结合原始帖子和正式模型复核。"
        ),
        "workflow": {
            "machine_status_counts": workflow_counts,
            "machine_count": machine_count,
            "pending_human_review": max(0, machine_count - len(reviews)),
            "human_confirmed": review_counts.get("HUMAN_CONFIRMED", 0) + review_counts.get("HUMAN_REVISED", 0),
            "verified_count": workflow_report["verified_count"],
            "excluded_count": workflow_report["excluded_count"],
            "policy": "机器结果仅为候选；正式研判仅纳入 HUMAN_CONFIRMED 或 HUMAN_REVISED。",
        },
    }


def _analysis_records(conn, *, role: str, record_ids: list[str] | None = None, topic: str = "") -> list[dict]:
    current_batch_id = _latest_batch_id(conn)
    if not current_batch_id:
        return []
    allowed = _allowed(role)
    placeholders = ",".join("?" for _ in allowed)
    rows = rows_to_dicts(conn.execute(
        f"""SELECT * FROM source_record WHERE batch_id=? AND sensitivity IN ({placeholders})
               ORDER BY id""",
        (current_batch_id, *allowed),
    ).fetchall())
    requested = {value for value in (record_ids or []) if value}
    topic_value = topic.strip().lower()
    selected = []
    for row in rows:
        if requested and str(row["id"]) not in requested:
            continue
        if topic_value:
            content = json.loads(row["content_json"])
            labels = [str(content.get(key) or "") for key in ("topic", "themes", "keywords")]
            if not any(topic_value in label.lower() for label in labels):
                continue
        selected.append(row)
    return selected


def _analysis_response_view(row: dict, review: dict | None = None) -> dict:
    viewed = analysis_view(dict(row))
    status = str(viewed.get("status") or "")
    workflow = str(review.get("decision")) if review else (
        "PENDING_HUMAN_REVIEW" if status == MACHINE_CANDIDATE else status
    )
    viewed.update({
        "workflow": workflow,
        "machine_reason": viewed.get("narrative", ""),
        "machine_sentiment": viewed.get("sentiment", ""),
        "machine_stance": viewed.get("stance", ""),
        "machine_risk_level": viewed.get("risk_level", ""),
        "evidence_snippet": (viewed.get("evidence_snippets") or [""])[0],
        "review": review,
    })
    return viewed


@app.post("/api/analysis/runs")
def create_analysis_run(payload: AnalysisRunRequest, request: Request) -> dict:
    actor, effective_role = _session_identity(request, payload.role, kind="run")
    _require_core(effective_role)
    with db() as conn:
        resolved_topic = TOPIC_SLUGS.get(payload.topic, payload.topic)
        records = _analysis_records(conn, role=effective_role, record_ids=payload.record_ids, topic=resolved_topic)
        if not records:
            raise HTTPException(404, "当前活动批次中没有匹配的可分析记录")
        scope = ({"type": "records", "record_ids": payload.record_ids} if payload.record_ids else {"type": "topic", "topic": resolved_topic, "topic_id": payload.topic} if payload.topic else {"type": "active_batch"})
        result = run_machine_analysis(conn, records, actor=actor, agent=qwen_agent, scope=scope)
        run = rows_to_dicts(conn.execute("SELECT * FROM analysis_run WHERE id=?", (result["run_id"],)).fetchall())[0]
        parameters = json.loads(run.get("parameters_json") or "{}")
        run["scope"] = parameters.get("scope", {})
        run["topic_name"] = run["scope"].get("topic", "")
        _write_audit(
            conn, actor, "运行机器研判", "analysis_run", result["run_id"],
            f"引擎={result['engine']['engine_version']}；新建={result['created_count']}；幂等跳过={result['skipped_count']}；失败={result['failed_count']}",
            "SUCCESS" if not result["failures"] else "PARTIAL",
        )
    return {"run": run, "analyses": result["created"], **{key: value for key, value in result.items() if key not in {"run_id", "created"}}}


@app.get("/api/analysis/runs")
def analysis_runs(request: Request, role: RoleName = "researcher") -> dict:
    _, effective_role = _session_identity(request, role, kind="run")
    with db() as conn:
        rows = rows_to_dicts(conn.execute("SELECT * FROM analysis_run ORDER BY started_at DESC,id DESC LIMIT 200").fetchall())
        for row in rows:
            parameters = json.loads(row.get("parameters_json") or "{}")
            row["scope"] = parameters.get("scope", {})
            row["topic_name"] = row["scope"].get("topic", "")
    return {"runs": rows, "count": len(rows), "role": effective_role}


@app.get("/api/analysis/queue")
def analysis_queue(request: Request, role: RoleName = "researcher") -> dict:
    _, effective_role = _session_identity(request, role, kind="review")
    with db() as conn:
        records = _analysis_records(conn, role=effective_role)
        record_ids = [str(item["id"]) for item in records]
        if not record_ids:
            return {"items": [], "counts": {}}
        placeholders = ",".join("?" for _ in record_ids)
        analyses = rows_to_dicts(conn.execute(
            f"""SELECT ma.*,sr.title,sr.summary,sr.evidence_type,sr.source_refs_json,sr.content_hash
                 FROM machine_analysis ma JOIN source_record sr ON sr.id=ma.record_id
                 WHERE ma.record_id IN ({placeholders}) ORDER BY ma.created_at DESC""", record_ids
        ).fetchall())
        reviews = latest_reviews(conn, [str(item["id"]) for item in analyses])
        all_items = []
        pending_items = []
        reviewed_items = []
        for item in analyses:
            original_record_id = item["record_id"]
            record = {
                "id": item.pop("record_id"), "title": item.pop("title"), "summary": item.pop("summary"),
                "evidence_type": item.pop("evidence_type"), "content_hash": item.pop("content_hash"),
                "source_refs": json.loads(item.pop("source_refs_json") or "[]"),
            }
            analysis_id = str(item["id"])
            review = reviews.get(analysis_id)
            viewed = _analysis_response_view(item, review)
            viewed["record_id"] = original_record_id
            entry = {"analysis": viewed, "record": record, "review": review}
            all_items.append(entry)
            if viewed["workflow"] == "PENDING_HUMAN_REVIEW":
                pending_items.append(entry)
            else:
                reviewed_items.append(entry)
        decision_counts = Counter(
            str(entry["review"]["decision"]) for entry in reviewed_items if entry["review"]
        )
        counts = {
            "machine_count": len(all_items),
            "pending_human_review": len(pending_items),
            "human_confirmed": decision_counts.get("HUMAN_CONFIRMED", 0),
            "human_revised": decision_counts.get("HUMAN_REVISED", 0),
            "human_rejected": decision_counts.get("HUMAN_REJECTED", 0),
            "needs_more_evidence": decision_counts.get("NEEDS_MORE_EVIDENCE", 0),
            "verified_count": decision_counts.get("HUMAN_CONFIRMED", 0) + decision_counts.get("HUMAN_REVISED", 0),
            "excluded_count": len(pending_items) + decision_counts.get("HUMAN_REJECTED", 0) + decision_counts.get("NEEDS_MORE_EVIDENCE", 0),
        }
        return {"items": pending_items, "analyses": all_items, "reviewed_items": reviewed_items, "counts": counts}


@app.get("/api/records/{record_id}/analysis")
def record_analysis(record_id: str, request: Request, role: RoleName = "researcher") -> dict:
    _, effective_role = _session_identity(request, role, kind="review")
    with db() as conn:
        records = _analysis_records(conn, role=effective_role, record_ids=[record_id])
        if not records:
            raise HTTPException(404, "记录不存在或当前无权访问")
        rows = rows_to_dicts(conn.execute(
            "SELECT * FROM machine_analysis WHERE record_id=? ORDER BY created_at DESC", (record_id,)
        ).fetchall())
        reviews = latest_reviews(conn, [str(item["id"]) for item in rows])
        rendered = [_analysis_response_view(row, reviews.get(str(row["id"]))) for row in rows]
        current = rendered[0] if rendered else None
        current_review = current.get("review") if current else None
        final_conclusion = None
        if current and current_review and current_review["decision"] in VERIFIED_DECISIONS:
            final_conclusion = {
                "workflow": current_review["decision"],
                "summary": current_review.get("narrative") or current.get("narrative") or "",
                "sentiment": current_review.get("sentiment") or current.get("sentiment") or "",
                "stance": current_review.get("stance") or current.get("stance") or "",
                "risk_level": current_review.get("risk_level") or current.get("risk_level") or "",
                "reviewer": current_review.get("reviewer") or "",
            }
    return {
        "record_id": record_id,
        "analyses": rendered,
        "current_analysis": current,
        "current_review": current_review,
        "final_conclusion": final_conclusion,
    }


@app.patch("/api/analysis/{analysis_id}/review")
def review_analysis(analysis_id: str, payload: HumanReviewRequest, request: Request) -> dict:
    actor, effective_role = _session_identity(request, payload.role, kind="review")
    _require_core(effective_role)
    if payload.decision not in HUMAN_DECISIONS:
        raise HTTPException(422, "不支持的人工研判决定")
    with db() as conn:
        row = conn.execute("SELECT * FROM machine_analysis WHERE id=?", (analysis_id,)).fetchone()
        if not row:
            raise HTTPException(404, "机器研判记录不存在")
        if row["status"] != MACHINE_CANDIDATE:
            raise HTTPException(409, "当前记录不是可供人工研判的机器候选")
        previous_review = conn.execute(
            "SELECT id FROM human_review WHERE machine_analysis_id=? LIMIT 1",
            (analysis_id,),
        ).fetchone()
        if previous_review:
            raise HTTPException(409, "该机器候选已经完成人工研判，不能重复提交")
        if payload.decision == "HUMAN_REVISED" and not any((
            payload.human_summary.strip(),
            payload.human_sentiment.strip(),
            payload.human_stance.strip(),
            payload.human_risk_level.strip(),
            payload.evidence_refs,
        )):
            raise HTTPException(422, "人工修订必须填写修订后的摘要、标签、风险或证据")
        records = _analysis_records(conn, role=effective_role, record_ids=[str(row["record_id"])])
        if not records:
            raise HTTPException(404, "对应源记录不在当前活动批次或无权访问")
        timestamp = now_iso()
        review_id = f"hr:{uuid.uuid4().hex}"
        conn.execute(
            """INSERT INTO human_review
               (id,machine_analysis_id,record_id,reviewer,decision,sentiment,stance,risk_level,narrative,evidence_refs_json,note,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (review_id, analysis_id, row["record_id"], actor, payload.decision, payload.human_sentiment,
             payload.human_stance, payload.human_risk_level, payload.human_summary, json.dumps(payload.evidence_refs, ensure_ascii=False),
             payload.review_note, timestamp, timestamp),
        )
        _write_audit(
            conn, actor, "人工研判决定", "machine_analysis", analysis_id,
            f"决定={payload.decision}；审核记录={review_id}", "SUCCESS",
        )
        review = rows_to_dicts(conn.execute("SELECT * FROM human_review WHERE id=?", (review_id,)).fetchall())[0]
        review["evidence_refs"] = json.loads(review.pop("evidence_refs_json") or "[]")
    return {
        "analysis_id": analysis_id,
        "workflow": payload.decision,
        "review": review,
        "verified": payload.decision in VERIFIED_DECISIONS,
    }


@app.get("/api/alerts")
def alerts(role: RoleName = "researcher") -> dict:
    allowed = _allowed(role)
    placeholders = ",".join("?" for _ in allowed)
    with db() as conn:
        current_batch_id = _latest_batch_id(conn)
        rows = conn.execute(
            f"""SELECT ac.*,sr.title,sr.summary,sr.content_json,sr.evidence_type,sr.source_refs_json
                  FROM alert_case ac JOIN source_record sr ON sr.id=ac.record_id
                  WHERE sr.batch_id=? AND sr.sensitivity IN ({placeholders}) ORDER BY ac.id""",
            (current_batch_id, *allowed),
        ).fetchall()
    items = []
    for row in rows:
        raw = json.loads(row["content_json"])
        items.append({
            "id": row["id"], "record_id": row["record_id"], "title": row["title"],
            "summary": row["summary"], "date": raw.get("date"), "risk": row["risk"],
            "evidence": row["evidence_type"], "sources": raw.get("sources") or json.loads(row["source_refs_json"]),
            "status": row["status"], "assignee": row["assignee"], "note": row["note"],
            "updated_at": row["updated_at"], "trigger": "规则命中",
        })
    with db() as conn:
        public_mode = _latest_batch_code(conn) == PUBLIC_BATCH_CODE
    return {
        "items": items,
        "count": len(items),
        "mode": "PUBLIC_WEB_SAMPLE" if public_mode else "TEST_DATA",
        "notice": (
            "公开样本尚未运行经评测的风险模型，当前没有自动生成风险结论。"
            if public_mode
            else "已生成规则命中线索，供人工核验与处置。"
        ),
    }


@app.patch("/api/alerts/{alert_id}")
def update_alert(alert_id: str, request: AlertAction) -> dict:
    _require_core(request.role)
    with db() as conn:
        current_batch_id = _latest_batch_id(conn)
        row = conn.execute(
            """SELECT ac.id FROM alert_case ac JOIN source_record sr ON sr.id=ac.record_id
                 WHERE ac.id=? AND sr.batch_id=?""",
            (alert_id, current_batch_id),
        ).fetchone()
        if not row:
            raise HTTPException(404, "预警记录不存在")
        timestamp = now_iso()
        conn.execute(
            "UPDATE alert_case SET status=?,assignee=?,note=?,updated_at=? WHERE id=?",
            (request.status, request.assignee, request.note, timestamp, alert_id),
        )
        _write_audit(conn, request.assignee or "演示操作员", "更新预警处置状态", "alert_case", alert_id, f"{request.status}:{request.note}")
    return {"id": alert_id, "status": request.status, "assignee": request.assignee, "note": request.note, "updated_at": timestamp}


@app.get("/api/knowledge/collections")
def knowledge_collections(role: RoleName = "researcher") -> dict:
    sql = """SELECT kc.*,kv.id AS version_id,kv.version,kv.status AS version_status,
                    kv.entry_count,kv.chunk_count,kv.created_at AS version_created_at,kv.notes
             FROM knowledge_collection kc LEFT JOIN knowledge_version kv ON kv.collection_id=kc.id
             WHERE kv.batch_id=?
             ORDER BY kc.updated_at DESC"""
    with db() as conn:
        current_batch_id = _latest_batch_id(conn)
        items = rows_to_dicts(conn.execute(sql, (current_batch_id,)).fetchall()) if current_batch_id else []
        if role != "core":
            allowed = _allowed(role)
            placeholders = ",".join("?" for _ in allowed)
            current_batch_id = _latest_batch_id(conn)
            visible_entries = conn.execute(
                f"SELECT COUNT(*) FROM source_record WHERE batch_id=? AND sensitivity IN ({placeholders})",
                (current_batch_id, *allowed),
            ).fetchone()[0] if current_batch_id else 0
            visible_chunks = conn.execute(
                f"""SELECT COUNT(*) FROM knowledge_chunk kc JOIN source_record sr ON sr.id=kc.record_id
                      WHERE sr.batch_id=? AND sr.sensitivity IN ({placeholders})""",
                (current_batch_id, *allowed),
            ).fetchone()[0] if current_batch_id else 0
            for item in items:
                item["entry_count"] = visible_entries
                item["chunk_count"] = visible_chunks
    return {"items": items}


@app.post("/api/knowledge/search")
def knowledge_search(request: SearchRequest) -> dict:
    return search(request.query, role=request.role, top_k=request.top_k, category=request.category)


@app.get("/api/knowledge/search")
def knowledge_search_get(
    q: str = Query(min_length=1, max_length=500),
    role: RoleName = "researcher",
    top_k: int = Query(default=8, ge=1, le=20),
    category: str | None = Query(default=None, max_length=40),
) -> dict:
    return search(q, role=role, top_k=top_k, category=category)


@app.get("/api/graph")
def knowledge_graph(view: GraphView = "actors", role: RoleName = "researcher") -> dict:
    return build_graph(view, role=role)


@app.get("/api/quality")
def quality(role: RoleName = "researcher") -> dict:
    allowed = _allowed(role)
    placeholders = ",".join("?" for _ in allowed)
    with db() as conn:
        current_batch_id = _latest_batch_id(conn)
        items = rows_to_dicts(conn.execute(
            f"""SELECT qi.* FROM quality_issue qi JOIN source_record sr ON sr.id=qi.record_id
                  WHERE sr.batch_id=? AND sr.sensitivity IN ({placeholders}) ORDER BY qi.severity DESC,qi.id""",
            (current_batch_id, *allowed),
        ).fetchall())
        counts = rows_to_dicts(conn.execute(
            f"""SELECT qi.issue_type,qi.severity,COUNT(*) AS count
                  FROM quality_issue qi JOIN source_record sr ON sr.id=qi.record_id
                  WHERE sr.batch_id=? AND sr.sensitivity IN ({placeholders})
                  GROUP BY qi.issue_type,qi.severity ORDER BY count DESC""",
            (current_batch_id, *allowed),
        ).fetchall())
    return {"items": items, "count": len(items), "distribution": counts}


@app.get("/api/audit")
def audit(role: RoleName = "researcher") -> dict:
    with db() as conn:
        ordered = rows_to_dicts(conn.execute("SELECT * FROM audit_event ORDER BY id").fetchall())
        checkpoint_valid = audit_checkpoint_matches(conn)
        block_chain = verify_audit_chain(conn)
    previous = ""
    verified = True
    for item in ordered:
        expected = hashlib.sha256("|".join([
            previous, item["event_time"], item["actor"], item["action"], item["object_type"],
            item["object_id"], item["detail"], item["outcome"],
        ]).encode("utf-8")).hexdigest()
        if expected != item["trace_hash"]:
            verified = False
        previous = item["trace_hash"]
    if not checkpoint_valid:
        verified = False
    items = list(reversed(ordered))
    if role != "core":
        items = [
            {
                **item,
                "actor": "已隐藏",
                "object_id": "已隐藏",
                "detail": "该审计事件详情仅限核心课题组查看",
            }
            for item in items
        ]
    return {"items": items, "count": len(items), "chain_verified": verified, "block_chain": block_chain, "details_redacted": role != "core"}


@app.get("/api/requirements")
def requirements() -> dict:
    with db() as conn:
        items = rows_to_dicts(conn.execute("SELECT * FROM requirement_feature ORDER BY id").fetchall())
    status = Counter(item["delivery_status"] for item in items)
    return {"items": items, "status": dict(status), "source": "社科院海外监测系统需求优先级.xlsx", "scope_note": "仅纳入需求表中的产品能力，非功能条目未进入系统模块。"}


@app.post("/api/chat")
def chat(request: ChatRequest) -> dict:
    return answer(request.query, role=request.role, top_k=request.top_k)


REPORT_TEMPLATES = [
    {"id": "validation", "name": "数据批次验证报告", "description": "检查分类、质量、索引、图谱和证据边界"},
    {"id": "person", "name": "人物画像报告", "description": "按人物聚合账号、内容、事件与来源"},
    {"id": "event", "name": "事件快报", "description": "按事件聚合时间、来源、风险与待核项"},
    {"id": "topic", "name": "议题研判", "description": "按主题标签聚合内容和关系节点"},
    {"id": "weekly", "name": "周报", "description": "按时间窗汇总事件和风险信号"},
    {"id": "monthly", "name": "月报", "description": "按月汇总对象、主题和质量变化"},
    {"id": "trace", "name": "来源溯源报告", "description": "展示记录到源文件的证据链"},
]


@app.get("/api/reports/templates")
def report_templates() -> dict:
    return {"items": REPORT_TEMPLATES}


@app.post("/api/reports/generate")
def generate_report(request: ReportRequest) -> dict:
    with db() as conn:
        batch = _batch_view(conn, code=_latest_batch_code(conn), role=request.role)
        current_batch_id = int(batch.get("id", 0) or 0)
        allowed = _allowed(request.role)
        placeholders = ",".join("?" for _ in allowed)
        quality_items = rows_to_dicts(conn.execute(
            f"""SELECT qi.severity,qi.title,qi.status FROM quality_issue qi
                  JOIN source_record sr ON sr.id=qi.record_id
                  WHERE sr.batch_id=? AND sr.sensitivity IN ({placeholders})
                  ORDER BY qi.severity DESC,qi.id LIMIT 8""",
            (current_batch_id, *allowed),
        ).fetchall())
        visible_record_ids = [str(row["id"]) for row in conn.execute(
            f"SELECT id FROM source_record WHERE batch_id=? AND sensitivity IN ({placeholders})",
            (current_batch_id, *allowed),
        ).fetchall()]
        workflow_summary = verified_report_summary(conn, visible_record_ids)
    analysis_payload = analysis(request.role)
    citations = []
    if request.focus.strip():
        citations = search(request.focus, role=request.role, top_k=6)["results"]
    template = next(item for item in REPORT_TEMPLATES if item["id"] == request.template)
    sections = [
        {"title": "批次说明", "content": f"{batch.get('name')}，用途为{batch.get('purpose')}。数据截至 {batch.get('source_date')}。"},
        {"title": "结构化概况", "content": f"共 {batch.get('record_count', 0)} 条记录、{len(batch.get('category_counts', {}))} 类数据；分类统计为 {json.dumps(batch.get('category_counts', {}), ensure_ascii=False)}。"},
        {"title": "主要主题", "content": "、".join(f"{item['name']}（{item['count']}）" for item in analysis_payload["topics"][:8]) or "未形成可用主题统计。"},
        {"title": "质量边界", "content": f"当前有 {batch.get('quality_issue_count', 0)} 项口径冲突或生产缺口，均保持待处理状态，未自动补全。"},
        {
            "title": "正式研判纳入规则",
            "content": (
                f"已纳入 {workflow_summary['verified_count']} 条人工确认或修订的研判；"
                f"排除 {workflow_summary['excluded_count']} 条未获人工确认、被驳回或待补证的机器候选。"
            ),
        },
    ]
    if workflow_summary["items"]:
        sections.append({
            "title": "已核验研判摘要",
            "content": "；".join(
                f"{item['record_id']}（{item['decision']}，{item['risk_level']}）：{item['narrative']}"
                for item in workflow_summary["items"][:5]
            ),
        })
    if request.focus.strip():
        sections.append({"title": f"检索焦点：{request.focus}", "content": "；".join(item["summary"] for item in citations[:4]) or "未检索到相关记录。"})
    return {
        "title": f"{template['name']} · {batch.get('source_date')}",
        "template": request.template,
        "focus": request.focus,
        "sections": sections,
        "quality_items": quality_items,
        "citations": citations,
        "analysis_workflow": workflow_summary,
        "verified_count": workflow_summary["verified_count"],
        "excluded_count": workflow_summary["excluded_count"],
        "status": "GENERATED_FROM_PUBLIC_WEB_SAMPLE" if batch.get("code") == PUBLIC_BATCH_CODE else "GENERATED_FROM_TEST_BATCH",
        "notice": "报告主体由结构化规则生成，未调用生成式大模型；机器候选不作为正式研判结论，已核验结论须由人工确认或修订。",
    }


DIST = ROOT / "dist"
if (DIST / "assets").exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")


@app.get("/")
def root_page():
    index = DIST / "index.html"
    if index.exists():
        return FileResponse(index)
    return {"name": app.title, "docs": "/docs", "health": "/api/health"}


@app.get("/api/monitor-objects")
def monitor_objects(layer: str | None = Query(default=None, max_length=20), role: RoleName = "researcher") -> dict:
    items = classification_list_objects(layer=layer, role=role)
    return {"items": items, "count": len(items)}


@app.get("/api/opinion/overview")
def opinion_overview(role: RoleName = "researcher") -> dict:
    return classification_overview(role=role)


@app.get("/api/opinion/items")
def opinion_items(
    role: RoleName = "researcher",
    layer: str | None = Query(default=None, max_length=20),
    topic: str | None = Query(default=None, max_length=40),
    sentiment: str | None = Query(default=None, max_length=10),
    risk_grade: str | None = Query(default=None, max_length=10),
    limit: int = Query(default=200, ge=1, le=500),
) -> dict:
    items = classification_list_items(
        role=role, layer=layer, topic=topic, sentiment=sentiment, risk_grade=risk_grade, limit=limit
    )
    return {"items": items, "count": len(items)}


@app.get("/api/capabilities")
def capabilities(role: RoleName = "researcher") -> dict:
    with db() as conn:
        verify = verify_audit_chain(conn)
    return {
        "llm_agent": {
            "provider": "qwen",
            "available": qwen_agent.available,
            "model": qwen_agent.model,
        },
        "embeddings": {
            "active": semantic_engine.supported_models(),
            "default": "qwen",
        },
        "graph_engine": "networkx",
        "vector_store": "sqlite-float32-blob",
        "audit_chain": {
            "algorithm": "SHA-256",
            "valid": bool(verify.get("valid")),
            "block_count": int(verify.get("block_count", 0)),
        },
    }


@app.get("/api/vectors/stats")
def vector_stats(role: RoleName = "researcher") -> dict:
    with db() as conn:
        current_batch_id = _latest_batch_id(conn)
        rows = conn.execute(
            """SELECT kc.dimensions,COUNT(*) AS count FROM knowledge_chunk kc
                 JOIN source_record sr ON sr.id=kc.record_id
                 WHERE sr.batch_id=? GROUP BY kc.dimensions ORDER BY count DESC""",
            (current_batch_id,),
        ).fetchall() if current_batch_id else []
    indexed = [dict(row) for row in rows]
    active = any(
        int(item["dimensions"]) in {model["dimension"] for key, model in SEMANTIC_MODELS.items() if model["provider"] != "local"}
        for item in indexed
    )
    return {"indexed": indexed, "total": sum(int(item["count"]) for item in indexed), "semantic_active": active}


@app.post("/api/vectors/rebuild")
def rebuild_vectors(request: RebuildVectorsRequest, role: RoleName = "researcher") -> dict:
    if role != "core":
        raise HTTPException(403, "仅核心课题组可重建向量索引")
    model = (request.model or "qwen").strip().lower()
    if model not in SEMANTIC_MODELS:
        raise HTTPException(400, f"不支持的向量模型：{model}")
    with db() as conn:
        current_batch_id = _latest_batch_id(conn)
        version = conn.execute(
            "SELECT id,collection_id FROM knowledge_version WHERE batch_id=? ORDER BY created_at DESC,id DESC LIMIT 1",
            (current_batch_id,),
        ).fetchone() if current_batch_id else None
        if not version:
            raise HTTPException(404, "没有可重建的知识版本")
        rows = conn.execute(
            "SELECT id,text FROM knowledge_chunk WHERE version_id=? ORDER BY id",
            (version["id"],),
        ).fetchall()
        if not rows:
            raise HTTPException(404, "知识块为空")
        texts = [row["text"] for row in rows]
        vectors, meta = semantic_engine.resolve_embeddings(texts, model)
        if vectors is None:
            raise HTTPException(502, "该模型当前不可用，请检查 API Key 或无网络环境下使用 local")
        dimension = int(meta["dimension"])
        for row, vector in zip(rows, vectors):
            conn.execute(
                "UPDATE knowledge_chunk SET vector=?,dimensions=? WHERE id=?",
                (pack_vector(vector), dimension, row["id"]),
            )
        actor = "核心课题组"
        _write_audit(conn, actor, "重建语义向量索引", "knowledge_chunk", str(version["collection_id"]), f"{model}@{dimension} 维")
        append_audit_block(
            conn,
            event_type="VECTOR_INDEX_REBUILT",
            actor=actor,
            object_type="knowledge_vector",
            object_id=str(version["collection_id"]),
            detail=f"{model}@{dimension} 维",
            outcome="SUCCESS" if meta["embedding_active"] else "DEGRADED",
            batch_id=active_batch_code(conn) or BATCH_CODE,
        )
    return {
        "indexed": len(rows),
        "model": model,
        "model_name": meta["model"],
        "provider": meta["provider"],
        "dimension": dimension,
        "embedding_active": meta["embedding_active"],
        "degraded": meta["degraded"],
        "notice": "已重建为语义向量索引" if meta["embedding_active"] else "未配置 API Key，已降级为本地离线向量。",
    }


@app.get("/api/agent/status")
def agent_status(role: RoleName = "researcher") -> dict:
    return {
        "provider": "qwen",
        "available": qwen_agent.available,
        "model": qwen_agent.model,
        "embedding_store": "sqlite-float32-blob",
        "graph_engine": "networkx",
        "audit_chain": "SHA-256 chained blocks",
    }


@app.post("/api/agent/plan")
def agent_plan(request: AgentPlanRequest, role: RoleName = "researcher") -> dict:
    return qwen_agent.plan(request.task, request.context)


@app.post("/api/agent/explain")
def agent_explain(request: AgentExplainRequest, role: RoleName = "researcher") -> dict:
    return qwen_agent.explain(request.evidence, use_llm=request.use_llm)


@app.get("/api/audit/blocks")
def audit_blocks(role: RoleName = "researcher") -> dict:
    with db() as conn:
        rows = conn.execute("SELECT * FROM audit_block ORDER BY height DESC LIMIT 200").fetchall()
        verification = verify_audit_chain(conn)
    blocks = [dict(row) for row in rows]
    if role != "core":
        blocks = [
            {
                **block,
                "actor": "已隐藏",
                "object_id": "已隐藏",
                "detail": "该审计区块详情仅限核心课题组查看",
            }
            for block in blocks
        ]
    return {"blocks": blocks, "verification": verification, "details_redacted": role != "core"}


@app.get("/api/audit/verify")
def audit_verify(role: RoleName = "researcher") -> dict:
    with db() as conn:
        return verify_audit_chain(conn)


@app.get("/{full_path:path}")
def frontend_route(full_path: str):
    index = DIST / "index.html"
    if index.exists() and not full_path.startswith("api/"):
        return FileResponse(index)
    raise HTTPException(404, "资源不存在")

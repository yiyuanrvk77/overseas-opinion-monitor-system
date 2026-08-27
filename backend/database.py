from __future__ import annotations

import json
import os
import hashlib
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "backend" / "data" / "opinion_monitor.db"
DB_PATH = Path(os.environ.get("OPINION_MONITOR_DB", DEFAULT_DB))


SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS dataset_batch (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    purpose TEXT NOT NULL,
    subject TEXT NOT NULL,
    source_date TEXT,
    status TEXT NOT NULL,
    checksum TEXT NOT NULL,
    source_files_json TEXT NOT NULL,
    record_count INTEGER NOT NULL DEFAULT 0,
    category_counts_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workspace_setting (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_record (
    id TEXT PRIMARY KEY,
    batch_id INTEGER NOT NULL REFERENCES dataset_batch(id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    content_json TEXT NOT NULL,
    evidence_type TEXT NOT NULL,
    source_refs_json TEXT NOT NULL DEFAULT '[]',
    sensitivity TEXT NOT NULL DEFAULT 'INTERNAL',
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_source_record_batch_category
    ON source_record(batch_id, category);
CREATE INDEX IF NOT EXISTS idx_source_record_hash
    ON source_record(content_hash);

CREATE TABLE IF NOT EXISTS knowledge_collection (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    lifecycle TEXT NOT NULL,
    index_method TEXT NOT NULL,
    classification TEXT NOT NULL,
    owner TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_version (
    id TEXT PRIMARY KEY,
    collection_id TEXT NOT NULL REFERENCES knowledge_collection(id),
    batch_id INTEGER NOT NULL REFERENCES dataset_batch(id),
    version TEXT NOT NULL,
    status TEXT NOT NULL,
    entry_count INTEGER NOT NULL,
    chunk_count INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    notes TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS knowledge_chunk (
    id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES knowledge_version(id) ON DELETE CASCADE,
    record_id TEXT NOT NULL REFERENCES source_record(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    tokens_json TEXT NOT NULL,
    vector BLOB NOT NULL,
    dimensions INTEGER NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_knowledge_chunk_version
    ON knowledge_chunk(version_id);

CREATE TABLE IF NOT EXISTS quality_issue (
    id TEXT PRIMARY KEY,
    batch_id INTEGER NOT NULL REFERENCES dataset_batch(id) ON DELETE CASCADE,
    issue_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    title TEXT NOT NULL,
    details TEXT NOT NULL,
    status TEXT NOT NULL,
    record_id TEXT REFERENCES source_record(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_time TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    detail TEXT NOT NULL,
    outcome TEXT NOT NULL,
    trace_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_checkpoint (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    event_count INTEGER NOT NULL,
    head_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_block (
    height INTEGER PRIMARY KEY,
    prev_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL,
    block_time TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    detail TEXT NOT NULL,
    outcome TEXT NOT NULL,
    batch_id TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS connector_registry (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    channel_type TEXT NOT NULL,
    supported_media TEXT NOT NULL,
    status TEXT NOT NULL,
    mode TEXT NOT NULL,
    last_checked_at TEXT,
    note TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS collection_task (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    dimension TEXT NOT NULL,
    target_value TEXT NOT NULL,
    connector_id TEXT NOT NULL REFERENCES connector_registry(id),
    frequency TEXT NOT NULL,
    history_days INTEGER NOT NULL DEFAULT 90,
    media_types_json TEXT NOT NULL DEFAULT '[]',
    languages_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'DRAFT',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alert_case (
    id TEXT PRIMARY KEY,
    record_id TEXT NOT NULL REFERENCES source_record(id) ON DELETE CASCADE,
    risk TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    assignee TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS requirement_feature (
    id TEXT PRIMARY KEY,
    module TEXT NOT NULL,
    feature TEXT NOT NULL,
    priority TEXT NOT NULL,
    delivery_status TEXT NOT NULL,
    verification TEXT NOT NULL,
    note TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS monitor_object (
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
CREATE INDEX IF NOT EXISTS idx_monitor_object_layer
    ON monitor_object(layer);

CREATE TABLE IF NOT EXISTS opinion_item (
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
CREATE INDEX IF NOT EXISTS idx_opinion_item_object
    ON opinion_item(object_id);
CREATE INDEX IF NOT EXISTS idx_opinion_item_topic
    ON opinion_item(topic);

CREATE TABLE IF NOT EXISTS collected_item (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_item_id TEXT NOT NULL,
    vendor TEXT NOT NULL,
    platform TEXT NOT NULL,
    platform_item_type TEXT NOT NULL DEFAULT 'post',
    author_name TEXT NOT NULL DEFAULT '',
    author_handle TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    media_type TEXT NOT NULL DEFAULT 'text',
    language TEXT NOT NULL DEFAULT 'zh',
    published_at TEXT NOT NULL DEFAULT '',
    collected_at TEXT NOT NULL,
    likes_count INTEGER NOT NULL DEFAULT 0,
    reposts_count INTEGER NOT NULL DEFAULT 0,
    comments_count INTEGER NOT NULL DEFAULT 0,
    related_accounts_json TEXT NOT NULL DEFAULT '[]',
    raw_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(vendor, vendor_item_id)
);
"""


CONNECTORS = [
    ("x", "X", "social", "text,image,video", "NOT_CONFIGURED", "adapter", "等待正式数据源配置"),
    ("truth-social", "Truth Social", "social", "text,image,video", "NOT_CONFIGURED", "adapter", "等待正式数据源配置"),
    ("facebook", "Facebook", "social", "text,image,video", "NOT_CONFIGURED", "adapter", "等待正式数据源配置"),
    ("tiktok", "TikTok", "social", "video,text", "NOT_CONFIGURED", "adapter", "等待正式数据源配置"),
    ("youtube", "YouTube", "social", "video,text", "NOT_CONFIGURED", "adapter", "等待正式数据源配置"),
    ("instagram", "Instagram", "social", "image,video,text", "NOT_CONFIGURED", "adapter", "等待正式数据源配置"),
    ("reuters", "Reuters", "media", "text,image,video", "NOT_CONFIGURED", "adapter", "等待正式数据源配置"),
    ("ap", "AP", "media", "text,image,video", "NOT_CONFIGURED", "adapter", "等待正式数据源配置"),
    ("nyt", "The New York Times", "media", "text,image,video", "NOT_CONFIGURED", "adapter", "等待正式数据源配置"),
    ("wsj", "The Wall Street Journal", "media", "text,image,video", "NOT_CONFIGURED", "adapter", "等待正式数据源配置"),
    ("cnn", "CNN", "media", "text,image,video", "NOT_CONFIGURED", "adapter", "等待正式数据源配置"),
    ("bbc", "BBC", "media", "text,image,video", "NOT_CONFIGURED", "adapter", "等待正式数据源配置"),
]


REQUIREMENTS = [
    ("REQ-01", "监测对象", "第一层：美国核心政要及家族动态、言论与互动", "P0", "IMPLEMENTED", "测试批次人物、账号、内容与角色过滤", "当前仅由可替换测试数据验证，正式平台连接待接入"),
    ("REQ-02", "监测对象", "第二层A：西方世家及政治经济精英关系网络", "P1", "SCAFFOLDED", "异构人物关系模型和图谱接口", "正式对象名录、商业网络和倾向模型待配置"),
    ("REQ-03", "监测对象", "第二层B：西方主流媒体涉华报道与叙事变化", "P1", "PARTIAL", "六家媒体公开源巡检、专题样本、来源证据和内容数据契约", "连续样本、正式全文授权及叙事变化模型待接入"),
    ("REQ-04", "监测对象", "第三层：海外社交平台高影响力涉华账号", "P2", "SCAFFOLDED", "账号实体、影响字段和筛选接口", "粉丝阈值和涉华频率需用正式数据计算"),
    ("REQ-05", "数据采集", "六个社媒与六个主流媒体平台覆盖", "P0", "PARTIAL", "12个渠道公开 RSS/HTML 巡检、访问状态审计和正式连接器注册表", "公开巡检不等同于正式授权连接器；受限渠道等待官方 API 或供应商接口"),
    ("REQ-06", "数据采集", "人名、账号ID、关键词、话题标签多维任务", "P0", "IMPLEMENTED", "四种维度任务草稿持久化接口", "连接器就绪前任务不进入运行态"),
    ("REQ-07", "数据采集", "文本、图片、视频多模态及多语种采集翻译", "P1", "SCAFFOLDED", "媒体类型、语言字段和连接器能力声明", "OCR、ASR和翻译服务待部署"),
    ("REQ-08", "数据采集", "至少近三个月公开数据回溯", "P1", "PARTIAL", "公开专题快照覆盖跨月记录，任务历史天数字段限制为90天", "全量三个月分页回溯仍依赖平台授权和正式接口"),
    ("REQ-09", "数据采集", "分钟、小时、每日差异化采集频率", "P2", "SCAFFOLDED", "任务频率枚举与暂停/归档状态", "生产调度器、限流和重试队列待部署"),
    ("REQ-10", "数据采集", "时间、互动量及关联账号等完整元数据", "P2", "IMPLEMENTED", "规范化记录、原始JSON、批次、哈希与来源锚点", "ZIP缺少的URL和帖子ID作为质量问题保留"),
    ("REQ-11", "人物画像", "身份、职务、家族背景与机构基础画像", "P0", "IMPLEMENTED", "人物实体、详情抽屉、来源与敏感级", "正式画像更新需要对象维护审批流程"),
    ("REQ-12", "人物画像", "跨平台账号矩阵及活跃度统计", "P1", "PARTIAL", "账号实体与人物账号关系", "活跃度需根据正式时间窗内容重新计算"),
    ("REQ-13", "人物画像", "言论时间线及立场演变", "P1", "PARTIAL", "逐帖内容和事件时间字段可检索", "ZIP无立场真值，暂不生成立场变化分数"),
    ("REQ-14", "人物画像", "互动、引用、亲属与商业关联图谱", "P1", "IMPLEMENTED", "人物、事件、传播、证据四种有向图", "部分边为源文件作者分析，已单独标记证据性质"),
    ("REQ-15", "人物画像", "粉丝、互动率与传播范围影响力评估", "P1", "PARTIAL", "保留源文件粉丝和互动字段", "统一影响力公式及正式触达数据待确认"),
    ("REQ-16", "人物画像", "定位异常、批量转发和机器账号特征识别", "P2", "SCAFFOLDED", "质量问题和异常扩展字段", "需要带真值的异常账号数据集和模型评测"),
    ("REQ-17", "智能分析", "涉华内容自动识别与热点发现", "P0", "PARTIAL", "公开采集器规则命中、三专题聚合与混合检索接口", "正式涉华分类模型、趋势窗口及独立评测集待接入"),
    ("REQ-18", "智能分析", "情感三分类、核心议题与关键词", "P0", "PARTIAL", "源文件主题统计；情感明确标记未运行", "需要人工情感真值和模型版本后方可输出比例"),
    ("REQ-19", "智能分析", "传播路径、关键节点与发酵趋势", "P1", "PARTIAL", "发布、引用和主题关系传播图", "完整转发链和时序趋势依赖平台原始ID"),
    ("REQ-20", "风险预警", "主权、安全与发展利益风险分级预警", "P0", "PARTIAL", "测试字段命中、认领、处置和审计接口", "当前风险等级来自测试源字段；生产规则矩阵、阈值和通知渠道需课题组确认"),
    ("REQ-21", "智能报告", "人物言论、立场与影响力专报", "P2", "PARTIAL", "人物报告模板与可追溯检索引用", "立场和影响力研判受当前数据边界限制"),
    ("REQ-22", "智能报告", "重大事件各方反应与态势快报", "P1", "PARTIAL", "事件快报结构、事件检索和来源引用", "当前使用通用结构化生成器；自动触发、专用模板及时效依赖正式连接器"),
    ("REQ-23", "智能报告", "核心动态、倾向、表态与趋势专题研判", "P0", "PARTIAL", "议题研判模板和结构化章节", "内参行文模板和人工审批规则待确认"),
    ("REQ-24", "智能报告", "海外舆情周报与月报", "P1", "PARTIAL", "周报、月报模板可按需生成", "定时调度和周期对账待部署"),
    ("REQ-25", "智能报告", "结论回到原始公开来源的分析溯源", "P1", "IMPLEMENTED", "记录ID、来源引用、证据类型和内容哈希", "缺失原始URL的ZIP记录保持缺口状态"),
    ("REQ-26", "AI能力", "私有化、本地化部署", "P0", "IMPLEMENTED", "本地FastAPI、React、SQLite和一键启动脚本", "生产环境仍需IAM、TLS、备份和高可用"),
    ("REQ-27", "AI能力", "中英为主并兼顾其他语种的NLP", "P2", "SCAFFOLDED", "中英文词法索引和语言任务字段", "多语种识别、翻译和分语种评测待接入"),
    ("REQ-28", "AI能力", "历史报告和专业语料模型微调", "P2", "SCAFFOLDED", "版本化知识集合和评测扩展位", "本版未训练或声称拥有微调模型"),
    ("REQ-29", "AI能力", "自然语言检索、问答与追问", "P1", "PARTIAL", "本地混合检索、单轮问答摘要、拒答和逐条引用", "当前不是生成式大模型；多轮上下文与推理待模型接入"),
    ("REQ-30", "Web界面", "简洁、功能优先的多人Web工作台", "P0", "IMPLEMENTED", "统一响应式前端和FastAPI接口", "生产并发会话与统一登录待部署"),
    ("REQ-31", "权限安全", "第一层限少数授权、其他层实名开放", "P0", "PARTIAL", "服务端按角色过滤限制记录", "当前角色切换仅演示，生产必须由统一身份签发角色"),
    ("REQ-32", "权限安全", "全系统本地存储处理与科研单位安全要求", "P2", "PARTIAL", "本地SQLite、受限CORS和哈希审计链", "生产需补静态加密、HTTPS、备份恢复和安全基线"),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def get_conn(path: Path | None = None) -> sqlite3.Connection:
    db_path = Path(path or DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


@contextmanager
def db(path: Path | None = None):
    conn = get_conn(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(path: Path | None = None) -> None:
    with db(path) as conn:
        conn.executescript(SCHEMA)
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_knowledge_chunk_version_record "
                "ON knowledge_chunk(version_id, record_id)"
            )
        except sqlite3.IntegrityError:
            # A damaged legacy index is rebuilt by ingest_snapshot before this
            # constraint is installed again.
            pass
        conn.executemany(
            """INSERT INTO connector_registry
               (id,name,channel_type,supported_media,status,mode,last_checked_at,note)
               VALUES(?,?,?,?,?,?,NULL,?)
               ON CONFLICT(id) DO UPDATE SET
                 name=excluded.name, channel_type=excluded.channel_type,
                 supported_media=excluded.supported_media, mode=excluded.mode,
                 note=excluded.note""",
            CONNECTORS,
        )
        conn.executemany(
            """INSERT INTO requirement_feature
               (id,module,feature,priority,delivery_status,verification,note)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 module=excluded.module, feature=excluded.feature,
                 priority=excluded.priority, delivery_status=excluded.delivery_status,
                 verification=excluded.verification, note=excluded.note""",
            REQUIREMENTS,
        )


def active_batch_code(conn: sqlite3.Connection) -> str | None:
    """Return the explicitly selected batch, falling back to the newest batch."""
    setting = conn.execute(
        "SELECT value FROM workspace_setting WHERE key='active_batch_code'"
    ).fetchone()
    if setting:
        selected = conn.execute(
            "SELECT code FROM dataset_batch WHERE code=?", (setting["value"],)
        ).fetchone()
        if selected:
            return str(selected["code"])
    row = conn.execute(
        "SELECT code FROM dataset_batch ORDER BY updated_at DESC,id DESC LIMIT 1"
    ).fetchone()
    return str(row["code"]) if row else None


def set_active_batch(conn: sqlite3.Connection, code: str) -> None:
    """Persist the batch used by user-facing workbench views."""
    conn.execute(
        """INSERT INTO workspace_setting(key,value,updated_at) VALUES('active_batch_code',?,?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
        (code, now_iso()),
    )
def ensure_knowledge_chunk_constraint(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_knowledge_chunk_version_record "
        "ON knowledge_chunk(version_id, record_id)"
    )


def audit_checkpoint_matches(conn: sqlite3.Connection) -> bool:
    count = conn.execute("SELECT COUNT(*) FROM audit_event").fetchone()[0]
    checkpoint = conn.execute(
        "SELECT event_count,head_hash FROM audit_checkpoint WHERE id=1"
    ).fetchone()
    if count == 0:
        return checkpoint is None
    last = conn.execute("SELECT trace_hash FROM audit_event ORDER BY id DESC LIMIT 1").fetchone()
    return bool(
        checkpoint
        and checkpoint["event_count"] == count
        and last
        and checkpoint["head_hash"] == last["trace_hash"]
    )


def require_audit_appendable(conn: sqlite3.Connection) -> None:
    if not audit_checkpoint_matches(conn):
        raise RuntimeError("审计检查点缺失或不一致，已拒绝追加事件")


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def audit_block_head(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM audit_block ORDER BY height DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def append_audit_block(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    actor: str,
    object_type: str,
    object_id: str,
    detail: str,
    outcome: str = "SUCCESS",
    batch_id: str = "",
) -> dict:
    head = audit_block_head(conn)
    height = (int(head["height"]) + 1) if head else 1
    prev_hash = head["event_hash"] if head else "GENESIS"
    block_time = _now_iso()
    payload = [height, prev_hash, block_time, event_type, actor, object_type, object_id, detail, outcome, batch_id]
    event_hash = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    conn.execute(
        """INSERT INTO audit_block
           (height,prev_hash,event_hash,block_time,event_type,actor,object_type,object_id,detail,outcome,batch_id)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (height, prev_hash, event_hash, block_time, event_type, actor, object_type, object_id, detail, outcome, batch_id),
    )
    return {
        "height": height,
        "prev_hash": prev_hash,
        "event_hash": event_hash,
        "block_time": block_time,
        "event_type": event_type,
        "actor": actor,
        "object_type": object_type,
        "object_id": object_id,
        "detail": detail,
        "outcome": outcome,
        "batch_id": batch_id,
    }


def verify_audit_chain(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("SELECT * FROM audit_block ORDER BY height").fetchall()
    if not rows:
        return {"valid": True, "block_count": 0, "height": 0, "head_hash": ""}
    expected_prev = "GENESIS"
    valid = True
    last_hash = ""
    tamper_block: int | None = None
    for row in rows:
        try:
            calculated = _expected_block_hash(row)
        except Exception:  # noqa: BLE001 - malicious row
            calculated = ""
        if str(row["prev_hash"]) != expected_prev or calculated != str(row["event_hash"]):
            valid = False
            if tamper_block is None:
                tamper_block = int(row["height"])
        expected_prev = str(row["event_hash"])
        last_hash = str(row["event_hash"])
    return {
        "valid": valid,
        "block_count": len(rows),
        "height": int(rows[-1]["height"]),
        "head_hash": last_hash,
        "tamper_block": tamper_block,
    }


def _expected_block_hash(row) -> str:
    payload = [
        int(row["height"]), str(row["prev_hash"]), str(row["block_time"]),
        str(row["event_type"]), str(row["actor"]), str(row["object_type"]),
        str(row["object_id"]), str(row["detail"]), str(row["outcome"]),
        str(row["batch_id"]),
    ]
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(row) for row in rows]

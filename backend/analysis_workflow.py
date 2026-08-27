"""Auditable machine-candidate and human-review workflow.

The workflow deliberately keeps source records independent from analysis rows.  A
snapshot refresh may replace source records, while historical model and reviewer
decisions remain available for audit.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import Counter
from typing import Any

from .database import now_iso, rows_to_dicts

RULE_ENGINE = "auditable-lexicon-v2"
QWEN_ENGINE = "qwen-chat-v1"
PROMPT_VERSION = "overseas-opinion-analysis-v1"
MACHINE_CANDIDATE = "MACHINE_CANDIDATE"
FAILED = "FAILED"
HUMAN_DECISIONS = {
    "HUMAN_CONFIRMED",
    "HUMAN_REVISED",
    "HUMAN_REJECTED",
    "NEEDS_MORE_EVIDENCE",
}
VERIFIED_DECISIONS = {"HUMAN_CONFIRMED", "HUMAN_REVISED"}

_POSITIVE = ("support", "progress", "cooperation", "agreement", "welcome", "positive", "支持", "合作", "进展", "欢迎")
_NEGATIVE = ("critic", "concern", "tension", "sanction", "threat", "risk", "attack", "dispute", "批评", "担忧", "紧张", "制裁", "威胁", "争议")
_HIGH_RISK = ("war", "military", "security", "sanction", "attack", "crisis", "战争", "军事", "安全", "制裁", "危机", "袭击")
_MEDIUM_RISK = ("tension", "dispute", "concern", "critic", "紧张", "争议", "担忧", "批评")
_THEMES = (
    ("中美关系", ("china", "chinese", "beijing", "中国", "中美")),
    ("经贸与产业", ("trade", "tariff", "economy", "investment", "贸易", "关税", "经济", "投资")),
    ("国际会议", ("apec", "summit", "conference", "峰会", "会议")),
    ("安全议题", ("security", "military", "war", "安全", "军事", "战争")),
)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _as_json(value: str, default: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _record_input(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "record_id": str(record["id"]),
        "title": str(record.get("title") or ""),
        "summary": str(record.get("summary") or ""),
        "source_refs": _as_json(str(record.get("source_refs_json") or "[]"), []),
        "evidence_type": str(record.get("evidence_type") or "source_snapshot"),
        "content_hash": str(record.get("content_hash") or ""),
    }


def _snippets(title: str, summary: str) -> list[str]:
    result = [part.strip() for part in (title, summary) if part and part.strip()]
    return result[:2]


def _contains_term(text: str, term: str) -> bool:
    """Match English lexicon entries as whole words; retain substring matching for CJK terms."""
    if any("\u4e00" <= char <= "\u9fff" for char in term):
        return term in text
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", text, flags=re.IGNORECASE) is not None


def _rule_candidate(item: dict[str, Any]) -> dict[str, Any]:
    text = f"{item['title']} {item['summary']}"
    positive = [term for term in _POSITIVE if _contains_term(text, term)]
    negative = [term for term in _NEGATIVE if _contains_term(text, term)]
    high = [term for term in _HIGH_RISK if _contains_term(text, term)]
    medium = [term for term in _MEDIUM_RISK if _contains_term(text, term)]
    if len(positive) > len(negative):
        sentiment = "POSITIVE"
    elif len(negative) > len(positive):
        sentiment = "NEGATIVE"
    else:
        sentiment = "NEUTRAL"
    risk_level = "HIGH" if high else ("MEDIUM" if medium else "LOW")
    themes = [name for name, terms in _THEMES if any(_contains_term(text, term) for term in terms)]
    if not themes:
        themes = ["待人工归类"]
    matched = positive + negative + high + medium
    unique_matched = list(dict.fromkeys(matched))
    confidence = min(0.78, 0.36 + 0.08 * len(unique_matched) + 0.05 * len(themes))
    evidence_type = item["evidence_type"]
    uncertainty = (
        "规则基线仅根据标题与摘要中的可见词项生成候选；未对全文、上下文或原始帖子互动链做语义判断。"
        + (" 当前来源为转述或分析性材料，需回到原始链接复核。" if evidence_type not in {"direct_post_excerpt", "explicit_source_text"} else "")
    )
    return {
        "sentiment": sentiment,
        "stance": "NOT_DETERMINED",
        "risk_level": risk_level,
        "themes": themes,
        "keywords": unique_matched[:12],
        "narrative": "可审计规则基线候选：基于标题和摘要中的词项，不构成正式事实或立场认定。",
        "confidence": round(confidence, 2),
        "uncertainty": uncertainty,
        "evidence_snippets": _snippets(item["title"], item["summary"]),
        "evidence_refs": item["source_refs"],
    }


def _qwen_candidate(item: dict[str, Any], agent: Any) -> dict[str, Any]:
    prompt = (
        "你是海外舆情机器初筛器。仅依据下列标题、摘要和来源元数据输出 JSON；"
        "不得补充未提供的事实。字段必须为 sentiment(POSITIVE/NEGATIVE/NEUTRAL)、"
        "stance、risk_level(LOW/MEDIUM/HIGH)、themes(数组)、keywords(数组)、"
        "narrative、confidence(0-1)、uncertainty。\n输入：" + _canonical(item)
    )
    raw = agent._chat([
        {"role": "system", "content": "只输出符合要求的 JSON，不使用 Markdown。"},
        {"role": "user", "content": prompt},
    ])
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("Qwen 输出不是对象")
    sentiment = str(parsed.get("sentiment") or "NEUTRAL").upper()
    risk = str(parsed.get("risk_level") or "LOW").upper()
    if sentiment not in {"POSITIVE", "NEGATIVE", "NEUTRAL"} or risk not in {"LOW", "MEDIUM", "HIGH"}:
        raise ValueError("Qwen 输出枚举无效")
    confidence = float(parsed.get("confidence", 0))
    return {
        "sentiment": sentiment,
        "stance": str(parsed.get("stance") or "NOT_DETERMINED")[:80],
        "risk_level": risk,
        "themes": [str(x)[:80] for x in (parsed.get("themes") or []) if str(x).strip()][:8] or ["待人工归类"],
        "keywords": [str(x)[:80] for x in (parsed.get("keywords") or []) if str(x).strip()][:12],
        "narrative": str(parsed.get("narrative") or "机器候选，待人工复核。")[:1200],
        "confidence": max(0.0, min(1.0, confidence)),
        "uncertainty": str(parsed.get("uncertainty") or "机器初筛结果，需人工结合原始来源复核。")[:1200],
        "evidence_snippets": _snippets(item["title"], item["summary"]),
        "evidence_refs": item["source_refs"],
    }


def _engine(agent: Any) -> dict[str, str]:
    if bool(getattr(agent, "available", False)):
        model = str(getattr(agent, "model", "qwen-plus"))
        return {
            "provider": "QWEN",
            "provider_label": "Qwen 大模型",
            "model": model,
            "engine_version": f"{QWEN_ENGINE}:{model}:{PROMPT_VERSION}",
        }
    return {
        "provider": "RULE_BASELINE",
        "provider_label": "可审计规则基线",
        "model": RULE_ENGINE,
        "engine_version": f"{RULE_ENGINE}:{PROMPT_VERSION}",
    }


def run_machine_analysis(conn: Any, records: list[dict[str, Any]], *, actor: str, agent: Any, scope: dict[str, Any] | None = None) -> dict[str, Any]:
    engine = _engine(agent)
    record_inputs = [_record_input(record) for record in records]
    run_id = f"run:{uuid.uuid4().hex}"
    started_at = now_iso()
    input_hash = _sha({"engine": engine["engine_version"], "records": record_inputs})
    conn.execute(
        """INSERT INTO analysis_run
           (id,provider,provider_label,model,engine_version,prompt_version,parameters_json,status,
            input_count,success_count,failed_count,input_hash,output_hash,error,created_by,started_at,completed_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)""",
        (run_id, engine["provider"], engine["provider_label"], engine["model"], engine["engine_version"],
         PROMPT_VERSION, _canonical({"scope": scope or {"type": "active_batch"}, "record_ids": [item["record_id"] for item in record_inputs], "idempotency": "record_id+content_hash+engine_version"}),
         "RUNNING", len(record_inputs), 0, 0, input_hash, "", "", actor, started_at),
    )
    created: list[dict[str, Any]] = []
    skipped: list[str] = []
    failures: list[dict[str, str]] = []
    for item in record_inputs:
        existing = conn.execute(
            "SELECT id FROM machine_analysis WHERE record_id=? AND source_content_hash=? AND engine_version=?",
            (item["record_id"], item["content_hash"], engine["engine_version"]),
        ).fetchone()
        if existing:
            skipped.append(str(existing["id"]))
            continue
        try:
            result = _qwen_candidate(item, agent) if engine["provider"] == "QWEN" else _rule_candidate(item)
            analysis_id = f"ma:{uuid.uuid4().hex}"
            output_hash = _sha(result)
            conn.execute(
                """INSERT INTO machine_analysis
                   (id,run_id,record_id,source_content_hash,provider,provider_label,model,engine_version,prompt_version,
                    status,sentiment,stance,risk_level,themes_json,keywords_json,narrative,confidence,uncertainty,
                    evidence_snippets_json,evidence_refs_json,input_hash,output_hash,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (analysis_id, run_id, item["record_id"], item["content_hash"], engine["provider"], engine["provider_label"],
                 engine["model"], engine["engine_version"], PROMPT_VERSION, MACHINE_CANDIDATE, result["sentiment"],
                 result["stance"], result["risk_level"], _canonical(result["themes"]), _canonical(result["keywords"]),
                 result["narrative"], result["confidence"], result["uncertainty"], _canonical(result["evidence_snippets"]),
                 _canonical(result["evidence_refs"]), _sha(item), output_hash, now_iso()),
            )
            created.append({"id": analysis_id, "record_id": item["record_id"], "status": MACHINE_CANDIDATE})
        except Exception as exc:  # retain an explicit, auditable failed candidate
            analysis_id = f"ma:{uuid.uuid4().hex}"
            error = str(exc)[:1000]
            failed_output = {"error": error, "engine": engine["engine_version"]}
            conn.execute(
                """INSERT INTO machine_analysis
                   (id,run_id,record_id,source_content_hash,provider,provider_label,model,engine_version,prompt_version,
                    status,sentiment,stance,risk_level,narrative,confidence,uncertainty,evidence_snippets_json,evidence_refs_json,input_hash,output_hash,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (analysis_id, run_id, item["record_id"], item["content_hash"], engine["provider"], engine["provider_label"],
                 engine["model"], engine["engine_version"], PROMPT_VERSION, FAILED, "NOT_RUN", "NOT_RUN", "NOT_RUN",
                 "机器分析失败，未生成候选结论。", 0.0, error, _canonical(_snippets(item["title"], item["summary"])),
                 _canonical(item["source_refs"]), _sha(item), _sha(failed_output), now_iso()),
            )
            failures.append({"id": analysis_id, "record_id": item["record_id"], "error": error})
    output_hash = _sha({"created": created, "skipped": skipped, "failures": failures})
    status = "COMPLETED_WITH_ERRORS" if failures else "COMPLETED"
    conn.execute(
        "UPDATE analysis_run SET status=?,success_count=?,failed_count=?,output_hash=?,completed_at=? WHERE id=?",
        (status, len(created), len(failures), output_hash, now_iso(), run_id),
    )
    return {
        "run_id": run_id, "engine": engine, "created": created, "skipped": skipped, "failures": failures,
        "requested_count": len(record_inputs), "created_count": len(created), "skipped_count": len(skipped), "failed_count": len(failures),
    }


def analysis_view(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for field in ("themes", "keywords", "evidence_snippets", "evidence_refs"):
        result[field] = _as_json(str(result.pop(f"{field}_json", "[]")), [])
    return result


def latest_reviews(conn: Any, analysis_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not analysis_ids:
        return {}
    placeholders = ",".join("?" for _ in analysis_ids)
    rows = rows_to_dicts(conn.execute(
        f"""SELECT hr.* FROM human_review hr JOIN (
                SELECT machine_analysis_id,MAX(rowid) AS latest_rowid FROM human_review
                WHERE machine_analysis_id IN ({placeholders}) GROUP BY machine_analysis_id
            ) recent ON recent.machine_analysis_id=hr.machine_analysis_id AND recent.latest_rowid=hr.rowid""",
        analysis_ids,
    ).fetchall())
    output: dict[str, dict[str, Any]] = {}
    for row in rows:
        row["evidence_refs"] = _as_json(row.pop("evidence_refs_json", "[]"), [])
        output[str(row["machine_analysis_id"])] = row
    return output


def queue_counts(conn: Any, record_ids: list[str] | None = None) -> dict[str, int]:
    args: list[Any] = []
    where = ""
    if record_ids is not None:
        if not record_ids:
            return {}
        where = " WHERE record_id IN (" + ",".join("?" for _ in record_ids) + ")"
        args.extend(record_ids)
    rows = conn.execute(f"SELECT status,COUNT(*) AS count FROM machine_analysis{where} GROUP BY status", args).fetchall()
    return {str(row["status"]): int(row["count"]) for row in rows}


def verified_report_summary(conn: Any, record_ids: list[str]) -> dict[str, Any]:
    if not record_ids:
        return {"verified_count": 0, "excluded_count": 0, "items": []}
    placeholders = ",".join("?" for _ in record_ids)
    analyses = rows_to_dicts(conn.execute(
        f"SELECT * FROM machine_analysis WHERE record_id IN ({placeholders}) ORDER BY created_at DESC", record_ids
    ).fetchall())
    reviews = latest_reviews(conn, [str(item["id"]) for item in analyses])
    items: list[dict[str, Any]] = []
    excluded = 0
    seen_records: set[str] = set()
    for analysis in analyses:
        record_id = str(analysis["record_id"])
        if record_id in seen_records:
            continue
        seen_records.add(record_id)
        review = reviews.get(str(analysis["id"]))
        if not review or review["decision"] not in VERIFIED_DECISIONS:
            excluded += 1
            continue
        items.append({
            "analysis_id": analysis["id"], "record_id": record_id, "decision": review["decision"],
            "reviewer": review["reviewer"], "sentiment": review["sentiment"] or analysis["sentiment"],
            "risk_level": review["risk_level"] or analysis["risk_level"],
            "narrative": review["narrative"] or analysis["narrative"],
        })
    excluded += max(0, len(record_ids) - len(seen_records))
    return {"verified_count": len(items), "excluded_count": excluded, "items": items}

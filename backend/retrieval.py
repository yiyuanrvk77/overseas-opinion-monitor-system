from __future__ import annotations

import json
import math
import re
import struct
from collections import Counter
from pathlib import Path

from .database import active_batch_code, db
from .features import DIMENSIONS, cosine, feature_vector, tokenize, unpack_vector
from .semantic import semantic_engine


EVIDENCE_LABELS = {
    "explicit_source_text": "源文件明示",
    "direct_post_excerpt": "直接帖子摘录",
    "metric_only_no_text": "仅有指标、无正文",
    "reported_by_source_file": "来源报告转述",
    "source_list_only": "来源清单",
    "link_domain_only": "来源域名线索",
    "source_analysis": "源文件作者分析",
    "explicit_source_text_plus_analysis": "明示与分析混合",
    "inference_or_speculation": "研究推断",
    "mixed_reported_and_inferred": "转述与推断混合",
    "source_conflict": "跨文件口径冲突",
    "production_gap": "生产字段缺口",
    "source_snapshot": "来源快照",
}

CATEGORY_LABELS = {
    "account": "账号实体",
    "actor": "人物实体",
    "profile_signal": "画像信号",
    "content": "逐帖内容",
    "event": "事件线索",
    "relationship_layer": "关系圈层",
    "business_signal": "商业与政治信号",
    "source": "来源台账",
    "analysis": "研究分析",
    "quality_conflict": "口径冲突",
    "production_gap": "生产缺口",
}

LATIN_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in",
    "is", "it", "of", "on", "or", "that", "the", "this", "to", "was", "with",
}
CHINESE_STOP_BIGRAMS = {"这个", "那个", "什么", "一个", "的是", "以及", "关于"}

MODEL_LABELS = {
    "qwen": "通义千问",
    "zhipu": "智谱AI",
    "openai": "OpenAI",
    "local": "本地离线向量",
}


def _allowed_sensitivity(role: str) -> tuple[str, ...]:
    access = {
        "researcher": ("INTERNAL", "CONFIDENTIAL"),
        "core": ("INTERNAL", "CONFIDENTIAL", "RESTRICTED"),
    }
    try:
        return access[role.lower()]
    except (AttributeError, KeyError) as exc:
        raise ValueError("未知角色，已拒绝访问") from exc


def _lexical_score(query_tokens: list[str], doc_tokens: list[str]) -> float:
    if not query_tokens or not doc_tokens:
        return 0.0
    query_counts = Counter(query_tokens)
    doc_counts = Counter(doc_tokens)
    overlap = sum(min(count, doc_counts[token]) for token, count in query_counts.items())
    coverage = overlap / max(sum(query_counts.values()), 1)
    density = overlap / math.sqrt(max(len(doc_tokens), 1) * max(len(query_tokens), 1))
    return min(1.0, 0.75 * coverage + 0.25 * density)


def _rule_score(query: str, title: str, summary: str, category: str) -> float:
    value = query.strip().lower()
    if not value:
        return 0.0
    title_value = title.lower()
    summary_value = summary.lower()
    score = 0.0
    if value == title_value:
        score += 1.0
    elif value in title_value:
        score += 0.75
    if value in summary_value:
        score += 0.35
    if value in CATEGORY_LABELS.get(category, "").lower():
        score += 0.25
    return min(1.0, score)


def _has_lexical_anchor(query_tokens: list[str], doc_tokens: list[str]) -> bool:
    doc_set = set(doc_tokens)
    latin = {
        token
        for token in query_tokens
        if re.fullmatch(r"[a-z0-9_@.-]+", token) and len(token.strip("@.-")) >= 2
        and token not in LATIN_STOP_WORDS
    }
    latin_matches = latin & doc_set
    if latin and len(latin_matches) >= min(2, len(latin)) and len(latin_matches) / len(latin) >= 0.5:
        return True
    chinese_bigrams = {token for token in query_tokens if re.fullmatch(r"[\u4e00-\u9fff]{2}", token)}
    matched = chinese_bigrams & doc_set
    if not matched:
        return False
    if len(chinese_bigrams) == 1:
        return True
    return len(matched) >= 2 and len(matched) / len(chinese_bigrams) >= 0.5


def _has_meaningful_query(query_tokens: list[str]) -> bool:
    latin = {
        token
        for token in query_tokens
        if re.fullmatch(r"[a-z0-9_@.-]+", token)
        and len(token.strip("@.-")) >= 2
        and token not in LATIN_STOP_WORDS
    }
    chinese = {
        token
        for token in query_tokens
        if re.fullmatch(r"[\u4e00-\u9fff]{2}", token) and token not in CHINESE_STOP_BIGRAMS
    }
    return bool(latin or chinese)


def search(
    query: str,
    *,
    role: str = "researcher",
    top_k: int = 8,
    category: str | None = None,
    db_path: Path | None = None,
) -> dict:
    query = (query or "").strip()
    if not query:
        return {
            "query": query,
            "results": [],
            "retrieval_mode": "hybrid_lexical_offline_feature_vector",
            "notice": "请输入检索问题。",
        }
    query_tokens = tokenize(query)
    if not _has_meaningful_query(query_tokens):
        return {
            "query": query,
            "results": [],
            "result_count": 0,
            "margin": 0.0,
            "retrieval_mode": "hybrid_lexical_offline_feature_vector",
            "invalid_vector_count": 0,
            "notice": "问题缺少可用于检索的有效关键词，请补充人物、事件、主题或来源。",
        }
    query_vectors: dict[int, tuple[list[float] | None, bool, str]] = {}
    semantic_active = False
    embedding_model = ""
    allowed = _allowed_sensitivity(role)
    placeholders = ",".join("?" for _ in allowed)
    with db(db_path) as conn:
        current_batch_code = active_batch_code(conn)
        if not current_batch_code:
            return {
                "query": query,
                "results": [],
                "result_count": 0,
                "margin": 0.0,
                "retrieval_mode": "hybrid_lexical_offline_feature_vector",
                "invalid_vector_count": 0,
                "notice": "当前没有可检索的数据批次。",
            }
        sql = f"""
            SELECT kc.id AS chunk_id,kc.text,kc.tokens_json,kc.vector,kc.dimensions,
                   sr.id AS record_id,sr.category,sr.title,sr.summary,sr.evidence_type,
                   sr.source_refs_json,sr.sensitivity,sr.content_hash,
                   db.code AS batch_code,db.source_date
            FROM knowledge_chunk kc
            JOIN source_record sr ON sr.id=kc.record_id
            JOIN dataset_batch db ON db.id=sr.batch_id
            WHERE db.code=? AND sr.sensitivity IN ({placeholders})
        """
        args: list[object] = [current_batch_code, *allowed]
        if category:
            sql += " AND sr.category=?"
            args.append(category)
        rows = conn.execute(sql, args).fetchall()

    results = []
    evidence_bonus = {
        "direct_post_excerpt": 0.06,
        "explicit_source_text": 0.06,
        "reported_by_source_file": 0.03,
        "source_conflict": -0.02,
        "production_gap": -0.03,
        "inference_or_speculation": -0.04,
    }
    invalid_vectors = 0
    for row in rows:
        try:
            doc_tokens = json.loads(row["tokens_json"])
            if not isinstance(doc_tokens, list) or not all(isinstance(token, str) for token in doc_tokens):
                raise ValueError("词元索引格式无效")
            dimensions = int(row["dimensions"])
            blob = bytes(row["vector"])
            if dimensions <= 0 or len(blob) != dimensions * 4:
                raise ValueError("向量长度与维度不一致")
            entry = query_vectors.get(dimensions)
            if entry is None:
                query_vec, active, model_key, _model_name = semantic_engine.embed_query(query, dimensions)
                entry = (query_vec, active, model_key)
                query_vectors[dimensions] = entry
                if active:
                    semantic_active = True
                    embedding_model = MODEL_LABELS.get(model_key, model_key)
            query_vec, _active, _label = entry
            if query_vec is None:
                raise ValueError("该维度缺少可用语义向量")
            unpacked = unpack_vector(blob, dimensions)
            if not all(math.isfinite(value) for value in unpacked) or not any(value != 0.0 for value in unpacked):
                raise ValueError("向量包含非有限值或为空")
            vector = max(0.0, cosine(query_vec, unpacked))
        except (ValueError, struct.error, TypeError, json.JSONDecodeError):
            doc_tokens = []
            vector = 0.0
            invalid_vectors += 1
        lexical = _lexical_score(query_tokens, doc_tokens)
        rules = _rule_score(query, row["title"], row["summary"], row["category"])
        score = max(0.0, min(1.0, 0.44 * lexical + 0.36 * vector + 0.20 * rules + evidence_bonus.get(row["evidence_type"], 0.0)))
        if (not _has_lexical_anchor(query_tokens, doc_tokens) and rules < 0.75) or score < 0.12:
            continue
        results.append({
            "record_id": row["record_id"],
            "title": row["title"],
            "summary": row["summary"],
            "category": row["category"],
            "category_label": CATEGORY_LABELS.get(row["category"], row["category"]),
            "score": round(score, 4),
            "score_breakdown": {
                "lexical": round(lexical, 4),
                "vector": round(vector, 4),
                "rule": round(rules, 4),
                "vector_model": embedding_model or "local",
            },
            "evidence_type": row["evidence_type"],
            "evidence_label": EVIDENCE_LABELS.get(row["evidence_type"], row["evidence_type"]),
            "source_refs": json.loads(row["source_refs_json"]),
            "content_hash": row["content_hash"],
            "sensitivity": row["sensitivity"],
            "batch_code": row["batch_code"],
            "source_date": row["source_date"],
        })
    results.sort(key=lambda item: (-item["score"], item["record_id"]))
    limited = results[: max(1, min(top_k, 20))]
    margin = round(limited[0]["score"] - limited[1]["score"], 4) if len(limited) > 1 else (limited[0]["score"] if limited else 0.0)
    mode = "hybrid_rule_lexical_semantic_embedding" if semantic_active else "hybrid_lexical_offline_feature_vector"
    if semantic_active:
        notice = f"当前使用词法 + 规则 + {embedding_model} 语义向量融合检索。"
    else:
        notice = "当前使用本地词法、规则与离线特征向量融合检索，不等同于大模型语义嵌入。"
    if invalid_vectors:
        notice += f" 检测到 {invalid_vectors} 个无效向量，已降级为词法与规则检索。"
    return {
        "query": query,
        "results": limited,
        "result_count": len(limited),
        "margin": margin,
        "retrieval_mode": mode,
        "invalid_vector_count": invalid_vectors,
        "embedding_active": semantic_active,
        "embedding_model": embedding_model,
        "notice": notice,
    }


def answer(query: str, *, role: str = "researcher", top_k: int = 5, db_path: Path | None = None) -> dict:
    payload = search(query, role=role, top_k=top_k, db_path=db_path)
    results = payload["results"]
    if not results:
        response = "知识库中没有找到足够相关的记录。请调整关键词后重试。"
    else:
        lines = []
        for index, item in enumerate(results[:3], start=1):
            summary = item["summary"].strip() or "该记录没有可展示摘要。"
            lines.append(f"{index}. {item['title']}：{summary}")
        response = "根据当前知识库的可追溯记录，检索到：\n" + "\n".join(lines)
        if any(item["evidence_type"] in {"source_conflict", "production_gap", "inference_or_speculation", "mixed_reported_and_inferred"} for item in results[:3]):
            response += "\n其中包含冲突、缺口或研究推断，请以引用中的证据性质为准，不应直接视为已核实事实。"
    return {
        "answer": response,
        "citations": results,
        "retrieval_mode": payload["retrieval_mode"],
        "notice": payload["notice"],
    }

from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any

import networkx as nx

from .database import ROOT, active_batch_code, db, verify_audit_chain


SNAPSHOT_PATH = ROOT / "src" / "zipSnapshot.json"

CATEGORY_TO_SNAPSHOT = {
    "account": "accounts",
    "actor": "familyMembers",
    "profile_signal": "profileSignals",
    "content": "tweets",
    "event": "timeline",
    "relationship_layer": "interactionLayers",
    "business_signal": "businessAndPolitics",
    "source": "sources",
    "analysis": "themeMatrix",
    "quality_conflict": "inconsistencies",
    "production_gap": "missingForProduction",
}


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", (value or "").lower()).strip("-") or "node"


def _source_ref(item: dict, fallback: str = "") -> str:
    refs = item.get("_source_refs") or item.get("sourceRefs") or item.get("sources") or []
    if isinstance(refs, str):
        refs = [refs]
    return str(refs[0]) if refs else fallback


def _source_refs(item: dict) -> list[str]:
    refs = item.get("_source_refs") or item.get("sourceRefs") or item.get("sources") or []
    if isinstance(refs, str):
        refs = [refs]
    return [str(ref) for ref in refs if ref]


def _item_sensitivity(item: dict, fallback: str = "INTERNAL") -> str:
    return str(item.get("_sensitivity") or fallback)


def _name_tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", (value or "").lower())
        if len(token) > 1 and token not in {"first", "lady", "official"}
    }


def _same_person(left: str, right: str) -> bool:
    left_tokens = _name_tokens(left)
    right_tokens = _name_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    return left_tokens == right_tokens or left_tokens.issubset(right_tokens) or right_tokens.issubset(left_tokens)


def _snapshot_from_database(db_path: Path | None = None) -> dict:
    with db(db_path) as conn:
        selected_code = active_batch_code(conn)
        batch = conn.execute("SELECT * FROM dataset_batch WHERE code=?", (selected_code,)).fetchone() if selected_code else None
        if not batch:
            batch = conn.execute("SELECT * FROM dataset_batch ORDER BY updated_at DESC,id DESC LIMIT 1").fetchone()
        if not batch:
            raise ValueError("数据库中没有可用于建图的数据批次")
        rows = conn.execute(
            """SELECT id,category,content_json,evidence_type,source_refs_json,
                      sensitivity,content_hash
                 FROM source_record WHERE batch_id=? ORDER BY category,id""",
            (batch["id"],),
        ).fetchall()
    metadata = json.loads(batch["metadata_json"])
    metadata["_batch_code"] = batch["code"]
    metadata["_record_count"] = batch["record_count"]
    snapshot: dict[str, Any] = {"meta": metadata}
    for source_key in CATEGORY_TO_SNAPSHOT.values():
        snapshot[source_key] = []
    for row in rows:
        source_key = CATEGORY_TO_SNAPSHOT.get(row["category"])
        if not source_key:
            continue
        value = json.loads(row["content_json"])
        if not isinstance(value, dict):
            value = {"value": value}
        value = dict(value)
        value.setdefault("evidence", row["evidence_type"])
        value["_record_id"] = row["id"]
        value["_sensitivity"] = row["sensitivity"]
        value["_content_hash"] = row["content_hash"]
        value["_source_refs"] = json.loads(row["source_refs_json"])
        snapshot[source_key].append(value)
    return snapshot


def _node(node_id: str, node_type: str, label: str, subtitle: str = "", *, sensitivity: str = "INTERNAL", evidence: str = "source_snapshot", metadata: dict | None = None) -> dict:
    return {
        "id": node_id,
        "type": node_type,
        "label": label,
        "subtitle": subtitle,
        "sensitivity": sensitivity,
        "evidence": evidence,
        "metadata": metadata or {},
    }


def _edge(source: str, target: str, relation: str, label: str, *, evidence: str = "source_snapshot", source_ref: str = "") -> dict:
    return {
        "id": f"edge:{source}:{relation}:{target}",
        "source": source,
        "target": target,
        "relation": relation,
        "label": label,
        "evidence": evidence,
        "source_ref": source_ref,
    }


def _unique_nodes(nodes: list[dict]) -> list[dict]:
    rank = {"INTERNAL": 0, "CONFIDENTIAL": 1, "RESTRICTED": 2}
    unique: dict[str, dict] = {}
    for node in nodes:
        current = unique.get(node["id"])
        if current is None or rank.get(node["sensitivity"], 0) >= rank.get(current["sensitivity"], 0):
            unique[node["id"]] = node
    return list(unique.values())


def _unique_edges(edges: list[dict]) -> list[dict]:
    return list({edge["id"]: edge for edge in edges}.values())


def _spread(count: int, index: int) -> float:
    if count <= 1:
        return 50.0
    return 8.0 + index * (84.0 / (count - 1))


def _layout(nodes: list[dict], view: str) -> None:
    columns = {
        "actors": {"person": 18, "account": 72, "topic": 92},
        "events": {"person": 8, "event": 38, "source": 82},
        "propagation": {"account": 8, "content": 43, "quoted_account": 76, "topic": 92},
        "evidence": {"dataset": 7, "category": 35, "quality": 67, "source_file": 91},
    }[view]
    by_type: dict[str, list[dict]] = {}
    for node in nodes:
        by_type.setdefault(node["type"], []).append(node)
    for node_type, values in by_type.items():
        values.sort(key=lambda item: item["label"])
        for index, node in enumerate(values):
            node["x"] = columns.get(node_type, 50)
            node["y"] = round(_spread(len(values), index), 2)
            node["size"] = 12 if node_type in {"dataset", "person"} else 9 if node_type in {"event", "account"} else 7


def _audit_chain_blocks(conn) -> tuple[list[dict], list[dict], dict]:
    rows = conn.execute("SELECT * FROM audit_block ORDER BY height").fetchall()
    verified = verify_audit_chain(conn)
    nodes: list[dict] = []
    edges: list[dict] = []
    previous_id = ""
    for row in rows:
        node_id = f"audit:{int(row['height'])}"
        nodes.append(_node(
            node_id, "audit", f"审计 #{int(row['height'])} · {row['event_type']}",
            f"{row['actor']} · {row['object_type']} · {row['outcome']}",
            sensitivity="INTERNAL", evidence="audit_chain",
            metadata={"height": row["height"], "prev_hash": row["prev_hash"], "event_hash": row["event_hash"], "block_time": row["block_time"]},
        ))
        if previous_id:
            edges.append(_edge(previous_id, node_id, "PREVIOUS_HASH", "上一哈希", evidence="audit_chain"))
        previous_id = node_id
    if nodes:
        nodes[0]["metadata"]["verified"] = bool(verified.get("valid"))
    return nodes, edges, verified


def _networkx_layout(graph) -> dict[str, tuple[float, float]]:
    if graph.number_of_nodes() == 0:
        return {}
    try:
        positions = nx.spring_layout(graph, seed=5497, k=1.0 / max(graph.number_of_nodes() ** 0.5, 1.0), iterations=60)
    except Exception:  # noqa: BLE001
        positions = _manual_layout(graph)
    xs = [point[0] for point in positions.values()]
    ys = [point[1] for point in positions.values()]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)

    def scale_value(value: float, low: float, high: float) -> float:
        if high == low:
            return 50.0
        return round((value - low) / (high - low) * 100.0, 2)

    return {
        node_id: (scale_value(point[0], x_min, x_max), scale_value(point[1], y_min, y_max))
        for node_id, point in positions.items()
    }


def _manual_layout(graph) -> dict[str, tuple[float, float]]:
    """Pure-Python fallback layout (no numpy) grouped by node type."""
    columns = {
        "dataset": 15, "category": 32, "quality": 52, "source_file": 78, "audit": 92,
        "person": 22, "account": 48, "topic": 74, "event": 40, "content": 55,
        "quoted_account": 70, "source": 82,
    }
    by_type: dict[str, list[str]] = {}
    for node in graph.nodes(data=True):
        node_id = node[0]
        node_type = node[1].get("type", "person")
        by_type.setdefault(node_type, []).append(node_id)
    positions: dict[str, tuple[float, float]] = {}
    for node_type, values in by_type.items():
        values.sort(key=lambda node_id: str(graph.nodes[node_id].get("label", node_id)))
        x = columns.get(node_type, 50)
        count = len(values)
        for index, node_id in enumerate(values):
            y = _spread(count, index) if count > 1 else 50.0
            positions[node_id] = (x, y)
    return positions
def _actors(snapshot: dict) -> tuple[list[dict], list[dict]]:
    nodes: list[dict] = []
    edges: list[dict] = []
    family = snapshot.get("familyMembers", [])
    accounts = snapshot.get("accounts", [])
    central_id = f"person:{family[0].get('id') or _slug(family[0].get('name', ''))}" if family else ""
    for member in family:
        person_id = f"person:{member.get('id') or _slug(member.get('name', ''))}"
        nodes.append(_node(
            person_id, "person", member.get("nameZh") or member.get("name") or "未命名人物",
            member.get("relation") or member.get("role") or "家族成员",
            sensitivity=_item_sensitivity(member, "RESTRICTED"), evidence=member.get("evidence", "explicit_source_text"), metadata=member,
        ))
        if person_id != central_id:
            edges.append(_edge(person_id, central_id, "RELATED_TO", member.get("relation") or "家族关系", evidence=member.get("evidence", "explicit_source_text"), source_ref=_source_ref(member)))

    for account in accounts:
        handle = account.get("handle") or account.get("id")
        account_id = f"account:{_slug(handle)}"
        nodes.append(_node(
            account_id, "account", handle, account.get("nameZh") or account.get("name") or "平台账号",
            sensitivity=_item_sensitivity(account, "RESTRICTED"), evidence=account.get("evidence", "explicit_source_text"), metadata=account,
        ))
        matching_member = next(
            (member for member in family if _same_person(str(member.get("name", "")), str(account.get("name", "")))),
            None,
        )
        if matching_member:
            person_id = f"person:{matching_member.get('id') or _slug(matching_member.get('name', ''))}"
            edges.append(_edge(person_id, account_id, "HAS_ACCOUNT", "拥有账号", evidence=account.get("evidence", "explicit_source_text"), source_ref=_source_ref(account)))
        for theme in (account.get("themes") or [])[:3]:
            topic_id = f"topic:{_slug(theme)}"
            nodes.append(_node(topic_id, "topic", theme, "账号主题", sensitivity=_item_sensitivity(account, "RESTRICTED"), evidence="source_analysis"))
            edges.append(_edge(account_id, topic_id, "ABOUT_TOPIC", "涉及主题", evidence="source_analysis", source_ref=_source_ref(account)))
    return _unique_nodes(nodes), _unique_edges(edges)


def _events(snapshot: dict) -> tuple[list[dict], list[dict]]:
    nodes: list[dict] = []
    edges: list[dict] = []
    actors = snapshot.get("familyMembers", [])
    actor_nodes = []
    for actor in actors:
        actor_id = f"person:{actor.get('id') or _slug(actor.get('name', ''))}"
        actor_nodes.append((actor_id, actor))
    for index, event in enumerate(snapshot.get("timeline", []), start=1):
        event_id = f"event:{index:03d}"
        nodes.append(_node(
            event_id, "event", event.get("title") or f"事件 {index}", event.get("date") or "日期未提供",
            sensitivity=_item_sensitivity(event, "RESTRICTED"), evidence=event.get("evidence", "reported_by_source_file"), metadata=event,
        ))
        text = f"{event.get('title', '')} {event.get('summary', '')}"
        for actor_id, actor in actor_nodes:
            names = [actor.get("nameZh"), actor.get("name")]
            if any(name and str(name) in text for name in names):
                nodes.append(_node(
                    actor_id, "person", actor.get("nameZh") or actor.get("name") or "人物",
                    actor.get("relation") or "家族成员", sensitivity=_item_sensitivity(actor, "RESTRICTED"),
                    evidence=actor.get("evidence", "explicit_source_text"), metadata=actor,
                ))
                edges.append(_edge(actor_id, event_id, "MENTIONED_IN", "事件提及", evidence=event.get("evidence", "reported_by_source_file"), source_ref=_source_ref(event)))
        for source in event.get("sources") or []:
            source_id = f"source:{_slug(source)}"
            nodes.append(_node(source_id, "source", source, "事件来源", sensitivity=_item_sensitivity(event, "RESTRICTED"), evidence="source_list_only"))
            edges.append(_edge(event_id, source_id, "SUPPORTED_BY", "来源支撑", evidence="source_list_only", source_ref=_source_ref(event, str(source))))
    return _unique_nodes(nodes), _unique_edges(edges)


def _propagation(snapshot: dict) -> tuple[list[dict], list[dict]]:
    nodes: list[dict] = []
    edges: list[dict] = []
    account_by_handle = {str(item.get("handle", "")).lower(): item for item in snapshot.get("accounts", [])}
    for index, post in enumerate(snapshot.get("tweets", []), start=1):
        content_id = f"content:{index:03d}"
        label = " / ".join(post.get("themes") or []) or f"内容 {index}"
        nodes.append(_node(
            content_id, "content", label, post.get("dateText") or "日期未提供",
            sensitivity=_item_sensitivity(post, "RESTRICTED"), evidence=post.get("evidence", "direct_post_excerpt"), metadata=post,
        ))
        handle = str(post.get("account") or "")
        account_id = f"account:{_slug(handle)}"
        account = account_by_handle.get(handle.lower(), {})
        nodes.append(_node(account_id, "account", handle or "未知账号", account.get("nameZh") or account.get("name") or "发布账号", sensitivity=_item_sensitivity(account, _item_sensitivity(post, "RESTRICTED")), evidence=account.get("evidence", "explicit_source_text"), metadata=account))
        edges.append(_edge(account_id, content_id, "PUBLISHED", "发布", evidence=post.get("evidence", "direct_post_excerpt"), source_ref=_source_ref(post)))
        quoted = post.get("quotedAccount")
        if quoted:
            quoted_id = f"quoted:{_slug(str(quoted))}"
            nodes.append(_node(quoted_id, "quoted_account", str(quoted), "被引用账号", sensitivity=_item_sensitivity(post, "RESTRICTED"), evidence=post.get("evidence", "direct_post_excerpt")))
            edges.append(_edge(content_id, quoted_id, "QUOTES", "引用", evidence=post.get("evidence", "direct_post_excerpt"), source_ref=_source_ref(post)))
        for theme in post.get("themes") or []:
            topic_id = f"topic:{_slug(theme)}"
            nodes.append(_node(topic_id, "topic", theme, "内容主题", sensitivity=_item_sensitivity(post, "RESTRICTED"), evidence="source_analysis"))
            edges.append(_edge(content_id, topic_id, "ABOUT_TOPIC", "涉及主题", evidence="source_analysis", source_ref=_source_ref(post)))
    return _unique_nodes(nodes), _unique_edges(edges)


def _evidence(snapshot: dict) -> tuple[list[dict], list[dict]]:
    nodes: list[dict] = []
    edges: list[dict] = []
    meta = snapshot.get("meta", {})
    batch_code = str(meta.get("_batch_code") or "test-dataset")
    batch_id = f"dataset:{_slug(batch_code)}"
    record_count = meta.get("_record_count") or sum(len(snapshot.get(key, [])) for key in snapshot if isinstance(snapshot.get(key), list))
    nodes.append(_node(batch_id, "dataset", "海外舆情监测批次", meta.get("asOf", ""), metadata={"record_count": record_count, "batch_code": batch_code}))
    categories = [
        ("account", "账号实体", "accounts", "RESTRICTED"),
        ("actor", "人物实体", "familyMembers", "RESTRICTED"),
        ("profile", "画像信号", "profileSignals", "RESTRICTED"),
        ("content", "逐帖内容", "tweets", "RESTRICTED"),
        ("event", "事件线索", "timeline", "RESTRICTED"),
        ("relation", "关系圈层", "interactionLayers", "RESTRICTED"),
        ("business", "商业政治信号", "businessAndPolitics", "RESTRICTED"),
        ("source", "来源台账", "sources", "RESTRICTED"),
        ("analysis", "研究分析", "themeMatrix", "RESTRICTED"),
    ]
    sensitivity_rank = {"INTERNAL": 0, "CONFIDENTIAL": 1, "RESTRICTED": 2}
    for key, label, snapshot_key, fallback_sensitivity in categories:
        items = snapshot.get(snapshot_key, [])
        count = len(items)
        sensitivity = max(
            (_item_sensitivity(item, fallback_sensitivity) for item in items if isinstance(item, dict)),
            key=lambda value: sensitivity_rank.get(value, 0),
            default=fallback_sensitivity,
        )
        category_id = f"category:{key}"
        nodes.append(_node(category_id, "category", label, f"{count} 条", sensitivity=sensitivity, metadata={"count": count}))
        edges.append(_edge(batch_id, category_id, "CONTAINS", "包含"))
        source_files = sorted({ref for item in items if isinstance(item, dict) for ref in _source_refs(item)})
        for source_file in source_files:
            file_id = f"file:{_slug(source_file)}"
            nodes.append(_node(file_id, "source_file", source_file, "记录来源锚点", sensitivity=sensitivity, evidence="explicit_source_text"))
            edges.append(_edge(category_id, file_id, "DERIVED_FROM", "派生自", source_ref=source_file))
    for index, issue in enumerate(snapshot.get("inconsistencies", []), start=1):
        issue = issue if isinstance(issue, dict) else {"value": issue}
        issue_id = f"quality:conflict:{index:03d}"
        nodes.append(_node(issue_id, "quality", issue.get("field") or f"口径冲突 {index}", "跨文件冲突", sensitivity=_item_sensitivity(issue, "RESTRICTED"), evidence="source_conflict", metadata=issue))
        edges.append(_edge(batch_id, issue_id, "HAS_QUALITY_ISSUE", "发现问题", evidence="source_conflict"))
    for index, gap in enumerate(snapshot.get("missingForProduction", []), start=1):
        gap = gap if isinstance(gap, dict) else {"value": gap}
        issue_id = f"quality:gap:{index:03d}"
        nodes.append(_node(issue_id, "quality", str(gap.get("value") or gap), "生产缺口", sensitivity=_item_sensitivity(gap, "INTERNAL"), evidence="production_gap", metadata=gap))
        edges.append(_edge(batch_id, issue_id, "HAS_QUALITY_ISSUE", "待补字段", evidence="production_gap"))
    return _unique_nodes(nodes), _unique_edges(edges)


BUILDERS = {
    "actors": _actors,
    "events": _events,
    "propagation": _propagation,
    "evidence": _evidence,
}


def build_graph(
    view: str = "actors",
    *,
    role: str = "researcher",
    snapshot_path: Path | None = None,
    db_path: Path | None = None,
) -> dict:
    if view not in BUILDERS:
        raise ValueError("未知图谱视图")
    selected_view = view
    if role not in {"core", "researcher"}:
        raise ValueError("未知角色，已拒绝访问")
    snapshot = (
        json.loads(Path(snapshot_path).read_text(encoding="utf-8"))
        if snapshot_path
        else _snapshot_from_database(db_path)
    )
    nodes, edges = BUILDERS[selected_view](snapshot)
    hidden = 0
    if role.lower() == "researcher":
        hidden_ids = {node["id"] for node in nodes if node["sensitivity"] == "RESTRICTED"}
        hidden = len(hidden_ids)
        nodes = [node for node in nodes if node["id"] not in hidden_ids]
        edges = [edge for edge in edges if edge["source"] not in hidden_ids and edge["target"] not in hidden_ids]
    node_ids = {node["id"] for node in nodes}
    edges = [edge for edge in edges if edge["source"] in node_ids and edge["target"] in node_ids]

    # Merge the tamper-evident audit chain into the graph as trusted nodes.
    audit_verification: dict = {"valid": False, "block_count": 0}
    try:
        with db(db_path) as conn:
            audit_nodes, audit_edges, audit_verification = _audit_chain_blocks(conn)
        nodes = nodes + audit_nodes
        edges = edges + audit_edges
    except Exception:  # noqa: BLE001 - keep graph functional without audit blocks
        category_index = len(nodes) + 1
        nodes.append(_node(f"audit:{category_index}", "audit", "审计链不可用", "需要数据库审计块", sensitivity="INTERNAL", evidence="audit_chain"))

    graph = nx.DiGraph(batch_id=selected_view)
    for node in nodes:
        graph.add_node(node["id"], **{k: v for k, v in node.items() if k not in {"x", "y"}})
    for edge in edges:
        graph.add_edge(edge["source"], edge["target"], **edge)

    positions = _networkx_layout(graph)
    for node in nodes:
        node["x"], node["y"] = positions.get(node["id"], (50.0, 50.0))
        node_type = node.get("type", "")
        node["size"] = 12 if node_type in {"dataset", "person"} else 9 if node_type in {"event", "account"} else 7

    type_counts = Counter(node["type"] for node in nodes)
    relation_counts = Counter(edge["relation"] for edge in edges)
    degree = nx.degree_centrality(graph) if graph.number_of_nodes() else {}
    weighted_degree = nx.degree_centrality(graph) if graph.number_of_nodes() else {}
    connected = (
        nx.number_weakly_connected_components(graph) if graph.number_of_nodes() else 0
    )
    for node in nodes:
        node["centrality"] = round(float(degree.get(node["id"], 0.0)), 4)

    return {
        "view": selected_view,
        "directed": True,
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "node_types": dict(type_counts),
            "relation_types": dict(relation_counts),
            "hidden_restricted": hidden,
            "connected_components": int(connected),
            "engine": "networkx",
            "audit_chain": audit_verification,
        },
        "notice": "图谱由 NetworkX 构建，含可核验审计链；证据类型和来源锚点随边返回，研究推断不等同于事实。",
    }

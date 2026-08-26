from __future__ import annotations

import json
import re

from .database import db, init_db, now_iso, rows_to_dicts


PLATFORMS = [
    "x", "truth-social", "facebook", "tiktok", "youtube", "instagram",
    "reuters", "ap", "nyt", "wsj", "cnn", "bbc",
]


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", (value or "").lower()).strip("-") or "item"


def normalize(item: dict, vendor: str, collected_at: str) -> dict:
    """数据商返回的一条数据 -> 我方 collected_item 字段。"""
    return {
        "vendor_item_id": str(item.get("vendor_item_id") or item.get("id") or ""),
        "vendor": vendor,
        "platform": str(item.get("platform") or ""),
        "platform_item_type": str(item.get("platform_item_type") or item.get("type") or "post"),
        "author_name": str(item.get("author_name") or ""),
        "author_handle": str(item.get("author_handle") or ""),
        "url": str(item.get("url") or ""),
        "title": str(item.get("title") or ""),
        "content": str(item.get("content") or ""),
        "media_type": str(item.get("media_type") or "text"),
        "language": str(item.get("language") or "zh"),
        "published_at": str(item.get("published_at") or ""),
        "collected_at": collected_at,
        "likes_count": int(item.get("likes_count") or 0),
        "reposts_count": int(item.get("reposts_count") or 0),
        "comments_count": int(item.get("comments_count") or 0),
        "related_accounts_json": json.dumps(item.get("related_accounts") or [], ensure_ascii=False),
        "raw_json": json.dumps(item, ensure_ascii=False),
    }


class DataVendor:
    """数据商适配器接口：按查询条件返回规范化前的结果列表。"""

    def fetch(self, query: dict) -> list[dict]:
        raise NotImplementedError


class MockVendor(DataVendor):
    """沙箱数据商：返回模拟采集结果，演示真实采集链路。"""

    def fetch(self, query: dict) -> list[dict]:
        platforms = query.get("platforms") or ["x"]
        target = str(query.get("target") or "涉华")
        media_types = query.get("media_types") or ["text"]
        languages = query.get("languages") or ["zh"]
        results = []
        for i, platform in enumerate(platforms):
            slug = _slug(target)
            results.append({
                "vendor_item_id": f"mock-{platform}-{slug}-{i}",
                "platform": platform,
                "platform_item_type": "post",
                "author_name": target,
                "author_handle": "@" + slug,
                "url": f"https://example.com/{platform}/{slug}-{i}",
                "title": f"关于「{target}」的公开内容",
                "content": f"这是一条来自 {platform} 的模拟采集内容，主题：{target}。",
                "media_type": media_types[i % len(media_types)],
                "language": languages[i % len(languages)],
                "published_at": "2026-08-26T12:00:00Z",
                "likes_count": 123 + i,
                "reposts_count": 45 + i,
                "comments_count": 6 + i,
                "related_accounts": ["@related-a", "@related-b"],
            })
        return results


VENDORS: dict[str, DataVendor] = {
    "mock": MockVendor(),
}


def list_vendors() -> list[dict]:
    return [
        {
            "id": name,
            "name": name,
            "type": "mock" if isinstance(v, MockVendor) else "http",
            "status": "READY" if isinstance(v, MockVendor) else "NOT_CONFIGURED",
            "platforms": PLATFORMS,
        }
        for name, v in VENDORS.items()
    ]


def fetch_and_store(query: dict, vendor_name: str = "mock") -> dict:
    init_db()
    vendor = VENDORS.get(vendor_name)
    if vendor is None:
        raise ValueError(f"未知数据商：{vendor_name}")
    ts = now_iso()
    raw_items = vendor.fetch(query)
    normalized = [normalize(item, vendor_name, ts) for item in raw_items]
    stored = 0
    with db() as conn:
        for n in normalized:
            conn.execute(
                """INSERT INTO collected_item
                   (vendor_item_id,vendor,platform,platform_item_type,author_name,author_handle,
                    url,title,content,media_type,language,published_at,collected_at,
                    likes_count,reposts_count,comments_count,related_accounts_json,raw_json)
                   VALUES(:vendor_item_id,:vendor,:platform,:platform_item_type,:author_name,:author_handle,
                    :url,:title,:content,:media_type,:language,:published_at,:collected_at,
                    :likes_count,:reposts_count,:comments_count,:related_accounts_json,:raw_json)
                   ON CONFLICT(vendor, vendor_item_id) DO UPDATE SET
                    likes_count=excluded.likes_count,reposts_count=excluded.reposts_count,
                    comments_count=excluded.comments_count,raw_json=excluded.raw_json""",
                n,
            )
            stored += 1
    return {"vendor": vendor_name, "fetched": len(raw_items), "stored": stored}


def list_collected_items(
    *,
    platform: str | None = None,
    media_type: str | None = None,
    limit: int = 200,
) -> list[dict]:
    sql = "SELECT * FROM collected_item"
    args: list[object] = []
    conds = []
    if platform:
        conds.append("platform=?")
        args.append(platform)
    if media_type:
        conds.append("media_type=?")
        args.append(media_type)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with db() as conn:
        rows = rows_to_dicts(conn.execute(sql, args).fetchall())
    for row in rows:
        row["related_accounts"] = json.loads(row.pop("related_accounts_json"))
        row["raw"] = json.loads(row.pop("raw_json"))
    return rows

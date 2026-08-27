"""Collect a small, auditable public-web sample for the demo workbench.

The collector deliberately uses only directly readable RSS/Atom feeds and
public HTML metadata. It never submits credentials, solves challenges, or
attempts to evade access controls. Social profile probes are kept as channel
observations; only topic-matching feed/article metadata is written as content.

Usage from the repository root::

    python scripts/collect_public_web.py
    python scripts/collect_public_web.py --dry-run
"""

from __future__ import annotations

import argparse
import email.utils
import hashlib
import html
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPO_ROOT / "data" / "public_demo_data.json"
USER_AGENT = "OverseasOpinionMonitor/1.0 (+public-rss-only)"
MAX_BODY_BYTES = 3 * 1024 * 1024
BLOCKED_MARKERS = (
    "cf-chl",
    "captcha",
    "verify you are human",
    "access denied",
    "attention required! | cloudflare",
    "unusual traffic",
    "enable javascript and cookies",
    "sign in to continue",
    "login to continue",
    "subscribe to continue",
    "just a moment...",
)


CHANNELS: tuple[dict[str, Any], ...] = (
    {
        "id": "x",
        "platform": "X",
        "kind": "page",
        "url": "https://x.com/XHNews",
        "method": "public_profile_probe",
        "note": "公开主页元数据探测；主题检索和批量帖子读取需平台授权。",
    },
    {
        "id": "truth-social",
        "platform": "Truth Social",
        "kind": "page",
        "url": "https://truthsocial.com/@realDonaldTrump",
        "method": "public_profile_probe",
        "note": "公开主页探测；当前入口返回访问控制响应，未绕过。",
    },
    {
        "id": "facebook",
        "platform": "Facebook",
        "kind": "page",
        "url": "https://www.facebook.com/XinhuaNewsAgency/",
        "method": "public_profile_probe",
        "note": "公开主页元数据可见；登录覆盖层下的帖子内容不入库。",
    },
    {
        "id": "tiktok",
        "platform": "TikTok",
        "kind": "page",
        "url": "https://www.tiktok.com/@xinhuaofficial",
        "method": "public_profile_probe",
        "note": "公开主页可访问；短视频列表需要稳定公开接口后再纳入内容批次。",
    },
    {
        "id": "youtube",
        "platform": "YouTube",
        "kind": "feed",
        "url": "https://www.youtube.com/feeds/videos.xml?channel_id=UC16niRr50-MSBwiO3YDb3RA",
        "method": "public_atom_feed",
        "note": "读取官方公开 Atom 视频元数据，不抓取评论或受限内容。",
    },
    {
        "id": "instagram",
        "platform": "Instagram",
        "kind": "page",
        "url": "https://www.instagram.com/xinhuanews/",
        "method": "public_profile_probe",
        "note": "公开主页元数据可见；帖子列表需要稳定公开接口后再纳入内容批次。",
    },
    {
        "id": "reuters",
        "platform": "Reuters",
        "kind": "page",
        "url": "https://www.reuters.com/world/china/",
        "method": "public_page_probe",
        "note": "公开页面探测；当前入口需要授权响应，未绕过。",
    },
    {
        "id": "ap",
        "platform": "Associated Press",
        "kind": "html_list",
        "url": "https://apnews.com/hub/china",
        "method": "public_html_list",
        "note": "读取公开专题页可见标题和链接，短摘要以页面元数据为限。",
    },
    {
        "id": "nyt",
        "platform": "The New York Times",
        "kind": "feed",
        "url": "https://rss.nytimes.com/services/xml/rss/nyt/AsiaPacific.xml",
        "method": "public_rss",
        "note": "读取官方 RSS 标题、短摘要、发布时间和来源链接。",
    },
    {
        "id": "wsj",
        "platform": "The Wall Street Journal",
        "kind": "feed",
        "url": "https://feeds.a.dj.com/rss/RSSWorldNews.xml",
        "method": "public_rss",
        "note": "读取公开 RSS 元数据；付费正文不抓取。",
    },
    {
        "id": "cnn",
        "platform": "CNN",
        "kind": "feed",
        "url": "https://rss.cnn.com/rss/edition_world.rss",
        "method": "public_rss_and_html",
        "note": "读取公开 RSS 元数据；不采集评论、正文或受限内容。",
    },
    {
        "id": "bbc",
        "platform": "BBC",
        "kind": "feed",
        "url": "https://feeds.bbci.co.uk/news/world/asia/china/rss.xml",
        "method": "public_rss",
        "note": "读取官方 RSS 标题、短摘要、发布时间和来源链接。",
    },
)


TOPIC_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("雄安新区", ("xiong'an", "xiongan", "xiong an", "雄安")),
    ("APEC 2026", ("apec", "asia-pacific economic cooperation", "亚太经合", "亚太经济合作")),
    (
        "习近平海外活动",
        (
            "xi jinping",
            "president xi",
            "xi's visit",
            "xi visits",
            "xi arrives",
            "习近平",
            "pyongyang",
            "north korea",
            "kim jong",
        ),
    ),
)

CNN_TRANSCRIPT_SUMMARY = (
    "Public CNN transcript page about Xi Jinping's overseas activity; only "
    "the program heading, air date and source URL are retained."
)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def clean_text(value: str, limit: int = 420) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<script\b[^>]*>.*?</script>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<style\b[^>]*>.*?</style>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) > limit:
        return value[: limit - 1].rstrip() + "…"
    return value


def fetch(url: str, timeout: int) -> tuple[int, str, str]:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/rss+xml,application/atom+xml,application/xml;q=0.9,*/*;q=0.8"})
    try:
        with urlopen(request, timeout=timeout) as response:
            status_value = getattr(response, "status", None)
            status = int(status_value if status_value is not None else response.getcode())
            headers = getattr(response, "headers", None)
            content_type = str(headers.get("Content-Type") or "") if headers is not None else ""
            body = response.read(MAX_BODY_BYTES + 1)
            if len(body) > MAX_BODY_BYTES:
                body = body[:MAX_BODY_BYTES]
            get_charset = getattr(headers, "get_content_charset", None)
            charset = (get_charset() if callable(get_charset) else None) or "utf-8"
            try:
                text = body.decode(charset, errors="replace")
            except LookupError:
                text = body.decode("utf-8", errors="replace")
            lowered = text[:32768].lower()
            if any(marker in lowered for marker in BLOCKED_MARKERS):
                return status, content_type, "__ACCESS_CONTROL_MARKER__"
            final_path = urlsplit(str(response.geturl())).path.lower()
            if any(token in final_path for token in ("/login", "/signin", "/challenge")):
                return status, content_type, "__ACCESS_CONTROL_MARKER__"
            return status, content_type, text
    except HTTPError as exc:
        return int(exc.code), "", ""
    except TimeoutError:
        return 0, "", "请求超时"
    except URLError as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
            return 0, "", "请求超时"
        return 0, "", "网络连接失败"
    except OSError:
        return 0, "", "网络连接失败"


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def child_text(element: ElementTree.Element, *names: str) -> str:
    wanted = {name.lower() for name in names}
    for child in list(element):
        if local_name(child.tag) in wanted:
            return clean_text("".join(child.itertext()), 1000)
    return ""


def parse_date(value: str) -> str:
    value = clean_text(value, 100)
    if not value:
        return ""
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone().isoformat(timespec="seconds")
    except (TypeError, ValueError, IndexError):
        pass
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone().isoformat(timespec="seconds")
    except ValueError:
        return value


def classify(title: str, summary: str) -> str | None:
    haystack = f"{title} {summary}".lower()
    if any(keyword.lower() in haystack for keyword in TOPIC_RULES[0][1]):
        return "雄安新区"
    # Keep the year in the rule: a generic APEC article is not an APEC 2026
    # demo record.  The compact form also catches ``APEC2026`` headlines.
    apec_hit = bool(re.search(r"\bapec(?:\s*[-_/]?\s*\d{4})?\b", haystack)) or any(
        keyword in haystack for keyword in ("亚太经合", "亚太经济合作")
    )
    if apec_hit and "2026" in haystack:
        return "APEC 2026"
    # A country/venue word such as "North Korea" or "Pyongyang" is context,
    # not proof that the item is about Xi.  Require an explicit Xi mention.
    xi_hit = "习近平" in haystack or bool(
        re.search(
            r"\b(?:xi jinping|president xi(?:'s)?|xi(?:'s)?\s+(?:visits?|arrives?))\b",
            haystack,
        )
    )
    overseas_hit = any(
        marker in haystack
        for marker in (
            "state visit", "overseas", "abroad", "foreign", "summit", "bilateral",
            "dprk", "north korea", "vietnam", "russia", "europe", "africa", "asean",
            "pyongyang", "tokyo", "seoul", "washington", "moscow", "meeting with", "meets",
            "海外", "出访", "外访", "外事", "国外", "国际", "访越", "访俄", "访朝", "峰会", "会见",
        )
    )
    if xi_hit and overseas_hit:
        return "习近平海外活动"
    return None


def parse_feed(text: str, platform: str, source_url: str, limit: int) -> list[dict[str, Any]]:
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return []
    elements = [element for element in root.iter() if local_name(element.tag) in {"item", "entry"}]
    items: list[dict[str, Any]] = []
    for element in elements[:limit]:
        title = child_text(element, "title")
        summary = child_text(element, "description", "summary", "content", "encoded")
        link = ""
        for child in list(element):
            if local_name(child.tag) != "link":
                continue
            link = str(child.attrib.get("href") or "").strip() or clean_text("".join(child.itertext()), 1000)
            if link:
                break
        published = child_text(element, "pubdate", "published", "updated", "date")
        topic = classify(title, summary)
        if not topic or not title or not link:
            continue
        items.append(
            {
                "topic": topic,
                "platform": platform,
                "title": title,
                "summary": clean_text(summary, 360) or title,
                "author_or_channel": platform,
                "published_at": parse_date(published),
                "original_url": link,
                "language": "en",
                "country_region": "International",
                "interaction": None,
                "acquisition_method": "Public RSS/Atom feed",
                "access_status": "Publicly readable; title and short summary only",
                "source_feed": source_url,
            }
        )
    return items


def feed_entries_seen(text: str, limit: int) -> int:
    """Count feed entries actually considered without treating matches as all input."""

    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return 0
    return min(limit, sum(local_name(element.tag) in {"item", "entry"} for element in root.iter()))


def public_links_seen(text: str, source_url: str) -> int:
    """Count unique public AP links considered by the bounded HTML parser."""

    seen: set[str] = set()
    pattern = re.compile(r"<a\b([^>]+)>(.*?)</a>", flags=re.I | re.S)
    for match in pattern.finditer(text):
        href_match = re.search(r"href\s*=\s*['\"]([^'\"]+)['\"]", match.group(1), flags=re.I)
        if not href_match:
            continue
        url = urljoin(source_url, html.unescape(href_match.group(1)).strip())
        if url.startswith("https://apnews.com/"):
            seen.add(url)
    return len(seen)


def meta_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    pattern = re.compile(r"<meta\b([^>]+)>", flags=re.I | re.S)
    for match in pattern.finditer(text):
        attrs = match.group(1)
        key_match = re.search(r"(?:property|name)\s*=\s*['\"]([^'\"]+)['\"]", attrs, flags=re.I)
        value_match = re.search(r"content\s*=\s*['\"]([^'\"]*)['\"]", attrs, flags=re.I)
        if key_match and value_match:
            values[key_match.group(1).lower()] = clean_text(value_match.group(1), 500)
    title_match = re.search(r"<title\b[^>]*>(.*?)</title>", text, flags=re.I | re.S)
    if title_match:
        values.setdefault("title", clean_text(title_match.group(1), 500))
    return values


def parse_html_list(text: str, platform: str, source_url: str, limit: int) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    pattern = re.compile(r"<a\b([^>]+)>(.*?)</a>", flags=re.I | re.S)
    seen: set[str] = set()
    for match in pattern.finditer(text):
        attrs, raw_title = match.groups()
        href_match = re.search(r"href\s*=\s*['\"]([^'\"]+)['\"]", attrs, flags=re.I)
        if not href_match:
            continue
        url = urljoin(source_url, html.unescape(href_match.group(1)).strip())
        title = clean_text(raw_title, 360)
        if not url.startswith("https://apnews.com/") or not title or url in seen:
            continue
        topic = classify(title, "")
        if not topic:
            continue
        seen.add(url)
        result.append(
            {
                "topic": topic,
                "platform": platform,
                "title": title,
                "summary": title,
                "author_or_channel": "AP News",
                "published_at": "",
                "original_url": url,
                "language": "en",
                "country_region": "International",
                "interaction": None,
                "acquisition_method": "Public AP hub HTML list",
                "access_status": "Publicly readable; short title only",
                "source_feed": source_url,
            }
        )
        if len(result) >= limit:
            break
    return result


def parse_cnn_transcript(text: str, platform: str, source_url: str) -> list[dict[str, Any]]:
    subhead = ""
    match = re.search(r"class=['\"]cnnTransSubHead['\"][^>]*>(.*?)</", text, flags=re.I | re.S)
    if match:
        subhead = clean_text(match.group(1), 900)
    lowered = text.lower()
    positions = [
        position
        for marker in ("xi jinping", "president xi", "xi visits", "习近平")
        if (position := lowered.find(marker)) >= 0
    ]
    if not positions:
        return []
    start = min(positions)
    context = clean_text(text[start : start + 1200], 1200)
    topic = classify(subhead, context)
    if topic != "习近平海外活动":
        return []
    title = "CNN transcript: " + (subhead.split(". Aired", 1)[0].split(";")[-1].strip() or "Xi Jinping overseas activity")
    excerpt = CNN_TRANSCRIPT_SUMMARY
    date_match = re.search(r"/date/(\d{4}-\d{2}-\d{2})/", source_url)
    published = date_match.group(1) + "T00:00:00+00:00" if date_match else ""
    return [
        {
            "topic": topic,
            "platform": platform,
            "title": clean_text(title, 360),
            "summary": clean_text(excerpt, 360),
            "author_or_channel": "CNN International",
            "published_at": published,
            "original_url": source_url,
            "language": "en",
            "country_region": "China / North Korea",
            "interaction": None,
            "acquisition_method": "Public CNN HTML transcript",
            "access_status": "Publicly readable; short transcript heading only",
            "source_feed": source_url,
        }
    ]


def stable_id(item: dict[str, Any]) -> str:
    digest = hashlib.sha256(str(item["original_url"]).encode("utf-8")).hexdigest()[:12]
    platform = re.sub(r"[^a-z0-9]+", "-", str(item.get("platform") or "source").lower()).strip("-")
    return f"web-{platform or 'source'}-{digest}"


def observation(
    channel: dict[str, Any],
    *,
    status: str,
    checked_at: str,
    http_status: int,
    method: str,
    observation_text: str,
    limitation: str,
    items_seen: int = 0,
    topic_matches: int = 0,
    records_added: int = 0,
) -> dict[str, Any]:
    # `reason` is the machine-readable counterpart of the display text.  Keep
    # both names for consumers that already use the older observation field.
    return {
        "platform": channel["platform"],
        "url": channel["url"],
        "status": status,
        "reason": observation_text,
        "method": method,
        "http_status": http_status or None,
        "items_seen": items_seen,
        "records_seen": items_seen,
        "topic_matches": topic_matches,
        "records_added": records_added,
        "checked_at": checked_at,
        "observation": observation_text,
        "limitation": limitation,
    }


def collect_channel(channel: dict[str, Any], timeout: int, limit: int, checked_at: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    # Exactly one ordinary request is made per configured channel.  A failed
    # endpoint is recorded as-is; no fallback, retry, or access-control probe
    # is attempted.
    urls = [str(channel["url"])]
    last_status = 0
    last_error = ""
    feed_items: list[dict[str, Any]] = []
    feed_success_status = 0
    feed_attempts = 0
    feed_records_seen = 0
    for index, url in enumerate(urls):
        status, content_type, body = fetch(url, timeout)
        last_status = status
        if status == 0:
            last_error = body
            continue
        if status in {401, 402, 403, 407, 451}:
            last_error = f"HTTP {status}"
            continue
        if status >= 400:
            last_error = f"HTTP {status}"
            continue
        if body == "__ACCESS_CONTROL_MARKER__":
            return [], observation(
                channel,
                status="ACCESS_RESTRICTED",
                checked_at=checked_at,
                http_status=status,
                method=channel["method"],
                observation_text="公开入口返回登录、验证码、付费墙、机器人挑战或访问控制标记；未重试或绕过。",
                limitation=channel["note"],
            )
        if channel["kind"] == "feed":
            feed_attempts += 1
            feed_records_seen += feed_entries_seen(body, limit)
            items = parse_feed(body, channel["platform"], url, limit)
            feed_items.extend(items)
            feed_success_status = status
            continue
        if channel["kind"] == "html_list":
            items = parse_html_list(body, channel["platform"], url, limit)
            links_seen = public_links_seen(body, url)
            return items, observation(
                channel,
                status="PUBLIC_READABLE",
                checked_at=checked_at,
                http_status=status,
                method=channel["method"],
                observation_text=f"公开专题页可读取，检查 {links_seen} 个公开链接，发现 {len(items)} 条命中专题。",
                limitation=channel["note"],
                items_seen=links_seen,
                topic_matches=len(items),
                records_added=len(items),
            )
        values = meta_values(body)
        description = values.get("og:description") or values.get("description") or ""
        title = values.get("og:title") or values.get("twitter:title") or values.get("title") or ""
        return [], observation(channel, status="PUBLIC_READABLE", checked_at=checked_at, http_status=status, method=channel["method"], observation_text=f"公开主页可读取元数据（{clean_text(title, 100)}）。未将主页描述当作主题帖子入库。", limitation=channel["note"] + (f" 可见描述：{clean_text(description, 160)}" if description else ""), items_seen=0)
    if channel["kind"] == "feed" and feed_attempts:
        deduped: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for item in feed_items:
            if item["original_url"] in seen_urls:
                continue
            seen_urls.add(item["original_url"])
            deduped.append(item)
        return deduped, observation(
            channel,
            status="PUBLIC_READABLE",
            checked_at=checked_at,
            http_status=feed_success_status,
            method=channel["method"],
            observation_text=(
                f"公开 RSS/Atom 可读取，发现 {len(deduped)} 条命中专题的元数据。"
                if deduped
                else "公开 RSS/Atom 可读取，但本次条目未命中当前三个演示专题。"
            ),
            limitation=channel["note"],
            items_seen=feed_records_seen,
            topic_matches=len(feed_items),
            records_added=len(deduped),
        )
    status = "ACCESS_RESTRICTED" if last_status in {401, 402, 403, 407, 429, 451} else "FAILED"
    reason = last_error or "未得到可读取响应"
    return [], observation(channel, status=status, checked_at=checked_at, http_status=last_status, method=channel["method"], observation_text=f"未写入内容：{reason}。", limitation=channel["note"])


def load_payload(path: Path) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"records": []}


def merge_payload(payload: dict[str, Any], new_items: list[dict[str, Any]], observations: list[dict[str, Any]], checked_at: str, scope: str) -> tuple[dict[str, Any], dict[str, Any]]:
    existing: list[dict[str, Any]] = []
    previous_collected_at = str(payload.get("collected_at") or checked_at)
    for source in payload.get("records") or []:
        if not isinstance(source, dict):
            continue
        item = dict(source)
        record_id = str(item.get("id") or "")
        if record_id.startswith("web-"):
            topic = classify(str(item.get("title") or ""), str(item.get("summary") or ""))
            if not topic:
                continue
            item["topic"] = topic
            if item.get("platform") == "CNN" and "transcripts.cnn.com" in str(item.get("original_url") or ""):
                item["summary"] = CNN_TRANSCRIPT_SUMMARY
        item.setdefault("collected_at", previous_collected_at)
        existing.append(item)
    by_url = {str(item.get("original_url")): item for item in existing if item.get("original_url")}
    added = 0
    duplicates = 0
    appended_platforms: set[str] = set()
    for item in new_items:
        url = str(item["original_url"])
        if url in by_url:
            duplicates += 1
            continue
        item = {"id": stable_id(item), "collected_at": checked_at, **item}
        existing.append(item)
        by_url[url] = item
        added += 1
        if item.get("platform"):
            appended_platforms.add(str(item["platform"]))
    existing.sort(key=lambda item: (str(item.get("topic") or ""), str(item.get("published_at") or ""), str(item.get("id") or "")))
    for index, item in enumerate(existing, start=1):
        item.setdefault("id", stable_id(item))
        item.setdefault("summary", item.get("title") or "")
        item.setdefault("interaction", None)
        item.setdefault("access_status", "Publicly readable; metadata only")
    public_channels = sum(str(item.get("status", "")).startswith("PUBLIC") for item in observations)
    source_platforms = sorted(appended_platforms)
    records_seen = sum(int(item.get("records_seen") or 0) for item in observations)
    topic_matches = sum(int(item.get("topic_matches") or 0) for item in observations)
    collection_run = {
        "run_id": f"public-web-{checked_at.replace(':', '').replace('+', '_')}",
        "status": "COMPLETED" if public_channels == len(observations) else ("PARTIAL" if public_channels else "FAILED"),
        "checked_at": checked_at,
        "policy": "One anonymous request per configured public endpoint; no credentials, cookies, login, CAPTCHA, paywall, bot challenge, or access-control bypass.",
        "attempted_platforms": len(observations),
        "publicly_readable_platforms": [item["platform"] for item in observations if str(item.get("status", "")).startswith("PUBLIC")],
        "restricted_or_failed_platforms": [item["platform"] for item in observations if not str(item.get("status", "")).startswith("PUBLIC")],
        "candidate_items_seen": records_seen,
        "topic_matching_items": topic_matches,
        "records_appended": added,
        "records_total": len(existing),
        "duplicate_records": duplicates,
        "source_platforms_added": source_platforms,
        "output": "",
    }
    result = {
        "collected_at": checked_at,
        "scope": scope,
        "collector": {"name": "collect_public_web.py", "version": "1.0", "run_at": checked_at},
        "records": existing,
        "platform_access_observations": observations,
        "collection_run": collection_run,
    }
    result["collection_summary"] = {
        "channels_checked": len(observations),
        "public_channels": public_channels,
        "blocked_or_limited_channels": len(observations) - public_channels,
        "records_total": len(existing),
        "records_added": added,
        "topics": {topic: sum(item.get("topic") == topic for item in existing) for topic, _ in TOPIC_RULES},
    }
    return result, result["collection_summary"]


def run(output: Path, timeout: int, limit: int, dry_run: bool) -> dict[str, Any]:
    checked_at = now_iso()
    all_items: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    for channel in CHANNELS:
        items, status = collect_channel(channel, timeout, limit, checked_at)
        all_items.extend(items)
        status["records_added"] = len(items)
        observations.append(status)
    payload = load_payload(output)
    merged, summary = merge_payload(
        payload,
        all_items,
        observations,
        checked_at,
        "公开网页小规模试采；仅保存公开标题、短摘要、发布时间、来源链接和渠道访问证据，不复制全文。",
    )
    merged["collection_run"]["status"] = "DRY_RUN" if dry_run else merged["collection_run"]["status"]
    try:
        output_label = str(output.resolve().relative_to(REPO_ROOT))
    except ValueError:
        output_label = str(output)
    merged["collection_run"]["output"] = output_label.replace("\\", "/")
    if not dry_run:
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=output.parent, delete=False, suffix=".tmp") as handle:
                json.dump(merged, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                temporary = Path(handle.name)
            os.replace(temporary, output)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return {
        "output": str(output),
        "dry_run": dry_run,
        **summary,
        "source_platforms_added": merged["collection_run"]["source_platforms_added"],
        "collection_run": merged["collection_run"],
        "platform_access_observations": observations,
        "observations": observations,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect public RSS/HTML metadata for the overseas opinion demo")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--timeout", type=int, default=18)
    parser.add_argument("--limit", "--max-items-per-channel", dest="limit", type=int, default=24)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.timeout < 3 or args.limit < 1:
        parser.error("--timeout 至少为 3，--limit 至少为 1")
    try:
        print(json.dumps(run(args.output, args.timeout, args.limit, args.dry_run), ensure_ascii=False, indent=2))
    except (OSError, ValueError, ElementTree.ParseError) as exc:
        print(f"采集器失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

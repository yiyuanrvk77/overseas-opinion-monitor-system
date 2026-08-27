from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from scripts.collect_public_web import CHANNELS, classify, merge_payload, parse_feed
from backend.vendor import fetch_and_store, list_vendors


class PublicCollectorTests(unittest.TestCase):
    def test_channel_registry_and_topic_rules(self):
        self.assertEqual(len(CHANNELS), 12)
        self.assertEqual(len({item["platform"] for item in CHANNELS}), 12)
        self.assertEqual(classify("Xiong'an New Area opens innovation center", ""), "雄安新区")
        self.assertEqual(classify("APEC 2026 agenda", "Asia-Pacific Economic Cooperation"), "APEC 2026")
        self.assertIsNone(classify("APEC 2025 agenda", "Asia-Pacific Economic Cooperation"))
        self.assertEqual(classify("President Xi arrives in Pyongyang", "State visit to DPRK"), "习近平海外活动")
        self.assertIsNone(classify("North Korea", "Latest news"))

    def test_feed_parser_keeps_only_topic_matches(self):
        feed = """<?xml version="1.0"?><rss><channel>
          <item><title>APEC 2026 agenda</title><description>Asia-Pacific cooperation</description>
          <link>https://example.test/apec</link><pubDate>Thu, 27 Aug 2026 00:00:00 GMT</pubDate></item>
          <item><title>Unrelated item</title><description>Nothing in scope</description>
          <link>https://example.test/other</link></item>
        </channel></rss>"""
        items = parse_feed(feed, "Test", "https://example.test/feed", 10)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["topic"], "APEC 2026")
        self.assertEqual(items[0]["original_url"], "https://example.test/apec")

    def test_merge_is_idempotent_and_preserves_existing_snapshot(self):
        existing = {
            "records": [
                {
                    "id": "existing",
                    "topic": "雄安新区",
                    "platform": "Existing",
                    "title": "Existing record",
                    "summary": "Existing summary",
                    "original_url": "https://example.test/existing",
                }
            ]
        }
        new_item = {
            "topic": "雄安新区",
            "platform": "Test",
            "title": "New record",
            "summary": "New summary",
            "original_url": "https://example.test/new",
        }
        observations = [{"platform": f"P{i}", "status": "PUBLIC_RSS"} for i in range(12)]
        merged, summary = merge_payload(existing, [new_item, new_item], observations, "2026-08-28T00:00:00+08:00", "test")
        self.assertEqual(len(merged["records"]), 2)
        self.assertEqual(summary["records_added"], 1)
        self.assertEqual(summary["channels_checked"], 12)
        self.assertIn("collection_run", merged)

    def test_mock_vendor_is_disabled_by_default(self):
        with patch.dict(os.environ, {"OPINION_MONITOR_ENABLE_MOCK_VENDOR": ""}, clear=False):
            mock = next(item for item in list_vendors() if item["id"] == "mock")
            self.assertEqual(mock["status"], "DISABLED")
            with self.assertRaisesRegex(ValueError, "模拟数据商默认关闭"):
                fetch_and_store({"target": "demo"}, "mock")


if __name__ == "__main__":
    unittest.main()

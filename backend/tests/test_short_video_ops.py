from __future__ import annotations

import json
import unittest
from pathlib import Path

from app.querying.duckdb_engine import DuckDbEngine
from app.security import AccessController


DATABASE = "short_video_ops"
DATABASE_DIR = Path(__file__).resolve().parents[1] / "data" / "databases" / DATABASE


class ShortVideoOpsDataTest(unittest.TestCase):
    def test_low_cardinality_schema_keeps_filter_values(self) -> None:
        schema = json.loads((DATABASE_DIR / "_schema.json").read_text(encoding="utf-8"))
        category = next(
            field
            for table in schema["tables"]
            if table["name"] == "content_categories"
            for field in table["fields"]
            if field["name"] == "category_name"
        )

        self.assertIn("搞笑", category["data_profile"]["sample_values"])
        self.assertIn("搞笑", category["index_content"]["keyword_text"])

    def test_manifest_and_role_scopes(self) -> None:
        manifest = json.loads((DATABASE_DIR / "_database_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["table_count"], 41)
        self.assertGreater(manifest["total_rows"], 500_000)
        self.assertEqual(len(manifest["roles"]["growth_ops"]), 18)
        self.assertEqual(len(manifest["roles"]["channel_ops"]), 16)
        self.assertEqual(len(manifest["roles"]["content_ops"]), 19)

    def test_growth_scope_can_query_growth_but_not_content(self) -> None:
        scope = AccessController().resolve("demo_growth_ops")
        engine = DuckDbEngine()
        allowed = engine.execute(
            DATABASE,
            "SELECT channel_id, COUNT(user_id) AS user_count FROM user_registrations GROUP BY channel_id ORDER BY user_count DESC LIMIT 5",
            scope,
        )
        denied = engine.execute(
            DATABASE,
            "SELECT category_id, COUNT(content_id) AS content_count FROM contents GROUP BY category_id",
            scope,
        )
        self.assertTrue(allowed.success, allowed.error)
        self.assertFalse(denied.success)
        self.assertIn("无权访问", denied.error or "")

    def test_channel_and_content_scopes_are_isolated(self) -> None:
        controller = AccessController()
        channel_scope = controller.resolve("demo_channel_ops")
        content_scope = controller.resolve("demo_content_ops")
        self.assertIn("short_video_ops.ad_daily_stats", channel_scope.allowed_tables)
        self.assertNotIn("short_video_ops.contents", channel_scope.allowed_tables)
        self.assertIn("short_video_ops.contents", content_scope.allowed_tables)
        self.assertNotIn("short_video_ops.ad_daily_stats", content_scope.allowed_tables)

if __name__ == "__main__":
    unittest.main()

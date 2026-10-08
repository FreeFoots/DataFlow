from __future__ import annotations

import json
import sqlite3
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import duckdb

from app.config import settings
from app.models import QueryResult
from app.mcp_runtime.tools.database_tools import build_database_query_tool
from app.querying.duckdb_engine import DuckDbEngine
from app.querying.models import SqlExecution
from app.security import AccessController
from app.services.dataflow_service import DataFlowService
from app.services.session_archive import SessionArchive
from app.services.session_context import SessionContext
from app.workflows.query_graph import QueryWorkflow
from app.workflows.result_builder import ResultBuilder
from tests.artifact_directory import artifact_directory


DATABASE = "short_video_ops"


class SqlBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.engine = DuckDbEngine()
        self.scope = AccessController().resolve("demo_growth_ops")

    def test_cte_shadow_cannot_hide_qualified_forbidden_table(self):
        result = self.engine.execute(DATABASE,
            "WITH contents AS (SELECT 1 AS x) SELECT COUNT(content_id) AS n FROM main.contents", self.scope)
        self.assertFalse(result.success)
        self.assertIn("无权访问", result.error)

    def test_nested_union_and_unused_cte_sources_are_checked(self):
        queries = [
            "SELECT user_id FROM user_registrations UNION ALL SELECT content_id FROM contents",
            "SELECT user_id FROM user_registrations WHERE EXISTS (SELECT content_id FROM main.contents)",
            "WITH hidden AS (SELECT content_id FROM contents) SELECT COUNT(user_id) AS n FROM user_registrations",
            "SELECT g.user_id FROM user_registrations g SEMI JOIN contents c ON g.user_id = c.creator_id",
        ]
        for sql in queries:
            with self.subTest(sql=sql):
                with self.assertRaisesRegex(ValueError, "无权访问"):
                    self.engine._validate_sql(DATABASE, sql, self.scope)

    def test_cte_from_other_scope_does_not_hide_real_table(self):
        sql = "SELECT COUNT(content_id) AS n FROM contents WHERE EXISTS (WITH contents AS (SELECT 1 AS x) SELECT x FROM contents)"
        with self.assertRaisesRegex(ValueError, "无权访问"):
            self.engine._validate_sql(DATABASE, sql, self.scope)

    def test_allowed_cte_and_date_filter_execute(self):
        sql = "WITH monthly AS (SELECT SUM(new_users) AS n FROM growth_daily_metrics WHERE metric_date >= DATE '2026-04-01' AND metric_date < DATE '2026-05-01') SELECT n FROM monthly"
        result = self.engine.execute(DATABASE, sql, self.scope)
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.rows, [{"n": 1525}])

    def test_external_functions_literals_and_catalogs_are_blocked(self):
        for sql in [
            "SELECT content FROM read_text('/etc/hosts')",
            "SELECT a FROM read_csv_auto('/tmp/example.csv')",
            "SELECT a FROM '/tmp/example.csv'",
            "SELECT getenv('HOME') AS value",
            "SELECT name FROM sqlite_master",
            "SELECT COUNT(user_id) AS n FROM other.main.user_registrations",
            "SELECT COUNT(user_id) AS n FROM information_schema.user_registrations",
        ]:
            with self.subTest(sql=sql):
                with self.assertRaises(ValueError):
                    self.engine._validate_sql(DATABASE, sql, self.scope)

    def test_connection_does_not_register_denied_tables(self):
        with self.engine.connect(DATABASE, self.scope) as connection:
            names = {row[0] for row in connection.execute("SHOW TABLES").fetchall()}
            self.assertIn("growth_daily_metrics", names)
            self.assertNotIn("contents", names)
            with self.assertRaises(duckdb.CatalogException):
                connection.execute("SELECT COUNT(content_id) FROM main.contents")

    def test_result_cap_boundary_is_explicit(self):
        for limit, expected in [(199, False), (200, False), (201, True)]:
            with self.subTest(limit=limit):
                result = self.engine.execute(DATABASE,
                    f"SELECT user_id FROM user_registrations ORDER BY user_id LIMIT {limit}", self.scope)
                self.assertTrue(result.success, result.error)
                self.assertEqual(result.truncated, expected)
                self.assertEqual(len(result.rows), min(limit, 200))

    def test_mcp_result_carries_truncation(self):
        result = build_database_query_tool(DATABASE, self.engine, self.scope)(
            "SELECT user_id FROM user_registrations ORDER BY user_id LIMIT 201")
        self.assertTrue(result.truncated)
        self.assertEqual(result.row_count, 200)


class TaskOwnershipTest(unittest.TestCase):
    def service_for(self, context):
        service = DataFlowService.__new__(DataFlowService)
        service.tasks = context.tasks
        service.context = context
        service.access_controller = AccessController()
        service.memories = Mock()
        service.workflow = Mock()
        return service

    def test_archive_roundtrip_preserves_owner_and_access(self):
        with artifact_directory() as directory:
            config = replace(settings, session_archive_enabled=True,
                session_archive_path=str(Path(directory) / "archive.db"), short_term_summary_enabled=False)
            context = SessionContext(Mock(), config)
            result = QueryResult(task_id="owned", status="completed", route="database_query", message="ok")
            context.remember("demo_channel_ops:session", result, "q", {}, "demo_channel_ops")
            restored = SessionContext(Mock(), config)
            service = self.service_for(restored)
            self.assertEqual(service.get_task("owned", "demo_channel_ops").task_id, "owned")
            with self.assertRaises(PermissionError):
                service.get_task("owned", "demo_growth_ops")
            with self.assertRaises(PermissionError):
                service.save_memory("owned", "demo_growth_ops")

    def test_legacy_archive_is_migrated_without_guessing_owner(self):
        with artifact_directory() as directory:
            path = Path(directory) / "legacy.db"
            result = QueryResult(task_id="legacy", status="completed", route="database_query", message="ok")
            with sqlite3.connect(path) as connection:
                connection.execute("CREATE TABLE session_turns (task_id TEXT PRIMARY KEY, session_id TEXT, query TEXT, workspace_json TEXT, result_json TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)")
                connection.execute("INSERT INTO session_turns (task_id, session_id, query, workspace_json, result_json) VALUES (?, ?, ?, ?, ?)",
                    ("legacy", "demo_growth_ops:old", "q", "{}", result.model_dump_json()))
            config = replace(settings, session_archive_enabled=True, session_archive_path=str(path), short_term_summary_enabled=False)
            context = SessionContext(Mock(), config)
            self.assertIsNone(context.tasks["legacy"]["user_id"])
            self.assertFalse(context.session_tasks.get("demo_growth_ops:old"))
            self.assertEqual(context.route_context("demo_growth_ops:old"), "")
            self.assertEqual(context.recent_result_context("demo_growth_ops:old"), "")
            service = self.service_for(context)
            for user in ["demo_growth_ops", "demo_admin"]:
                with self.subTest(user=user), self.assertRaises(PermissionError):
                    service.get_task("legacy", user)
            with self.assertRaises(PermissionError):
                service.save_memory("legacy", "demo_growth_ops")

    def test_missing_owner_cannot_access_or_resume(self):
        context = Mock()
        context.tasks = {"missing": {"result": QueryResult(task_id="missing", status="completed", route="database_query", message="ok")}}
        service = self.service_for(context)
        for method, args in [(service.get_task, ("missing",)), (service.save_memory, ("missing",)), (service.clarify, ("missing", "option"))]:
            with self.subTest(method=method.__name__), self.assertRaises(PermissionError):
                method(*args, user_id="demo_growth_ops")

    def test_archive_task_owner_cannot_be_reassigned(self):
        with artifact_directory() as directory:
            archive = SessionArchive(Path(directory)/"archive.db")
            archive.save_turn("t", "s", "q", {}, {}, user_id="demo_growth_ops")
            with self.assertRaises(PermissionError):
                archive.save_turn("t", "s", "q", {}, {}, user_id="demo_channel_ops")


class ResultStatusTest(unittest.TestCase):
    def test_model_unavailable_is_failed_instead_of_completed(self):
        result = ResultBuilder.direct_response("t", "稍后重试", {"source": "model_unavailable_fallback"}, [])
        self.assertEqual(result.status, "failed")

    def test_normal_direct_answer_is_completed(self):
        result = ResultBuilder.direct_response("t", "你好", {"source": "model"}, [])
        self.assertEqual(result.status, "completed")

    def test_truncation_reaches_workflow_and_api(self):
        workflow = QueryWorkflow.__new__(QueryWorkflow)
        workflow.response_generator = Mock()
        workflow.response_generator.finalize.return_value = {"valid": True, "analysis": "预览"}
        state = {"task_id": "t", "standalone_query": "q", "schema_context": "", "mcp_execution": {
            "sql": "SELECT user_id FROM user_registrations", "success": True,
            "columns": ["user_id"], "rows": [{"user_id": 1}], "truncated": True}}
        result = workflow._execute_single_database(state)["result"]
        self.assertTrue(result["truncated"])
        self.assertTrue(result["warnings"])
        self.assertTrue(workflow.response_generator.finalize.call_args.args[1].truncated)


if __name__ == "__main__":
    unittest.main()

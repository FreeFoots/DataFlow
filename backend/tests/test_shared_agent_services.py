import unittest
from dataclasses import replace
from unittest.mock import Mock

from app.config import Settings
from app.errors import PipelineStageError
from app.mcp_runtime import LocalMcpClient, create_local_mcp_server
from app.models import QueryResult
from app.preprocessing import RequestPreprocessor
from app.querying.request_understanding_agent import RequestUnderstandingAgent
from app.querying.analysis_metrics import QueryMetric
from app.querying.analysis_tools import AnalysisTools
from app.querying.data_qa_agent import DataQaAgent
from app.querying.duckdb_engine import DuckDbEngine
from app.querying.metric_query_service import execute_metric
from app.querying.result_assets import checked_claims
from app.security import AccessController
from app.services.session_context import SessionContext
from app.skills import SkillRegistry
from app.workflows.query_graph import QueryWorkflow
from tests.test_analysis_harness import OfflineIndex, metric


class SharedAgentServicesTest(unittest.TestCase):
    def setUp(self):
        self.engine = DuckDbEngine()
        self.scope = AccessController().resolve("demo_growth_ops")
        self.config = replace(Settings(), short_term_summary_enabled=False, session_archive_enabled=False)
        self.client_factory = lambda access: LocalMcpClient(create_local_mcp_server(self.engine, AccessController().resolve("demo_growth_ops")))

    def asset(self, activation=False):
        query = metric(4, "activation_cohort" if activation else "new_users")
        result = execute_metric(QueryMetric.model_validate(query), self.engine, self.scope)
        return {**result["artifact"], "result_id": "source:r1"}

    def claim(self, source="source:r1", field="new_users"):
        return {"text": "整体情况", "evidence_ids": [source],
                "facts": [{"source_id": source, "section": "summary", "field": field}]}

    def qa(self, payload, artifacts=None):
        model = Mock()
        model.chat_json.return_value = payload
        agent = DataQaAgent(model, self.client_factory, SkillRegistry().get("data_qa"))
        return agent.run("解释已有结果", {}, self.scope.public(), artifacts=artifacts or [self.asset()])

    def test_direct_and_analysis_share_metric_rows_contract_and_weighted_ratio(self):
        query = metric(4, "activation_cohort")
        direct = self.client_factory({}).call_tool("query_metric", {"query": query})
        analysis = AnalysisTools(None, self.engine, self.client_factory).execute("query_metric", query, {}, self.scope.public())
        for key in ("sql", "columns", "rows", "contract", "metric_version", "data_version", "summary"):
            self.assertEqual(direct["artifact"][key], analysis.artifact[key])
        self.assertEqual(direct["artifact"]["summary"]["new_users"], 1525)
        self.assertEqual(direct["artifact"]["summary"]["activated_users"], 1127)
        self.assertAlmostEqual(direct["artifact"]["summary"]["activation_rate"], 1127 / 1525)

    def test_direct_metric_workflow_renders_checked_facts_without_an_extra_model_call(self):
        class Model:
            calls = 0
            def chat_json(self, system, user):
                self.calls += 1
                if "请求预处理器" in system:
                    return {"action": "database_query", "mode": "query", "standalone_query": "查询2026年4月新增注册用户", "retrieval": {}}
                if "单数据库问数" in system:
                    return {"action": "call_tool", "tool_name": "query_metric", "arguments": {"query": metric(4)}}
                raise AssertionError("metric description must use stored facts")
        model = Model()
        flow = QueryWorkflow(model, OfflineIndex(), self.config)
        result = flow.invoke({"task_id": "direct", "query": "查询2026年4月新增注册用户", "access_scope": self.scope.public()}, "direct")
        self.assertEqual(result.status, "completed")
        self.assertEqual(model.calls, 2)
        self.assertIsInstance(flow.request_understanding_agent, RequestUnderstandingAgent)
        self.assertIs(RequestPreprocessor, RequestUnderstandingAgent)
        self.assertEqual(result.result_artifacts[0]["result_id"], "direct:r1")
        self.assertEqual(result.tool_calls[0]["arguments"]["tool_name"], "query_metric")
        self.assertIn("1,525", result.analysis)
        self.assertNotIn("尚未通过业务指标编译器", result.interpretation.assumptions)

    def test_metric_ranking_is_sorted_and_top_n_cannot_supply_whole_result_facts(self):
        result = execute_metric(QueryMetric.model_validate(metric(4, order_by="new_users", top_n=3)), self.engine, self.scope)
        values = [row["new_users"] for row in result["rows"]]
        self.assertEqual(values, sorted(values, reverse=True))
        self.assertEqual(len(values), 3)
        self.assertTrue(result["artifact"]["limited"])
        self.assertNotIn("summary", result["artifact"])
        with self.assertRaises(ValueError):
            checked_claims([self.claim(field="new_users")], [{**result["artifact"], "result_id": "source:r1"}])

    def test_metric_rejects_missing_window_unsupported_filter_and_sort(self):
        for query in (metric(4, "activation_cohort", observation_days=None), metric(4, region="上海"),
                      metric(4, order_by="activation_rate"), metric(4, top_n=3)):
            with self.subTest(query=query), self.assertRaises(ValueError):
                QueryMetric.model_validate(query)

    def test_metric_out_of_coverage_is_an_observation_and_permissions_limit_discovery(self):
        result = self.client_factory({}).call_tool("query_metric", {"query": metric(9)})
        self.assertFalse(result["success"])
        self.assertIn("覆盖范围", result["error"])
        content_scope = replace(self.scope, allowed_tables=frozenset())
        client = LocalMcpClient(create_local_mcp_server(self.engine, content_scope))
        self.assertNotIn("query_metric", [tool["name"] for tool in client.list_tools()])

    def test_empty_grouped_metric_preserves_zero_summary(self):
        result = execute_metric(QueryMetric.model_validate(metric(4, channel_ids=["missing"])), self.engine, self.scope)
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["artifact"]["summary"], {"new_users": 0})

    def test_qa_answer_uses_same_facts_and_ignores_untrusted_prose(self):
        result = self.qa({"action": "answer", "answer": "共有99999人", "claims": [self.claim()]})
        self.assertIn("1,525", result.answer)
        self.assertNotIn("99999", result.answer)
        self.assertEqual(result.claims[0]["facts"][0]["value"], 1525)

    def test_qa_report_and_chart_keep_asset_reference_and_ignore_model_numbers(self):
        result = self.qa({"action": "report", "title": "渠道报告", "markdown": "99999人", "claims": [self.claim()],
                          "tool_calls": [{"name": "build_bar_chart", "arguments": {
                              "title": "新增", "source_task_id": "source:r1", "category_field": "channel_id", "value_field": "new_users"}}]})
        self.assertIn("1,525", result.report.markdown)
        self.assertNotIn("99999", result.report.markdown)
        self.assertEqual(result.report.visualizations[0].source_task_id, result.artifacts[0]["result_id"])
        self.assertTrue(result.report.visualizations[0].category_labels)

    def test_qa_rejects_unknown_ambiguous_preview_and_fabricated_facts(self):
        duplicate = self.asset()
        duplicate["rows"] *= 2
        row_claim = self.claim()
        row_claim["facts"] = [{"source_id": "source:r1", "field": "new_users", "where": {"channel_id": "CH01"}}]
        for claims, assets in (([self.claim("unknown")], [self.asset()]), ([row_claim], [duplicate]),
                              ([self.claim()], [{**self.asset(), "complete": False}]),
                              ([{**self.claim(), "text": "总计99999人"}], [self.asset()])):
            with self.subTest(claims=claims), self.assertRaises(PipelineStageError):
                self.qa({"action": "answer", "claims": claims}, assets)

    def test_qa_rejects_mixed_data_versions(self):
        second = {**self.asset(), "result_id": "source:r2", "data_version": "another"}
        with self.assertRaises(PipelineStageError):
            self.qa({"action": "answer", "claims": [self.claim(), self.claim("source:r2")]}, [self.asset(), second])

    def test_qa_scope_notes_do_not_mark_a_finished_result_partial(self):
        result = self.qa({"action": "answer", "claims": [self.claim()], "gaps": [],
                          "notes": ["演示口径，正式业务定义需确认。"]})
        self.assertEqual(result.limitations, [])
        self.assertNotIn("尚未完成", result.answer)

    def test_qa_gaps_must_reference_the_confirmed_request(self):
        with self.assertRaises(PipelineStageError):
            self.qa({"action": "answer", "claims": [self.claim()], "gaps": [
                {"requirement": "计算集中度", "reason": "没有计算证据"}]})

    def test_qa_repairs_scope_classification_before_executing_chart_tools(self):
        model = Mock()
        model.chat_json.side_effect = [
            {"action": "answer", "claims": [self.claim()], "limitations": ["演示口径"]},
            {"action": "answer", "claims": [self.claim()], "gaps": [], "notes": ["演示口径"]},
        ]
        agent = DataQaAgent(model, self.client_factory, SkillRegistry().get("data_qa"))
        result = agent.run("解释已有结果", {}, self.scope.public(), artifacts=[self.asset()])
        self.assertEqual(result.limitations, [])
        self.assertEqual(model.chat_json.call_count, 2)

    def test_qa_repairs_text_dimension_reference_without_weakening_fact_checks(self):
        row = self.asset()["rows"][0]
        valid = {"text": "渠道情况", "evidence_ids": ["source:r1"], "facts": [
            {"source_id": "source:r1", "field": "new_users", "where": {"channel_name": row["channel_name"]}}]}
        invalid = {**valid, "facts": [{**valid["facts"][0], "field": "channel_name"}]}
        model = Mock()
        model.chat_json.side_effect = [{"action": "answer", "claims": [invalid]},
                                       {"action": "answer", "claims": [valid]}]
        agent = DataQaAgent(model, self.client_factory, SkillRegistry().get("data_qa"))
        result = agent.run("解释已有结果", {}, self.scope.public(), artifacts=[self.asset()])
        self.assertIn(row["channel_name"], result.answer)
        self.assertEqual(result.claims[0]["facts"][0]["value"], row["new_users"])
        self.assertIn("channel_name", model.chat_json.call_args.args[1])
        self.assertIn("请放where", model.chat_json.call_args.args[1])

    def test_server_asset_context_excludes_other_sessions_and_client_supplied_assets(self):
        context = SessionContext(Mock(), self.config)
        asset = self.asset()
        result = QueryResult(task_id="source", status="completed", route="database_query", message="完成",
                             columns=asset["columns"], rows=asset["rows"], result_artifacts=[asset])
        context.remember("growth:s", result, "query", {}, "demo_growth_ops")
        workspace = {"analysis_table_ids": ["source"], "analysis_tables": [{"task_id": "source", "rows": [{"new_users": 99999}]}]}
        self.assertEqual(context.result_artifacts("growth:s", workspace), [asset])
        self.assertEqual(context.result_artifacts("another:s", workspace), [])
        self.assertEqual(context.result_artifacts("growth:s", {"analysis_table_ids": ["forged"]}), [])

    def test_legacy_qa_without_assets_keeps_existing_response(self):
        model = Mock()
        model.chat_json.return_value = {"action": "answer", "answer": "已有结果解读", "tool_calls": []}
        agent = DataQaAgent(model, self.client_factory, SkillRegistry().get("data_qa"))
        result = agent.run("解释", {}, self.scope.public())
        self.assertEqual(result.answer, "已有结果解读")


if __name__ == "__main__":
    unittest.main()

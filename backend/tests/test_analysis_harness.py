import copy
import json
import sqlite3
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from langgraph.types import Command

from app.config import Settings
from app.database import SCHEMA
from app.querying.analysis_agent import AnalysisAgent, AnalysisDecision, FactReference
from app.querying.analysis_metrics import QueryMetric, compile_metric
from app.querying.analysis_tools import AnalysisTools
from app.querying.duckdb_engine import DuckDbEngine
from app.runtime.execution import CURRENT, Execution, LeasedSqliteSaver, durable_call
from app.runtime.store import TaskStore, LeaseLost
from app.security import AccessController
from app.workflows.query_graph import QueryWorkflow
from tests.artifact_directory import artifact_directory


def plan(*ids):
    return {"action": "plan", "plan": [{"id": key, "title": key, "depends_on": [ids[i-1]] if i else []} for i, key in enumerate(ids)]}


def call(step, tool, arguments, complete=True):
    return {"action": "call_tool", "step_id": step, "tool_name": tool, "arguments": arguments, "complete_step": complete}


def metric(month, metric_id="new_users", **changes):
    return {"metric_id": metric_id, "start_date": f"2026-{month:02d}-01", "end_date": f"2026-{month+1:02d}-01",
            "dimensions": ["channel"], **({"observation_days": 7} if metric_id == "activation_cohort" else {}), **changes}


def finish(payload):
    artifact = payload["result_catalog"][-1]
    source = artifact["result_id"]
    section = "summary" if artifact.get("summary") else "rows"
    field = "delta" if "delta" in artifact.get("summary", {}) else "new_users"
    where = {} if section == "summary" else {k: artifact["sample_rows"][0][k] for k in artifact["grain"]}
    return {"action": "finish", "claims": [{"text": "查询事实", "evidence_ids": [source],
             "facts": [{"source_id": source, "field": field, "section": section, "where": where}]}], "primary_result_id": source}


class ScriptedModel:
    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.inputs = []

    def chat_json(self, system, user):
        def operation():
            execution = CURRENT.get()
            if execution:
                execution.attempt()
            if "请求预处理器" in system:
                return {"action": "database_query", "mode": "analysis", "standalone_query": "比较2026年3月和4月新增用户，分析渠道变化", "retrieval": {}}
            payload = json.loads(user)
            self.inputs.append(payload)
            if not self.decisions:
                raise AssertionError("Unexpected decision")
            item = self.decisions.pop(0)
            return item(payload) if callable(item) else copy.deepcopy(item)
        return durable_call("offline_model", {"system": system, "user": user}, operation)


class OfflineIndex:
    def retrieve(self, query, **kwargs):
        table = next(t for t in SCHEMA if t["name"] == "growth_daily_metrics")
        return {"hits": [{"doc_id": f"{table['id']}.{f['name']}", "table_id": table["id"], "database_id": "short_video_ops", "field_name": f["name"],
                         "field_label": f["label"], "field_type": f["type"]} for f in table["fields"]], "threshold": 0.55}

    def include_workspace(self, retrieval, workspace, scope):
        return retrieval


class AnalysisFixtures:
    def config(self, **changes):
        return replace(Settings(), short_term_summary_enabled=False, session_archive_enabled=False, **changes)

    def workflow(self, decisions, **changes):
        model = ScriptedModel(decisions)
        return QueryWorkflow(model, OfflineIndex(), self.config(**changes)), model

    def payload(self, task="analysis-test", mode="auto", user="demo_growth_ops"):
        return {"task_id": task, "query": "比较2026年3月和4月新增用户，分析渠道变化", "workspace": {},
                "access_scope": AccessController().resolve(user).public(), "request_mode": mode}

    def decisions(self, task="analysis-test"):
        return [plan("metrics", "march", "april", "compare"), call("metrics", "list_metrics", {}),
                call("march", "query_metric", metric(3)), call("april", "query_metric", metric(4)),
                call("compare", "compare_results", {"baseline_id": f"{task}:r1", "current_id": f"{task}:r2", "key_fields": ["channel_id"], "value_field": "new_users"}), finish]


class AnalysisHarnessTest(AnalysisFixtures, unittest.TestCase):

    def test_plan_repair_has_explicit_success_feedback_and_ready_steps(self):
        invalid = {**plan("metrics"), "plan_version": 1}
        def after_plan(payload):
            feedback = payload["observations"][-1]
            self.assertEqual(feedback["category"], "plan_saved")
            self.assertTrue(feedback["success"])
            self.assertEqual(feedback["ready_step_ids"], ["metrics"])
            self.assertEqual(payload["budget_used"]["no_progress"], 0)
            return call("metrics", "list_metrics", {})
        workflow, _ = self.workflow([invalid, plan("metrics"), after_plan,
                                   {"action": "finish", "limitations": ["仅验证计划反馈"]}])
        result = workflow.invoke(self.payload(), "analysis-test")
        self.assertEqual(result.analysis_budget["tool_calls"], 1)
        self.assertEqual(result.stop_reason, "insufficient_evidence")

    def test_successful_query_returns_to_planner_and_computes_real_difference(self):
        workflow, model = self.workflow(self.decisions())
        result = workflow.invoke(self.payload(), "analysis-test")
        self.assertEqual(result.status, "completed")
        self.assertEqual([sum(row["new_users"] for row in a["rows"]) for a in result.result_artifacts[:2]], [1538, 1525])
        self.assertEqual(result.result_artifacts[-1]["summary"]["delta"], -13)
        self.assertEqual(result.analysis_claims[0]["facts"][0]["value"], -13)
        self.assertEqual(result.analysis_budget["tool_calls"], 4)
        self.assertEqual(model.inputs[-1]["result_catalog"][-1]["derived_from"], ["analysis-test:r1", "analysis-test:r2"])

    def test_scope_notes_do_not_mark_finished_analysis_as_partial(self):
        def with_notes(payload):
            return {**finish(payload), "notes": ["演示定义；相关性不代表因果"]}
        workflow, _ = self.workflow([*self.decisions()[:-1], with_notes])
        result = workflow.invoke(self.payload(), "analysis-test")
        self.assertEqual(result.status, "completed")
        self.assertIn("演示定义；相关性不代表因果", result.warnings)
        self.assertEqual(result.analysis_limitations, [])

    def test_self_declared_unrequested_limitations_require_scope_note_repair(self):
        def misplaced(payload):
            return {**finish(payload), "limitations": ["未做因果推断（用户未要求）"]}
        def repaired(payload):
            self.assertIn("移入notes", payload["observations"][-1]["error"])
            return {**finish(payload), "notes": ["未做因果推断（用户未要求）"]}
        workflow, _ = self.workflow([*self.decisions()[:-1], misplaced, repaired])
        result = workflow.invoke(self.payload(), "analysis-test")
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.analysis_budget["tool_calls"], 4)

    def test_plan_changes_after_observing_actual_data(self):
        decisions = self.decisions()
        first_plan = plan("metrics", "march")
        def extend(payload):
            self.assertEqual(sum(row["new_users"] for row in payload["result_catalog"][0]["sample_rows"]), 1538)
            return {**plan("metrics", "march", "april", "compare"), "reason": "已取得基期分组，追加对比"}
        workflow, _ = self.workflow([first_plan, *decisions[1:3], extend, *decisions[3:]])
        result = workflow.invoke(self.payload(), "analysis-test")
        self.assertEqual(result.status, "completed")
        self.assertEqual(sum(item.get("stage") == "analysis_plan" for item in result.execution_log), 2)

    def test_budget_stops_with_partial_evidence(self):
        workflow, _ = self.workflow(self.decisions(), analysis_max_tool_calls=2)
        result = workflow.invoke(self.payload(), "analysis-test")
        self.assertEqual((result.status, result.stop_reason), ("partial", "budget_exhausted"))
        self.assertEqual(len(result.result_artifacts), 1)

    def test_repeated_success_is_not_executed_again(self):
        repeated = call("metrics", "list_metrics", {}, complete=False)
        workflow, _ = self.workflow([plan("metrics"), repeated, repeated, repeated, repeated])
        result = workflow.invoke(self.payload(), "analysis-test")
        self.assertEqual(result.stop_reason, "no_progress")
        self.assertEqual(result.analysis_budget["tool_calls"], 1)

    def test_cached_success_can_close_a_multi_call_step_without_requery(self):
        decisions = [plan("metrics", "march", "april", "compare"), call("metrics", "list_metrics", {}),
                     call("march", "query_metric", metric(3), complete=False),
                     call("march", "query_metric", metric(3), complete=True), *self.decisions()[3:]]
        workflow, _ = self.workflow(decisions)
        result = workflow.invoke(self.payload(), "analysis-test")
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(result.result_artifacts), 3)
        self.assertEqual(result.analysis_budget["tool_calls"], 4)
        self.assertEqual(result.analysis_plan[1]["evidence_ids"], ["analysis-test:r1"])
        self.assertTrue(any(item.get("stage") == "analysis_cache_reused" for item in result.execution_log))

    def test_blocked_dependency_feedback_names_the_unfinished_step_and_cache_repair(self):
        def close_pending(payload):
            self.assertIn("march", payload["observations"][-1]["error"])
            self.assertIn("complete_step=true", payload["observations"][-1]["error"])
            self.assertEqual(payload["ready_step_ids"], ["march"])
            return call("march", "query_metric", metric(3))
        decisions = [plan("metrics", "march", "april", "compare"), call("metrics", "list_metrics", {}),
                     call("march", "query_metric", metric(3), complete=False), call("april", "query_metric", metric(4)),
                     close_pending, *self.decisions()[3:]]
        workflow, _ = self.workflow(decisions)
        result = workflow.invoke(self.payload(), "analysis-test")
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.analysis_budget["tool_calls"], 4)

    def test_model_decision_cap_is_bounded(self):
        workflow, _ = self.workflow([plan("metrics")], analysis_max_decisions=1)
        self.assertEqual(workflow.invoke(self.payload(), "analysis-test").stop_reason, "budget_exhausted")

    def test_parameter_failure_is_observed_then_repaired(self):
        decisions = [plan("query"), call("query", "query_metric", metric(3, end_date="2026-12-01")), call("query", "query_metric", metric(3)), finish]
        workflow, model = self.workflow(decisions)
        result = workflow.invoke({**self.payload(), "query": "查询2026年3月新增用户"}, "analysis-test")
        self.assertEqual(result.status, "completed")
        self.assertEqual(model.inputs[-2]["observations"][-1]["category"], "tool_execution")

    def test_plan_cycles_and_unmet_dependencies_do_not_execute(self):
        cycle = {"action": "plan", "plan": [{"id": "a", "title": "a", "depends_on": ["b"]}, {"id": "b", "title": "b", "depends_on": ["a"]}]}
        for decisions in [[cycle]*3, [plan("a", "b"), *[call("b", "list_metrics", {})]*3]]:
            workflow, _ = self.workflow(decisions)
            result = workflow.invoke(self.payload(), "analysis-test")
            self.assertEqual(result.analysis_budget["tool_calls"], 0)

    def test_fabricated_result_or_ambiguous_row_cannot_finish(self):
        for invalid in [lambda payload: {**finish(payload), "claims": [{"text": "事实", "evidence_ids": ["foreign:r1"], "facts": [{"source_id": "foreign:r1", "field": "new_users"}]}]},
                        lambda payload: {**finish(payload), "claims": [{"text": "事实", "evidence_ids": ["analysis-test:r1"], "facts": [{"source_id": "analysis-test:r1", "field": "new_users"}]}]}]:
            workflow, _ = self.workflow([plan("query"), call("query", "query_metric", metric(3)), invalid, invalid, invalid])
            result = workflow.invoke(self.payload(), "analysis-test")
            self.assertEqual(result.status, "partial")
            self.assertEqual(result.analysis_claims, [])

    def test_permissions_and_tool_whitelist_remain_enforced(self):
        for tool, args in [("inspect_table", {"table": "contents"}), ("python", {"code": "print(1)"})]:
            forbidden = call("inspect", tool, args)
            workflow, model = self.workflow([plan("inspect"), forbidden, forbidden, forbidden])
            result = workflow.invoke(self.payload(), "analysis-test")
            self.assertEqual(result.status, "failed")
            self.assertFalse(any(t["name"] == "contents" for t in model.inputs[0]["available_tables"]))
        tools = AnalysisTools(None, DuckDbEngine(), lambda access: None)
        self.assertNotIn("query_data", tools.INPUTS)

    def test_missing_original_period_cannot_be_reported_completed(self):
        workflow, _ = self.workflow([plan("query"),call("query","query_metric",metric(3)),finish,finish,finish])
        result=workflow.invoke(self.payload(),"analysis-test")
        self.assertEqual(result.status,"partial")
        self.assertEqual(result.analysis_claims,[])

    def test_model_cannot_supply_unreferenced_numbers_in_factual_text(self):
        def fabricated(payload):
            value=finish(payload);value["claims"][0]["text"]="总计99999人";return value
        workflow,_=self.workflow([plan("query"),call("query","query_metric",metric(3)),fabricated,fabricated,fabricated])
        result=workflow.invoke(self.payload(),"analysis-test")
        self.assertEqual(result.analysis_claims,[])
        self.assertEqual(result.stop_reason,"no_progress")

    def test_explicit_query_mode_preserves_simple_path(self):
        class Model:
            def chat_json(self, system, user):
                if "请求预处理器" in system:
                    return {"action": "database_query", "mode": "analysis", "standalone_query": "2026年3月新增用户", "retrieval": {}}
                if "单数据库问数" in system:
                    return {"action": "call_tool", "tool_name": "query_short_video_ops", "arguments": {"sql": "SELECT SUM(new_users) AS new_users FROM growth_daily_metrics WHERE metric_date >= DATE '2026-03-01' AND metric_date < DATE '2026-04-01'"}}
                return {"valid": True, "title": "新增", "analysis": "1538人"}
        workflow = QueryWorkflow(Model(), OfflineIndex(), self.config())
        result = workflow.invoke(self.payload(mode="query"), "analysis-test")
        self.assertEqual(result.workflow_mode, "single_database_agent")
        self.assertEqual(result.rows[0]["new_users"], 1538)

    def test_explicit_analysis_confirms_unclear_purpose_before_starting_tools(self):
        class Model(ScriptedModel):
            def chat_json(self, system, user):
                if "请求预处理器" in system and not getattr(self, "asked", False):
                    self.asked = True
                    return {"action": "direct_response", "response_type": "clarification", "response": "主要看新增人数还是激活效果？"}
                return super().chat_json(system, user)
        model = Model(self.decisions())
        workflow = QueryWorkflow(model, OfflineIndex(), self.config())
        result = workflow.invoke(self.payload(mode="analysis"), "analysis-test")
        self.assertEqual(result.workflow_mode, "request_clarification")
        self.assertEqual(result.status, "waiting_clarification")
        self.assertEqual(model.inputs, [])
        result = workflow.invoke(Command(resume={"answer": "比较2026年3月和4月新增人数，分析渠道变化"}), "analysis-test")
        self.assertEqual(result.workflow_mode, "analysis_agent")
        self.assertEqual(result.status, "completed")


class AnalysisCalculationTest(unittest.TestCase):
    def source(self, source_id, rows, **changes):
        return {"result_id": source_id, "title": source_id, "columns": ["channel", "value", "denominator"], "rows": rows,
                "metric": "test", "metric_version": "v1", "contract": {}, "semantic_verified": True,
                "grain": ["channel"], "time_range": source_id, "unit": "人", "data_version": "v1", "complete": True,
                "limited": False, "additive_fields": ["value", "denominator"], "ratio_fields": [], **changes}

    def tools(self):
        return AnalysisTools(None, DuckDbEngine(), lambda access: None)

    def compare(self, a, b, **changes):
        return self.tools().execute("compare_results", {"baseline_id": "a", "current_id": "b", "key_fields": ["channel"], "value_field": "value", **changes}, {"artifacts": [a,b]}, AccessController().resolve("demo_growth_ops").public()).artifact

    def test_comparison_rejects_incompatible_results(self):
        a = self.source("a", [{"channel": "X", "value": 10}])
        for changes in [{"complete": False}, {"limited": True}, {"grain": []}, {"unit": "元"}, {"data_version": "v2"}, {"metric_version": "v2"}, {"contract": {"channel_ids": ["CH01"]}}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.compare(a, self.source("b", [{"channel": "X", "value": 20}], **changes))

    def test_comparison_rejects_self_and_overlapping_periods(self):
        a = self.source("a", [{"channel": "X", "value": 10}], start_date="2026-03-01", end_date="2026-05-01")
        b = self.source("b", [{"channel": "X", "value": 20}], start_date="2026-04-01", end_date="2026-05-01")
        with self.assertRaisesRegex(ValueError, "不重叠"):
            self.compare(a, b)
        with self.assertRaisesRegex(ValueError, "自身"):
            self.tools().execute("compare_results", {"baseline_id": "a", "current_id": "a", "key_fields": ["channel"], "value_field": "value"},
                                 {"artifacts": [a]}, AccessController().resolve("demo_growth_ops").public())

    def test_finish_requires_full_month_coverage_and_requested_contribution(self):
        state = {"original_query": "分析2026年3月新增变化贡献", "charts": [], "artifacts": [
            {"metric": "new_users", "semantic_verified": True, "start_date": "2026-03-15", "end_date": "2026-04-01"}]}
        gaps = AnalysisAgent._goal_gaps(state)
        self.assertIn("缺少3月new_users证据", gaps)
        self.assertIn("缺少可加指标的变化贡献计算", gaps)

    def test_activation_comparison_requires_computed_difference(self):
        state = {"original_query": "比较2026年3月和4月激活率", "charts": [], "artifacts": [
            {"metric": "activation_cohort", "semantic_verified": True, "start_date": "2026-03-01", "end_date": "2026-05-01"}]}
        self.assertTrue(any("差值" in gap for gap in AnalysisAgent._goal_gaps(state)))
        state["artifacts"].append({"metric": "activation_cohort:activation_rate:comparison"})
        self.assertFalse(any("差值" in gap for gap in AnalysisAgent._goal_gaps(state)))

    def test_missing_groups_duplicates_and_zero_baseline(self):
        a = self.source("a", [{"channel": "X", "value": 0}]); b = self.source("b", [{"channel": "Y", "value": 10}])
        with self.assertRaises(ValueError): self.compare(a,b)
        self.assertIsNone(self.compare(a,b,missing_policy="zero")["rows"][1]["change_rate"])
        with self.assertRaises(ValueError): self.compare(a,self.source("b",[{"channel":"X","value":1},{"channel":"X","value":2}]))

    def test_ratio_uses_summed_numerator_and_denominator(self):
        a = self.source("a", [{"channel":"X","value":1,"denominator":2},{"channel":"Y","value":9,"denominator":30}])
        output = self.tools().execute("summarize_result", {"source_id":"a","value_field":"value","denominator_field":"denominator","operation":"ratio"}, {"artifacts":[a]}, AccessController().resolve("demo_growth_ops").public())
        self.assertAlmostEqual(output.artifact["rows"][0]["value"], 10/32)
        with self.assertRaises(ValueError):
            self.tools().execute("summarize_result", {"source_id":"a","value_field":"value","operation":"mean"}, {"artifacts":[{**a,"additive_fields":[]}]}, AccessController().resolve("demo_growth_ops").public())

    def test_ratio_comparison_does_not_sum_rates_or_allocate_contribution(self):
        a=self.source("a",[{"channel":"X","value":0.2}],additive_fields=[],ratio_fields=["value"])
        b=self.source("b",[{"channel":"X","value":0.3}],additive_fields=[],ratio_fields=["value"])
        result=self.compare(a,b)
        self.assertEqual(result["summary"], {})
        self.assertIsNone(result["rows"][0]["contribution_rate"])
        result["result_id"]="c"
        fact=AnalysisAgent._resolve_fact(FactReference(source_id="c",field="delta",where={"channel":"X"}),{"c":result})
        self.assertIn("10.00个百分点",fact["display"])

    def test_metric_dates_observation_window_and_permissions_are_checked(self):
        scope=AccessController().resolve("demo_growth_ops"); engine=DuckDbEngine()
        for args in [metric(8,"activation_cohort"),metric(3,end_date="2027-01-01")]:
            with self.assertRaises(ValueError):compile_metric(QueryMetric(**args),engine,scope)
        with self.assertRaises(ValueError):QueryMetric(**metric(3,"activation_cohort",observation_days=None))
        with self.assertRaises(ValueError):compile_metric(QueryMetric(**metric(3,"activation_cohort")),engine,AccessController().resolve("demo_content_ops"))

    def test_duplicate_sql_columns_are_rejected_and_limit_is_marked(self):
        engine=DuckDbEngine();scope=AccessController().resolve("demo_growth_ops")
        self.assertFalse(engine.execute("short_video_ops","SELECT user_id, user_id FROM users",scope).success)
        self.assertTrue(engine.execute("short_video_ops","SELECT user_id FROM users LIMIT 1",scope).limited)


class DurableAnalysisTest(AnalysisFixtures, unittest.TestCase):
    def test_complete_comparison_chart_survives_live_progress_and_checkpoint(self):
        query = "比较2026年3月和4月各渠道的新增注册用户，分析变化贡献，并对比注册后7天的激活率。给出可核对的数值事实和一张变化图表，不推断因果。"
        with artifact_directory() as folder:
            store, task = self.runtime(folder)
            task_id = task["id"]
            def conclude(payload):
                return {"action": "finish", "title": "两月新增注册及激活率对比", "primary_result_id": f"{task_id}:r5",
                        "claims": [{"text": "新增变化", "evidence_ids": [f"{task_id}:r5"],
                                    "facts": [{"source_id": f"{task_id}:r5", "section": "summary", "field": key}
                                              for key in ["current", "delta", "change_rate"]]},
                                   {"text": "激活表现", "evidence_ids": [f"{task_id}:r6"],
                                    "facts": [{"source_id": f"{task_id}:r6", "field": "delta", "where": {"channel_id": "CH04"}}]}]}
            decisions = [plan("metrics", "march", "april", "act_mar", "act_apr", "compare", "compare_act", "chart"),
                         call("metrics", "list_metrics", {}), call("march", "query_metric", metric(3)),
                         call("april", "query_metric", metric(4)), call("act_mar", "query_metric", metric(3, "activation_cohort")),
                         call("act_apr", "query_metric", metric(4, "activation_cohort")),
                         call("compare", "compare_results", {"baseline_id": f"{task_id}:r1", "current_id": f"{task_id}:r2", "key_fields": ["channel_id"], "value_field": "new_users"}),
                         call("compare_act", "compare_results", {"baseline_id": f"{task_id}:r3", "current_id": f"{task_id}:r4", "key_fields": ["channel_id"], "value_field": "activation_rate"}),
                         call("chart", "build_chart", {"source_id": f"{task_id}:r5", "title": "各渠道新增变化", "category_field": "channel_id", "value_field": "delta"}), conclude]
            workflow, token, connection = self.workflow_in_runtime(store, task, ScriptedModel(decisions))
            try:
                result = workflow.invoke({**self.payload(task_id), "query": query}, task_id)
                self.assertEqual(result.status, "completed")
                self.assertEqual(len(result.result_artifacts), 6)
                self.assertEqual(result.analysis_limitations, [])
                self.assertIn("1,525人", result.report.summary)
                self.assertIn("减少 13人", result.report.summary)
                self.assertIn("百分点", result.report.markdown)
                self.assertEqual(len(result.report.visualizations), 1)
                self.assertEqual(result.report.visualizations[0].category_labels["CH01"], "自然推荐")
                self.assertEqual(store.get(task_id)["progress"]["report"], result.report.model_dump())
                recovered = workflow._state_result(workflow.graph.get_state(workflow.run_config(task_id)).values, task_id)
                self.assertEqual(recovered.report, result.report)
            finally:
                connection.close()
                CURRENT.reset(token)

    def runtime(self, folder):
        store=TaskStore(Path(folder)/"runtime.db")
        task=store.accept("demo_growth_ops",{"query":"比较新增","session_id":"session","mode":"analysis"},"submission-analysis","v1")
        return store,store.claim("worker",120)

    def workflow_in_runtime(self, store, task, model):
        execution=Execution(store,task,"worker",60,40)
        token=CURRENT.set(execution)
        connection=sqlite3.connect(store.path,check_same_thread=False);connection.row_factory=sqlite3.Row
        workflow=QueryWorkflow(model,OfflineIndex(),self.config(),checkpointer=LeasedSqliteSaver(connection,execution))
        return workflow,token,connection

    def test_clarification_survives_reconstruction_without_requery(self):
        clarification={"action":"clarify","clarification":{"parameter":"window","question":"激活观察多少天？","reason":"队列口径","options":[{"id":"seven","label":"七天","description":"七天"},{"id":"one","label":"一天","description":"一天"}]}}
        with artifact_directory() as folder:
            store,task=self.runtime(folder);task_id=task["id"]
            decisions=self.decisions(task_id);model=ScriptedModel([*decisions[:3],clarification,*decisions[3:]])
            workflow,token,connection=self.workflow_in_runtime(store,task,model)
            try:
                result=workflow.invoke(self.payload(task_id),task_id);self.assertEqual(result.status,"waiting_clarification")
                store.finish(task_id,"worker",task["generation"],result.model_dump(mode="json"));used=store.get(task_id)["model_attempts"]
            finally:connection.close();CURRENT.reset(token)
            current=store.get(task_id);store.clarify(task_id,"demo_growth_ops","seven","resume-analysis",current["version"])
            task=store.claim("worker",120);workflow,token,connection=self.workflow_in_runtime(store,task,model)
            try:
                result=workflow.invoke(Command(resume={"option_id":"seven"}),task_id)
                self.assertEqual(result.status,"completed");self.assertEqual(result.analysis_budget["tool_calls"],4)
                self.assertEqual(len(result.result_artifacts),3);self.assertGreater(store.get(task_id)["model_attempts"],used)
            finally:connection.close();CURRENT.reset(token)

    def test_crash_after_tool_commit_replays_response_before_checkpoint(self):
        with artifact_directory() as folder:
            store,task=self.runtime(folder);task_id=task["id"]
            model=ScriptedModel([plan("query"),call("query","query_metric",metric(3)),finish])
            workflow,token,connection=self.workflow_in_runtime(store,task,model)
            original=AnalysisAgent.execute
            def crash(agent,*args):
                original(agent,*args)
                raise SystemExit("simulated crash before checkpoint")
            try:
                with patch.object(AnalysisAgent,"execute",crash),self.assertRaises(SystemExit):workflow.invoke({**self.payload(task_id),"query":"查询2026年3月新增用户"},task_id)
                used=store.get(task_id)["model_attempts"]
                store.stop(task_id,"worker",task["generation"],"queued","",requeue=True)
            finally:connection.close();CURRENT.reset(token)
            task=store.claim("worker",120);workflow,token,connection=self.workflow_in_runtime(store,task,model)
            try:
                with patch.object(workflow.database_engine,"execute",side_effect=AssertionError("SQL must replay")):
                    result=workflow.invoke(None,task_id)
                self.assertEqual(result.status,"completed");self.assertEqual(result.analysis_budget["tool_calls"],1)
                self.assertEqual(store.get(task_id)["model_attempts"],used+1)
            finally:connection.close();CURRENT.reset(token)

    def test_model_attempt_limit_survives_requeue(self):
        with artifact_directory() as folder:
            store,task=self.runtime(folder);task_id=task["id"]
            store.model_attempt(task_id,"worker",task["generation"],1)
            store.stop(task_id,"worker",task["generation"],"queued","",requeue=True)
            task=store.claim("worker",120);workflow,token,connection=self.workflow_in_runtime(store,task,ScriptedModel([]))
            try:
                CURRENT.get().model_limit=1
                state=workflow.analysis_agent.decide(task_id,workflow.analysis_agent.initialize("test"),self.payload()["access_scope"],{})
                self.assertEqual(state["stop_reason"],"budget_exhausted");self.assertEqual(store.get(task_id)["model_attempts"],1)
            finally:connection.close();CURRENT.reset(token)

    def test_cancel_and_timeout_preserve_checked_evidence(self):
        for state in ["cancelled","timed_out"]:
            with artifact_directory() as folder:
                store,task=self.runtime(folder);task_id=task["id"]
                snapshot={"task_id":task_id,"status":"partial","route":"database_query","message":"pending","result_artifacts":[{"result_id":"r1"}],"workflow_mode":"analysis_agent"}
                store.progress(task_id,"worker",task["generation"],snapshot)
                if state=="cancelled":store.cancel(task_id,"demo_growth_ops")
                store.stop(task_id,"worker",task["generation"],state,"stopped")
                result=store.get(task_id,"demo_growth_ops")
                self.assertEqual(result["state"],state);self.assertEqual(result["result"]["status"],"partial")
                self.assertEqual(result["result"]["result_artifacts"],snapshot["result_artifacts"])
                with self.assertRaises(PermissionError):store.get(task_id,"demo_channel_ops")

    def test_partial_is_terminal_and_progress_cannot_use_stale_lease(self):
        with artifact_directory() as folder:
            store,task=self.runtime(folder);task_id=task["id"]
            result={"task_id":task_id,"status":"partial","route":"database_query","message":"partial"}
            store.finish(task_id,"worker",task["generation"],result)
            self.assertEqual(store.cancel(task_id,"demo_growth_ops")["state"],"partial")
            store.hide_session("demo_growth_ops","session")
            with self.assertRaises(LeaseLost):store.progress(task_id,"worker",task["generation"],result)

    def test_auto_mode_preserves_legacy_submission_idempotency(self):
        from app.runtime.store import ConflictError
        with artifact_directory() as folder:
            store=TaskStore(Path(folder)/"runtime.db")
            request={"query":"hello","session_id":"session","workspace":{}}
            first=store.accept("demo_growth_ops",request,"same-submission","v1")
            second=store.accept("demo_growth_ops",{**request,"mode":"auto"},"same-submission","v1")
            self.assertEqual(first["id"],second["id"])
            with self.assertRaises(ConflictError):store.accept("demo_growth_ops",{**request,"mode":"analysis"},"same-submission","v1")


if __name__=="__main__": unittest.main()

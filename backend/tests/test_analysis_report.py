import copy
import unittest

from app.models import AnalysisReport, QueryResult, VisualizationSpec
from app.querying.analysis_report import build_analysis_report, present_analysis_result


class AnalysisReportTest(unittest.TestCase):
    def evidence(self):
        parents = [
            {"result_id": "task:r1", "start_date": "2026-03-01", "end_date": "2026-04-01",
             "rows": [{"channel_id": "CH07", "channel_name": "好友邀请", "new_users": 172}]},
            {"result_id": "task:r2", "start_date": "2026-04-01", "end_date": "2026-05-01",
             "rows": [{"channel_id": "CH07", "channel_name": "好友邀请", "new_users": 148}]},
        ]
        comparison = {"result_id": "task:r3", "metric": "new_users:new_users:comparison", "metric_version": "demo-v1",
                      "semantic_verified": True, "complete": True, "limited": False, "unit": "人", "ratio_fields": ["change_rate", "contribution_rate"],
                      "contract": {"metric_id": "new_users"}, "derived_from": ["task:r1", "task:r2"],
                      "columns": ["channel_id", "baseline", "current", "delta", "change_rate", "contribution_rate"],
                      "summary": {"baseline": 1538, "current": 1525, "delta": -13, "change_rate": -13/1538},
                      "rows": [{"channel_id": "CH07", "baseline": 172, "current": 148, "delta": -24, "change_rate": -24/172, "contribution_rate": 24/13}]}
        claims = [{"text": "旧报告重复日期和（依据：21e66efa405b499cbbc9fde3856341be:r3）", "facts": [
            {"source_id": "task:r3", "section": "summary", "field": key, "value": 99999} for key in comparison["summary"]]},
            {"text": "错标的渠道名称", "facts": [{"source_id": "task:r3", "section": "rows", "where": {"channel_id": "CH07"}, "field": key} for key in ["delta", "change_rate", "contribution_rate"]]}]
        return claims, [*parents, comparison]

    def test_readable_overview_uses_evidence_and_avoids_numeric_inventory(self):
        claims, artifacts = self.evidence()
        report = build_analysis_report("渠道变化", claims, artifacts)
        self.assertEqual(report.summary, "4月新增注册 **1,525人**，较3月**减少 13人**（0.85%）。")
        self.assertIn("**好友邀请**：新增注册**减少 24人**", report.markdown)
        for unwanted in ["99999", "依据：", "task:r3", "21e66efa", "基期", "本期", "2026-03-01", "184.62%", "错标的渠道名称"]:
            self.assertNotIn(unwanted, report.markdown)
        self.assertIn("2026年3月、4月", report.markdown)

    def test_rate_difference_is_percentage_points_and_does_not_repeat_endpoints(self):
        claims, artifacts = self.evidence()
        rate = {**artifacts[-1], "result_id": "rate", "metric": "activation_cohort:activation_rate:comparison", "unit": "比例",
                "contract": {"metric_id": "activation_cohort", "observation_days": 7}, "ratio_fields": ["baseline", "current", "delta"],
                "rows": [{"channel_id": "CH07", "baseline": .8, "current": .75, "delta": -.05}], "summary": {}}
        facts = [{"source_id": "rate", "where": {"channel_id": "CH07"}, "field": key} for key in ["baseline", "current", "delta"]]
        report = build_analysis_report("激活", [{"facts": facts}], [*artifacts, rate])
        self.assertIn("下降 5.00 个百分点", report.summary)
        self.assertNotIn("80.00%", report.markdown)
        self.assertNotIn("75.00%", report.markdown)
        self.assertNotIn("尚未完成", report.markdown)
        self.assertIn("7天观察窗口", report.markdown)

    def test_old_saved_report_is_presented_without_mutating_raw_facts(self):
        claims, artifacts = self.evidence()
        result = QueryResult(task_id="task", status="completed", route="database_query", message="完成", workflow_mode="analysis_agent",
                             report=AnalysisReport(title="渠道变化", markdown="旧正文"), analysis_claims=claims, result_artifacts=artifacts)
        original = copy.deepcopy(result.model_dump())
        presented = present_analysis_result(result)
        self.assertEqual(result.model_dump(), original)
        self.assertNotEqual(presented.report.markdown, "旧正文")
        self.assertEqual(presented.analysis_claims, claims)
        self.assertEqual(presented.result_artifacts, artifacts)
        self.assertEqual(present_analysis_result(presented).report, presented.report)

    def test_partial_report_keeps_missing_evidence_and_cancel_notice(self):
        report = build_analysis_report("进度", [], [{"result_id": "r1"}], limitations=["尚缺激活率证据"], stop_reason="cancelled")
        self.assertIn("尚缺激活率证据", report.markdown)
        self.assertIn("任务已取消", report.markdown)

    def test_incomplete_or_ambiguous_fact_is_not_published(self):
        claims, artifacts = self.evidence()
        for changes in [{"complete": False}, {"limited": True}, {"semantic_verified": False}]:
            with self.subTest(changes=changes):
                report = build_analysis_report("检查", claims, [*artifacts[:-1], {**artifacts[-1], **changes}])
                self.assertNotIn("1,525", report.markdown)
        artifacts[-1]["rows"] *= 2
        report = build_analysis_report("检查", claims[1:], artifacts)
        self.assertNotIn("减少 24人", report.markdown)

    def test_zero_change_and_filtered_scope_remain_explicit(self):
        claims, artifacts = self.evidence()
        artifacts[-1]["summary"].update(baseline=0, current=0, delta=0, change_rate=None)
        artifacts[-1]["contract"]["channel_ids"] = ["CH07"]
        report = build_analysis_report("检查", claims, artifacts)
        self.assertIn("**0人**", report.summary)
        self.assertIn("持平", report.summary)
        self.assertNotIn("None", report.markdown)
        self.assertIn("限定渠道：好友邀请", report.markdown)

    def test_chart_labels_use_the_same_checked_source_without_changing_fields(self):
        claims, artifacts = self.evidence()
        chart = VisualizationSpec(type="bar", title="变化", source_task_id="task:r3", category_field="channel_id", value_field="delta")
        report = build_analysis_report("检查", claims, artifacts, charts=[chart])
        presented = report.visualizations[0]
        self.assertEqual(presented.category_labels, {"CH07": "好友邀请"})
        self.assertEqual(presented.value_label, "新增注册变化（人）")
        self.assertEqual(presented.category_field, "channel_id")
        self.assertEqual(presented.value_field, "delta")
        self.assertEqual(chart.category_labels, {})

    def test_live_chart_dictionary_and_saved_model_render_identically(self):
        claims, artifacts = self.evidence()
        raw = {"type": "bar", "title": "变化", "source_task_id": "task:r3",
               "category_field": "channel_id", "value_field": "delta"}
        original = copy.deepcopy(raw)
        live = build_analysis_report("检查", claims, artifacts, charts=[raw])
        saved = build_analysis_report("检查", claims, artifacts, charts=[VisualizationSpec.model_validate(raw)])
        self.assertEqual(live, saved)
        self.assertEqual(live.visualizations[0].category_labels, {"CH07": "好友邀请"})
        self.assertEqual(raw, original)

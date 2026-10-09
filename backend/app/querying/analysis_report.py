"""Reader-facing reports from checked facts; source references remain in evidence."""
from __future__ import annotations

import re
from collections import OrderedDict
from datetime import date, timedelta

from ..models import AnalysisReport, QueryResult, VisualizationSpec
from .result_assets import FactReference, resolve_fact


def _escape(value: object) -> str:
    text = re.sub(r"\b[0-9a-f]{32}(?::r\d+)?\b", "相关数据", str(value))
    return re.sub(r"([\\`*_|<>\[\]])", r"\\\1", text).replace("\n", " ")


def _number(value: float | int) -> str:
    return f"{value:,.4f}".rstrip("0").rstrip(".")


def _period(source: dict) -> str:
    if not source.get("start_date") or not source.get("end_date"):
        return ""
    start, end = date.fromisoformat(source["start_date"]), date.fromisoformat(source["end_date"])
    if start.day == end.day == 1 and (end.year * 12 + end.month) - (start.year * 12 + start.month) == 1:
        return f"{start.year}年{start.month}月"
    return f"{start:%Y年%m月%d日}至{end - timedelta(days=1):%Y年%m月%d日}"


def _period_pair(source: dict, sources: dict) -> tuple[str, str]:
    parents = source.get("derived_from", [])
    return tuple(_period(sources.get(key, {})) for key in parents[:2]) if len(parents) >= 2 else ("", "")


def _short_period(period: str, previous: str = "") -> str:
    year = re.match(r"(\d{4}年)", previous)
    return period.removeprefix(year[1]) if year else period


def _metric(source: dict) -> str:
    key = source.get("contract", {}).get("metric_id") or source.get("metric", "")
    if key.startswith("activation_cohort"):
        return "激活率" if source.get("unit") == "比例" else "注册队列"
    if key.startswith("new_users"):
        return "新增注册"
    return "查询结果"


def _category(source: dict, where: dict, sources: dict) -> str:
    channel = where.get("channel_id")
    if channel is not None:
        candidates = [source, *[sources.get(key, {}) for key in reversed(source.get("derived_from", []))]]
        for candidate in candidates:
            row = next((row for row in candidate.get("rows", []) if row.get("channel_id") == channel and row.get("channel_name")), None)
            if row:
                return str(row["channel_name"])
        return str(channel)
    return "、".join(str(value)[:60] for value in where.values()) or "整体"


def _checked_value(fact: dict, source: dict):
    """Re-read stored evidence, including historical reports, without trusting prose."""
    reference = FactReference.model_validate({key: value for key, value in fact.items()
                                             if key in FactReference.model_fields})
    return resolve_fact(reference, {reference.source_id: source})["value"]


def _sentence(source: dict, values: dict, where: dict, sources: dict) -> str:
    label = _metric(source)
    category = _escape(_category(source, where, sources))
    baseline_period, current_period = _period_pair(source, sources)
    rate_delta = "delta" in source.get("ratio_fields", [])
    delta = values.get("delta")
    if not where and baseline_period and current_period and not rate_delta:
        current = values.get("current")
        subject = f"{_short_period(current_period, current_period)}{label}"
        text = f"{subject} **{_number(current)}{_escape(source.get('unit', ''))}**" if current is not None else label
        if delta is not None:
            verb = "增加" if delta > 0 else "减少" if delta < 0 else "持平"
            text += f"，较{_short_period(baseline_period, current_period)}**{verb}"
            text += f" {_number(abs(delta))}{_escape(source.get('unit', ''))}**" if delta else "**"
            rate = values.get("change_rate")
            if rate is not None and delta:
                text += f"（{abs(rate) * 100:.2f}%）"
        if current is not None or delta is not None:
            return text + "。"
    if delta is not None:
        if rate_delta:
            change = "提升" if delta > 0 else "下降" if delta < 0 else "持平"
            detail = f"{change} {abs(delta) * 100:.2f} 个百分点" if delta else change
        else:
            change = "增加" if delta > 0 else "减少" if delta < 0 else "持平"
            detail = f"{change} {_number(abs(delta))}{_escape(source.get('unit', ''))}" if delta else change
        return f"**{category}**：{label}**{detail}**。"
    labels = {"new_users": "新增注册", "activated_users": "激活人数", "activation_rate": "激活率",
              "baseline": "之前", "current": "本期", "change_rate": "相对变化", "contribution_rate": "净变化贡献",
              "value": label}
    preferred = [field for field in ("current", "activation_rate", "new_users", "value", "activated_users", "baseline", "change_rate", "contribution_rate") if field in values]
    if not preferred:
        preferred = list(values)[:2]
    parts = []
    for field in preferred[:2]:
        value = values[field]
        ratio = field in source.get("ratio_fields", []) or field == "change_rate"
        number = "不可计算" if value is None else f"{value * 100:.2f}%" if ratio else _number(value) + _escape(source.get("unit", ""))
        parts.append(f"{labels.get(field, _escape(field))} **{number}**")
    period = _short_period(_period(source))
    return f"{period}**{category}**：{'，'.join(parts)}。"


def build_analysis_report(title: str, claims: list[dict], artifacts: list[dict], *,
                          notes: list[str] | None = None, limitations: list[str] | None = None,
                          charts: list | None = None, stop_reason: str | None = None) -> AnalysisReport:
    sources = {item["result_id"]: item for item in artifacts}
    findings: OrderedDict[str, list[str]] = OrderedDict()
    overview = ""
    for claim in claims:
        groups: OrderedDict[tuple, dict] = OrderedDict()
        for fact in claim.get("facts", []):
            source = sources.get(fact.get("source_id"))
            if not source:
                continue
            try:
                value = _checked_value(fact, source)
            except (ValueError, KeyError, TypeError):
                continue
            where = fact.get("where") or {}
            key = (source["result_id"], tuple(sorted(where.items())))
            group = groups.setdefault(key, {"source": source, "where": where, "values": {}})
            group["values"][fact["field"]] = value
        for group in groups.values():
            source = group["source"]
            line = _sentence(source, group["values"], group["where"], sources)
            if not overview and not group["where"]:
                overview = line
                continue
            section = "激活表现" if _metric(source) == "激活率" or "activation_rate" in group["values"] else "主要变化"
            items = findings.setdefault(section, [])
            if line != overview and line not in items:
                items.append(line)
    if not overview:
        first = next((items for items in findings.values() if items), None)
        overview = first.pop(0) if first else "已取得部分数据，分析尚未完成。" if artifacts else "分析尚未形成可核对的结论。"
    body = [overview]
    for section, items in findings.items():
        # The evidence panel retains every fact; the report highlights a few per topic.
        if items:
            body += [f"### {section}", "\n".join(f"- {item}" for item in items[:3])]
    ranges = list(dict.fromkeys(_period(a) for a in artifacts if a.get("start_date") and a.get("end_date")))
    scope = []
    if ranges:
        scope.append("统计范围：" + "、".join([ranges[0], *[_short_period(p, ranges[0]) for p in ranges[1:]]]) + "。")
    filters = sorted({channel for a in artifacts for channel in a.get("contract", {}).get("channel_ids", [])})
    if filters:
        # Raw query assets carry channel labels even when a summary has no parent metadata.
        names = [next((str(row["channel_name"]) for a in artifacts for row in a.get("rows", [])
                       if row.get("channel_id") == channel and row.get("channel_name")), channel)
                 for channel in filters]
        scope.append("限定渠道：" + "、".join(_escape(name) for name in names) + "。")
    windows = sorted({a.get("contract", {}).get("observation_days") for a in artifacts if a.get("contract", {}).get("observation_days")})
    if windows:
        scope.append("激活率按注册后的" + "、".join(str(n) for n in windows) + "天观察窗口计算。")
    if any(a.get("metric_version", "").startswith("demo-") for a in artifacts):
        scope.append("采用演示指标定义，正式使用前需确认业务口径。")
    if any("因果" in note for note in notes or []):
        scope.append("数据变化不能直接解释为因果关系。")
    if any("contribution_rate" in a.get("columns", []) for a in artifacts):
        scope.append("渠道增减会相互抵消，净变化贡献可能超过百分之百；完整计算见数据依据。")
    if scope:
        body += ["### 统计口径", " ".join(scope)]
    elif notes:
        body += ["### 范围说明", "\n".join(f"- {_escape(note)}" for note in dict.fromkeys(notes))]
    remaining = list(dict.fromkeys(limitations or []))
    if stop_reason in {"cancelled", "timed_out"}:
        remaining.insert(0, "任务已取消，已有数据保留。" if stop_reason == "cancelled" else "任务已超时，已有数据保留。")
    if remaining:
        body += ["### 尚未完成", "\n".join(f"- {_escape(item)}" for item in remaining)]
    visualizations = []
    for raw_chart in charts or []:
        # Live graph/checkpoint state stores dictionaries; saved API results use models.
        # Normalize at the report boundary before enriching labels for either path.
        chart = VisualizationSpec.model_validate(raw_chart)
        source = sources.get(chart.source_task_id, {})
        labels = {}
        if chart.category_field == "channel_id":
            labels = {str(row["channel_id"]): _category(source, {"channel_id": row["channel_id"]}, sources)
                      for row in source.get("rows", []) if row.get("channel_id") is not None}
        metric = _metric(source)
        value_label = {"delta": f"{metric}变化", "new_users": "新增注册人数", "activation_rate": "激活率"}.get(chart.value_field, "")
        if chart.value_field == "delta" and source.get("unit") == "人":
            value_label += "（人）"
        visualizations.append(chart.model_copy(update={"category_labels": labels, "value_label": value_label}))
    return AnalysisReport(title=title, summary=overview, markdown="\n\n".join(body), visualizations=visualizations)


def present_analysis_result(result: QueryResult) -> QueryResult:
    """Render old saved reports without changing their facts, records or timestamps."""
    if result.workflow_mode != "analysis_agent" or result.report is None:
        return result
    report = build_analysis_report(result.report.title, result.analysis_claims, result.result_artifacts,
                                  limitations=result.analysis_limitations, notes=result.warnings,
                                  charts=result.report.visualizations, stop_reason=result.stop_reason)
    return result.model_copy(update={"report": report, "analysis": report.markdown}, deep=True)

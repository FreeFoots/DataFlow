from __future__ import annotations

from typing import Any

from ...querying.analysis_metrics import QueryMetric, allowed_metrics
from ...querying.metric_query_service import execute_metric
from ...querying.duckdb_engine import DuckDbEngine
from ...security import AccessScope


def build_metric_query_tool(engine: DuckDbEngine, scope: AccessScope):
    def query_metric(query: QueryMetric) -> dict[str, Any]:
        try:
            return execute_metric(query, engine, scope)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
    return query_metric


def metric_description(scope: AccessScope) -> str:
    definitions = allowed_metrics(scope)
    return "按受控指标定义查询，程序编译SQL并返回结果资产。query.end_date不包含该日。可用指标：" + "; ".join(
        f"{item['metric_id']}：{item['definition']}" for item in definitions
    ) + " 支持channel/day/month维度、渠道编号筛选及order_by/descending/top_n。不能忽略不支持的筛选或擅自补日期/观察窗口。"

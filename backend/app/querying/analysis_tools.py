from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

import sqlglot
from pydantic import BaseModel, ConfigDict, Field
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

from ..database import SCHEMA, physical_table_name
from ..mcp_runtime import LocalMcpClient
from ..retrieval import SchemaGraphBuilder, SchemaIndex
from ..security import AccessScope
from .duckdb_engine import DuckDbEngine
from .analysis_metrics import QueryMetric, allowed_metrics
from .metric_query_service import data_version


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchSchema(ToolInput):
    query: str = Field(min_length=1, max_length=500)


class InspectTable(ToolInput):
    table: str = Field(min_length=1, max_length=160)


class QueryData(ToolInput):
    sql: str = Field(min_length=8, max_length=12000)
    title: str = Field(min_length=1, max_length=120)
    metric: str = Field(min_length=1, max_length=120)
    grain: list[str] = Field(default_factory=list, max_length=6)
    time_range: str = Field(min_length=1, max_length=160)
    unit: str = Field(default="", max_length=40)


class CompareResults(ToolInput):
    baseline_id: str
    current_id: str
    key_fields: list[str] = Field(default_factory=list, max_length=6)
    value_field: str
    missing_policy: Literal["error", "zero"] = "error"


class SummarizeResult(ToolInput):
    source_id: str
    group_by: list[str] = Field(default_factory=list, max_length=6)
    value_field: str
    operation: Literal["sum", "mean", "ratio"]
    denominator_field: str | None = None


class ReadResult(ToolInput):
    source_id: str
    offset: int = Field(default=0, ge=0, le=200)
    limit: int = Field(default=20, ge=1, le=50)


class BuildChart(ToolInput):
    source_id: str
    type: Literal["bar", "pie"] = "bar"
    title: str = Field(min_length=1, max_length=120)
    category_field: str
    value_field: str
    max_items: int = Field(default=12, ge=3, le=30)


class CurrentTime(ToolInput):
    timezone: Literal["Asia/Shanghai", "UTC"] = "Asia/Shanghai"


class ListMetrics(ToolInput):
    pass


@dataclass
class ToolOutput:
    observation: dict[str, Any]
    artifact: dict[str, Any] | None = None
    discovered_tables: list[str] = field(default_factory=list)
    chart: dict[str, Any] | None = None


class AnalysisTools:
    """Task-scoped result references and validated tools; data execution uses MCP."""

    INPUTS = {
        "list_metrics": (ListMetrics, "查看有权限的演示指标定义、版本、允许维度及激活队列口径。"),
        "query_metric": (QueryMetric, "按指标定义查询，程序编译SQL。start_date包含，end_date不含；激活队列必须明确观察天数。"),
        "search_schema": (SearchSchema, "按业务词检索有权限的字段、表与关联。"),
        "inspect_table": (InspectTable, "查看有权限表的真实字段、聚合方式、单位与数据画像。"),
        "query_data": (QueryData, "通过MCP执行只读SQL，必须先发现相关表。保存带口径的结果。"),
        "compare_results": (CompareResults, "对完整、同指标/粒度/单位的两份结果计算差值、变化率及净变化贡献率。"),
        "summarize_result": (SummarizeResult, "对完整结果按分组计算总和、平均或汇总分子/分母的比率。"),
        "read_result": (ReadResult, "分页查看本任务结果，适合检查摘要之外的行。"),
        "build_chart": (BuildChart, "从本任务已保存的结果生成图表，不接受模型编造的数据。"),
        "current_datetime": (CurrentTime, "获取当前日期，解析相对时间时使用。"),
    }

    def __init__(self, index: SchemaIndex, engine: DuckDbEngine, client_factory: Callable, *, exploratory_sql: bool = False):
        self.index = index
        self.engine = engine
        self.client_factory = client_factory
        self.graph_builder = SchemaGraphBuilder()
        self.INPUTS = dict(type(self).INPUTS)
        if not exploratory_sql:
            self.INPUTS.pop("query_data")

    def definitions(self) -> list[dict[str, Any]]:
        return [
            {"name": name, "description": description, "inputSchema": model.model_json_schema()}
            for name, (model, description) in self.INPUTS.items()
        ]

    def execute(self, name: str, arguments: dict, state: dict, access: dict) -> ToolOutput:
        if name not in self.INPUTS:
            raise ValueError("分析工具不在白名单中")
        args = self.INPUTS[name][0].model_validate(arguments)
        scope = AccessScope.from_dict(access)
        if not scope.allowed_databases:
            raise ValueError("当前用户没有可分析的数据源")
        client: LocalMcpClient = self.client_factory(access)
        artifacts = {item["result_id"]: item for item in state.get("artifacts", [])}
        if name == "list_metrics":
            return ToolOutput({"success": True, "metrics": allowed_metrics(scope)})
        if name == "search_schema":
            retrieval = self.index.retrieve(args.query, access_scope=scope)
            graph = self.graph_builder.build(retrieval.get("hits", []), scope)
            return ToolOutput({"success": True, "schema_graph": graph}, discovered_tables=[t["name"] for t in graph["tables"]])
        if name == "inspect_table":
            table = next((t for t in SCHEMA if args.table in {t["id"], physical_table_name(t)} and scope.allows_table(t.get("database", "short_video_ops"), t["id"])), None)
            if not table:
                raise ValueError("数据表不存在或无权访问")
            return ToolOutput({"success": True, "table": table}, discovered_tables=[physical_table_name(table)])
        if name == "query_metric":
            result = client.call_tool("query_metric", {"query": args.model_dump(mode="json")})
            return ToolOutput({"success": bool(result.get("success")), "error": result.get("error"),
                               "category": "tool_execution" if not result.get("success") else "metric_query"},
                              artifact=result.get("artifact"))
        if name == "query_data":
            database = "short_video_ops"
            safe_sql = self.engine._validate_sql(database, args.sql, scope)
            contract = {"title": args.title, "metric": args.metric, "grain": args.grain,
                        "time_range": args.time_range, "unit": args.unit, "semantic_verified": False,
                        "metric_version": "exploratory", "additive_fields": [], "ratio_fields": [],
                        "contract": {"sql": safe_sql}, "notes": ["探索性SQL口径尚未验证，不能作为受控指标事实"]}
            sources = self._sources(safe_sql)
            if not sources:
                raise ValueError("分析查询必须读取实际业务表")
            if name == "query_data" and not sources.issubset(set(state.get("discovered_tables", []))):
                raise ValueError("请先检索或查看查询涉及的数据表")
            if len(set(contract["grain"])) != len(contract["grain"]):
                raise ValueError("结果粒度字段不可重复")
            before = self._data_version(database, scope)
            result = client.call_tool(f"query_{database}", {"sql": safe_sql})
            if not result.get("success"):
                return ToolOutput({"success": False, "error": result.get("error"), "category": "sql_execution"})
            if before != self._data_version(database, scope):
                raise ValueError("查询期间数据发生变化，请重新查询")
            if not set(contract["grain"]).issubset(result["columns"]):
                raise ValueError("声明的结果粒度字段不在返回列中")
            artifact = {
                **result, **contract,
                "data_version": before, "source_tables": sorted(sources), "derived_from": [],
                "complete": not result.get("truncated", False),
            }
            return ToolOutput({"success": True}, artifact=artifact)
        if name == "current_datetime":
            return ToolOutput({"success": True, **client.call_tool(name, args.model_dump())})
        if name == "compare_results":
            if args.baseline_id == args.current_id:
                raise ValueError("不能将同一结果与自身比较。请分别query_metric查询两个不重叠周期，维度仅保留要对齐的渠道；不要同时按月份分组。")
            left = self._result(artifacts, args.baseline_id, require_complete=True)
            right = self._result(artifacts, args.current_id, require_complete=True)
            if left.get("start_date") and right.get("start_date") and not (
                left["end_date"] <= right["start_date"] or right["end_date"] <= left["start_date"]
            ):
                raise ValueError("周期对比需要不重叠的两期结果，请分别查询基期和本期")
            for key in ("metric", "unit", "data_version"):
                if left[key] != right[key]:
                    raise ValueError(f"比较结果的{key}不一致")
            if left.get("metric_version") != right.get("metric_version") or left.get("contract") != right.get("contract"):
                raise ValueError("比较结果的指标定义或筛选/观察窗口不一致")
            if left["grain"] != args.key_fields or right["grain"] != args.key_fields:
                raise ValueError("比较键必须与两份结果粒度一致")
            a = self._keyed_values(left, args.key_fields, args.value_field)
            b = self._keyed_values(right, args.key_fields, args.value_field)
            if a.keys() != b.keys() and args.missing_policy == "error":
                raise ValueError("两期分组不一致；请补查，或明确使用缺失分组按0处理")
            total_delta = sum(b.values()) - sum(a.values())
            additive = args.value_field in left.get("additive_fields", []) and args.value_field in right.get("additive_fields", [])
            rows = []
            for key in sorted(a.keys() | b.keys(), key=repr):
                baseline, current = a.get(key, 0), b.get(key, 0)
                delta = current - baseline
                rows.append({**dict(zip(args.key_fields, key)), "baseline": baseline, "current": current, "delta": delta,
                             "change_rate": delta / baseline if baseline else None,
                             "contribution_rate": delta / total_delta if additive and total_delta else None})
            return ToolOutput({"success": True}, artifact={
                "title": f"{left['title']}与{right['title']}对比", "columns": [*args.key_fields, "baseline", "current", "delta", "change_rate", "contribution_rate"], "rows": rows,
                "metric": f"{left['metric']}:{args.value_field}:comparison", "grain": args.key_fields,
                "unit": "比例" if args.value_field in left.get("ratio_fields", []) else left["unit"],
                "metric_version": left.get("metric_version"), "contract": left.get("contract"),
                "semantic_verified": left.get("semantic_verified", False) and right.get("semantic_verified", False),
                "additive_fields": ["baseline", "current", "delta"] if additive else [],
                "ratio_fields": ["change_rate", "contribution_rate"] + (["baseline", "current", "delta"] if args.value_field in left.get("ratio_fields", []) else []),
                "time_range": f"{left['time_range']} → {right['time_range']}", "data_version": left["data_version"],
                "complete": True, "truncated": False, "limited": False,
                "derived_from": [args.baseline_id, args.current_id],
                "summary": {"baseline": sum(a.values()), "current": sum(b.values()), "delta": total_delta,
                            "change_rate": total_delta / sum(a.values()) if sum(a.values()) else None} if additive else {},
                "notes": ["change_rate与contribution_rate为小数比例；分母为0时返回空值", "贡献率使用净变化作分母，存在正负抵消时可能超过100%"],
            })
        if name == "summarize_result":
            source = self._result(artifacts, args.source_id, require_complete=True)
            required = {*args.group_by, args.value_field}
            if args.operation == "ratio":
                if not args.denominator_field:
                    raise ValueError("比率计算必须指定分母字段")
                required.add(args.denominator_field)
            if len(set(args.group_by)) != len(args.group_by) or not required.issubset(source["columns"]):
                raise ValueError("汇总字段不存在或分组重复")
            if not set(args.group_by).issubset(source["grain"]):
                raise ValueError("汇总只能保留来源中的粒度字段")
            if args.operation in {"sum", "mean"} and args.value_field not in source.get("additive_fields", []):
                raise ValueError("该字段不支持整体求和或普通平均，请使用分子/分母比率")
            if args.operation == "ratio" and not {args.value_field, args.denominator_field}.issubset(source.get("additive_fields", [])):
                raise ValueError("比率必须使用可加的分子和分母")
            groups: dict[tuple, list[dict]] = {}
            for row in source["rows"]:
                key = tuple(row[field] for field in args.group_by)
                groups.setdefault(key, []).append(row)
            rows = []
            for key, items in groups.items():
                numerator = sum(self._number(row[args.value_field]) for row in items)
                value = numerator
                if args.operation == "mean":
                    value /= len(items)
                elif args.operation == "ratio":
                    denominator = sum(self._number(row[args.denominator_field]) for row in items)
                    if numerator < 0 or denominator < 0:
                        raise ValueError("比例分子/分母不可为负")
                    value = numerator / denominator if denominator else None
                rows.append({**dict(zip(args.group_by, key)), "value": value})
            return ToolOutput({"success": True}, artifact={
                "title": f"{source['title']}{'加权比率' if args.operation == 'ratio' else '汇总'}", "columns": [*args.group_by, "value"], "rows": rows,
                "metric": f"{args.operation}({args.value_field})", "grain": args.group_by,
                "unit": "比例" if args.operation == "ratio" else source["unit"],
                "metric_version": source.get("metric_version"), "contract": source.get("contract"),
                "semantic_verified": source.get("semantic_verified", False),
                "additive_fields": ["value"] if args.operation == "sum" else [],
                "ratio_fields": ["value"] if args.operation == "ratio" else [],
                "time_range": source["time_range"], "data_version": source["data_version"],
                "complete": True, "truncated": False, "limited": False, "derived_from": [args.source_id],
                "notes": [*source.get("notes", []), "比率按分子总和除以分母总和计算"] if args.operation == "ratio" else source.get("notes", []),
            })
        source = self._result(artifacts, args.source_id)
        if name == "read_result":
            return ToolOutput({"success": True, "source_id": args.source_id, "columns": source["columns"], "row_count": len(source["rows"]), "complete": source["complete"], "rows": source["rows"][args.offset:args.offset + args.limit]})
        if name == "build_chart":
            if not {args.category_field, args.value_field}.issubset(source["columns"]):
                raise ValueError("图表字段不在来源结果中")
            spec = client.call_tool(f"build_{args.type}_chart", {
                "title": args.title, "source_task_id": args.source_id,
                "category_field": args.category_field, "value_field": args.value_field, "max_items": args.max_items,
            })
            return ToolOutput({"success": True, "chart": spec}, chart=spec)
        raise ValueError("不支持的工具")

    @staticmethod
    def _sources(sql: str) -> set[str]:
        return {source.name.casefold() for scope in traverse_scope(sqlglot.parse_one(sql, read="duckdb")) for _, source in scope.selected_sources.values() if isinstance(source, exp.Table)}

    def _data_version(self, database: str, scope: AccessScope) -> str:
        return data_version(self.engine, database, scope)

    @staticmethod
    def _result(artifacts: dict, result_id: str, require_complete: bool = False) -> dict:
        if result_id not in artifacts:
            raise ValueError("结果引用不存在于本任务")
        result = artifacts[result_id]
        if require_complete and (not result["complete"] or result.get("limited")):
            raise ValueError("截断或LIMIT/OFFSET结果不能用于整体汇总和变化归因；请重新聚合查询")
        return result

    @staticmethod
    def _number(value: Any) -> int | float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("计算字段包含空值或非有限数值，请先明确处理方式")
        return value

    def _keyed_values(self, result: dict, fields: list[str], value_field: str) -> dict:
        if not {*fields, value_field}.issubset(result["columns"]):
            raise ValueError("比较字段不存在")
        values = {}
        for row in result["rows"]:
            key = tuple(row[field] for field in fields)
            if key in values:
                raise ValueError("比较结果在指定粒度下不唯一，请先聚合")
            values[key] = self._number(row[value_field])
        return values

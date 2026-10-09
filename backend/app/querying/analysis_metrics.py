from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Literal

from pydantic import Field, model_validator

from ..database import SCHEMA, physical_table_name
from ..security import AccessScope
from .duckdb_engine import DuckDbEngine
from pydantic import BaseModel, ConfigDict


METRICS = {
    "new_users": {
        "title": "新增注册用户", "tables": ["users"], "unit": "人",
        "definition": "按 users.registered_at 的注册日期统计去重 user_id；渠道使用注册渠道，不使用广告归因渠道。",
        "additive_fields": ["new_users"],
    },
    "activation_cohort": {
        "title": "注册队列激活", "tables": ["users", "user_activations"], "unit": "人",
        "definition": "指定期间注册的用户，在注册后指定天数内的去重激活人数/注册人数。观察窗口不足时拒绝执行，不能与按激活发生日期统计混用。",
        "additive_fields": ["new_users", "activated_users"],
    },
}
METRIC_VERSION = "demo-registration-cohort-v1"


class QueryMetric(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metric_id: Literal["new_users", "activation_cohort"]
    start_date: date
    end_date: date = Field(description="不含该日，例如4月用2026-04-01到2026-05-01")
    dimensions: list[Literal["channel", "day", "month"]] = Field(default_factory=list, max_length=2)
    channel_ids: list[str] = Field(default_factory=list, max_length=10)
    observation_days: int | None = Field(default=None, ge=1, le=30, description="activation_cohort必须明确指定，如7天；new_users无需填写")
    order_by: Literal["new_users", "activated_users", "activation_rate"] | None = None
    descending: bool = True
    top_n: int | None = Field(default=None, ge=1, le=200)

    @model_validator(mode="after")
    def validate_range(self):
        if self.start_date >= self.end_date or (self.end_date - self.start_date).days > 366:
            raise ValueError("日期范围必须递增，最多366天")
        if len(set(self.dimensions)) != len(self.dimensions) or {"day", "month"}.issubset(self.dimensions):
            raise ValueError("日期粒度只能选择day或month，维度不可重复")
        if self.metric_id == "activation_cohort" and self.observation_days is None:
            raise ValueError("请明确激活观察天数")
        if self.metric_id == "new_users" and self.observation_days is not None:
            raise ValueError("新增注册指标不使用激活观察天数")
        if self.metric_id == "new_users" and self.order_by in {"activated_users", "activation_rate"}:
            raise ValueError("新增注册指标不能按激活字段排序")
        if self.top_n is not None and (not self.dimensions or self.order_by is None):
            raise ValueError("前N查询必须明确分组维度和排序指标")
        return self


def allowed_metrics(scope: AccessScope) -> list[dict]:
    tables = {physical_table_name(t) for t in SCHEMA if scope.allows_table(t.get("database", "short_video_ops"), t["id"])}
    return [{"metric_id": key, "version": METRIC_VERSION, **value, "dimensions": ["channel", "day", "month"],
             "definition_status": "演示定义，正式业务口径需确认"}
            for key, value in METRICS.items() if set(value["tables"]).issubset(tables)]


def compile_metric(args: QueryMetric, engine: DuckDbEngine, scope: AccessScope) -> tuple[str, dict]:
    metric = next((m for m in allowed_metrics(scope) if m["metric_id"] == args.metric_id), None)
    if not metric:
        raise ValueError("当前权限不支持此指标")
    manifest = engine._database_folder("short_video_ops") / "_database_manifest.json"
    if not manifest.exists():
        raise ValueError("缺少数据覆盖日期，无法验证查询范围")
    coverage = json.loads(manifest.read_text())["data_range"]
    first, last = date.fromisoformat(coverage["start"]), date.fromisoformat(coverage["end"])
    if args.start_date < first or args.end_date > last + timedelta(days=1):
        raise ValueError(f"请求超出数据覆盖范围：{first}至{last}")
    # The last registration may occur immediately before the exclusive end.
    if args.observation_days and args.end_date + timedelta(days=args.observation_days) > last + timedelta(days=1):
        raise ValueError("注册队列的激活观察窗口尚未完整，请缩小周期或观察天数")
    channel_table = next(t for t in SCHEMA if physical_table_name(t) == "acquisition_channels")
    if "channel" in args.dimensions and not scope.allows_table("short_video_ops", channel_table["id"]):
        raise ValueError("无权查询渠道信息")
    if any(not value or len(value) > 64 or not all(c.isalnum() or c in "_-" for c in value) for value in args.channel_ids):
        raise ValueError("渠道编号无效")
    projections, grain, grouping = [], [], []
    if "channel" in args.dimensions:
        projections.extend(["u.register_channel_id AS channel_id", "c.channel_name AS channel_name"])
        grain.append("channel_id")
        grouping.extend(["u.register_channel_id", "c.channel_name"])
    for dimension in ("day", "month"):
        if dimension in args.dimensions:
            expression = "CAST(u.registered_at AS DATE)" if dimension == "day" else "DATE_TRUNC('month', u.registered_at)"
            projections.append(f"{expression} AS {dimension}")
            grain.append(dimension)
            grouping.append(expression)
    projections.append("COUNT(DISTINCT u.user_id) AS new_users")
    joins = ""
    if "channel" in args.dimensions:
        joins += " LEFT JOIN acquisition_channels c ON u.register_channel_id = c.channel_id"
    if args.metric_id == "activation_cohort":
        joins += " LEFT JOIN user_activations a ON u.user_id = a.user_id"
        expression = ("COUNT(DISTINCT CASE WHEN a.is_activated = TRUE AND a.activated_at >= u.registered_at "
                      f"AND a.activated_at < u.registered_at + INTERVAL '{args.observation_days} days' THEN u.user_id END)")
        projections += [f"{expression} AS activated_users", f"{expression} * 1.0 / NULLIF(COUNT(DISTINCT u.user_id), 0) AS activation_rate"]
    predicate = f"u.registered_at >= DATE '{args.start_date}' AND u.registered_at < DATE '{args.end_date}'"
    if args.channel_ids:
        predicate += " AND u.register_channel_id IN (" + ",".join("'" + x + "'" for x in sorted(set(args.channel_ids))) + ")"
    sql = "SELECT " + ", ".join(projections) + " FROM users u" + joins + " WHERE " + predicate
    if grouping:
        sql += " GROUP BY " + ", ".join(grouping)
    ordering = ([args.order_by + (" DESC" if args.descending else " ASC")] if args.order_by else []) + grain
    if ordering:
        sql += " ORDER BY " + ", ".join(ordering)
    if args.top_n is not None:
        sql += f" LIMIT {args.top_n}"
    contract = {
        "metric": args.metric_id, "metric_version": METRIC_VERSION, "semantic_verified": True,
        "title": f"{args.start_date}至{args.end_date}（不含）{metric['title']}",
        "grain": grain, "unit": metric["unit"], "time_range": f"[{args.start_date}, {args.end_date})",
        "start_date": str(args.start_date), "end_date": str(args.end_date),
        "contract": {"metric_id": args.metric_id, "metric_version": METRIC_VERSION,
                     "channel_ids": sorted(set(args.channel_ids)), "observation_days": args.observation_days,
                     "dimensions": sorted(args.dimensions)},
        "additive_fields": metric["additive_fields"], "ratio_fields": ["activation_rate"] if args.metric_id == "activation_cohort" else [],
        "notes": [metric["definition"], "演示口径；正式业务定义需确认", f"数据覆盖截至{last}"] +
                 ([f"激活观察窗口为注册后{args.observation_days}天"] if args.observation_days else []),
    }
    return engine._validate_sql("short_video_ops", sql, scope), contract

"""One metric compiler, execution policy and asset format for every query path."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

from ..database import SCHEMA, physical_table_name
from ..security import AccessScope
from .analysis_metrics import QueryMetric, compile_metric
from .duckdb_engine import DuckDbEngine


def data_version(engine: DuckDbEngine, database: str, scope: AccessScope) -> str:
    folder = engine._database_folder(database)
    versions = []
    for table in SCHEMA:
        if table.get("database", "short_video_ops") == database and scope.allows_table(database, table["id"]):
            path = folder / f"{physical_table_name(table)}.csv"
            stat = path.stat()
            versions.append((path.name, stat.st_size, stat.st_mtime_ns))
    return hashlib.sha256(json.dumps(versions).encode()).hexdigest()[:16]


def execute_metric(query: QueryMetric, engine: DuckDbEngine, scope: AccessScope) -> dict:
    sql, contract = compile_metric(query, engine, scope)
    database = "short_video_ops"
    before = data_version(engine, database, scope)
    result = {**asdict(engine.execute(database, sql, scope)), "database": database}
    result["row_count"] = len(result["rows"])
    if not result["success"]:
        return result
    if before != data_version(engine, database, scope):
        raise ValueError("查询期间数据发生变化，请重新查询")
    if not set(contract["grain"]).issubset(result["columns"]):
        raise ValueError("声明的结果粒度字段不在返回列中")
    from sqlglot import exp, parse_one
    from sqlglot.optimizer.scope import traverse_scope
    sources = {source.name.casefold() for item in traverse_scope(parse_one(sql, read="duckdb"))
               for _, source in item.selected_sources.values() if isinstance(source, exp.Table)}
    artifact = {**result, **contract, "data_version": before, "source_tables": sorted(sources),
                "derived_from": [], "complete": not result["truncated"]}
    # Whole-result statistics are never computed from previews or Top N subsets.
    if artifact["complete"] and not result["limited"]:
        summary = {key: sum(row[key] for row in result["rows"]) for key in contract["additive_fields"]}
        if query.metric_id == "activation_cohort":
            summary["activation_rate"] = summary["activated_users"] / summary["new_users"] if summary["new_users"] else None
        artifact["summary"] = summary
    return {**result, "artifact": artifact}

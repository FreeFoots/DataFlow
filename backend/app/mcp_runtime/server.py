from __future__ import annotations

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from ..database import SCHEMA, physical_table_name
from ..querying.duckdb_engine import DuckDbEngine
from ..security import AccessController, AccessScope
from .tools import (
    build_bar_chart,
    build_database_query_tool,
    build_pie_chart,
    current_datetime,
    resolve_date_range,
)
from .tools.metric_tools import build_metric_query_tool, metric_description
from ..querying.analysis_metrics import allowed_metrics


READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


def create_local_mcp_server(
    engine: DuckDbEngine | None = None,
    access_scope: AccessScope | None = None,
) -> MCPServer:
    """创建与FastAPI运行在同一进程中的MCP服务。"""
    database_engine = engine or DuckDbEngine()
    scope = access_scope or AccessController().resolve(None)
    server = MCPServer(
        name="dataflow-local-tools",
        title="DataFlow 本地工具服务",
        description="提供本地数据库只读查询和基础时间计算工具。",
        instructions="调用数据库工具前先根据Schema图生成一条只读DuckDB SQL。",
    )

    server.tool(
        name="current_datetime",
        title="获取当前日期时间",
        description="获取Asia/Shanghai或UTC时区的当前日期与时间。",
        annotations=READ_ONLY,
    )(current_datetime)
    server.tool(
        name="resolve_date_range",
        title="解析相对日期范围",
        description="将今天、昨天、本周、上周、本月、上月或今年转换为明确起止日期。",
        annotations=READ_ONLY,
    )(resolve_date_range)
    server.tool(
        name="build_bar_chart",
        title="生成柱状图配置",
        description=(
            "基于一张已有查询结果生成前端柱状图配置。"
            "适合比较不同类别的数值，不读取数据库，也不生成模拟数据。"
        ),
        annotations=READ_ONLY,
    )(build_bar_chart)
    server.tool(
        name="build_pie_chart",
        title="生成饼图配置",
        description=(
            "基于一张已有查询结果生成前端饼图配置。"
            "只适合类别较少的占比分析，不读取数据库，也不生成模拟数据。"
        ),
        annotations=READ_ONLY,
    )(build_pie_chart)

    databases = sorted({str(table.get("database") or "short_video_ops") for table in SCHEMA})
    for database in databases:
        if not scope.allows_database(database):
            continue
        tables = [
            physical_table_name(table)
            for table in SCHEMA
            if table.get("database") == database
            and scope.allows_table(database, table["id"])
        ]
        handler = build_database_query_tool(database, database_engine, scope)
        server.tool(
            name=f"query_{database}",
            title=f"查询数据库 {database}",
            description=(
                f"在数据库{database}中执行一条只读DuckDB SQL。"
                f"可用表：{', '.join(tables)}。每次调用最多返回200行。"
            ),
            annotations=READ_ONLY,
        )(handler)
    if allowed_metrics(scope):
        server.tool(name="query_metric", title="查询业务指标", description=metric_description(scope),
                    annotations=READ_ONLY)(build_metric_query_tool(database_engine, scope))
    return server

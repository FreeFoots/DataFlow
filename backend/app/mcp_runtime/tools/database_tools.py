from __future__ import annotations

from typing import Annotated, Callable

from pydantic import Field

from ...querying.duckdb_engine import DuckDbEngine
from ...security import AccessScope
from ..schemas import DatabaseQueryResult


SqlStatement = Annotated[
    str,
    Field(
        description=(
            "需要执行的一条DuckDB SELECT或WITH只读SQL。"
            "只能引用当前工具对应数据库中的表，最多返回200行。"
        ),
        min_length=8,
        max_length=12000,
    ),
]


def build_database_query_tool(
    database: str,
    engine: DuckDbEngine,
    access_scope: AccessScope,
) -> Callable[[SqlStatement], DatabaseQueryResult]:
    """为一个数据库创建一个独立的MCP查询工具。"""

    def query_database(sql: SqlStatement) -> DatabaseQueryResult:
        # 工具处理器再次执行数据库、表和固定SQL规则校验。
        execution = engine.execute(database, sql, access_scope)
        return DatabaseQueryResult(
            database=database,
            sql=execution.sql,
            success=execution.success,
            columns=execution.columns,
            rows=execution.rows,
            row_count=len(execution.rows),
            truncated=execution.truncated,
            limited=execution.limited,
            execution_ms=execution.execution_ms,
            error=execution.error,
        )

    query_database.__name__ = f"query_{database}"
    return query_database

from __future__ import annotations

import re
import time
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

import duckdb
import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError
from sqlglot.optimizer.scope import Scope, traverse_scope

from ..config import BASE_DIR
from ..database import SCHEMA, physical_table_name
from ..security import AccessScope
from .models import SqlExecution


DATABASE_ROOT = BASE_DIR / "data" / "databases"

# Only reviewed analytical functions may be called by generated SQL. In particular,
# an expression being a SELECT does not make file/network/catalog functions safe.
SAFE_FUNCTIONS = frozenset({
    "ABS", "AVG", "CEIL", "CEILING", "COALESCE", "COUNT", "CURRENT_DATE",
    "CURRENT_TIMESTAMP", "DATE", "DATE_ADD", "DATE_DIFF", "DATE_SUB", "DATE_TRUNC",
    "DATEDIFF", "DAY", "EXTRACT", "FLOOR", "GREATEST", "IF", "LAG", "LEAD",
    "LEAST", "LOWER", "MAX", "MIN", "MONTH", "NULLIF", "POWER", "ROUND",
    "ROW_NUMBER", "RANK", "DENSE_RANK", "SQRT", "STDDEV", "STDDEV_POP",
    "STDDEV_SAMP", "STRFTIME", "SUM", "TIME_TO_STR", "TRIM", "UPPER", "YEAR",
    "CAST", "TRY_CAST", "CASE", "EXISTS", "CONCAT", "CONCAT_WS", "SUBSTRING", "LENGTH",
    "DATE_PART", "TS_OR_DS_TO_DATE", "TS_OR_DS_ADD", "TIMESTAMP_TRUNC",
})


class DuckDbEngine:
    """将 CSV 目录映射为只读 DuckDB 数据库。

    CSV 文件名作为 SQL 表名使用。
    """

    def __init__(self, database_root: Path | None = None) -> None:
        self.database_root = database_root or DATABASE_ROOT

    def execute(
        self,
        database: str,
        sql: str,
        access_scope: AccessScope | None = None,
    ) -> SqlExecution:
        started_at = time.perf_counter()
        try:
            safe_sql = self._validate_sql(database, sql, access_scope)
            with self.connect(database, access_scope) as connection:
                cursor = connection.execute(safe_sql)
                raw_rows = cursor.fetchmany(201)
                columns = [item[0] for item in cursor.description or []]
                rows = [
                    {column: self._json_value(value) for column, value in zip(columns, row)}
                    for row in raw_rows[:200]
                ]
            return SqlExecution(
                safe_sql,
                True,
                columns,
                rows,
                execution_ms=round((time.perf_counter() - started_at) * 1000, 3),
                truncated=len(raw_rows) > 200,
            )
        except (ValueError, ParseError, OptimizeError, duckdb.Error, OSError) as exc:
            return SqlExecution(
                sql,
                False,
                error=str(exc),
                execution_ms=round((time.perf_counter() - started_at) * 1000, 3),
            )

    @contextmanager
    def connect(self, database: str, access_scope: AccessScope | None = None) -> Iterator[duckdb.DuckDBPyConnection]:
        """创建内存连接并将 CSV 文件注册为只读视图。"""
        folder = self._database_folder(database)
        if access_scope and not access_scope.allows_database(database):
            raise ValueError("当前用户无权访问该数据库")
        allowed_names = {
            physical_table_name(table) for table in SCHEMA
            if table.get("database", "short_video_ops") == database
            and (not access_scope or access_scope.allows_table(database, table["id"]))
        }
        connection = duckdb.connect(":memory:")
        try:
            csv_files = sorted(folder.glob("*.csv"))
            if not csv_files:
                raise ValueError(f"数据库文件夹没有CSV表：{folder}")
            for csv_path in csv_files:
                table = csv_path.stem
                if table not in allowed_names:
                    continue
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
                    raise ValueError(f"CSV文件名不能作为安全表名：{csv_path.name}")
                path = csv_path.resolve().as_posix().replace("'", "''")
                connection.execute(
                    f'CREATE VIEW "{table}" AS '
                    f"SELECT * FROM read_csv_auto('{path}', header=true, sample_size=-1)"
                )
            yield connection
        finally:
            connection.close()

    def _validate_sql(
        self,
        database: str,
        sql: str,
        access_scope: AccessScope | None = None,
    ) -> str:
        cleaned = self.clean_sql(sql).rstrip(";").strip()
        statements = [item for item in sqlglot.parse(cleaned, read="duckdb") if item]
        if len(statements) != 1:
            raise ValueError("一次只允许执行一条SQL")
        statement = statements[0]
        if not isinstance(statement, exp.Query):
            raise ValueError("只允许执行SELECT/WITH只读查询")
        forbidden_nodes = {
            "insert", "update", "delete", "merge", "create", "drop", "alter",
            "copy", "attach", "detach", "command", "transaction", "grant", "revoke",
        }
        if any(node.key in forbidden_nodes for node in statement.walk()):
            raise ValueError("SQL包含禁止的写入或管理操作")

        # 查询必须显式列出返回字段。
        for select in statement.find_all(exp.Select):
            if any(
                isinstance(projection, exp.Star)
                or isinstance(projection, exp.Column) and projection.is_star
                for projection in select.expressions
            ):
                raise ValueError("不允许使用SELECT *，必须明确列出查询字段")

        if access_scope and not access_scope.allows_database(database):
            raise ValueError("当前用户无权访问该数据库")

        database_tables = [
            table
            for table in SCHEMA
            if table.get("database", "short_video_ops") == database
        ]
        allowed = {
            physical_table_name(table): table["id"]
            for table in database_tables
        }
        referenced: set[str] = set()
        for scope in traverse_scope(statement):
            # Resolve CTEs in their lexical scope, including unused CTEs, subqueries,
            # UNION branches and semi/anti joins. Qualified tables are always real.
            for table in scope.tables:
                if not isinstance(table.this, exp.Identifier):
                    raise ValueError("不允许使用外部文件、表函数或动态数据源")
                if table.catalog or (table.db and table.db.lower() != "main"):
                    raise ValueError("不允许引用其他数据库或系统数据源")
                source = scope.sources.get(table.alias_or_name)
                if not table.db and not table.catalog and isinstance(source, Scope):
                    continue
                referenced.add(table.name)
        for function in statement.find_all(exp.Func):
            if isinstance(function, (exp.Binary, exp.Not)):
                continue  # SQLGlot also represents boolean operators as Func nodes.
            name = function.name.upper() if isinstance(function, exp.Anonymous) else function.sql_name().upper()
            if name not in SAFE_FUNCTIONS:
                raise ValueError(f"当前不支持该查询函数：{name}")
        unknown = {name for name in referenced if name not in allowed}
        if unknown:
            raise ValueError(f"SQL引用了未知数据表：{', '.join(sorted(unknown))}")
        if access_scope:
            denied = {
                table
                for table in referenced
                if not access_scope.allows_table(database, allowed[table])
            }
            if denied:
                raise ValueError("SQL引用了当前用户无权访问的数据表")
        return cleaned

    def _database_folder(self, database: str) -> Path:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", database):
            raise ValueError("数据库名称不合法")
        folder = (self.database_root / database).resolve()
        root = self.database_root.resolve()
        if root not in folder.parents or not folder.is_dir():
            raise ValueError(f"本地CSV数据库不存在：{database}")
        return folder

    @staticmethod
    def clean_sql(text: str) -> str:
        cleaned = text.strip()
        cleaned = re.sub(r"^```(?:sql)?\s*|\s*```$", "", cleaned, flags=re.I)
        return cleaned.strip()

    @staticmethod
    def _json_value(value: Any) -> Any:
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        if isinstance(value, Decimal):
            return float(value)
        return value

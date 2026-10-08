from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class DateTimeResult(BaseModel):
    date: str = Field(description="当前日期，格式为YYYY-MM-DD")
    datetime: str = Field(description="包含时区的当前时间")
    timezone: str = Field(description="返回结果使用的时区")


class DateRangeResult(BaseModel):
    expression: str = Field(description="输入的相对时间表达式")
    start_date: str = Field(description="起始日期，格式为YYYY-MM-DD")
    end_date: str = Field(description="结束日期，格式为YYYY-MM-DD，包含当天")
    timezone: str = Field(description="计算日期范围时使用的时区")


class DatabaseQueryResult(BaseModel):
    database: str = Field(description="执行查询的数据库名称")
    sql: str = Field(description="实际执行或尝试执行的SQL")
    success: bool = Field(description="SQL是否执行成功")
    columns: list[str] = Field(default_factory=list, description="结果字段")
    rows: list[dict[str, Any]] = Field(default_factory=list, description="查询结果，最多200行")
    row_count: int = Field(default=0, description="返回结果行数")
    truncated: bool = Field(default=False, description="结果超过200行，当前仅返回预览")
    execution_ms: float = Field(default=0.0, description="SQL校验与执行耗时，单位毫秒")
    error: str | None = Field(default=None, description="执行失败时的错误信息")


class ChartSpecResult(BaseModel):
    type: str = Field(description="前端图表类型：bar或pie")
    title: str = Field(description="图表标题")
    source_task_id: str = Field(description="提供图表数据的查询任务ID")
    category_field: str = Field(description="分类字段")
    value_field: str = Field(description="数值字段")
    max_items: int = Field(description="前端最多展示的数据项数")

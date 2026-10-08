from __future__ import annotations

from typing import Annotated

from pydantic import Field

from ..schemas import ChartSpecResult


Title = Annotated[str, Field(description="简洁、明确的图表标题", min_length=1, max_length=80)]
SourceTaskId = Annotated[
    str,
    Field(description="数据来源任务ID，必须来自当前可用数据上下文", min_length=1, max_length=64),
]
FieldName = Annotated[
    str,
    Field(description="查询结果中真实存在的字段名", min_length=1, max_length=128),
]
MaxItems = Annotated[int, Field(description="最多展示的数据项数", ge=3, le=30)]


def build_bar_chart(
    title: Title,
    source_task_id: SourceTaskId,
    category_field: FieldName,
    value_field: FieldName,
    max_items: MaxItems = 12,
) -> ChartSpecResult:
    """生成柱状图展示配置，用于比较不同类别的数值大小。"""
    return ChartSpecResult(
        type="bar",
        title=title,
        source_task_id=source_task_id,
        category_field=category_field,
        value_field=value_field,
        max_items=max_items,
    )


def build_pie_chart(
    title: Title,
    source_task_id: SourceTaskId,
    category_field: FieldName,
    value_field: FieldName,
    max_items: MaxItems = 8,
) -> ChartSpecResult:
    """生成饼图展示配置，用于展示少量类别之间的占比构成。"""
    return ChartSpecResult(
        type="pie",
        title=title,
        source_task_id=source_task_id,
        category_field=category_field,
        value_field=value_field,
        max_items=max_items,
    )

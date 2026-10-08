"""Explicit, case-specific display aliases; never match arbitrary columns by value."""
from typing import Any


CASE_COLUMN_ALIASES = {
    "GROWTH_OPS-001": {"month": ["月份"], "value": ["new_users", "新增用户数", "新增用户", "指标值"]},
    "GROWTH_OPS-061": {"channel_name": ["渠道名称"], "avg_activation_seconds": ["平均激活耗时"]},
    "CHANNEL_OPS-001": {"stat_date": ["统计日期", "日期"], "spend": ["广告消耗"]},
    "CHANNEL_OPS-021": {"month": ["月份"], "spend": ["广告消耗"], "mom": ["mom_rate", "环比变化率"]},
    "CONTENT_OPS-001": {"metric_date": ["指标日期", "日期"], "impressions": ["曝光量"], "plays": ["play_count", "播放量"]},
    "CONTENT_OPS-029": {"category_name": ["内容分类"], "avg_watch_seconds": ["平均观看时长"], "avg_completion_rate": ["平均完播率"]},
}


def canonicalize_columns(case: dict[str, Any], columns: list[str], rows: list[dict[str, Any]]):
    expected = case["expected_columns"]
    allowed = CASE_COLUMN_ALIASES.get(case["case_id"], {})
    aliases = {canonical: canonical for canonical in expected}
    for canonical, names in allowed.items():
        if canonical not in expected:
            raise ValueError("列名契约引用了不存在的标准列")
        for name in names:
            if name in aliases and aliases[name] != canonical:
                raise ValueError("列名契约存在歧义")
            aliases[name] = canonical
    mapped = [aliases.get(column, column) for column in columns]
    if len(set(mapped)) != len(mapped) or any(set(row) != set(columns) for row in rows):
        raise ValueError("结果列存在重复、歧义或未声明字段")
    return mapped, [{aliases.get(column, column): value for column, value in row.items()} for row in rows]

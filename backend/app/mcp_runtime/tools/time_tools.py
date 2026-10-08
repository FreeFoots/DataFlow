from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from typing import Literal

from ..schemas import DateRangeResult, DateTimeResult


TimezoneName = Literal["Asia/Shanghai", "UTC"]
DateExpression = Literal["今天", "昨天", "本周", "上周", "本月", "上月", "今年"]


def _timezone(name: TimezoneName) -> timezone:
    if name == "UTC":
        return timezone.utc
    return timezone(timedelta(hours=8), name="Asia/Shanghai")


def current_datetime(timezone_name: TimezoneName = "Asia/Shanghai") -> DateTimeResult:
    """获取指定时区的当前日期和时间，用于解析今天、当前等相对时间。"""
    now = datetime.now(_timezone(timezone_name))
    return DateTimeResult(
        date=now.date().isoformat(),
        datetime=now.isoformat(timespec="seconds"),
        timezone=timezone_name,
    )


def resolve_date_range(
    expression: DateExpression,
    timezone_name: TimezoneName = "Asia/Shanghai",
) -> DateRangeResult:
    """将常见中文相对时间转换为包含首尾日期的明确日期范围。"""
    today = datetime.now(_timezone(timezone_name)).date()
    start, end = _date_range(expression, today)
    return DateRangeResult(
        expression=expression,
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        timezone=timezone_name,
    )


def _date_range(expression: DateExpression, today: date) -> tuple[date, date]:
    if expression == "今天":
        return today, today
    if expression == "昨天":
        yesterday = today - timedelta(days=1)
        return yesterday, yesterday
    if expression == "本周":
        start = today - timedelta(days=today.weekday())
        return start, start + timedelta(days=6)
    if expression == "上周":
        end = today - timedelta(days=today.weekday() + 1)
        return end - timedelta(days=6), end
    if expression == "本月":
        start = today.replace(day=1)
        return start, today.replace(day=monthrange(today.year, today.month)[1])
    if expression == "上月":
        end = today.replace(day=1) - timedelta(days=1)
        return end.replace(day=1), end
    return today.replace(month=1, day=1), today.replace(month=12, day=31)

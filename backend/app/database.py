from __future__ import annotations

"""加载短视频运营数据库的表、字段及表关系定义。"""

import json
from pathlib import Path


SCHEMA_PATH = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "databases"
    / "short_video_ops"
    / "_schema.json"
)


def _load_schema() -> tuple[list[dict], list[dict]]:
    if not SCHEMA_PATH.exists():
        raise FileNotFoundError(f"未找到运营场景Schema：{SCHEMA_PATH}")
    payload = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return list(payload.get("tables") or []), list(payload.get("relations") or [])


SCHEMA, RELATIONS = _load_schema()


def physical_table_name(table: dict) -> str:
    return str(table.get("name") or table["id"])

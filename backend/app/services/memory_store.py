from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import BASE_DIR


class MemoryStore:
    """按用户隔离的长期记忆JSON存储。"""

    def __init__(self, path: Path | None = None, limit: int = 20) -> None:
        self.path = path or BASE_DIR / "data" / "saved_memories.json"
        self.limit = limit

    def save_result(
        self,
        task_id: str,
        query: str,
        summary: str,
        title: str,
        columns: list[str],
        rows: list[dict[str, Any]],
        user_id: str = "demo_growth_ops",
    ) -> None:
        memory_id = f"result:{task_id}"
        self._upsert(user_id, {
            "id": memory_id,
            "user_id": user_id,
            "kind": "result_table",
            "task_id": task_id,
            "query": query,
            "summary": summary,
            "title": title,
            "columns": columns,
            "rows": rows,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        })

    def save_field(
        self,
        table_id: str,
        name: str,
        label: str,
        field_type: str,
        user_id: str = "demo_growth_ops",
    ) -> None:
        memory_id = f"field:{table_id}.{name}"
        self._upsert(user_id, {
            "id": memory_id,
            "user_id": user_id,
            "kind": "schema_field",
            "table_id": table_id,
            "name": name,
            "label": label,
            "field_type": field_type,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        })

    def delete(
        self,
        memory_id: str,
        user_id: str | None = None,
        *,
        include_all: bool = False,
    ) -> None:
        items = self._read_all()
        self._write([
            item
            for item in items
            if not (
                item.get("id") == memory_id
                and (include_all or user_id is None or self._owner(item) == user_id)
            )
        ])

    def _upsert(self, user_id: str, item: dict[str, Any]) -> None:
        items = self._read_all()
        owned = [
            current
            for current in items
            if self._owner(current) == user_id and current.get("id") != item["id"]
        ]
        others = [current for current in items if self._owner(current) != user_id]
        self._write([item, *owned[: self.limit - 1], *others])

    def _write(self, items: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(items, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def list(
        self,
        user_id: str | None = None,
        *,
        include_all: bool = False,
    ) -> list[dict[str, Any]]:
        items = self._read_all()
        if include_all or user_id is None:
            return items
        return [item for item in items if self._owner(item) == user_id]

    def _read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    @staticmethod
    def _owner(item: dict[str, Any]) -> str:
        # 兼容升级前未记录所属用户的长期记忆。
        return str(item.get("user_id") or "demo_growth_ops")

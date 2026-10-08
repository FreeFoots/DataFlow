from __future__ import annotations

import hashlib
import json
from collections import deque
from typing import Any

from ..database import RELATIONS, SCHEMA, physical_table_name
from ..security import AccessScope


class SchemaGraphBuilder:
    """根据字段检索结果构建最小连通 Schema 图。"""

    def __init__(self) -> None:
        self.tables = {table["id"]: table for table in SCHEMA}

    def build(
        self,
        hits: list[dict[str, Any]],
        access_scope: AccessScope | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        scope = (
            access_scope
            if isinstance(access_scope, AccessScope)
            else AccessScope.from_dict(access_scope)
            if access_scope is not None
            else None
        )
        if scope:
            hits = [
                hit
                for hit in hits
                if scope.allows_table(str(hit.get("database_id") or ""), hit["table_id"])
            ]
        allowed_tables = set(scope.allowed_tables) if scope else set(self.tables)
        selected_tables = set(hit["table_id"] for hit in hits)
        relations = self._shortest_path_relations(selected_tables, allowed_tables)
        graph_tables = set(selected_tables)
        for relation in relations:
            graph_tables.update((relation["left_table"], relation["right_table"]))

        fields: dict[str, dict[str, Any]] = {}
        for hit in hits:
            table_definition = self.tables.get(hit["table_id"], {})
            sql_table_name = physical_table_name(table_definition) if table_definition else hit["table_id"]
            fields[hit["doc_id"]] = {
                "id": hit["doc_id"],
                "table_id": hit["table_id"],
                "table_name": sql_table_name,
                "sql_name": f"{sql_table_name}.{hit['field_name']}",
                "name": hit["field_name"],
                "label": hit["field_label"],
                "type": hit["field_type"],
                "description": hit.get("field_description", ""),
                "role": hit.get("field_role", ""),
                "source": hit.get("source", "retrieval"),
                "score": hit.get("score", 0),
            }

        # 补充最短连接路径所需的关联字段。
        for relation in relations:
            for side in ("left", "right"):
                table_id = relation[f"{side}_table"]
                field_name = relation[f"{side}_field"]
                doc_id = f"{table_id}.{field_name}"
                if doc_id in fields:
                    continue
                definition = self._field_definition(table_id, field_name)
                fields[doc_id] = {
                    "id": doc_id,
                    "table_id": table_id,
                    "table_name": physical_table_name(self.tables[table_id]),
                    "sql_name": f"{physical_table_name(self.tables[table_id])}.{field_name}",
                    "name": field_name,
                    "label": definition.get("label", field_name),
                    "type": definition.get("type", "未知"),
                    "description": definition.get("description", "关联键"),
                    "role": definition.get("role", "join_key"),
                    "source": "relation_key",
                    "score": 1.0,
                }

        tables = [
            {
                "id": table_id,
                "name": physical_table_name(self.tables[table_id]),
                "label": self.tables[table_id]["label"],
                "description": self.tables[table_id]["description"],
                "domain": self.tables[table_id].get("domain", ""),
                "database": self.tables[table_id].get("database", "short_video_ops"),
            }
            for table_id in sorted(graph_tables)
            if table_id in self.tables
        ]
        joins = [
            {
                **relation,
                "left_table_name": relation.get("left_table_name")
                or physical_table_name(self.tables[relation["left_table"]]),
                "right_table_name": relation.get("right_table_name")
                or physical_table_name(self.tables[relation["right_table"]]),
                "relation_type": relation.get("relation_type", "foreign_key"),
            }
            for relation in relations
        ]
        version_source = json.dumps(
            {"tables": tables, "fields": list(fields.values()), "joins": joins},
            ensure_ascii=False,
            sort_keys=True,
        )
        databases = sorted({table["database"] for table in tables})
        return {
            "database": databases[0] if len(databases) == 1 else None,
            "databases": databases,
            "graph_version": hashlib.sha256(version_source.encode("utf-8")).hexdigest()[:12],
            "tables": tables,
            "fields": list(fields.values()),
            "joins": joins,
        }

    @staticmethod
    def context_text(graph: dict[str, Any]) -> str:
        lines = [f"数据库: {graph.get('database') or graph.get('databases') or '未确定'} (DuckDB/CSV)"]
        for table in graph.get("tables", []):
            lines.append(f"表 {table.get('name') or table['id']}（{table['label']}）：{table['description']}")
            for field in graph.get("fields", []):
                if field["table_id"] == table["id"]:
                    lines.append(
                        f"  - {field.get('sql_name') or field['id']} | {field['label']} | {field['type']} | {field.get('description', '')}"
                    )
        for join in graph.get("joins", []):
            lines.append(
                f"关联: {join.get('left_table_name') or join['left_table']}.{join['left_field']} = "
                f"{join.get('right_table_name') or join['right_table']}.{join['right_field']} | {join.get('description', '')}"
            )
        return "\n".join(lines)

    def _shortest_path_relations(
        self,
        selected_tables: set[str],
        allowed_tables: set[str],
    ) -> list[dict[str, Any]]:
        if len(selected_tables) < 2:
            return []
        ordered = sorted(selected_tables)
        chosen: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        anchor = ordered[0]
        for target in ordered[1:]:
            for relation in self._bfs(anchor, target, allowed_tables):
                key = (
                    relation["left_table"], relation["left_field"],
                    relation["right_table"], relation["right_field"],
                )
                chosen[key] = relation
        return list(chosen.values())

    @staticmethod
    def _bfs(
        start: str,
        target: str,
        allowed_tables: set[str],
    ) -> list[dict[str, Any]]:
        queue: deque[tuple[str, list[dict[str, Any]]]] = deque([(start, [])])
        visited = {start}
        while queue:
            table, path = queue.popleft()
            if table == target:
                return path
            for relation in RELATIONS:
                if relation["left_table"] == table:
                    neighbor = relation["right_table"]
                elif relation["right_table"] == table:
                    neighbor = relation["left_table"]
                else:
                    continue
                if neighbor not in allowed_tables:
                    continue
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append((neighbor, [*path, relation]))
        return []

    def _field_definition(self, table_id: str, field_name: str) -> dict[str, Any]:
        table = self.tables.get(table_id, {})
        return next((field for field in table.get("fields", []) if field["name"] == field_name), {})

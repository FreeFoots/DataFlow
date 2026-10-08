from __future__ import annotations

import json
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb

from short_video_ops_definitions import (
    DATABASE,
    FIELD_ALIASES,
    FIELD_LABELS,
    FIELD_RETRIEVAL_CONTEXT,
    PRIMARY_KEYS,
    RELATIONS,
    ROLE_TABLES,
    TABLE_META,
)


DATABASE_DIR = Path(__file__).resolve().parents[1] / "data" / "databases" / DATABASE
MANIFEST_PATH = DATABASE_DIR / "_database_manifest.json"
SCHEMA_PATH = DATABASE_DIR / "_schema.json"

TOKEN_LABELS = {
    "id": "编号", "name": "名称", "type": "类型", "status": "状态",
    "date": "日期", "time": "时间", "at": "时间", "count": "数量",
    "rate": "比例", "score": "评分", "amount": "金额", "target": "目标",
    "user": "用户", "content": "内容", "creator": "创作者", "channel": "渠道",
    "campaign": "活动", "ad": "广告", "metric": "指标", "daily": "每日",
    "registration": "注册", "activation": "激活", "retention": "留存",
    "activity": "活跃", "device": "设备", "session": "会话", "tag": "标签",
    "exposure": "曝光", "play": "播放", "interaction": "互动", "topic": "话题",
    "budget": "预算", "spend": "消耗", "creative": "创意", "conversion": "转化",
    "impression": "曝光", "impressions": "曝光量", "click": "点击", "clicks": "点击量",
    "new": "新增", "active": "活跃", "days": "天数", "seconds": "秒数",
    "reason": "原因", "source": "来源", "platform": "平台", "level": "等级",
}


def table_id(table_name: str) -> str:
    return f"{DATABASE}.{table_name}"


def normalize(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def schema_type(source_type: str) -> str:
    upper = source_type.upper()
    if "BOOL" in upper:
        return "布尔"
    if any(token in upper for token in ("INT", "HUGEINT")):
        return "整数"
    if any(token in upper for token in ("DOUBLE", "FLOAT", "DECIMAL", "REAL")):
        return "数值"
    if any(token in upper for token in ("DATE", "TIME")):
        return "日期"
    return "文本"


def label_for(field_name: str) -> str:
    if field_name in FIELD_LABELS:
        return FIELD_LABELS[field_name]
    return "".join(TOKEN_LABELS.get(token, token.upper()) for token in field_name.split("_"))


def unit_for(field_name: str) -> str:
    if re.search(r"amount|spend|budget|cost|revenue", field_name):
        return "CNY"
    if re.search(r"rate|ratio", field_name):
        return "比例"
    if field_name.endswith("_seconds"):
        return "秒"
    if re.search(r"count|users|impressions|clicks|conversions", field_name):
        return "次/人"
    return ""


def role_for(table_name: str, field_name: str, field_type: str, foreign_fields: set[tuple[str, str]]) -> tuple[str, str]:
    if field_name in PRIMARY_KEYS[table_name]:
        return "identifier", "none"
    if (table_name, field_name) in foreign_fields:
        return "foreign_key", "none"
    if field_type == "日期":
        return "time", "group"
    if field_type in {"整数", "数值"}:
        if re.search(r"rate|ratio|score|confidence", field_name):
            return "metric", "avg"
        if re.search(r"amount|spend|budget|cost|count|users|minutes|seconds|impressions|clicks|conversions|ranking|boost|days", field_name):
            return "metric", "sum"
    if re.search(r"status|enabled|clicked|completed|retained|activated|recalled|delivered|valid", field_name):
        return "filter", "group"
    return "dimension", "group"


def relation_payload() -> list[dict[str, str]]:
    return [
        {
            "left_table": table_id(left_table),
            "left_table_name": left_table,
            "left_field": left_field,
            "right_table": table_id(right_table),
            "right_table_name": right_table,
            "right_field": right_field,
            "relation_type": "foreign_key",
            "description": description,
        }
        for left_table, left_field, right_table, right_field, description in RELATIONS
    ]


def profile_field(connection: duckdb.DuckDBPyConnection, table_name: str, field_name: str, source_type: str) -> dict[str, Any]:
    table_sql = f'"{table_name}"'
    field_sql = f'"{field_name}"'
    total, non_null, distinct = connection.execute(
        f"SELECT COUNT(*), COUNT({field_sql}), COUNT(DISTINCT {field_sql}) FROM {table_sql}"
    ).fetchone()
    # 低基数字段保留完整枚举，确保用户直接输入枚举值时能够召回字段。
    sample_limit = min(int(distinct), 20) if distinct <= 20 else 5
    sample_values = [
        normalize(row[0])
        for row in connection.execute(
            f"SELECT DISTINCT {field_sql} FROM {table_sql} "
            f"WHERE {field_sql} IS NOT NULL LIMIT {sample_limit}"
        ).fetchall()
    ]
    profile: dict[str, Any] = {
        "sample_values": sample_values,
        "row_count": total,
        "null_ratio": round((total - non_null) / total, 6) if total else 0,
        "distinct_count": distinct,
        "duplicate_ratio": round((non_null - distinct) / non_null, 6) if non_null else 0,
        "unit": unit_for(field_name),
    }
    field_type = schema_type(source_type)
    if field_type in {"整数", "数值"}:
        minimum, maximum, average = connection.execute(
            f"SELECT MIN({field_sql}), MAX({field_sql}), AVG({field_sql}) FROM {table_sql}"
        ).fetchone()
        profile.update({
            "min": normalize(minimum), "max": normalize(maximum),
            "avg": round(float(average), 4) if average is not None else None,
        })
    elif field_type == "日期":
        minimum, maximum = connection.execute(
            f"SELECT MIN({field_sql}), MAX({field_sql}) FROM {table_sql}"
        ).fetchone()
        profile.update({"min": normalize(minimum), "max": normalize(maximum), "format": "YYYY-MM-DD HH:mm:ss"})
    if distinct <= 20:
        profile["top_values"] = [
            {"value": normalize(value), "count": count}
            for value, count in connection.execute(
                f"SELECT {field_sql}, COUNT(*) AS count FROM {table_sql} "
                f"GROUP BY {field_sql} ORDER BY count DESC LIMIT 20"
            ).fetchall()
        ]
    return profile


def profile_summary(profile: dict[str, Any]) -> str:
    parts = [
        f"样例值：{'、'.join(str(value) for value in profile['sample_values']) or '无'}",
        f"空值率：{profile['null_ratio']:.2%}",
        f"不同值数量：{profile['distinct_count']}",
    ]
    if "min" in profile:
        parts.append(f"范围：{profile['min']} 至 {profile['max']}")
    if profile.get("avg") is not None:
        parts.append(f"均值：{profile['avg']}")
    if profile.get("unit"):
        parts.append(f"单位：{profile['unit']}")
    return "；".join(parts)


def generate() -> dict[str, Any]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    relations = relation_payload()
    foreign_fields = {(item["right_table_name"], item["right_field"]) for item in relations}
    relation_lookup: dict[tuple[str, str], list[dict[str, str]]] = {}
    for relation in relations:
        relation_lookup.setdefault((relation["left_table_name"], relation["left_field"]), []).append(relation)
        relation_lookup.setdefault((relation["right_table_name"], relation["right_field"]), []).append(relation)

    tables = []
    connection = duckdb.connect(":memory:")
    try:
        for table_name, manifest_table in manifest["tables"].items():
            csv_path = (DATABASE_DIR / f"{table_name}.csv").resolve().as_posix().replace("'", "''")
            connection.execute(
                f'CREATE OR REPLACE VIEW "{table_name}" AS SELECT * FROM read_csv_auto(\'{csv_path}\', header=true, sample_size=-1)'
            )
            source_types = {
                row[0]: row[1]
                for row in connection.execute(f'DESCRIBE SELECT * FROM "{table_name}"').fetchall()
            }
            table_label, domain = TABLE_META[table_name]
            fields = []
            for field_name in manifest_table["columns"]:
                label = label_for(field_name)
                field_type = schema_type(source_types[field_name])
                role, aggregation = role_for(table_name, field_name, field_type, foreign_fields)
                profile = profile_field(connection, table_name, field_name, source_types[field_name])
                related = relation_lookup.get((table_name, field_name), [])
                related_text = "、".join(item["description"] for item in related)
                description = f"{table_label}中的{label}"
                if related_text:
                    description += f"；{related_text}"
                aliases = list(dict.fromkeys([label, *FIELD_ALIASES.get(field_name, [])]))
                retrieval_context = FIELD_RETRIEVAL_CONTEXT.get(field_name, [])
                retrieval_context_text = "、".join(retrieval_context)
                summary = profile_summary(profile)
                keyword_text = " ".join([
                    DATABASE, table_name, table_label, domain, field_name, label,
                    description, *aliases, *retrieval_context,
                    *(str(value) for value in profile["sample_values"]),
                ])
                vector_text = (
                    f"字段名：{field_name}；字段语义：{description}；所属表：{table_name}（{table_label}）；"
                    f"业务域：{domain}；数据类型：{field_type}；业务表达：{'、'.join(aliases)}；"
                    f"计算关联：{retrieval_context_text or '无'}；{summary}。"
                )
                rerank_text = (
                    f"数据库：{DATABASE}；表：{table_name}（{table_label}）；表用途：{table_label}相关查询与分析；"
                    f"字段：{field_name}（{label}）；字段含义：{description}；原始类型：{source_types[field_name]}；"
                    f"字段角色：{role}；默认聚合：{aggregation}；同义词：{'、'.join(aliases)}；"
                    f"计算关联：{retrieval_context_text or '无'}；{summary}；"
                    f"关联信息：{related_text or '无'}。"
                )
                fields.append({
                    "name": field_name, "label": label, "type": field_type,
                    "source_type": source_types[field_name], "description": description,
                    "aliases": aliases, "role": role, "aggregation": aggregation,
                    "retrieval_context": retrieval_context,
                    "data_profile": profile,
                    "index_content": {
                        "keyword_text": keyword_text,
                        "vector_text": vector_text,
                        "rerank_text": rerank_text,
                    },
                })
            tables.append({
                "id": table_id(table_name), "name": table_name, "label": table_label,
                "database": DATABASE, "domain": domain,
                "description": f"{table_label}，用于{domain}相关的查询、统计和分析。",
                "business_terms": [table_label, domain, table_name.replace("_", " ")],
                "primary_key": PRIMARY_KEYS[table_name],
                "row_count": manifest_table["row_count"], "fields": fields,
            })
    finally:
        connection.close()

    payload = {
        "schema_version": "1.0.1",
        "database": DATABASE,
        "scenario": manifest["scenario"],
        "schema_layers": {
            "table_level": "表名、业务含义、用途、所属数据库和主键",
            "column_level": "字段名、数据类型、字段语义、别名、角色和聚合方式",
            "data_level": "样例值、数据范围、均值、枚举分布、空值率和重复率",
            "integrated_field_schema": "字段级Schema融合表级和数据级上下文，并生成三级索引文本",
        },
        "role_tables": ROLE_TABLES,
        "tables": tables,
        "relations": relations,
    }
    SCHEMA_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


if __name__ == "__main__":
    schema = generate()
    print(json.dumps({
        "database": DATABASE,
        "tables": len(schema["tables"]),
        "fields": sum(len(table["fields"]) for table in schema["tables"]),
        "relations": len(schema["relations"]),
        "path": str(SCHEMA_PATH),
    }, ensure_ascii=False))

"""Shared references and checked facts for query, analysis and result QA."""
from __future__ import annotations

import math
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

class FactReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str
    field: str = Field(description="数值指标列（如new_users、activation_rate）；渠道名称等文本维度只能放where，不能作为field")
    section: Literal["rows", "summary"] = "rows"
    where: dict[str, str | int | float | None] = Field(default_factory=dict, max_length=6)


class EvidenceClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=1200, pattern=r"^[^0-9]+$",
                      description="不含任何阿拉伯数字的小标题，如新增变化、队列激活率对比；日期、观察天数和数值由事实来源展示")
    evidence_ids: list[str] = Field(min_length=1, max_length=12)
    facts: list[FactReference] = Field(min_length=1, max_length=12)


def resolve_fact(fact: FactReference, artifacts: dict) -> dict:
    artifact = artifacts[fact.source_id]
    if not artifact.get("semantic_verified") or not artifact["complete"] or artifact.get("limited"):
        raise ValueError("事实需要完整的受控指标结果，探索性或预览结果只能作为线索")
    if fact.section == "summary":
        if fact.where:
            raise ValueError("整体摘要不可指定行筛选")
        row = artifact.get("summary", {})
    else:
        if not set(fact.where).issubset(artifact["columns"]):
            raise ValueError("事实筛选列不存在")
        rows = [row for row in artifact["rows"] if all(row.get(k) == v for k, v in fact.where.items())]
        if len(rows) != 1:
            raise ValueError("事实引用必须精确定位一行；整体值请先用工具汇总")
        row = rows[0]
    if fact.field not in row:
        raise ValueError("事实引用字段不存在")
    value = row[fact.field]
    if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
        raise ValueError(f"事实字段{fact.field}是文本或非有限数值；field只能引用数值指标，渠道名称等文本维度请放where定位该行")
    ratio = fact.field in artifact.get("ratio_fields", []) or fact.field == "change_rate"
    formatted = "不可计算" if value is None else f"{value * 100:.2f}个百分点" if ratio and fact.field == "delta" else f"{value * 100:.2f}%" if ratio else f"{value:,.4f}".rstrip("0").rstrip(".")
    category = "、".join(f"{k}={v}" for k, v in fact.where.items()) or "整体"
    labels = {"new_users": "新增注册用户", "activated_users": "队列激活人数", "activation_rate": "队列激活率",
              "baseline": "基期", "current": "本期", "delta": "变化量", "change_rate": "相对变化率",
              "contribution_rate": "净变化贡献率", "value": "汇总值"}
    return {**fact.model_dump(), "value": value, "unit": "比例" if ratio else artifact["unit"],
            "display": f"{artifact.get('title', '查询结果')} · {category} · {labels.get(fact.field, fact.field)} {formatted}"}


def checked_claims(raw: list[dict], sources: list[dict]) -> list[dict]:
    artifacts = {item["result_id"]: item for item in sources}
    claims = []
    for item in raw:
        claim = EvidenceClaim.model_validate(item)
        if not set(claim.evidence_ids).issubset(artifacts):
            raise ValueError("结论引用了不存在的结果")
        if not {fact.source_id for fact in claim.facts}.issubset(claim.evidence_ids):
            raise ValueError("事实来源必须包含在该结论的evidence_ids中")
        if any(word in claim.text for word in ("导致", "造成", "原因是", "因为", "由于")):
            raise ValueError("数据变化不能作为已验证因果结论")
        claims.append({**claim.model_dump(), "facts": [resolve_fact(fact, artifacts) for fact in claim.facts]})
    versions = {artifacts[fact["source_id"]]["data_version"] for claim in claims for fact in claim["facts"]}
    if len(versions) > 1:
        raise ValueError("引用结果来自不同数据版本，不能合并为同一统计结论")
    return claims

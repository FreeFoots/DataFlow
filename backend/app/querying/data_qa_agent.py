from __future__ import annotations

import json
from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import Any, Callable
from pydantic import BaseModel, ConfigDict, Field

from ..errors import PipelineStageError
from ..mcp_runtime import LocalMcpClient
from ..model_client import ModelClient
from ..models import AnalysisReport, VisualizationSpec
from ..skills import SkillDefinition
from .result_assets import EvidenceClaim, checked_claims
from .analysis_report import build_analysis_report


class QaGap(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requirement: str = Field(min_length=1, max_length=500, description="从已确认query中逐字引用尚未满足的要求")
    reason: str = Field(min_length=1, max_length=500)


@dataclass
class DataQaResult:
    action: str
    answer: str
    report: AnalysisReport | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    claims: list[dict[str, Any]] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class DataQaAgent:
    """分析已有数据，并按需生成Markdown报告和图表配置。"""

    def __init__(
        self,
        model_client: ModelClient,
        mcp_client_factory: Callable[[dict[str, Any]], LocalMcpClient],
        skill: SkillDefinition,
    ) -> None:
        self.model_client = model_client
        self.mcp_client_factory = mcp_client_factory
        self.skill = skill

    def run(
        self,
        query: str,
        contexts: dict[str, str],
        access_scope: dict[str, Any],
        artifacts: list[dict[str, Any]] | None = None,
    ) -> DataQaResult:
        client = self.mcp_client_factory(access_scope)
        tools = [
            tool for tool in client.list_tools()
            if self._tool_allowed(str(tool.get("name") or ""))
        ]
        source_catalog = self._source_catalog(contexts)
        artifacts = artifacts or []
        if artifacts:
            source_catalog = {}
        for artifact in artifacts:
            source_catalog[artifact["result_id"]] = {"task_id": artifact["result_id"], "title": artifact["title"],
                                                  "columns": artifact["columns"], "row_count": len(artifact["rows"])}
        system = (
            "你是数据问答智能体。严格执行下面的Skill，并只返回JSON。\n\n"
            f"{self.skill.instructions}\n\n"
            "可用前端展示工具：\n"
            f"{json.dumps(tools, ensure_ascii=False)}\n\n"
            "输出格式：\n"
            "普通回答：{\"action\":\"answer\",\"answer\":\"...\",\"tool_calls\":[]}\n"
            "分析报告：{\"action\":\"report\",\"answer\":\"一句话摘要\","
            "\"title\":\"...\",\"markdown\":\"...\","
            "\"tool_calls\":[{\"name\":\"build_bar_chart\",\"arguments\":{...}}]}"
        )
        if artifacts:
            system += (
                "\n已提供服务端结果资产。数值结论必须输出claims，格式遵守claim_schema；"
                "facts引用result_id、field、section(rows或summary)、where，where必须定位唯一行。"
                "text仅为不含数字的小标题；数值、日期和窗口由程序从实际结果渲染。"
                "不得自行计算两期差值或编造因果；缺少用户要求的计算证据时输出gaps。"
                "answer/markdown不会作为数值事实发布。图表优先引用资产result_id。"
                "没有可引用数值时claims=[]并说明缺口，不能以成功总结替代证据。"
                "每条claim的evidence_ids必须包含所有facts的source_id；不能在fact中填value或display。"
                "field只能填数值指标列，绝不能填channel_name、channel_id等文本维度。"
                "例如渠道排名事实使用field=new_users、section=rows、where={channel_name: 实际渠道名称}，"
                "程序会从where展示渠道名称，无需另加channel_name事实。"
                "受控资产输出只需action、title、claims、tool_calls、gaps、notes，无需再写answer/markdown。"
                "gaps只记录未满足的用户明确要求，每项为{requirement: 从query逐字引用该要求, reason: 缺失的数据或计算}。"
                "已满足要求时gaps=[]；用户未要求的占比/集中度等未做分析不属于缺口。"
                "演示定义、统计范围等适用说明放notes，不放gaps；不再输出limitations。"
            )
        user = json.dumps(
            {
                "query": query,
                "available_data_sources": list(source_catalog.values()),
                "context": contexts,
                "result_artifacts": [{**{key: value for key, value in item.items() if key not in {"rows", "sql"}},
                                      "rows": item["rows"][:12], "row_count": len(item["rows"]),
                                      "sample_complete": len(item["rows"]) <= 12} for item in artifacts],
                "claim_schema": EvidenceClaim.model_json_schema() if artifacts else None,
                "gap_schema": QaGap.model_json_schema() if artifacts else None,
            },
            ensure_ascii=False,
        )
        try:
            for attempt in range(2):
                payload = self.model_client.chat_json(system, user)
                if artifacts:
                    try:
                        self._checked_evidence(payload, artifacts, query)
                    except (ValueError, KeyError, TypeError) as exc:
                        if attempt:
                            raise
                        retry = json.loads(user)
                        retry.update(output_validation_error=str(exc), previous_invalid_output=payload,
                                     output_validation_instruction="修正事实引用或缺口格式。未要求的分析不列缺口；范围说明放notes。保持原需求与数据，返回完整对象。")
                        user = json.dumps(retry, ensure_ascii=False)
                        continue
                return self._execute(payload, client, source_catalog, artifacts, query)
        except PipelineStageError:
            raise
        except (RuntimeError, ValueError, KeyError, TypeError) as exc:
            raise PipelineStageError("qa_answer", str(exc)) from exc

    def _execute(
        self,
        payload: dict[str, Any],
        client: LocalMcpClient,
        source_catalog: dict[str, dict[str, Any]],
        artifacts: list[dict] | None = None,
        query: str = "",
    ) -> DataQaResult:
        action = str(payload.get("action") or "")
        if action not in self.skill.output_actions:
            raise ValueError(f"问答Skill不支持输出动作：{action or '空'}")

        answer = str(payload.get("answer") or "").strip()
        raw_calls = payload.get("tool_calls") or []
        if not isinstance(raw_calls, list):
            raise ValueError("tool_calls必须是数组")
        artifacts = artifacts or []
        claims, limitations, notes = [], [], []
        if artifacts:
            claims, limitations, notes = self._checked_evidence(payload, artifacts, query)
            if not claims:
                limitations = [*limitations, "现有数据不足以形成所需结论，请补充查询范围或数据。"]

        if action == "answer":
            if raw_calls:
                raise ValueError("普通回答不能调用前端展示工具")
            if artifacts:
                rendered = build_analysis_report(str(payload.get("title") or "结果解读"), claims, artifacts, limitations=limitations, notes=notes)
                return DataQaResult(action="answer", answer=rendered.markdown, artifacts=artifacts,
                                    claims=claims, limitations=limitations, notes=notes)
            if not answer:
                raise ValueError("普通回答缺少answer")
            return DataQaResult(action="answer", answer=answer)

        if len(raw_calls) > self.skill.max_tool_calls:
            raise ValueError(f"报告最多调用{self.skill.max_tool_calls}个展示工具")

        title = str(payload.get("title") or "").strip()
        markdown = str(payload.get("markdown") or "").strip()
        if not title or (not markdown and not artifacts):
            raise ValueError("分析报告缺少title或markdown")

        traces: list[dict[str, Any]] = []
        visualizations: list[VisualizationSpec] = []
        for index, raw_call in enumerate(raw_calls, start=1):
            if not isinstance(raw_call, dict):
                raise ValueError("展示工具调用必须是对象")
            name = str(raw_call.get("name") or "")
            arguments = raw_call.get("arguments") or {}
            if not self._tool_allowed(name) or not isinstance(arguments, dict):
                raise ValueError(f"展示工具调用不合法：{name or '空'}")
            self._validate_chart_source(arguments, source_catalog)
            result = client.call_tool(name, arguments)
            visualization = VisualizationSpec.model_validate(result)
            visualizations.append(visualization)
            traces.append({
                "call_index": index,
                "tool": name,
                "arguments": arguments,
                "result": visualization.model_dump(mode="json"),
            })

        report = build_analysis_report(title, claims, artifacts, limitations=limitations, charts=visualizations, notes=notes) if artifacts else AnalysisReport(
            title=title,
            markdown=markdown,
            visualizations=visualizations,
        )
        return DataQaResult(
            action="report",
            answer=answer or title,
            report=report,
            tool_calls=traces,
            artifacts=artifacts, claims=claims, limitations=limitations, notes=notes,
        )

    @staticmethod
    def _checked_evidence(payload: dict, artifacts: list[dict], query: str):
        if not isinstance(payload.get("claims"), list) or len(payload["claims"]) > 12:
            raise ValueError("已有结果解读需要带来源的claims")
        claims = checked_claims(payload["claims"], artifacts)
        if payload.get("limitations"):
            raise ValueError("请改用gaps逐字引用用户未满足的要求；演示口径与未要求的分析放notes")
        raw_gaps = payload.get("gaps") or []
        if not isinstance(raw_gaps, list) or len(raw_gaps) > 12:
            raise ValueError("gaps必须为至多十二项的数组")
        gaps = [QaGap.model_validate(item) for item in raw_gaps]
        if any(gap.requirement not in query for gap in gaps):
            raise ValueError("缺口必须逐字引用query中尚未满足的用户要求")
        notes = payload.get("notes") or []
        if not isinstance(notes, list) or any(not isinstance(item, str) for item in notes):
            raise ValueError("notes必须为说明文字数组")
        return claims, [f"{gap.requirement}：{gap.reason}" for gap in gaps], notes

    def _tool_allowed(self, name: str) -> bool:
        return any(fnmatch(name, pattern) for pattern in self.skill.allowed_tools)

    @staticmethod
    def _source_catalog(contexts: dict[str, str]) -> dict[str, dict[str, Any]]:
        catalog: dict[str, dict[str, Any]] = {}
        for key in ("recent_result", "selected_tables"):
            raw = contexts.get(key) or ""
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                continue
            items = parsed if isinstance(parsed, list) else [parsed]
            for item in items:
                if not isinstance(item, dict) or not item.get("task_id"):
                    continue
                task_id = str(item["task_id"])
                catalog[task_id] = {
                    "task_id": task_id,
                    "title": str(item.get("title") or "查询结果"),
                    "query": str(item.get("query") or ""),
                    "columns": [str(column) for column in item.get("columns") or []],
                    "row_count": int(item.get("row_count") or len(item.get("rows") or [])),
                }
        return catalog

    @staticmethod
    def _validate_chart_source(
        arguments: dict[str, Any],
        source_catalog: dict[str, dict[str, Any]],
    ) -> None:
        task_id = str(arguments.get("source_task_id") or "")
        source = source_catalog.get(task_id)
        if not source:
            raise ValueError(f"图表引用了不可用的数据来源：{task_id or '空'}")
        columns = set(source["columns"])
        category = str(arguments.get("category_field") or "")
        value = str(arguments.get("value_field") or "")
        missing = [field for field in (category, value) if field not in columns]
        if missing:
            raise ValueError(f"图表字段不在查询结果中：{', '.join(missing)}")

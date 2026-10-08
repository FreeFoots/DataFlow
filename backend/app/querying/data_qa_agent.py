from __future__ import annotations

import json
from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import Any, Callable

from ..errors import PipelineStageError
from ..mcp_runtime import LocalMcpClient
from ..model_client import ModelClient
from ..models import AnalysisReport, VisualizationSpec
from ..skills import SkillDefinition


@dataclass
class DataQaResult:
    action: str
    answer: str
    report: AnalysisReport | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


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
    ) -> DataQaResult:
        client = self.mcp_client_factory(access_scope)
        tools = [
            tool for tool in client.list_tools()
            if self._tool_allowed(str(tool.get("name") or ""))
        ]
        source_catalog = self._source_catalog(contexts)
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
        user = json.dumps(
            {
                "query": query,
                "available_data_sources": list(source_catalog.values()),
                "context": contexts,
            },
            ensure_ascii=False,
        )
        try:
            payload = self.model_client.chat_json(system, user)
            return self._execute(payload, client, source_catalog)
        except PipelineStageError:
            raise
        except (RuntimeError, ValueError, KeyError, TypeError) as exc:
            raise PipelineStageError("qa_answer", str(exc)) from exc

    def _execute(
        self,
        payload: dict[str, Any],
        client: LocalMcpClient,
        source_catalog: dict[str, dict[str, Any]],
    ) -> DataQaResult:
        action = str(payload.get("action") or "")
        if action not in self.skill.output_actions:
            raise ValueError(f"问答Skill不支持输出动作：{action or '空'}")

        answer = str(payload.get("answer") or "").strip()
        raw_calls = payload.get("tool_calls") or []
        if not isinstance(raw_calls, list):
            raise ValueError("tool_calls必须是数组")

        if action == "answer":
            if raw_calls:
                raise ValueError("普通回答不能调用前端展示工具")
            if not answer:
                raise ValueError("普通回答缺少answer")
            return DataQaResult(action="answer", answer=answer)

        if len(raw_calls) > self.skill.max_tool_calls:
            raise ValueError(f"报告最多调用{self.skill.max_tool_calls}个展示工具")

        title = str(payload.get("title") or "").strip()
        markdown = str(payload.get("markdown") or "").strip()
        if not title or not markdown:
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

        report = AnalysisReport(
            title=title,
            markdown=markdown,
            visualizations=visualizations,
        )
        return DataQaResult(
            action="report",
            answer=answer or title,
            report=report,
            tool_calls=traces,
        )

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

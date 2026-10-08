from __future__ import annotations

import json
import re
from fnmatch import fnmatch
from typing import Any, Callable

from ..errors import PipelineStageError
from ..mcp_runtime.client import LocalMcpClient
from ..model_client import ModelClient
from ..skills import SkillDefinition


class SingleDatabaseAgent:
    """让模型通过MCP工具完成单数据库查询。"""

    def __init__(
        self,
        model_client: ModelClient,
        mcp_client_factory: Callable[[dict[str, Any]], LocalMcpClient],
        skill: SkillDefinition,
        max_tool_calls: int = 3,
    ) -> None:
        self.model_client = model_client
        self.mcp_client_factory = mcp_client_factory
        self.skill = skill
        self.max_tool_calls = min(max_tool_calls, skill.max_tool_calls)

    def prepare(
        self,
        query: str,
        database: str,
        schema_graph: dict[str, Any],
        schema_context: str,
        retrieval: dict[str, Any],
        workspace: dict[str, Any],
        access_scope: dict[str, Any],
    ) -> dict[str, Any]:
        database_tool = f"query_{database}"
        mcp_client = self.mcp_client_factory(access_scope)

        # 每次请求都通过MCP tools/list获取当前工具定义。
        catalog = mcp_client.list_tools()
        tools = [
            tool
            for tool in catalog
            if any(fnmatch(tool["name"], pattern) for pattern in self.skill.allowed_tools)
            and (not tool["name"].startswith("query_") or tool["name"] == database_tool)
        ]
        tool_names = {tool["name"] for tool in tools}
        if database_tool not in tool_names:
            raise PipelineStageError(
                "mcp_tool_discovery",
                f"没有找到数据库工具：{database_tool}",
            )

        system = f"你是单数据库问数智能体。\n\n{self.skill.instructions}"

        observations: list[dict[str, Any]] = []
        tool_trace: list[dict[str, Any]] = []
        base_payload = {
            "query": query,
            "database": database,
            "schema_graph": schema_graph,
            "retrieval": {
                "threshold": retrieval.get("threshold"),
                "selected_fields": retrieval.get("hits", []),
                "low_confidence_candidates": retrieval.get("low_confidence_candidates", []),
            },
            "confirmed_fields": workspace.get("schema_fields", []),
            "confirmed_parameters": workspace.get("confirmed_parameters", {}),
            "schema_text": schema_context,
            "mcp_tools": tools,
        }

        try:
            for call_index in range(1, self.max_tool_calls + 1):
                payload = {**base_payload, "tool_results": observations}
                decision = self._validated_decision(system, payload, tool_names)
                action = str(decision["action"])

                if action == "clarify":
                    clarification = decision.get("clarification")
                    if not isinstance(clarification, dict) or len(
                        clarification.get("options") or []
                    ) < 2:
                        raise ValueError("智能体返回的澄清信息不完整")
                    return {
                        "action": "clarify",
                        "clarification": clarification,
                        "tool_trace": tool_trace,
                    }

                if action != "call_tool":
                    raise ValueError("智能体必须返回call_tool或clarify")

                tool_name = str(decision["tool_name"])
                arguments = decision["arguments"]

                tool_result = mcp_client.call_tool(tool_name, arguments)
                trace = {
                    "call_index": call_index,
                    "tool": tool_name,
                    "arguments": arguments,
                    "query_contract": decision.get("query_contract", {}),
                    "result": tool_result,
                    "reason": str(decision.get("reason") or ""),
                }
                tool_trace.append(trace)

                if tool_name == database_tool:
                    if not bool(tool_result.get("success")) and call_index < self.max_tool_calls:
                        observations.append({
                            "tool": tool_name,
                            "result": tool_result,
                            "instruction": (
                                "上一条SQL执行失败。只根据数据库错误、原问题和Schema修正SQL，"
                                "不得改变查询口径，然后重新调用同一个数据库工具。"
                            ),
                        })
                        continue
                    return {
                        "action": "executed",
                        "execution": tool_result,
                        "tool_trace": tool_trace,
                        "source": "model_mcp",
                    }

                observations.append({"tool": tool_name, "result": tool_result})

            raise ValueError(f"MCP工具调用超过上限：{self.max_tool_calls}")
        except PipelineStageError:
            raise
        except (RuntimeError, KeyError, TypeError, ValueError) as exc:
            raise PipelineStageError("single_database_agent", str(exc)) from exc

    def _validated_decision(
        self,
        system: str,
        payload: dict[str, Any],
        tool_names: set[str],
    ) -> dict[str, Any]:
        """结构化结果不完整时让模型最多修正两次，不执行不完整的工具调用。"""
        validation_error = ""
        previous_decision: dict[str, Any] | None = None
        for attempt in range(3):
            request_payload = dict(payload)
            if validation_error:
                request_payload["previous_invalid_output"] = previous_decision
                request_payload["output_validation_error"] = validation_error
                request_payload["output_validation_instruction"] = (
                    "上一次输出未通过调用前校验。根据错误修正JSON字段或SQL，"
                    "保持原查询口径并重新输出完整对象。"
                )
            decision = self.model_client.chat_json(
                system,
                json.dumps(request_payload, ensure_ascii=False),
            )
            previous_decision = decision
            validation_error = self._decision_error(decision, tool_names)
            if not validation_error:
                validation_error = self._explicit_filter_error(decision, payload)
            if not validation_error:
                return decision
            if attempt == 2:
                raise ValueError(validation_error)
        raise ValueError(validation_error)

    def _decision_error(self, decision: dict[str, Any], tool_names: set[str]) -> str:
        action = str(decision.get("action") or "")
        if action not in self.skill.output_actions:
            return f"Skill不允许输出动作：{action}"
        if action == "clarify":
            clarification = decision.get("clarification")
            if not isinstance(clarification, dict) or len(clarification.get("options") or []) < 2:
                return "智能体返回的澄清信息不完整"
            return ""
        if action != "call_tool":
            return "智能体必须返回call_tool或clarify"
        tool_name = str(decision.get("tool_name") or "")
        if tool_name not in tool_names:
            return f"智能体选择了未提供的MCP工具：{tool_name}"
        if not isinstance(decision.get("arguments"), dict):
            return "MCP工具参数必须是JSON对象"
        return ""

    @staticmethod
    def _explicit_filter_error(
        decision: dict[str, Any],
        payload: dict[str, Any],
    ) -> str:
        """用户明确写出Schema样例值时，SQL必须使用该值。"""
        if decision.get("action") != "call_tool":
            return ""
        if not str(decision.get("tool_name") or "").startswith("query_"):
            return ""
        sql = str((decision.get("arguments") or {}).get("sql") or "")
        if not sql:
            return "数据库工具调用缺少SQL参数"
        query = str(payload.get("query") or "")
        selected_fields = (
            (payload.get("retrieval") or {}).get("selected_fields") or []
        )
        matched_samples: list[tuple[str, str, str, bool]] = []
        for field in selected_fields:
            field_identity = " ".join(
                str(field.get(key) or "")
                for key in ("field_name", "field_label", "field_type")
            ).casefold()
            if any(
                token in field_identity
                for token in ("date", "time", "month", "year", "日期", "时间", "月份", "年度")
            ):
                continue
            samples = list(field.get("samples") or [])
            if len(samples) > 20:
                continue
            for sample in samples:
                value = str(sample).strip()
                if len(value) < 2:
                    continue
                if isinstance(sample, (int, float)) and not isinstance(sample, bool):
                    query_pattern = rf"(?<![\d.]){re.escape(value)}(?![\d.])"
                    if not re.search(query_pattern, query):
                        continue
                    pattern = rf"(?<![\w.]){re.escape(value)}(?![\w.])"
                else:
                    if value.casefold() not in query.casefold():
                        continue
                    escaped = re.escape(value.replace("'", "''"))
                    pattern = rf"'\s*{escaped}\s*'"
                matched_samples.append(
                    (
                        str(field.get("field_name") or ""),
                        value,
                        pattern,
                        not isinstance(sample, (int, float)) or isinstance(sample, bool),
                    )
                )

        # 同一段文本可能同时命中“侵权”和“侵权搬运”等重叠枚举，
        # 此时只校验信息更完整的最长值，避免把正确SQL误判为缺少短枚举。
        precise_samples: list[tuple[str, str, str, bool]] = []
        for candidate in matched_samples:
            _, value, _, is_text = candidate
            if is_text and any(
                other_is_text
                and len(other_value) > len(value)
                and value.casefold() in other_value.casefold()
                for _, other_value, _, other_is_text in matched_samples
            ):
                continue
            precise_samples.append(candidate)

        missing = [
            f"{field_name}={value}"
            for field_name, value, pattern, _ in precise_samples
            if not re.search(pattern, sql, flags=re.IGNORECASE)
        ]
        if missing:
            return "SQL遗漏用户明确给出的Schema取值：" + "、".join(dict.fromkeys(missing))
        return ""

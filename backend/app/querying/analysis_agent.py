from __future__ import annotations

import copy
import hashlib
import json
import re
import time
from dataclasses import asdict
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..config import Settings
from ..database import SCHEMA, physical_table_name
from ..model_client import ModelClient
from ..models import Clarification, QueryResult
from ..security import AccessScope
from .analysis_tools import AnalysisTools
from .analysis_report import build_analysis_report
from .result_assets import FactReference, EvidenceClaim, resolve_fact
from ..runtime.execution import CURRENT, durable_call


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=40, pattern=r"^[a-zA-Z0-9_-]+$")
    title: str = Field(min_length=1, max_length=160)
    depends_on: list[str] = Field(default_factory=list, max_length=8)


class Hypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    statement: str = Field(min_length=1, max_length=500)
    status: Literal["unverified"] = "unverified"
    evidence_ids: list[str] = Field(default_factory=list, max_length=12)


class AnalysisDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["plan", "call_tool", "clarify", "finish"]
    reason: str = Field(default="", max_length=500)
    plan: list[PlanStep] | None = Field(default=None, max_length=8)
    step_id: str | None = None
    tool_name: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    complete_step: bool = True
    clarification: Clarification | None = None
    title: str = Field(default="分析结果", max_length=120)
    claims: list[EvidenceClaim] = Field(default_factory=list, max_length=12)
    limitations: list[str] = Field(default_factory=list, max_length=12, description="尚未回答的用户要求、缺失证据或执行失败，会产生部分完成状态")
    notes: list[str] = Field(default_factory=list, max_length=12, description="已采用的口径、统计解释与适用范围说明，不代表任务未完成")
    primary_result_id: str | None = None
    hypotheses: list[Hypothesis] | None = Field(default=None, max_length=8)


class AnalysisAgent:
    """One decision or tool execution per graph node, so pauses retain all evidence."""

    def __init__(self, model: ModelClient, tools: AnalysisTools, config: Settings, instructions: str):
        self.model = model
        self.tools = tools
        self.config = config
        self.instructions = instructions

    def initialize(self, query: str, original_query: str | None = None) -> dict[str, Any]:
        return {
            "goal": query, "original_query": original_query or query, "plan": [], "artifacts": [], "observations": [],
            "charts": [], "claims": [], "hypotheses": [], "limitations": [], "notes": [], "discovered_tables": [],
            "clarification_answers": {}, "cache": {}, "trace": [],
            "budget": {"decisions": 0, "tool_calls": 0, "elapsed_seconds": 0.0, "no_progress": 0},
            "next": "decide", "stop_reason": None, "plan_version": 0, "action_sequence": 0,
        }

    def decide(self, task_id: str, raw: dict, access: dict, workspace: dict) -> dict:
        state = copy.deepcopy(raw)
        if self._exhausted(state):
            return state
        scope = AccessScope.from_dict(access)
        payload = {
            "goal": state["goal"], "original_query": state["original_query"], "plan": state["plan"], "plan_version": state["plan_version"],
            "ready_step_ids": self._ready_steps(state),
            "hypotheses": state["hypotheses"],
            "clarification_answers": state["clarification_answers"],
            "confirmed_parameters": workspace.get("confirmed_parameters", {}),
            "confirmed_fields": workspace.get("schema_fields", []),
            "available_tables": [{"name": physical_table_name(t), "label": t["label"], "description": t["description"]} for t in SCHEMA if scope.allows_table(t.get("database", "short_video_ops"), t["id"])],
            "tools": self.tools.definitions(),
            "result_catalog": [self._summary(a) for a in state["artifacts"]],
            "observations": state["observations"][-8:], "budget_used": state["budget"],
            "decision_schema": AnalysisDecision.model_json_schema(),
        }
        started = time.monotonic()
        try:
            raw_decision = self.model.chat_json(self.instructions, json.dumps(payload, ensure_ascii=False))
            self._runtime_check()
            decision = AnalysisDecision.model_validate(raw_decision)
            if decision.hypotheses is not None:
                known_ids = {a["result_id"] for a in state["artifacts"]}
                for hypothesis in decision.hypotheses:
                    if not set(hypothesis.evidence_ids).issubset(known_ids) or hypothesis.status != "unverified" and not hypothesis.evidence_ids:
                        raise ValueError("已支持或反驳的假设必须引用本任务结果")
                state["hypotheses"] = [h.model_dump() for h in decision.hypotheses]
            if decision.action == "plan":
                self._update_plan(state, decision)
                state["next"] = "decide"
            elif decision.action == "call_tool":
                self._validate_call(state, decision)
                state["pending_call"] = decision.model_dump(mode="json")
                state["next"] = "tool"
            elif decision.action == "clarify":
                clarification = decision.clarification
                if not clarification or len(clarification.options) < 2 or len(clarification.options) > 6:
                    raise ValueError("澄清必须提供2至6个选项")
                if len({o.id for o in clarification.options}) != len(clarification.options):
                    raise ValueError("澄清选项ID不可重复")
                state["clarification"] = clarification.model_dump(mode="json")
                state["next"] = "clarify"
            else:
                self._finish(state, decision)
        except (ValueError, TypeError, KeyError, RuntimeError) as exc:
            self._error(state, "decision_validation", str(exc))
        finally:
            self._runtime_check()
            state["budget"]["decisions"] += 1
            state["budget"]["elapsed_seconds"] += time.monotonic() - started
        if state["next"] not in {"final", "clarify"}:
            self._exhausted(state)
        return state

    def execute(self, task_id: str, raw: dict, access: dict) -> dict:
        state = copy.deepcopy(raw)
        if self._exhausted(state):
            return state
        call = AnalysisDecision.model_validate(state.pop("pending_call"))
        fingerprint = hashlib.sha256(json.dumps({"tool": call.tool_name, "arguments": call.arguments}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if fingerprint in state["cache"]:
            observation = {**state["cache"][fingerprint], "cached": True}
            observation["step_id"] = call.step_id
            state["observations"].append(observation)
            if call.complete_step:
                # A later decision can close a multi-call step using its saved response.
                step = next(s for s in state["plan"] if s["id"] == call.step_id)
                step["status"] = "completed"
                source_id = observation.get("result", {}).get("result_id") or call.arguments.get("source_id")
                if source_id and source_id not in step["evidence_ids"]:
                    step["evidence_ids"].append(source_id)
                state["budget"]["no_progress"] = 0
                observation.update(step_status="completed", ready_step_ids=self._ready_steps(state),
                                   message="成功响应已复用，步骤已完成；请继续就绪步骤。")
                state["trace"].append({"stage": "analysis_cache_reused", "step_id": call.step_id,
                                       "tool": call.tool_name, "result_id": source_id, "reason": call.reason})
            else:
                state["budget"]["no_progress"] += 1
            state["next"] = "decide"
            self._exhausted(state)
            return state
        started = time.monotonic()
        state["budget"]["tool_calls"] += 1
        state["action_sequence"] += 1
        try:
            action_id = f"{state['plan_version']}:{state['action_sequence']}:{call.step_id}"
            response = durable_call("analysis_tool", {"action_id": action_id, "tool": call.tool_name, "arguments": call.arguments},
                                    lambda: asdict(self.tools.execute(call.tool_name, call.arguments, state, access)))
            from .analysis_tools import ToolOutput
            output = ToolOutput(**response)
            self._runtime_check()
            observation = {"tool": call.tool_name, "step_id": call.step_id, **output.observation}
            if output.artifact:
                artifact = {**output.artifact, "result_id": f"{task_id}:r{len(state['artifacts']) + 1}", "step_id": call.step_id}
                state["artifacts"].append(artifact)
                observation["result"] = self._summary(artifact)
            if output.chart:
                state["charts"].append(output.chart)
            state["discovered_tables"] = sorted(set(state["discovered_tables"]) | set(output.discovered_tables))
            state["observations"].append(observation)
            success = observation.get("success") is True
            state["trace"].append({"call_index": state["budget"]["tool_calls"], "tool": call.tool_name, "step_id": call.step_id, "arguments": call.arguments, "success": success, "result_id": output.artifact and artifact["result_id"], "error": observation.get("error"), "reason": call.reason})
            if success:
                state["cache"][fingerprint] = observation
                state["budget"]["no_progress"] = 0
                step = next(s for s in state["plan"] if s["id"] == call.step_id)
                if output.artifact:
                    step["evidence_ids"] = [*step["evidence_ids"], artifact["result_id"]]
                elif call.arguments.get("source_id") and call.arguments["source_id"] not in step["evidence_ids"]:
                    step["evidence_ids"] = [*step["evidence_ids"], call.arguments["source_id"]]
                if call.complete_step:
                    step["status"] = "completed"
                observation["step_status"] = step["status"]
                observation["ready_step_ids"] = self._ready_steps(state)
                observation["message"] = ("步骤已完成，请继续就绪步骤。" if call.complete_step else
                    f"步骤{call.step_id}仍待完成；继续该步骤的必要工具，或用相同调用并设置complete_step=true复用成功响应结束该步骤。")
            else:
                state["budget"]["no_progress"] += 1
            state["next"] = "decide"
        except (ValueError, TypeError, KeyError, RuntimeError, OSError) as exc:
            self._error(state, "tool_execution", str(exc))
            state["trace"].append({"call_index": state["budget"]["tool_calls"], "tool": call.tool_name, "step_id": call.step_id, "success": False, "error": str(exc)})
        finally:
            self._runtime_check()
            state["budget"]["elapsed_seconds"] += time.monotonic() - started
        if state["next"] != "final":
            self._exhausted(state)
        return state

    def result(self, task_id: str, state: dict, intent: dict) -> QueryResult:
        partial = state["stop_reason"] != "completed" or bool(state["limitations"])
        artifacts = state["artifacts"]
        primary = next((a for a in artifacts if a["result_id"] == state.get("primary_result_id")), artifacts[-1] if artifacts else {})
        claims = state["claims"]
        title = state.get("title", "分析进度与已有证据")
        report = build_analysis_report(title, claims, artifacts, notes=state.get("notes", []),
                                       limitations=state["limitations"], charts=state["charts"], stop_reason=state["stop_reason"])
        return QueryResult(
            task_id=task_id, status="partial" if partial and artifacts else "failed" if partial else "completed",
            route="database_query", message="分析暂未完成，已保留现有证据" if partial else "分析完成",
            analysis=report.markdown, result_title=title, standalone_query=state["goal"],
            workflow_mode="analysis_agent", report=report,
            sql=primary.get("sql"), columns=primary.get("columns", []), rows=primary.get("rows", []),
            truncated=bool(primary.get("truncated")), warnings=list(dict.fromkeys([*primary.get("notes", []), *state.get("notes", [])])),
            steps=[s["title"] for s in state["plan"]], route_reason=intent.get("reason"),
            analysis_plan=state["plan"], result_artifacts=artifacts, analysis_claims=claims,
            analysis_limitations=list(dict.fromkeys(state["limitations"])), stop_reason=state["stop_reason"],
            analysis_hypotheses=state["hypotheses"],
            analysis_budget=state["budget"], execution_log=state["trace"],
            analysis_sources=[{"task_id": a["result_id"], "title": a["title"], "columns": a["columns"], "row_count": len(a["rows"])} for a in artifacts],
        )

    @staticmethod
    def _runtime_check():
        execution = CURRENT.get()
        if execution:
            execution.check()

    def _exhausted(self, state: dict) -> bool:
        budget = state["budget"]
        if budget["no_progress"] >= self.config.analysis_max_no_progress:
            self._stop(state, "no_progress", "连续步骤未取得进展，分析已停止")
        elif budget["elapsed_seconds"] >= self.config.task_timeout_seconds:
            self._stop(state, "budget_exhausted", "分析时间预算已用完")
        elif budget["decisions"] >= self.config.analysis_max_decisions:
            self._stop(state, "budget_exhausted", "分析决策次数已用完")
        elif budget["tool_calls"] >= self.config.analysis_max_tool_calls and state.get("next") == "tool":
            self._stop(state, "budget_exhausted", "分析工具调用预算已用完")
        execution = CURRENT.get()
        if execution and execution.store.get(execution.task["id"])["model_attempts"] >= execution.model_limit and state["next"] != "final":
            self._stop(state, "budget_exhausted", "任务累计模型请求预算已用完")
        return state["next"] == "final"

    @staticmethod
    def _stop(state: dict, reason: str, message: str) -> None:
        state["next"] = "final"
        state["stop_reason"] = reason
        state["limitations"].append(message)

    @staticmethod
    def _error(state: dict, category: str, message: str) -> None:
        state["observations"].append({"success": False, "category": category, "error": message[:1200]})
        state["trace"].append({"stage": "analysis_error", "category": category, "error": message[:1200]})
        state["budget"]["no_progress"] += 1
        state["next"] = "decide"

    def _update_plan(self, state: dict, decision: AnalysisDecision) -> None:
        if not decision.plan:
            raise ValueError("分析计划不可为空")
        new = [step.model_dump() for step in decision.plan]
        by_id = {s["id"]: s for s in new}
        if len(by_id) != len(new):
            raise ValueError("分析步骤ID不可重复")
        visited, active = set(), set()

        def visit(key: str) -> None:
            if key in active:
                raise ValueError("分析计划包含循环依赖")
            if key not in by_id:
                raise ValueError("分析步骤依赖不存在")
            if key in visited:
                return
            active.add(key)
            for dependency in by_id[key]["depends_on"]:
                visit(dependency)
            active.remove(key)
            visited.add(key)

        for key in by_id:
            visit(key)
        old = {s["id"]: s for s in state["plan"]}
        for step in new:
            previous = old.get(step["id"])
            if previous and previous["status"] == "completed":
                if any(previous[key] != step[key] for key in ("title", "depends_on")):
                    raise ValueError("已完成步骤不可改写；请新增步骤")
                step.update(status="completed", evidence_ids=previous["evidence_ids"])
            else:
                step.update(status="pending", evidence_ids=[])
        removed = [s for s in state["plan"] if s["id"] not in by_id]
        if any(s["status"] == "completed" for s in removed):
            raise ValueError("不能删除已完成步骤及其证据")
        if removed:
            if not decision.reason:
                raise ValueError("取消计划步骤必须说明原因")
            state["limitations"].append(f"计划调整：{decision.reason}；取消：{'、'.join(s['title'] for s in removed)}")
        signature = [(s["id"], s["title"], s["depends_on"]) for s in new]
        previous_signature = [(s["id"], s["title"], s["depends_on"]) for s in state["plan"]]
        state["budget"]["no_progress"] = state["budget"]["no_progress"] + 1 if signature == previous_signature else 0
        state["plan"] = new
        state["plan_version"] += 1
        state["trace"].append({"stage": "analysis_plan", "plan": copy.deepcopy(new), "reason": decision.reason})
        state["observations"].append({
            "success": True, "category": "plan_saved", "plan_version": state["plan_version"],
            "ready_step_ids": [s["id"] for s in new if s["status"] == "pending"
                               and all(by_id[d]["status"] == "completed" for d in s["depends_on"])],
            "message": "计划已保存。下一步选择就绪步骤调用工具；仅在观察表明需要调整时修改计划。",
        })

    def _validate_call(self, state: dict, decision: AnalysisDecision) -> None:
        if decision.tool_name not in self.tools.INPUTS:
            raise ValueError("工具不在白名单中")
        step = next((s for s in state["plan"] if s["id"] == decision.step_id), None)
        if not step or step["status"] != "pending":
            raise ValueError("工具调用必须属于一个待完成的计划步骤")
        done = {s["id"] for s in state["plan"] if s["status"] == "completed"}
        unfinished = [key for key in step["depends_on"] if key not in done]
        if unfinished:
            raise ValueError(f"前置步骤尚未完成：{','.join(unfinished)}。先继续这些步骤；如已有所需结果，用相同成功调用且complete_step=true复用响应并结束步骤。当前可执行：{','.join(self._ready_steps(state))}")
        self.tools.INPUTS[decision.tool_name][0].model_validate(decision.arguments)

    @staticmethod
    def _ready_steps(state: dict) -> list[str]:
        done = {step["id"] for step in state["plan"] if step["status"] == "completed"}
        return [step["id"] for step in state["plan"] if step["status"] == "pending" and set(step["depends_on"]).issubset(done)]

    def _finish(self, state: dict, decision: AnalysisDecision) -> None:
        if any("用户未要求" in item for item in decision.limitations):
            raise ValueError("明确标注用户未要求的事项属于notes范围说明，请移入notes；limitations仅保留未满足的用户要求或证据缺口")
        if decision.limitations and not decision.claims:
            state["limitations"].extend(decision.limitations)
            self._stop(state, "insufficient_evidence", "尚未形成可核对的分析事实")
            return
        if not state["plan"] or not decision.claims:
            raise ValueError("完成分析需要计划和带证据的结论")
        artifacts = {a["result_id"]: a for a in state["artifacts"]}
        if not artifacts:
            raise ValueError("完成分析需要实际查询结果")
        for claim in decision.claims:
            if not set(claim.evidence_ids).issubset(artifacts):
                raise ValueError("结论引用了不存在的结果")
            if re.search(r"\d", claim.text):
                raise ValueError(f"结论标题{claim.text!r}含数字；日期和观察天数也不能填入text。改为新增变化或队列激活率对比，用facts引用数值")
            if any(word in claim.text for word in ("导致", "造成", "原因是", "因为", "由于")):
                raise ValueError("因果解释应放入待验证hypotheses，不可当作数值事实发布")
            if not {fact.source_id for fact in claim.facts}.issubset(claim.evidence_ids):
                raise ValueError("事实来源必须包含在该结论的evidence_ids中")
            for fact in claim.facts:
                self._resolve_fact(fact, artifacts)

        if decision.primary_result_id and decision.primary_result_id not in artifacts:
            raise ValueError("主结果引用不存在")
        pending = [s["title"] for s in state["plan"] if s["status"] != "completed"]
        if pending and not decision.limitations:
            raise ValueError("尚有未完成步骤；请继续执行或明确未解决事项")
        missing = self._goal_gaps(state)
        if missing and not decision.limitations:
            raise ValueError("原问题尚缺证据：" + "、".join(missing))
        rendered = []
        for claim in decision.claims:
            facts = [self._resolve_fact(fact, artifacts) for fact in claim.facts]
            rendered.append({**claim.model_dump(), "facts": facts})
        state["claims"] = rendered
        state["notes"] = list(dict.fromkeys([*state.get("notes", []), *decision.notes]))
        state["limitations"].extend(decision.limitations)
        state["limitations"].extend(missing)
        if pending:
            state["limitations"].append(f"未完成步骤：{'、'.join(pending)}")
        if any(not artifacts[r]["complete"] for c in decision.claims for r in c.evidence_ids):
            state["limitations"].append("部分结论引用的结果被截断，不能视为整体结论")
        versions = {artifacts[r]["data_version"] for c in decision.claims for r in c.evidence_ids}
        if len(versions) > 1:
            state["limitations"].append("引用结果来自不同数据版本，需要重新核对")
        for hypothesis in state["hypotheses"]:
            if hypothesis["status"] == "unverified":
                state["limitations"].append(f"未验证假设：{hypothesis['statement']}")
        state.update(title=decision.title, primary_result_id=decision.primary_result_id, stop_reason="completed" if not state["limitations"] else "insufficient_evidence", next="final")

    @staticmethod
    def _goal_gaps(state: dict) -> list[str]:
        """Check explicit pilot metric/month requirements, not general NL entailment."""
        from datetime import date, timedelta
        query = state["original_query"]
        years = re.findall(r"(20\d{2})年", query)
        months = {int(m) for m in re.findall(r"(?<!\d)(\d{1,2})月", query) if 1 <= int(m) <= 12}
        months.update(int(m) for _, m in re.findall(r"(20\d{2})-(\d{2})(?:-\d{2})?", query))
        required = []
        if any(word in query for word in ("新增", "注册")):
            required.append("new_users")
        if "激活" in query:
            required.append("activation_cohort")
        gaps = []
        if "图表" in query and not state["charts"]:
            gaps.append("缺少用户要求的图表")
        if "贡献" in query and not any(a.get("derived_from") and "contribution_rate" in a.get("columns", [])
                                         and a.get("additive_fields") for a in state["artifacts"]):
            gaps.append("缺少可加指标的变化贡献计算")
        if "激活率" in query and len(months) > 1 and any(word in query for word in ("比较", "对比")) and not any(
            a.get("metric") == "activation_cohort:activation_rate:comparison" for a in state["artifacts"]
        ):
            gaps.append("缺少两期激活率差值计算，请用compare_results对比两期activation_rate")
        for metric in required:
            sources = [a for a in state["artifacts"] if a.get("metric") in ({"new_users", "activation_cohort"} if metric == "new_users" else {metric})
                       and a.get("semantic_verified") and a.get("start_date") and a.get("end_date")]
            if not sources:
                gaps.append(f"缺少{metric}指标证据")
                continue
            for month in months:
                found = False
                for source in sources:
                    start, end = date.fromisoformat(source["start_date"]), date.fromisoformat(source["end_date"]) - timedelta(days=1)
                    year = int(years[0]) if years else start.year
                    month_start = date(year, month, 1)
                    month_end = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
                    if start <= month_start and end >= month_end:
                        found = True
                if not found:
                    gaps.append(f"缺少{month}月{metric}证据")
        return gaps

    _resolve_fact = staticmethod(resolve_fact)

    @staticmethod
    def _summary(artifact: dict) -> dict:
        return {**{key: value for key, value in artifact.items() if key not in {"rows", "execution_ms"}}, "row_count": len(artifact["rows"]), "sample_rows": artifact["rows"][:12], "sample_complete": len(artifact["rows"]) <= 12}

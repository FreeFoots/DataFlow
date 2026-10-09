from __future__ import annotations

from typing import Any
from dataclasses import replace
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from ..config import Settings, settings
from ..clarification import clarification_answer
from ..database import SCHEMA, physical_table_name
from ..errors import PipelineStageError
from ..mcp_runtime import LocalMcpClient, create_local_mcp_server
from ..model_client import ModelClient
from ..models import QueryResult
from ..querying.request_understanding_agent import RequestUnderstandingAgent
from ..querying.duckdb_engine import DuckDbEngine
from ..querying.data_qa_agent import DataQaAgent
from ..querying.models import SqlExecution
from ..querying.analysis_agent import AnalysisAgent
from ..querying.analysis_tools import AnalysisTools
from ..querying.response_generator import ResponseGenerator
from ..querying.single_database_agent import SingleDatabaseAgent
from ..querying.result_assets import EvidenceClaim, resolve_fact
from ..querying.analysis_report import build_analysis_report
from ..retrieval import SchemaGraphBuilder, SchemaIndex
from ..security import AccessScope
from ..skills import SkillRegistry
from .result_builder import ResultBuilder
from .state import QueryState


class QueryWorkflow:
    """基于 LangGraph 的问答和数据库查询工作流。"""

    def __init__(
        self,
        model_client: ModelClient,
        schema_index: SchemaIndex,
        config: Settings | None = None,
        checkpointer=None,
    ) -> None:
        self.model_client = model_client
        self.schema_index = schema_index
        self.config = config or settings
        self.request_understanding_agent = RequestUnderstandingAgent(model_client)
        self.preprocessor = self.request_understanding_agent  # Compatibility for existing callers.
        self.skills = SkillRegistry()
        self.graph_builder = SchemaGraphBuilder()
        self.database_engine = DuckDbEngine(Path(self.config.database_root))
        analysis_skill = self.skills.get("analysis")
        analysis_tools = AnalysisTools(schema_index, self.database_engine, self.mcp_client,
                                       exploratory_sql=self.config.analysis_allow_exploratory_sql)
        analysis_tools.INPUTS = {name: definition for name, definition in analysis_tools.INPUTS.items() if name in analysis_skill.allowed_tools}
        self.analysis_agent = AnalysisAgent(model_client, analysis_tools,
            replace(self.config, analysis_max_tool_calls=min(self.config.analysis_max_tool_calls, analysis_skill.max_tool_calls)),
            analysis_skill.instructions)
        self.single_database_agent = SingleDatabaseAgent(
            model_client,
            self.mcp_client,
            self.skills.get("database_query"),
            self.config.mcp_max_tool_calls,
        )
        self.response_generator = ResponseGenerator(
            model_client,
            self.config,
        )
        self.data_qa_agent = DataQaAgent(
            model_client,
            self.mcp_client,
            self.skills.get("data_qa"),
        )
        self.checkpointer = checkpointer if checkpointer is not None else InMemorySaver()
        self.graph = self._compile()

    def mcp_client(self, access_scope: dict[str, Any]) -> LocalMcpClient:
        scope = AccessScope.from_dict(access_scope)
        return LocalMcpClient(create_local_mcp_server(self.database_engine, scope))

    def _compile(self):
        builder = StateGraph(QueryState)
        from ..runtime.execution import CURRENT
        def node(name, operation):
            def run(state):
                execution = CURRENT.get()
                output = execution.node(name, operation, state) if execution else operation(state)
                if execution and output.get("analysis_state"):
                    preview = self.analysis_agent.result(state["task_id"], output["analysis_state"], state.get("intent") or {})
                    execution.store.progress(state["task_id"], execution.owner, execution.generation, preview.model_dump(mode="json"))
                return output
            return run
        for name in ["preprocess", "request_clarification", "respond_directly", "answer_qa", "retrieve_schema", "human_clarification", "prepare_single_database", "execute_single_database", "run_multi_database", "analysis_initialize", "analysis_decide", "analysis_execute", "analysis_clarification", "analysis_finalize"]:
            builder.add_node(name, node(name, getattr(self, "_" + name)))
        builder.add_edge(START, "preprocess")
        builder.add_conditional_edges(
            "preprocess",
            lambda state: "request_clarification" if state.get("clarification") else "analysis_initialize" if state["intent"].get("mode") == "analysis" else {
                "direct_response": "respond_directly",
                "data_qa": "answer_qa",
                "database_query": "retrieve_schema",
            }[state["intent"]["action"]],
            {
                "respond_directly": "respond_directly",
                "answer_qa": "answer_qa",
                "retrieve_schema": "retrieve_schema",
                "analysis_initialize": "analysis_initialize",
                "request_clarification": "request_clarification",
            },
        )
        builder.add_edge("request_clarification", "preprocess")
        builder.add_edge("respond_directly", END)
        builder.add_edge("answer_qa", END)
        builder.add_edge("analysis_initialize", "analysis_decide")
        analysis_routes = {"decide": "analysis_decide", "tool": "analysis_execute", "clarify": "analysis_clarification", "final": "analysis_finalize"}
        builder.add_conditional_edges("analysis_decide", lambda state: state["analysis_route"], analysis_routes)
        builder.add_conditional_edges("analysis_execute", lambda state: state["analysis_route"], analysis_routes)
        builder.add_edge("analysis_clarification", "analysis_decide")
        builder.add_edge("analysis_finalize", END)
        builder.add_conditional_edges(
            "retrieve_schema",
            self._after_retrieval,
            {
                "human_clarification": "human_clarification",
                "prepare_single_database": "prepare_single_database",
                "run_multi_database": "run_multi_database",
            },
        )
        builder.add_edge("human_clarification", "retrieve_schema")
        builder.add_conditional_edges(
            "prepare_single_database",
            lambda state: "human_clarification" if state.get("clarification") else "execute_single_database",
            {
                "human_clarification": "human_clarification",
                "execute_single_database": "execute_single_database",
            },
        )
        builder.add_edge("execute_single_database", END)
        builder.add_conditional_edges(
            "run_multi_database",
            lambda state: "human_clarification" if state.get("clarification") else "end",
            {"human_clarification": "human_clarification", "end": END},
        )
        return builder.compile(checkpointer=self.checkpointer)

    @staticmethod
    def run_config(task_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": task_id}, "recursion_limit": 128}

    def invoke(self, payload: QueryState | Command | None, task_id: str) -> QueryResult:
        state = self.graph.invoke(payload, config=self.run_config(task_id), durability="sync")
        return self._state_result(state, task_id)

    def _state_result(self, state: dict[str, Any], task_id: str) -> QueryResult:
        if state.get("result"):
            result = QueryResult.model_validate(state["result"])
        else:
            state = {**state, "task_id": state.get("task_id") or task_id}
            result = ResultBuilder.waiting(state)
        if state.get("request_understanding"):
            from ..models import RequestUnderstanding
            result.request_understanding = RequestUnderstanding.model_validate(state["request_understanding"])
        return result

    def _preprocess(self, state: QueryState) -> dict[str, Any]:
        """生成路由、独立查询和 Schema 检索参数。"""
        answers = state.get("understanding_answers") or []
        decision = self.preprocessor.prepare(state["query"], state.get("route_context", ""), answers)
        requested_mode = state.get("request_mode", "auto")
        mode = "query"
        if decision.action == "database_query":
            mode = decision.mode if requested_mode == "auto" else requested_mode
        if requested_mode == "analysis" and decision.action != "direct_response" and decision.source != "model_unavailable_fallback":
            mode = "analysis"
        execution_log = list(state.get("execution_log") or [])
        if decision.source == "model_unavailable_fallback":
            execution_log.append({
                "stage": "route_fallback",
                "success": True,
                "source": decision.source,
                "mode": mode,
                "action": decision.action,
                "reason": decision.reason,
            })
        return {
            "intent": {
                "action": decision.action,
                "mode": mode,
                "confidence": decision.confidence,
                "reason": decision.reason,
                "response_type": decision.response_type,
                "source": decision.source,
            },
            "direct_response": decision.response,
            "standalone_query": decision.standalone_query,
            "rewritten": decision.rewritten,
            "extraction": decision.retrieval.public(),
            "execution_log": execution_log,
            "request_understanding": {**decision.understanding.model_dump(mode="json"), "clarifications": answers},
            "clarification": decision.clarification,
            "workflow_mode": "request_clarification" if decision.clarification else "",
            "result": {},
        }

    def _request_clarification(self, state: QueryState) -> dict[str, Any]:
        question = state["clarification"]
        response = interrupt(question)
        response = response if isinstance(response, dict) else {"option_id": str(response)}
        answer = clarification_answer(question, response.get("option_id", ""), response.get("answer"))
        return {"understanding_answers": [*(state.get("understanding_answers") or []), answer],
                "clarification": None, "result": {}}

    def _analysis_initialize(self, state: QueryState) -> dict[str, Any]:
        analysis = self.analysis_agent.initialize(state.get("standalone_query") or state["query"], state["query"])
        return {"analysis_state": analysis, "workflow_mode": "analysis_agent", "clarification": None}

    def _analysis_decide(self, state: QueryState) -> dict[str, Any]:
        analysis = self.analysis_agent.decide(state["task_id"], state["analysis_state"], state.get("access_scope") or {}, state.get("workspace") or {})
        return {"analysis_state": analysis, "analysis_route": analysis["next"], "clarification": analysis.get("clarification") if analysis["next"] == "clarify" else None}

    def _analysis_execute(self, state: QueryState) -> dict[str, Any]:
        analysis = self.analysis_agent.execute(state["task_id"], state["analysis_state"], state.get("access_scope") or {})
        return {"analysis_state": analysis, "analysis_route": analysis["next"]}

    def _analysis_clarification(self, state: QueryState) -> dict[str, Any]:
        response = interrupt(state["clarification"])
        option_id = str(response.get("option_id") if isinstance(response, dict) else response)
        analysis = dict(state["analysis_state"])
        question = analysis["clarification"]
        option = next((o for o in question["options"] if o["id"] == option_id), None)
        if option is None:
            raise PipelineStageError("analysis_clarification", "无效的澄清选项")
        analysis["clarification_answers"] = {**analysis["clarification_answers"], question["parameter"]: option}
        analysis.pop("clarification", None)
        analysis["next"] = "decide"
        return {"analysis_state": analysis, "clarification": None}

    def _analysis_finalize(self, state: QueryState) -> dict[str, Any]:
        result = self.analysis_agent.result(state["task_id"], state["analysis_state"], state.get("intent") or {})
        return {"result": result.model_dump(mode="json")}

    def _respond_directly(self, state: QueryState) -> dict[str, Any]:
        """返回预处理模型生成的普通回答或自然语言澄清。"""
        result = ResultBuilder.direct_response(
            state["task_id"],
            state.get("direct_response", ""),
            state["intent"],
            list(state.get("execution_log") or []),
        )
        return {
            "workflow_mode": result.workflow_mode,
            "result": result.model_dump(mode="json"),
        }

    def _answer_qa(self, state: QueryState) -> dict[str, Any]:
        # Read authorization from the task, never from a model or client table.
        scope = AccessScope.from_dict(state.get("access_scope") or {})
        allowed = {physical_table_name(table) for table in SCHEMA if scope.allows_table(table.get("database", "short_video_ops"), table["id"])}
        artifacts = state.get("qa_artifacts") or []
        trusted = {}
        for artifact in artifacts:
            tables = set(artifact.get("source_tables") or [])
            if tables and tables.issubset(allowed):
                trusted[artifact["result_id"]] = artifact
        # Derived assets inherit access only if all of their parents are authorized.
        for _ in artifacts:
            for artifact in artifacts:
                parents = artifact.get("derived_from") or []
                if parents and set(parents).issubset(trusted):
                    trusted[artifact["result_id"]] = artifact
        qa_result = self.data_qa_agent.run(
            (state.get("request_understanding") or {}).get("standard_request") or state["query"],
            {
                "short_term": state.get("short_term_context", ""),
                "recent_result": state.get("recent_result_context", ""),
                "selected_tables": state.get("analysis_context", ""),
            },
            state.get("access_scope") or {},
            artifacts=list(trusted.values()),
        )
        result = ResultBuilder.qa(
            state["task_id"], qa_result, state["intent"], state.get("analysis_sources") or []
        )
        return {
            "workflow_mode": "qa_report" if qa_result.report else "qa",
            "result": result.model_dump(mode="json"),
        }

    def _retrieve_schema(self, state: QueryState) -> dict[str, Any]:
        standalone_query = state["standalone_query"]
        extraction = state.get("extraction") or {}
        retrieval = self.schema_index.retrieve(
            standalone_query,
            retrieval_terms=list(extraction.get("retrieval_terms") or []),
            access_scope=state.get("access_scope"),
        )
        workspace = state.get("workspace") or {}
        query_workspace = {
            "schema_fields": list(workspace.get("schema_fields") or []),
            "confirmed_schema_tables": list(workspace.get("confirmed_schema_tables") or []),
            "confirmed_parameters": dict(workspace.get("confirmed_parameters") or {}),
        }
        retrieval = self.schema_index.include_workspace(
            retrieval,
            query_workspace,
            state.get("access_scope"),
        )
        retrieval["extraction"] = extraction
        schema_graph = self.graph_builder.build(
            retrieval["hits"],
            state.get("access_scope"),
        )
        retrieval["schema_graph"] = schema_graph
        databases = sorted({
            str(table.get("database") or schema_graph.get("database") or "short_video_ops")
            for table in schema_graph.get("tables", [])
        })
        return {
            "standalone_query": standalone_query,
            "extraction": extraction,
            "retrieval": retrieval,
            "schema_graph": schema_graph,
            "schema_context": self.graph_builder.context_text(schema_graph),
            "database_names": databases,
            "clarification": None,
            "direct_sql": "",
        }

    @staticmethod
    def _after_retrieval(state: QueryState) -> str:
        if state.get("clarification"):
            return "human_clarification"
        return "prepare_single_database" if len(state.get("database_names") or []) <= 1 else "run_multi_database"

    def _human_clarification(self, state: QueryState) -> dict[str, Any]:
        # LangGraph 将 Command(resume=...) 的值作为 interrupt 返回值。
        response = interrupt(state.get("clarification") or {})
        option_id = str(response.get("option_id") if isinstance(response, dict) else response)
        workspace = dict(state.get("workspace") or {})
        payload = state.get("clarification") or {}
        parameter = str(payload.get("parameter") or "other")
        tables = list(workspace.get("confirmed_schema_tables") or [])
        if any(table["id"] == option_id for table in SCHEMA):
            tables = list(dict.fromkeys([*tables, option_id]))
        workspace["confirmed_schema_tables"] = tables
        workspace["confirmed_parameters"] = {
            **dict(workspace.get("confirmed_parameters") or {}),
            parameter: option_id,
        }
        return {"workspace": workspace, "clarification": None, "direct_sql": "", "result": {}}

    def _prepare_single_database(self, state: QueryState) -> dict[str, Any]:
        workspace = dict(state.get("workspace") or {})
        database = (state.get("database_names") or ["short_video_ops"])[0]
        decision = self.single_database_agent.prepare(
            state["standalone_query"],
            database,
            state["schema_graph"],
            state["schema_context"],
            state["retrieval"],
            workspace,
            state.get("access_scope") or {},
        )
        if decision["action"] == "clarify":
            return {
                "workflow_mode": "single_database_agent",
                "clarification": decision["clarification"],
                "mcp_tool_trace": decision.get("tool_trace", []),
                "direct_sql": "",
            }
        execution = decision["execution"]
        return {
            "workflow_mode": "single_database_agent",
            "clarification": None,
            "mcp_execution": execution,
            "mcp_tool_trace": decision.get("tool_trace", []),
            "direct_sql": str(execution.get("sql") or ""),
            "sql_source": decision.get("source", "model"),
            "result_artifacts": [{**execution["artifact"], "result_id": f"{state['task_id']}:r1"}]
                if execution.get("artifact") else [],
        }

    def _execute_single_database(self, state: QueryState) -> dict[str, Any]:
        database = (state.get("database_names") or ["short_video_ops"])[0]
        raw_execution = state.get("mcp_execution") or {}
        execution = SqlExecution(
            sql=str(raw_execution.get("sql") or state.get("direct_sql") or ""),
            success=bool(raw_execution.get("success")),
            columns=list(raw_execution.get("columns") or []),
            rows=list(raw_execution.get("rows") or []),
            error=raw_execution.get("error"),
            truncated=bool(raw_execution.get("truncated", False)),
            limited=bool(raw_execution.get("limited", False)),
        )
        trace = list(state.get("mcp_tool_trace") or [])
        log = [
            {
                "stage": "mcp_tool_call",
                "success": not bool(item.get("result", {}).get("error")),
                **item,
            }
            for item in trace
        ]
        log.append({
            "stage": "execute_duckdb",
            "success": execution.success,
            "error": execution.error,
            "via": "mcp",
        })
        database_call = next(
            (item for item in reversed(trace) if item.get("tool") in {f"query_{database}", "query_metric"}),
            {},
        )
        call = {
            "call_index": int(database_call.get("call_index") or 1),
            "database": database,
            "arguments": {
                "mode": "single_database_agent",
                "transport": "mcp_in_process",
                "tool_name": database_call.get("tool"),
                "schema_graph_version": state.get("schema_graph", {}).get("graph_version"),
                "sql_source": state.get("sql_source", "model"),
            },
            "sql": execution.sql,
            "success": execution.success,
            "row_count": len(execution.rows),
            "error": execution.error,
        }
        if not execution.success:
            result = ResultBuilder.failed(state, execution, log)
            return {"execution_log": log, "tool_calls": [call], "result": result.model_dump(mode="json")}
        try:
            artifacts = state.get("result_artifacts") or []
            final = self._metric_description(artifacts) if artifacts else self.response_generator.finalize(
                state["standalone_query"], execution, state["schema_context"],
                state.get("analysis_context", ""),
            )
        except PipelineStageError as exc:
            # 结果说明失败时仍保留已成功执行的查询结果。
            log.append({"stage": exc.stage, "success": False, "error": exc.message})
            final = {
                "valid": True,
                "reason": f"{exc.stage}失败",
                "title": "查询结果（文字说明生成失败）",
                "analysis": f"SQL已成功执行，但{exc.stage}失败：{exc.message}",
            }
        result = ResultBuilder.completed(state, [execution], execution, final, [call], log)
        return {"execution_log": log, "tool_calls": [call], "result": result.model_dump(mode="json")}

    @staticmethod
    def _metric_description(artifacts: list[dict]) -> dict:
        source = artifacts[0]
        if not source.get("complete") or source.get("limited"):
            return {"valid": True, "title": source["title"],
                    "analysis": "查询结果已返回；当前展示为限定范围数据，不能据此计算整体统计。"}
        source_id = source["result_id"]
        facts = [{"source_id": source_id, "section": "summary", "field": key}
                 for key in source.get("summary", {})]
        claim = EvidenceClaim(text="查询事实", evidence_ids=[source_id], facts=facts)
        checked = [{**claim.model_dump(), "facts": [resolve_fact(fact, {source_id: source}) for fact in claim.facts]}]
        report = build_analysis_report(source["title"], checked, artifacts)
        return {"valid": True, "title": source["title"], "analysis": report.markdown, "claims": checked}

    def _run_multi_database(self, state: QueryState) -> dict[str, Any]:
        """返回尚未实现的多数据库查询结果。"""
        failure = SqlExecution(
            sql="",
            success=False,
            error="当前仅支持单库直接查询；多库 Handoff 尚未启用。",
        )
        result = ResultBuilder.failed(state, failure, [])
        return {"workflow_mode": "multi_database_pending", "result": result.model_dump(mode="json")}

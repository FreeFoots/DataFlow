from __future__ import annotations

from typing import Any

from ..database import SCHEMA
from ..models import Clarification, Interpretation, QueryResult
from ..querying.models import SqlExecution
from ..querying.data_qa_agent import DataQaResult


class ResultBuilder:
    """将工作流状态转换为 API 响应。"""

    @staticmethod
    def qa(
        task_id: str,
        generated: DataQaResult,
        intent: dict[str, Any],
        analysis_sources: list[dict[str, Any]],
    ) -> QueryResult:
        return QueryResult(
            task_id=task_id,
            status="completed",
            route="data_qa",
            message="已进入数据问答，没有重复查询数据库。",
            columns=[],
            rows=[],
            analysis=generated.report.markdown if generated.report else generated.answer,
            route_reason=str(intent.get("reason") or "数据问答"),
            steps=(
                ["一级意图路由：数据问答", "读取可用分析上下文", "生成Markdown报告并调用展示工具"]
                if generated.report
                else ["一级意图路由：数据问答", "读取可用分析上下文", "生成文字回答"]
            ),
            result_title=generated.report.title if generated.report else "结果解读",
            analysis_sources=analysis_sources,
            workflow_mode="qa_report" if generated.report else "qa",
            report=generated.report,
            report_tool_calls=generated.tool_calls,
        )

    @staticmethod
    def direct_response(
        task_id: str,
        response: str,
        intent: dict[str, Any],
        execution_log: list[dict[str, Any]],
    ) -> QueryResult:
        response_type = str(intent.get("response_type") or "answer")
        fallback_used = intent.get("source") == "model_unavailable_fallback"
        return QueryResult(
            task_id=task_id,
            status="failed" if fallback_used else "completed",
            route="direct_response",
            message="服务暂时不可用，请稍后重试" if fallback_used else ("请补充说明" if response_type == "clarification" else "已直接回答"),
            columns=[],
            rows=[],
            analysis=response,
            route_reason=str(intent.get("reason") or "预处理模型直接回复"),
            steps=["一次请求预处理", "预处理模型直接生成回复"],
            execution_log=execution_log,
            workflow_mode="route_fallback" if fallback_used else "direct_response",
        )

    @staticmethod
    def waiting(state: dict[str, Any]) -> QueryResult:
        payload = state.get("clarification") or {}
        clarification = Clarification(
            parameter=str(payload.get("parameter") or "other"),
            question=str(payload.get("question") or "请补充本次查询所需信息"),
            reason=str(payload.get("reason") or "该信息会改变查询结果"),
            options=payload.get("options") or [],
        )
        labels = "、".join(item.label for item in clarification.options)
        return QueryResult(
            task_id=state["task_id"],
            status="waiting_clarification",
            route="database_query",
            message="查询已暂停，等待你的补充信息。",
            clarification=clarification,
            analysis=f"{clarification.question} 你可以直接回复：{labels}。",
            route_reason=str(state.get("intent", {}).get("reason") or "问数"),
            retrieval=ResultBuilder.public_retrieval(state.get("retrieval") or {}),
            standalone_query=state.get("standalone_query"),
            schema_graph=state.get("schema_graph"),
            steps=["一次请求预处理：路由并提取检索词", "召回字段并构建Schema图", "LangGraph暂停并等待用户回复"],
            workflow_mode=str(state.get("workflow_mode") or "langgraph_hitl"),
        )

    @staticmethod
    def completed(
        state: dict[str, Any],
        executions: list[SqlExecution],
        combined: SqlExecution,
        final: dict[str, Any],
        tool_calls: list[dict[str, Any]],
        execution_log: list[dict[str, Any]],
    ) -> QueryResult:
        graph = state.get("schema_graph") or {}
        table_ids = [item["id"] for item in graph.get("tables", [])]
        table_labels = [item["label"] for item in SCHEMA if item["id"] in table_ids]
        metric_columns = [
            column for column in combined.columns
            if any(term in column for term in ("额", "数", "率", "平均", "目标"))
        ]
        dimension_columns = [column for column in combined.columns if column not in metric_columns]
        mode = str(state.get("workflow_mode") or "single_database_agent")
        steps = [
            "一次预处理：上下文聚合、问答/问数路由、检索词提取",
            *([f"独立查询改写：{state.get('standalone_query')}"] if state.get("rewritten") else []),
            "BM25与Dense召回，经RRF融合和Rerank阈值筛选",
            f"构建Schema图：{len(graph.get('tables', []))}张表 / {len(graph.get('fields', []))}个字段",
            "单库智能体选择MCP工具并生成SQL" if mode == "single_database_agent" else "多库路径：按数据库生成Handoff",
            "通过MCP数据库工具调用DuckDB并整理结果",
        ]
        if state.get("analysis_sources"):
            steps.append(f"综合分析{len(state['analysis_sources'])}张用户指定历史表")
        return QueryResult(
            task_id=state["task_id"],
            status="completed" if final.get("valid", True) else "failed",
            route="database_query",
            message="查询完成" if final.get("valid", True) else "结果校验未通过",
            interpretation=Interpretation(
                metric="、".join(metric_columns) or "查询结果指标",
                dimension="、".join(dimension_columns) or "无分组维度",
                time_range="、".join(
                    (state.get("extraction") or {}).get("time_expressions") or []
                ) or "未指定",
                table="、".join(table_labels) or "Schema召回数据表",
                assumptions=["当前口径由模型识别，尚未通过业务指标编译器验证"],
            ),
            steps=steps,
            sql=combined.sql,
            columns=combined.columns,
            rows=combined.rows,
            truncated=combined.truncated,
            warnings=["结果超过200行，目前只展示前200行；导出与解读仅覆盖当前预览。"] if combined.truncated else [],
            analysis=final.get("analysis"),
            route_reason=str(state.get("intent", {}).get("reason") or "问数"),
            retrieval=ResultBuilder.public_retrieval(state.get("retrieval") or {}),
            execution_log=execution_log,
            tool_calls=tool_calls,
            result_title=final.get("title") or "查询结果",
            analysis_sources=state.get("analysis_sources") or [],
            standalone_query=state.get("standalone_query"),
            schema_graph=graph,
            workflow_mode=mode,
        )

    @staticmethod
    def failed(state: dict[str, Any], execution: SqlExecution, log: list[dict[str, Any]]) -> QueryResult:
        return QueryResult(
            task_id=state["task_id"],
            status="failed",
            route="database_query",
            message="SQL生成或执行失败",
            analysis=execution.error,
            sql=execution.sql or None,
            route_reason=str(state.get("intent", {}).get("reason") or "问数"),
            retrieval=ResultBuilder.public_retrieval(state.get("retrieval") or {}),
            execution_log=log,
            standalone_query=state.get("standalone_query"),
            schema_graph=state.get("schema_graph"),
            workflow_mode=str(state.get("workflow_mode") or "single_database_agent"),
        )

    @staticmethod
    def public_retrieval(retrieval: dict[str, Any]) -> dict[str, Any]:
        visible = {
            "query", "retrieval_terms", "extraction", "embedding_source",
            "rerank_source", "threshold", "bm25_count", "dense_count", "rrf_count",
            "candidate_count", "selected_count", "hits", "table_candidates",
            "low_confidence_candidates", "schema_graph",
        }
        return {key: value for key, value in retrieval.items() if key in visible}

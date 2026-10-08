from __future__ import annotations

from typing import Any, TypedDict


class QueryState(TypedDict, total=False):
    """LangGraph 工作流状态。"""

    task_id: str
    query: str
    session_id: str
    workspace: dict[str, Any]
    access_scope: dict[str, Any]
    route_context: str
    short_term_context: str
    recent_result_context: str
    analysis_context: str
    analysis_sources: list[dict[str, Any]]
    intent: dict[str, Any]
    direct_response: str
    response_type: str
    standalone_query: str
    rewritten: bool
    extraction: dict[str, Any]
    retrieval: dict[str, Any]
    schema_graph: dict[str, Any]
    schema_context: str
    database_names: list[str]
    clarification: dict[str, Any] | None
    direct_sql: str
    sql_source: str
    tool_facts: dict[str, Any]
    mcp_execution: dict[str, Any]
    mcp_tool_trace: list[dict[str, Any]]
    execution_log: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]
    workflow_mode: str
    result: dict[str, Any]

"""Business-language failure descriptions; diagnostic details stay on the server."""
from ..models import QueryResult


def failure_message(stage: str | None) -> str:
    if stage in {"preprocess", "request_clarification"}:
        return "这次还没能整理出完整的分析需求。你的问题已保留，请稍后重试。"
    if stage == "retrieve_schema":
        return "暂时没能准备好这次分析需要的数据。已确认的需求会保留，请稍后重试。"
    if stage == "prepare_single_database":
        return "这次未能整理好查询方案。已确认的需求会保留，请稍后重试。"
    if stage in {"execute_single_database", "analysis_finalize"}:
        return "这次未能完成结果整理。已确认的需求会保留，请稍后重试。"
    if stage in {"answer_qa", "analysis_initialize", "analysis_decide", "analysis_execute", "analysis_clarification"}:
        return "这次分析暂时没有完成。已有内容会保留，请稍后重试。"
    return "这次处理暂时没有完成。你的问题已保留，请稍后重试。"


def present_failure(result: QueryResult, stage: str | None) -> QueryResult:
    if result.status != "failed" or result.stop_reason in {"cancelled", "timed_out"}:
        return result
    message = failure_message(stage)
    return result.model_copy(update={"message": message, "analysis": message,
                                    "execution_log": [], "tool_calls": [], "sql": None,
                                    "warnings": [], "route_reason": None,
                                    "steps": [], "report": None, "result_title": None,
                                    "analysis_limitations": [message]}, deep=True)

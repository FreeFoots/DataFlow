from __future__ import annotations

from typing import Any

from ..config import Settings, settings
from ..errors import PipelineStageError
from ..model_client import ModelClient
from .models import SqlExecution


class ResponseGenerator:
    """生成问答内容和查询结果说明。"""

    def __init__(
        self,
        model_client: ModelClient,
        config: Settings | None = None,
    ) -> None:
        self.model_client = model_client
        self.table_row_limit = max(1, (config or settings).context_table_row_limit)

    def finalize(
        self,
        query: str,
        execution: SqlExecution,
        schema_context: str,
        analysis_context: str,
    ) -> dict[str, Any]:
        system = """你是查询结果整理器。检查结果能否回答问题，并生成简短标题和一到两句说明。
只能使用结果中真实存在的数值。只返回JSON：
如果数据范围标注为预览，不得推断全量总数、全量排名或整体占比；明确说明范围。
{"valid":true,"reason":"...","title":"...","analysis":"..."}。"""
        user = (
            f"问题：{query}\nSQL：{execution.sql}\n列：{execution.columns}\n"
            f"结果数据：{execution.rows[: self.table_row_limit]}\nSchema：{schema_context}\n"
            f"数据范围：{'查询结果超过200行，当前结果被截断；' if execution.truncated else ''}"
            f"本次文字说明仅读取前{min(len(execution.rows), self.table_row_limit)}行，共返回{len(execution.rows)}行。\n"
            f"用户保存的分析表格：{analysis_context or '无'}"
        )
        try:
            payload = self.model_client.chat_json(system, user)
            return {
                "valid": bool(payload.get("valid", True)),
                "reason": str(payload.get("reason") or "结果检查通过"),
                "title": str(payload.get("title") or "查询结果"),
                "analysis": str(payload.get("analysis") or f"查询返回{len(execution.rows)}行。"),
            }
        except RuntimeError as exc:
            raise PipelineStageError("result_analysis", str(exc)) from exc

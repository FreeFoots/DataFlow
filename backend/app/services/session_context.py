from __future__ import annotations

import json
import logging
from collections import defaultdict
from typing import Any

from ..config import Settings
from ..model_client import ModelClient
from ..models import QueryResult
from .session_archive import SessionArchive
from .short_term_memory import ShortTermMemory


logger = logging.getLogger(__name__)


class SessionContext:
    """管理会话历史和前端上下文。"""

    def __init__(self, model_client: ModelClient, config: Settings) -> None:
        self.tasks: dict[str, dict[str, Any]] = {}
        self.session_tasks: dict[str, list[str]] = defaultdict(list)
        self.table_row_limit = max(1, config.context_table_row_limit)
        self.route_context_turns = max(1, config.route_context_turns)
        self.archive = SessionArchive(
            config.session_archive_file,
            enabled=config.session_archive_enabled,
        )
        self.short_term_memory = ShortTermMemory(
            model_client,
            trigger_tokens=config.short_term_summary_trigger_tokens,
            batch_tokens=config.short_term_summary_batch_tokens,
            min_recent_turns=config.short_term_min_recent_turns,
            summary_enabled=config.short_term_summary_enabled,
            on_summary_updated=self.archive.save_summary,
        )
        self._restore_archive()

    def remember(
        self,
        session_id: str,
        result: QueryResult,
        query: str,
        workspace: dict[str, Any],
        user_id: str = "demo_growth_ops",
    ) -> None:
        self.tasks[result.task_id] = {
            "query": query,
            "session_id": session_id,
            "workspace": workspace,
            "user_id": user_id,
            "result": result,
        }
        if result.task_id not in self.session_tasks[session_id]:
            self.session_tasks[session_id].append(result.task_id)
        self.archive.save_turn(
            result.task_id,
            session_id,
            query,
            workspace,
            result.model_dump(mode="json"),
            user_id=user_id,
        )
        self.short_term_memory.maybe_schedule(session_id, self._memory_items(session_id))

    def _restore_archive(self) -> None:
        quarantined_sessions: set[str] = set()
        for item in self.archive.load_turns():
            try:
                result = QueryResult.model_validate(item["result"])
            except ValueError as exc:
                logger.warning(
                    "session_turn_restore_failed task_id=%s error=%s",
                    item.get("task_id"),
                    exc,
                )
                continue
            task_id = str(item["task_id"])
            session_id = str(item["session_id"])
            self.tasks[task_id] = {
                "query": item["query"],
                "session_id": session_id,
                "workspace": item["workspace"],
                "user_id": item.get("user_id"),
                "result": result,
                "restored": True,
            }
            owner = item.get("user_id")
            if not owner or not session_id.startswith(f"{owner}:"):
                quarantined_sessions.add(session_id)
                logger.warning("session_turn_owner_missing_or_mismatched task_id=%s", task_id)
                continue
            self.session_tasks[session_id].append(task_id)

        for state in self.archive.load_summaries():
            if state["session_id"] in quarantined_sessions or state["session_id"] not in self.session_tasks:
                continue
            self.short_term_memory.restore(
                state["session_id"],
                state["summary"],
                state["summarized_ids"],
            )

    def latest_pending(self, session_id: str) -> dict[str, Any] | None:
        ids = self.session_tasks.get(session_id, [])
        if not ids:
            return None
        task = self.tasks.get(ids[-1])
        return (
            task
            if task
            and not task.get("restored")
            and task["result"].status == "waiting_clarification"
            else None
        )

    def latest_table_result(self, session_id: str) -> QueryResult | None:
        for task_id in reversed(self.session_tasks.get(session_id, [])):
            result: QueryResult = self.tasks[task_id]["result"]
            if result.status == "completed" and result.route == "database_query" and result.rows:
                return result
        return None

    def route_context(
        self, session_id: str, workspace: dict[str, Any] | None = None
    ) -> str:
        # 路由上下文只读取近期轮次，不读取异步摘要。
        recent_items = self._memory_items(session_id)[-self.route_context_turns :]
        context = {
            "recent_turns": [self._route_item(item) for item in recent_items],
        }
        selected_headers = self._selected_table_headers(session_id, workspace or {})
        if selected_headers:
            context["selected_table_headers"] = selected_headers
        return json.dumps(context, ensure_ascii=False) if recent_items or selected_headers else ""

    def short_term_context(self, session_id: str) -> str:
        """返回滑动窗口和异步摘要，供已有数据分析流程使用。"""
        return self.short_term_memory.context(session_id, self._memory_items(session_id))

    @staticmethod
    def _route_item(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "turn_id": str(item.get("task_id") or ""),
            "user_message": item.get("query", ""),
            "route": item.get("route", ""),
            "status": item.get("status", ""),
            "result_title": item.get("result_title"),
            "assistant_summary": str(item.get("analysis") or "")[:240],
            "columns": list(item.get("columns") or []),
            "row_count": int(item.get("row_count") or 0),
        }

    def _selected_table_headers(
        self, session_id: str, workspace: dict[str, Any]
    ) -> list[dict[str, Any]]:
        supplied = {
            str(item.get("task_id")): item
            for item in workspace.get("analysis_tables", [])
            if item.get("task_id")
        }
        headers = []
        for task_id in list(dict.fromkeys(workspace.get("analysis_table_ids") or []))[:5]:
            provided = supplied.get(task_id)
            if provided:
                headers.append({
                    "task_id": task_id,
                    "title": str(provided.get("title") or "参考表格"),
                    "query": str(provided.get("query") or ""),
                    "columns": list(provided.get("columns") or []),
                    "row_count": len(provided.get("rows") or []),
                })
                continue
            task = self.tasks.get(task_id)
            if not task or task.get("session_id") != session_id:
                continue
            result: QueryResult = task["result"]
            headers.append({
                "task_id": task_id,
                "title": result.result_title or "参考表格",
                "query": task.get("query", ""),
                "columns": result.columns,
                "row_count": len(result.rows),
            })
        return headers

    def _memory_items(self, session_id: str) -> list[dict[str, Any]]:
        output = []
        for task_id in self.session_tasks.get(session_id, []):
            task = self.tasks[task_id]
            result: QueryResult = task["result"]
            output.append({
                "task_id": task_id,
                "query": task["query"],
                "route": result.route,
                "status": result.status,
                "result_title": result.result_title,
                "analysis": result.analysis or "",
                "columns": result.columns,
                "row_count": len(result.rows),
            })
        return output

    def recent_result_context(self, session_id: str) -> str:
        result = self.latest_table_result(session_id)
        if not result:
            return ""
        return json.dumps({
            "task_id": result.task_id,
            "title": result.result_title or "最近查询结果",
            "row_count": len(result.rows),
            "analysis": result.analysis,
            "columns": result.columns,
            "rows": result.rows[: self.table_row_limit],
            "sql": result.sql,
        }, ensure_ascii=False)

    def analysis_context(
        self, session_id: str, workspace: dict[str, Any]
    ) -> tuple[str, list[dict[str, Any]]]:
        payloads: list[dict[str, Any]] = []
        sources: list[dict[str, Any]] = []
        supplied = {
            str(item.get("task_id")): item
            for item in workspace.get("analysis_tables", [])
            if item.get("task_id") and item.get("rows")
        }
        for task_id in list(dict.fromkeys(workspace.get("analysis_table_ids") or []))[:5]:
            provided = supplied.get(task_id)
            if provided:
                source = {
                    "task_id": task_id,
                    "title": str(provided.get("title") or "历史查询结果"),
                    "query": str(provided.get("query") or ""),
                    "row_count": len(provided.get("rows") or []),
                    "columns": list(provided.get("columns") or []),
                }
                sources.append(source)
                payloads.append({
                    **source,
                    "rows": list(provided.get("rows") or [])[: self.table_row_limit],
                })
                continue
            task = self.tasks.get(task_id)
            if not task or task.get("session_id") != session_id:
                continue
            result: QueryResult = task["result"]
            if result.status != "completed" or not result.rows:
                continue
            source = {
                "task_id": task_id,
                "title": result.result_title or "历史查询结果",
                "query": task.get("query", ""),
                "row_count": len(result.rows),
                "columns": result.columns,
            }
            sources.append(source)
            payloads.append({
                **source,
                "analysis": result.analysis,
                "rows": result.rows[: self.table_row_limit],
            })
        return json.dumps(payloads, ensure_ascii=False) if payloads else "", sources

    @staticmethod
    def normalize_workspace(workspace: dict[str, Any] | None) -> dict[str, Any]:
        resolved = dict(workspace or {})
        resolved["schema_fields"] = list(resolved.get("schema_fields") or resolved.get("fields") or [])
        resolved["analysis_table_ids"] = list(dict.fromkeys(resolved.get("analysis_table_ids") or []))
        resolved["analysis_tables"] = list(resolved.get("analysis_tables") or [])
        resolved["confirmed_schema_tables"] = list(dict.fromkeys(resolved.get("confirmed_schema_tables") or []))
        resolved["confirmed_parameters"] = dict(resolved.get("confirmed_parameters") or {})
        return resolved

    @staticmethod
    def query_workspace(workspace: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_fields": list(workspace.get("schema_fields") or []),
            "confirmed_schema_tables": list(workspace.get("confirmed_schema_tables") or []),
            "confirmed_parameters": dict(workspace.get("confirmed_parameters") or {}),
        }

    @staticmethod
    def match_clarification(text: str, result: QueryResult) -> str | None:
        if not result.clarification:
            return None
        normalized = text.strip().lower().replace(" ", "")
        options = result.clarification.options
        for option in options:
            if normalized == option.id.lower().replace(" ", "") or option.label.lower().replace(" ", "") in normalized:
                return option.id
        ordinal = {"第一个": 0, "第一项": 0, "选一": 0, "1": 0, "第二个": 1, "第二项": 1, "选二": 1, "2": 1}
        if normalized in ordinal and ordinal[normalized] < len(options):
            return options[ordinal[normalized]].id
        return None

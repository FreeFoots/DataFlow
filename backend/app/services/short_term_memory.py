from __future__ import annotations

import json
import logging
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Callable

from ..model_client import ModelClient


logger = logging.getLogger(__name__)


class TokenCounter:
    """本地Token估算器，可替换为模型对应的精确Tokenizer。"""

    @staticmethod
    def count(text: str) -> int:
        # 中文字符按一个 Token、ASCII 文本按四字符一个 Token 估算。
        non_ascii = sum(1 for char in text if ord(char) > 127)
        ascii_count = len(text) - non_ascii
        return non_ascii + (ascii_count + 3) // 4


@dataclass
class SessionSummary:
    """单个会话的摘要状态。"""

    text: str = ""
    summarized_ids: set[str] = field(default_factory=set)
    running: bool = False
    future: Future[None] | None = None


class ShortTermMemory:
    """按Token批量压缩旧轮次，保留原始轮次作为可恢复归档。"""

    def __init__(
        self,
        model_client: ModelClient,
        *,
        trigger_tokens: int = 12_000,
        batch_tokens: int = 6_000,
        min_recent_turns: int = 5,
        summary_enabled: bool = True,
        on_summary_updated: Callable[[str, str, set[str]], None] | None = None,
        token_counter: TokenCounter | None = None,
    ) -> None:
        self.model_client = model_client
        self.trigger_tokens = max(1, trigger_tokens)
        self.batch_tokens = max(1, min(batch_tokens, self.trigger_tokens))
        self.min_recent_turns = max(1, min_recent_turns)
        self.summary_enabled = summary_enabled
        self.on_summary_updated = on_summary_updated
        self.token_counter = token_counter or TokenCounter()
        self._states: dict[str, SessionSummary] = {}
        self._lock = Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="memory-summary")

    def restore(
        self, session_id: str, summary: str, summarized_ids: set[str]
    ) -> None:
        """恢复持久化的摘要状态。"""
        with self._lock:
            self._states[session_id] = SessionSummary(
                text=summary,
                summarized_ids=set(summarized_ids),
            )

    def context(self, session_id: str, items: list[dict[str, Any]]) -> str:
        """返回上一版摘要和所有尚未摘要的完整轮次。"""
        with self._lock:
            state = self._states.get(session_id, SessionSummary())
            summary = state.text
            summarized_ids = set(state.summarized_ids)

        pending = [
            self._public_item(item)
            for item in items
            if str(item["task_id"]) not in summarized_ids
        ]
        if not summary and not pending:
            return ""
        return json.dumps(
            {"history_summary": summary, "pending_turns": pending},
            ensure_ascii=False,
        )

    def maybe_schedule(self, session_id: str, items: list[dict[str, Any]]) -> bool:
        """上下文达到Token软阈值后，异步摘要最早的一批完整轮次。"""
        if not self.summary_enabled or len(items) <= self.min_recent_turns:
            return False

        with self._lock:
            state = self._states.setdefault(session_id, SessionSummary())
            if state.running:
                return False

            unsummarized = [
                item
                for item in items
                if str(item["task_id"]) not in state.summarized_ids
            ]
            public_turns = [self._public_item(item) for item in unsummarized]
            total_tokens = self._context_tokens(state.text, public_turns)
            if total_tokens < self.trigger_tokens:
                return False

            # 最近若干完整轮次受到保护，不进入本次摘要批次。
            candidates = unsummarized[: -self.min_recent_turns]
            batch = self._take_complete_turns(candidates)
            if not batch:
                return False

            previous_summary = state.text
            batch_ids = [str(item["task_id"]) for item in batch]
            state.running = True
            state.future = self._executor.submit(
                self._summarize,
                session_id,
                previous_summary,
                batch,
                batch_ids,
            )

        logger.info(
            "short_term_summary_scheduled session_id=%s context_tokens=%s batch_turns=%s",
            session_id,
            total_tokens,
            len(batch),
        )
        return True

    def wait_for_idle(self, session_id: str, timeout: float = 5) -> None:
        """测试和调试使用；正常请求不等待摘要任务。"""
        with self._lock:
            state = self._states.get(session_id)
            future = state.future if state else None
        if future:
            future.result(timeout=timeout)

    def _take_complete_turns(
        self, candidates: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        batch: list[dict[str, Any]] = []
        used_tokens = 0
        for item in candidates:
            # 轮次是不可拆分的最小单位，最后一轮可以略微超过批次目标。
            batch.append(item)
            used_tokens += self._turn_tokens(item)
            if used_tokens >= self.batch_tokens:
                break
        return batch

    def _summarize(
        self,
        session_id: str,
        previous_summary: str,
        batch: list[dict[str, Any]],
        batch_ids: list[str],
    ) -> None:
        system = """你负责增量压缩会话历史。保留用户目标、已确认业务口径、筛选条件、
关键结果、分析结论和未完成事项。输入包含上一版摘要和一批新的旧轮次；请合并为新版摘要。
不要推测缺失信息，不要返回JSON，只输出简洁的摘要文本。"""
        user = json.dumps(
            {
                "previous_summary": previous_summary or "无",
                "turns_to_merge": [self._public_item(item) for item in batch],
            },
            ensure_ascii=False,
        )
        try:
            summary = self.model_client.chat(system, user).strip()
            if not summary:
                raise RuntimeError("摘要模型返回空内容")
            with self._lock:
                state = self._states.setdefault(session_id, SessionSummary())
                state.text = summary
                state.summarized_ids.update(batch_ids)
                summarized_ids = set(state.summarized_ids)
            if self.on_summary_updated:
                self.on_summary_updated(session_id, summary, summarized_ids)
            logger.info(
                "short_term_summary_completed session_id=%s batch_turns=%s",
                session_id,
                len(batch_ids),
            )
        except Exception as exc:
            # 失败时不标记轮次，原始内容继续进入模型上下文。
            logger.warning(
                "short_term_summary_failed session_id=%s error=%s",
                session_id,
                exc,
            )
        finally:
            with self._lock:
                state = self._states.setdefault(session_id, SessionSummary())
                state.running = False

    def _context_tokens(
        self, summary: str, turns: list[dict[str, Any]]
    ) -> int:
        return self.token_counter.count(
            json.dumps(
                {"history_summary": summary, "pending_turns": turns},
                ensure_ascii=False,
            )
        )

    def _turn_tokens(self, item: dict[str, Any]) -> int:
        return self.token_counter.count(
            json.dumps(self._public_item(item), ensure_ascii=False)
        )

    @staticmethod
    def _public_item(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "turn_id": str(item.get("task_id") or ""),
            "user_message": item.get("query", ""),
            "route": item.get("route", ""),
            "status": item.get("status", ""),
            "result_title": item.get("result_title"),
            "assistant_message": str(item.get("analysis") or ""),
            # 短期记忆保留表头和行数，完整表格保存在原始归档中。
            "columns": list(item.get("columns") or []),
            "row_count": int(item.get("row_count") or 0),
        }

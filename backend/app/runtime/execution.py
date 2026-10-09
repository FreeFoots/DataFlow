from __future__ import annotations

import sqlite3
import time
import hashlib
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, Any

from langgraph.checkpoint.sqlite import SqliteSaver

from .store import TaskStore, encoded


CURRENT: ContextVar["Execution | None"] = ContextVar("dataflow_execution", default=None)


class Execution:
    def __init__(self, store: TaskStore, task: dict, owner: str, remaining: float, model_limit: int):
        self.store, self.task, self.owner = store, task, owner
        self.generation = task["generation"]
        self.deadline = time.monotonic() + remaining
        self.model_limit = model_limit
        self.scope = "setup"
        self.ordinal = 0
        self.counts: dict[str, int] = {}

    def check(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("任务执行时间已用尽")
        with self.store.connection() as db:
            self.store.guard(db, self.task["id"], self.owner, self.generation)
        return remaining

    def attempt(self):
        self.check()
        self.store.model_attempt(self.task["id"], self.owner, self.generation, self.model_limit)

    def call(self, kind: str, request: Any, operation: Callable[[], Any]) -> Any:
        self.check()
        digest = hashlib.sha256(encoded(request).encode()).hexdigest()
        scope = f"{self.task.get('resume_key') or 'initial'}:{self.scope}:{kind}:{digest}"
        ordinal = self.counts.get(scope, 0) + 1
        self.counts[scope] = ordinal
        cached, response = self.store.begin_call(self.task["id"], self.owner, self.generation, scope, ordinal, request)
        if cached:
            return response
        response = operation()
        self.check()
        self.store.complete_call(self.task["id"], self.owner, self.generation, scope, ordinal, response)
        return response

    def node(self, name: str, operation: Callable, state):
        self.check()
        self.scope, self.ordinal = name, 0
        self.counts = {}
        self.store.stage(self.task["id"], self.owner, self.generation, name)
        return operation(state)


def durable_call(kind: str, request: Any, operation: Callable[[], Any]) -> Any:
    execution = CURRENT.get()
    return execution.call(kind, request, operation) if execution else operation()


class LeasedSqliteSaver(SqliteSaver):
    """Fence checkpoint writes in the same database transaction as task ownership."""

    def __init__(self, connection: sqlite3.Connection, execution: Execution):
        super().__init__(connection)
        self.execution = execution

    @contextmanager
    def cursor(self, transaction: bool = True):
        with self.lock:
            self.setup()
            cursor = self.conn.cursor()
            try:
                if transaction:
                    cursor.execute("BEGIN IMMEDIATE")
                    TaskStore.guard(self.conn, self.execution.task["id"], self.execution.owner, self.execution.generation)
                yield cursor
                if transaction:
                    self.conn.commit()
            except BaseException:
                if transaction:
                    self.conn.rollback()
                raise
            finally:
                cursor.close()

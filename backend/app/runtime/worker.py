from __future__ import annotations

import hashlib
import logging
import multiprocessing
import sqlite3
import threading
import time
import uuid
import os
from dataclasses import replace
from pathlib import Path

from langgraph.types import Command

from .execution import CURRENT, Execution, LeasedSqliteSaver
from .store import TaskStore, VERSION
from .presentation import failure_message


logger = logging.getLogger(__name__)


def data_version(root: Path, config=None) -> str:
    """Changes to any CSV/schema or runtime source prohibit mixed-version recovery."""
    digest = hashlib.sha256()
    backend = root.parent.parent
    paths = sorted(root.rglob("*.csv")) + sorted(root.rglob("_schema.json")) + sorted(root.rglob("_database_manifest.json"))
    paths += sorted((backend / "app").rglob("*.py")) + sorted((backend / "app" / "skills").rglob("*.md")) + sorted((backend / "app" / "skills").rglob("*.json"))
    for path in paths:
        digest.update(str(path.relative_to(backend)).encode())
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
    if config:
        from .store import encoded
        # Credentials never enter version fingerprints or task records.
        digest.update(encoded({name: getattr(config, name) for name in ["llm_base_url", "llm_model", "llm_thinking_format", "llm_enable_thinking", "llm_max_tokens", "temperature", "embedding_base_url", "embedding_model", "embedding_dimensions", "rerank_base_url", "rerank_model", "schema_recall_threshold", "bm25_top_k", "dense_top_k", "rrf_top_k", "max_schema_fields", "mcp_max_tool_calls", "context_table_row_limit", "route_context_turns", "analysis_max_tool_calls", "analysis_max_decisions", "analysis_max_no_progress", "analysis_allow_exploratory_sql"]}).encode())
    return digest.hexdigest()


def execute_task(path: str, task_id: str, owner: str, generation: int, config, remaining: float):
    """Spawned process; its entire model/SQL execution can be terminated by the supervisor."""
    from ..model_client import ModelClient
    from ..models import QueryResult
    from ..retrieval import SchemaIndex
    from ..services.dataflow_service import DataFlowService
    from ..services.session_context import SessionContext
    from ..security import AccessController
    from ..workflows.query_graph import QueryWorkflow

    store = TaskStore(Path(path))
    task = store.get(task_id)
    if task["generation"] != generation:
        return
    execution = Execution(store, task, owner, remaining, config.task_model_attempt_limit)
    watchdog_stop = threading.Event()
    def watch_lease():
        while not watchdog_stop.wait(.25):
            try:
                execution.check()
            except Exception:
                # Supervisor loss/cancel/deadline must also stop an orphaned SQL/model process.
                os._exit(75)
    threading.Thread(target=watch_lease, name="execution-lease-watchdog", daemon=True).start()
    token = CURRENT.set(execution)
    checkpoint = sqlite3.connect(path, check_same_thread=False, timeout=10)
    checkpoint.row_factory = sqlite3.Row
    checkpoint.execute("PRAGMA synchronous=FULL")
    service = None
    try:
        execution.check()
        if task["runtime_version"] != VERSION or task["data_version"] != data_version(Path(config.database_root), config):
            raise RuntimeError("数据或执行版本已经变化，请重新提交问题")
        # Each process reconstructs history from committed durable results, never from API memory.
        client = ModelClient(config)
        context = SessionContext(client, replace(config, short_term_summary_enabled=False))
        for previous in store.history(task["user_id"], include_hidden=True):
            if previous["id"] == task_id or previous["session_id"] != task["session_id"] or not previous["result"]:
                continue
            result = QueryResult.model_validate(previous["result"])
            context.tasks[result.task_id] = {"query": previous["request"]["query"], "session_id": f"{task['user_id']}:{task['session_id']}", "workspace": previous["request"].get("workspace") or {}, "user_id": task["user_id"], "result": result}
            if result.task_id not in context.session_tasks[f"{task['user_id']}:{task['session_id']}"]:
                context.session_tasks[f"{task['user_id']}:{task['session_id']}"].append(result.task_id)
        service = DataFlowService.__new__(DataFlowService)
        service.context, service.access_controller = context, AccessController()
        scope = service.access_controller.resolve(task["user_id"]).public()
        # Worker-owned index beside its task database; isolated from legacy rebuild APIs.
        index = SchemaIndex(client, config, index_path=store.path.with_name(store.path.stem + "-schema.json"))
        workflow = QueryWorkflow(client, index, config, checkpointer=LeasedSqliteSaver(checkpoint, execution))
        request = task["request"]
        payload = service._payload(task_id, request["query"], f"{task['user_id']}:{task['session_id']}", request.get("workspace") or {}, scope)
        payload["request_mode"] = request.get("mode", "auto")
        payload = store.context(task_id, owner, generation, payload)
        # Current role definitions must match the stored authorization context before reuse.
        if payload["access_scope"] != scope:
            raise PermissionError("任务的数据权限已变化，请重新提交")
        if task["data_version"] != data_version(Path(config.database_root), config):
            raise RuntimeError("数据版本已变化，请重新提交问题")
        snapshot = workflow.graph.get_state(workflow.run_config(task_id))
        if task["resume"]:
            checkpoint_id = (snapshot.config or {}).get("configurable", {}).get("checkpoint_id")
            original_checkpoint = store.resume_checkpoint(task_id, owner, generation, checkpoint_id)
            if snapshot.interrupts and checkpoint_id == original_checkpoint:
                output = workflow.invoke(Command(resume=task["resume"]), task_id)
            elif snapshot.interrupts:
                output = workflow._state_result(snapshot.values, task_id)
            elif snapshot.next:
                output = workflow.invoke(None, task_id)
            elif snapshot.values.get("result"):
                output = workflow._state_result(snapshot.values, task_id)
            else:
                raise RuntimeError("未找到可恢复的补充信息状态")
        elif snapshot.values:
            output = workflow.invoke(None, task_id)
        else:
            output = workflow.invoke(payload, task_id)
        execution.check()
        if task["data_version"] != data_version(Path(config.database_root), config):
            raise RuntimeError("处理期间的数据版本已变化，请重新提交问题")
        store.finish(task_id, owner, generation, output.model_dump(mode="json"))
    except Exception:
        logger.exception("durable_task_failed task_id=%s", task_id)
        # User-facing errors stay generic; detailed provider errors can contain sensitive input.
        try:
            understanding = None
            if "workflow" in locals():
                try:
                    understanding = workflow.graph.get_state(workflow.run_config(task_id)).values.get("request_understanding")
                except Exception:
                    logger.exception("failed_task_context_unavailable task_id=%s", task_id)
            store.stop(task_id, owner, generation, "failed", failure_message(execution.scope), request_understanding=understanding)
        except Exception:
            logger.exception("durable_task_failure_commit_failed task_id=%s", task_id)
    finally:
        watchdog_stop.set()
        checkpoint.close()
        CURRENT.reset(token)


class TaskWorker:
    def __init__(self, store: TaskStore, config, target=execute_task):
        self.store, self.config, self.target = store, config, target
        self.owner = uuid.uuid4().hex
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.process = None

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self.run, name="dataflow-task-worker", daemon=True)
        self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=8)

    @staticmethod
    def terminate(process):
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)
        if process.is_alive():
            process.kill()
            process.join(timeout=2)

    def run(self):
        while not self.stop_event.is_set():
            try:
                task = self.store.claim(self.owner, self.config.task_lease_seconds)
                if task:
                    self.run_one(task)
                    continue
            except Exception:
                logger.exception("task_worker_iteration_failed")
            self.stop_event.wait(.25)

    def run_one(self, task):
        generation, task_id = task["generation"], task["id"]
        remaining = self.config.task_timeout_seconds - task["elapsed_seconds"]
        if remaining <= 0:
            self.store.stop(task_id, self.owner, generation, "timed_out", "任务执行超时，请缩小查询范围后重试")
            return
        process = multiprocessing.get_context("spawn").Process(target=self.target, args=(str(self.store.path), task_id, self.owner, generation, self.config, remaining))
        self.process = process
        started = time.monotonic()
        try:
            process.start()
            while process.is_alive():
                process.join(timeout=.2)
                current = self.store.get(task_id)
                owned = current["generation"] == generation and current["lease_owner"] == self.owner
                if not owned and current["state"] in {"completed", "partial", "failed", "waiting_clarification"}:
                    process.join(timeout=1)
                    if process.is_alive():
                        self.terminate(process)
                    return
                if not owned:
                    self.terminate(process)
                    return
                elapsed = task["elapsed_seconds"] + time.monotonic() - started
                if self.stop_event.is_set() or current["state"] == "cancel_requested" or elapsed >= self.config.task_timeout_seconds:
                    self.terminate(process)
                    self.store.heartbeat(task_id, self.owner, generation, self.config.task_lease_seconds, elapsed)
                    if self.stop_event.is_set():
                        self.store.stop(task_id, self.owner, generation, "queued", "", requeue=True)
                    else:
                        state = "cancelled" if current["state"] == "cancel_requested" else "timed_out"
                        message = "任务已取消" if state == "cancelled" else "任务执行超时，请缩小查询范围后重试"
                        self.store.stop(task_id, self.owner, generation, state, message)
                    return
                if not self.store.heartbeat(task_id, self.owner, generation, self.config.task_lease_seconds, elapsed):
                    self.terminate(process)
                    return
            # Failed child may exit before its final state could be saved. Bound recovery attempts.
            current = self.store.get(task_id)
            if current["state"] in {"running", "cancel_requested"}:
                elapsed = task["elapsed_seconds"] + time.monotonic() - started
                self.store.heartbeat(task_id, self.owner, generation, self.config.task_lease_seconds, elapsed)
                if elapsed >= self.config.task_timeout_seconds:
                    self.store.stop(task_id, self.owner, generation, "timed_out", "任务执行超时，请缩小查询范围后重试")
                else:
                    self.store.stop(task_id, self.owner, generation, "failed", "执行进程异常退出，请重试", requeue=current["recovery_count"] < 2)
        except Exception:
            if process.pid:
                self.terminate(process)
            raise
        finally:
            self.process = None

from __future__ import annotations

from ..models import QueryResult, RequestUnderstanding
from ..querying.analysis_report import present_analysis_result
from .store import TaskStore
from .worker import TaskWorker, data_version
from pathlib import Path
from .presentation import present_failure


class TaskManager:
    def __init__(self, service, config):
        self.service, self.config = service, config
        self.store = TaskStore(config.task_store_file)
        self.worker = TaskWorker(self.store, config)

    def submit(self, user_id: str, request: dict, key: str) -> dict:
        task = self.store.accept(user_id, request, key, data_version(Path(self.config.database_root), self.config), self.config.task_queue_limit)
        return self.public(task)

    def presented_result(self, task: dict) -> QueryResult:
        result = QueryResult.model_validate(task["result"])
        if task["state"] == "failed":
            stage, understanding = self.store.failure_context(task)
            if understanding and not result.request_understanding:
                result.request_understanding = RequestUnderstanding.model_validate(understanding)
                if result.request_understanding.status == "ready":
                    result.standalone_query = result.request_understanding.standard_request
            result = present_failure(result, stage)
        return present_analysis_result(result)

    def public(self, task: dict) -> dict:
        return {"task_id": task["id"], "session_id": task["session_id"], "query": task["request"]["query"],
                "workspace": task["request"].get("workspace") or {}, "state": task["state"], "stage": task["stage"],
                "version": task["version"], "created_at": task["created_at"], "updated_at": task["updated_at"],
                "elapsed_seconds": task["elapsed_seconds"], "recovery_count": task["recovery_count"],
                "analysis_progress": {"plan": task["progress"].get("analysis_plan", []),
                                      "result_count": len(task["progress"].get("result_artifacts", []))} if task.get("progress") else None,
                "result": self.presented_result(task).model_dump(mode="json") if task["result"] and task["state"] not in {"queued", "running", "cancel_requested"} else None}

    def get(self, task_id: str, user_id: str) -> dict:
        task = self.store.get(task_id, user_id)
        self.materialize(task)
        return self.public(task)

    def materialize(self, task):
        """Compatibility: existing saved-result and follow-up APIs see durable results."""
        if not task["result"]:
            return
        context = self.service.context
        result = self.presented_result(task)
        session = f"{task['user_id']}:{task['session_id']}"
        context.tasks[task["id"]] = {"query": task["request"]["query"], "session_id": session,
                                    "workspace": task["request"].get("workspace") or {},
                                    "user_id": task["user_id"], "result": result, "restored": True}
        if task["id"] not in context.session_tasks[session]:
            context.session_tasks[session].append(task["id"])

    def history(self, user_id: str) -> list[dict]:
        tasks = self.store.history(user_id)
        for task in tasks:
            self.materialize(task)
        return [self.public(task) for task in tasks]

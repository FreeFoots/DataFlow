import copy
import json
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import Mock

from app.models import AnalysisReport, QueryResult
from app.runtime.execution import CURRENT, Execution, LeasedSqliteSaver
from app.runtime.manager import TaskManager
from app.runtime.presentation import failure_message, present_failure
from app.runtime.store import TaskStore
from tests.artifact_directory import artifact_directory
from tests.test_request_understanding import CapturingWorkflow, UnderstandingModel, ready


class FailurePresentationTest(unittest.TestCase):
    def manager(self, store):
        manager = TaskManager.__new__(TaskManager)
        manager.store = store
        return manager

    def test_user_receives_natural_language_without_diagnostic_content(self):
        result = QueryResult(task_id="t", status="failed", route="database_query", message="JSON SECRET", analysis="SQL SECRET",
                             execution_log=[{"error": "SECRET"}], tool_calls=[{"error": "SECRET"}], sql="SECRET",
                             warnings=["SECRET"], steps=["SECRET"], route_reason="SECRET", result_title="SECRET",
                             report=AnalysisReport(title="SECRET", markdown="SECRET"))
        original = copy.deepcopy(result.model_dump())
        public = present_failure(result, "prepare_single_database")
        serialized = public.model_dump_json()
        for forbidden in ["JSON", "SQL", "SECRET", "prepare_single_database"]:
            self.assertNotIn(forbidden, serialized)
        self.assertIn("未能整理好查询方案", public.analysis)
        self.assertEqual(result.model_dump(), original)

    def test_unknown_stage_is_never_echoed_and_cancel_timeout_keep_their_meaning(self):
        self.assertNotIn("SECRET", failure_message("SECRET"))
        for reason in ["cancelled", "timed_out"]:
            result = QueryResult(task_id="t", status="failed", route="database_query", message="用户取消或超时", stop_reason=reason)
            self.assertEqual(present_failure(result, "prepare_single_database"), result)

    def test_failed_task_keeps_confirmed_demand_and_private_stage(self):
        with artifact_directory() as directory:
            store = TaskStore(Path(directory)/"runtime.db")
            task = store.accept("user", {"query": "哪个好", "session_id": "s"}, "submit-key", "v")
            claimed = store.claim("worker", 60)
            store.stage(task["id"], "worker", claimed["generation"], "prepare_single_database")
            understanding = {"summary": "看激活效果", "standard_request": "对比2026年4月各渠道7天激活率", "status": "ready", "clarifications": []}
            store.stop(task["id"], "worker", claimed["generation"], "failed", "private JSON SECRET", request_understanding=understanding)
            raw = store.get(task["id"])
            original = copy.deepcopy(raw["result"])
            public = self.manager(store).public(raw)
            self.assertEqual(raw["failure_stage"], "prepare_single_database")
            self.assertNotIn("failure_stage", public)
            self.assertEqual(public["result"]["request_understanding"], understanding)
            self.assertEqual(public["result"]["standalone_query"], understanding["standard_request"])
            self.assertNotIn("SECRET", json.dumps(public))
            self.assertEqual(store.get(task["id"])["result"], original)

    def test_old_failed_task_reads_confirmed_demand_from_checkpoint_without_rewriting_result(self):
        with artifact_directory() as directory:
            path=Path(directory)/"runtime.db"
            store=TaskStore(path)
            task=store.accept("user", {"query": "哪个好", "session_id": "s"}, "submit-key", "v")
            claimed=store.claim("worker", 60)
            with sqlite3.connect(path, check_same_thread=False) as connection:
                connection.row_factory=sqlite3.Row
                execution=Execution(store, claimed, "worker", 60, 10)
                token=CURRENT.set(execution)
                try:
                    flow=CapturingWorkflow(UnderstandingModel(ready()), LeasedSqliteSaver(connection, execution))
                    flow._prepare_single_database=Mock(side_effect=RuntimeError("private JSON SECRET"))
                    flow.graph=flow._compile()
                    with self.assertRaises(RuntimeError):
                        flow.invoke({"task_id":task["id"],"query":"哪个好"}, task["id"])
                    store.stop(task["id"], "worker", claimed["generation"], "failed", "private JSON SECRET")
                finally:
                    CURRENT.reset(token)
            raw=store.get(task["id"])
            original=copy.deepcopy(raw["result"])
            # Emulate a pre-migration record with no persisted failure_stage.
            with store.connection() as db:
                db.execute("UPDATE runtime_tasks SET failure_stage=NULL WHERE id=?",(task["id"],))
            public=self.manager(store).public(store.get(task["id"]))
            self.assertEqual(public["result"]["request_understanding"]["standard_request"], ready()["understanding"]["standard_request"])
            self.assertIn("未能整理好查询方案", public["result"]["analysis"])
            self.assertNotIn("SECRET", json.dumps(public))
            self.assertEqual(store.get(task["id"])["result"], original)

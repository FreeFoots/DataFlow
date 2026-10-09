from __future__ import annotations

import multiprocessing
import sqlite3
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt, Command

from app.config import settings
from app.runtime.store import TaskStore, ConflictError, LeaseLost
from app.runtime.execution import Execution, LeasedSqliteSaver, CURRENT, durable_call
from app.runtime.worker import TaskWorker
from app.security.auth import AuthService
from tests.artifact_directory import artifact_directory


def hanging_child(path, task_id, owner, generation, config, remaining):
    store = TaskStore(Path(path))
    store.stage(task_id, owner, generation, "test_hanging")
    time.sleep(60)


def persistent_step(state):
    value = durable_call("test_tool", {"input": 1}, lambda: {"value": 2})
    option = interrupt({"options": ["yes", "no"]})
    return {"value": value["value"] + (1 if option == "yes" else 0)}


def crash_after_tool(path, task_id, owner, generation):
    import os
    store = TaskStore(Path(path))
    execution = Execution(store, store.get(task_id), owner, 60, 10)
    CURRENT.set(execution)
    execution.scope = "crash_test"
    durable_call("test_tool", {"input": 1}, lambda: {"value": 42})
    os._exit(17)


class DurableRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.directory = artifact_directory()
        self.path = Path(self.directory.__enter__()) / "runtime.db"
        self.store = TaskStore(self.path)
        self.request = {"query": "hello", "session_id": "session", "workspace": {}}

    def tearDown(self):
        self.directory.__exit__(None, None, None)

    def accept(self, key="submission-1", user="demo_growth_ops", request=None):
        return self.store.accept(user, request or self.request, key, "data-v1")

    def claim(self, owner="worker-1", lease=60):
        return self.store.claim(owner, lease)

    def test_acceptance_is_durable_idempotent_and_owned(self):
        task = self.accept()
        restored = TaskStore(self.path)
        self.assertEqual(restored.accept("demo_growth_ops", self.request, "submission-1", "data-v2")["id"], task["id"])
        with self.assertRaises(ConflictError):
            restored.accept("demo_growth_ops", {**self.request, "query": "different"}, "submission-1", "data-v1")
        with self.assertRaises(PermissionError):
            restored.get(task["id"], "demo_channel_ops")
        second = self.accept(user="demo_channel_ops")
        self.assertNotEqual(second["id"], task["id"])
        self.assertEqual(restored.events(task["id"], "demo_growth_ops")[0]["kind"], "accepted")

    def test_same_session_mutations_are_serialized(self):
        self.accept()
        with self.assertRaises(ConflictError):
            self.accept("different-key")

    def test_queue_limit_and_storage_failure_are_not_acceptance(self):
        self.accept()
        with self.assertRaises(ConflictError):
            self.store.accept("demo_channel_ops", self.request, "other-key", "v", limit=1)
        bad = TaskStore.__new__(TaskStore)
        bad.path = self.path.parent  # A directory cannot be opened as a SQLite database.
        with self.assertRaises(sqlite3.Error):
            bad.accept("u", self.request, "some-key", "v")

    def test_expired_lease_cannot_publish_or_extend(self):
        initial = self.accept()
        first = self.claim(lease=.05)
        time.sleep(.07)
        self.assertFalse(self.store.heartbeat(first["id"], "worker-1", first["generation"], 60, 1))
        second = self.claim("worker-2")
        self.assertEqual(second["id"], initial["id"])
        self.assertGreater(second["generation"], first["generation"])
        with self.assertRaises(LeaseLost):
            self.store.finish(first["id"], "worker-1", first["generation"], {"status": "completed"})
        self.store.finish(second["id"], "worker-2", second["generation"], {"task_id": second["id"], "status": "completed", "route": "direct_response", "message": "done"})
        self.assertEqual(TaskStore(self.path).get(second["id"])["state"], "completed")

    def test_cancel_fences_inflight_result(self):
        self.accept()
        task = self.claim()
        self.store.cancel(task["id"], task["user_id"])
        with self.assertRaises(LeaseLost):
            self.store.finish(task["id"], "worker-1", task["generation"], {"status": "completed"})
        self.store.stop(task["id"], "worker-1", task["generation"], "cancelled", "cancelled")
        self.assertEqual(self.store.get(task["id"])["state"], "cancelled")

    def test_completion_wins_before_cancel(self):
        self.accept()
        task = self.claim()
        self.store.finish(task["id"], "worker-1", task["generation"], {"status": "completed"})
        self.assertEqual(self.store.cancel(task["id"], task["user_id"])["state"], "completed")

    def test_clarification_is_versioned_and_idempotent(self):
        self.accept()
        task = self.claim()
        result = {"status": "waiting_clarification", "clarification": {"options": [{"id": "yes"}, {"id": "no"}]}}
        self.store.finish(task["id"], "worker-1", task["generation"], result)
        waiting = self.store.get(task["id"])
        queued = TaskStore(self.path).clarify(task["id"], task["user_id"], "yes", "resume-1", waiting["version"])
        self.assertEqual(queued["state"], "queued")
        self.assertEqual(self.store.clarify(task["id"], task["user_id"], "yes", "resume-1", waiting["version"])["version"], queued["version"])
        with self.assertRaises(ConflictError):
            self.store.clarify(task["id"], task["user_id"], "no", "resume-1", waiting["version"])
        with self.assertRaises(ConflictError):
            self.store.clarify(task["id"], task["user_id"], "yes", "resume-2", waiting["version"])

    def test_saved_tool_response_survives_abrupt_process_exit(self):
        self.accept()
        first = self.claim(lease=10)
        process = multiprocessing.get_context("spawn").Process(target=crash_after_tool, args=(str(self.path), first["id"], "worker-1", first["generation"]))
        process.start(); process.join(timeout=10)
        self.assertEqual(process.exitcode, 17)
        with self.store.connection() as db:
            db.execute("UPDATE runtime_tasks SET lease_until=0 WHERE id=?", (first["id"],))
        second = self.claim("worker-2")
        execution = Execution(self.store, second, "worker-2", 60, 10)
        token = CURRENT.set(execution)
        execution.scope = "crash_test"
        operation = Mock(side_effect=AssertionError("completed tool must not execute twice"))
        try:
            self.assertEqual(durable_call("test_tool", {"input": 1}, operation), {"value": 42})
            operation.assert_not_called()
        finally:
            CURRENT.reset(token)

    def test_checkpoint_pause_survives_reconstruction_and_stale_writer_is_fenced(self):
        self.accept()
        first = self.claim()
        execution = Execution(self.store, first, "worker-1", 60, 10)
        token = CURRENT.set(execution)
        def graph(saver):
            builder = StateGraph(dict)
            builder.add_node("step", persistent_step)
            builder.add_edge(START, "step"); builder.add_edge("step", END)
            return builder.compile(checkpointer=saver)
        config = {"configurable": {"thread_id": first["id"]}}
        conn = sqlite3.connect(self.path, check_same_thread=False); conn.row_factory = sqlite3.Row
        try:
            flow = graph(LeasedSqliteSaver(conn, execution))
            paused = flow.invoke({"value": 0}, config, durability="sync")
            self.assertIn("__interrupt__", paused)
        finally:
            conn.close(); CURRENT.reset(token)
        self.store.stop(first["id"], "worker-1", first["generation"], "queued", "", requeue=True)
        second = self.claim("worker-2")
        execution = Execution(self.store, second, "worker-2", 60, 10)
        token = CURRENT.set(execution)
        conn = sqlite3.connect(self.path, check_same_thread=False); conn.row_factory = sqlite3.Row
        try:
            flow = graph(LeasedSqliteSaver(conn, execution))
            self.assertTrue(flow.get_state(config).interrupts)
            resumed = flow.invoke(Command(resume="yes"), config, durability="sync")
            self.assertEqual(resumed["value"], 3)
            with self.store.connection() as db:
                count = db.execute("SELECT SUM(attempts) FROM runtime_calls").fetchone()[0]
            self.assertEqual(count, 1)
            stale = Execution(self.store, first, "worker-1", 60, 10)
            saver = LeasedSqliteSaver(conn, stale)
            with self.assertRaises(LeaseLost), saver.cursor():
                pass
        finally:
            conn.close(); CURRENT.reset(token)

    def test_persistent_model_attempt_budget_and_token_revocation(self):
        self.accept()
        task = self.claim()
        self.store.model_attempt(task["id"], "worker-1", task["generation"], 1)
        with self.assertRaises(RuntimeError):
            TaskStore(self.path).model_attempt(task["id"], "worker-1", task["generation"], 1)
        first = AuthService(self.store)
        token, _ = first.login("growth", "growth123")
        restored = AuthService(TaskStore(self.path))
        self.assertEqual(restored.authenticate("Bearer " + token).username, "growth")
        restored.logout("Bearer " + token)
        self.assertIsNone(first.authenticate("Bearer " + token))
        with self.store.connection() as db:
            self.assertNotEqual(db.execute("SELECT token_hash FROM runtime_auth").fetchone()[0], token)

    def test_online_backup_preserves_task_events_auth_and_call_results(self):
        task = self.accept()
        claimed = self.claim()
        self.store.begin_call(task["id"], "worker-1", claimed["generation"], "step", 1, {"q": 1})
        self.store.complete_call(task["id"], "worker-1", claimed["generation"], "step", 1, {"answer": 2})
        self.store.save_token("test-token", "growth", 60)
        backup = self.path.with_name("backup.db")
        self.store.backup(backup)
        restored = TaskStore(backup)
        self.assertEqual(restored.get(task["id"])["user_id"], "demo_growth_ops")
        self.assertTrue(restored.events(task["id"], "demo_growth_ops"))
        self.assertEqual(restored.token_user("test-token"), "growth")
        with restored.connection() as db:
            self.assertEqual(db.execute("SELECT response_json FROM runtime_calls").fetchone()[0], '{"answer":2}')
        with self.assertRaises(FileExistsError):
            self.store.backup(backup)

    def test_resume_checkpoint_is_not_replaced_by_new_interrupt(self):
        self.accept()
        task = self.claim()
        self.assertEqual(self.store.resume_checkpoint(task["id"], "worker-1", task["generation"], "checkpoint-original"), "checkpoint-original")
        self.assertEqual(self.store.resume_checkpoint(task["id"], "worker-1", task["generation"], "checkpoint-new"), "checkpoint-original")

    def test_recovery_attempts_are_bounded(self):
        initial = self.accept()
        for index in range(3):
            task = self.claim("worker-"+str(index))
            with self.store.connection() as db:
                db.execute("UPDATE runtime_tasks SET lease_until=0 WHERE id=?", (initial["id"],))
        self.assertIsNone(self.claim("last"))
        self.assertEqual(self.store.get(initial["id"])["state"], "failed")

    def test_supervisor_timeout_terminates_process(self):
        task = self.accept()
        config = replace(settings, task_store_path=str(self.path), task_timeout_seconds=.8, task_lease_seconds=10)
        worker = TaskWorker(self.store, config, target=hanging_child)
        worker.start()
        try:
            end = time.monotonic() + 8
            while self.store.get(task["id"])["state"] not in {"timed_out", "failed"} and time.monotonic() < end:
                time.sleep(.1)
            self.assertEqual(self.store.get(task["id"])["state"], "timed_out")
            self.assertTrue(worker.process is None or not worker.process.is_alive())
        finally:
            worker.close()

    def test_supervisor_cancel_terminates_process(self):
        task = self.accept()
        config = replace(settings, task_store_path=str(self.path), task_timeout_seconds=30, task_lease_seconds=10)
        worker = TaskWorker(self.store, config, target=hanging_child)
        worker.start()
        try:
            end = time.monotonic()+8
            while self.store.get(task["id"])["stage"] != "test_hanging" and time.monotonic()<end:
                time.sleep(.1)
            self.store.cancel(task["id"], task["user_id"])
            while self.store.get(task["id"])["state"] != "cancelled" and time.monotonic()<end:
                time.sleep(.1)
            self.assertEqual(self.store.get(task["id"])["state"], "cancelled")
            self.assertTrue(worker.process is None or not worker.process.is_alive())
        finally:
            worker.close()


if __name__ == "__main__":
    unittest.main()

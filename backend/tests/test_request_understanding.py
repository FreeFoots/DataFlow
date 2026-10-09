import json
import sqlite3
import unittest
from dataclasses import replace
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.models import QueryResult, RequestUnderstanding
from app.config import Settings
from app.errors import PipelineStageError
from app.preprocessing import RequestPreprocessor
from app.runtime.execution import CURRENT, Execution, LeasedSqliteSaver, durable_call
from app.runtime.store import TaskStore, ConflictError
from app.workflows.query_graph import QueryWorkflow
from app.services.session_context import SessionContext
from tests.artifact_directory import artifact_directory


def unclear(question="你更想了解渠道带来了多少新用户，还是用户的激活效果？", options=True):
    return {"action": "database_query", "mode": "analysis",
            "understanding": {"summary": "你想评估渠道表现。", "standard_request": "", "status": "needs_clarification"},
            "clarification": {"question": question, "reason": "评价标准不同，需要看的数据也不同。", "options": [
                {"id": "new_users", "label": "看新增人数", "description": "比较各渠道带来的新增注册人数。"},
                {"id": "activation", "label": "看激活效果", "description": "比较注册后激活的比例，需要确认观察天数。"},
            ] if options else []},
            # These fields must never run before the user's purpose is confirmed.
            "standalone_query": "擅自猜测的需求", "retrieval": {"retrieval_terms": ["猜测指标"]}}


def ready(action="database_query", mode="query", standard="统计2026年4月各渠道新增注册人数，按人数从高到低排序。"):
    return {"action": action, "mode": mode, "confidence": .4,
            "understanding": {"summary": "比较渠道带来的新增人数。", "standard_request": standard, "status": "ready"},
            "standalone_query": "旧的改写", "retrieval": {"retrieval_terms": ["注册时间", "渠道"]}}


class UnderstandingModel:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.inputs = []

    def chat_json(self, system, user):
        def call():
            execution = CURRENT.get()
            if execution:
                execution.attempt()
            self.inputs.append(json.loads(user))
            return self.responses.pop(0)
        return durable_call("understanding_test_model", {"system": system, "user": user}, call)


class CapturingWorkflow(QueryWorkflow):
    """Real routing/interrupt graph; replace downstream execution with a recorder."""
    def __init__(self, model, saver=None):
        self.preprocessor = RequestPreprocessor(model)
        self.checkpointer = saver if saver is not None else InMemorySaver()
        self.executed = []
        self.graph = self._compile()

    def _retrieve_schema(self, state):
        self.executed.append(("query", state["standalone_query"]))
        return {"database_names": ["short_video_ops"], "workflow_mode": "test_query"}

    def _prepare_single_database(self, state):
        return {"clarification": None}

    def _execute_single_database(self, state):
        return {"result": QueryResult(task_id=state["task_id"], status="completed", route="database_query", message="完成").model_dump()}

    def _analysis_initialize(self, state):
        self.executed.append(("analysis", state["standalone_query"]))
        return {"analysis_state": {}, "workflow_mode": "analysis_agent"}

    def _analysis_decide(self, state):
        return {"analysis_route": "final"}

    def _analysis_finalize(self, state):
        return self._execute_single_database(state)

    def _answer_qa(self, state):
        self.executed.append(("qa", state["request_understanding"]["standard_request"]))
        return {"result": QueryResult(task_id=state["task_id"], status="completed", route="data_qa", message="完成").model_dump()}


class RequestUnderstandingTest(unittest.TestCase):
    def payload(self, task="understanding", mode="auto"):
        return {"task_id": task, "query": "哪个渠道好", "request_mode": mode}

    def test_clear_request_runs_without_confirmation_and_uses_standard_request(self):
        model = UnderstandingModel(ready())
        flow = CapturingWorkflow(model)
        result = flow.invoke(self.payload(), "understanding")
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(model.inputs), 1)
        self.assertEqual(flow.executed, [("query", ready()["understanding"]["standard_request"])])
        self.assertEqual(result.request_understanding.status, "ready")
        self.assertEqual(result.request_understanding.clarifications, [])
        # Low confidence alone must not force an extra confirmation.
        self.assertLess(ready()["confidence"], .5)

    def test_ambiguous_request_does_not_retrieve_or_execute_even_in_analysis_mode(self):
        model = UnderstandingModel(unclear())
        flow = CapturingWorkflow(model)
        result = flow.invoke(self.payload(mode="analysis"), "understanding")
        self.assertEqual(result.status, "waiting_clarification")
        self.assertEqual(result.workflow_mode, "request_clarification")
        self.assertTrue(result.clarification.allow_free_text)
        self.assertEqual(flow.executed, [])
        self.assertEqual(result.standalone_query, "")
        self.assertEqual(result.request_understanding.status, "needs_clarification")
        self.assertNotIn("召回字段", " ".join(result.steps))

    def test_free_text_is_merged_with_original_request_before_execution(self):
        model = UnderstandingModel(unclear(), ready(mode="analysis"))
        flow = CapturingWorkflow(model)
        flow.invoke(self.payload(), "understanding")
        answer = "主要看2026年4月带来的新注册，按渠道排名，不比较激活"
        result = flow.invoke(Command(resume={"answer": answer}), "understanding")
        self.assertEqual(model.inputs[-1]["query"], "哪个渠道好")
        self.assertEqual(model.inputs[-1]["clarifications"][0]["answer"], answer)
        self.assertEqual(result.request_understanding.clarifications[0]["answer"], answer)
        self.assertEqual(flow.executed, [("analysis", ready()["understanding"]["standard_request"])])

    def test_selected_direction_and_followup_missing_range_survive_multiple_rounds(self):
        model = UnderstandingModel(unclear(), unclear("你想看哪个时间段？", options=False), ready())
        flow = CapturingWorkflow(model)
        flow.invoke(self.payload(), "understanding")
        waiting = flow.invoke(Command(resume={"option_id": "new_users"}), "understanding")
        self.assertEqual(waiting.status, "waiting_clarification")
        self.assertEqual(flow.executed, [])
        self.assertEqual(waiting.clarification.options, [])
        result = flow.invoke(Command(resume={"answer": "2026年4月"}), "understanding")
        self.assertEqual(len(result.request_understanding.clarifications), 2)
        self.assertEqual(model.inputs[-1]["clarifications"][0]["answer"], "看新增人数")
        self.assertIn("新增注册", model.inputs[-1]["clarifications"][0]["direction"])
        self.assertEqual(model.inputs[-1]["clarifications"][1]["answer"], "2026年4月")

    def test_free_text_cannot_be_submitted_as_empty_both_or_an_unknown_option(self):
        for response in [{"answer": "  "}, {"option_id": "missing"}, {"option_id": "new_users", "answer": "别的"}, {"answer": "x" * 501}]:
            with self.subTest(response=response):
                flow = CapturingWorkflow(UnderstandingModel(unclear()))
                flow.invoke(self.payload(), "understanding")
                with self.assertRaises(ValueError):
                    flow.invoke(Command(resume=response), "understanding")
                self.assertEqual(flow.executed, [])

    def test_qa_receives_the_standard_request_without_querying_new_data(self):
        flow = CapturingWorkflow(UnderstandingModel(ready(action="data_qa", standard="解释上表4月新增下降的渠道贡献，不补查新数据。")))
        result = flow.invoke(self.payload(), "understanding")
        self.assertEqual(result.route, "data_qa")
        self.assertEqual(flow.executed, [("qa", result.request_understanding.standard_request)])

    def test_greeting_does_not_become_an_analysis_when_manual_analysis_is_selected(self):
        flow = CapturingWorkflow(UnderstandingModel({"action": "direct_response", "response": "你好，可以告诉我想了解什么。"}))
        result = flow.invoke(self.payload(mode="analysis"), "understanding")
        self.assertEqual(result.route, "direct_response")
        self.assertEqual(flow.executed, [])

    def test_legacy_natural_language_question_now_pauses_for_a_free_response(self):
        flow = CapturingWorkflow(UnderstandingModel({"action": "direct_response", "response_type": "clarification", "response": "想看新增还是激活？"}))
        result = flow.invoke(self.payload(), "understanding")
        self.assertEqual(result.status, "waiting_clarification")
        self.assertTrue(result.clarification.allow_free_text)

    def test_followup_context_retains_confirmed_request_and_excludes_unconfirmed_draft(self):
        context = SessionContext(UnderstandingModel(), replace(Settings(), session_archive_enabled=False, short_term_summary_enabled=False))
        result = QueryResult(task_id="ready", status="completed", route="database_query", message="完成", rows=[{"新增": 1}],
                             request_understanding=RequestUnderstanding(standard_request="只看2026年4月新增人数，不分析激活或成本。"))
        context.remember("user:s", result, "哪个渠道好", {}, "user")
        recent = json.loads(context.route_context("user:s"))["recent_turns"][-1]
        self.assertEqual(recent["user_message"], "哪个渠道好")
        self.assertEqual(recent["standard_request"], result.request_understanding.standard_request)
        self.assertIn(result.request_understanding.standard_request, context.short_term_context("user:s"))
        self.assertEqual(json.loads(context.recent_result_context("user:s"))["standard_request"], result.request_understanding.standard_request)
        result = QueryResult(task_id="draft", status="waiting_clarification", route="direct_response", message="等补充",
                             request_understanding=RequestUnderstanding(standard_request="尚未确认的激活分析", status="needs_clarification"))
        context.remember("user:s", result, "哪个好", {}, "user")
        self.assertEqual(json.loads(context.route_context("user:s"))["recent_turns"][-1]["standard_request"], "")

    def test_missing_or_duplicate_suggestion_fields_are_rejected(self):
        for change in ["question", "id", "description"]:
            payload = unclear()
            if change == "question":
                payload["clarification"]["question"] = ""
            elif change == "id":
                payload["clarification"]["options"][1]["id"] = "new_users"
            else:
                payload["clarification"]["options"][0]["description"] = ""
            with self.subTest(change=change), self.assertRaises(PipelineStageError):
                RequestPreprocessor(UnderstandingModel(payload)).prepare("哪个渠道好", "")

    def test_durable_free_response_is_owned_versioned_and_idempotent(self):
        with artifact_directory() as directory:
            store = TaskStore(Path(directory) / "runtime.db")
            task = store.accept("user", {"query": "哪个渠道好", "session_id": "s"}, "submission", "v")
            claimed = store.claim("worker", 60)
            result = CapturingWorkflow(UnderstandingModel(unclear())).invoke(self.payload(task["id"]), task["id"])
            store.finish(task["id"], "worker", claimed["generation"], result.model_dump(mode="json"))
            waiting = store.get(task["id"])
            with self.assertRaises(PermissionError):
                store.clarify(task["id"], "other", "", "reply", waiting["version"], "2026年4月")
            queued = store.clarify(task["id"], "user", "", "reply", waiting["version"], "2026年4月")
            self.assertEqual(queued["resume"], {"answer": "2026年4月"})
            self.assertEqual(store.clarify(task["id"], "user", "", "reply", waiting["version"], "2026年4月")["version"], queued["version"])
            with self.assertRaises(ConflictError):
                store.clarify(task["id"], "user", "", "reply", waiting["version"], "2026年3月")

    def test_persistent_checkpoint_restores_question_and_normalizes_response_once(self):
        with artifact_directory() as directory:
            path = Path(directory) / "runtime.db"
            store = TaskStore(path)
            task = store.accept("user", {"query": "哪个渠道好", "session_id": "s"}, "submission", "v")
            claimed = store.claim("worker", 60)
            model = UnderstandingModel(unclear(), ready())
            with sqlite3.connect(path, check_same_thread=False) as connection:
                connection.row_factory = sqlite3.Row
                execution = Execution(store, claimed, "worker", 60, 10)
                token = CURRENT.set(execution)
                try:
                    flow = CapturingWorkflow(model, LeasedSqliteSaver(connection, execution))
                    result = flow.invoke(self.payload(task["id"]), task["id"])
                    store.finish(task["id"], "worker", claimed["generation"], result.model_dump(mode="json"))
                finally:
                    CURRENT.reset(token)
            waiting = store.get(task["id"])
            store.clarify(task["id"], "user", "", "reply", waiting["version"], "2026年4月新增排名")
            claimed = store.claim("restarted-worker", 60)
            with sqlite3.connect(path, check_same_thread=False) as connection:
                connection.row_factory = sqlite3.Row
                execution = Execution(store, claimed, "restarted-worker", 60, 10)
                token = CURRENT.set(execution)
                try:
                    restored = CapturingWorkflow(model, LeasedSqliteSaver(connection, execution))
                    self.assertTrue(restored.graph.get_state(restored.run_config(task["id"])).interrupts)
                    result = restored.invoke(Command(resume=claimed["resume"]), task["id"])
                    store.finish(task["id"], "restarted-worker", claimed["generation"], result.model_dump(mode="json"))
                    saved = store.get(task["id"])
                    self.assertEqual(saved["state"], "completed")
                    self.assertEqual(saved["model_attempts"], 2)
                    self.assertEqual(len(model.inputs), 2)
                    self.assertEqual(result.request_understanding.clarifications[0]["answer"], "2026年4月新增排名")
                finally:
                    CURRENT.reset(token)

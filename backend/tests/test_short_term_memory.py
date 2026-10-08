from __future__ import annotations

import json
import unittest

from app.services.short_term_memory import ShortTermMemory


class SummaryModel:
    def __init__(self) -> None:
        self.users: list[str] = []

    def chat(self, system: str, user: str) -> str:
        self.users.append(user)
        return "用户正在分析销售额，已确认按地区查看。"


def item(index: int, text: str = "销售分析") -> dict:
    return {
        "task_id": f"task-{index}",
        "query": f"{text}-{index}",
        "route": "database_query",
        "status": "completed",
        "result_title": "销售结果",
        "analysis": text,
        "columns": ["地区", "销售额"],
        "row_count": 100,
        "rows": [{"地区": "华东", "销售额": 100}],
    }


class ShortTermMemoryTest(unittest.TestCase):
    def test_below_token_threshold_keeps_all_unsummarized_turns(self) -> None:
        model = SummaryModel()
        memory = ShortTermMemory(
            model,
            trigger_tokens=100_000,
            batch_tokens=6_000,
            min_recent_turns=2,
        )
        items = [item(index) for index in range(5)]

        self.assertFalse(memory.maybe_schedule("s1", items))
        context = json.loads(memory.context("s1", items))

        self.assertEqual(model.users, [])
        self.assertEqual(len(context["pending_turns"]), 5)
        self.assertEqual(context["pending_turns"][0]["columns"], ["地区", "销售额"])
        self.assertEqual(context["pending_turns"][0]["row_count"], 100)
        self.assertNotIn("rows", context["pending_turns"][0])

    def test_summary_uses_whole_turns_and_keeps_recent_turns(self) -> None:
        model = SummaryModel()
        memory = ShortTermMemory(
            model,
            trigger_tokens=1,
            batch_tokens=1,
            min_recent_turns=2,
        )
        items = [item(index, "很长的历史内容") for index in range(5)]

        self.assertTrue(memory.maybe_schedule("s1", items))
        memory.wait_for_idle("s1")
        context = json.loads(memory.context("s1", items))

        pending_ids = [turn["turn_id"] for turn in context["pending_turns"]]
        self.assertEqual(len(model.users), 1)
        self.assertNotIn("task-0", pending_ids)
        self.assertEqual(pending_ids[-2:], ["task-3", "task-4"])

    def test_next_summary_merges_previous_summary_with_new_batch(self) -> None:
        model = SummaryModel()
        memory = ShortTermMemory(
            model,
            trigger_tokens=1,
            batch_tokens=1,
            min_recent_turns=2,
        )
        items = [item(index) for index in range(5)]

        memory.maybe_schedule("s1", items)
        memory.wait_for_idle("s1")
        memory.maybe_schedule("s1", items)
        memory.wait_for_idle("s1")

        second_payload = json.loads(model.users[1])
        self.assertEqual(
            second_payload["previous_summary"],
            "用户正在分析销售额，已确认按地区查看。",
        )
        self.assertEqual(len(second_payload["turns_to_merge"]), 1)


if __name__ == "__main__":
    unittest.main()

from tests.artifact_directory import artifact_directory
import unittest
from pathlib import Path

from app.services.memory_store import MemoryStore


class MemoryStoreTest(unittest.TestCase):
    def test_result_and_field_memories_persist(self) -> None:
        with artifact_directory() as directory:
            path = Path(directory) / "memories.json"
            store = MemoryStore(path=path, limit=20)

            store.save_result(
                "task-1",
                "查询销售额",
                "查询完成",
                "销售额结果",
                ["地区", "销售额"],
                [{"地区": "华东", "销售额": 100}],
            )
            store.save_field("orders_current", "paid_amount", "实付金额", "数值")

            reloaded = MemoryStore(path=path, limit=20).list()

            self.assertEqual(reloaded[0]["kind"], "schema_field")
            self.assertEqual(reloaded[1]["kind"], "result_table")
            self.assertEqual(reloaded[1]["rows"][0]["销售额"], 100)

    def test_memory_can_be_deleted(self) -> None:
        with artifact_directory() as directory:
            path = Path(directory) / "memories.json"
            store = MemoryStore(path=path, limit=20)
            store.save_field("customers", "customer_level", "客户等级", "文本")

            store.delete("field:customers.customer_level")

            self.assertEqual(store.list(), [])

    def test_memories_are_persistent_and_isolated_by_user(self) -> None:
        with artifact_directory() as directory:
            path = Path(directory) / "memories.json"
            store = MemoryStore(path=path, limit=20)
            store.save_field(
                "customers", "customer_level", "客户等级", "文本", "user-a"
            )
            store.save_field(
                "orders_current", "paid_amount", "实付金额", "数值", "user-b"
            )

            reloaded = MemoryStore(path=path, limit=20)

            self.assertEqual(len(reloaded.list("user-a")), 1)
            self.assertEqual(reloaded.list("user-a")[0]["name"], "customer_level")
            self.assertEqual(reloaded.list("user-b")[0]["name"], "paid_amount")


if __name__ == "__main__":
    unittest.main()

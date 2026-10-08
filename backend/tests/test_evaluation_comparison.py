import unittest

from evaluation.run_benchmark import compare_case_result, compare_results, quality_gate_passed


class EvaluationComparisonTest(unittest.TestCase):
    def compare(self, columns, rows, ordered=True):
        return compare_results(columns, rows, ["name", "count"],
            [{"name": "a", "count": 2}, {"name": "b", "count": 1}], ordered)[0]

    def test_rank_order_is_checked(self):
        rows = [{"name": "b", "count": 1}, {"name": "a", "count": 2}]
        self.assertFalse(self.compare(["name", "count"], rows))
        self.assertTrue(self.compare(["name", "count"], rows, ordered=False))

    def test_columns_are_matched_by_name_not_dict_insertion_order(self):
        self.assertTrue(self.compare(["count", "name"],
            [{"count": 2, "name": "a"}, {"count": 1, "name": "b"}]))

    def test_extra_or_wrong_columns_do_not_count_as_correct(self):
        self.assertFalse(self.compare(["name", "count", "extra"],
            [{"name": "a", "count": 2, "extra": 9}, {"name": "b", "count": 1, "extra": 9}]))
        self.assertFalse(self.compare(["name", "wrong_metric"],
            [{"name": "a", "wrong_metric": 2}, {"name": "b", "wrong_metric": 1}]))

    def test_unordered_comparison_preserves_duplicate_counts(self):
        self.assertFalse(self.compare(["name", "count"],
            [{"name": "a", "count": 2}, {"name": "a", "count": 2}], ordered=False))

    def test_quality_gate_rejects_low_accuracy_or_empty_runs(self):
        self.assertTrue(quality_gate_passed({"case_count": 49, "sql_execution_success_rate": .9, "result_accuracy": .8}))
        self.assertFalse(quality_gate_passed({"case_count": 49, "sql_execution_success_rate": .89, "result_accuracy": .85}))
        self.assertFalse(quality_gate_passed({"case_count": 49, "sql_execution_success_rate": .99, "result_accuracy": .79}))
        self.assertFalse(quality_gate_passed({"case_count": 0, "sql_execution_success_rate": 1, "result_accuracy": 1}))

    def test_only_explicit_business_aliases_are_accepted(self):
        case = {"case_id": "GROWTH_OPS-001", "expected_columns": ["month", "value"],
            "expected_rows": [{"month": "2026-04", "value": 1525}], "order_sensitive": True}
        self.assertTrue(compare_case_result(case, ["月份", "new_users"], [{"月份": "2026-04", "new_users": 1525}])[0])
        self.assertFalse(compare_case_result(case, ["month", "activated_users"], [{"month": "2026-04", "activated_users": 1525}])[0])
        self.assertFalse(compare_case_result(case, ["month", "new_users"], [{"month": "2026-04", "new_users": 12000}])[0])

    def test_business_alias_mapping_cannot_hide_extra_or_duplicate_columns(self):
        case = {"case_id": "GROWTH_OPS-001", "expected_columns": ["month", "value"],
            "expected_rows": [{"month": "2026-04", "value": 1525}]}
        self.assertFalse(compare_case_result(case, ["month", "new_users", "value"],
            [{"month": "2026-04", "new_users": 1525, "value": 1525}])[0])


if __name__ == "__main__":
    unittest.main()

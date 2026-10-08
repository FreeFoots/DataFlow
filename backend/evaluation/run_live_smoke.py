"""Run a small real-model HTTP test. Calls may incur provider usage charges."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import time
from typing import Any
import urllib.request
import urllib.error
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.config import BASE_DIR, settings
from evaluation.run_benchmark import compare_case_result, load_cases, CASES_PATH


CASE_IDS = (
    "GROWTH_OPS-001", "GROWTH_OPS-061",
    "CHANNEL_OPS-001", "CHANNEL_OPS-021",
    "CONTENT_OPS-001", "CONTENT_OPS-029",
)
ACCOUNTS = {
    "growth_ops": ("growth", "growth123"),
    "channel_ops": ("channel", "channel123"),
    "content_ops": ("content", "content123"),
}


def request(base: str, path: str, payload: dict | None = None, token: str = "") -> Any:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None,
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=300) as response:
        return json.load(response)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=f"http://{settings.server_host}:{settings.server_port}")
    parser.add_argument("--output", type=Path, default=BASE_DIR / "evaluation/results/live_http_smoke_next.json")
    args = parser.parse_args()
    cases = {c["case_id"]: c for c in load_cases(CASES_PATH, set(), False)}
    results: list[dict[str, Any]] = []
    tokens: dict[str, str] = {}
    for attempt in range(20):
        try:
            health = request(args.base_url, "/api/health")
            break
        except urllib.error.URLError:
            if attempt == 19:
                raise RuntimeError("后端未就绪，请先启动 run.py")
            time.sleep(0.5)
    for role, (username, password) in ACCOUNTS.items():
        login = request(args.base_url, "/api/auth/login", {"username": username, "password": password})
        tokens[role] = login["access_token"]

    def submit(label: str, query: str, session: str, role: str, expected: dict | None = None,
               expected_route: str = "database_query", require_chart: bool = False) -> dict:
        started = time.perf_counter()
        try:
            result = request(args.base_url, "/api/query", {
                "query": query, "session_id": session, "workspace": {},
            }, tokens[role])
            passed = result["status"] == "completed" and result["route"] == expected_route
            reason = "路由与状态符合预期" if passed else "路由或任务状态不符合预期"
            if expected is not None and passed:
                passed, _, reason = compare_case_result(expected, result["columns"], result["rows"])
            if require_chart:
                report = result.get("report") or {}
                passed = passed and bool(report.get("markdown")) and bool(report.get("visualizations"))
                passed = passed and not result.get("tool_calls")
                reason = "报告包含图表，且未查询新数据" if passed else "报告、图表或路由未满足预期"
            record = {"test": label, "query": query, "role": role, "passed": passed,
                      "reason": reason, "elapsed_seconds": round(time.perf_counter() - started, 3),
                      "result": result}
        except Exception as exc:
            record = {"test": label, "query": query, "role": role, "passed": False,
                      "reason": str(exc), "elapsed_seconds": round(time.perf_counter() - started, 3)}
        results.append(record)
        print(f"{label}: passed={record['passed']} time={record['elapsed_seconds']}s reason={record['reason']}", flush=True)
        return record

    prefix = "live-smoke-" + uuid4().hex[:8]
    for case_id in CASE_IDS:
        case = cases[case_id]
        session = prefix + "-" + case_id
        record = submit(case_id, case["query"], session, case["scenario"], case)
        if case_id == "GROWTH_OPS-001" and record["passed"]:
            submit("已有结果报告与柱状图", "基于刚才已有查询结果写一份简短分析报告，并添加一张柱状图，不要查询新数据。",
                   session, "growth_ops", expected_route="data_qa", require_chart=True)
            april = {**case, "expected_rows": [r for r in case["expected_rows"] if r["month"] == "2026-04"]}
            submit("多轮追问改写", "只保留2026年4月，其余查询口径和列保持不变。", session, "growth_ops", april)
    submit("普通交流", "你好，请用一句话介绍你的能力。", prefix + "-greeting", "growth_ops", expected_route="direct_response")
    for token in tokens.values():
        request(args.base_url, "/api/auth/logout", {}, token)
    config = {k: health.get(k) for k in (
        "llm_model", "llm_enable_thinking", "llm_thinking_format", "embedding_model",
        "embedding_dimensions", "rerank_model", "schema_recall_threshold",
    )}
    payload = {
        "recorded_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "base_url": args.base_url, "config": config,
        "coverage": "authenticated_http_routing_retrieval_sql_analysis_report_and_followup",
        "comparison": "strict_order_and_columns_with_explicit_case_display_aliases_v2",
        "summary": {"total": len(results), "passed": sum(r["passed"] for r in results)},
        "tests": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(payload["summary"], ensure_ascii=False), flush=True)
    raise SystemExit(0 if all(r["passed"] for r in results) else 1)


if __name__ == "__main__":
    main()

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from ..clarification import clarification_answer


ACTIVE = {"queued", "running", "cancel_requested"}
TERMINAL = {"completed", "partial", "failed", "cancelled", "timed_out"}
VERSION = "dataflow-runtime-1"


class ConflictError(ValueError):
    pass


class LeaseLost(RuntimeError):
    pass


def encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class TaskStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            try:
                self.path.touch(mode=0o600, exist_ok=False)
            except FileExistsError:
                pass
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runtime_tasks (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, session_id TEXT NOT NULL,
                    submission_key TEXT NOT NULL, input_hash TEXT NOT NULL, request_json TEXT NOT NULL,
                    state TEXT NOT NULL, stage TEXT NOT NULL DEFAULT 'queued', version INTEGER NOT NULL DEFAULT 1,
                    generation INTEGER NOT NULL DEFAULT 0, lease_owner TEXT, lease_until REAL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL, result_json TEXT,
                    context_json TEXT, resume_json TEXT, resume_key TEXT, resume_hash TEXT,
                    resume_checkpoint TEXT,
                    runtime_version TEXT NOT NULL, data_version TEXT NOT NULL,
                    elapsed_seconds REAL NOT NULL DEFAULT 0, model_attempts INTEGER NOT NULL DEFAULT 0,
                    recovery_count INTEGER NOT NULL DEFAULT 0, hidden INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(user_id, submission_key)
                );
                CREATE INDEX IF NOT EXISTS runtime_ready ON runtime_tasks(state, created_at);
                CREATE INDEX IF NOT EXISTS runtime_history ON runtime_tasks(user_id, session_id, created_at);
                CREATE TABLE IF NOT EXISTS runtime_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES runtime_tasks(id),
                    kind TEXT NOT NULL, payload_json TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runtime_calls (
                    task_id TEXT NOT NULL REFERENCES runtime_tasks(id), scope TEXT NOT NULL, ordinal INTEGER NOT NULL,
                    input_hash TEXT NOT NULL, state TEXT NOT NULL, response_json TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(task_id, scope, ordinal)
                );
                CREATE TABLE IF NOT EXISTS runtime_auth (
                    token_hash TEXT PRIMARY KEY, username TEXT NOT NULL, expires_at REAL NOT NULL
                );
                PRAGMA user_version=1;
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(runtime_tasks)")}
            if "failure_stage" not in columns:
                db.execute("ALTER TABLE runtime_tasks ADD COLUMN failure_stage TEXT")
            if "resume_checkpoint" not in columns:
                db.execute("ALTER TABLE runtime_tasks ADD COLUMN resume_checkpoint TEXT")
            if "progress_json" not in columns:
                db.execute("ALTER TABLE runtime_tasks ADD COLUMN progress_json TEXT")
        self.path.chmod(0o600)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(self.path) + suffix)
            if sidecar.exists():
                sidecar.chmod(0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def event(db, task_id: str, kind: str, payload: dict | None = None):
        db.execute("INSERT INTO runtime_events(task_id,kind,payload_json,created_at) VALUES(?,?,?,?)",
                   (task_id, kind, encoded(payload or {}), time.time()))

    def accept(self, user_id: str, request: dict, key: str, data_version: str, limit: int = 32) -> dict:
        canonical = dict(request)
        if canonical.get("mode", "auto") == "auto":
            canonical.pop("mode", None)
        digest = hashlib.sha256(encoded(canonical).encode()).hexdigest()
        now = time.time()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM runtime_tasks WHERE user_id=? AND submission_key=?", (user_id, key)).fetchone()
            if existing:
                if existing["input_hash"] != digest:
                    raise ConflictError("同一提交编号对应了不同内容，请重新提交")
                return self.decode(existing)
            if db.execute("SELECT COUNT(*) FROM runtime_tasks WHERE state IN ('queued','running','cancel_requested')").fetchone()[0] >= limit:
                raise ConflictError("当前任务较多，请稍后提交")
            # A session has one mutable workflow at a time, including a clarification pause.
            busy = db.execute("SELECT id FROM runtime_tasks WHERE user_id=? AND session_id=? AND state IN ('queued','running','cancel_requested','waiting_clarification')",
                              (user_id, request["session_id"])).fetchone()
            if busy:
                raise ConflictError("这个对话还有未完成的任务，请先等待、补充信息或取消")
            task_id = uuid.uuid4().hex
            db.execute("""INSERT INTO runtime_tasks(id,user_id,session_id,submission_key,input_hash,request_json,
                state,created_at,updated_at,runtime_version,data_version) VALUES(?,?,?,?,?,?,'queued',?,?,?,?)""",
                       (task_id, user_id, request["session_id"], key, digest, encoded(request), now, now, VERSION, data_version))
            self.event(db, task_id, "accepted")
            return self.decode(db.execute("SELECT * FROM runtime_tasks WHERE id=?", (task_id,)).fetchone())

    @staticmethod
    def decode(row) -> dict:
        result = dict(row)
        for name in ["request", "result", "context", "resume", "progress"]:
            result[name] = json.loads(result.pop(name + "_json")) if result[name + "_json"] else None
        return result

    def get(self, task_id: str, user_id: str | None = None) -> dict:
        with self.connection() as db:
            row = db.execute("SELECT * FROM runtime_tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise KeyError(task_id)
        if user_id is not None and row["user_id"] != user_id:
            raise PermissionError("无权访问该任务")
        return self.decode(row)

    def history(self, user_id: str, *, include_hidden: bool = False) -> list[dict]:
        with self.connection() as db:
            rows = db.execute("SELECT * FROM runtime_tasks WHERE user_id=? AND (? OR hidden=0) ORDER BY created_at",
                              (user_id, include_hidden)).fetchall()
        return [self.decode(row) for row in rows]

    def events(self, task_id: str, user_id: str, after: int = 0) -> list[dict]:
        self.get(task_id, user_id)
        with self.connection() as db:
            rows = db.execute("SELECT seq,kind,payload_json,created_at FROM runtime_events WHERE task_id=? AND seq>? ORDER BY seq LIMIT 200",
                              (task_id, after)).fetchall()
        return [{"seq": r["seq"], "kind": r["kind"], "payload": json.loads(r["payload_json"]), "created_at": r["created_at"]} for r in rows]

    def claim(self, owner: str, lease_seconds: float) -> dict | None:
        now = time.time()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for row in db.execute("SELECT * FROM runtime_tasks WHERE state IN ('running','cancel_requested') AND lease_until<?", (now,)).fetchall():
                state = "cancelled" if row["state"] == "cancel_requested" else "failed" if row["recovery_count"] >= 2 else "queued"
                db.execute("UPDATE runtime_tasks SET state=?,stage=?,lease_owner=NULL,lease_until=NULL,version=version+1,recovery_count=recovery_count+1 WHERE id=?", (state, state, row["id"]))
                if state in {"cancelled", "failed"}:
                    result = self.stopped_result(row, state, "任务已取消" if state == "cancelled" else "任务多次中断，请重新提交")
                    db.execute("UPDATE runtime_tasks SET result_json=? WHERE id=?", (encoded(result), row["id"]))
                self.event(db, row["id"], "lease_expired", {"state": state})
            # This local SQLite runtime deliberately has one execution slot across API processes.
            if db.execute("SELECT 1 FROM runtime_tasks WHERE state IN ('running','cancel_requested') LIMIT 1").fetchone():
                return None
            row = db.execute("SELECT id FROM runtime_tasks WHERE state='queued' ORDER BY created_at LIMIT 1").fetchone()
            if not row:
                return None
            db.execute("UPDATE runtime_tasks SET state='running',stage='starting',generation=generation+1,lease_owner=?,lease_until=?,updated_at=?,version=version+1 WHERE id=?",
                       (owner, now + lease_seconds, now, row["id"]))
            self.event(db, row["id"], "running")
            return self.decode(db.execute("SELECT * FROM runtime_tasks WHERE id=?", (row["id"],)).fetchone())

    @staticmethod
    def guard(db, task_id: str, owner: str, generation: int):
        row = db.execute("SELECT * FROM runtime_tasks WHERE id=?", (task_id,)).fetchone()
        if not row or row["state"] != "running" or row["lease_owner"] != owner or row["generation"] != generation or row["lease_until"] < time.time():
            raise LeaseLost("任务已停止或执行租约已失效")
        return row

    def heartbeat(self, task_id: str, owner: str, generation: int, seconds: float, elapsed: float) -> bool:
        with self.connection() as db:
            cursor = db.execute("UPDATE runtime_tasks SET lease_until=?,elapsed_seconds=?,updated_at=? WHERE id=? AND lease_owner=? AND generation=? AND state IN ('running','cancel_requested') AND lease_until>?",
                                (time.time() + seconds, elapsed, time.time(), task_id, owner, generation, time.time()))
            return cursor.rowcount == 1

    def stage(self, task_id: str, owner: str, generation: int, stage: str):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.guard(db, task_id, owner, generation)
            if row["stage"] != stage:
                db.execute("UPDATE runtime_tasks SET stage=?,updated_at=? WHERE id=?", (stage, time.time(), task_id))
                self.event(db, task_id, "stage", {"stage": stage})

    def context(self, task_id: str, owner: str, generation: int, payload: dict) -> dict:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.guard(db, task_id, owner, generation)
            if row["context_json"]:
                return json.loads(row["context_json"])
            db.execute("UPDATE runtime_tasks SET context_json=? WHERE id=?", (encoded(payload), task_id))
            return payload

    def finish(self, task_id: str, owner: str, generation: int, result: dict):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self.guard(db, task_id, owner, generation)
            db.execute("UPDATE runtime_tasks SET state=?,stage=?,result_json=?,resume_json=NULL,lease_owner=NULL,lease_until=NULL,updated_at=?,version=version+1 WHERE id=?",
                       (result["status"], result["status"], encoded(result), time.time(), task_id))
            self.event(db, task_id, "result", {"state": result["status"]})

    def progress(self, task_id: str, owner: str, generation: int, result: dict):
        """Keep the last checked analysis evidence even if cancellation kills the child."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self.guard(db, task_id, owner, generation)
            db.execute("UPDATE runtime_tasks SET progress_json=? WHERE id=?", (encoded(result), task_id))

    def stop(self, task_id: str, owner: str, generation: int, state: str, message: str, *, requeue: bool = False,
             request_understanding: dict | None = None):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM runtime_tasks WHERE id=?", (task_id,)).fetchone()
            if not row or row["generation"] != generation or row["lease_owner"] != owner or row["state"] not in {"running", "cancel_requested"} or row["lease_until"] < time.time():
                return
            if row["state"] == "cancel_requested":
                state, requeue = "cancelled", False
            result = None if requeue else encoded(self.stopped_result(row, state, message, request_understanding))
            db.execute("UPDATE runtime_tasks SET state=?,stage=?,result_json=?,failure_stage=?,lease_owner=NULL,lease_until=NULL,updated_at=?,version=version+1,recovery_count=recovery_count+? WHERE id=?",
                       ("queued" if requeue else state, "queued" if requeue else state, result,
                        row["stage"] if state == "failed" and not requeue else None, time.time(), int(requeue), task_id))
            self.event(db, task_id, "requeued" if requeue else state)

    @staticmethod
    def stopped_result(row, state: str, message: str, request_understanding: dict | None = None) -> dict:
        snapshot = json.loads(row["progress_json"]) if row["progress_json"] else {}
        if request_understanding:
            snapshot["request_understanding"] = request_understanding
            if request_understanding.get("status") == "ready":
                snapshot["standalone_query"] = request_understanding.get("standard_request")
        report = snapshot.get("report")
        if report:
            report = {**report, "markdown": message + "\n\n" + report.get("markdown", "")}
        return {**snapshot, "task_id": row["id"], "status": "partial" if snapshot.get("result_artifacts") else "failed",
                "route": "database_query", "message": message, "analysis": message, "report": report,
                "workflow_mode": snapshot.get("workflow_mode", state), "stop_reason": state,
                "analysis_limitations": [*snapshot.get("analysis_limitations", []), message]}

    def failure_context(self, task: dict) -> tuple[str | None, dict | None]:
        """Read old diagnostic stage and confirmed demand without rewriting history."""
        stage = task.get("failure_stage")
        understanding = (task.get("result") or {}).get("request_understanding")
        with self.connection() as db:
            if not stage:
                event = db.execute("SELECT payload_json FROM runtime_events WHERE task_id=? AND kind='stage' ORDER BY seq DESC LIMIT 1", (task["id"],)).fetchone()
                if event:
                    stage = json.loads(event[0]).get("stage")
            if understanding is None and db.execute("SELECT 1 FROM sqlite_master WHERE name='checkpoints'").fetchone():
                checkpoint = db.execute("SELECT type,checkpoint FROM checkpoints WHERE thread_id=? AND checkpoint_ns='' ORDER BY checkpoint_id DESC LIMIT 1", (task["id"],)).fetchone()
                if checkpoint:
                    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
                    values = JsonPlusSerializer().loads_typed((checkpoint[0], checkpoint[1])).get("channel_values", {})
                    understanding = values.get("request_understanding")
        return stage, understanding

    def cancel(self, task_id: str, user_id: str) -> dict:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM runtime_tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise KeyError(task_id)
            if row["user_id"] != user_id:
                raise PermissionError("无权取消该任务")
            if row["state"] not in TERMINAL and row["state"] != "cancel_requested":
                state = "cancel_requested" if row["state"] == "running" else "cancelled"
                db.execute("UPDATE runtime_tasks SET state=?,stage=?,updated_at=?,version=version+1 WHERE id=?", (state, state, time.time(), task_id))
                if state == "cancelled":
                    db.execute("UPDATE runtime_tasks SET result_json=? WHERE id=?", (encoded(self.stopped_result(row, "cancelled", "任务已取消")), task_id))
                self.event(db, task_id, state)
        return self.get(task_id, user_id)

    def clarify(self, task_id: str, user_id: str, option: str, key: str, version: int, answer: str | None = None) -> dict:
        option = option.strip()
        answer = answer.strip() if answer is not None else None
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM runtime_tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise KeyError(task_id)
            if row["user_id"] != user_id:
                raise PermissionError("无权访问该任务")
            digest = hashlib.sha256(encoded([option, version] if answer is None else [option, version, answer]).encode()).hexdigest()
            if row["resume_key"] == key:
                if row["resume_hash"] != digest:
                    raise ConflictError("同一补充编号对应了不同内容")
                return self.decode(row)
            if row["state"] != "waiting_clarification" or row["version"] != version:
                raise ConflictError("任务状态已更新，请刷新后补充")
            result = json.loads(row["result_json"])
            clarification_answer(result.get("clarification") or {}, option, answer)
            response = {"option_id": option} if option else {"answer": answer}
            db.execute("UPDATE runtime_tasks SET state='queued',stage='queued',resume_json=?,resume_key=?,resume_hash=?,resume_checkpoint=NULL,updated_at=?,version=version+1 WHERE id=?",
                       (encoded(response), key, digest, time.time(), task_id))
            self.event(db, task_id, "clarification_accepted")
        return self.get(task_id, user_id)

    def resume_checkpoint(self, task_id: str, owner: str, generation: int, checkpoint_id: str | None) -> str | None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.guard(db, task_id, owner, generation)
            if row["resume_checkpoint"]:
                return row["resume_checkpoint"]
            db.execute("UPDATE runtime_tasks SET resume_checkpoint=? WHERE id=?", (checkpoint_id, task_id))
            return checkpoint_id

    def hide_session(self, user_id: str, session_id: str):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM runtime_tasks WHERE user_id=? AND session_id=? AND state IN ('queued','running','cancel_requested','waiting_clarification')", (user_id, session_id)).fetchone():
                raise ConflictError("请先取消或完成这个对话中的任务")
            db.execute("UPDATE runtime_tasks SET hidden=1 WHERE user_id=? AND session_id=?", (user_id, session_id))

    def begin_call(self, task_id: str, owner: str, generation: int, scope: str, ordinal: int, request: Any) -> tuple[bool, Any]:
        digest = hashlib.sha256(encoded(request).encode()).hexdigest()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self.guard(db, task_id, owner, generation)
            row = db.execute("SELECT * FROM runtime_calls WHERE task_id=? AND scope=? AND ordinal=?", (task_id, scope, ordinal)).fetchone()
            if row and row["input_hash"] != digest:
                raise ConflictError("恢复调用的参数与原记录不一致，已停止任务")
            if row and row["state"] == "completed":
                return True, json.loads(row["response_json"])
            if row:
                self.event(db, task_id, "unknown_call_replayed", {"scope": scope, "ordinal": ordinal})
            db.execute("INSERT INTO runtime_calls(task_id,scope,ordinal,input_hash,state,attempts) VALUES(?,?,?,?,'started',1) ON CONFLICT(task_id,scope,ordinal) DO UPDATE SET attempts=attempts+1,state='started'",
                       (task_id, scope, ordinal, digest))
            return False, None

    def complete_call(self, task_id: str, owner: str, generation: int, scope: str, ordinal: int, response: Any):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self.guard(db, task_id, owner, generation)
            db.execute("UPDATE runtime_calls SET state='completed',response_json=? WHERE task_id=? AND scope=? AND ordinal=?", (encoded(response), task_id, scope, ordinal))

    def model_attempt(self, task_id: str, owner: str, generation: int, maximum: int):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.guard(db, task_id, owner, generation)
            if row["model_attempts"] >= maximum:
                raise RuntimeError("本次任务已达到模型请求预算，请缩小问题范围后重试")
            db.execute("UPDATE runtime_tasks SET model_attempts=model_attempts+1 WHERE id=?", (task_id,))

    def save_token(self, token: str, username: str, lifetime: float):
        with self.connection() as db:
            db.execute("INSERT INTO runtime_auth VALUES(?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), username, time.time()+lifetime))

    def token_user(self, token: str) -> str | None:
        with self.connection() as db:
            row = db.execute("SELECT username FROM runtime_auth WHERE token_hash=? AND expires_at>?", (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
        return row[0] if row else None

    def revoke_token(self, token: str):
        with self.connection() as db:
            db.execute("UPDATE runtime_auth SET expires_at=0 WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))

    def backup(self, destination: Path):
        """SQLite online backup includes committed WAL records and checkpoints."""
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.touch(mode=0o600, exist_ok=False)
        source = sqlite3.connect(self.path, timeout=10)
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("备份完整性检查失败")
        finally:
            target.close()
            source.close()

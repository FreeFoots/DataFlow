from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from threading import Lock
from typing import Any


class SessionArchive:
    """持久化保存完整会话轮次和摘要状态。"""

    def __init__(self, path: Path, *, enabled: bool = True) -> None:
        self.path = path
        self.enabled = enabled
        self._lock = Lock()
        if enabled:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize()

    def save_turn(
        self,
        task_id: str,
        session_id: str,
        query: str,
        workspace: dict[str, Any],
        result: dict[str, Any],
        user_id: str | None = None,
    ) -> None:
        if not self.enabled:
            return
        with self._lock, closing(self._connect()) as connection, connection:
            existing = connection.execute(
                "SELECT user_id, session_id FROM session_turns WHERE task_id = ?", (task_id,)
            ).fetchone()
            if existing and (existing[0] != user_id or existing[1] != session_id):
                raise PermissionError("不能修改已有任务的归属")
            connection.execute(
                """
                INSERT INTO session_turns (
                    task_id, session_id, query, workspace_json, result_json, user_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    query = excluded.query,
                    workspace_json = excluded.workspace_json,
                    result_json = excluded.result_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    task_id,
                    session_id,
                    query,
                    json.dumps(workspace, ensure_ascii=False),
                    json.dumps(result, ensure_ascii=False),
                    user_id,
                ),
            )

    def load_turns(self) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        with self._lock, closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT task_id, session_id, query, workspace_json, result_json, user_id
                FROM session_turns
                ORDER BY created_at, rowid
                """
            ).fetchall()
        return [
            {
                "task_id": row[0],
                "session_id": row[1],
                "query": row[2],
                "workspace": json.loads(row[3]),
                "result": json.loads(row[4]),
                "user_id": row[5],
            }
            for row in rows
        ]

    def save_message(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        task_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if not self.enabled:
            return
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO session_messages (
                    session_id, task_id, role, content, metadata_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    task_id,
                    role,
                    content,
                    json.dumps(metadata or {}, ensure_ascii=False),
                ),
            )

    def load_messages(self, session_id: str) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        with self._lock, closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT task_id, role, content, metadata_json
                FROM session_messages
                WHERE session_id = ?
                ORDER BY message_id
                """,
                (session_id,),
            ).fetchall()
        return [
            {
                "task_id": row[0],
                "role": row[1],
                "content": row[2],
                "metadata": json.loads(row[3]),
            }
            for row in rows
        ]

    def save_summary(
        self, session_id: str, summary: str, summarized_ids: set[str]
    ) -> None:
        if not self.enabled:
            return
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO session_summaries (
                    session_id, summary, summarized_ids_json
                ) VALUES (?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    summary = excluded.summary,
                    summarized_ids_json = excluded.summarized_ids_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    session_id,
                    summary,
                    json.dumps(sorted(summarized_ids), ensure_ascii=False),
                ),
            )

    def load_summaries(self) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        with self._lock, closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT session_id, summary, summarized_ids_json FROM session_summaries"
            ).fetchall()
        return [
            {
                "session_id": row[0],
                "summary": row[1],
                "summarized_ids": set(json.loads(row[2])),
            }
            for row in rows
        ]

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS session_turns (
                    task_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    query TEXT NOT NULL,
                    workspace_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    user_id TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_session_turns_session
                    ON session_turns(session_id, created_at);

                CREATE TABLE IF NOT EXISTS session_messages (
                    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    task_id TEXT,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_session_messages_session
                    ON session_messages(session_id, message_id);

                CREATE TABLE IF NOT EXISTS session_summaries (
                    session_id TEXT PRIMARY KEY,
                    summary TEXT NOT NULL,
                    summarized_ids_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            # Preserve legacy rows. Missing ownership is intentionally NOT inferred
            # from a session prefix or default account when restoring old archives.
            columns = {row[1] for row in connection.execute("PRAGMA table_info(session_turns)")}
            if "user_id" not in columns:
                connection.execute("ALTER TABLE session_turns ADD COLUMN user_id TEXT")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

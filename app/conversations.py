"""Local chat persistence, deliberately separate from the read-only travel database."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class ConversationError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 409):
        self.code, self.message, self.status_code = code, message, status_code
        super().__init__(message)


def interruption_result() -> dict:
    return {"status": "error", "answer": "پردازش این پیام قطع شد؛ می‌توانید دوباره تلاش کنید.",
            "error": {"code": "request_interrupted", "message": "Request interrupted before completion."},
            "route": None, "sources": [], "sql_results": [], "calculations": [], "trace": [], "metrics": {}}


class ConversationStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS turns (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    request_key_hash TEXT NOT NULL, question TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','completed')),
                    created_at TEXT NOT NULL,
                    UNIQUE(conversation_id, request_key_hash)
                );
                CREATE TABLE IF NOT EXISTS messages (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    turn_id TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK(role IN ('user','assistant')),
                    content TEXT NOT NULL, created_at TEXT NOT NULL, result_json TEXT,
                    UNIQUE(turn_id, role)
                );
                CREATE INDEX IF NOT EXISTS messages_conversation_order
                    ON messages(conversation_id, sequence);
            """)

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _require(connection, conversation_id):
        row = connection.execute("SELECT * FROM conversations WHERE id=?", (conversation_id,)).fetchone()
        if row is None:
            raise ConversationError("conversation_not_found", "Conversation not found.", 404)
        return row

    def create(self, title: str | None = None) -> dict:
        identity, created = uuid4().hex, now_iso()
        title = (title or "").strip()[:120] or "گفتگوی جدید"
        with self._connect() as connection:
            connection.execute("INSERT INTO conversations VALUES (?,?,?,?)", (identity, title, created, created))
        return {"id": identity, "title": title, "created_at": created, "updated_at": created, "message_count": 0}

    def list(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute("""SELECT c.*, COUNT(m.id) AS message_count FROM conversations c
                LEFT JOIN messages m ON m.conversation_id=c.id GROUP BY c.id
                ORDER BY c.updated_at DESC, c.id""").fetchall()
        return [dict(row) for row in rows]

    def get(self, conversation_id: str) -> dict:
        with self._connect() as connection:
            conversation = dict(self._require(connection, conversation_id))
            rows = connection.execute("""SELECT id,role,content,created_at,result_json FROM messages
                WHERE conversation_id=? ORDER BY sequence""", (conversation_id,)).fetchall()
        messages = []
        for row in rows:
            message = dict(row)
            raw = message.pop("result_json")
            message["result"] = json.loads(raw) if raw else None
            messages.append(message)
        return {**conversation, "message_count": len(messages), "messages": messages}

    def delete(self, conversation_id: str):
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require(connection, conversation_id)
            if connection.execute("SELECT 1 FROM turns WHERE conversation_id=? AND status='pending'",
                                  (conversation_id,)).fetchone():
                raise ConversationError("conversation_busy", "This conversation has an active message.")
            connection.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))

    def clear(self):
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM turns WHERE status='pending'").fetchone():
                raise ConversationError("conversation_busy", "A conversation has an active message.")
            connection.execute("DELETE FROM conversations")

    def begin_turn(self, conversation_id: str, question: str, request_key: str | None = None) -> dict:
        """Reserve one turn atomically; replay a completed idempotency key without running tools."""
        key_hash = hashlib.sha256((request_key or uuid4().hex).encode()).hexdigest()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            conversation = self._require(connection, conversation_id)
            previous = connection.execute("SELECT * FROM turns WHERE conversation_id=? AND request_key_hash=?",
                                          (conversation_id, key_hash)).fetchone()
            if previous is not None:
                if previous["question"] != question:
                    raise ConversationError("idempotency_conflict", "This idempotency key belongs to another message.")
                if previous["status"] == "pending":
                    raise ConversationError("conversation_busy", "This message is already being processed.")
                saved = connection.execute("SELECT result_json FROM messages WHERE turn_id=? AND role='assistant'",
                                           (previous["id"],)).fetchone()
                return {"turn_id": previous["id"], "replay": json.loads(saved["result_json"])}
            if connection.execute("SELECT 1 FROM turns WHERE conversation_id=? AND status='pending'",
                                  (conversation_id,)).fetchone():
                raise ConversationError("conversation_busy", "This conversation has an active message.")
            turn_id, user_id, created = uuid4().hex, uuid4().hex, now_iso()
            connection.execute("INSERT INTO turns VALUES (?,?,?,?,?,?)",
                               (turn_id, conversation_id, key_hash, question, "pending", created))
            connection.execute("""INSERT INTO messages(id,conversation_id,turn_id,role,content,created_at)
                VALUES (?,?,?,'user',?,?)""", (user_id, conversation_id, turn_id, question, created))
            title = question[:80] if conversation["title"] == "گفتگوی جدید" else conversation["title"]
            connection.execute("UPDATE conversations SET title=?,updated_at=? WHERE id=?", (title, created, conversation_id))
        return {"turn_id": turn_id, "user_message_id": user_id, "replay": None}

    def context(self, conversation_id: str, *, exclude_turn_id: str | None = None) -> dict:
        with self._connect() as connection:
            self._require(connection, conversation_id)
            rows = connection.execute("""SELECT role,content,result_json FROM messages
                WHERE conversation_id=? AND (? IS NULL OR turn_id != ?)
                ORDER BY sequence DESC LIMIT 10""", (conversation_id, exclude_turn_id, exclude_turn_id)).fetchall()
            state = connection.execute("""SELECT json_extract(result_json,'$.conversation_state') AS value
                FROM messages WHERE conversation_id=? AND role='assistant'
                AND (? IS NULL OR turn_id != ?) AND json_type(result_json,'$.conversation_state')='object'
                ORDER BY sequence DESC LIMIT 1""", (conversation_id, exclude_turn_id, exclude_turn_id)).fetchone()
            options = connection.execute("""SELECT json_extract(result_json,'$.offered_options') AS value
                FROM messages WHERE conversation_id=? AND role='assistant'
                AND (? IS NULL OR turn_id != ?) AND json_type(result_json,'$.offered_options')='array'
                ORDER BY sequence DESC LIMIT 1""", (conversation_id, exclude_turn_id, exclude_turn_id)).fetchone()
        # Failed/interrupted turns contain no replacement state. Explicit empty
        # objects/arrays are replacements; no historical option identity is guessed.
        latest_state = json.loads(state["value"]) if state else {}
        latest_options = json.loads(options["value"]) if options else []
        return {"history": [{"role": row["role"], "content": row["content"][:4000]} for row in reversed(rows)],
                "conversation_state": latest_state, "offered_options": latest_options}

    def complete_turn(self, turn_id: str, result: dict) -> dict:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            turn = connection.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()
            if turn is None:
                raise ConversationError("turn_not_found", "Message turn not found.", 404)
            saved = connection.execute("SELECT result_json FROM messages WHERE turn_id=? AND role='assistant'",
                                       (turn_id,)).fetchone()
            if saved:
                return json.loads(saved["result_json"])
            user = connection.execute("SELECT id FROM messages WHERE turn_id=? AND role='user'", (turn_id,)).fetchone()
            assistant_id, created = uuid4().hex, now_iso()
            result = {**result, "conversation_id": turn["conversation_id"], "user_message_id": user["id"],
                      "assistant_message_id": assistant_id}
            content = str(result.get("answer") or (result.get("error") or {}).get("message") or "پردازش پایان یافت.")
            connection.execute("""INSERT INTO messages(id,conversation_id,turn_id,role,content,created_at,result_json)
                VALUES (?,?,?,'assistant',?,?,?)""", (assistant_id, turn["conversation_id"], turn_id, content, created,
                    json.dumps(result, ensure_ascii=False, allow_nan=False)))
            connection.execute("UPDATE turns SET status='completed' WHERE id=?", (turn_id,))
            connection.execute("UPDATE conversations SET updated_at=? WHERE id=?", (created, turn["conversation_id"]))
            return result

    def recover_interrupted(self) -> int:
        """Single-process demo startup: finish turns abandoned by a previous server process."""
        with self._connect() as connection:
            pending = [row["id"] for row in connection.execute("SELECT id FROM turns WHERE status='pending'")]
        for turn_id in pending:
            self.complete_turn(turn_id, interruption_result())
        return len(pending)

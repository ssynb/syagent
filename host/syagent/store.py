"""SQLite store: the single source of truth for conversations, messages, scores and host state."""

import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

TITLE_LENGTH = 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id), seq INTEGER NOT NULL,
    role TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, files TEXT NOT NULL,
    target_message_id TEXT, score INTEGER, status TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS messages_by_conversation ON messages(conversation_id, seq);
CREATE INDEX IF NOT EXISTS messages_by_target ON messages(target_message_id);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

# An agent reply's score is the latest non-null score of the feedback targeting it.
MESSAGE_COLUMNS = """id, role, kind, text, files, target_message_id, status, created_at,
    CASE WHEN role = 'agent' THEN (SELECT f.score FROM messages f WHERE f.target_message_id = m.id
        AND f.score IS NOT NULL ORDER BY f.seq DESC LIMIT 1) ELSE score END AS score"""


def now() -> str:
    return datetime.now(UTC).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("PRAGMA journal_mode=WAL;" + SCHEMA)
        self.lock = threading.Lock()

    def _query(self, sql: str, args=()) -> list[sqlite3.Row]:
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def create_conversation(self) -> str:
        cid, ts = new_id(), now()
        self._query("INSERT INTO conversations VALUES (?, '', ?, ?)", (cid, ts, ts))
        return cid

    def conversation_exists(self, cid: str) -> bool:
        return bool(self._query("SELECT 1 FROM conversations WHERE id = ?", (cid,)))

    def conversations(self) -> list[dict]:
        rows = self._query("SELECT id, title, updated_at FROM conversations ORDER BY updated_at DESC")
        return [dict(r) for r in rows]

    def add_message(self, cid: str, mid: str, role: str, kind: str, text: str, files: list[str],
                    status: str, target_message_id: str | None = None, score: int | None = None) -> None:
        ts = now()
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO messages SELECT ?, ?, COALESCE(MAX(seq), 0) + 1, ?, ?, ?, ?, ?, ?, ?, ? "
                "FROM messages WHERE conversation_id = ?",
                (mid, cid, role, kind, text, json.dumps(files), target_message_id, score, status, ts, cid),
            )
            title = text.strip().split("\n")[0][:TITLE_LENGTH] if role == "owner" else ""
            self.db.execute(
                "UPDATE conversations SET updated_at = ?, title = CASE WHEN title = '' THEN ? ELSE title END "
                "WHERE id = ?", (ts, title, cid),
            )

    def message(self, mid: str) -> dict | None:
        rows = self._query("SELECT conversation_id, role FROM messages WHERE id = ?", (mid,))
        return dict(rows[0]) if rows else None

    def messages(self, cid: str) -> list[dict]:
        rows = self._query(
            f"SELECT {MESSAGE_COLUMNS} FROM messages m WHERE conversation_id = ? ORDER BY seq", (cid,))
        return [{**dict(r), "files": json.loads(r["files"])} for r in rows]

    def export(self) -> dict:
        """records.json for the agent: every conversation with its messages (scores live on feedback)."""
        return {"conversations": [
            {"id": c["id"], "title": c["title"], "messages": self.messages(c["id"])}
            for c in self._query("SELECT id, title FROM conversations ORDER BY created_at")
        ]}

    def get_meta(self, key: str) -> str | None:
        rows = self._query("SELECT value FROM meta WHERE key = ?", (key,))
        return rows[0]["value"] if rows else None

    def set_meta(self, key: str, value: str) -> None:
        self._query("INSERT INTO meta VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value))

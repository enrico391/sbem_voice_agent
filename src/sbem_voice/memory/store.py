"""Small SQLite memory store; no inference or provider dependencies."""

import sqlite3
from contextlib import closing
from pathlib import Path


class MemoryStore:
    """Bind all operations to one user; connections belong to each operation."""

    def __init__(self, path: Path, user_id: str):
        self.path = Path(path)
        self.user_id = self._text(user_id, "User ID", 128)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    search_text TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    UNIQUE(user_id, content)
                )"""
            )

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _text(value, name, maximum):
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
            raise ValueError(f"{name} must contain 1 to {maximum} characters")
        return value.strip()

    @staticmethod
    def _integer(value, name, minimum, maximum):
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
        return value

    def _record(self, connection, memory_id):
        record = connection.execute(
            "SELECT id, content, created_at, updated_at FROM memories WHERE user_id=? AND id=?",
            (self.user_id, memory_id),
        ).fetchone()
        return dict(record) if record is not None else None

    def remember(self, content: str):
        content = self._text(content, "Memory", 2000)
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """INSERT INTO memories (user_id, content, search_text) VALUES (?, ?, ?)
                   ON CONFLICT(user_id, content) DO NOTHING""",
                (self.user_id, content, content.casefold()),
            )
            record_id = connection.execute(
                "SELECT id FROM memories WHERE user_id=? AND content=?", (self.user_id, content)
            ).fetchone()["id"]
            return {
                "status": "remembered" if cursor.rowcount else "already_exists",
                "memory": self._record(connection, record_id),
            }

    def retrieve(self, query: str = "", limit: int = 20, offset: int = 0):
        if not isinstance(query, str) or len(query) > 2000:
            raise ValueError("Query must be a string of at most 2000 characters")
        self._integer(limit, "Limit", 1, 50)
        self._integer(offset, "Offset", 0, 2**63 - 1)
        with closing(self._connect()) as connection:
            records = connection.execute(
                """SELECT id, content, created_at, updated_at FROM memories
                   WHERE user_id=? AND instr(search_text, ?) > 0
                   ORDER BY id LIMIT ? OFFSET ?""",
                (self.user_id, query.strip().casefold(), limit + 1, offset),
            ).fetchall()
        return {
            "status": "ok",
            "memories": [dict(record) for record in records[:limit]],
            "has_more": len(records) > limit,
            "next_offset": offset + limit if len(records) > limit else None,
        }

    def update(self, memory_id: int, content: str):
        self._integer(memory_id, "Memory ID", 1, 2**63 - 1)
        content = self._text(content, "Memory", 2000)
        with closing(self._connect()) as connection, connection:
            try:
                cursor = connection.execute(
                    """UPDATE memories SET content=?, search_text=?,
                       updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                       WHERE user_id=? AND id=?""",
                    (content, content.casefold(), self.user_id, memory_id),
                )
            except sqlite3.IntegrityError:
                return {"status": "already_exists", "reason": "Another memory has this content"}
            if not cursor.rowcount:
                return {"status": "not_found", "memory_id": memory_id}
            return {"status": "updated", "memory": self._record(connection, memory_id)}

    def remove(self, memory_id: int):
        self._integer(memory_id, "Memory ID", 1, 2**63 - 1)
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                "DELETE FROM memories WHERE user_id=? AND id=?", (self.user_id, memory_id)
            )
            return {
                "status": "removed" if cursor.rowcount else "not_found",
                "memory_id": memory_id,
            }

"""Accounts, sessions, and each user's conversations.

Mixed into ResearchStore, so this lives in the same SQLite file on the host
computer. Tables are created with CREATE IF NOT EXISTS and columns with ADD
COLUMN, so an existing database is upgraded in place and nothing is removed.

Timestamps are Unix seconds (UTC by definition), like the rest of the store.
All SQL uses bound parameters; no value from a request is ever put into SQL text.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid

AUTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at REAL NOT NULL, updated_at REAL NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,                       -- the session id itself is never stored
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf_token TEXT NOT NULL,
    created_at REAL NOT NULL, expires_at REAL NOT NULL, last_activity_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    user_id TEXT REFERENCES users(id) ON DELETE CASCADE,     -- NULL only for chats from before accounts existed
    project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    memory_excluded INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS conversations_user ON conversations(user_id, project_id, updated_at);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content TEXT NOT NULL,
    meta TEXT NOT NULL DEFAULT '{}',                   -- display details: evidence, citations, attachments ...
    created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS messages_conversation ON messages(conversation_id, id);
"""

# Ownership columns added to tables that existed before accounts.
OWNER_COLUMNS = {
    "projects": {"user_id": "TEXT REFERENCES users(id) ON DELETE CASCADE"},
    "memories": {"user_id": "TEXT REFERENCES users(id) ON DELETE CASCADE"},
    "members": {"user_id": "TEXT REFERENCES users(id) ON DELETE CASCADE"},
}

ROLES_OF_MESSAGES = ("user", "assistant", "system")
DEFAULT_TITLE = "New chat"


class Duplicate(Exception):
    """Registration clashed with an existing account; `fields` names what is taken."""

    def __init__(self, fields: list[str]):
        super().__init__(", ".join(fields))
        self.fields = fields


def title_from(text: str) -> str:
    """A conversation title from its first message, without asking the model for one."""
    return " ".join(text.split())[:60] or DEFAULT_TITLE


class AuthMixin:
    def _init_auth(self, db: sqlite3.Connection) -> None:
        db.executescript(AUTH_SCHEMA)
        for table, columns in OWNER_COLUMNS.items():
            have = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
            for column, ddl in columns.items():
                if column not in have:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        db.execute("CREATE INDEX IF NOT EXISTS projects_user ON projects(user_id)")
        self._migrate_chats(db)

    def _migrate_chats(self, db: sqlite3.Connection) -> None:
        """Copy chats saved before accounts existed into conversations and messages, once.

        The old `chats` table is left untouched as a backup. The copies have no
        owner until the first account is created, which then takes them over.
        """
        if db.execute("SELECT 1 FROM settings WHERE key = 'chats_migrated'").fetchone():
            return
        for chat in db.execute("SELECT * FROM chats").fetchall():
            if db.execute("SELECT 1 FROM conversations WHERE id = ?", (chat["id"],)).fetchone():
                continue
            try:
                old = json.loads(chat["messages"])
            except ValueError:
                old = []
            updated = chat["updated_at"] / 1000 if chat["updated_at"] > 1e11 else chat["updated_at"]   # was milliseconds
            db.execute("INSERT INTO conversations (id, user_id, project_id, title, memory_excluded, created_at, "
                       "updated_at) VALUES (?, NULL, ?, ?, ?, ?, ?)",
                       (chat["id"], chat["project_id"], chat["title"], chat["memory_excluded"], updated, updated))
            for message in old:
                if not isinstance(message, dict) or not isinstance(message.get("content"), str):
                    continue
                role = message.get("role") if message.get("role") in ROLES_OF_MESSAGES else "system"
                meta = {k: v for k, v in message.items() if k not in ("role", "content", "time", "node", "id")}
                sent = message.get("time")
                created = sent / 1000 if isinstance(sent, (int, float)) and sent > 1e11 else updated
                db.execute("INSERT INTO messages (conversation_id, role, content, meta, created_at) VALUES (?,?,?,?,?)",
                           (chat["id"], role, message["content"], json.dumps(meta), created))
            self._index_conversation(db, chat["id"])
        db.execute("INSERT INTO settings (key, value) VALUES ('chats_migrated', '1')")

    # ---------- users ----------
    @staticmethod
    def _user(row: sqlite3.Row | None) -> dict | None:
        """A user as the application sees it. The password hash is deliberately not included."""
        if not row:
            return None
        return {"id": row["id"], "username": row["username"], "email": row["email"],
                "created_at": row["created_at"], "is_active": bool(row["is_active"])}

    def count_users(self) -> int:
        with self._db() as db:
            return db.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def create_user(self, username: str, email: str, password_hash: str) -> dict:
        """Add an account. Raises Duplicate if the username or email is taken.

        The very first account takes over everything saved before accounts
        existed (projects, conversations, memories), so no earlier work is lost.
        """
        user_id, now = uuid.uuid4().hex, time.time()
        with self._db() as db:
            taken = [field for field, value in (("username", username), ("email", email))
                     if db.execute(f"SELECT 1 FROM users WHERE {field} = ?", (value,)).fetchone()]
            if taken:
                raise Duplicate(taken)
            first = not db.execute("SELECT 1 FROM users LIMIT 1").fetchone()
            try:
                db.execute("INSERT INTO users (id, username, email, password_hash, created_at, updated_at) "
                           "VALUES (?,?,?,?,?,?)", (user_id, username, email, password_hash, now, now))
            except sqlite3.IntegrityError:          # two registrations racing for the same name
                raise Duplicate(["username"]) from None
            if first:
                for table in ("projects", "conversations", "memories"):
                    db.execute(f"UPDATE {table} SET user_id = ? WHERE user_id IS NULL", (user_id,))
                # Settings saved before accounts were global; they become this user's.
                for row in db.execute("SELECT key, value FROM settings WHERE key IN "
                                      "('memory_enabled', 'memory_suggestions')").fetchall():
                    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                               (f"{user_id}:{row['key']}", row["value"]))
                    db.execute("DELETE FROM settings WHERE key = ?", (row["key"],))
            return self._user(db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())

    def get_user(self, user_id: str) -> dict | None:
        with self._db() as db:
            return self._user(db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())

    def find_login(self, identifier: str) -> tuple[dict, str] | None:
        """The active account with this username or email, with its password hash for verification."""
        with self._db() as db:
            row = db.execute("SELECT * FROM users WHERE (username = ? OR email = ?) AND is_active = 1",
                             (identifier, identifier)).fetchone()
            return (self._user(row), row["password_hash"]) if row else None

    def set_password_hash(self, user_id: str, password_hash: str) -> None:
        with self._db() as db:
            db.execute("UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
                       (password_hash, time.time(), user_id))

    # ---------- sessions ----------
    def create_session(self, user_id: str, token_hash: str, csrf_token: str, lifetime: float) -> None:
        now = time.time()
        with self._db() as db:
            db.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))        # tidy up as we go
            db.execute("INSERT INTO sessions (token_hash, user_id, csrf_token, created_at, expires_at, "
                       "last_activity_at) VALUES (?,?,?,?,?,?)",
                       (token_hash, user_id, csrf_token, now, now + lifetime, now))

    def get_session(self, token_hash: str, idle_limit: float) -> dict | None:
        """The session and its user, if it exists, has not expired, has not sat idle too long,
        and the account is still active. Anything else deletes it."""
        now = time.time()
        with self._db() as db:
            row = db.execute("SELECT s.*, u.username, u.email, u.is_active FROM sessions s "
                             "JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?", (token_hash,)).fetchone()
            if not row:
                return None
            if row["expires_at"] < now or now - row["last_activity_at"] > idle_limit or not row["is_active"]:
                db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
                return None
            if now - row["last_activity_at"] > 60:              # no write on every single request
                db.execute("UPDATE sessions SET last_activity_at = ? WHERE token_hash = ?", (now, token_hash))
            return {"user": {"id": row["user_id"], "username": row["username"], "email": row["email"]},
                    "csrf_token": row["csrf_token"], "expires_at": row["expires_at"]}

    def delete_session(self, token_hash: str) -> None:
        with self._db() as db:
            db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    # ---------- conversations ----------
    @staticmethod
    def _message(row: sqlite3.Row) -> dict:
        try:
            meta = json.loads(row["meta"])
        except ValueError:
            meta = {}
        # Stored display details first, so they can never overwrite the real id, role, content or time.
        return {**(meta if isinstance(meta, dict) else {}), "id": row["id"], "role": row["role"],
                "content": row["content"], "time": round(row["created_at"] * 1000)}

    @staticmethod
    def _conversation(row: sqlite3.Row) -> dict:
        return {"id": row["id"], "user_id": row["user_id"], "project_id": row["project_id"], "title": row["title"],
                "memoryExcluded": bool(row["memory_excluded"]),
                "createdAt": round(row["created_at"] * 1000), "updatedAt": round(row["updated_at"] * 1000)}

    def list_conversations(self, user_id: str, project_id: str | None = None,
                           with_messages: bool = False) -> list[dict]:
        """The user's own conversations, most recently used first. Never anyone else's."""
        with self._db() as db:
            if project_id:
                rows = db.execute("SELECT * FROM conversations WHERE user_id = ? AND project_id = ? "
                                  "ORDER BY updated_at DESC", (user_id, project_id)).fetchall()
            else:
                rows = db.execute("SELECT * FROM conversations WHERE user_id = ? ORDER BY updated_at DESC",
                                  (user_id,)).fetchall()
            found = []
            for row in rows:
                item = self._conversation(row)
                messages = db.execute("SELECT * FROM messages WHERE conversation_id = ? ORDER BY id",
                                      (row["id"],)).fetchall()
                item["message_count"] = len(messages)
                if with_messages:
                    item["messages"] = [self._message(m) for m in messages]
                found.append(item)
            return found

    def get_conversation(self, conversation_id: str) -> dict | None:
        """One conversation by id, whoever owns it. Callers must check `user_id` before using it."""
        with self._db() as db:
            row = db.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
            return self._conversation(row) if row else None

    def get_messages(self, conversation_id: str) -> list[dict]:
        """A conversation's messages in the order they were written."""
        with self._db() as db:
            return [self._message(r) for r in db.execute(
                "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id", (conversation_id,))]

    def create_conversation(self, conversation_id: str, user_id: str, project_id: str | None,
                            title: str = DEFAULT_TITLE) -> dict:
        now = time.time()
        with self._db() as db:
            db.execute("INSERT INTO conversations (id, user_id, project_id, title, created_at, updated_at) "
                       "VALUES (?,?,?,?,?,?)", (conversation_id, user_id, project_id, title, now, now))
        return self.get_conversation(conversation_id)

    def add_messages(self, conversation_id: str, items: list[dict]) -> list[dict]:
        """Append messages ({role, content, meta}) in one transaction and return them as stored.

        The first user message names a conversation that still has the default title.
        """
        now = time.time()
        with self._db() as db:
            conversation = db.execute("SELECT title FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
            ids = []
            for item in items:
                cursor = db.execute(
                    "INSERT INTO messages (conversation_id, role, content, meta, created_at) VALUES (?,?,?,?,?)",
                    (conversation_id, item["role"], item["content"], json.dumps(item.get("meta") or {}), now))
                ids.append(cursor.lastrowid)
            title = conversation["title"]
            if title == DEFAULT_TITLE:
                first = next((i["content"] for i in items
                              if i["role"] == "user" and not (i.get("meta") or {}).get("placeholder")), None)
                title = title_from(first) if first else title
            db.execute("UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?", (title, now, conversation_id))
            self._index_conversation(db, conversation_id)
            rows = db.execute(f"SELECT * FROM messages WHERE id IN ({','.join('?' * len(ids))}) ORDER BY id", ids)
            return [self._message(r) for r in rows]

    def update_message(self, conversation_id: str, message_id: int, content: str | None, meta: dict | None) -> bool:
        """Change a note's text or a message's display details. The message must be in that conversation."""
        with self._db() as db:
            row = db.execute("SELECT * FROM messages WHERE id = ? AND conversation_id = ?",
                             (message_id, conversation_id)).fetchone()
            if not row:
                return False
            merged = json.loads(row["meta"])
            merged.update(meta or {})
            # Only notes can be reworded; what the user asked and what the model answered stay as they were.
            text = content if content is not None and row["role"] == "system" else row["content"]
            db.execute("UPDATE messages SET content = ?, meta = ? WHERE id = ?", (text, json.dumps(merged), message_id))
        return True

    def update_conversation(self, conversation_id: str, title: str | None = None,
                            memory_excluded: bool | None = None) -> None:
        with self._db() as db:
            if title is not None:
                db.execute("UPDATE conversations SET title = ? WHERE id = ?", (title, conversation_id))
            if memory_excluded is not None:
                db.execute("UPDATE conversations SET memory_excluded = ? WHERE id = ?",
                           (int(memory_excluded), conversation_id))

    def delete_conversation(self, conversation_id: str) -> None:
        """Delete a conversation, its messages and search entries, and memory suggestions not yet approved."""
        with self._db() as db:
            db.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))      # messages go with it
            db.execute("DELETE FROM chat_fts WHERE chat_id = ?", (conversation_id,))
            db.execute("DELETE FROM memories WHERE source_chat_id = ? AND status = 'pending'", (conversation_id,))

    def _index_conversation(self, db: sqlite3.Connection, conversation_id: str) -> None:
        """Rebuild the search entries of one conversation. `seq` is a message's position in it."""
        row = db.execute("SELECT project_id FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        db.execute("DELETE FROM chat_fts WHERE chat_id = ?", (conversation_id,))
        messages = db.execute("SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY id",
                              (conversation_id,)).fetchall()
        db.executemany("INSERT INTO chat_fts (text, chat_id, project_id, seq, role) VALUES (?,?,?,?,?)",
                       [(m["content"], conversation_id, row["project_id"] if row else None, seq, m["role"])
                        for seq, m in enumerate(messages) if m["role"] in ("user", "assistant")])

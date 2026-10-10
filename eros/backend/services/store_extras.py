"""Storage added after the research core: settings, AI memory, chat search, and shared workspaces.

Mixed into ResearchStore, so everything stays in the same SQLite file. All of it
is created with CREATE IF NOT EXISTS / ADD COLUMN, so an existing database is
upgraded in place and no data is removed.
"""
from __future__ import annotations

import json
import re
import secrets
import sqlite3
import time
import uuid

EXTRA_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY, content TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'context',
    project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'saved', source_chat_id TEXT,
    created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS members (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL, role TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
    joined_at REAL NOT NULL, last_seen REAL NOT NULL);
CREATE INDEX IF NOT EXISTS members_project ON members(project_id);
"""

PROJECT_COLUMNS = {
    "shared": "INTEGER NOT NULL DEFAULT 0",
    "invite_code": "TEXT",
    "default_role": "TEXT NOT NULL DEFAULT 'contributor'",
    "revision": "INTEGER NOT NULL DEFAULT 0",
}
CHAT_COLUMNS = {"memory_excluded": "INTEGER NOT NULL DEFAULT 0"}

SETTING_DEFAULTS = {"memory_enabled": False, "memory_suggestions": True}     # memory is opt-in
MEMORY_KINDS = ("preference", "instruction", "topic", "context", "terminology")
ALWAYS_APPLY = ("preference", "instruction")       # standing wishes apply to every question
ROLES = ("viewer", "contributor", "manager")

_STOPWORDS = set("""a an and are as at be but by can could did do does for from had has have how i in is it its me my
of on or our please should so than that the their them then there these they this to us was we were what when where
which who why will with would you your about into over also just more most some such tell give show explain""".split())


def _tokens(text: str) -> set[str]:
    words = (w.rstrip("s") for w in re.findall(r"[a-z0-9]+", text.lower()))
    return {w for w in words if len(w) >= 3 and w not in _STOPWORDS}


def _add_columns(db: sqlite3.Connection, table: str, columns: dict) -> None:
    have = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
    for column, ddl in columns.items():
        if column not in have:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


class ExtrasMixin:
    fts = False        # True when SQLite's FTS5 full-text index is available

    def _init_extras(self, db: sqlite3.Connection) -> None:
        db.executescript(EXTRA_SCHEMA)
        _add_columns(db, "projects", PROJECT_COLUMNS)
        _add_columns(db, "chats", CHAT_COLUMNS)
        try:
            db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chat_fts USING fts5("
                       "text, chat_id UNINDEXED, project_id UNINDEXED, seq UNINDEXED, role UNINDEXED)")
            self.fts = True
        except sqlite3.OperationalError:      # SQLite built without FTS5: fall back to a plain table
            db.execute("CREATE TABLE IF NOT EXISTS chat_fts (text TEXT, chat_id TEXT, project_id TEXT, "
                       "seq INTEGER, role TEXT)")

    @staticmethod
    def _touch(db: sqlite3.Connection, project_id: str | None) -> None:
        """Mark a project as changed, so other devices looking at it know to reload."""
        if project_id:
            db.execute("UPDATE projects SET revision = revision + 1 WHERE id = ?", (project_id,))

    # ---------- settings ----------
    def get_settings(self, user_id: str) -> dict:
        """One user's settings. Each user has their own; they are stored as "<user id>:<name>"."""
        settings = dict(SETTING_DEFAULTS)
        with self._db() as db:
            for key in SETTING_DEFAULTS:
                row = db.execute("SELECT value FROM settings WHERE key = ?", (f"{user_id}:{key}",)).fetchone()
                if row:
                    settings[key] = json.loads(row["value"])
        return settings

    def set_settings(self, user_id: str, values: dict) -> dict:
        with self._db() as db:
            for key, value in values.items():
                if key in SETTING_DEFAULTS:
                    db.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                               "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                               (f"{user_id}:{key}", json.dumps(bool(value))))
        return self.get_settings(user_id)

    # ---------- memories ----------
    @staticmethod
    def _memory(row: sqlite3.Row) -> dict:
        return {k: row[k] for k in ("id", "user_id", "content", "kind", "project_id", "status", "source_chat_id",
                                    "created_at", "updated_at")}

    def list_memories(self, user_id: str, status: str | None = None) -> list[dict]:
        """One user's memories. Memories are personal and never shown to, or used for, anyone else."""
        with self._db() as db:
            if status:
                rows = db.execute("SELECT * FROM memories WHERE user_id = ? AND status = ? ORDER BY updated_at DESC",
                                  (user_id, status))
            else:
                rows = db.execute("SELECT * FROM memories WHERE user_id = ? ORDER BY updated_at DESC", (user_id,))
            return [self._memory(r) for r in rows]

    def get_memory(self, memory_id: str) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
            return self._memory(row) if row else None

    def add_memory(self, user_id: str, content: str, kind: str = "context", project_id: str | None = None,
                   status: str = "saved", source_chat_id: str | None = None) -> dict:
        memory_id, now = uuid.uuid4().hex, time.time()
        with self._db() as db:
            db.execute("INSERT INTO memories (id, user_id, content, kind, project_id, status, source_chat_id, "
                       "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                       (memory_id, user_id, content, kind, project_id, status, source_chat_id, now, now))
        return self.get_memory(memory_id)

    def update_memory(self, memory_id: str, fields: dict) -> dict | None:
        allowed = {k: v for k, v in fields.items() if k in ("content", "kind", "project_id", "status")}
        if allowed:
            with self._db() as db:
                db.execute(f"UPDATE memories SET {', '.join(f'{k} = ?' for k in allowed)}, updated_at = ? WHERE id = ?",
                           (*allowed.values(), time.time(), memory_id))
        return self.get_memory(memory_id)

    def delete_memory(self, memory_id: str) -> bool:
        with self._db() as db:
            return db.execute("DELETE FROM memories WHERE id = ?", (memory_id,)).rowcount > 0

    def clear_memories(self, user_id: str) -> int:
        with self._db() as db:
            return db.execute("DELETE FROM memories WHERE user_id = ?", (user_id,)).rowcount

    def relevant_memories(self, user_id: str, question: str, project_id: str | None, limit: int) -> list[dict]:
        """Saved memories worth showing the model for this question, best first.

        Keyword overlap decides relevance. Preferences and instructions are
        standing wishes, so they qualify without overlap; everything else must
        share a meaningful word with the question. Only this user's saved
        (approved) memories for all projects or this project are considered.
        """
        wanted = _tokens(question)
        with self._db() as db:
            rows = db.execute("SELECT * FROM memories WHERE user_id = ? AND status = 'saved' "
                              "AND (project_id IS NULL OR project_id = ?)", (user_id, project_id)).fetchall()
        scored = []
        for row in rows:
            overlap = len(wanted & _tokens(row["content"]))
            if overlap or row["kind"] in ALWAYS_APPLY:
                scored.append((overlap, row["updated_at"], self._memory(row)))
        scored.sort(key=lambda item: (-item[0], -item[1]))
        return [memory for _, _, memory in scored[:limit]]

    # ---------- chat search ----------
    def search_chats(self, user_id: str, project_id: str, query: str, since: float = 0,
                     limit: int = 40) -> list[dict]:
        """The user's own messages and chat titles containing every word of `query` (prefix match).

        Results are limited to conversations that belong to `user_id`; `since` is in milliseconds.
        """
        words = re.findall(r"\w+", query.lower())
        if not words:
            return []
        with self._db() as db:
            if self.fts:
                match = " ".join(f'"{w}"*' for w in words)
                hits = db.execute(
                    "SELECT chat_id, seq, role, snippet(chat_fts, 0, '‹', '›', ' … ', 14) AS snippet "
                    "FROM chat_fts WHERE chat_fts MATCH ? AND project_id = ? ORDER BY rank LIMIT ?",
                    (match, project_id, limit * 3)).fetchall()
            else:
                where = " AND ".join("LOWER(text) LIKE ?" for _ in words)
                hits = db.execute(
                    f"SELECT chat_id, seq, role, SUBSTR(text, 1, 160) AS snippet FROM chat_fts "
                    f"WHERE project_id = ? AND {where} LIMIT ?",
                    (project_id, *[f"%{w}%" for w in words], limit * 3)).fetchall()
            chats = {r["id"]: {"id": r["id"], "title": r["title"], "updated_at": round(r["updated_at"] * 1000)}
                     for r in db.execute(
                         "SELECT id, title, updated_at FROM conversations "
                         "WHERE user_id = ? AND project_id = ? AND updated_at >= ?",
                         (user_id, project_id, since / 1000))}
        results, seen = [], set()
        for chat in chats.values():                      # title matches first
            if all(w in chat["title"].lower() for w in words):
                seen.add(chat["id"])
                results.append({"chat_id": chat["id"], "title": chat["title"], "updatedAt": chat["updated_at"],
                                "seq": None, "role": "title", "snippet": chat["title"]})
        for hit in hits:
            chat = chats.get(hit["chat_id"])
            if chat and (hit["chat_id"], hit["seq"]) not in seen:
                seen.add((hit["chat_id"], hit["seq"]))
                results.append({"chat_id": chat["id"], "title": chat["title"], "updatedAt": chat["updated_at"],
                                "seq": hit["seq"], "role": hit["role"], "snippet": hit["snippet"]})
        results.sort(key=lambda r: -r["updatedAt"])
        return results[:limit]

    # ---------- shared workspaces ----------
    def set_sharing(self, project_id: str, shared: bool | None = None, default_role: str | None = None,
                    new_code: bool = False) -> None:
        """Turn sharing on or off for a project. Turning it off removes every participant."""
        with self._db() as db:
            row = db.execute("SELECT shared, invite_code FROM projects WHERE id = ?", (project_id,)).fetchone()
            if not row:
                return
            if shared is not None:
                db.execute("UPDATE projects SET shared = ? WHERE id = ?", (int(shared), project_id))
                if not shared:
                    db.execute("DELETE FROM members WHERE project_id = ?", (project_id,))
            if new_code or (shared and not row["invite_code"]):
                # Unambiguous characters only: easy to read aloud and type on a phone.
                code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))
                db.execute("UPDATE projects SET invite_code = ? WHERE id = ?", (code, project_id))
            if default_role in ROLES:
                db.execute("UPDATE projects SET default_role = ? WHERE id = ?", (default_role, project_id))
            self._touch(db, project_id)

    def shared_project_by_code(self, code: str) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT id, title, default_role FROM projects WHERE shared = 1 AND invite_code = ?",
                             (code,)).fetchone()
            return dict(row) if row else None

    @staticmethod
    def _member(row: sqlite3.Row) -> dict:
        return {k: row[k] for k in ("id", "project_id", "user_id", "name", "role", "joined_at", "last_seen")}

    def add_member(self, project_id: str, user_id: str, name: str, role: str) -> dict:
        """Admit a signed-in user to a shared workspace (once; joining again keeps the existing membership)."""
        now = time.time()
        with self._db() as db:
            row = db.execute("SELECT * FROM members WHERE project_id = ? AND user_id = ?",
                             (project_id, user_id)).fetchone()
            if not row:
                member_id = uuid.uuid4().hex
                # token_hash is a leftover column from before accounts; it only needs to be unique.
                db.execute("INSERT INTO members (id, project_id, user_id, name, role, token_hash, joined_at, last_seen) "
                           "VALUES (?,?,?,?,?,?,?,?)",
                           (member_id, project_id, user_id, name, role, secrets.token_hex(16), now, now))
                self._touch(db, project_id)
                row = db.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
            return self._member(row)

    def membership(self, project_id: str, user_id: str) -> dict | None:
        """The user's membership of a workspace that is still shared, if any. Records that they were seen."""
        with self._db() as db:
            row = db.execute("SELECT m.* FROM members m JOIN projects p ON p.id = m.project_id "
                             "WHERE m.project_id = ? AND m.user_id = ? AND p.shared = 1",
                             (project_id, user_id)).fetchone()
            if not row:
                return None
            if time.time() - row["last_seen"] > 20:      # activity status, without a write on every request
                db.execute("UPDATE members SET last_seen = ? WHERE id = ?", (time.time(), row["id"]))
            return self._member(row)

    def joined_projects(self, user_id: str) -> dict:
        """Shared workspaces the user has joined: project id -> their role there."""
        with self._db() as db:
            return {r["project_id"]: r["role"] for r in db.execute(
                "SELECT m.project_id, m.role FROM members m JOIN projects p ON p.id = m.project_id "
                "WHERE m.user_id = ? AND p.shared = 1", (user_id,))}

    def list_members(self, project_id: str) -> list[dict]:
        with self._db() as db:
            # Rows without an account are left over from the old invitation-token system and no longer count.
            return [self._member(r) for r in db.execute(
                "SELECT * FROM members WHERE project_id = ? AND user_id IS NOT NULL ORDER BY joined_at",
                (project_id,))]

    def get_member(self, member_id: str) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
            return self._member(row) if row else None

    def update_member(self, member_id: str, role: str) -> bool:
        member = self.get_member(member_id)
        if not member or role not in ROLES:
            return False
        with self._db() as db:
            db.execute("UPDATE members SET role = ? WHERE id = ?", (role, member_id))
            self._touch(db, member["project_id"])
        return True

    def remove_member(self, member_id: str) -> bool:
        """Remove a participant; their access to the workspace ends immediately."""
        member = self.get_member(member_id)
        if not member:
            return False
        with self._db() as db:
            db.execute("DELETE FROM members WHERE id = ?", (member_id,))
            self._touch(db, member["project_id"])
        return True

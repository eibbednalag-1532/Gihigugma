"""Local research store: one SQLite file + NumPy cosine search. No server, no cloud.

Holds everything that must survive a restart: research projects, documents,
extracted page text, chunks with embeddings, saved findings, and chats.
Sources View and Combined View both read the same `findings` rows.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import numpy as np

from services.store_auth import AuthMixin
from services.store_extras import ExtrasMixin

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, objectives TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT '', questions TEXT NOT NULL DEFAULT '[]', keywords TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, size INTEGER NOT NULL,
    pages INTEGER, chunks INTEGER NOT NULL, embed_model TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL, page INTEGER, text TEXT NOT NULL, embedding BLOB NOT NULL);
CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(doc_id);
CREATE TABLE IF NOT EXISTS doc_pages (
    doc_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL, page INTEGER, text TEXT NOT NULL, PRIMARY KEY (doc_id, seq));
CREATE TABLE IF NOT EXISTS findings (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL, doc_id TEXT REFERENCES documents(id) ON DELETE CASCADE, doc_name TEXT,
    chunk_idx INTEGER, page INTEGER, question TEXT NOT NULL DEFAULT '', quote TEXT NOT NULL,
    score REAL, ai_note TEXT NOT NULL DEFAULT '', user_note TEXT NOT NULL DEFAULT '',
    sources TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS findings_project ON findings(project_id);
CREATE TABLE IF NOT EXISTS chats (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL, messages TEXT NOT NULL DEFAULT '[]', updated_at REAL NOT NULL);
"""

# Columns added to `documents` after the first release (older databases get them via ALTER).
DOCUMENT_COLUMNS = {
    "project_id": "TEXT REFERENCES projects(id) ON DELETE CASCADE",
    "content_hash": "TEXT",
    "empty_pages": "INTEGER NOT NULL DEFAULT 0",
}

PROJECT_FIELDS = ("title", "objectives", "scope", "questions", "keywords")


class ResearchStore(ExtrasMixin, AuthMixin):
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._matrices: dict[tuple[str, int], tuple[list[dict], np.ndarray]] = {}
        with self._db() as db:
            db.executescript(SCHEMA)
            have = {row["name"] for row in db.execute("PRAGMA table_info(documents)")}
            for column, ddl in DOCUMENT_COLUMNS.items():
                if column not in have:
                    db.execute(f"ALTER TABLE documents ADD COLUMN {column} {ddl}")
            db.execute("CREATE INDEX IF NOT EXISTS documents_project ON documents(project_id)")
            # Documents indexed before projects existed keep working under a holding project.
            if db.execute("SELECT 1 FROM documents WHERE project_id IS NULL LIMIT 1").fetchone():
                now = time.time()
                pid = uuid.uuid4().hex
                db.execute("INSERT INTO projects (id, title, created_at, updated_at) VALUES (?,?,?,?)",
                           (pid, "Imported documents", now, now))
                db.execute("UPDATE documents SET project_id = ? WHERE project_id IS NULL", (pid,))
            self._init_extras(db)     # settings, memory, chat search, shared workspaces
            self._init_auth(db)       # accounts, sessions, each user's conversations

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        try:
            with db:        # commit on success, roll back on error
                yield db
        finally:
            db.close()

    def _forget_matrix(self, project_id: str | None) -> None:
        with self._lock:
            for key in [k for k in self._matrices if k[0] == project_id]:
                del self._matrices[key]

    # ---------- projects ----------
    @staticmethod
    def _project(row: sqlite3.Row) -> dict:
        out = {k: row[k] for k in ("id", "title", "objectives", "scope", "created_at", "updated_at")}
        out["questions"] = json.loads(row["questions"])
        out["keywords"] = json.loads(row["keywords"])
        out["stats"] = {k: row[k] for k in ("documents", "sections", "findings", "chats")}
        out.update(shared=bool(row["shared"]), invite_code=row["invite_code"], default_role=row["default_role"],
                   revision=row["revision"], members=row["members"], user_id=row["user_id"])
        return out

    _PROJECT_SELECT = """
        SELECT p.*,
          (SELECT COUNT(*) FROM documents d WHERE d.project_id = p.id) AS documents,
          (SELECT COALESCE(SUM(d.chunks), 0) FROM documents d WHERE d.project_id = p.id) AS sections,
          (SELECT COUNT(*) FROM findings f WHERE f.project_id = p.id) AS findings,
          (SELECT COUNT(*) FROM conversations c WHERE c.project_id = p.id) AS chats,
          (SELECT COUNT(*) FROM members m WHERE m.project_id = p.id AND m.user_id IS NOT NULL) AS members
        FROM projects p"""

    def list_projects(self) -> list[dict]:
        with self._db() as db:
            return [self._project(r) for r in db.execute(self._PROJECT_SELECT + " ORDER BY p.updated_at DESC")]

    def get_project(self, project_id: str) -> dict | None:
        with self._db() as db:
            row = db.execute(self._PROJECT_SELECT + " WHERE p.id = ?", (project_id,)).fetchone()
            return self._project(row) if row else None

    def create_project(self, data: dict, user_id: str | None = None) -> dict:
        """Create a project owned by `user_id`."""
        pid = uuid.uuid4().hex
        now = time.time()
        with self._db() as db:
            db.execute("INSERT INTO projects (id, user_id, title, objectives, scope, questions, keywords, created_at, "
                       "updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                       (pid, user_id, data["title"], data.get("objectives", ""), data.get("scope", ""),
                        json.dumps(data.get("questions", [])), json.dumps(data.get("keywords", [])), now, now))
        return self.get_project(pid)

    def update_project(self, project_id: str, data: dict) -> dict | None:
        sets, values = [], []
        for field in PROJECT_FIELDS:
            if field in data:
                sets.append(f"{field} = ?")
                values.append(json.dumps(data[field]) if field in ("questions", "keywords") else data[field])
        with self._db() as db:
            db.execute(f"UPDATE projects SET {', '.join(sets + ['updated_at = ?'])} WHERE id = ?",
                       (*values, time.time(), project_id))
            self._touch(db, project_id)
        return self.get_project(project_id)

    def delete_project(self, project_id: str) -> bool:
        with self._db() as db:
            deleted = db.execute("DELETE FROM projects WHERE id = ?", (project_id,)).rowcount > 0
            db.execute("DELETE FROM chat_fts WHERE project_id = ?", (project_id,))
        self._forget_matrix(project_id)
        return deleted

    # ---------- documents ----------
    _DOC_SELECT = ("SELECT d.*, (SELECT COUNT(*) FROM findings f WHERE f.doc_id = d.id) AS findings "
                   "FROM documents d")

    @staticmethod
    def _doc(row: sqlite3.Row) -> dict:
        keys = ("id", "project_id", "name", "size", "pages", "chunks", "embed_model",
                "empty_pages", "created_at", "findings")
        return {k: row[k] for k in keys}

    def find_duplicate(self, project_id: str, content_hash: str) -> dict | None:
        with self._db() as db:
            row = db.execute(self._DOC_SELECT + " WHERE d.project_id = ? AND d.content_hash = ?",
                             (project_id, content_hash)).fetchone()
            return self._doc(row) if row else None

    def add_document(self, project_id: str, name: str, size: int, pages: int | None, embed_model: str,
                     content_hash: str, empty_pages: int, page_texts: list[tuple[int | None, str]],
                     chunks: list[dict], vectors: list[list[float]]) -> dict:
        doc_id = uuid.uuid4().hex
        now = time.time()
        chunk_rows = [(doc_id, i, c["page"], c["text"], np.asarray(v, dtype=np.float32).tobytes())
                      for i, (c, v) in enumerate(zip(chunks, vectors))]
        with self._db() as db:
            db.execute("INSERT INTO documents (id, project_id, name, size, pages, chunks, embed_model, "
                       "content_hash, empty_pages, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (doc_id, project_id, name, size, pages, len(chunk_rows), embed_model,
                        content_hash, empty_pages, now))
            db.executemany("INSERT INTO chunks (doc_id, idx, page, text, embedding) VALUES (?,?,?,?,?)", chunk_rows)
            db.executemany("INSERT INTO doc_pages (doc_id, seq, page, text) VALUES (?,?,?,?)",
                           [(doc_id, i, page, text) for i, (page, text) in enumerate(page_texts)])
            db.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now, project_id))
            self._touch(db, project_id)
        self._forget_matrix(project_id)
        return self.get_document(doc_id)

    def list_documents(self, project_id: str) -> list[dict]:
        with self._db() as db:
            return [self._doc(r) for r in db.execute(
                self._DOC_SELECT + " WHERE d.project_id = ? ORDER BY d.created_at DESC", (project_id,))]

    def get_document(self, doc_id: str) -> dict | None:
        with self._db() as db:
            row = db.execute(self._DOC_SELECT + " WHERE d.id = ?", (doc_id,)).fetchone()
            return self._doc(row) if row else None

    def get_pages(self, doc_id: str) -> list[dict]:
        """The cleaned text extracted from the document, page by page."""
        with self._db() as db:
            return [{"page": r["page"], "text": r["text"]} for r in db.execute(
                "SELECT page, text FROM doc_pages WHERE doc_id = ? ORDER BY seq", (doc_id,))]

    def get_chunks(self, doc_id: str) -> list[dict]:
        """A document's indexed sections in reading order (what the research tools read)."""
        with self._db() as db:
            return [{"index": r["idx"], "page": r["page"], "text": r["text"]} for r in db.execute(
                "SELECT idx, page, text FROM chunks WHERE doc_id = ? ORDER BY idx", (doc_id,))]

    def delete_document(self, doc_id: str) -> bool:
        doc = self.get_document(doc_id)
        if not doc:
            return False
        with self._db() as db:
            db.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
            self._touch(db, doc["project_id"])
        self._forget_matrix(doc["project_id"])
        return True

    # ---------- semantic search ----------
    def _matrix(self, project_id: str, dim: int) -> tuple[list[dict], np.ndarray]:
        """Normalised embedding matrix for a project (cached until its documents change)."""
        key = (project_id, dim)
        with self._lock:
            if key in self._matrices:
                return self._matrices[key]
        with self._db() as db:
            rows = db.execute(
                "SELECT c.id, c.doc_id, c.idx, c.page, c.embedding, d.name FROM chunks c "
                "JOIN documents d ON d.id = c.doc_id WHERE d.project_id = ? ORDER BY d.created_at, c.idx",
                (project_id,)).fetchall()
        # Skip chunks embedded with a different model (different vector size).
        rows = [r for r in rows if len(r["embedding"]) == dim * 4]
        meta = [{"chunk_id": r["id"], "doc_id": r["doc_id"], "index": r["idx"], "page": r["page"], "name": r["name"]}
                for r in rows]
        if rows:
            matrix = np.vstack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
            matrix = matrix / np.maximum(np.linalg.norm(matrix, axis=1, keepdims=True), 1e-12)
        else:
            matrix = np.zeros((0, dim), dtype=np.float32)
        with self._lock:
            self._matrices[key] = (meta, matrix)
        return meta, matrix

    def search(self, project_id: str, query_vecs: list[list[float]], k: int, min_score: float = 0.0,
               doc_id: str | None = None, weights: list[float] | None = None,
               doc_ids: set[str] | None = None) -> list[dict]:
        """Best chunks by cosine similarity to any of `query_vecs`.

        `doc_id` or `doc_ids` limits the search to those documents.

        Each hit reports its best-matching query as `query` (an index into `query_vecs`)
        and its similarity to every query as `scores`.
        `score` is semantic similarity only; it says nothing about whether a passage is true.
        """
        queries = np.asarray(query_vecs, dtype=np.float32)
        if queries.ndim != 2 or not len(queries):
            return []
        queries = queries / np.maximum(np.linalg.norm(queries, axis=1, keepdims=True), 1e-12)
        meta, matrix = self._matrix(project_id, queries.shape[1])
        if not meta:
            return []
        scores = matrix @ queries.T
        if weights:
            scores = scores * np.asarray(weights, dtype=np.float32)
        best = scores.max(axis=1)
        which = scores.argmax(axis=1)

        picked = []
        for i in np.argsort(-best):
            if best[i] < min_score or len(picked) >= k:
                break
            if doc_id and meta[i]["doc_id"] != doc_id:
                continue
            if doc_ids is not None and meta[i]["doc_id"] not in doc_ids:
                continue
            picked.append(int(i))
        if not picked:
            return []

        ids = [meta[i]["chunk_id"] for i in picked]
        with self._db() as db:
            texts = {r["id"]: r["text"] for r in db.execute(
                f"SELECT id, text FROM chunks WHERE id IN ({','.join('?' * len(ids))})", ids)}
        return [{**{k: meta[i][k] for k in ("doc_id", "name", "page", "index")},
                 "text": texts.get(meta[i]["chunk_id"], ""), "score": float(best[i]), "query": int(which[i]),
                 "scores": [float(s) for s in scores[i]]}
                for i in picked]

    def read_through(self, project_id: str, doc_ids: list[str] | None, budget: int) -> tuple[list[dict], int]:
        """Sections read in order for a whole-document request, and how many sections exist in total.

        Short documents are returned complete. When the documents hold more than
        `budget` sections, each gets an even share, spread from start to end.
        """
        with self._db() as db:
            docs = db.execute("SELECT id, name, chunks FROM documents WHERE project_id = ? ORDER BY created_at DESC",
                              (project_id,)).fetchall()
            if doc_ids:
                docs = [d for d in docs if d["id"] in doc_ids]
            docs = docs[:budget]                       # newest first when there are more documents than room
            total = sum(d["chunks"] for d in docs)
            hits = []
            for position, doc in enumerate(docs):
                share = budget // len(docs) + (1 if position < budget % len(docs) else 0)
                count = doc["chunks"]
                if count <= share:
                    wanted = list(range(count))
                elif share == 1:
                    wanted = [0]
                else:
                    wanted = sorted({round(i * (count - 1) / (share - 1)) for i in range(share)})
                rows = db.execute(
                    f"SELECT idx, page, text FROM chunks WHERE doc_id = ? AND idx IN ({','.join('?' * len(wanted))}) "
                    "ORDER BY idx", (doc["id"], *wanted)).fetchall()
                hits += [{"doc_id": doc["id"], "name": doc["name"], "page": r["page"], "index": r["idx"],
                          "text": r["text"], "score": None} for r in rows]
        return hits, total

    def passage_scores(self, doc_id: str, chunk_idx: int, query_vecs: list[list[float]]) -> list[float]:
        """Cosine similarity of one stored passage to each query ([] if it can't be compared)."""
        with self._db() as db:
            row = db.execute("SELECT embedding FROM chunks WHERE doc_id = ? AND idx = ?",
                             (doc_id, chunk_idx)).fetchone()
        queries = np.asarray(query_vecs, dtype=np.float32)
        if not row or queries.ndim != 2 or len(row["embedding"]) != queries.shape[1] * 4:
            return []
        vec = np.frombuffer(row["embedding"], dtype=np.float32)
        vec = vec / max(float(np.linalg.norm(vec)), 1e-12)
        queries = queries / np.maximum(np.linalg.norm(queries, axis=1, keepdims=True), 1e-12)
        return [float(s) for s in queries @ vec]

    # ---------- findings ----------
    @staticmethod
    def _finding(row: sqlite3.Row) -> dict:
        out = {k: row[k] for k in ("id", "project_id", "kind", "doc_id", "doc_name", "chunk_idx", "page",
                                   "question", "quote", "score", "ai_note", "user_note", "created_at")}
        out["sources"] = json.loads(row["sources"])
        return out

    def list_findings(self, project_id: str) -> list[dict]:
        with self._db() as db:
            return [self._finding(r) for r in db.execute(
                "SELECT * FROM findings WHERE project_id = ? ORDER BY created_at DESC", (project_id,))]

    def get_finding(self, finding_id: str) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone()
            return self._finding(row) if row else None

    def add_passage_finding(self, project_id: str, doc_id: str, chunk_idx: int,
                            question: str, score: float | None) -> tuple[dict | None, bool]:
        """Save a passage as a finding. The quote is copied from the stored chunk, never from the client.

        Returns (finding, created). A passage is saved at most once per project.
        """
        with self._db() as db:
            chunk = db.execute(
                "SELECT c.page, c.text, d.name FROM chunks c JOIN documents d ON d.id = c.doc_id "
                "WHERE c.doc_id = ? AND c.idx = ? AND d.project_id = ?", (doc_id, chunk_idx, project_id)).fetchone()
            if not chunk:
                return None, False
            existing = db.execute("SELECT id FROM findings WHERE kind = 'passage' AND doc_id = ? AND chunk_idx = ?",
                                  (doc_id, chunk_idx)).fetchone()
            if existing:
                finding_id, created = existing["id"], False
            else:
                finding_id, created = uuid.uuid4().hex, True
                db.execute("INSERT INTO findings (id, project_id, kind, doc_id, doc_name, chunk_idx, page, question, "
                           "quote, score, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                           (finding_id, project_id, "passage", doc_id, chunk["name"], chunk_idx, chunk["page"],
                            question, chunk["text"], score, time.time()))
                self._touch(db, project_id)
        return self.get_finding(finding_id), created

    def add_answer_finding(self, project_id: str, question: str, text: str, sources: list[dict]) -> dict:
        finding_id = uuid.uuid4().hex
        with self._db() as db:
            db.execute("INSERT INTO findings (id, project_id, kind, question, quote, sources, created_at) "
                       "VALUES (?,?,?,?,?,?,?)",
                       (finding_id, project_id, "answer", question, text, json.dumps(sources), time.time()))
            self._touch(db, project_id)
        return self.get_finding(finding_id)

    def update_finding(self, finding_id: str, fields: dict) -> dict | None:
        allowed = {k: v for k, v in fields.items() if k in ("user_note", "ai_note", "question")}
        if allowed:
            with self._db() as db:
                db.execute(f"UPDATE findings SET {', '.join(f'{k} = ?' for k in allowed)} WHERE id = ?",
                           (*allowed.values(), finding_id))
                row = db.execute("SELECT project_id FROM findings WHERE id = ?", (finding_id,)).fetchone()
                self._touch(db, row["project_id"] if row else None)
        return self.get_finding(finding_id)

    def delete_finding(self, finding_id: str) -> bool:
        with self._db() as db:
            row = db.execute("SELECT project_id FROM findings WHERE id = ?", (finding_id,)).fetchone()
            self._touch(db, row["project_id"] if row else None)
            return db.execute("DELETE FROM findings WHERE id = ?", (finding_id,)).rowcount > 0

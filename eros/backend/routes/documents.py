"""Document endpoints. Documents always belong to a research project.

  POST   /api/projects/<pid>/documents   multipart form, field "file". Streams NDJSON progress:
           {"type": "start", "sections": N, "pages": P, "empty_pages": E}
           {"type": "progress", "done": n, "total": N}      (many)
           {"type": "done", "document": {...}}
           {"type": "error", "code": "...", "message": "..."}
         Problems found before indexing starts (bad file, duplicate) are plain JSON errors.
  GET    /api/projects/<pid>/documents   -> { documents[] }
  GET    /api/documents/<id>             -> { document, pages[] }   (extracted text)
  DELETE /api/documents/<id>             -> { ok }
"""
import hashlib
import json

from flask import Blueprint, Response, current_app, jsonify, request

from services.documents import DocumentError, chunk_pages, extract_text
from services.ollama_client import OllamaError
from services.retrieval import embed_passages

bp = Blueprint("documents", __name__, url_prefix="/api")


def _error(code: str, message: str, status: int, **extra):
    return jsonify({"error": {"code": code, "message": message}, **extra}), status


def _line(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False) + "\n"


@bp.post("/projects/<project_id>/documents")
def upload(project_id: str):
    cfg = current_app.config
    store = current_app.extensions["store"]
    client = current_app.extensions["ollama"]

    if not store.get_project(project_id):
        return _error("not_found", "That project doesn't exist.", 404)
    file = request.files.get("file")
    if file is None or not file.filename:
        return _error("no_file", "No file was uploaded.", 400)
    data = file.read()
    if not data:
        return _error("empty_file", "That file is empty.", 400)
    name = file.filename.replace("\\", "/").split("/")[-1][:200]

    content_hash = hashlib.sha256(data).hexdigest()
    duplicate = store.find_duplicate(project_id, content_hash)
    if duplicate:
        return _error("duplicate", f"This file is already in the project as '{duplicate['name']}'.", 409,
                      document=duplicate)

    try:
        extraction = extract_text(name, data)
    except DocumentError as err:
        return _error(err.code, err.message, err.status)
    chunks = chunk_pages(extraction.pages, cfg["CHUNK_CHARS"], cfg["CHUNK_OVERLAP"])
    if len(chunks) > cfg["MAX_CHUNKS_PER_DOC"]:
        return _error("document_too_large",
                      f"This document is too long to index ({len(chunks):,} sections; "
                      f"the limit is {cfg['MAX_CHUNKS_PER_DOC']:,}).", 413)

    embed_model, batch = cfg["EMBED_MODEL"], cfg["EMBED_BATCH"]

    def generate():
        yield _line({"type": "start", "sections": len(chunks), "pages": extraction.page_count,
                     "empty_pages": extraction.empty_pages})
        vectors: list = []
        try:
            for i in range(0, len(chunks), batch):
                vectors += embed_passages(client, embed_model, [c["text"] for c in chunks[i:i + batch]])
                yield _line({"type": "progress", "done": len(vectors), "total": len(chunks)})
        except OllamaError as err:
            yield _line({"type": "error", **err.to_dict()})
            return
        # Nothing is saved unless every section was embedded, so a failed or
        # cancelled upload never leaves a half-indexed document behind.
        doc = store.add_document(project_id, name, len(data), extraction.page_count, embed_model,
                                 content_hash, extraction.empty_pages, extraction.pages, chunks, vectors)
        yield _line({"type": "done", "document": doc})

    return Response(generate(), mimetype="application/x-ndjson",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@bp.get("/projects/<project_id>/documents")
def list_documents(project_id: str):
    return jsonify({"documents": current_app.extensions["store"].list_documents(project_id)})


@bp.get("/documents/<doc_id>")
def get_document(doc_id: str):
    store = current_app.extensions["store"]
    doc = store.get_document(doc_id)
    if not doc:
        return _error("not_found", "That document doesn't exist.", 404)
    return jsonify({"document": doc, "pages": store.get_pages(doc_id)})


@bp.delete("/documents/<doc_id>")
def delete_document(doc_id: str):
    if not current_app.extensions["store"].delete_document(doc_id):
        return _error("not_found", "That document doesn't exist.", 404)
    return jsonify({"ok": True})

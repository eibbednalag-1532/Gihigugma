"""Research projects and everything saved inside them.

  GET/POST          /api/projects
  GET/PATCH/DELETE  /api/projects/<pid>
  GET               /api/projects/<pid>/relevance?doc_id=&min_score=   passages matching the topic/questions
  GET/POST          /api/projects/<pid>/findings
  PATCH/DELETE      /api/findings/<id>
  POST              /api/findings/<id>/explain      AI note on how a saved passage relates to its question
"""
from flask import Blueprint, current_app, jsonify, request

from services.access import current_user, project_role
from services.ollama_client import OllamaError
from services.prompt_builder import build_explain_messages
from services.retrieval import classify_passage, project_queries, relevant_passages

bp = Blueprint("projects", __name__, url_prefix="/api")

MAX_TITLE, MAX_TEXT, MAX_ITEMS, MAX_ITEM = 200, 4000, 30, 500


def _error(code: str, message: str, status: int):
    return jsonify({"error": {"code": code, "message": message}}), status


def _store():
    return current_app.extensions["store"]


def _body() -> dict:
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


def _text(value, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _items(value) -> list[str]:
    if not isinstance(value, list):
        return []
    seen = []
    for item in value:
        item = _text(item, MAX_ITEM)
        if item and item not in seen:
            seen.append(item)
    return seen[:MAX_ITEMS]


def _project_fields(body: dict, partial: bool) -> dict:
    data = {}
    if "title" in body or not partial:
        data["title"] = _text(body.get("title"), MAX_TITLE)
    for field in ("objectives", "scope"):
        if field in body or not partial:
            data[field] = _text(body.get(field), MAX_TEXT)
    for field in ("questions", "keywords"):
        if field in body or not partial:
            data[field] = _items(body.get(field))
    return data


# ---------- projects ----------
def _visible(project: dict, role: str) -> dict:
    """A project as the signed-in user may see it, with their role in it.

    Only the owner gets the invitation code. The chat count is the user's own:
    conversations are private, even in a shared workspace.
    """
    user = current_user()
    mine = len(_store().list_conversations(user["id"], project["id"]))
    shown = {k: v for k, v in project.items() if k != "user_id"}
    shown["stats"] = {**project["stats"], "chats": mine}
    shown["role"] = role
    if role != "owner":
        shown["invite_code"] = None
    return shown


@bp.get("/projects")
def list_projects():
    """The user's own projects and the shared workspaces they joined. Nobody else's."""
    user = current_user()
    joined = _store().joined_projects(user["id"])
    projects = []
    for project in _store().list_projects():
        if project["user_id"] == user["id"]:
            projects.append(_visible(project, "owner"))
        elif project["id"] in joined:
            projects.append(_visible(project, joined[project["id"]]))
    return jsonify({"projects": projects})


@bp.post("/projects")
def create_project():
    data = _project_fields(_body(), partial=False)
    if not data["title"]:
        return _error("title_required", "Give the project a title.", 400)
    project = _store().create_project(data, current_user()["id"])
    return jsonify({"project": _visible(project, "owner")}), 201


@bp.get("/projects/<project_id>")
def get_project(project_id: str):
    project = _store().get_project(project_id)
    if not project:
        return _error("not_found", "That project doesn't exist.", 404)
    return jsonify({"project": _visible(project, project_role(project_id))})


@bp.patch("/projects/<project_id>")
def update_project(project_id: str):
    if not _store().get_project(project_id):
        return _error("not_found", "That project doesn't exist.", 404)
    data = _project_fields(_body(), partial=True)
    if "title" in data and not data["title"]:
        return _error("title_required", "Give the project a title.", 400)
    return jsonify({"project": _visible(_store().update_project(project_id, data), "owner")})


@bp.delete("/projects/<project_id>")
def delete_project(project_id: str):
    if not _store().delete_project(project_id):
        return _error("not_found", "That project doesn't exist.", 404)
    return jsonify({"ok": True})


# ---------- relevance ----------
@bp.get("/projects/<project_id>/relevance")
def relevance(project_id: str):
    cfg = current_app.config
    project = _store().get_project(project_id)
    if not project:
        return _error("not_found", "That project doesn't exist.", 404)
    try:
        min_score = float(request.args.get("min_score", cfg["MIN_SCORE"]))
        limit = max(1, min(int(request.args.get("limit", 60)), 200))
    except ValueError:
        return _error("bad_request", "min_score and limit must be numbers.", 400)
    try:
        result = relevant_passages(current_app.extensions["ollama"], _store(), cfg, project,
                                   request.args.get("doc_id") or None, min_score, limit)
    except OllamaError as err:
        return _error(err.code, err.message, err.status)
    return jsonify({**result, "min_score": min_score})


# ---------- findings ----------
@bp.get("/projects/<project_id>/findings")
def list_findings(project_id: str):
    return jsonify({"findings": _store().list_findings(project_id)})


@bp.post("/projects/<project_id>/findings")
def create_finding(project_id: str):
    store, body = _store(), _body()
    if not store.get_project(project_id):
        return _error("not_found", "That project doesn't exist.", 404)
    question = _text(body.get("question"), MAX_ITEM)

    if body.get("kind") == "answer":
        text = _text(body.get("text"), 20000)
        if not text:
            return _error("bad_request", "There is no answer text to save.", 400)
        sources = [{"n": s.get("n"), "doc_id": s.get("doc_id"), "name": _text(s.get("name"), 200),
                    "page": s.get("page"), "index": s.get("index")}
                   for s in (body.get("sources") or []) if isinstance(s, dict)][:20]
        return jsonify({"finding": store.add_answer_finding(project_id, question, text, sources),
                        "created": True}), 201

    doc_id, chunk_idx, score = body.get("doc_id"), body.get("chunk_index"), body.get("score")
    if not isinstance(doc_id, str) or not isinstance(chunk_idx, int) or isinstance(chunk_idx, bool):
        return _error("bad_request", "A passage finding needs doc_id and chunk_index.", 400)
    score = float(score) if isinstance(score, (int, float)) and not isinstance(score, bool) else None
    project = store.get_project(project_id)
    if question not in {q["label"] for q in project_queries(project)}:
        # Saved from a chat answer: file it under the research question it matches best,
        # so the Combined view can group it. If none matches, the chat question is kept.
        try:
            match = classify_passage(current_app.extensions["ollama"], store, current_app.config,
                                     project, doc_id, chunk_idx)
            if match:
                question, score = match["label"], match["score"]
        except OllamaError:
            pass
    finding, created = store.add_passage_finding(project_id, doc_id, chunk_idx, question, score)
    if not finding:
        return _error("not_found", "That passage no longer exists in this project.", 404)
    return jsonify({"finding": finding, "created": created}), 201 if created else 200


@bp.patch("/findings/<finding_id>")
def update_finding(finding_id: str):
    store, body = _store(), _body()
    if not store.get_finding(finding_id):
        return _error("not_found", "That finding doesn't exist.", 404)
    fields = {}
    if "user_note" in body:
        fields["user_note"] = _text(body.get("user_note"), MAX_TEXT)
    if "question" in body:
        fields["question"] = _text(body.get("question"), MAX_ITEM)
    return jsonify({"finding": store.update_finding(finding_id, fields)})


@bp.delete("/findings/<finding_id>")
def delete_finding(finding_id: str):
    if not _store().delete_finding(finding_id):
        return _error("not_found", "That finding doesn't exist.", 404)
    return jsonify({"ok": True})


@bp.post("/findings/<finding_id>/explain")
def explain_finding(finding_id: str):
    """Ask the local model how a saved passage relates to its question; stored as the finding's AI note."""
    cfg, store, client = current_app.config, _store(), current_app.extensions["ollama"]
    finding = store.get_finding(finding_id)
    if not finding or finding["kind"] != "passage":
        return _error("not_found", "That finding doesn't exist.", 404)
    project = store.get_project(finding["project_id"])
    question = finding["question"] or project["title"]
    try:
        models = [m for m in client.list_models() if m["can_chat"]]
        if not models:
            raise OllamaError("no_models", "No chat model is installed in Ollama.", 409)
        wanted = _text(_body().get("model"), 200) or cfg["DEFAULT_MODEL"]
        name = (client.resolve_name(wanted, models) if wanted else None) or models[0]["name"]
        thinks = next((m["thinks"] for m in models if m["name"] == name), False)
        note = client.chat(name, build_explain_messages(question, finding["quote"], finding["doc_name"] or ""),
                           options={"temperature": cfg["GROUNDED_TEMPERATURE"]},
                           think=False if thinks else None)
    except OllamaError as err:
        return _error(err.code, err.message, err.status)
    if not note:
        return _error("empty_reply", "The model returned an empty explanation. Try again.", 502)
    return jsonify({"finding": store.update_finding(finding_id, {"ai_note": note})})

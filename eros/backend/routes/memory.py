"""AI memory, its settings, and conversation search. All of it stays in the local database.

  GET/PUT  /api/settings                        { memory_enabled, memory_suggestions }
  GET      /api/memories                        { memories[], pending[] }
  POST     /api/memories                        { content, kind, project_id } - added by the user, saved at once
  PATCH    /api/memories/<id>                   { content, kind, project_id, approve }
  DELETE   /api/memories/<id>                   delete one;   DELETE /api/memories  clears all
  POST     /api/memories/suggest                { text, chat_id, project_id, model } -> pending suggestions
  GET      /api/projects/<pid>/search?q=&since= the user's own messages and chat titles matching the words

Memories and settings are personal: every route works only on the signed-in
user's own, and a memory that belongs to someone else is answered as not found.
Nothing becomes a memory without the user: suggestions are stored as "pending"
and are never given to the model until approved. Memory is off until switched on.
"""
import json
import re

from flask import Blueprint, current_app, jsonify, request

from services.access import can, current_user
from services.ollama_client import OllamaError
from services.store_extras import MEMORY_KINDS, _tokens

bp = Blueprint("memory", __name__, url_prefix="/api")

MAX_MEMORY_CHARS = 400

# Details that should never be stored as a memory, whoever asks.
_SENSITIVE = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "an email address"),
    (re.compile(r"(?<!\d)(?:\+?\d[\s().-]?){9,15}(?!\d)"), "a phone, card or ID number"),
    (re.compile(r"\b(pass(?:word|code|phrase)|pin code|otp|cvv|api[ _-]?key|secret key|private key|"
                r"social security|ssn|credit card|debit card|bank account|account number|passport number|"
                r"home address|date of birth)\b", re.I), "private credentials or personal identifiers"),
]

SUGGEST_PROMPT = """You decide what is worth remembering about a user for future conversations.
Read the user's message and output JSON in this exact shape:
{"memories": [{"content": "The user ...", "kind": "preference"}]}

Include only durable things the user states about themselves: preferences for how answers should be written,
standing instructions, their research topic or project context, and terminology they define.
kind is one of: preference, instruction, topic, context, terminology.
Write each memory as one short sentence that starts with the words "The user".
Never copy a question the user asked. Do not include one-off requests, facts taken from documents, or anything
private (passwords, contact details, addresses, ID numbers, health or financial details).
If nothing qualifies, output {"memories": []}.

Example message: "From now on keep answers short. What is a culvert?"
Example output: {"memories": [{"content": "The user wants answers kept short.", "kind": "preference"}]}
Example message: "What causes flooding in cities?"
Example output: {"memories": []}"""


def _error(code: str, message: str, status: int):
    return jsonify({"error": {"code": code, "message": message}}), status


def _store():
    return current_app.extensions["store"]


def _body() -> dict:
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


def _uid() -> str:
    return current_user()["id"]


def _own_memory(memory_id: str) -> dict | None:
    memory = _store().get_memory(memory_id)
    return memory if memory and memory["user_id"] == _uid() else None


def sensitive_reason(text: str) -> str | None:
    """What kind of private detail `text` appears to contain, if any."""
    for pattern, label in _SENSITIVE:
        if pattern.search(text):
            return label
    return None


def _clean(body: dict) -> tuple[dict, tuple | None]:
    """Validated memory fields from a request body, or an error response."""
    fields = {}
    if "content" in body:
        content = " ".join(body["content"].split())[:MAX_MEMORY_CHARS] if isinstance(body["content"], str) else ""
        if not content:
            return {}, _error("content_required", "Write what should be remembered.", 400)
        reason = sensitive_reason(content)
        if reason:
            return {}, _error("sensitive", f"This looks like it contains {reason}, so it wasn't saved to memory.", 422)
        fields["content"] = content
    if "kind" in body:
        if body["kind"] not in MEMORY_KINDS:
            return {}, _error("bad_request", "Unknown memory kind.", 400)
        fields["kind"] = body["kind"]
    if "project_id" in body:
        project_id = body["project_id"] or None
        if project_id is not None and (not isinstance(project_id, str) or not can("view", project_id)):
            return {}, _error("not_found", "That project doesn't exist.", 404)
        fields["project_id"] = project_id
    return fields, None


# ---------- settings ----------
@bp.get("/settings")
def get_settings():
    return jsonify({"settings": _store().get_settings(_uid())})


@bp.put("/settings")
def put_settings():
    return jsonify({"settings": _store().set_settings(_uid(), _body())})


# ---------- memories ----------
@bp.get("/memories")
def list_memories():
    store = _store()
    return jsonify({"memories": store.list_memories(_uid(), "saved"),
                    "pending": store.list_memories(_uid(), "pending")})


@bp.post("/memories")
def add_memory():
    fields, problem = _clean({"kind": "context", "project_id": None, **_body()})
    if problem:
        return problem
    if "content" not in fields:
        return _error("content_required", "Write what should be remembered.", 400)
    memory = _store().add_memory(_uid(), fields["content"], fields["kind"], fields["project_id"])
    return jsonify({"memory": memory}), 201


@bp.patch("/memories/<memory_id>")
def update_memory(memory_id: str):
    store, body = _store(), _body()
    if not _own_memory(memory_id):
        return _error("not_found", "That memory no longer exists.", 404)
    fields, problem = _clean(body)
    if problem:
        return problem
    if body.get("approve"):
        fields["status"] = "saved"
    return jsonify({"memory": store.update_memory(memory_id, fields)})


@bp.delete("/memories/<memory_id>")
def delete_memory(memory_id: str):
    if not _own_memory(memory_id):
        return _error("not_found", "That memory no longer exists.", 404)
    _store().delete_memory(memory_id)
    return jsonify({"ok": True})


@bp.delete("/memories")
def clear_memories():
    return jsonify({"ok": True, "deleted": _store().clear_memories(_uid())})


@bp.post("/memories/suggest")
def suggest_memories():
    """Ask the local model whether a user message holds something worth remembering.

    Whatever it proposes is stored as pending and shown to the user to approve or dismiss.
    """
    cfg, store, client, body = current_app.config, _store(), current_app.extensions["ollama"], _body()
    settings = store.get_settings(_uid())
    text = body.get("text")
    chat_id = body.get("chat_id") if isinstance(body.get("chat_id"), str) else None
    if not settings["memory_enabled"] or not settings["memory_suggestions"]:
        return jsonify({"suggestions": []})
    if not isinstance(text, str) or not text.strip():
        return jsonify({"suggestions": []})
    chat = store.get_conversation(chat_id) if chat_id else None
    if chat and (chat["user_id"] != _uid() or chat["memoryExcluded"]):
        return jsonify({"suggestions": []})

    try:
        models = [m for m in client.list_models() if m["can_chat"]]
        if not models:
            return jsonify({"suggestions": []})
        wanted = body.get("model") if isinstance(body.get("model"), str) else cfg["DEFAULT_MODEL"]
        name = (client.resolve_name(wanted, models) if wanted else None) or models[0]["name"]
        thinks = next((m["thinks"] for m in models if m["name"] == name), False)
        reply = client.chat(name, [{"role": "system", "content": SUGGEST_PROMPT},
                                   {"role": "user", "content": text.strip()[:2000]}],
                            options={"temperature": 0, "num_predict": 300}, think=False if thinks else None,
                            fmt="json")
    except OllamaError as err:
        return _error(err.code, err.message, err.status)

    try:
        proposed = json.loads(reply).get("memories", [])
    except (ValueError, AttributeError):
        proposed = []
    existing = [_tokens(m["content"]) for m in store.list_memories(_uid())]
    project_id = body.get("project_id") if isinstance(body.get("project_id"), str) else None
    if project_id and not can("view", project_id):
        project_id = None
    suggestions = []
    for item in proposed[:3] if isinstance(proposed, list) else []:
        content = item.get("content") if isinstance(item, dict) else None
        if not isinstance(content, str) or len(content.split()) < 4:
            continue
        content = " ".join(content.split())[:MAX_MEMORY_CHARS]
        # A real memory is a statement about the user; a copied question or stray text is discarded.
        if not content.lower().startswith("the user") or content.endswith("?"):
            continue
        if sensitive_reason(content) or sensitive_reason(text):
            continue                                   # private details are never offered as memories
        words = _tokens(content)
        if any(words and len(words & other) / len(words | other) > 0.7 for other in existing):
            continue                                   # already known or already suggested
        kind = item.get("kind") if item.get("kind") in MEMORY_KINDS else "context"
        scope = project_id if kind in ("topic", "context", "terminology") and project_id else None
        suggestions.append(store.add_memory(_uid(), content, kind, scope, status="pending",
                                            source_chat_id=chat_id))
        existing.append(words)
    return jsonify({"suggestions": suggestions})


# ---------- conversation history ----------
@bp.get("/projects/<project_id>/search")
def search_chats(project_id: str):
    query = request.args.get("q", "")[:200]
    try:
        since = float(request.args.get("since", 0))
    except ValueError:
        return _error("bad_request", "since must be a number.", 400)
    return jsonify({"results": _store().search_chats(_uid(), project_id, query, since)})

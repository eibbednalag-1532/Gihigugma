"""A user's conversations, stored on the host computer.

  GET    /api/conversations?project_id=&include=messages   the signed-in user's conversations
  POST   /api/conversations                 { project_id, title?, id? } -> { conversation }
  GET    /api/conversations/<id>            -> { conversation, messages[] } in the order written
  PATCH  /api/conversations/<id>            { title, memory_excluded }
  DELETE /api/conversations/<id>
  POST   /api/conversations/<id>/messages   { project_id, messages: [{ role, content, meta }] }
                                            notes and imported results; the model's replies are saved by /api/chat
  PATCH  /api/conversations/<id>/messages/<message_id>   { content, meta }

Every route here works only on conversations whose owner is the signed-in user.
The owner comes from the server-side session; the browser cannot name another
user. Someone else's conversation is answered exactly like one that doesn't exist.
"""
import json
import re
import uuid

from flask import Blueprint, current_app, jsonify, request

from services.access import can, current_user
from services.store_auth import ROLES_OF_MESSAGES

bp = Blueprint("conversations", __name__, url_prefix="/api")

ID_RULE = re.compile(r"[A-Za-z0-9-]{8,64}")
MAX_TITLE, MAX_APPENDED, MAX_META_BYTES = 200, 40, 400_000


def _error(code: str, message: str, status: int):
    return jsonify({"error": {"code": code, "message": message}}), status


def _store():
    return current_app.extensions["store"]


def _body() -> dict:
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


def _public(conversation: dict) -> dict:
    return {k: v for k, v in conversation.items() if k != "user_id"}


NOT_FOUND = ("not_found", "That conversation doesn't exist.", 404)


def owned(conversation_id: str) -> dict | None:
    """The conversation, only if it belongs to the signed-in user."""
    conversation = _store().get_conversation(conversation_id) if ID_RULE.fullmatch(conversation_id or "") else None
    return conversation if conversation and conversation["user_id"] == current_user()["id"] else None


def owned_or_new(conversation_id, project_id) -> tuple[dict | None, tuple | None]:
    """An existing conversation of this user, or a new one in a project they can open.

    The browser may propose the id of a new conversation (it shows the chat before the
    first message is stored). An id that already belongs to someone else is refused
    without revealing that it exists.
    """
    store, user = _store(), current_user()
    if conversation_id is not None:
        if not isinstance(conversation_id, str) or not ID_RULE.fullmatch(conversation_id):
            return None, ("bad_request", "That conversation id isn't valid.", 400)
        existing = store.get_conversation(conversation_id)
        if existing:
            return (existing, None) if existing["user_id"] == user["id"] else (None, NOT_FOUND)
    if not isinstance(project_id, str) or not can("view", project_id):
        return None, ("project_not_found", "Pick a research project first.", 404)
    return store.create_conversation(conversation_id or uuid.uuid4().hex, user["id"], project_id), None


def clean_messages(raw) -> list[dict] | None:
    """Messages a browser may append: the three known roles, text content, and display details of sane size."""
    if not isinstance(raw, list) or not raw or len(raw) > MAX_APPENDED:
        return None
    limit = current_app.config["MAX_MESSAGE_CHARS"]
    cleaned = []
    for item in raw:
        if not isinstance(item, dict) or item.get("role") not in ROLES_OF_MESSAGES:
            return None
        content = item.get("content")
        if not isinstance(content, str) or not content.strip() or len(content) > limit:
            return None
        meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
        if len(json.dumps(meta)) > MAX_META_BYTES:
            return None
        cleaned.append({"role": item["role"], "content": content.strip(), "meta": meta})
    return cleaned


@bp.get("/conversations")
def list_conversations():
    project_id = request.args.get("project_id") or None
    found = _store().list_conversations(current_user()["id"], project_id,
                                        with_messages=request.args.get("include") == "messages")
    return jsonify({"conversations": [_public(c) for c in found]})


@bp.post("/conversations")
def create_conversation():
    body = _body()
    if body.get("id") is not None and _store().get_conversation(str(body["id"])):
        return _error("conflict", "That conversation already exists.", 409)
    conversation, problem = owned_or_new(body.get("id"), body.get("project_id"))
    if problem:
        return _error(*problem)
    title = " ".join(body["title"].split())[:MAX_TITLE] if isinstance(body.get("title"), str) else ""
    if title:
        _store().update_conversation(conversation["id"], title=title)
    return jsonify({"conversation": _public(_store().get_conversation(conversation["id"]))}), 201


@bp.get("/conversations/<conversation_id>")
def get_conversation(conversation_id: str):
    conversation = owned(conversation_id)
    if not conversation:
        return _error(*NOT_FOUND)
    return jsonify({"conversation": _public(conversation), "messages": _store().get_messages(conversation_id)})


@bp.patch("/conversations/<conversation_id>")
def update_conversation(conversation_id: str):
    body = _body()
    if not owned(conversation_id):
        return _error(*NOT_FOUND)
    title = None
    if "title" in body:
        title = " ".join(body["title"].split())[:MAX_TITLE] if isinstance(body["title"], str) else ""
        if not title:
            return _error("title_required", "Give the chat a name.", 400)
    excluded = bool(body["memory_excluded"]) if "memory_excluded" in body else None
    _store().update_conversation(conversation_id, title=title, memory_excluded=excluded)
    return jsonify({"conversation": _public(_store().get_conversation(conversation_id))})


@bp.delete("/conversations/<conversation_id>")
def delete_conversation(conversation_id: str):
    if not owned(conversation_id):
        return _error(*NOT_FOUND)
    _store().delete_conversation(conversation_id)
    return jsonify({"ok": True})


@bp.post("/conversations/<conversation_id>/messages")
def add_messages(conversation_id: str):
    body = _body()
    messages = clean_messages(body.get("messages"))
    if messages is None:
        return _error("bad_request", "Messages must be a short list of { role, content }.", 400)
    conversation, problem = owned_or_new(conversation_id, body.get("project_id"))
    if problem:
        return _error(*problem)
    saved = _store().add_messages(conversation["id"], messages)
    return jsonify({"conversation": _public(_store().get_conversation(conversation["id"])), "messages": saved}), 201


@bp.patch("/conversations/<conversation_id>/messages/<int:message_id>")
def update_message(conversation_id: str, message_id: int):
    body = _body()
    if not owned(conversation_id):
        return _error(*NOT_FOUND)
    content = body.get("content") if isinstance(body.get("content"), str) and body["content"].strip() else None
    meta = body.get("meta") if isinstance(body.get("meta"), dict) else None
    if meta is not None and len(json.dumps(meta)) > MAX_META_BYTES:
        return _error("bad_request", "That is too much to store with a message.", 400)
    if not _store().update_message(conversation_id, message_id, content, meta):
        return _error("not_found", "That message doesn't exist.", 404)
    return jsonify({"ok": True})

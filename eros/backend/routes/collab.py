"""Local-network collaboration: sharing a project as a workspace, joining it, and its participants.

  GET    /api/collab/me                      network details for the Collaboration page
  POST   /api/collab/join                    { code } -> the signed-in user joins that workspace
  POST   /api/projects/<pid>/leave           a participant leaves
  GET    /api/projects/<pid>/members         participants and whether they are active
  GET    /api/projects/<pid>/changes         { revision } - polled by devices to notice updates
  PATCH  /api/projects/<pid>/sharing         owner: { shared, default_role, new_code }
  PATCH  /api/members/<id>                   owner: { role }
  DELETE /api/members/<id>                   owner: remove a participant (their access ends at once)

A shared workspace is an ordinary project with sharing turned on. People join it
with their own account and the invitation code; what they may do there depends on
their role (services/access.py). Documents and findings are shared. Conversations
and memories stay private to each user.
"""
import time

from flask import Blueprint, current_app, jsonify, request

from services.access import current_user, lan_addresses
from services.auth import Limiter
from services.store_extras import ROLES

bp = Blueprint("collab", __name__, url_prefix="/api")

ACTIVE_SECONDS = 45                      # a participant seen this recently counts as active
_wrong_codes = Limiter(attempts=5, window=60)       # per account, to slow guessing of invitation codes


def _error(code: str, message: str, status: int):
    return jsonify({"error": {"code": code, "message": message}}), status


def _store():
    return current_app.extensions["store"]


def _body() -> dict:
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


@bp.get("/collab/me")
def me():
    cfg = current_app.config
    return jsonify({"lan": {
        "enabled": cfg["HOST"] not in ("127.0.0.1", "localhost"),
        "addresses": [f"http://{ip}:{cfg['PORT']}" for ip in lan_addresses()],
        "port": cfg["PORT"],
    }})


@bp.post("/collab/join")
def join():
    user = current_user()
    code = _body().get("code")
    code = "".join(code.split()).upper() if isinstance(code, str) else ""
    if _wrong_codes.wait(user["id"]):
        return _error("too_many_attempts", "Too many wrong codes. Wait a minute and try again.", 429)
    project = _store().shared_project_by_code(code) if code else None
    if not project:
        _wrong_codes.failed(user["id"])
        return _error("bad_code", "That invitation code isn't valid. Ask the workspace owner for the current code.", 404)
    if _store().get_project(project["id"])["user_id"] == user["id"]:
        return _error("own_workspace", "That is your own workspace.", 400)
    member = _store().add_member(project["id"], user["id"], user["username"], project["default_role"])
    return jsonify({"member": member, "project": {"id": project["id"], "title": project["title"]}}), 201


@bp.post("/projects/<project_id>/leave")
def leave(project_id: str):
    member = _store().membership(project_id, current_user()["id"])
    if member:
        _store().remove_member(member["id"])
    return jsonify({"ok": True})


@bp.get("/projects/<project_id>/members")
def members(project_id: str):
    now = time.time()
    people = [{**m, "active": now - m["last_seen"] < ACTIVE_SECONDS} for m in _store().list_members(project_id)]
    return jsonify({"members": people})


@bp.get("/projects/<project_id>/changes")
def changes(project_id: str):
    project = _store().get_project(project_id)
    return jsonify({"revision": project["revision"], "shared": project["shared"]})


@bp.patch("/projects/<project_id>/sharing")
def sharing(project_id: str):
    store, body = _store(), _body()
    role = body.get("default_role")
    if role is not None and role not in ROLES:
        return _error("bad_request", "Unknown role.", 400)
    shared = body.get("shared")
    store.set_sharing(project_id, shared=bool(shared) if shared is not None else None,
                      default_role=role, new_code=bool(body.get("new_code")))
    project = store.get_project(project_id)
    return jsonify({"project": {k: v for k, v in project.items() if k != "user_id"}})


@bp.patch("/members/<member_id>")
def update_member(member_id: str):
    role = _body().get("role")
    if role not in ROLES:
        return _error("bad_request", "Unknown role.", 400)
    _store().update_member(member_id, role)
    return jsonify({"ok": True})


@bp.delete("/members/<member_id>")
def remove_member(member_id: str):
    _store().remove_member(member_id)
    return jsonify({"ok": True})

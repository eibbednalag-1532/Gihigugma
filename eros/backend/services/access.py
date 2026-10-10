"""Who may call which API endpoint.

Every request to /api passes through `guard` first:

  1. Requests sent by other websites are refused (cross-site request forgery).
  2. The session cookie is looked up on the server. No valid session means not signed in.
  3. Anything that changes data must also carry the session's CSRF token in a header.
  4. The endpoint must be listed in RULES. Unlisted endpoints are refused, so a new
     route is private until someone decides otherwise.
  5. Endpoints that belong to a project check the signed-in user's role in that project:
     the owner may do everything; people who joined a shared workspace have the role
     the owner gave them.

The user is always taken from the server-side session. A user id sent by the
browser is never used. Conversations and memories are personal: their routes
check ownership themselves (see routes/conversations.py, routes/memory.py).
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from flask import current_app, g, jsonify, request

from services import auth

SAFE_METHODS = ("GET", "HEAD", "OPTIONS")

# What each role may do inside a project. The owner is whoever created it.
PERMISSIONS = {
    "viewer": {"view"},
    "contributor": {"view", "add", "edit"},
    "manager": {"view", "add", "edit", "delete"},
    "owner": {"view", "add", "edit", "delete", "manage"},
}

# endpoint -> (requirement, where the request names its project)
#   "open"   no sign-in needed            "user"   any signed-in user
#   otherwise a permission from PERMISSIONS, with a locator:
#   "url" (<project_id>), "body" (JSON project_id), or a resource whose project is looked up
RULES = {
    "health.health": ("open", None),
    "auth.register": ("open", None),
    "auth.login": ("open", None),
    "auth.logout": ("open", None),
    "auth.me": ("open", None),

    "health.models": ("user", None),
    "paraphrase.run": ("user", None),
    "projects.list_projects": ("user", None),
    "projects.create_project": ("user", None),
    "collab.me": ("user", None),
    "collab.join": ("user", None),
    # Personal data: each route only ever touches rows that belong to the signed-in user.
    "conversations.list_conversations": ("user", None),
    "conversations.create_conversation": ("user", None),
    "conversations.get_conversation": ("user", None),
    "conversations.update_conversation": ("user", None),
    "conversations.delete_conversation": ("user", None),
    "conversations.add_messages": ("user", None),
    "conversations.update_message": ("user", None),
    "chat.chat": ("user", None),
    "memory.get_settings": ("user", None),
    "memory.put_settings": ("user", None),
    "memory.list_memories": ("user", None),
    "memory.add_memory": ("user", None),
    "memory.update_memory": ("user", None),
    "memory.delete_memory": ("user", None),
    "memory.clear_memories": ("user", None),
    "memory.suggest_memories": ("user", None),

    "projects.get_project": ("view", "url"),
    "projects.update_project": ("manage", "url"),
    "projects.delete_project": ("manage", "url"),
    "projects.relevance": ("view", "url"),
    "projects.list_findings": ("view", "url"),
    "projects.create_finding": ("add", "url"),
    "projects.update_finding": ("edit", "finding"),
    "projects.explain_finding": ("edit", "finding"),
    "projects.delete_finding": ("delete", "finding"),
    "documents.list_documents": ("view", "url"),
    "documents.upload": ("add", "url"),
    "documents.get_document": ("view", "document"),
    "documents.delete_document": ("delete", "document"),
    "memory.search_chats": ("view", "url"),
    "tools.run_tool": ("view", "body"),
    "collab.members": ("view", "url"),
    "collab.changes": ("view", "url"),
    "collab.leave": ("view", "url"),
    "collab.sharing": ("manage", "url"),
    "collab.update_member": ("manage", "member"),
    "collab.remove_member": ("manage", "member"),
}


def current_user() -> dict | None:
    """The signed-in user of this request, from the server-side session."""
    return getattr(g, "user", None)


def project_role(project_id: str | None, user: dict | None = None) -> str | None:
    """The user's role in a project: "owner", the role they were given when they joined, or None."""
    user = user or current_user()
    store = current_app.extensions["store"]
    project = store.get_project(project_id) if isinstance(project_id, str) else None
    if not project or not user:
        return None
    if project["user_id"] == user["id"]:
        return "owner"
    member = store.membership(project_id, user["id"]) if project["shared"] else None
    return member["role"] if member else None


def can(permission: str, project_id: str | None) -> bool:
    return permission in PERMISSIONS.get(project_role(project_id) or "", set())


def _refuse(code: str, message: str, status: int):
    return jsonify({"error": {"code": code, "message": message}}), status


def _project_of(locator: str) -> str | None:
    store = current_app.extensions["store"]
    args = request.view_args or {}
    if locator == "url":
        return args.get("project_id")
    if locator == "body":
        body = request.get_json(silent=True)
        return body.get("project_id") if isinstance(body, dict) else None
    found = {"document": lambda: store.get_document(args.get("doc_id", "")),
             "finding": lambda: store.get_finding(args.get("finding_id", "")),
             "member": lambda: store.get_member(args.get("member_id", ""))}[locator]()
    return found["project_id"] if found else None


def session_cookie(response, token: str | None):
    """Set (or, with None, clear) the session cookie on a response.

    HttpOnly keeps page scripts from reading it and SameSite=Strict keeps the
    browser from sending it with requests that other sites start. Secure is
    added whenever the page was loaded over HTTPS, so the cookie is then never
    sent in the clear. Over plain http:// it cannot be set (the browser would
    drop the cookie), and the cookie is readable by anyone capturing the traffic.
    """
    cfg = current_app.config
    secure = request.is_secure or cfg["COOKIE_SECURE"]
    if token is None:
        response.delete_cookie(auth.COOKIE, path="/", secure=secure, httponly=True, samesite="Strict")
    else:
        response.set_cookie(auth.COOKIE, token, max_age=cfg["SESSION_LIFETIME"], path="/",
                            secure=secure, httponly=True, samesite="Strict")
    return response


def guard():
    """Runs before every request. Returns an error response to stop it, or None to let it through."""
    if not request.path.startswith("/api/"):
        return None                                   # the page itself; it holds no data

    # A page from another site must not be able to drive this API through the user's browser.
    origin = request.headers.get("Origin")
    if origin and urlparse(origin).netloc != request.host:
        return _refuse("cross_origin", "Requests from other sites are not allowed.", 403)
    # The address used must be a local name or an IP address, so a hostile web page can't
    # reach the API by pointing its own domain name at this computer.
    name = request.host.rsplit(":", 1)[0].strip("[]")
    if name != "localhost":
        try:
            ipaddress.ip_address(name)
        except ValueError:
            return _refuse("bad_host", "Open this app by its IP address, for example http://127.0.0.1:5050.", 403)

    cfg = current_app.config
    store = current_app.extensions["store"]
    g.user = g.session_hash = g.csrf_token = None
    token = request.cookies.get(auth.COOKIE, "")
    if token:
        token_hash = auth.hash_token(cfg["SECRET"], token)
        session = store.get_session(token_hash, cfg["SESSION_IDLE"])
        if session:
            g.user, g.session_hash, g.csrf_token = session["user"], token_hash, session["csrf_token"]

    rule = RULES.get(request.endpoint)
    if rule is None:
        if request.endpoint is None:
            return None                               # unknown URL: the normal 404 answers it
        return _refuse("not_allowed", "That isn't available.", 403)
    requirement, locator = rule

    if g.user and request.method not in SAFE_METHODS:
        sent = request.headers.get(auth.CSRF_HEADER, "")
        if not sent or not auth.same(sent, g.csrf_token):
            return _refuse("csrf", "This page is out of date. Reload it and try again.", 403)
    if requirement == "open":
        return None
    if not g.user:
        return _refuse("not_signed_in", "Sign in to continue.", 401)
    if requirement == "user":
        return None

    project_id = _project_of(locator)
    role = project_role(project_id)
    if role is None:
        # The same answer whether the project doesn't exist or belongs to someone else.
        return _refuse("not_found", "That doesn't exist, or you don't have access to it.", 404)
    if requirement not in PERMISSIONS[role]:
        return _refuse("not_allowed", f"Your role ({role}) doesn't allow this.", 403)
    return None


def lan_addresses() -> list[str]:
    """This computer's local-network IPv4 addresses (what other devices type to connect)."""
    found: list[str] = []
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("10.255.255.255", 1))          # no packet is sent; this only picks the outgoing interface
        found.append(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    try:
        found += socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        pass
    return [ip for ip in dict.fromkeys(found) if not ip.startswith("127.")]

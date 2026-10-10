"""Accounts: register, sign in, sign out, and "who am I".

  POST /api/auth/register   { username, email, password, confirm_password } -> { user, csrf_token }
  POST /api/auth/login      { identifier (username or email), password }     -> { user, csrf_token }
  POST /api/auth/logout
  GET  /api/auth/me         -> { user, csrf_token }  or 401 when not signed in

The session id travels only in an HttpOnly cookie; it is never put in a response
body. The csrf_token is not a login secret: the page keeps it in memory and
sends it back in a header with every change, which a page on another site cannot do.
Failures say what the user can fix and nothing about the database or the server.
"""
import secrets

from flask import Blueprint, current_app, g, jsonify, make_response, request

from services import auth
from services.access import session_cookie
from services.store_auth import Duplicate

bp = Blueprint("auth", __name__, url_prefix="/api/auth")

BAD_LOGIN = "Incorrect username, email or password."


def make_limits() -> dict:
    """Pauses that slow password guessing: 5 wrong tries per account name and 20 per device in
    5 minutes, and 10 new accounts per device per hour. Kept in memory by the running server."""
    return {"name": auth.Limiter(attempts=5, window=300), "address": auth.Limiter(attempts=20, window=300),
            "registrations": auth.Limiter(attempts=10, window=3600)}


def _error(code: str, message: str, status: int, **extra):
    return jsonify({"error": {"code": code, "message": message, **extra}}), status


def _body() -> dict:
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


def _public(user: dict) -> dict:
    return {"id": user["id"], "username": user["username"], "email": user["email"]}


def _start_session(user: dict, status: int):
    """Sign `user` in: a brand-new session id every time, so an id planted before login is useless."""
    cfg, store = current_app.config, current_app.extensions["store"]
    if g.get("session_hash"):
        store.delete_session(g.session_hash)
    token, csrf = auth.new_token(), secrets.token_urlsafe(24)
    store.create_session(user["id"], auth.hash_token(cfg["SECRET"], token), csrf, cfg["SESSION_LIFETIME"])
    response = make_response(jsonify({"user": _public(user), "csrf_token": csrf}), status)
    return session_cookie(response, token)


@bp.post("/register")
def register():
    store = current_app.extensions["store"]
    _registrations = current_app.extensions["limits"]["registrations"]
    address = request.remote_addr or ""
    if _registrations.wait(address):
        return _error("too_many_attempts", "Too many accounts were created from this device. Try again later.", 429)
    # Whatever was saved before accounts existed goes to the first account, so it has to be
    # created at the host computer itself, not by whoever reaches the server first.
    if store.count_users() == 0 and address not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
        return _error("host_first", "The first account must be created on the host computer. "
                      "Ask the person running Gihigugma to set it up.", 403)
    try:
        username, email, password = auth.validate_registration(_body())
        user = store.create_user(username, email, auth.hash_password(password))
    except auth.Invalid as problem:
        return _error("invalid", "Please correct the highlighted fields.", 400, fields=problem.fields)
    except Duplicate as taken:
        messages = {"username": "That username is already taken.", "email": "An account with that email already exists."}
        return _error("taken", "That account already exists.", 409, fields={f: messages[f] for f in taken.fields})
    _registrations.failed(address)          # counts accounts created, not failures
    return _start_session(user, 201)


@bp.post("/login")
def login():
    store, body = current_app.extensions["store"], _body()
    limits = current_app.extensions["limits"]
    _by_name, _by_address = limits["name"], limits["address"]
    identifier = auth.normalize_email(body.get("identifier"))
    password = body.get("password")
    address = request.remote_addr or ""
    if not identifier or not isinstance(password, str) or not password:
        return _error("invalid", "Enter your username or email and your password.", 400)

    wait = max(_by_name.wait(identifier), _by_address.wait(address))
    if wait:
        return _error("too_many_attempts", f"Too many failed attempts. Try again in {max(1, wait // 60)} minute(s).", 429)

    found = store.find_login(identifier)
    user, stored_hash = found if found else (None, None)
    if not auth.verify_password(stored_hash, password[:auth.PASSWORD_MAX]):
        _by_name.failed(identifier)
        _by_address.failed(address)
        # One message for every kind of failure, so it doesn't reveal which accounts exist.
        return _error("bad_login", BAD_LOGIN, 401)

    _by_name.clear(identifier)
    if auth.needs_rehash(stored_hash):      # hashing parameters were strengthened since this password was set
        store.set_password_hash(user["id"], auth.hash_password(password))
    return _start_session(user, 200)


@bp.post("/logout")
def logout():
    """End the session on the server and remove the cookie. Safe to call when not signed in."""
    if g.get("session_hash"):
        current_app.extensions["store"].delete_session(g.session_hash)
    return session_cookie(make_response(jsonify({"ok": True})), None)


@bp.get("/me")
def me():
    if not g.get("user"):
        return _error("not_signed_in", "Sign in to continue.", 401)
    return jsonify({"user": _public(g.user), "csrf_token": g.csrf_token})

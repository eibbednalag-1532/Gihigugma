"""Accounts and sessions: password hashing, input rules, session cookies, brute-force limits.

How a session works
  - At login the server makes a random 256-bit session id and sends it to the
    browser in a cookie the page's JavaScript cannot read (HttpOnly).
  - The server stores only a keyed hash of that id, with the user, an expiry
    and the time of last use. A copy of the database is therefore not enough
    to impersonate anyone.
  - Every request looks the cookie up again; logout deletes the row.

What this does NOT do: encrypt anything in transit. Over plain http:// on a
shared Wi-Fi network, passwords and the cookie travel in the clear and can be
read by someone capturing traffic. HttpOnly only keeps scripts away from the
cookie. Use HTTPS (see the README) if that matters for your network.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
import unicodedata
from collections import defaultdict, deque
from pathlib import Path

try:
    from argon2 import PasswordHasher
    from argon2.exceptions import InvalidHashError, VerificationError
except ImportError as missing:      # a clear instruction beats a traceback at startup
    raise SystemExit("The 'argon2-cffi' package is needed for accounts. Install it with:\n"
                     "    pip install -r requirements.txt") from missing

COOKIE = "gihigugma_session"
CSRF_HEADER = "X-CSRF-Token"

USERNAME_RULE = re.compile(r"[a-z0-9][a-z0-9._-]{2,29}")
EMAIL_RULE = re.compile(r"[^@\s]+@[^@\s.]+(\.[^@\s.]+)+")
PASSWORD_MIN, PASSWORD_MAX = 8, 128

# Argon2id with the library's current recommended parameters.
_hasher = PasswordHasher()
# Verified when the account doesn't exist, so a wrong username takes as long as a wrong password.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


class Invalid(Exception):
    """Input the user can correct. `fields` maps a form field to a message for it."""

    def __init__(self, fields: dict[str, str]):
        super().__init__("; ".join(fields.values()))
        self.fields = fields


# ---------- input rules ----------
def normalize_username(value) -> str:
    """Usernames are compared case-insensitively: trimmed, Unicode-normalised, lower-cased."""
    return unicodedata.normalize("NFKC", value).strip().lower() if isinstance(value, str) else ""


def normalize_email(value) -> str:
    return unicodedata.normalize("NFKC", value).strip().lower() if isinstance(value, str) else ""


def validate_registration(body: dict) -> tuple[str, str, str]:
    """Checked username, email and password from a registration form, or Invalid with a message per field."""
    username, email = normalize_username(body.get("username")), normalize_email(body.get("email"))
    password, confirm = body.get("password"), body.get("confirm_password")
    problems = {}
    if not username:
        problems["username"] = "Choose a username."
    elif not USERNAME_RULE.fullmatch(username):
        problems["username"] = ("Usernames are 3 to 30 characters: letters, digits, dot, dash or underscore, "
                                "starting with a letter or digit.")
    if not email:
        problems["email"] = "Enter your email address."
    elif len(email) > 254 or not EMAIL_RULE.fullmatch(email):
        problems["email"] = "That doesn't look like an email address."
    if not isinstance(password, str) or not password:
        problems["password"] = "Choose a password."
    elif len(password) < PASSWORD_MIN:
        problems["password"] = f"Use at least {PASSWORD_MIN} characters."
    elif len(password) > PASSWORD_MAX:
        problems["password"] = f"Use at most {PASSWORD_MAX} characters."
    elif password.lower() in (username, email):
        problems["password"] = "Don't use your username or email as the password."
    if "password" not in problems and password != confirm:
        problems["confirm_password"] = "The two passwords don't match."
    if problems:
        raise Invalid(problems)
    return username, email, password


# ---------- passwords ----------
def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str | None, password: str) -> bool:
    """True if the password matches. Always does the work of one verification."""
    try:
        return _hasher.verify(stored_hash or _DUMMY_HASH, password) and stored_hash is not None
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    return _hasher.check_needs_rehash(stored_hash)


# ---------- secret key and session ids ----------
def load_secret(configured: str, data_dir: Path) -> bytes:
    """The key that session ids are hashed with: EROS_SECRET_KEY, or a random one kept in the data folder.

    It is created on first run and never appears in source code. The data
    folder is outside the folder the web server serves.
    """
    if configured:
        return configured.encode()
    path = Path(data_dir) / "secret.key"
    if path.is_file():
        return path.read_bytes().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_hex(32).encode()
    path.write_bytes(key)
    return key


def new_token() -> str:
    return secrets.token_urlsafe(32)        # 256 bits from the operating system's secure generator


def hash_token(secret: bytes, token: str) -> str:
    return hmac.new(secret, token.encode(), hashlib.sha256).hexdigest()


def same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


# ---------- brute-force limits ----------
class Limiter:
    """Allows a number of failures per key within a time window; kept in memory on the host."""

    def __init__(self, attempts: int, window: float):
        self.attempts, self.window = attempts, window
        self.failures: dict[str, deque] = defaultdict(deque)

    def wait(self, key: str) -> int:
        """Seconds until `key` may try again (0 if it may try now)."""
        now, recent = time.time(), self.failures[key]
        while recent and now - recent[0] > self.window:
            recent.popleft()
        return int(self.window - (now - recent[0])) + 1 if len(recent) >= self.attempts else 0

    def failed(self, key: str) -> None:
        self.failures[key].append(time.time())

    def clear(self, key: str) -> None:
        self.failures.pop(key, None)

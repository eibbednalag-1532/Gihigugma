"""A stand-in for Ollama, so the tests run with no model, no GPU and no network.

Embeddings are word-count vectors (texts sharing words are similar), which is
enough for retrieval to behave meaningfully. Chat replies are canned, and every
request is recorded so tests can check exactly what the model would have been sent.
"""
import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app import create_app  # noqa: E402
from config import Config  # noqa: E402
from services.ollama_client import OllamaError  # noqa: E402

DIM = 256
REMOTE = {"REMOTE_ADDR": "192.168.1.50"}      # a request arriving from another device on the network


class FakeOllama:
    def __init__(self):
        self.models = [
            {"name": "test-chat:latest", "can_chat": True, "thinks": False},
            {"name": "embeddinggemma:latest", "can_chat": False, "thinks": False},
        ]
        self.offline = False
        self.timeout = False             # Ollama accepts the request but never answers in time
        self.break_after = None          # the reply breaks off after this many words
        self.reply = "According to the document, the answer is 42 [1]."
        self.json_reply = '{"memories": []}'
        self.calls = []          # every chat request: {"model", "messages", "options"}

    def _check(self):
        if self.offline:
            raise OllamaError("ollama_offline", "Ollama isn't reachable.", 503)

    def version(self):
        self._check()
        return "0.0-test"

    def list_models(self):
        self._check()
        return [dict(m) for m in self.models]

    def resolve_name(self, name, installed=None):
        names = {m["name"] for m in (installed if installed is not None else self.list_models())}
        for candidate in (name, f"{name}:latest", name.removesuffix(":latest")):
            if candidate in names:
                return candidate
        return None

    def has_model(self, name, installed=None):
        return self.resolve_name(name, installed) is not None

    def embed(self, model, texts):
        self._check()
        vectors = []
        for text in texts:
            vector = [0.0] * DIM
            for word in re.findall(r"[a-z0-9]+", text.lower()):
                if len(word) > 3:          # skip "the", "and", ... so unrelated texts don't look alike
                    vector[int(hashlib.md5(word.encode()).hexdigest(), 16) % DIM] += 1.0
            vectors.append(vector)
        return vectors

    def chat_stream(self, model, messages, options=None, think=None):
        self._check()
        if self.timeout:
            raise OllamaError("ollama_timeout", "Ollama didn't respond in time.", 504)
        self.calls.append({"model": model, "messages": messages, "options": options})
        reply = self.reply(messages) if callable(self.reply) else self.reply
        words = reply.split(" ")
        for i, word in enumerate(words):
            if self.break_after is not None and i >= self.break_after:
                raise OllamaError("stream_interrupted", "Ollama stopped responding mid-reply.", 504)
            yield {"message": {"content": word + (" " if i < len(words) - 1 else "")}, "done": False}
        yield {"message": {"content": ""}, "done": True, "done_reason": "stop", "eval_count": len(words),
               "eval_duration": 1_000_000}

    def chat(self, model, messages, options=None, think=None, fmt=None):
        if fmt:
            self._check()
            self.calls.append({"model": model, "messages": messages, "options": options, "fmt": fmt})
            return self.json_reply
        return "".join(c["message"]["content"] for c in self.chat_stream(model, messages, options, think)).strip()


def make_app(folder=None, fake=None):
    """An app on a temporary database with the fake model. Pass `folder` to reopen an
    existing database, which is what restarting the server does."""
    folder = folder or tempfile.mkdtemp(prefix="gihigugma-test-")

    class TestConfig(Config):
        DATA_DIR = Path(folder)
        MIN_SCORE = 0.2

    fake = fake or FakeOllama()
    app = create_app(TestConfig, ollama=fake)
    return app, fake, Path(folder)


class Browser:
    """One browser: it keeps its cookies, and sends the CSRF token it was given like the page does."""

    def __init__(self, app, remote=False):
        self.client = app.test_client()
        self.base = dict(REMOTE) if remote else {}
        self.csrf = None
        self.user = None

    def open(self, url, method="GET", **kwargs):
        headers = dict(kwargs.pop("headers", None) or {})
        if self.csrf and "X-CSRF-Token" not in headers:
            headers["X-CSRF-Token"] = self.csrf
        kwargs.setdefault("environ_base", self.base)
        return self.client.open(url, method=method, headers=headers, **kwargs)

    def get(self, url, **kwargs):
        return self.open(url, "GET", **kwargs)

    def post(self, url, **kwargs):
        return self.open(url, "POST", **kwargs)

    def put(self, url, **kwargs):
        return self.open(url, "PUT", **kwargs)

    def patch(self, url, **kwargs):
        return self.open(url, "PATCH", **kwargs)

    def delete(self, url, **kwargs):
        return self.open(url, "DELETE", **kwargs)

    def _signed_in(self, response):
        body = response.get_json() or {}
        if "csrf_token" in body:
            self.csrf, self.user = body["csrf_token"], body["user"]
        return response

    def register(self, username, password="correct horse 42", email=None):
        return self._signed_in(self.post("/api/auth/register", json={
            "username": username, "email": email or f"{username}@example.com",
            "password": password, "confirm_password": password}))

    def login(self, identifier, password="correct horse 42"):
        return self._signed_in(self.post("/api/auth/login", json={"identifier": identifier, "password": password}))

    def logout(self):
        response = self.post("/api/auth/logout")
        self.csrf = self.user = None
        return response

    def cookie(self):
        found = self.client.get_cookie("gihigugma_session")
        return found.value if found else None


def events(response):
    """The NDJSON events of a streamed response."""
    return [json.loads(line) for line in response.get_data(as_text=True).splitlines() if line.strip()]


def text_of(response):
    return "".join(e["content"] for e in events(response) if e["type"] == "token")


def upload(client, project_id, name, data, **kwargs):
    import io
    return client.post(f"/api/projects/{project_id}/documents",
                       data={"file": (io.BytesIO(data), name)}, content_type="multipart/form-data", **kwargs)

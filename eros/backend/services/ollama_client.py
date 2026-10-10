"""Thin client for the local Ollama HTTP API.

This module is the only place in Gihigugma that talks to Ollama. Every failure is
converted into an OllamaError with a stable `code` the frontend can act on.

Ollama endpoints used:
  GET  /api/version  - is Ollama running?
  GET  /api/tags     - installed models
  POST /api/chat     - chat completion (streamed as newline-delimited JSON)
"""
from __future__ import annotations

import json
from typing import Iterator

import requests


class OllamaError(Exception):
    """A failure the user can understand and usually fix."""

    def __init__(self, code: str, message: str, status: int = 502):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


class OllamaClient:
    def __init__(self, base_url: str, connect_timeout: float = 3.0,
                 status_timeout: float = 4.0, stream_read_timeout: float = 300.0,
                 keep_alive: str = "10m"):
        self.base_url = base_url.rstrip("/")
        self.connect_timeout = connect_timeout
        self.status_timeout = status_timeout
        self.stream_read_timeout = stream_read_timeout
        self.keep_alive = keep_alive
        self.session = requests.Session()
        # Never route local Ollama traffic through a system proxy.
        self.session.trust_env = False

    # ---------- helpers ----------
    def _offline_error(self) -> OllamaError:
        return OllamaError(
            "ollama_offline",
            f"Ollama isn't reachable at {self.base_url}. Start it with `ollama serve` "
            "or open the Ollama app, then try again.",
            503,
        )

    def _get(self, path: str) -> dict:
        try:
            res = self.session.get(self.base_url + path,
                                   timeout=(self.connect_timeout, self.status_timeout))
        except (requests.ConnectionError, requests.Timeout):
            raise self._offline_error() from None
        if res.status_code != 200:
            raise OllamaError("ollama_error", f"Ollama returned HTTP {res.status_code} for {path}.", 502)
        try:
            return res.json()
        except ValueError:
            raise OllamaError("ollama_error", f"Ollama sent an unreadable response for {path}.", 502) from None

    # ---------- status ----------
    def version(self) -> str:
        return self._get("/api/version").get("version", "unknown")

    def list_models(self) -> list[dict]:
        """Installed models, newest first, in a frontend-friendly shape."""
        data = self._get("/api/tags")
        models = []
        for m in data.get("models", []):
            details = m.get("details") or {}
            name = m.get("name") or m.get("model") or ""
            caps = m.get("capabilities")
            if caps is not None:
                can_chat = "completion" in caps
            else:   # older Ollama versions don't report capabilities; fall back to the name
                can_chat = not any(t in name.lower() for t in ("embed", "bge-", "minilm"))
            models.append({
                "can_chat": can_chat,
                "thinks": "thinking" in (caps or []),
                "name": m.get("name") or m.get("model"),
                "size": m.get("size"),
                "modified_at": m.get("modified_at"),
                "family": details.get("family"),
                "parameter_size": details.get("parameter_size"),
                "quantization": details.get("quantization_level"),
            })
        models.sort(key=lambda m: m.get("modified_at") or "", reverse=True)
        return [m for m in models if m["name"]]

    def resolve_name(self, name: str, installed: list[dict] | None = None) -> str | None:
        """Exact installed name for `name`, or None if it isn't installed.

        Ollama treats "llama3.2" and "llama3.2:latest" as the same model.
        """
        names = {m["name"] for m in (installed if installed is not None else self.list_models())}
        for candidate in (name, f"{name}:latest", name.removesuffix(":latest")):
            if candidate in names:
                return candidate
        return None

    def has_model(self, name: str, installed: list[dict] | None = None) -> bool:
        return self.resolve_name(name, installed) is not None

    # ---------- embeddings ----------
    def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        """Embed `texts` with `model`; returns one vector per input, in order."""
        try:
            res = self.session.post(self.base_url + "/api/embed",
                                    json={"model": model, "input": texts, "keep_alive": self.keep_alive},
                                    timeout=(self.connect_timeout, self.stream_read_timeout))
        except requests.ConnectionError:
            raise self._offline_error() from None
        except requests.Timeout:
            raise OllamaError("ollama_timeout", "Ollama didn't respond in time while embedding.", 504) from None

        if res.status_code != 200:
            try:
                detail = res.json().get("error", "")
            except ValueError:
                detail = res.text[:300]
            if res.status_code == 404 or "not found" in detail.lower():
                raise OllamaError("embed_model_not_found",
                                  f"The embedding model '{model}' isn't installed. Run `ollama pull {model}`.", 404)
            raise OllamaError("ollama_error", f"Ollama error: {detail or 'HTTP ' + str(res.status_code)}", 502)

        try:
            vectors = res.json().get("embeddings")
        except ValueError:
            vectors = None
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise OllamaError("ollama_error", "Ollama returned unexpected embedding data.", 502)
        return vectors

    # ---------- chat ----------
    def chat(self, model: str, messages: list[dict], options: dict | None = None,
             think: bool | None = None, fmt: str | None = None) -> str:
        """A complete (non-streamed) reply as text. `fmt="json"` makes the model answer in JSON."""
        if fmt:
            return self._chat_once(model, messages, options, think, fmt)
        return "".join((chunk.get("message") or {}).get("content") or ""
                       for chunk in self.chat_stream(model, messages, options, think)).strip()

    def _chat_once(self, model: str, messages: list[dict], options: dict | None,
                   think: bool | None, fmt: str) -> str:
        payload = {"model": model, "messages": messages, "stream": False, "format": fmt,
                   "keep_alive": self.keep_alive}
        if options:
            payload["options"] = options
        if think is not None:
            payload["think"] = think
        try:
            res = self.session.post(self.base_url + "/api/chat", json=payload,
                                    timeout=(self.connect_timeout, self.stream_read_timeout))
        except requests.ConnectionError:
            raise self._offline_error() from None
        except requests.Timeout:
            raise OllamaError("ollama_timeout", "Ollama didn't respond in time.", 504) from None
        if res.status_code != 200:
            raise OllamaError("ollama_error", f"Ollama returned HTTP {res.status_code}.", 502)
        try:
            return ((res.json().get("message") or {}).get("content") or "").strip()
        except ValueError:
            raise OllamaError("ollama_error", "Ollama sent an unreadable response.", 502) from None

    def chat_stream(self, model: str, messages: list[dict], options: dict | None = None,
                    think: bool | None = None) -> Iterator[dict]:
        """Yield Ollama stream chunks as dicts. Raises OllamaError on failure.

        The HTTP request is opened eagerly by the caller iterating once; errors
        before the first chunk (offline, unknown model) raise immediately.
        `think=False` turns off the hidden reasoning pass of "thinking" models.
        """
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "keep_alive": self.keep_alive,
        }
        if options:
            payload["options"] = options
        if think is not None:
            payload["think"] = think

        try:
            res = self.session.post(self.base_url + "/api/chat", json=payload, stream=True,
                                    timeout=(self.connect_timeout, self.stream_read_timeout))
        except requests.ConnectionError:
            raise self._offline_error() from None
        except requests.Timeout:
            raise OllamaError("ollama_timeout", "Ollama didn't respond in time.", 504) from None

        if res.status_code != 200:
            detail = ""
            try:
                detail = res.json().get("error", "")
            except ValueError:
                detail = res.text[:300]
            res.close()
            if res.status_code == 404 or "not found" in detail.lower():
                raise OllamaError("model_not_found",
                                  f"The model '{model}' isn't installed. Run `ollama pull {model}`.", 404)
            raise OllamaError("ollama_error", f"Ollama error: {detail or 'HTTP ' + str(res.status_code)}", 502)

        try:
            for line in res.iter_lines(decode_unicode=True):
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except ValueError:
                    continue
                if chunk.get("error"):
                    raise OllamaError("ollama_error", f"Ollama error: {chunk['error']}", 502)
                yield chunk
                if chunk.get("done"):
                    break
        except requests.exceptions.ChunkedEncodingError:
            raise OllamaError("stream_interrupted", "The connection to Ollama was interrupted mid-reply.", 502) from None
        except (requests.ConnectionError, requests.Timeout):
            raise OllamaError("stream_interrupted", "Ollama stopped responding mid-reply.", 504) from None
        finally:
            res.close()

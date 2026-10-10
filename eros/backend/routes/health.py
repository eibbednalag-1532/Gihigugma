"""Status endpoints: is the backend up, is Ollama reachable, which models exist."""
from flask import Blueprint, current_app, jsonify

from services.ollama_client import OllamaError

bp = Blueprint("health", __name__, url_prefix="/api")


def _client():
    return current_app.extensions["ollama"]


@bp.get("/health")
def health():
    """Always 200 when the backend is running; Ollama status is reported inside.

    Status is checked live on every call, so the UI never shows "connected"
    unless Ollama actually answered just now.
    """
    cfg = current_app.config
    result = {
        "ok": True,
        "server": "eros",
        "ollama": {"reachable": False, "url": cfg["OLLAMA_URL"], "version": None},
        "models": [],
        "default_model": cfg["DEFAULT_MODEL"] or None,
        # Needed for reading documents; the page warns when it is missing.
        "embed_model": {"name": cfg["EMBED_MODEL"], "installed": False},
        "min_score": cfg["MIN_SCORE"],
    }
    try:
        result["ollama"]["version"] = _client().version()
        result["ollama"]["reachable"] = True
        installed = _client().list_models()
        result["models"] = [m["name"] for m in installed if m["can_chat"]]   # embedding-only models can't chat
        result["embed_model"]["installed"] = _client().has_model(cfg["EMBED_MODEL"], installed)
    except OllamaError as err:
        result["ollama"]["error"] = err.to_dict()
    return jsonify(result)


@bp.get("/models")
def models():
    try:
        return jsonify({"models": _client().list_models()})
    except OllamaError as err:
        return jsonify({"error": err.to_dict()}), err.status

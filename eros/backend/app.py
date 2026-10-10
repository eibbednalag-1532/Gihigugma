"""Gihigugma backend: serves the frontend and a local-only API in front of Ollama.

Run:  python app.py          (from the backend/ folder) -> http://127.0.0.1:5050, this computer only
      python app.py --lan    also reachable from other devices on the same Wi-Fi or hotspot,
                             for shared workspaces (see the Collaboration page)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Allow `python app.py` from any working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from flask import Flask, jsonify, request, send_from_directory  # noqa: E402

from config import Config  # noqa: E402
from routes.auth import bp as auth_bp, make_limits  # noqa: E402
from routes.chat import bp as chat_bp  # noqa: E402
from routes.collab import bp as collab_bp  # noqa: E402
from routes.conversations import bp as conversations_bp  # noqa: E402
from routes.documents import bp as documents_bp  # noqa: E402
from routes.health import bp as health_bp  # noqa: E402
from routes.memory import bp as memory_bp  # noqa: E402
from routes.paraphrase import bp as paraphrase_bp  # noqa: E402
from routes.projects import bp as projects_bp  # noqa: E402
from routes.tools import bp as tools_bp  # noqa: E402
from services.access import guard, lan_addresses  # noqa: E402
from services.auth import load_secret  # noqa: E402
from services.ollama_client import OllamaClient, OllamaError  # noqa: E402
from services.store import ResearchStore  # noqa: E402


def create_app(config: type[Config] = Config, ollama=None) -> Flask:
    """Build the app. `ollama` replaces the Ollama client (the tests pass a stand-in)."""
    app = Flask(__name__, static_folder=None)
    app.config.from_object(config)
    app.config["MAX_CONTENT_LENGTH"] = config.MAX_REQUEST_BYTES

    app.extensions["ollama"] = ollama or OllamaClient(
        base_url=config.OLLAMA_URL,
        connect_timeout=config.CONNECT_TIMEOUT,
        status_timeout=config.STATUS_TIMEOUT,
        stream_read_timeout=config.STREAM_READ_TIMEOUT,
        keep_alive=config.KEEP_ALIVE,
    )

    app.extensions["store"] = ResearchStore(config.DATA_DIR / "eros.db")

    # The key session ids are hashed with: from EROS_SECRET_KEY, or generated once into the data folder.
    app.config["SECRET"] = load_secret(config.SECRET_KEY, config.DATA_DIR)
    app.extensions["limits"] = make_limits()

    # Every API request passes the access check first: it identifies the signed-in user from
    # the session cookie and refuses anything that user may not do.
    app.before_request(guard)

    for blueprint in (health_bp, auth_bp, chat_bp, conversations_bp, projects_bp, documents_bp, tools_bp,
                      paraphrase_bp, memory_bp, collab_bp):
        app.register_blueprint(blueprint)

    frontend = config.FRONTEND_DIR

    @app.get("/")
    def index():
        return send_from_directory(frontend, "index.html")

    @app.get("/<path:filename>")
    def static_files(filename: str):
        # send_from_directory refuses paths outside the frontend folder.
        return send_from_directory(frontend, filename)

    @app.errorhandler(404)
    def not_found(_err):
        if request.path.startswith("/api/"):
            return jsonify({"error": {"code": "not_found", "message": "Unknown API endpoint."}}), 404
        return "Not found", 404

    @app.errorhandler(405)
    def method_not_allowed(_err):
        return jsonify({"error": {"code": "method_not_allowed", "message": "Method not allowed."}}), 405

    @app.errorhandler(413)
    def too_large(_err):
        return jsonify({"error": {"code": "request_too_large", "message": "Request is too large."}}), 413

    @app.errorhandler(500)
    def server_error(_err):
        return jsonify({"error": {"code": "server_error", "message": "Something went wrong in the Gihigugma server."}}), 500

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        # The page may only load and call this server: nothing is fetched from, or sent to, anywhere else.
        resp.headers.setdefault("Content-Security-Policy",
                                "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                                "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        if request.path in ("/", "/index.html", "/script.js", "/modules.js", "/style.css", "/textstats.js"):
            resp.headers["Cache-Control"] = "no-cache"   # always pick up frontend edits
        return resp

    return app


def startup_report(app: Flask) -> None:
    client: OllamaClient = app.extensions["ollama"]
    cfg = app.config
    lan = cfg["HOST"] in ("0.0.0.0", "")
    print(f"\n  Gihigugma  ->  http://{'127.0.0.1' if lan else cfg['HOST']}:{cfg['PORT']}   (open this on this computer)")
    if lan:
        for ip in lan_addresses() or ["<this computer's IP address>"]:
            print(f"  Network    ->  http://{ip}:{cfg['PORT']}   (other devices on the same Wi-Fi or hotspot)")
        print("  ! Network mode is on: any device on this network can reach this address and create an account.")
        print("    Each account sees only its own projects and chats, plus workspaces it was invited to.")
        print("    This is plain http: passwords are not encrypted on the network. Use it on networks you trust.")
    else:
        print("  Network    ->  off (this computer only). Start with  python app.py --lan  to share workspaces.")
    print(f"  Ollama ->  {cfg['OLLAMA_URL']}")
    try:
        version = client.version()
        models = client.list_models()
        print(f"  Ollama {version} is running · {len(models)} model(s) installed")
        if models:
            print("  Models: " + ", ".join(m["name"] for m in models))
        else:
            print("  No models yet. Install one with:  ollama pull llama3.2")
        embed = cfg["EMBED_MODEL"]
        if not client.has_model(embed, models):
            print(f"  ! Embedding model '{embed}' is missing (needed for documents). Run:  ollama pull {embed}")
    except OllamaError as err:
        print(f"  ! {err.message}")
        print("    Gihigugma will keep checking; the page shows the status.")
    print()


if __name__ == "__main__":
    # Localhost only unless network mode is asked for explicitly.
    if "--lan" in sys.argv or os.environ.get("EROS_LAN") == "1":
        Config.HOST = "0.0.0.0"
    application = create_app()
    startup_report(application)
    application.run(host=Config.HOST, port=Config.PORT, debug=Config.DEBUG, threaded=True)

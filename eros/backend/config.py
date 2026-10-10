"""Gihigugma backend configuration.

Every setting can be overridden with an environment variable so nothing
machine-specific is hard-coded. No API keys are needed: all AI runs locally
through Ollama.
"""
import os
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _ollama_url() -> str:
    # Respect Ollama's own OLLAMA_HOST variable if the user already set it.
    url = os.environ.get("EROS_OLLAMA_URL") or os.environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434"
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    if url.startswith("http://0.0.0.0"):
        url = url.replace("0.0.0.0", "127.0.0.1", 1)
    return url.rstrip("/")


class Config:
    BASE_DIR = Path(__file__).resolve().parent
    FRONTEND_DIR = (BASE_DIR.parent / "frontend").resolve()

    # Server. 127.0.0.1 = this computer only. LAN access is added in Module 7.
    HOST = os.environ.get("EROS_HOST", "127.0.0.1")
    PORT = _env_int("EROS_PORT", 5050)
    DEBUG = os.environ.get("EROS_DEBUG", "0") == "1"

    # Ollama
    OLLAMA_URL = _ollama_url()
    DEFAULT_MODEL = os.environ.get("EROS_DEFAULT_MODEL", "")      # optional preferred model
    CONNECT_TIMEOUT = _env_float("EROS_CONNECT_TIMEOUT", 3.0)     # seconds to reach Ollama
    STATUS_TIMEOUT = _env_float("EROS_STATUS_TIMEOUT", 4.0)       # health / model list calls
    STREAM_READ_TIMEOUT = _env_float("EROS_STREAM_TIMEOUT", 300)  # max gap between streamed chunks
    KEEP_ALIVE = os.environ.get("EROS_KEEP_ALIVE", "10m")         # how long Ollama keeps a model loaded

    # Documents (Module 3): upload -> extract -> chunk -> embed -> retrieve
    EMBED_MODEL = os.environ.get("EROS_EMBED_MODEL", "embeddinggemma")
    DATA_DIR = Path(os.environ.get("EROS_DATA_DIR", str(BASE_DIR / "data")))
    CHUNK_CHARS = _env_int("EROS_CHUNK_CHARS", 1000)
    CHUNK_OVERLAP = _env_int("EROS_CHUNK_OVERLAP", 150)
    RETRIEVAL_TOP_K = _env_int("EROS_TOP_K", 5)
    # Passages scoring below this similarity are treated as "not about the question".
    MIN_SCORE = _env_float("EROS_MIN_SCORE", 0.25)
    GROUNDED_TEMPERATURE = _env_float("EROS_GROUNDED_TEMPERATURE", 0.1)   # low = sticks to the sources
    GROUNDED_NUM_CTX = _env_int("EROS_NUM_CTX", 8192)                    # room for evidence + history
    GROUNDED_MAX_HISTORY = _env_int("EROS_GROUNDED_HISTORY", 6)
    # Sections read for "summarise this" / "what are the contents" requests (whole documents when they fit).
    OVERVIEW_SECTIONS = _env_int("EROS_OVERVIEW_SECTIONS", 16)           # per document
    OVERVIEW_MAX_DOCS = _env_int("EROS_OVERVIEW_MAX_DOCS", 4)            # documents reported in one reply
    OVERVIEW_NUM_CTX = _env_int("EROS_OVERVIEW_NUM_CTX", 12288)
    # Research tools (Module 4): long documents are read a batch at a time.
    TOOL_BATCH_CHARS = _env_int("EROS_TOOL_BATCH_CHARS", 8000)           # text given to the model per request
    TOOL_NUM_CTX = _env_int("EROS_TOOL_NUM_CTX", 8192)
    TOOL_MAX_TOKENS = _env_int("EROS_TOOL_MAX_TOKENS", 1500)             # per request; stops a model that loops
    TOOL_MAX_SECTIONS = _env_int("EROS_TOOL_MAX_SECTIONS", 120)          # larger selections are sampled evenly
    TOOL_TOPIC_SECTIONS = _env_int("EROS_TOOL_TOPIC_SECTIONS", 12)       # per document, when a topic is given
    # How much better an older file must match a question than the files just attached to be used instead.
    FOCUS_MARGIN = _env_float("EROS_FOCUS_MARGIN", 0.1)
    # Inside the files a question is about, weaker matches are still accepted (cutoff times this).
    FOCUS_RELAX = _env_float("EROS_FOCUS_RELAX", 0.6)
    # Paraphrasing (Module 5): long text is rewritten a part at a time. Parts are kept small because
    # a small model summarises instead of rewriting when it is given many paragraphs at once.
    PARAPHRASE_MAX_CHARS = _env_int("EROS_PARAPHRASE_MAX_CHARS", 30000)
    PARAPHRASE_PART_CHARS = _env_int("EROS_PARAPHRASE_PART_CHARS", 1200)
    # Memory (Module 6): how many remembered notes may be given to the model for one question.
    MEMORY_LIMIT = _env_int("EROS_MEMORY_LIMIT", 5)
    MAX_CHUNKS_PER_DOC = _env_int("EROS_MAX_CHUNKS", 2000)
    EMBED_BATCH = _env_int("EROS_EMBED_BATCH", 16)
    MAX_UPLOAD_BYTES = _env_int("EROS_MAX_UPLOAD_MB", 25) * 1024 * 1024

    # Accounts and sessions
    SECRET_KEY = os.environ.get("EROS_SECRET_KEY", "")                   # empty = generated into DATA_DIR/secret.key
    SESSION_LIFETIME = _env_int("EROS_SESSION_DAYS", 7) * 86400           # a login lasts at most this long
    SESSION_IDLE = _env_int("EROS_SESSION_IDLE_HOURS", 48) * 3600         # ... and ends sooner if unused this long
    # Send the session cookie over HTTPS only. Turn on when Gihigugma is served through HTTPS.
    COOKIE_SECURE = os.environ.get("EROS_COOKIE_SECURE", "0") == "1"

    # Request limits
    MAX_HISTORY_MESSAGES = _env_int("EROS_MAX_HISTORY", 40)
    MAX_MESSAGE_CHARS = _env_int("EROS_MAX_MESSAGE_CHARS", 32000)
    MAX_REQUEST_BYTES = MAX_UPLOAD_BYTES + 1024 * 1024   # uploads are the largest request

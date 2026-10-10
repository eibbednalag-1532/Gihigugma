"""Paraphrasing endpoint.

  POST /api/paraphrase
  Body: { text, style, strength (1-3), instructions, model }

Streams newline-delimited JSON:
  {"type": "start", "model": "...", "parts": N}          (long text is rewritten in N parts)
  {"type": "progress", "step": i, "total": N}
  {"type": "token", "content": "..."}
  {"type": "done", "stats": {...}, "checks": {numbers_lost, numbers_new, citations_lost, citations_new}}
  {"type": "error", "code": "...", "message": "..."}

Runs on the local Ollama only.
"""
import json
import time

from flask import Blueprint, Response, current_app, jsonify, request

from routes.chat import _resolve_model
from services import paraphrase as para
from services.ollama_client import OllamaError

bp = Blueprint("paraphrase", __name__, url_prefix="/api")


def _error(code: str, message: str, status: int):
    return jsonify({"error": {"code": code, "message": message}}), status


def _line(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False) + "\n"


@bp.post("/paraphrase")
def run():
    cfg = current_app.config
    client = current_app.extensions["ollama"]
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _error("bad_request", "Request body must be JSON.", 400)

    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        return _error("empty_text", "Enter some text to paraphrase.", 400)
    text = text.strip()
    if len(text) > cfg["PARAPHRASE_MAX_CHARS"]:
        return _error("text_too_long",
                      f"That is {len(text):,} characters; the limit is {cfg['PARAPHRASE_MAX_CHARS']:,}. "
                      "Paraphrase it in sections.", 413)
    style = body.get("style", "natural")
    if style not in para.STYLES:
        return _error("bad_request", "Unknown rewriting style.", 400)
    strength = body.get("strength", 2)
    if isinstance(strength, bool) or strength not in para.STRENGTHS:
        return _error("bad_request", "Strength must be 1, 2 or 3.", 400)
    instructions = body.get("instructions")
    instructions = " ".join(instructions.split())[:400] if isinstance(instructions, str) else ""
    requested = body.get("model") if isinstance(body.get("model"), str) else ""

    try:
        model = _resolve_model(requested.strip(), client.list_models())
    except OllamaError as err:
        return _error(err.code, err.message, err.status)

    parts = para.split_text(text, cfg["PARAPHRASE_PART_CHARS"])
    think = False if model["thinks"] else None
    # More variation at higher strength; the output cap stops a model that starts to ramble.
    options = {"temperature": {1: 0.3, 2: 0.6, 3: 0.8}[strength], "num_ctx": 4096,
               "num_predict": cfg["PARAPHRASE_PART_CHARS"] if style != "expanded" else cfg["PARAPHRASE_PART_CHARS"] * 2}

    def generate():
        started = time.monotonic()
        written = []
        tokens = eval_ns = 0
        done_reason = None
        yield _line({"type": "start", "model": model["name"], "parts": len(parts)})
        try:
            for step, part in enumerate(parts, start=1):
                yield _line({"type": "progress", "step": step, "total": len(parts)})
                if step > 1:
                    written.append("\n\n")
                    yield _line({"type": "token", "content": "\n\n"})
                stream = client.chat_stream(model["name"], para.build_messages(part, style, strength, instructions),
                                            options, think=think)
                try:
                    for chunk in stream:
                        piece = (chunk.get("message") or {}).get("content") or ""
                        if piece:
                            written.append(piece)
                            yield _line({"type": "token", "content": piece})
                        if chunk.get("done"):
                            tokens += chunk.get("eval_count") or 0
                            eval_ns += chunk.get("eval_duration") or 0
                            if chunk.get("done_reason") == "length" or done_reason is None:
                                done_reason = chunk.get("done_reason")
                finally:
                    stream.close()
            yield _line({
                "type": "done", "model": model["name"],
                "checks": para.check_preserved(text, "".join(written)),
                "stats": {"tokens": tokens,
                          "tokens_per_sec": round(tokens / (eval_ns / 1e9), 1) if eval_ns else None,
                          "total_ms": round((time.monotonic() - started) * 1000),
                          "done_reason": done_reason},
            })
        except OllamaError as err:
            yield _line({"type": "error", **err.to_dict()})

    return Response(generate(), mimetype="application/x-ndjson",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

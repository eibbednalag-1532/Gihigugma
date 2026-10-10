"""Research tools endpoint: Extract, Summarize, Analyze, Suggest.

  POST /api/tools/run
  Body: { tool, project_id, document_ids[], model, options }

Streams newline-delimited JSON:
  {"type": "start", "tool": "...", "model": "...", "steps": N, "remarks": [...]}
  {"type": "evidence", "evidence": [{n, doc_id, name, page, index, text}]}    (the sections being read)
  {"type": "progress", "step": i, "total": N, "label": "Reading x.pdf, pages 3-6"}
  {"type": "working"}                                (heartbeat while notes are taken; safe to ignore)
  {"type": "token", "content": "..."}                (the result, as it is written)
  {"type": "done", "stats": {...}, "grounding": "documents" | "none",
   "citations": {"used": [...], "invalid": [...]}, "coverage": {"read": r, "total": t}}
  {"type": "error", "code": "...", "message": "..."}

Problems found before any work starts (bad input, Ollama offline) are plain JSON errors.
All inference runs on the local Ollama; nothing is sent anywhere else.
"""
import json
import time

from flask import Blueprint, Response, current_app, jsonify, request

from routes.chat import CitationShifter, _resolve_model, check_citations
from services import research_tools as tools
from services.ollama_client import OllamaError

bp = Blueprint("tools", __name__, url_prefix="/api")

NOTHING_FOUND = "Nothing of that kind was found in this document."


def _error(code: str, message: str, status: int):
    return jsonify({"error": {"code": code, "message": message}}), status


def _line(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False) + "\n"


@bp.post("/tools/run")
def run_tool():
    cfg = current_app.config
    client = current_app.extensions["ollama"]
    store = current_app.extensions["store"]

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _error("bad_request", "Request body must be JSON.", 400)
    tool = body.get("tool")
    if tool not in tools.TOOLS:
        return _error("bad_request", "Unknown research tool.", 400)
    project = store.get_project(body.get("project_id")) if isinstance(body.get("project_id"), str) else None
    if not project:
        return _error("project_not_found", "Pick a research project first.", 404)

    wanted = body.get("document_ids")
    wanted = [d for d in wanted if isinstance(d, str)] if isinstance(wanted, list) else []
    by_id = {d["id"]: d for d in store.list_documents(project["id"])}
    documents = [by_id[d] for d in dict.fromkeys(wanted) if d in by_id]
    options = tools.clean_options(tool, body.get("options"))

    if tool != "suggest" and not documents:
        return _error("no_documents", "Select at least one document.", 400)
    if tool == "analyze" and options["compare"] and len(documents) < 2:
        return _error("no_documents", "Select at least two documents to compare.", 400)

    requested = body.get("model") if isinstance(body.get("model"), str) else ""
    try:
        model = _resolve_model(requested.strip(), client.list_models())
        plan = tools.build_plan(store, client, cfg, project, documents, tool, options)
    except tools.ToolError as err:
        return _error(err.code, err.message, err.status)
    except OllamaError as err:
        return _error(err.code, err.message, err.status)

    think = False if model["thinks"] else None
    model_options = {"temperature": cfg["GROUNDED_TEMPERATURE"], "num_ctx": cfg["TOOL_NUM_CTX"],
                     "num_predict": cfg["TOOL_MAX_TOKENS"]}
    file_names = [d["name"] for d in documents]

    def collect(messages, offset=0):
        """Run one request to the end, yielding heartbeats so Cancel is noticed; returns the text.

        `offset` turns the request's own source numbers into the run-wide section numbers.
        """
        parts = []
        stream = client.chat_stream(model["name"], messages, model_options, think=think)
        try:
            for count, chunk in enumerate(stream):
                parts.append((chunk.get("message") or {}).get("content") or "")
                if count % 25 == 24:
                    yield _line({"type": "working"})
        finally:
            stream.close()
        tidy = CitationShifter(offset)
        return (tidy.feed("".join(parts)) + tidy.flush()).strip()

    def generate():
        started = time.monotonic()
        answer = []
        tokens = eval_ns = step = 0
        done_reason = None

        def say(text):
            answer.append(text)
            return _line({"type": "token", "content": text})

        yield _line({"type": "start", "tool": tool, "model": model["name"], "steps": plan.steps,
                     "remarks": plan.remarks})
        if plan.sections:
            yield _line({"type": "evidence", "evidence": plan.sections})
        try:
            for position, unit in enumerate(plan.units):
                lead = ("\n\n" if position else "") + unit.heading
                if lead:
                    yield say(lead)

                if unit.direct:
                    material = tools.sources_text(unit.batches[0]) if unit.batches else ""
                    from_notes, offset = False, tools.batch_offset(unit.batches[0] if unit.batches else [])
                else:
                    notes = []
                    for batch in unit.batches:
                        step += 1
                        yield _line({"type": "progress", "step": step, "total": plan.steps,
                                     "label": tools.batch_label(batch)})
                        text = yield from collect(tools.map_messages(tool, options, batch),
                                                  tools.batch_offset(batch))
                        if text:
                            notes.append((batch[0]["name"], text))
                    # Notes that still don't fit one request are shortened, a group at a time.
                    for _ in range(3):
                        if sum(len(t) for _, t in notes) <= cfg["TOOL_BATCH_CHARS"]:
                            break
                        yield _line({"type": "progress", "step": step, "total": plan.steps,
                                     "label": f"Condensing notes on {unit.label}"})
                        shorter, group, size = [], [], 0
                        for note in notes + [None]:
                            if group and (note is None or size + len(note[1]) > cfg["TOOL_BATCH_CHARS"]):
                                text = yield from collect(tools.condense_messages(group))
                                shorter.append((group[0][0], text))
                                group, size = [], 0
                            if note is not None:
                                group.append(note)
                                size += len(note[1])
                        notes = shorter
                    if not notes:
                        step += 1
                        yield say(NOTHING_FOUND)
                        continue
                    material, from_notes, offset = tools.notes_text(notes), True, 0   # notes carry run-wide numbers

                step += 1
                yield _line({"type": "progress", "step": step, "total": plan.steps,
                             "label": f"Writing the result for {unit.label}"})
                stream = client.chat_stream(
                    model["name"], tools.final_messages(tool, options, project, material, from_notes, file_names),
                    model_options, think=think)
                tidy = CitationShifter(offset)
                try:
                    for chunk in stream:
                        text = tidy.feed((chunk.get("message") or {}).get("content") or "")
                        if chunk.get("done"):
                            text += tidy.flush()
                            tokens += chunk.get("eval_count") or 0
                            eval_ns += chunk.get("eval_duration") or 0
                            if chunk.get("done_reason") == "length" or done_reason is None:
                                done_reason = chunk.get("done_reason")
                        if text:
                            yield say(text)
                finally:
                    stream.close()

            done = {
                "type": "done", "tool": tool, "model": model["name"],
                "grounding": "documents" if plan.sections else "none",
                "stats": {"tokens": tokens,
                          "tokens_per_sec": round(tokens / (eval_ns / 1e9), 1) if eval_ns else None,
                          "total_ms": round((time.monotonic() - started) * 1000),
                          "done_reason": done_reason},
            }
            if plan.sections:
                done["citations"] = check_citations("".join(answer), len(plan.sections))
                done["coverage"] = {"read": len(plan.sections), "total": plan.total}
            yield _line(done)
        except OllamaError as err:
            yield _line({"type": "error", **err.to_dict()})

    return Response(generate(), mimetype="application/x-ndjson",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

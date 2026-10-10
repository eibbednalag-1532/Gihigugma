"""Chat endpoint: saves the question, builds the history from the database, streams the reply, saves it.

Body: { message, conversation_id, project_id, model, context, focus_document_ids, meta }
  message          the new question. Left out, the last stored question is answered again (Retry).
  conversation_id  an existing conversation of the signed-in user. A new id, or none, starts a
                   conversation in project_id.
  context "project" -> answer ONLY from the project's documents (the default)
  context "none"    -> general-knowledge chat, no documents involved

The signed-in user comes from the session cookie. The conversation history sent
to the model is read from the database for that one conversation; the browser
does not supply it.

Response format: newline-delimited JSON (one object per line):
  {"type": "start", "model": "...", "conversation": {id, title}, "user_message_id": n}
  {"type": "evidence", "evidence": [{n, doc_id, name, page, index, text, score}]}   (document answers)
  {"type": "token", "content": "..."}          (many)
  {"type": "done",  "model": "...", "stats": {...}, "grounding": "documents" | "no_evidence" | "none",
   "citations": {"used": [1, 2], "invalid": [7]},
   "coverage": {"read": 16, "total": 40},      (whole-document requests: sections read of sections available)
   "content": "the reply as saved", "message_id": n}

The reply is saved only when the model finishes it. If it is stopped, times out
or fails, nothing is saved as the assistant's answer; the question stays stored.

A request about a document as a whole ("what are the contents?", "summarise it")
has no subject to search for, so the document is read through in order instead.
  {"type": "error", "code": "...", "message": "..."}   (only if it fails mid-stream)

When no passage in the project matches the question, the model is not asked at
all: a fixed "not in your documents" reply is returned, so nothing from outside
the documents can leak into the answer.

Failures before streaming starts (bad input, Ollama offline, unknown model)
return a normal JSON error with an HTTP status instead.
"""
import json
import re
import sqlite3
import time
import uuid

from flask import Blueprint, Response, current_app, jsonify, request, stream_with_context

from routes.conversations import owned_or_new
from services.access import can, current_user
from services.ollama_client import OllamaError
from services.prompt_builder import (PromptError, build_grounded_messages, build_messages, clean_history)
from services.retrieval import (is_overview_request, named_documents, refers_to_the_file, retrieve_for_question,
                                to_evidence, wants_other_documents)

bp = Blueprint("chat", __name__, url_prefix="/api")

NO_DOCUMENTS_REPLY = ("This project has no documents yet. Add a PDF, DOCX, or TXT file and "
                      "I'll answer from it.")
NO_EVIDENCE_REPLY = ("I couldn't find anything in this project's documents that answers that. "
                     "Try rephrasing the question, or add a document that covers it.")

_CITATION = re.compile(r"\[(\d+(?:\s*[,\-–]\s*\d+)*)\]")      # [3], [1, 4] or a range like [2-5]


def _cited_numbers(group: str) -> list[int]:
    """The source numbers inside one marker; a range such as 2-5 counts every number in it."""
    numbers: list[int] = []
    for part in group.split(","):
        ends = [int(n) for n in re.split(r"[\-–]", part) if n.strip()]
        if len(ends) == 2 and 0 < ends[1] - ends[0] <= 30:
            numbers += range(ends[0], ends[1] + 1)
        else:
            numbers += ends
    return numbers


def _error(code: str, message: str, status: int):
    return jsonify({"error": {"code": code, "message": message}}), status


def history_for(messages: list[dict], context: str) -> list[dict]:
    """The stored conversation as the model should see it: real user and assistant turns only.

    Exchanges that didn't come from the documents are left out: "nothing found"
    replies always, and general-knowledge replies whenever a document-only answer
    is requested, so nothing from outside the documents is carried into it. A
    question that never got a reply (stopped, failed) is replaced by the next one.
    """
    turns: list[dict] = []
    for m in messages:
        if m["role"] not in ("user", "assistant") or m.get("placeholder"):
            continue
        outside = m["role"] == "assistant" and (
            m.get("grounding") == "no_evidence" or (context == "project" and m.get("grounding") == "none"))
        if outside:
            if turns and turns[-1]["role"] == "user":
                turns.pop()                    # and the question that led to it
            continue
        if m["role"] == "user" and turns and turns[-1]["role"] == "user":
            turns.pop()
        turns.append({"role": m["role"], "content": m["content"]})
    return turns


_STUTTER = re.compile(r"(\[\d+\])(?:\s*\1)+")
_CLOSING_OFFER = re.compile(
    r"\s*(?:\n|(?<=[.!?])\s)[^\n.!?]*\b(?:feel free to ask|let me know if|if you (?:have|need) any (?:other|more|further)"
    r"|hope this helps|is there anything else|don't hesitate to)\b[^\n]*$", re.I)


def tidy_reply(text: str) -> str:
    """The reply as it is saved and shown: a stuttered citation ("[1] [1] [1]") once, and no
    closing offer of more help, which the assistant is told not to add."""
    return _CLOSING_OFFER.sub("", _STUTTER.sub(r"\1", text)).strip()


def _line(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False) + "\n"


def _resolve_model(requested: str, installed: list[dict]) -> dict:
    client = current_app.extensions["ollama"]
    installed = [m for m in installed if m["can_chat"]]   # embedding-only models can't answer questions
    if not installed:
        raise OllamaError("no_models",
                          "No models are installed in Ollama yet. Run `ollama pull qwen2.5:3b` "
                          "(or another model), then refresh.", 409)
    by_name = {m["name"]: m for m in installed}
    if requested:
        name = client.resolve_name(requested, installed)
        if not name:
            raise OllamaError("model_not_found",
                              f"The model '{requested}' isn't installed. Run `ollama pull {requested}` "
                              "or pick another model.", 404)
        return by_name[name]
    default = current_app.config["DEFAULT_MODEL"]
    name = client.resolve_name(default, installed) if default else None
    return by_name[name] if name else installed[0]


# A source block's opening and closing tags, as a model may echo them: <source id="2" ...> or [source id="2" ...]
_SOURCE_OPEN = re.compile(r"[\[<]source\s+id=\W?(\d+)[^\]>\n]*[\]>]\s*", re.I)
_SOURCE_CLOSE = re.compile(r"\s*[\[<]/source[\]>]", re.I)


class CitationShifter:
    """Tidies citation markers in streamed text.

    - A small model sometimes echoes a source block instead of citing it. The
      echoed tag becomes a plain [n] marker, so the quote is still attributed.
    - Each file in a multi-file reply is shown to the model with sources numbered
      from 1 (small models cite those most reliably); the reader sees one list, so
      markers are shifted by `offset` to match it.

    Text is held back only while a "[" or "<" is still open, because a marker
    or tag can arrive split across chunks.
    """

    def __init__(self, offset: int):
        self.offset = offset
        self.held = ""

    def _shift(self, text: str) -> str:
        text = _SOURCE_CLOSE.sub("", _SOURCE_OPEN.sub(lambda m: f"[{m.group(1)}] ", text))
        if not self.offset:
            return text
        return _CITATION.sub(
            lambda m: "[" + ", ".join(str(n + self.offset) for n in _cited_numbers(m.group(1))) + "]", text)

    def feed(self, text: str) -> str:
        self.held += text
        cut = max(self.held.rfind("["), self.held.rfind("<"))
        if cut != -1:
            tail = self.held[cut:]
            name = tail[1:].lstrip("/")[:6].lower()
            is_tag = "source".startswith(name)       # an echoed tag is longer than a citation marker
            closed = "]" in tail or ">" in tail
            if closed or len(tail) > (160 if is_tag else 24):
                cut = -1
        if cut == -1:
            cut = len(self.held)
        ready, self.held = self.held[:cut], self.held[cut:]
        return self._shift(ready)

    def flush(self) -> str:
        ready, self.held = self.held, ""
        return self._shift(ready)


def check_citations(text: str, available: int) -> dict:
    """Which [n] markers the answer used, and which point at sources that were never provided."""
    cited = {n for group in _CITATION.findall(text) for n in _cited_numbers(group)}
    return {"used": sorted(n for n in cited if 1 <= n <= available),
            "invalid": sorted(n for n in cited if not 1 <= n <= available)}


@bp.post("/chat")
def chat():
    cfg = current_app.config
    client = current_app.extensions["ollama"]
    store = current_app.extensions["store"]

    user = current_user()             # from the server-side session, never from the request body

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _error("bad_request", "Request body must be JSON.", 400)
    requested = body.get("model") or ""
    if not isinstance(requested, str):
        return _error("bad_request", "Model must be a string.", 400)
    context = "project" if body.get("context", "project") == "project" else "none"

    # The question. Without one, the last stored question is answered again (Retry, or a question
    # that was stored together with notes about attached files).
    message = body.get("message")
    if message is not None:
        if not isinstance(message, str) or not message.strip():
            return _error("empty_message", "Type a message before sending.", 400)
        message = message.strip()
        if len(message) > cfg["MAX_MESSAGE_CHARS"]:
            return _error("message_too_long",
                          f"A message is longer than {cfg['MAX_MESSAGE_CHARS']:,} characters. Please shorten it.", 400)

    # The conversation must be this user's own; a new one is created in a project they can open.
    conversation, problem = owned_or_new(body.get("conversation_id"), body.get("project_id"))
    if problem:
        return _error(*problem)
    project = None
    if context == "project":
        # Grounding always uses the conversation's own project, and only while the user still has access.
        if not can("view", conversation["project_id"]):
            return _error("project_not_found", "You no longer have access to this conversation's project.", 404)
        project = store.get_project(conversation["project_id"])

    if message is not None:
        meta = body.get("meta") if isinstance(body.get("meta"), dict) and len(json.dumps(body["meta"])) < 20000 else {}
        saved_question = store.add_messages(conversation["id"], [{"role": "user", "content": message, "meta": meta}])[0]
    stored = store.get_messages(conversation["id"])

    # The history comes from the database, so one conversation can never see another's messages.
    try:
        history = clean_history(history_for(stored, context), cfg["MAX_MESSAGE_CHARS"])
    except PromptError as err:
        return _error(err.code, err.message, 400)
    if message is None:
        saved_question = next(m for m in reversed(stored) if m["role"] == "user" and not m.get("placeholder"))

    # A research-tool result this question follows up on: its text helps find the passages it came from.
    anchor = next((m["content"][:800] for m in reversed(stored[-8:]) if m.get("fromTool")), None)

    # The files most recently attached in this chat (sent by the page). A question in that chat is
    # about those files, not about older ones in the project, unless it names or asks for the others.
    focus = body.get("focus_document_ids")
    focus = {d for d in focus if isinstance(d, str)} if isinstance(focus, list) else set()

    # The user's own approved memories that fit this question, unless they switched memory off for this chat.
    memories: list[dict] = []
    if not conversation["memoryExcluded"] and store.get_settings(user["id"])["memory_enabled"]:
        memories = store.relevant_memories(user["id"], history[-1]["content"], conversation["project_id"],
                                           cfg["MEMORY_LIMIT"])
    facts = [m["content"] for m in memories]

    evidence: list[dict] = []
    coverage = None        # set for whole-document requests
    canned = None          # a fixed reply sent without asking the model
    jobs: list[dict] = []  # model requests that make up the reply: {heading, messages, options}
    stream = first = None
    try:
        model = _resolve_model(requested.strip(), client.list_models())
        think = False if model["thinks"] else None
        if project:
            if not project["stats"]["documents"]:
                canned = NO_DOCUMENTS_REPLY
            else:
                asked = [m["content"] for m in history if m["role"] == "user"]
                documents = store.list_documents(project["id"])
                named = named_documents(asked[-1], documents)
                focused = [d for d in documents if d["id"] in focus]
                if named or wants_other_documents(asked[-1]):
                    focused = []                       # the user pointed at specific or additional files
                if is_overview_request(asked[-1], documents):
                    # Each file is read and reported in its own request, so the model can never
                    # mix one document's content into another's. Newest files first.
                    wanted = named or focused or documents
                    options = {"temperature": cfg["GROUNDED_TEMPERATURE"], "num_ctx": cfg["OVERVIEW_NUM_CTX"]}
                    hits = []
                    comparing = len(wanted) > 1 and wants_other_documents(asked[-1])
                    if comparing:
                        # Only an explicit request to compare puts several files in front of the model at once.
                        hits, _ = store.read_through(project["id"], [d["id"] for d in wanted[:cfg["OVERVIEW_MAX_DOCS"]]],
                                                     cfg["OVERVIEW_SECTIONS"])
                        if hits:
                            jobs.append({"heading": "", "options": options,
                                         "messages": build_grounded_messages(history[-1:], hits, project, 1,
                                                                             memory_facts=facts, compare=True)})
                    for doc in [] if comparing else wanted[:cfg["OVERVIEW_MAX_DOCS"]]:
                        sections, _ = store.read_through(project["id"], [doc["id"]], cfg["OVERVIEW_SECTIONS"])
                        if not sections:
                            continue
                        jobs.append({
                            "heading": f"**{doc['name']}**\n\n" if len(wanted) > 1 else "",
                            "messages": build_grounded_messages(history[-1:], sections, project, 1, overview=True,
                                                                memory_facts=facts),
                            "options": options,
                            "offset": len(hits),      # this file's sources follow the earlier files' in the list
                        })
                        hits += sections
                    coverage = {"read": len(hits), "total": sum(d["chunks"] for d in wanted)}
                else:
                    previous = asked[-2] if len(asked) > 1 else None
                    # The question is about particular files when it names one, or says "this file"
                    # in a chat where files were just attached.
                    about = named if len(named) == 1 else (focused if refers_to_the_file(asked[-1]) else [])
                    num_ctx = cfg["GROUNDED_NUM_CTX"]
                    if about and sum(d["chunks"] for d in about) <= cfg["OVERVIEW_SECTIONS"]:
                        # A short file is given to the model whole, so nothing in it can be missed by the search.
                        hits, _ = store.read_through(project["id"], [d["id"] for d in about], cfg["OVERVIEW_SECTIONS"])
                        num_ctx = cfg["OVERVIEW_NUM_CTX"]
                    elif about:
                        hits = retrieve_for_question(client, store, cfg, project["id"], asked[-1], previous=previous,
                                                     doc_ids={d["id"] for d in about}, anchor=anchor,
                                                     min_score=cfg["MIN_SCORE"] * cfg["FOCUS_RELAX"])
                    else:
                        hits = retrieve_for_question(client, store, cfg, project["id"], asked[-1], previous=previous,
                                                     doc_ids={d["id"] for d in focused} or None, anchor=anchor,
                                                     min_score=cfg["MIN_SCORE"] * cfg["FOCUS_RELAX"] if focused else None)
                        if focused:
                            # The newly attached files come first, but when an older file clearly matches the
                            # question better (or the new ones have nothing), the answer is there.
                            anywhere = retrieve_for_question(client, store, cfg, project["id"], asked[-1],
                                                             previous=previous, anchor=anchor)
                            if anywhere and (not hits or anywhere[0]["score"] - hits[0]["score"] > cfg["FOCUS_MARGIN"]):
                                hits = anywhere
                    if hits:
                        jobs.append({
                            "heading": "",
                            "messages": build_grounded_messages(history, hits, project, cfg["GROUNDED_MAX_HISTORY"],
                                                                memory_facts=facts),
                            "options": {"temperature": cfg["GROUNDED_TEMPERATURE"], "num_ctx": num_ctx},
                        })
                evidence = to_evidence(hits)
                if not hits:
                    canned = NO_EVIDENCE_REPLY
        else:
            jobs.append({"heading": "", "options": None,
                         "messages": build_messages(history, cfg["MAX_HISTORY_MESSAGES"], memory_facts=facts)})
        if canned is None:
            stream = client.chat_stream(model["name"], jobs[0]["messages"], jobs[0]["options"], think=think)
            first = next(stream, None)   # open the connection now so early errors get a proper status
    except OllamaError as err:
        # The question is already stored. Saying so lets the page retry without storing it twice.
        return jsonify({"error": {"code": err.code, "message": err.message},
                        "user_message_id": saved_question["id"], "conversation_id": conversation["id"]}), err.status

    grounding = "none" if not project else "no_evidence" if canned else "documents"

    used_memories = [{"id": m["id"], "content": m["content"], "kind": m["kind"]} for m in memories]

    def save_reply(text: str, done: dict) -> dict:
        """Store a finished reply with what is needed to show it again later. Called only on success,
        so a reply that was stopped, timed out or failed is never saved as if it were complete."""
        details = {"model": model["name"], "question": saved_question["content"], "grounding": grounding,
                   "stats": done["stats"], "uid": uuid.uuid4().hex}
        if evidence:
            details["evidence"] = evidence
        if "citations" in done:
            details["cited"], details["invalidCitations"] = done["citations"]["used"], done["citations"]["invalid"]
        if "coverage" in done:
            details["coverage"] = done["coverage"]
        if used_memories and canned is None:
            details["memories"] = used_memories
        try:
            saved = store.add_messages(conversation["id"], [{"role": "assistant", "content": text, "meta": details}])[0]
        except sqlite3.Error:
            return done            # the conversation was deleted while the model was writing
        return {**done, "content": text, "message_id": saved["id"]}

    def generate():
        nonlocal stream
        started = time.monotonic()
        yield _line({"type": "start", "model": model["name"], "user_message_id": saved_question["id"],
                     "conversation": {"id": conversation["id"],
                                      "title": store.get_conversation(conversation["id"])["title"]}})
        if evidence:
            yield _line({"type": "evidence", "evidence": evidence})
        if used_memories and canned is None:
            # Shown under the reply, so remembered notes are never mistaken for the conversation or the documents.
            yield _line({"type": "memories", "memories": used_memories})
        if canned is not None:
            yield _line({"type": "token", "content": canned})
            yield _line(save_reply(canned, {"type": "done", "model": model["name"], "grounding": grounding,
                                            "stats": {"total_ms": round((time.monotonic() - started) * 1000)}}))
            return
        answer = []
        tokens = eval_ns = 0
        done_reason = None
        try:
            for position, job in enumerate(jobs):
                lead = ("\n\n" if position else "") + job["heading"]
                if lead:
                    answer.append(lead)
                    yield _line({"type": "token", "content": lead})
                if position:
                    stream = client.chat_stream(model["name"], job["messages"], job["options"], think=think)
                    chunk = next(stream, None)
                else:
                    chunk = first
                finished = False
                shifter = CitationShifter(job.get("offset", 0))
                while chunk is not None:
                    text = shifter.feed((chunk.get("message") or {}).get("content") or "")
                    if chunk.get("done"):
                        text += shifter.flush()
                    if text:
                        answer.append(text)
                        yield _line({"type": "token", "content": text})
                    if chunk.get("done"):
                        tokens += chunk.get("eval_count") or 0
                        eval_ns += chunk.get("eval_duration") or 0
                        if chunk.get("done_reason") == "length" or done_reason is None:
                            done_reason = chunk.get("done_reason")
                        finished = True
                        break
                    chunk = next(stream, None)
                if not finished:
                    yield _line({"type": "error", "code": "stream_interrupted",
                                 "message": "Ollama ended the reply unexpectedly."})
                    return
            done = {
                "type": "done",
                "model": model["name"],
                "grounding": grounding,
                "stats": {
                    "tokens": tokens,
                    "tokens_per_sec": round(tokens / (eval_ns / 1e9), 1) if eval_ns else None,
                    "total_ms": round((time.monotonic() - started) * 1000),
                    "done_reason": done_reason,
                },
            }
            text = tidy_reply("".join(answer))
            if not text:
                yield _line({"type": "error", "code": "empty_reply",
                             "message": "The model returned an empty reply. Try rephrasing, or pick another model."})
                return
            if project:
                done["citations"] = check_citations(text, len(evidence))
            if coverage:
                done["coverage"] = coverage
            yield _line(save_reply(text, done))
        except OllamaError as err:
            yield _line({"type": "error", **err.to_dict()})
        except GeneratorExit:
            # Client pressed Stop or closed the tab: stop pulling from Ollama.
            stream.close()
            raise

    return Response(
        stream_with_context(generate()),
        mimetype="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

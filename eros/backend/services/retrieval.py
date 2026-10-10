"""Semantic retrieval: embeds text with the local embedding model and finds matching passages.

Scores are cosine similarity between a passage and a query. They measure how
related the wording is, not whether the passage is correct.
"""
from __future__ import annotations

import re
from collections import OrderedDict
from pathlib import PurePath
from threading import Lock

# EmbeddingGemma is trained with task prefixes; using them separates relevant
# from irrelevant passages much more cleanly. Other models get the raw text.
_GEMMA_QUERY = "task: search result | query: "
_GEMMA_DOC = "title: none | text: "

_query_cache: OrderedDict[tuple[str, str], list[float]] = OrderedDict()
_cache_lock = Lock()
_CACHE_SIZE = 256


def _prefixes(model: str) -> tuple[str, str]:
    return (_GEMMA_QUERY, _GEMMA_DOC) if "embeddinggemma" in model.lower() else ("", "")


def embed_passages(client, model: str, texts: list[str]) -> list[list[float]]:
    prefix = _prefixes(model)[1]
    return client.embed(model, [prefix + t for t in texts])


def embed_queries(client, model: str, texts: list[str]) -> list[list[float]]:
    """Embed search queries, reusing vectors for text seen before (project questions repeat a lot)."""
    with _cache_lock:
        missing = [t for t in dict.fromkeys(texts) if (model, t) not in _query_cache]
    if missing:
        prefix = _prefixes(model)[0]
        vectors = client.embed(model, [prefix + t for t in missing])
        with _cache_lock:
            for text, vec in zip(missing, vectors):
                _query_cache[(model, text)] = vec
                while len(_query_cache) > _CACHE_SIZE:
                    _query_cache.popitem(last=False)
    with _cache_lock:
        cached = {t: _query_cache.get((model, t)) for t in texts}
    if any(v is None for v in cached.values()):     # evicted between the two locks; embed directly
        prefix = _prefixes(model)[0]
        return client.embed(model, [prefix + t for t in texts])
    return [cached[t] for t in texts]


# Words that ask about a document as a whole rather than about a subject in it.
_OVERVIEW_WORDS = {
    "summary", "summarise", "summarize", "summarised", "summarized", "summaries", "overview", "outline", "gist",
    "tldr", "tl", "dr", "contents", "content", "about", "inside", "contain", "contains", "cover", "covers", "say", "says",
    "describe", "explain", "read", "main", "key", "points", "point", "ideas", "idea", "findings", "topics",
    "topic", "takeaways", "recap", "brief", "briefly",
    # "compare both files" is also about whole documents rather than a subject inside them
    "compare", "compares", "comparison", "contrast", "versus", "both", "difference", "differences",
    "differ", "different", "similar", "similarities", "two", "three",
}
_FILLER_WORDS = {
    "a", "an", "the", "this", "that", "these", "those", "my", "our", "your", "its", "it", "they", "them",
    "is", "are", "was", "were", "be", "do", "does", "did", "can", "could", "would", "will", "please", "pls",
    "me", "us", "i", "you", "we", "what", "whats", "which", "how", "tell", "give", "show", "list", "write",
    "make", "get", "provide", "of", "in", "on", "to", "for", "from", "with", "and", "or", "all", "whole",
    "entire", "full", "everything", "short", "quick", "simple", "detailed", "s", "there", "here", "just",
    "file", "files", "document", "documents", "doc", "docs", "paper", "papers", "pdf", "docx", "txt", "report",
    "reports", "notes", "note", "text", "upload", "uploaded", "attached", "attachment", "reading", "article",
    "project", "source", "sources", "now", "again", "then", "so", "ok", "okay", "hi", "hello",
}


def named_documents(question: str, documents: list[dict]) -> list[dict]:
    """Documents whose file name (with or without extension) appears in the question."""
    text = question.lower()
    found = []
    for doc in documents:
        name = doc["name"].lower()
        stem = PurePath(name).stem
        if name in text or (len(stem) >= 4 and stem in text):
            found.append(doc)
    return found


def is_overview_request(question: str, documents: list[dict]) -> bool:
    """True for requests about a whole document: "tell me the contents", "summarise this file".

    Such a request names no subject, so there is nothing to search for; the
    document itself is what should be read. A question that mentions any real
    subject ("what does it say about drainage?") is not an overview request.
    """
    text = question.lower()
    for doc in named_documents(question, documents):     # the file's own name is not a subject
        text = text.replace(doc["name"].lower(), " ").replace(PurePath(doc["name"].lower()).stem, " ")
    words = re.findall(r"[a-z0-9]+", text.replace("'", ""))
    if not words or any(w not in _OVERVIEW_WORDS and w not in _FILLER_WORDS for w in words):
        return False
    return any(w in _OVERVIEW_WORDS for w in words)


def retrieve_for_question(client, store, cfg, project_id: str, question: str,
                          previous: str | None = None, doc_ids: set[str] | None = None,
                          anchor: str | None = None, min_score: float | None = None) -> list[dict]:
    """Passages from the project's documents that match a chat question. May raise OllamaError.

    `doc_ids` limits the search to those documents (a named file, or the files just attached).
    `min_score` replaces the usual similarity cutoff.

    A follow-up such as "what about the second one?" carries little meaning alone,
    so the previous question is also searched (at a small discount).
    """
    queries, weights = [question], [1.0]
    if previous:
        queries.append(f"{previous}\n{question}")
        weights.append(0.9)
    if anchor:
        # The question follows a research-tool result ("explain the second point"): search with that result too.
        queries.append(f"{anchor}\n{question}")
        weights.append(0.85)
    vectors = embed_queries(client, cfg["EMBED_MODEL"], queries)
    return store.search(project_id, vectors, cfg["RETRIEVAL_TOP_K"],
                        cfg["MIN_SCORE"] if min_score is None else min_score, doc_ids=doc_ids, weights=weights)


_OTHER_DOCUMENTS = re.compile(
    r"\b(compare[ds]?|comparing|comparison|contrast|versus|vs|both)\b|"
    r"\b(all|every|each|other|another|previous|earlier|older|old|first|existing)\s+(?:\w+\s+)?"
    r"(files?|documents?|docs?|pdfs?|reports?|papers?|sources?|uploads?|ones?)\b", re.I)


_THE_FILE = re.compile(
    r"\b(this|the|that|my|attached|uploaded|new|latest)\s+(?:\w+\s+)?"
    r"(file|document|doc|pdf|report|paper|attachment|upload|notes?|article|reading)\b", re.I)


def refers_to_the_file(question: str) -> bool:
    """True when the question points at a file itself ("what does this file say about costs?")."""
    return bool(_THE_FILE.search(question))


def wants_other_documents(question: str) -> bool:
    """True when the question reaches beyond the files just attached ("compare with the old one", "all files")."""
    return bool(_OTHER_DOCUMENTS.search(question))


def project_queries(project: dict) -> list[dict]:
    """What a project is looking for: its topic, then each research question."""
    topic = project["title"]
    if project["objectives"]:
        topic += ". " + project["objectives"]
    if project["keywords"]:
        topic += " Keywords: " + ", ".join(project["keywords"])
    queries = [{"kind": "topic", "label": project["title"], "text": topic}]
    # A question like "What are the primary causes?" only means something next to its topic.
    queries += [{"kind": "question", "label": q, "text": f"{project['title']}: {q}"} for q in project["questions"]]
    return queries


def _matches(queries: list[dict], scores: list[float], min_score: float) -> list[dict]:
    found = [{"kind": q["kind"], "label": q["label"], "score": s}
             for q, s in zip(queries, scores) if s >= min_score]
    return sorted(found, key=lambda m: -m["score"])


def _best_match(queries: list[dict], scores: list[float], min_score: float) -> dict:
    """The research question a passage matches best; the broad topic is only the fallback."""
    best = max(range(1, len(queries)), key=lambda i: scores[i], default=0)
    if best and scores[best] < min_score:
        best = 0
    return {"kind": queries[best]["kind"], "label": queries[best]["label"], "score": scores[best]}


def classify_passage(client, store, cfg, project: dict, doc_id: str, chunk_idx: int) -> dict | None:
    """Which of the project's questions (or its topic) a stored passage belongs under, if any."""
    queries = project_queries(project)
    vectors = embed_queries(client, cfg["EMBED_MODEL"], [q["text"] for q in queries])
    scores = store.passage_scores(doc_id, chunk_idx, vectors)
    if not scores:
        return None
    match = _best_match(queries, scores, cfg["MIN_SCORE"])
    return match if match["score"] >= cfg["MIN_SCORE"] else None


def relevant_passages(client, store, cfg, project: dict, doc_id: str | None,
                      min_score: float, limit: int) -> dict:
    """Passages that match the project's topic or research questions, best first."""
    queries = project_queries(project)
    vectors = embed_queries(client, cfg["EMBED_MODEL"], [q["text"] for q in queries])
    hits = store.search(project["id"], vectors, limit, min_score, doc_id=doc_id)
    for hit in hits:
        scores = hit.pop("scores")
        hit.pop("query")
        # A passage can support several questions, so every match is reported
        # (`matches`) along with the single best one (`match`).
        hit["matches"] = _matches(queries, scores, min_score)
        hit["match"] = _best_match(queries, scores, min_score)
        hit["score"] = hit["match"]["score"]
    hits.sort(key=lambda h: -h["score"])
    return {"queries": [{"kind": q["kind"], "label": q["label"]} for q in queries], "passages": hits}


def to_evidence(hits: list[dict]) -> list[dict]:
    """Numbered evidence sent to the browser: exactly what the model was shown."""
    return [{"n": n, "doc_id": h["doc_id"], "name": h["name"], "page": h["page"], "index": h["index"],
             "text": h["text"], "score": None if h["score"] is None else round(h["score"], 3)}
            for n, h in enumerate(hits, start=1)]

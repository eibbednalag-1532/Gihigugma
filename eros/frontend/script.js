/* =========================================================
   Gihigugma — frontend logic (offline document research)

   Sections:
     1. Config
     2. API layer  (the only place that talks to the backend)
     3. State
     4. Utilities (escaping, markdown-lite, word count, time, dialogs)
     5. Evidence, passages and findings (shared by chat and the research views)
     6. Chat rendering
     7. Connection & models
     8. Projects
     9. Documents & Sources view
    10. Combined research view
   10b. Research tools (extract, summarize, analyze, suggest)
    11. Composer & attachments
    12. Sending & streaming
    13. Navigation, sidebar, events
   13b. Account: sign in, create account, sign out
    14. Init
   ========================================================= */
(() => {
  "use strict";

  /* ---------- 1. CONFIG ---------- */
  const CONFIG = {
    // Empty string = same origin: the Python backend serves this page and /api/*.
    API_BASE: "",
    HEALTH_TIMEOUT_MS: 6000,
    HEALTH_POLL_MS: 15000,
    SLOW_START_MS: 5000,          // after this with no tokens, explain model loading
    MAX_FILE_MB: 25,
    ACCEPTED_EXT: [".pdf", ".txt", ".md", ".docx", ".csv", ".json", ".html", ".htm"],   // keep in sync with backend SUPPORTED_EXT
    MODEL_PREF_KEY: "eros.selectedModel",
    PROJECT_PREF_KEY: "eros.activeProject",
    CHANGES_POLL_MS: 5000,              // how often a shared workspace is checked for updates
    SUGGESTIONS_PER_QUESTION: 4,  // unsaved passages shown under each research question
  };

  // Must match NOT_IN_DOCUMENTS in backend/services/prompt_builder.py.
  const NOT_IN_DOCUMENTS = "The uploaded documents don't contain enough information to answer this.";
  const SCORE_HINT = "How closely this passage matches the question or topic, from 0 to 1. It is not a measure of accuracy.";

  /* ---------- 2. API LAYER ----------
     Every backend call goes through here. No keys or secrets live in the
     frontend; the backend talks to Ollama on the user's machine. */
  class ApiError extends Error {
    constructor(code, message, status = 0, data = null) {
      super(message);
      this.name = "ApiError";
      this.code = code;
      this.status = status;
      this.data = data;       // full error body, e.g. the existing document for a duplicate upload
    }
  }

  const BACKEND_OFFLINE_MSG =
    "Can't reach the Gihigugma server. Start it from the `backend` folder with `python app.py`, " +
    "then open the address it prints (http://127.0.0.1:5050).";

  async function readError(res) {
    try {
      const body = await res.json();
      if (body && body.error) return new ApiError(body.error.code, body.error.message, res.status, body);
    } catch { /* not JSON */ }
    if (res.status === 404 || res.status === 405 || res.status === 501) {
      // A plain static server (no /api) — e.g. the page was opened another way.
      return new ApiError("backend_offline", BACKEND_OFFLINE_MSG, res.status);
    }
    return new ApiError("http_error", `The server returned an error (HTTP ${res.status}).`, res.status);
  }

  const api = {
    async request(path, { timeoutMs = 0, signal, ...options } = {}) {
      const ctrl = new AbortController();
      const timer = timeoutMs ? setTimeout(() => ctrl.abort(), timeoutMs) : null;
      if (signal) {
        if (signal.aborted) ctrl.abort();
        else signal.addEventListener("abort", () => ctrl.abort(), { once: true });
      }
      // Who is signed in is known to the server from a cookie this script cannot read. Requests that
      // change something also carry the CSRF token, which is kept in memory only, never in storage.
      if (state.csrf && options.method && options.method !== "GET") {
        options.headers = { ...options.headers, "X-CSRF-Token": state.csrf };
      }
      try {
        const res = await fetch(CONFIG.API_BASE + path, { credentials: "same-origin", ...options, signal: ctrl.signal });
        if (!res.ok) throw await readError(res);
        return res;
      } catch (err) {
        // The session expired or was ended elsewhere: back to the sign-in screen.
        if (err instanceof ApiError && err.code === "not_signed_in" && state.user) sessionEnded();
        if (err instanceof ApiError) throw err;
        if (signal && signal.aborted) throw new ApiError("aborted", "Stopped.");
        throw new ApiError("backend_offline", BACKEND_OFFLINE_MSG);
      } finally {
        if (timer) clearTimeout(timer);
      }
    },

    /** JSON in, JSON out. */
    async json(path, { method = "GET", body, signal } = {}) {
      const options = { method, signal };
      if (body !== undefined) {
        options.headers = { "Content-Type": "application/json" };
        options.body = JSON.stringify(body);
      }
      return (await this.request(path, options)).json();
    },

    /** Reads a newline-delimited JSON response, calling `onEvent` for each line. */
    async readStream(res, onEvent, signal) {
      if (!res.body || !res.body.getReader) {
        // Very old browsers: no streaming, read it all at once.
        const text = await res.text();
        text.split("\n").filter(Boolean).forEach((l) => onEvent(JSON.parse(l)));
        return;
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      try {
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let nl;
          while ((nl = buffer.indexOf("\n")) >= 0) {
            const line = buffer.slice(0, nl).trim();
            buffer = buffer.slice(nl + 1);
            if (line) onEvent(JSON.parse(line));
          }
        }
        if (buffer.trim()) onEvent(JSON.parse(buffer));
      } catch (err) {
        if (signal && signal.aborted) throw new ApiError("aborted", "Stopped.");
        if (err instanceof ApiError) throw err;          // error event from the server
        if (err instanceof SyntaxError) throw new ApiError("bad_stream", "Received an unreadable reply from the server.");
        throw new ApiError("stream_interrupted", "The connection to the Gihigugma server dropped mid-reply.");
      }
    },

    /** GET /api/health → { ok, ollama, models[], default_model, embed_model, min_score } */
    async health() {
      const res = await this.request("/api/health", { timeoutMs: CONFIG.HEALTH_TIMEOUT_MS });
      const data = await res.json();
      if (!data || data.server !== "eros") throw new ApiError("backend_offline", BACKEND_OFFLINE_MSG);
      return data;
    },

    /**
     * POST /api/projects/<id>/documents — reads and indexes one file locally.
     * `onEvent` receives start | progress events; resolves with the saved document.
     */
    async uploadDocument(projectId, file, { signal, onEvent } = {}) {
      const form = new FormData();
      form.append("file", file);
      const res = await this.request(`/api/projects/${projectId}/documents`, { method: "POST", body: form, signal });
      let doc = null;
      await this.readStream(res, (evt) => {
        if (evt.type === "error") throw new ApiError(evt.code, evt.message);
        if (evt.type === "done") doc = evt.document;
        else if (onEvent) onEvent(evt);
      }, signal);
      if (!doc) throw new ApiError("stream_interrupted", "The upload stopped before the document was saved.");
      return doc;
    },

    /**
     * POST /api/chat — streams newline-delimited JSON events to `onEvent`.
     * Events: start | evidence | token | done | error
     */
    async chatStream({ body, signal, onEvent }) {
      const res = await this.request("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal,
      });
      await this.readStream(res, onEvent, signal);
    },
  };

  /* ---------- 3. STATE ----------
     Projects, documents, findings and chats are stored by the backend in a
     local database; this is the in-memory copy for the active project. */
  const state = {
    view: "chat",              // chat | projects | sources | findings

    projects: [],
    activeProjectId: "",
    documents: [],             // documents of the active project
    findings: [],              // saved findings of the active project
    uploads: [],               // uploads in progress in the Sources view

    chats: [],                 // chats of the active project
    activeChatId: null,
    pendingFiles: [],          // File objects waiting to be sent
    historyFilter: "",

    sources: { docId: null, tab: "relevant", minScore: 0.25, passages: null, pages: null, loading: false, error: "", highlight: null },
    combined: { query: "", docId: "", minScore: 0, suggestions: null, scanning: false },
    fileTarget: "chat",        // where the file picker sends files: chat | sources
    historyRange: 0,           // show chats from the last N days (0 = any time)
    searchResults: null,       // message search results while the search box has text
    user: null,                // the signed-in account: { id, username, email }
    csrf: "",                  // sent with every change; held in memory only
    identity: { lan: null },   // network details for the Collaboration page
    settings: { memory_enabled: false, memory_suggestions: true },
    revision: null,            // the active project's change counter, to notice other devices' updates
    tools: {
      tool: "summarize",       // extract | summarize | analyze | suggest
      docIds: null,            // selected documents; null = all documents in the project
      form: {
        extract: { types: ["facts", "definitions", "arguments", "dates", "statistics", "points"], focus: "", pages: "" },
        summarize: { length: "standard", topic: "", pages: "" },
        analyze: { aspects: ["themes", "arguments", "evidence", "assumptions", "strengths", "weaknesses", "gaps"], compare: false, focus: "", pages: "" },
        suggest: { kinds: ["questions", "topics", "followups", "investigation"], draft: "" },
      },
      running: false,
      abortCtrl: null,
      progress: null,          // { step, total, label }
      result: null,            // the last run: { tool, label, content, evidence, cited, ... }
    },

    // Connection
    connection: "checking",    // checking | online | no_models | ollama_offline | backend_offline
    connectionMessage: "",
    models: [],
    selectedModel: "",
    embedWarned: false,

    // Generation in progress
    sending: false,
    abortCtrl: null,
  };

  const $ = (sel) => document.querySelector(sel);
  const el = {
    app: $("#app"),
    sidebar: $("#sidebar"),
    scrim: $("#scrim"),
    sidebarOpen: $("#sidebar-open"),
    sidebarClose: $("#sidebar-close"),
    newChat: $("#new-chat-btn"),
    historyList: $("#history-list"),
    historySearch: $("#history-search"),
    status: $("#connection-status"),
    chatTitle: $("#chat-title"),
    chat: $("#chat"),
    welcome: $("#welcome"),
    messages: $("#messages"),
    composer: $("#composer"),
    input: $("#composer-input"),
    send: $("#send-btn"),
    attach: $("#attach-btn"),
    fileInput: $("#file-input"),
    attachments: $("#attachments"),
    contextSelect: $("#context-select"),
    modelPicker: $("#model-picker"),
    modelSelect: $("#model-select"),
    refreshModels: $("#refresh-models"),
    projectSelect: $("#project-select"),
    wordCount: $("#word-count"),
    dropzone: $("#dropzone"),
    toasts: $("#toasts"),
    views: {
      projects: $("#view-projects"), sources: $("#view-sources"), findings: $("#view-findings"), tools: $("#view-tools"),
      paraphrase: $("#view-paraphrase"), counter: $("#view-counter"), memory: $("#view-memory"), collab: $("#view-collab"),
    },
    historyRange: $("#history-range"),
    memoryToggle: $("#memory-toggle"),
    toolsRoot: $("#tools-root"),
    projectsRoot: $("#projects-root"),
    sourcesRoot: $("#sources-root"),
    findingsRoot: $("#findings-root"),
    projectDialog: $("#project-dialog"),
    projectForm: $("#project-form"),
    confirmDialog: $("#confirm-dialog"),
  };

  /* ---------- 4. UTILITIES ---------- */
  /**
   * A random id (32 hex characters). crypto.randomUUID only exists on secure pages (https or
   * 127.0.0.1), and other devices reach the host over plain http, so getRandomValues is used:
   * it works everywhere.
   */
  const uid = () => Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, "0")).join("");

  function escapeHtml(str) {
    return String(str)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  /** Minimal, safe markdown: escapes first, then formats. Tolerates partial (streaming) input. */
  function renderMarkdown(src) {
    const codeBlocks = [];
    let text = escapeHtml(src)
      .replace(/```[\w+-]*\n?([\s\S]*?)```/g, (_, code) => {
        codeBlocks.push(`<pre><code>${code.replace(/\n$/, "")}</code></pre>`);
        return `\u0000${codeBlocks.length - 1}\u0000`;
      })
      // An unclosed fence while streaming: render the rest as code so it doesn't flicker.
      .replace(/```[\w+-]*\n?([\s\S]*)$/, (_, code) => {
        codeBlocks.push(`<pre><code>${code}</code></pre>`);
        return `\n\n\u0000${codeBlocks.length - 1}\u0000`;
      });

    const inline = (s) => s
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");

    // Line by line: a heading stands alone, and runs of bullets, numbered items or plain
    // lines each become one block, even when the model leaves no blank line between them.
    const blocks = [];
    let kind = null;
    let run = [];
    const flush = () => {
      if (!run.length) return;
      if (kind === "ul") blocks.push(`<ul>${run.map((l) => `<li>${inline(l.replace(/^[-*•]\s+/, ""))}</li>`).join("")}</ul>`);
      else if (kind === "ol") blocks.push(`<ol>${run.map((l) => `<li>${inline(l.replace(/^\d+[.)]\s+/, ""))}</li>`).join("")}</ol>`);
      else blocks.push(`<p>${run.map(inline).join("<br>")}</p>`);
      run = [];
      kind = null;
    };
    for (const raw of text.split("\n")) {
      const line = raw.trim();
      if (!line) { flush(); continue; }
      if (/^\u0000\d+\u0000$/.test(line)) { flush(); blocks.push(line); continue; }
      const h = line.match(/^(#{1,4})\s+(.*)$/);
      if (h) { flush(); blocks.push(`<p class="md-h"><strong>${inline(h[2])}</strong></p>`); continue; }
      const next = /^[-*•]\s+/.test(line) ? "ul" : /^\d+[.)]\s+/.test(line) ? "ol" : "p";
      if (next !== kind) flush();
      kind = next;
      run.push(line);
    }
    flush();

    return blocks.join("").replace(/\u0000(\d+)\u0000/g, (_, i) => codeBlocks[Number(i)]);
  }

  /** Word count, by the same rules as the Word Counter page (see textstats.js). */
  const countWords = (text) => TextStats.countWords(text);

  function formatBytes(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(0)} KB`;
    if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
    return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
  }

  function formatTime(ts) {
    return new Date(ts).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  }

  function relativeDay(ts) {
    const days = Math.floor((Date.now() - ts) / 86_400_000);
    if (days <= 0) return "Today";
    if (days === 1) return "Yesterday";
    if (days < 7) return `${days} days ago`;
    return new Date(ts).toLocaleDateString([], { month: "short", day: "numeric" });
  }

  const plural = (n, word) => `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`;
  const formatScore = (score) => (typeof score === "number" ? score.toFixed(2) : "");
  const sourceLabel = (name, page) => `${name}${page ? ` · p. ${page}` : ""}`;

  function toast(message, ms = 3200) {
    const t = document.createElement("div");
    t.className = "toast";
    t.textContent = message;
    el.toasts.appendChild(t);
    setTimeout(() => t.remove(), ms);
  }

  /** Shows a failed request as a toast (backticks are for chat messages, not toasts). */
  function toastError(err, fallback = "Something went wrong.") {
    toast((err instanceof ApiError ? err.message : fallback).replace(/`/g, ""), 4500);
  }

  /**
   * Copies text to the clipboard. The modern clipboard API only exists on secure pages (https or
   * 127.0.0.1); devices that reach the host over plain http fall back to the older method.
   */
  async function copyToClipboard(text) {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch { /* try the older way */ }
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.className = "visually-hidden";
    document.body.appendChild(area);
    area.select();
    let copied = false;
    try { copied = document.execCommand("copy"); } catch { copied = false; }
    area.remove();
    return copied;
  }

  function savePref(key, value) {
    try { localStorage.setItem(key, value); } catch { /* storage unavailable */ }
  }
  function loadPref(key) {
    try { return localStorage.getItem(key) || ""; } catch { return ""; }
  }

  /** Asks before something is deleted. Resolves true only if the user confirms. */
  function confirmDialog({ title, text, okLabel = "Delete" }) {
    const dlg = el.confirmDialog;
    $("#confirm-title").textContent = title;
    $("#confirm-text").textContent = text;
    $("#confirm-ok").textContent = okLabel;
    return new Promise((resolve) => {
      const ok = $("#confirm-ok");
      const cancel = $("#confirm-cancel");
      const finish = (answer) => {
        ok.removeEventListener("click", onOk);
        cancel.removeEventListener("click", onCancel);
        dlg.removeEventListener("close", onClose);
        if (dlg.open) dlg.close();
        resolve(answer);
      };
      const onOk = () => finish(true);
      const onCancel = () => finish(false);
      const onClose = () => finish(false);       // Esc
      ok.addEventListener("click", onOk);
      cancel.addEventListener("click", onCancel);
      dlg.addEventListener("close", onClose);
      dlg.showModal();
      cancel.focus();
    });
  }

  /** Asks for a short piece of text (a chat name, a memory). Resolves the text, or null if cancelled. */
  function textDialog({ title, label, value = "", okLabel = "Save" }) {
    const dlg = $("#text-dialog");
    const input = $("#text-dialog-input");
    $("#text-dialog-title").textContent = title;
    $("#text-dialog-label").textContent = label;
    $("#text-dialog-ok").textContent = okLabel;
    input.value = value;
    return new Promise((resolve) => {
      const form = $("#text-form");
      const cancel = $("#text-dialog-cancel");
      const finish = (answer) => {
        form.removeEventListener("submit", onSubmit);
        cancel.removeEventListener("click", onCancel);
        dlg.removeEventListener("close", onCancel);
        if (dlg.open) dlg.close();
        resolve(answer);
      };
      const onSubmit = (e) => { e.preventDefault(); if (input.value.trim()) finish(input.value.trim()); };
      const onCancel = () => finish(null);
      form.addEventListener("submit", onSubmit);
      cancel.addEventListener("click", onCancel);
      dlg.addEventListener("close", onCancel);
      dlg.showModal();
      input.focus();
      input.select();
    });
  }

  const ICONS = {
    file: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/></svg>',
    copy: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>',
    retry: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7"/></svg>',
    save: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 4h12v16l-6-4-6 4z"/></svg>',
    x: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 7l10 10M17 7L7 17"/></svg>',
    edit: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20h4L19 9l-4-4L4 16zM13.5 6.5l4 4"/></svg>',
    book: '<svg viewBox="0 0 24 24" aria-hidden="true" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 5a2 2 0 0 1 2-2h13v16H6a2 2 0 0 0-2 2z"/></svg>',
    send: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6" /></svg>',
    stop: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="7" y="7" width="10" height="10" rx="2" /></svg>',
  };

  /* ---------- 5. EVIDENCE, PASSAGES AND FINDINGS ----------
     One renderer for a passage wherever it appears (chat evidence, a
     document's relevant passages, suggestions) and one for a saved finding,
     so the Sources and Combined views show the same records the same way. */
  const activeProject = () => state.projects.find((p) => p.id === state.activeProjectId) || null;

  // Passages currently on screen, looked up when a Save / Open source button is clicked.
  const passageIndex = new Map();

  const savedFinding = (docId, index) =>
    state.findings.find((f) => f.kind === "passage" && f.doc_id === docId && f.chunk_idx === index);

  /**
   * A passage quoted from a document. `scope` keeps keys unique per place it is shown;
   * `question` is what the passage answers (the chat question, or the matched research question).
   */
  function passageHtml(p, { scope, question, number, cited } = {}) {
    const key = `${scope}|${p.doc_id}:${p.index}`;
    passageIndex.set(key, { ...p, question: question ?? p.match?.label ?? "" });
    const saved = savedFinding(p.doc_id, p.index);
    return `
      <article class="passage${cited === false ? " is-uncited" : ""}">
        <div class="passage__meta">
          <span class="source">${ICONS.book}${number ? `[${number}] ` : ""}${escapeHtml(sourceLabel(p.name, p.page))}</span>
          ${typeof p.score === "number" ? `<span class="score" title="${SCORE_HINT}">Similarity ${formatScore(p.score)}</span>` : ""}
          ${cited ? '<span class="badge">Cited</span>' : ""}
          ${p.match ? `<span class="passage__q">Matches: ${escapeHtml(p.match.label)}</span>` : ""}
        </div>
        <blockquote class="quote">${escapeHtml(p.text)}</blockquote>
        <div class="passage__actions">
          <button type="button" class="msg__action" data-act="save-passage" data-key="${escapeHtml(key)}"${saved ? " disabled" : ""}>${saved ? "Saved to findings" : `${ICONS.save}Save finding`}</button>
          <button type="button" class="msg__action" data-act="open-source" data-key="${escapeHtml(key)}">${ICONS.file}Open source</button>
        </div>
      </article>`;
  }

  /** A saved finding: the quoted evidence, any AI explanation (kept visibly separate), and the user's note. */
  function findingHtml(f) {
    const note = `
      <label class="visually-hidden" for="note-${f.id}">Your notes</label>
      <textarea class="input" id="note-${f.id}" rows="1" data-note="${f.id}" placeholder="Your notes…">${escapeHtml(f.user_note)}</textarea>`;

    if (f.kind === "answer") {
      const sources = f.sources.map((s) =>
        `<span class="source">${ICONS.book}${s.n ? `[${s.n}] ` : ""}${escapeHtml(sourceLabel(s.name, s.page))}</span>`).join("");
      return `
        <article class="passage" data-finding="${f.id}">
          <div class="passage__meta"><span class="badge">Saved AI answer</span>
            ${f.question ? `<span class="passage__q">${escapeHtml(f.question)}</span>` : ""}</div>
          <span class="label">AI answer, generated from the sources below</span>
          <div class="ainote">${renderMarkdown(f.quote)}</div>
          ${sources ? `<div class="tags">${sources}</div>` : ""}
          ${note}
          <div class="passage__actions">
            <button type="button" class="msg__action" data-act="delete-finding" data-id="${f.id}">${ICONS.x}Remove</button>
          </div>
        </article>`;
    }

    return `
      <article class="passage" data-finding="${f.id}">
        <div class="passage__meta">
          <span class="source">${ICONS.book}${escapeHtml(sourceLabel(f.doc_name || "Unknown source", f.page))}</span>
          ${f.score != null ? `<span class="score" title="${SCORE_HINT}">Similarity ${formatScore(f.score)}</span>` : ""}
          ${f.question ? `<span class="passage__q">${escapeHtml(f.question)}</span>` : ""}
        </div>
        <span class="label">Source evidence, quoted from the document</span>
        <blockquote class="quote">${escapeHtml(f.quote)}</blockquote>
        ${f.ai_note ? `<span class="label">AI explanation, generated</span><div class="ainote">${renderMarkdown(f.ai_note)}</div>` : ""}
        ${note}
        <div class="passage__actions">
          <button type="button" class="msg__action" data-act="open-finding-source" data-id="${f.id}">${ICONS.file}Open source</button>
          <button type="button" class="msg__action" data-act="explain-finding" data-id="${f.id}">${f.ai_note ? "Explain again" : "Explain with AI"}</button>
          <button type="button" class="msg__action" data-act="delete-finding" data-id="${f.id}">${ICONS.x}Remove</button>
        </div>
      </article>`;
  }

  /** Re-draws whatever is on screen after findings change. */
  function findingsChanged() {
    if (state.view === "sources") renderSources();
    else if (state.view === "findings") renderCombinedGroups();
    else if (state.view === "tools") renderToolResult();
    const chat = activeChat();
    if (chat) chat.messages.forEach((m) => { if (m.evidence) updateMessageNode(m); });
    refreshProjects();
  }

  async function savePassage(key, button) {
    const p = passageIndex.get(key);
    const project = activeProject();
    if (!p || !project) return;
    button.disabled = true;
    try {
      const { finding, created } = await api.json(`/api/projects/${project.id}/findings`, {
        method: "POST",
        body: { kind: "passage", doc_id: p.doc_id, chunk_index: p.index, question: p.question, score: p.score },
      });
      if (!state.findings.some((f) => f.id === finding.id)) state.findings.unshift(finding);
      toast(created ? "Saved to findings" : "Already in your findings");
      findingsChanged();
    } catch (err) {
      button.disabled = false;
      toastError(err, "Couldn't save that finding.");
    }
  }

  async function saveAnswer(msg) {
    const project = activeProject();
    if (!project || msg.savedAnswer) return;
    const cited = (msg.evidence || []).filter((p) => (msg.cited || []).includes(p.n));
    try {
      const { finding } = await api.json(`/api/projects/${project.id}/findings`, {
        method: "POST",
        body: {
          kind: "answer", question: msg.question || "", text: msg.content,
          sources: (cited.length ? cited : msg.evidence || []).map(({ n, doc_id, name, page, index }) => ({ n, doc_id, name, page, index })),
        },
      });
      state.findings.unshift(finding);
      msg.savedAnswer = true;
      updateMessageNode(msg);
      toast("Answer saved to findings");
      refreshProjects();
      patchMessage(msg, { savedAnswer: true });
    } catch (err) {
      toastError(err, "Couldn't save that answer.");
    }
  }

  async function explainFinding(id, button) {
    button.disabled = true;
    button.textContent = "Explaining…";
    try {
      const { finding } = await api.json(`/api/findings/${id}/explain`, { method: "POST", body: { model: state.selectedModel } });
      state.findings = state.findings.map((f) => (f.id === id ? finding : f));
    } catch (err) {
      toastError(err, "Couldn't generate an explanation.");
    }
    findingsChanged();
  }

  async function deleteFinding(id) {
    const ok = await confirmDialog({ title: "Remove this finding?", text: "The source document is not affected.", okLabel: "Remove" });
    if (!ok) return;
    try {
      await api.json(`/api/findings/${id}`, { method: "DELETE" });
      state.findings = state.findings.filter((f) => f.id !== id);
      findingsChanged();
    } catch (err) {
      toastError(err, "Couldn't remove that finding.");
    }
  }

  async function saveNote(id, value) {
    try {
      const { finding } = await api.json(`/api/findings/${id}`, { method: "PATCH", body: { user_note: value } });
      state.findings = state.findings.map((f) => (f.id === id ? finding : f));
    } catch (err) {
      toastError(err, "Couldn't save your note.");
    }
  }

  /* ---------- 6. CHAT RENDERING ---------- */
  const activeChat = () => state.chats.find((c) => c.id === state.activeChatId) || null;

  /** Earliest time a chat may have been updated to be listed, from the date filter. */
  const historySince = () => (state.historyRange ? Date.now() - state.historyRange * 86_400_000 : 0);

  function renderHistory() {
    const q = state.historyFilter.trim();
    el.historyList.innerHTML = "";

    // With text in the search box the list shows matching messages, found by the server's full-text index.
    if (q && state.searchResults) {
      if (!state.searchResults.length) {
        el.historyList.innerHTML = '<li class="history__empty">No chats or messages match.</li>';
        return;
      }
      for (const hit of state.searchResults) {
        const li = document.createElement("li");
        const btn = document.createElement("button");
        btn.className = "history__item";
        btn.dataset.chatId = hit.chat_id;
        if (hit.seq !== null) btn.dataset.seq = hit.seq;
        const who = { user: "You", assistant: "Gihigugma", title: "Title" }[hit.role] || "";
        btn.innerHTML = `
          <span class="history__item-title">${escapeHtml(hit.title)}</span>
          <span class="history__item-snippet">${who ? `${who}: ` : ""}${escapeHtml(hit.snippet).replace(/‹/g, "<mark>").replace(/›/g, "</mark>")}</span>
          <span class="history__item-meta">${relativeDay(hit.updatedAt)}</span>`;
        li.appendChild(btn);
        el.historyList.appendChild(li);
      }
      return;
    }

    const since = historySince();
    const chats = [...state.chats]
      .filter((c) => c.messages.length > 0 || c.id === state.activeChatId)
      .filter((c) => !q || c.title.toLowerCase().includes(q.toLowerCase()))
      .filter((c) => c.updatedAt >= since || c.id === state.activeChatId)
      .sort((a, b) => b.updatedAt - a.updatedAt);

    if (!chats.length) {
      el.historyList.innerHTML = `<li class="history__empty">${q || since ? "No chats match." : "No chats yet."}</li>`;
      return;
    }

    for (const chat of chats) {
      const li = document.createElement("li");
      li.className = "history__row";
      const btn = document.createElement("button");
      btn.className = "history__item" + (chat.id === state.activeChatId ? " is-active" : "");
      btn.dataset.chatId = chat.id;
      if (chat.id === state.activeChatId) btn.setAttribute("aria-current", "true");
      btn.innerHTML = `
        <span class="history__item-title">${escapeHtml(chat.title)}</span>
        <span class="history__item-meta">${relativeDay(chat.updatedAt)}</span>`;
      li.appendChild(btn);
      if (chat.messages.length) {
        const tools = document.createElement("span");
        tools.className = "history__tools";
        tools.innerHTML = `
          <button type="button" data-act="rename-chat" data-id="${chat.id}" aria-label="Rename chat" title="Rename">${ICONS.edit}</button>
          <button type="button" data-act="delete-chat" data-id="${chat.id}" aria-label="Delete chat" title="Delete">${ICONS.x}</button>`;
        li.appendChild(tools);
      }
      el.historyList.appendChild(li);
    }
  }

  let searchTimer = null;
  /** Searches every message of the project's chats as the user types. */
  function searchHistory() {
    clearTimeout(searchTimer);
    const q = state.historyFilter.trim();
    const project = activeProject();
    if (q.length < 2 || !project) { state.searchResults = null; renderHistory(); return; }
    searchTimer = setTimeout(async () => {
      try {
        const params = new URLSearchParams({ q, since: historySince() });
        const data = await api.json(`/api/projects/${project.id}/search?${params}`);
        if (state.historyFilter.trim() === q) state.searchResults = data.results;
      } catch {
        state.searchResults = null;       // fall back to matching titles in the list
      }
      renderHistory();
    }, 250);
  }

  async function renameChat(id) {
    const chat = state.chats.find((c) => c.id === id);
    if (!chat) return;
    const title = await textDialog({ title: "Rename chat", label: "Chat name", value: chat.title });
    if (!title || title === chat.title) return;
    try {
      await api.json(`/api/conversations/${id}`, { method: "PATCH", body: { title: title.slice(0, 200) } });
      chat.title = title.slice(0, 200);
      renderHistory();
      renderTitle();
    } catch (err) {
      toastError(err, "Couldn't rename the chat.");
    }
  }

  async function deleteChat(id) {
    const chat = state.chats.find((c) => c.id === id);
    if (!chat) return;
    const ok = await confirmDialog({
      title: `Delete "${chat.title}"?`,
      text: "The conversation and its messages are deleted from this computer. Documents, saved findings and saved memories are kept.",
    });
    if (!ok) return;
    try {
      await api.json(`/api/conversations/${id}`, { method: "DELETE" });
    } catch (err) {
      if (err.status !== 404) { toastError(err, "Couldn't delete the chat."); return; }
    }
    state.chats = state.chats.filter((c) => c.id !== id);
    if (state.activeChatId === id) newChat({ focus: false, show: state.view === "chat" });
    if (state.historyFilter.trim()) searchHistory(); else renderHistory();
    refreshProjects();
    toast("Chat deleted");
  }

  function msgName(msg) {
    if (msg.role === "user") return "You";
    if (msg.role === "system") return msg.variant === "error" ? "Couldn't get a reply" : "Note";
    return msg.model ? `Gihigugma · ${msg.model}` : "Gihigugma";
  }

  function statsText(stats) {
    if (!stats) return "";
    const parts = [];
    if (stats.tokens) parts.push(`${stats.tokens} tokens`);
    if (stats.tokens_per_sec) parts.push(`${stats.tokens_per_sec} tok/s`);
    if (stats.total_ms) parts.push(`${(stats.total_ms / 1000).toFixed(1)} s`);
    return parts.join(" · ");
  }

  /** The passages an answer was generated from, kept apart from the answer itself. */
  function evidenceHtml(msg) {
    const done = Array.isArray(msg.cited);
    const cards = msg.evidence.map((p) => passageHtml(p, {
      scope: msg.uid, question: msg.question, number: p.n,
      cited: done ? msg.cited.includes(p.n) : undefined,
    })).join("");
    return `
      <details class="evidence">
        <summary>Source evidence · ${plural(msg.evidence.length, "passage")} from your documents</summary>
        <p class="hint">The reply above was written by the AI from these passages only. The quotes below are the original text.
          ${msg.coverage ? "They are the sections of the document that were read, in order."
            : "Similarity shows how closely a passage matches your question; it is not a measure of accuracy."}</p>
        <div class="passages">${cards}</div>
      </details>`;
  }

  /** Builds a message element. `msg.node` keeps a reference for streaming updates. */
  function messageNode(msg) {
    const li = document.createElement("li");
    li.className = `msg msg--${msg.role}` + (msg.variant ? ` is-${msg.variant}` : "");
    msg.uid ||= uid();

    const avatar = { user: "You", assistant: "G", system: msg.variant === "error" ? "!" : "i" }[msg.role];

    const attachments = (msg.attachments || []).map((a) =>
      `<span class="attachment">${ICONS.file}<span class="attachment__name">${escapeHtml(a.name)}</span>
        <span class="attachment__size">${formatBytes(a.size)}</span></span>`).join("");

    li.innerHTML = `
      <div class="msg__avatar" aria-hidden="true">${avatar}</div>
      <div class="msg__body">
        <div class="msg__head">
          <span class="msg__name">${escapeHtml(msgName(msg))}</span>
          <time class="msg__time" datetime="${new Date(msg.time).toISOString()}">${formatTime(msg.time)}</time>
          <span class="msg__stats"></span>
        </div>
        <div class="msg__content"></div>
        <div class="msg__notice" hidden></div>
        ${attachments ? `<div class="msg__attachments">${attachments}</div>` : ""}
        <div class="msg__sources" aria-label="Source evidence" hidden></div>
        <div class="msg__memory" hidden></div>
        <div class="msg__actions"></div>
      </div>`;

    msg.node = li;
    updateMessageNode(msg);
    return li;
  }

  /** Re-renders a message's dynamic parts (content, loading state, notice, evidence, actions). */
  function updateMessageNode(msg) {
    const li = msg.node;
    if (!li) return;
    const content = li.querySelector(".msg__content");
    const notice = li.querySelector(".msg__notice");
    const actions = li.querySelector(".msg__actions");

    li.className = `msg msg--${msg.role}` + (msg.variant ? ` is-${msg.variant}` : "");
    li.classList.toggle("is-pending", Boolean(msg.pending));
    li.classList.toggle("is-streaming", Boolean(msg.streaming));
    li.setAttribute("aria-busy", msg.pending || msg.streaming ? "true" : "false");
    li.querySelector(".msg__avatar").textContent = { user: "You", assistant: "G", system: msg.variant === "error" ? "!" : "i" }[msg.role];
    li.querySelector(".msg__name").textContent = msgName(msg);
    li.querySelector(".msg__stats").textContent = statsText(msg.stats);

    if (msg.pending) {
      content.innerHTML = `
        <div class="typing" role="status">
          <span class="typing__dots" aria-hidden="true"><i></i><i></i><i></i></span>
          <span class="typing__label">${escapeHtml(msg.pendingLabel || "Thinking…")}</span>
        </div>`;
    } else {
      content.innerHTML = renderMarkdown(msg.content);
    }

    notice.hidden = !msg.notice;
    notice.textContent = msg.notice || "";

    // Evidence is rebuilt only when it changes, so an opened panel stays open while text streams in.
    const sourcesEl = li.querySelector(".msg__sources");
    const evidence = !msg.pending && msg.evidence && msg.evidence.length ? msg.evidence : null;
    const signature = evidence
      ? [evidence.length, (msg.cited || ["-"]).join(","), evidence.map((p) => (savedFinding(p.doc_id, p.index) ? 1 : 0)).join("")].join("|")
      : "";
    if (sourcesEl.dataset.signature !== signature) {
      const wasOpen = sourcesEl.querySelector("details")?.open;
      sourcesEl.dataset.signature = signature;
      sourcesEl.hidden = !evidence;
      sourcesEl.innerHTML = evidence ? evidenceHtml(msg) : "";
      if (wasOpen) sourcesEl.querySelector("details").open = true;
    }

    // Remembered notes that were given to the model for this reply, kept apart from the reply and the evidence.
    const memoryEl = li.querySelector(".msg__memory");
    const used = msg.memories || [];
    memoryEl.hidden = !used.length;
    memoryEl.innerHTML = used.length
      ? `<span class="label">From memory, not from this chat or your documents</span>${used.map((m) =>
        `<span class="chip">${escapeHtml(m.content)}</span>`).join("")}`
      : "";

    actions.innerHTML = "";
    if (msg.pending || msg.streaming) return;
    if (msg.memoryId && !msg.memoryDone) {
      // A suggested memory: nothing is remembered unless the user says so.
      for (const [label, approve] of [["Save to memory", true], ["Don't save", false]]) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "btn btn--small" + (approve ? " btn--primary" : "");
        button.textContent = label;
        button.addEventListener("click", () => answerSuggestion(msg, approve));
        actions.appendChild(button);
      }
      actions.classList.add("is-shown");
      return;
    }
    actions.classList.remove("is-shown");
    if (msg.role !== "system" && msg.content) {
      const copy = document.createElement("button");
      copy.type = "button";
      copy.className = "msg__action";
      copy.innerHTML = `${ICONS.copy}Copy`;
      copy.addEventListener("click", async () => {
        toast((await copyToClipboard(msg.content)) ? "Copied to clipboard" : "Couldn't copy. Select the text and copy it by hand.");
      });
      actions.appendChild(copy);
    }
    if (msg.role === "assistant" && msg.grounding === "documents" && msg.content) {
      const save = document.createElement("button");
      save.type = "button";
      save.className = "msg__action";
      save.disabled = Boolean(msg.savedAnswer);
      save.innerHTML = msg.savedAnswer ? "Answer saved" : `${ICONS.save}Save answer`;
      save.addEventListener("click", () => saveAnswer(msg));
      actions.appendChild(save);
    }
    if (msg.retryable) {
      const retry = document.createElement("button");
      retry.type = "button";
      retry.className = "msg__action";
      retry.innerHTML = `${ICONS.retry}Retry`;
      retry.addEventListener("click", () => retryLast(msg));
      actions.appendChild(retry);
    }
  }

  function renderChat() {
    const chat = activeChat();
    el.messages.innerHTML = "";
    const hasMessages = chat && chat.messages.length > 0;

    el.welcome.hidden = hasMessages;
    renderTitle();

    if (hasMessages) {
      const frag = document.createDocumentFragment();
      chat.messages.forEach((m) => frag.appendChild(messageNode(m)));
      el.messages.appendChild(frag);
      scrollToBottom(true);
    }
  }

  function appendMessage(chat, msg) {
    chat.messages.push(msg);
    if (chat.id !== state.activeChatId) return;   // user switched chats mid-reply
    el.welcome.hidden = true;
    el.messages.appendChild(messageNode(msg));
    scrollToBottom(true);
  }

  function removeMessage(chat, msg) {
    chat.messages = chat.messages.filter((m) => m !== msg);
    msg.node?.remove();
  }

  const nearBottom = () => el.chat.scrollHeight - el.chat.scrollTop - el.chat.clientHeight < 120;

  function scrollToBottom(force = false) {
    if (!force && !nearBottom()) return;   // don't yank the user away while they read
    requestAnimationFrame(() => { el.chat.scrollTop = el.chat.scrollHeight; });
  }

  /** What is stored with a message besides its text: evidence, attachments and so on, minus the page's own state. */
  function messageMeta(m) {
    const { node, pending, streaming, pendingLabel, retryable, local, sending, id, role, content, time, ...meta } = m;
    return meta;
  }

  /** A conversation as loaded from the host, ready to show. */
  function hydrateChat(c) {
    const messages = c.messages.map((m) => (m.role === "assistant" && !m.notice ? { ...m, notice: groundingNotice(m) } : m));
    return {
      ...c, messages, projectId: c.project_id, saved: true, savedMemoryExcluded: c.memoryExcluded,
      focusDocIds: messages.filter((m) => m.focusDocIds).at(-1)?.focusDocIds || [],
    };
  }

  /**
   * Stores on the host whatever this chat shows that the host doesn't have yet: notes about
   * attached files, a tool result brought into the chat, and the per-chat memory switch.
   * Questions and the model's replies are stored by the server itself when a message is sent.
   * Saves run one after another per chat, so nothing is stored twice.
   * With `strict`, a failure is thrown to the caller instead of being retried quietly later.
   */
  function saveChat(chat, { strict = false } = {}) {
    const run = async () => {
      if (!chat.projectId) return;
      const unsaved = chat.messages.filter((m) => !m.id && !m.local && !m.sending && !m.pending && !m.streaming);
      if (unsaved.length) {
        const stored = await api.json(`/api/conversations/${chat.id}/messages`, {
          method: "POST",
          body: { project_id: chat.projectId, messages: unsaved.map((m) => ({ role: m.role, content: m.content, meta: messageMeta(m) })) },
        });
        stored.messages.forEach((s, i) => { unsaved[i].id = s.id; });
        if (!chat.saved && chat.title !== stored.conversation.title) {
          await api.json(`/api/conversations/${chat.id}`, { method: "PATCH", body: { title: chat.title } });
        }
        chat.saved = true;
      }
      // The memory switch can be flipped before the chat exists on the host; store it once it does.
      if (Boolean(chat.memoryExcluded) !== Boolean(chat.savedMemoryExcluded)) {
        if (!chat.saved) {
          await api.json("/api/conversations", { method: "POST", body: { id: chat.id, project_id: chat.projectId } });
          chat.saved = true;
        }
        await api.json(`/api/conversations/${chat.id}`, { method: "PATCH", body: { memory_excluded: Boolean(chat.memoryExcluded) } });
        chat.savedMemoryExcluded = Boolean(chat.memoryExcluded);
      }
    };
    chat.queue = (chat.queue || Promise.resolve()).catch(() => {}).then(run);
    // Not worth interrupting the user by default: the chat stays usable and is saved again next time.
    return strict ? chat.queue : chat.queue.catch(() => {});
  }

  /** Updates what is stored with one message (for example that its answer was saved to findings). */
  async function patchMessage(msg, meta, content) {
    const chat = state.chats.find((c) => c.messages.includes(msg));
    if (!chat) return;
    await saveChat(chat);                // the message must exist on the host first
    if (!msg.id) return;
    try {
      await api.json(`/api/conversations/${chat.id}/messages/${msg.id}`, { method: "PATCH", body: { meta, content } });
    } catch {
      /* the page already shows the change; it is only not remembered after a reload */
    }
  }

  /* ---------- 7. CONNECTION & MODELS ---------- */
  const STATUS_TEXT = {
    checking:        "Checking connection…",
    online:          "Ollama connected",
    no_models:       "Ollama connected · no models",
    ollama_offline:  "Ollama offline",
    backend_offline: "Gihigugma server offline",
  };

  function renderStatus() {
    const s = state.connection;
    const dotState = s === "online" ? "online" : s === "checking" ? "checking" : s === "backend_offline" ? "error" : "offline";
    el.status.dataset.state = dotState;
    el.modelPicker.dataset.state = dotState;

    let text = STATUS_TEXT[s];
    if (s === "online") text += ` · ${state.models.length} model${state.models.length === 1 ? "" : "s"}`;
    el.status.querySelector(".status__text").textContent = text;
    el.status.title = state.connectionMessage || text;
    el.modelPicker.title = state.connectionMessage || (state.selectedModel ? `Using ${state.selectedModel}` : text);

    // Model dropdown
    const sel = el.modelSelect;
    if (s === "online") {
      const current = [...sel.options].map((o) => o.value).join("|");
      if (current !== state.models.join("|") || sel.disabled) {
        sel.innerHTML = state.models.map((m) => `<option value="${escapeHtml(m)}">${escapeHtml(m)}</option>`).join("");
      }
      sel.value = state.selectedModel;
      sel.disabled = state.sending;
    } else {
      const label = {
        checking: "Checking models…",
        no_models: "No models installed",
        ollama_offline: "Ollama offline",
        backend_offline: "Server offline",
      }[s];
      sel.innerHTML = `<option value="">${label}</option>`;
      sel.disabled = true;
    }

    el.projectSelect.disabled = state.sending || !state.projects.length;
  }

  function pickModel(models, serverDefault) {
    const saved = loadPref(CONFIG.MODEL_PREF_KEY);
    if (state.selectedModel && models.includes(state.selectedModel)) return state.selectedModel;
    if (saved && models.includes(saved)) return saved;
    if (serverDefault && models.includes(serverDefault)) return serverDefault;
    return models[0] || "";
  }

  let healthInFlight = null;
  async function checkHealth({ announce = false } = {}) {
    if (healthInFlight) return healthInFlight;
    const prev = state.connection;
    healthInFlight = (async () => {
      let embedMissing = "";
      try {
        const h = await api.health();
        if (!h.ollama.reachable) {
          state.connection = "ollama_offline";
          state.connectionMessage = h.ollama.error?.message || "Ollama isn't running.";
          state.models = [];
        } else {
          state.models = h.models;
          state.selectedModel = pickModel(h.models, h.default_model);
          state.connection = h.models.length ? "online" : "no_models";
          state.connectionMessage = h.models.length
            ? `Ollama ${h.ollama.version} at ${h.ollama.url}`
            : "No models installed. Run `ollama pull qwen2.5:3b` in a terminal, then refresh.";
          if (h.embed_model && !h.embed_model.installed) embedMissing = h.embed_model.name;
        }
      } catch (err) {
        state.connection = "backend_offline";
        state.connectionMessage = err.message;
        state.models = [];
      } finally {
        healthInFlight = null;
      }
      renderStatus();
      if (announce || (prev !== "checking" && prev !== state.connection)) {
        toast(state.connection === "online"
          ? `Connected · ${state.models.length} model${state.models.length === 1 ? "" : "s"} available`
          : state.connectionMessage.replace(/`/g, ""), 4500);
      }
      if (embedMissing && !state.embedWarned) {
        state.embedWarned = true;
        toast(`Documents can't be read yet: run "ollama pull ${embedMissing}" in a terminal.`, 7000);
      }
      // The server came back: load the projects it holds.
      if (prev === "backend_offline" && state.connection !== "backend_offline" && state.user) loadWorkspace();
    })();
    return healthInFlight;
  }

  /* ---------- 8. PROJECTS ---------- */
  function renderProjectSelect() {
    const sel = el.projectSelect;
    sel.innerHTML = state.projects.length
      ? state.projects.map((p) => `<option value="${p.id}">${escapeHtml(p.title)}</option>`).join("")
      : '<option value="">No project</option>';
    sel.value = state.activeProjectId;
    sel.disabled = state.sending || !state.projects.length;
  }

  /** Re-reads the project list (titles and counts) without changing the active project. */
  async function refreshProjects() {
    try {
      state.projects = (await api.json("/api/projects")).projects;
    } catch {
      return false;
    }
    renderProjectSelect();
    if (state.view === "projects") renderProjects();
    return true;
  }

  /** Makes a project active and loads its documents, findings and chats. */
  async function selectProject(id, { view } = {}) {
    state.activeProjectId = id;
    savePref(CONFIG.PROJECT_PREF_KEY, id);
    state.documents = [];
    state.findings = [];
    state.chats = [];
    state.uploads = [];
    state.pendingFiles = [];
    Object.assign(state.sources, { docId: null, tab: "relevant", passages: null, pages: null, error: "", highlight: null });
    Object.assign(state.combined, { query: "", docId: "", minScore: 0, suggestions: null });
    if (state.tools.abortCtrl) state.tools.abortCtrl.abort();
    Object.assign(state.tools, { docIds: null, result: null, progress: null });
    renderProjectSelect();

    if (id) {
      try {
        const [docs, findings, chats] = await Promise.all([
          api.json(`/api/projects/${id}/documents`),
          api.json(`/api/projects/${id}/findings`),
          api.json(`/api/conversations?project_id=${id}&include=messages`),      // the signed-in user's own
        ]);
        if (state.activeProjectId !== id) return;      // switched again while loading
        state.documents = docs.documents;
        state.findings = findings.findings;
        state.chats = chats.conversations.map(hydrateChat);
      } catch (err) {
        toastError(err, "Couldn't load that project.");
      }
    }
    state.revision = null;          // the next check records where this project stands
    state.searchResults = null;
    renderAttachments();
    newChat({ focus: false, show: false });
    showView(view || state.view);
  }

  /** First load, and again whenever the server comes back. */
  async function loadWorkspace() {
    if (!(await refreshProjects())) return;
    const saved = loadPref(CONFIG.PROJECT_PREF_KEY);
    const project = state.projects.find((p) => p.id === saved) || state.projects[0];
    if (project) await selectProject(project.id, { view: "chat" });
    else { state.activeProjectId = ""; showView("projects"); }
  }

  function renderProjects() {
    const cards = state.projects.map((p) => {
      const active = p.id === state.activeProjectId;
      const questions = p.questions.slice(0, 4).map((q) => `<li>${escapeHtml(q)}</li>`).join("");
      const more = p.questions.length > 4 ? `<li>and ${p.questions.length - 4} more</li>` : "";
      return `
        <article class="card${active ? " is-active" : ""}">
          <div class="card__head"><h3>${escapeHtml(p.title)}</h3>${active ? '<span class="badge">Active</span>' : ""}</div>
          ${p.objectives ? `<p class="card__text">${escapeHtml(p.objectives)}</p>` : ""}
          ${questions ? `<ul class="card__list">${questions}${more}</ul>` : ""}
          ${p.keywords.length ? `<div class="tags">${p.keywords.map((k) => `<span class="chip">${escapeHtml(k)}</span>`).join("")}</div>` : ""}
          ${p.scope ? `<p class="card__text"><span class="label">Scope</span> ${escapeHtml(p.scope)}</p>` : ""}
          <p class="stats"><span><b>${p.stats.documents}</b> documents</span><span><b>${p.stats.sections}</b> sections</span>
            <span><b>${p.stats.findings}</b> findings</span><span><b>${p.stats.chats}</b> chats</span></p>
          <div class="card__actions">
            <button type="button" class="btn btn--small${active ? "" : " btn--primary"}" data-act="open-project" data-id="${p.id}">${active ? "Open" : "Make active"}</button>
            ${p.shared ? `<span class="badge">${p.role === "owner" ? "Shared" : `Joined as ${escapeHtml(p.role)}`}</span>` : ""}
            ${p.role === "owner" ? `
            <button type="button" class="btn btn--small" data-act="edit-project" data-id="${p.id}">Edit</button>
            <button type="button" class="btn btn--small btn--danger" data-act="delete-project" data-id="${p.id}">Delete</button>` : ""}
          </div>
        </article>`;
    }).join("");

    el.projectsRoot.innerHTML = `
      <header class="view__head">
        <div><h2>Research projects</h2><p>Each project keeps its own documents, findings and chats.</p></div>
        <button type="button" class="btn btn--primary" data-act="new-project">New project</button>
      </header>
      ${state.projects.length ? `<div class="cards">${cards}</div>` : `
        <div class="empty">
          <strong>Start your first research project</strong>
          <p>Give it a topic and the questions you want answered. Gihigugma uses them to find the relevant parts of the documents you add.</p>
          <button type="button" class="btn btn--primary" data-act="new-project">New project</button>
        </div>`}`;
  }

  let editingProjectId = null;

  function openProjectDialog(project = null) {
    editingProjectId = project ? project.id : null;
    $("#project-dialog-title").textContent = project ? "Edit research project" : "New research project";
    $("#pf-title").value = project?.title || "";
    $("#pf-objectives").value = project?.objectives || "";
    $("#pf-questions").value = (project?.questions || []).join("\n");
    $("#pf-keywords").value = (project?.keywords || []).join(", ");
    $("#pf-scope").value = project?.scope || "";
    el.projectDialog.showModal();
    $("#pf-title").focus();
  }

  async function submitProject(e) {
    e.preventDefault();
    const split = (value, sep) => value.split(sep).map((s) => s.trim()).filter(Boolean);
    const body = {
      title: $("#pf-title").value.trim(),
      objectives: $("#pf-objectives").value.trim(),
      questions: split($("#pf-questions").value, "\n"),
      keywords: split($("#pf-keywords").value, ","),
      scope: $("#pf-scope").value.trim(),
    };
    if (!body.title) { $("#pf-title").focus(); return; }
    const saveBtn = $("#pf-save");
    saveBtn.disabled = true;
    try {
      if (editingProjectId) {
        await api.json(`/api/projects/${editingProjectId}`, { method: "PATCH", body });
        el.projectDialog.close();
        await refreshProjects();
        // The topic or questions may have changed, so matched passages are recomputed when next viewed.
        state.sources.passages = null;
        state.combined.suggestions = null;
        showView(state.view);
        toast("Project updated");
      } else {
        const { project } = await api.json("/api/projects", { method: "POST", body });
        el.projectDialog.close();
        await refreshProjects();
        await selectProject(project.id, { view: "sources" });
        toast("Project created. Add documents to start researching.", 4500);
      }
    } catch (err) {
      toastError(err, "Couldn't save the project.");
    } finally {
      saveBtn.disabled = false;
    }
  }

  async function deleteProject(id) {
    const project = state.projects.find((p) => p.id === id);
    if (!project) return;
    const ok = await confirmDialog({
      title: `Delete "${project.title}"?`,
      text: `This permanently deletes its ${plural(project.stats.documents, "document")}, ${plural(project.stats.findings, "finding")} and ${plural(project.stats.chats, "chat")} from this computer.`,
    });
    if (!ok) return;
    try {
      await api.json(`/api/projects/${id}`, { method: "DELETE" });
    } catch (err) {
      toastError(err, "Couldn't delete the project.");
      return;
    }
    await refreshProjects();
    if (id === state.activeProjectId) await selectProject(state.projects[0]?.id || "", { view: "projects" });
    toast("Project deleted");
  }

  /* ---------- 9. DOCUMENTS & SOURCES VIEW ---------- */
  /** Splits a file list into files Gihigugma can read and reasons for the rest. */
  function checkFiles(fileList) {
    const accepted = [];
    const rejected = [];
    for (const file of fileList) {
      const ext = "." + file.name.split(".").pop().toLowerCase();
      if (!CONFIG.ACCEPTED_EXT.includes(ext)) rejected.push(`${file.name} (unsupported type)`);
      else if (file.size > CONFIG.MAX_FILE_MB * 1024 ** 2) rejected.push(`${file.name} (over ${CONFIG.MAX_FILE_MB} MB)`);
      else accepted.push(file);
    }
    if (rejected.length) toast(`Skipped: ${rejected.join(", ")}`, 4500);
    return accepted;
  }

  /** Human summary of what was read, including pages that could not be. */
  function readSummary(doc) {
    const pages = doc.pages ? `${plural(doc.pages, "page")}, ` : "";
    return `${pages}${plural(doc.chunks, "section")} indexed on this computer`;
  }

  function unreadWarning(doc) {
    if (!doc.empty_pages) return "";
    return `${doc.empty_pages} of ${doc.pages} pages had no readable text (possibly scanned images) and ${doc.empty_pages === 1 ? "was" : "were"} skipped.`;
  }

  /**
   * Uploads one file into the active project. Resolves { doc, duplicate }.
   * A file already in the project is not an error: the existing copy is used.
   */
  async function uploadOne(projectId, file, { signal, onProgress } = {}) {
    try {
      const doc = await api.uploadDocument(projectId, file, {
        signal,
        onEvent: (evt) => { if (evt.type === "progress" && onProgress) onProgress(evt.done, evt.total); },
      });
      if (projectId === state.activeProjectId && !state.documents.some((d) => d.id === doc.id)) state.documents.unshift(doc);
      return { doc, duplicate: false };
    } catch (err) {
      if (err.code === "duplicate" && err.data?.document) return { doc: err.data.document, duplicate: true };
      throw err;
    }
  }

  /** Sources view: upload straight away, showing each file's progress as a row. */
  async function uploadToSources(fileList) {
    const project = activeProject();
    if (!project) { toast("Create a research project first."); return; }
    const files = checkFiles(fileList);
    const entries = files.map((file) => ({ id: uid(), file, name: file.name, status: "waiting", text: "Waiting…" }));
    state.uploads.push(...entries);
    renderSources();

    for (const entry of entries) {
      entry.status = "reading";
      entry.text = "Reading the file…";
      renderSources();
      try {
        const { doc, duplicate } = await uploadOne(project.id, entry.file, {
          onProgress: (done, total) => {
            entry.text = `Indexing ${done} of ${plural(total, "section")}…`;
            const row = el.sourcesRoot.querySelector(`[data-upload="${entry.id}"] .row__meta`);
            if (row) row.textContent = entry.text;
          },
        });
        state.uploads = state.uploads.filter((u) => u !== entry);
        toast(duplicate ? `${doc.name} is already in this project.` : `Ready: ${doc.name}`);
      } catch (err) {
        entry.status = "error";
        entry.text = (err.message || "Couldn't read this file.").replace(/`/g, "");
        if (["backend_offline", "ollama_offline", "embed_model_not_found"].includes(err.code)) checkHealth();
      }
      state.sources.passages = null;      // new text may match the project's questions
      state.combined.suggestions = null;
      renderSources();
    }
    refreshProjects();
  }

  async function deleteDocument(id) {
    const doc = state.documents.find((d) => d.id === id);
    if (!doc) return;
    const saved = state.findings.filter((f) => f.doc_id === id).length;
    const ok = await confirmDialog({
      title: `Delete "${doc.name}"?`,
      text: saved
        ? `Its ${plural(saved, "saved finding")} will be deleted too. The original file on your device is not touched.`
        : "Its indexed text is removed from this project. The original file on your device is not touched.",
    });
    if (!ok) return;
    try {
      await api.json(`/api/documents/${id}`, { method: "DELETE" });
    } catch (err) {
      toastError(err, "Couldn't delete the document.");
      return;
    }
    state.documents = state.documents.filter((d) => d.id !== id);
    state.findings = state.findings.filter((f) => f.doc_id !== id);
    if (state.sources.docId === id) state.sources.docId = null;
    state.combined.suggestions = null;
    renderSources();
    refreshProjects();
    toast("Document deleted");
  }

  function docMeta(doc) {
    const count = state.findings.filter((f) => f.doc_id === doc.id).length;
    return [
      doc.pages ? plural(doc.pages, "page") : null,
      plural(doc.chunks, "section"),
      formatBytes(doc.size),
      `Added ${relativeDay(doc.created_at * 1000).toLowerCase()}`,
      count ? plural(count, "finding") : null,
    ].filter(Boolean).join(" · ");
  }

  function noProjectHtml(title) {
    return `
      <header class="view__head"><div><h2>${title}</h2></div></header>
      <div class="empty">
        <strong>No research project yet</strong>
        <p>Create a project first. Documents and findings are kept inside the project they belong to.</p>
        <button type="button" class="btn btn--primary" data-act="new-project">New project</button>
      </div>`;
  }

  function renderSources() {
    const project = activeProject();
    if (!project) { el.sourcesRoot.innerHTML = noProjectHtml("Sources"); return; }
    const doc = state.documents.find((d) => d.id === state.sources.docId);
    if (doc) { renderSourceDetail(project, doc); return; }
    state.sources.docId = null;

    const uploads = state.uploads.map((u) => `
      <li class="row ${u.status === "error" ? "is-error" : "is-busy"}" data-upload="${u.id}">
        ${ICONS.file}
        <div class="row__main"><span class="row__title">${escapeHtml(u.name)}</span><span class="row__meta">${escapeHtml(u.text)}</span></div>
        ${u.status === "error" ? `<div class="row__actions"><button type="button" class="btn btn--small" data-act="dismiss-upload" data-id="${u.id}">Dismiss</button></div>` : ""}
      </li>`).join("");

    const docs = state.documents.map((d) => `
      <li class="row">
        ${ICONS.file}
        <div class="row__main">
          <button type="button" class="row__title" data-act="open-doc" data-id="${d.id}">${escapeHtml(d.name)}</button>
          <span class="row__meta">${docMeta(d)}</span>
          ${d.empty_pages ? `<span class="row__warn">${unreadWarning(d)}</span>` : ""}
        </div>
        <div class="row__actions">
          <button type="button" class="btn btn--small" data-act="open-doc" data-id="${d.id}">Open</button>
          <button type="button" class="btn btn--small btn--danger" data-act="delete-doc" data-id="${d.id}">Delete</button>
        </div>
      </li>`).join("");

    el.sourcesRoot.innerHTML = `
      <header class="view__head">
        <div><h2>Sources</h2><p>Documents in <strong>${escapeHtml(project.title)}</strong>. Files are read and indexed on this computer and never uploaded anywhere.</p></div>
        <button type="button" class="btn btn--primary" data-act="add-docs">Add documents</button>
      </header>
      ${uploads || docs ? `<ul class="rows">${uploads}${docs}</ul>` : `
        <div class="empty">
          <strong>No documents yet</strong>
          <p>Drop PDF, DOCX or TXT files anywhere on this page, or choose them from this device or a connected drive.</p>
          <button type="button" class="btn btn--primary" data-act="add-docs">Add documents</button>
        </div>`}`;
  }

  function renderSourceDetail(project, doc) {
    const s = state.sources;
    const saved = state.findings.filter((f) => f.doc_id === doc.id);
    const tab = (id, label) => `<button type="button" class="tab${s.tab === id ? " is-active" : ""}" role="tab" aria-selected="${s.tab === id}" data-act="source-tab" data-id="${id}">${label}</button>`;

    let panel = "";
    if (s.tab === "relevant") {
      let list;
      if (s.loading) list = '<p class="hint">Comparing this document with your topic and research questions…</p>';
      else if (s.error) list = `<p class="notice">${escapeHtml(s.error)}</p><div><button type="button" class="btn btn--small" data-act="retry-relevance">Try again</button></div>`;
      else if (s.passages && !s.passages.length) list = '<div class="empty"><strong>No passages reach this similarity</strong><p>Lower the minimum similarity, or add research questions to the project so Gihigugma knows what to look for.</p></div>';
      else if (s.passages) list = `<div class="passages">${s.passages.map((p) => passageHtml(p, { scope: "doc" })).join("")}</div>`;
      else list = "";
      panel = `
        <div class="filters">
          <label>Minimum similarity
            <input type="range" id="source-min-score" min="0.1" max="0.8" step="0.05" value="${s.minScore}" />
            <output id="source-min-score-value">${s.minScore.toFixed(2)}</output></label>
        </div>
        <p class="hint">Passages are matched against the project's topic and research questions. Similarity measures how close the wording is, not whether a passage is correct.</p>
        ${list}`;
    } else if (s.tab === "saved") {
      panel = saved.length
        ? `<div class="passages">${saved.map(findingHtml).join("")}</div>`
        : '<div class="empty"><strong>No findings saved from this document</strong><p>Use "Save finding" on a relevant passage or on the evidence under a chat answer.</p></div>';
    } else {
      if (s.loading) panel = '<p class="hint">Loading the extracted text…</p>';
      else if (s.error) panel = `<p class="notice">${escapeHtml(s.error)}</p>`;
      else if (s.pages) {
        panel = `
          <p class="hint">This is the text Gihigugma extracted from the file. Answers and findings can only come from what appears here.</p>
          ${doc.empty_pages ? `<p class="notice">${unreadWarning(doc)}</p>` : ""}
          ${s.pages.map((pg) => `
            <section class="pagetext" ${pg.page ? `id="page-${pg.page}"` : ""}>
              ${pg.page ? `<h4>Page ${pg.page}</h4>` : ""}
              <p>${highlightQuote(pg.text, s.highlight && (s.highlight.page ?? null) === (pg.page ?? null) ? s.highlight.quote : "")}</p>
            </section>`).join("")}`;
      }
    }

    el.sourcesRoot.innerHTML = `
      <header class="view__head">
        <div>
          <button type="button" class="link-btn" data-act="back-sources">← All sources</button>
          <h2>${escapeHtml(doc.name)}</h2>
          <p>${docMeta(doc)} · in ${escapeHtml(project.title)}</p>
        </div>
        <button type="button" class="btn btn--small btn--danger" data-act="delete-doc" data-id="${doc.id}">Delete</button>
      </header>
      <div class="tabs" role="tablist">
        ${tab("relevant", "Relevant passages")}${tab("saved", `Saved findings (${saved.length})`)}${tab("text", "Extracted text")}
      </div>
      ${panel}`;

    if (s.tab === "text" && s.highlight) {
      const target = el.sourcesRoot.querySelector("mark") || (s.highlight.page && el.sourcesRoot.querySelector(`#page-${s.highlight.page}`));
      if (target) requestAnimationFrame(() => target.scrollIntoView({ block: "center" }));
    }
  }

  /** Escaped page text with the start of a quoted passage marked, so the reader can find it. */
  function highlightQuote(text, quote) {
    if (!quote) return escapeHtml(text);
    const words = quote.split(/\s+/).filter(Boolean).slice(0, 14);
    const pattern = words.map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("\\s+");
    const match = pattern ? new RegExp(pattern).exec(text) : null;
    if (!match) return escapeHtml(text);
    return escapeHtml(text.slice(0, match.index)) + `<mark>${escapeHtml(match[0])}</mark>` + escapeHtml(text.slice(match.index + match[0].length));
  }

  /** Loads whatever the open document's current tab needs. */
  async function loadSourceTab() {
    const s = state.sources;
    const project = activeProject();
    const docId = s.docId;
    if (!project || !docId) return;
    const needsPassages = s.tab === "relevant" && !s.passages;
    const needsPages = s.tab === "text" && !s.pages;
    if (!needsPassages && !needsPages) { renderSources(); return; }

    s.loading = true;
    s.error = "";
    renderSources();
    try {
      if (needsPassages) {
        const params = new URLSearchParams({ doc_id: docId, min_score: s.minScore });
        const data = await api.json(`/api/projects/${project.id}/relevance?${params}`);
        if (s.docId === docId) s.passages = data.passages;
      } else {
        const data = await api.json(`/api/documents/${docId}`);
        if (s.docId === docId) s.pages = data.pages;
      }
    } catch (err) {
      s.error = (err.message || "Couldn't load this.").replace(/`/g, "");
      if (["backend_offline", "ollama_offline", "embed_model_not_found"].includes(err.code)) checkHealth();
    }
    s.loading = false;
    if (state.view === "sources" && s.docId === docId) renderSources();
  }

  /** Opens a document in the Sources view, optionally on its text with a quoted passage marked. */
  function openDocument(docId, { tab = "relevant", page = null, quote = "" } = {}) {
    if (!state.documents.some((d) => d.id === docId)) { toast("That document is no longer in this project."); return; }
    const s = state.sources;
    if (s.docId !== docId) Object.assign(s, { passages: null, pages: null });
    Object.assign(s, { docId, tab, error: "", highlight: quote ? { page, quote } : null });
    showView("sources");
  }

  /* ---------- 10. COMBINED RESEARCH VIEW ---------- */
  function renderCombined() {
    const project = activeProject();
    if (!project) { el.findingsRoot.innerHTML = noProjectHtml("Combined research"); return; }
    const c = state.combined;
    const docOptions = state.documents.map((d) =>
      `<option value="${d.id}"${c.docId === d.id ? " selected" : ""}>${escapeHtml(d.name)}</option>`).join("");
    const scores = [[0, "Any similarity"], [0.3, "0.30 and above"], [0.4, "0.40 and above"], [0.5, "0.50 and above"], [0.6, "0.60 and above"]];

    el.findingsRoot.innerHTML = `
      <header class="view__head">
        <div><h2>Combined research</h2>
          <p>Evidence from every document in <strong>${escapeHtml(project.title)}</strong>, grouped by research question. Each item keeps its source.</p></div>
        <button type="button" class="btn" data-act="scan-documents"${c.scanning || !state.documents.length ? " disabled" : ""}>${c.scanning ? "Searching…" : "Find evidence in all documents"}</button>
      </header>
      <div class="filters">
        <label class="visually-hidden" for="combined-search">Search findings</label>
        <input class="input" type="search" id="combined-search" placeholder="Search findings…" value="${escapeHtml(c.query)}" />
        <label class="visually-hidden" for="combined-doc">Filter by document</label>
        <select class="select" id="combined-doc"><option value="">All documents</option>${docOptions}</select>
        <label class="visually-hidden" for="combined-score">Filter by similarity</label>
        <select class="select" id="combined-score">${scores.map(([v, label]) => `<option value="${v}"${c.minScore === v ? " selected" : ""}>${label}</option>`).join("")}</select>
      </div>
      <div id="combined-groups" class="view__inner"></div>`;
    renderCombinedGroups();
  }

  /** The grouped list only, so typing in the search box doesn't lose focus. */
  function renderCombinedGroups() {
    const project = activeProject();
    const root = el.findingsRoot.querySelector("#combined-groups");
    if (!project || !root) return;
    const c = state.combined;
    const q = c.query.trim().toLowerCase();

    const keep = (item) => {
      const text = `${item.quote ?? item.text} ${item.doc_name ?? item.name ?? ""} ${item.ai_note ?? ""} ${item.user_note ?? ""} ${item.question ?? ""}`.toLowerCase();
      if (q && !text.includes(q)) return false;
      if (c.docId) {
        const inSources = (item.sources || []).some((s) => s.doc_id === c.docId);
        if (item.doc_id !== c.docId && !inSources) return false;
      }
      if (c.minScore && item.kind !== "answer" && !((item.score ?? 0) >= c.minScore)) return false;
      return true;
    };

    const findings = state.findings.filter(keep);
    // Suggestions are passages found by "Find evidence" that haven't been saved yet.
    const unsaved = (c.suggestions || []).filter((p) => !savedFinding(p.doc_id, p.index));
    // Each question lists its own best-matching passages, so one passage can appear under several.
    const suggestionsFor = (label, isQuestion) => unsaved
      .map((p) => {
        const m = isQuestion ? p.matches.find((x) => x.kind === "question" && x.label === label)
          : (p.match.kind === "topic" ? p.match : null);
        return m ? { ...p, score: m.score, match: null } : null;
      })
      .filter((p) => p && keep(p))
      .sort((a, b) => b.score - a.score)
      .slice(0, CONFIG.SUGGESTIONS_PER_QUESTION);

    const labels = [...project.questions, project.title];
    const groups = labels.map((label, i) => {
      const isQuestion = i < project.questions.length;
      return {
        label, isQuestion, scope: `scan${i}`,
        saved: findings.filter((f) => f.kind === "passage" && f.question === label),
        suggested: suggestionsFor(label, isQuestion),
      };
    });
    const other = findings.filter((f) => f.kind === "passage" && !labels.includes(f.question));
    const answers = findings.filter((f) => f.kind === "answer");
    const filtering = Boolean(q || c.docId || c.minScore);

    const section = (title, count, body, gap = "") => `
      <section class="group">
        <div class="group__head"><h3>${escapeHtml(title)}</h3><span class="badge">${count}</span></div>
        ${body ? `<div class="passages">${body}</div>` : gap}
      </section>`;

    let html = groups.map((g) => {
      if (filtering && !g.saved.length && !g.suggested.length) return "";
      const body = g.saved.map(findingHtml).join("") +
        g.suggested.map((p) => passageHtml(p, { scope: g.scope, question: g.label })).join("");
      const gap = g.isQuestion
        ? `<p class="group__gap">No evidence saved for this question yet.${c.suggestions ? " None of your documents has a passage that matches it closely." : ""}</p>`
        : '<p class="group__gap">No general evidence saved for the topic yet.</p>';
      return section(g.isQuestion ? g.label : `Topic: ${g.label}`, g.saved.length + g.suggested.length, body, gap);
    }).join("");
    if (other.length) html += section("Other saved evidence", other.length, other.map(findingHtml).join(""));
    if (answers.length) html += section("Saved AI answers", answers.length, answers.map(findingHtml).join(""));

    if (!html.trim()) {
      html = filtering
        ? '<div class="empty"><strong>Nothing matches these filters</strong><p>Clear the search or choose a different document.</p></div>'
        : '<div class="empty"><strong>No findings yet</strong><p>Add documents, then use "Find evidence in all documents" or save passages from the Sources view and from chat answers.</p></div>';
    }
    root.innerHTML = html;
  }

  /** Looks through every document for passages that match the topic and research questions. */
  async function scanDocuments() {
    const project = activeProject();
    const c = state.combined;
    if (!project || c.scanning) return;
    c.scanning = true;
    renderCombined();
    try {
      const data = await api.json(`/api/projects/${project.id}/relevance?limit=80`);
      c.suggestions = data.passages;
      const fresh = data.passages.filter((p) => !savedFinding(p.doc_id, p.index)).length;
      toast(fresh ? `${plural(fresh, "matching passage")} found. Save the ones worth keeping.` : "No new matching passages found.", 4500);
    } catch (err) {
      toastError(err, "Couldn't search the documents.");
      if (["backend_offline", "ollama_offline", "embed_model_not_found"].includes(err.code)) checkHealth();
    }
    c.scanning = false;
    if (state.view === "findings") renderCombined();
  }

  /* ---------- 10b. RESEARCH TOOLS ----------
     Extract, Summarize, Analyze, Suggest. Each runs on the selected documents
     through POST /api/tools/run and streams its result into a panel. */
  const TOOLS = {
    extract: {
      name: "Extract", action: "Extract information", done: "Extracted information",
      about: "Pull out key facts, definitions, arguments, dates, statistics and important points, each with its source.",
    },
    summarize: {
      name: "Summarize", action: "Summarize", done: "Summary",
      about: "A short, standard or detailed summary of whole documents, chosen pages, or only the part about a topic.",
    },
    analyze: {
      name: "Analyze", action: "Analyze", done: "Analysis",
      about: "Themes, arguments, evidence, assumptions, strengths, weaknesses and gaps, or a comparison between documents.",
    },
    suggest: {
      name: "Suggest", action: "Get suggestions", done: "Suggestions",
      about: "Research questions, study topics, follow-ups and improvements. Documents are optional for this tool.",
    },
  };
  const TOOL_CHOICES = {
    types: [["facts", "Key facts"], ["definitions", "Definitions"], ["arguments", "Arguments"], ["dates", "Dates"],
            ["statistics", "Statistics"], ["points", "Important points"]],
    aspects: [["themes", "Themes"], ["arguments", "Arguments"], ["evidence", "Evidence"], ["assumptions", "Assumptions"],
              ["strengths", "Strengths"], ["weaknesses", "Weaknesses"], ["gaps", "Knowledge gaps"]],
    kinds: [["questions", "Research questions"], ["topics", "Study topics"], ["followups", "Follow-up questions"],
            ["investigation", "Further investigation"], ["improvements", "Improvements"]],
    length: [["short", "Short (one paragraph)"], ["standard", "Standard"], ["detailed", "Detailed (by section)"]],
  };
  // What a reader must know about where each kind of result comes from.
  const TOOL_LEGEND = {
    extract: "Every item was taken from your documents; the number after it points to the passage it came from.",
    summarize: "Written by the AI from your documents only. Numbers in brackets point to the passages used.",
    analyze: "Points with a number in brackets are stated in your documents. Parts labelled Interpretation are the AI's own reading, not facts from the source.",
    suggest: "These are AI-generated suggestions to consider, not statements from your documents. A number in brackets shows the passage that prompted a suggestion.",
  };

  /** Documents ticked in the tools form (all of them until the user changes the selection). */
  function toolDocIds() {
    const have = state.documents.map((d) => d.id);
    return state.tools.docIds ? state.tools.docIds.filter((id) => have.includes(id)) : have;
  }

  /** Copies the form's current values into state, so they survive a re-render. */
  function readToolForm() {
    const root = el.toolsRoot;
    const t = state.tools;
    if (!root.querySelector(".toolform")) return;
    t.docIds = [...root.querySelectorAll("input[data-doc]:checked")].map((b) => b.value);
    const form = t.form[t.tool];
    for (const key of ["types", "aspects", "kinds"]) {
      if (key in form) form[key] = [...root.querySelectorAll(`input[data-choice="${key}"]:checked`)].map((b) => b.value);
    }
    for (const key of ["focus", "pages", "topic", "draft", "length"]) {
      const field = root.querySelector(`[data-field="${key}"]`);
      if (field && key in form) form[key] = field.value;
    }
    if ("compare" in form) form.compare = Boolean(root.querySelector('[data-field="compare"]')?.checked);
  }

  function renderTools() {
    const project = activeProject();
    if (!project) { el.toolsRoot.innerHTML = noProjectHtml("Research tools"); return; }
    const t = state.tools;
    const tool = TOOLS[t.tool];
    const form = t.form[t.tool];
    const picked = toolDocIds();
    const text = (key, label, placeholder) => `
      <label class="field"><span>${label}</span>
        <input class="input" data-field="${key}" value="${escapeHtml(form[key])}" placeholder="${placeholder}" /></label>`;
    const choices = (key, legend) => `
      <fieldset><legend>${legend}</legend><div class="checks">${TOOL_CHOICES[key].map(([value, label]) =>
        `<label class="check"><input type="checkbox" data-choice="${key}" value="${value}"${form[key].includes(value) ? " checked" : ""} />${label}</label>`).join("")}</div></fieldset>`;
    const pages = text("pages", "Pages <em>optional, for documents with page numbers</em>", "e.g. 3-7 or 2, 5, 9");

    let fields = "";
    if (t.tool === "extract") {
      fields = choices("types", "What to extract") +
        `<div class="toolform__row">${text("focus", "Looking for something specific? <em>optional</em>", "e.g. costs of each proposed measure")}${pages}</div>`;
    } else if (t.tool === "summarize") {
      fields = `<div class="toolform__row">
          <label class="field"><span>Length</span><select class="select" data-field="length">${TOOL_CHOICES.length.map(([value, label]) =>
            `<option value="${value}"${form.length === value ? " selected" : ""}>${label}</option>`).join("")}</select></label>
          ${text("topic", "Only the part about <em>optional</em>", "e.g. early warning systems")}${pages}</div>`;
    } else if (t.tool === "analyze") {
      const canCompare = picked.length >= 2;
      fields = choices("aspects", "What to look for") + `
        <label class="check"><input type="checkbox" data-field="compare"${form.compare && canCompare ? " checked" : ""}${canCompare ? "" : " disabled"} />
          Compare the selected documents with each other${canCompare ? "" : " (select two or more)"}</label>
        <div class="toolform__row">${text("focus", "Focus on <em>optional</em>", "e.g. how well the evidence supports the conclusions")}${pages}</div>`;
    } else {
      fields = choices("kinds", "What to suggest") + `
        <label class="field"><span>Your own argument, outline or approach to improve <em>optional</em></span>
          <textarea class="input" rows="3" data-field="draft" placeholder="Paste a draft here and tick Improvements to get feedback on it">${escapeHtml(form.draft)}</textarea></label>`;
    }

    const docs = state.documents.length
      ? `<div class="checks checks--stack">${state.documents.map((d) => `
          <label class="check"><input type="checkbox" data-doc value="${d.id}"${picked.includes(d.id) ? " checked" : ""} />
            <span class="check__name">${escapeHtml(d.name)}</span>
            <span class="row__meta">${d.pages ? `${plural(d.pages, "page")} · ` : ""}${plural(d.chunks, "section")}</span></label>`).join("")}</div>`
      : '<p class="hint">This project has no documents yet. Add some in Sources.</p>';

    el.toolsRoot.innerHTML = `
      <header class="view__head">
        <div><h2>Research tools</h2>
          <p>Work on the documents in <strong>${escapeHtml(project.title)}</strong> with the local model. Nothing leaves this computer.</p></div>
      </header>
      <div class="tabs" role="tablist">${Object.entries(TOOLS).map(([id, info]) =>
        `<button type="button" class="tab${id === t.tool ? " is-active" : ""}" role="tab" aria-selected="${id === t.tool}" data-act="pick-tool" data-id="${id}"${t.running ? " disabled" : ""}>${info.name}</button>`).join("")}</div>
      <p class="hint">${tool.about}</p>
      <div class="toolform">
        <fieldset><legend>Documents${state.documents.length > 1 ? '<button type="button" class="link-btn" data-act="tool-all-docs">Select all / none</button>' : ""}</legend>${docs}</fieldset>
        ${fields}
        <div class="toolform__run">
          <button type="button" class="btn btn--primary" data-act="run-tool"${t.running ? " disabled" : ""}>${tool.action}</button>
          <button type="button" class="btn" data-act="cancel-tool"${t.running ? "" : " hidden"}>Cancel</button>
          <div class="progress" id="tool-progress" role="status" aria-live="polite"></div>
        </div>
      </div>
      <div id="tool-result"></div>`;
    renderToolProgress();
    renderToolResult();
  }

  function renderToolProgress() {
    const box = el.toolsRoot.querySelector("#tool-progress");
    if (!box) return;
    const t = state.tools;
    if (!t.running) { box.innerHTML = ""; return; }
    const p = t.progress;
    const label = p ? `${p.label}… (step ${p.step} of ${p.total})` : `Starting ${state.selectedModel || "the model"}…`;
    const width = p ? Math.round(((p.step - 0.5) / p.total) * 100) : 3;
    box.innerHTML = `<span>${escapeHtml(label)}</span><div class="progress__bar"><i style="width:${width}%"></i></div>`;
  }

  /** The passages a result actually cites, with the same Save / Open source actions as chat evidence. */
  function toolEvidenceHtml(r) {
    const cited = r.evidence.filter((p) => r.cited.includes(p.n));
    if (!cited.length) return "";
    return `
      <details class="evidence">
        <summary>Sources cited · ${plural(cited.length, "passage")} from your documents</summary>
        <p class="hint">The original text behind each number in the result.</p>
        <div class="passages">${cited.map((p) => passageHtml(p, { scope: "tool", question: r.label, number: p.n })).join("")}</div>
      </details>`;
  }

  function renderToolResult() {
    const box = el.toolsRoot.querySelector("#tool-result");
    if (!box) return;
    const r = state.tools.result;
    if (!r) { box.innerHTML = ""; return; }
    if (r.error) {
      box.innerHTML = `<p class="notice">${escapeHtml(r.error)}</p>`;
      return;
    }
    const wasOpen = box.querySelector("details")?.open;
    const remarks = r.remarks.map((x) => `<p class="hint">${escapeHtml(x)}</p>`).join("");
    box.innerHTML = `
      <article class="result">
        <div class="result__head"><h3>${escapeHtml(r.label)}</h3><span class="msg__stats">${escapeHtml([r.model, statsText(r.stats)].filter(Boolean).join(" · "))}</span></div>
        ${remarks}
        <p class="hint">${TOOL_LEGEND[r.tool]}</p>
        <div class="msg__content${r.done ? "" : " is-writing"}" id="tool-output">${renderMarkdown(r.content) || '<p class="hint">Working…</p>'}</div>
        ${r.notice ? `<p class="notice">${escapeHtml(r.notice)}</p>` : ""}
        ${r.done ? toolEvidenceHtml(r) : ""}
        ${r.done && r.content ? `<div class="view__actions">
          <button type="button" class="btn btn--small" data-act="tool-copy">Copy</button>
          <button type="button" class="btn btn--small" data-act="tool-chat">Ask about this in chat</button>
          <button type="button" class="btn btn--small" data-act="tool-save"${r.saved ? " disabled" : ""}>${r.saved ? "Saved to findings" : "Save to findings"}</button>
        </div>` : ""}
      </article>`;
    if (wasOpen) box.querySelector("details").open = true;
  }

  /** A short title for a run, e.g. "Summary (detailed) · Flood Study.pdf". */
  function toolLabel(tool, options, docs) {
    let title = TOOLS[tool].done;
    if (tool === "summarize") title += ` (${options.length})`;
    if (tool === "analyze" && options.compare) title = "Comparison";
    const names = docs.length > 2 ? `${docs.length} documents` : docs.map((d) => d.name).join(" and ");
    return names ? `${title} · ${names}` : title;
  }

  /** Warnings about how far a result can be relied on. */
  function toolNotice(r, evt) {
    const notes = [];
    if (evt.stats && evt.stats.done_reason === "length") notes.push("The result reached the length limit and may be cut off.");
    if (evt.coverage && evt.coverage.read < evt.coverage.total) {
      notes.push(`This selection is long: ${evt.coverage.read} of ${evt.coverage.total} sections were read, spread evenly from start to end. Choose pages or fewer documents to read everything.`);
    }
    if (r.invalid.length) notes.push(`It cites sources that don't exist (${r.invalid.map((n) => `[${n}]`).join(", ")}). Treat those points as unsupported.`);
    else if (evt.grounding === "documents" && !r.cited.length && r.tool !== "suggest") {
      notes.push("The result has no source numbers, so its points can't be traced to a passage. Check it against the documents.");
    }
    if (evt.grounding === "none") notes.push("No documents were selected, so these suggestions are general and not based on your sources.");
    return notes.join(" ");
  }

  async function runTool() {
    const t = state.tools;
    const project = activeProject();
    if (!project || t.running) return;
    readToolForm();
    const tool = t.tool;
    const options = { ...t.form[tool] };
    const docs = state.documents.filter((d) => toolDocIds().includes(d.id));
    if (tool !== "suggest" && !docs.length) { toast("Select at least one document."); return; }
    if (tool === "analyze" && docs.length < 2) options.compare = false;
    if (["types", "aspects", "kinds"].some((key) => key in options && !options[key].length)) {
      toast("Tick at least one option."); return;
    }

    const r = { tool, label: toolLabel(tool, options, docs), content: "", evidence: [], cited: [], invalid: [],
                remarks: [], notice: "", model: state.selectedModel, stats: null, grounding: "", done: false };
    Object.assign(t, { running: true, abortCtrl: new AbortController(), progress: null, result: r });
    renderTools();

    let frameQueued = false;
    const queueRender = () => {
      if (frameQueued) return;
      frameQueued = true;
      requestAnimationFrame(() => {
        frameQueued = false;
        const out = el.toolsRoot.querySelector("#tool-output");
        if (out && t.result === r) out.innerHTML = renderMarkdown(r.content);
      });
    };

    try {
      const res = await api.request("/api/tools/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tool, project_id: project.id, document_ids: docs.map((d) => d.id), model: state.selectedModel, options }),
        signal: t.abortCtrl.signal,
      });
      await api.readStream(res, (evt) => {
        if (evt.type === "start") { r.model = evt.model; r.remarks = evt.remarks || []; renderToolResult(); }
        else if (evt.type === "evidence") r.evidence = evt.evidence;
        else if (evt.type === "progress") { t.progress = evt; renderToolProgress(); }
        else if (evt.type === "token") { r.content += evt.content; queueRender(); }
        else if (evt.type === "done") {
          r.content = r.content.replace(/(\[\d+\])(?:\s*\1)+/g, "$1");
          r.stats = evt.stats;
          r.grounding = evt.grounding;
          r.cited = evt.citations ? evt.citations.used : [];
          r.invalid = evt.citations ? evt.citations.invalid : [];
          r.notice = toolNotice(r, evt);
        }
        else if (evt.type === "error") throw new ApiError(evt.code, evt.message);
      }, t.abortCtrl.signal);
      if (!r.content.trim()) throw new ApiError("empty_reply", "The model returned nothing. Try again, or pick another model.");
    } catch (err) {
      const e = err instanceof ApiError ? err : new ApiError("unknown", "Something went wrong while running the tool.");
      if (e.code === "aborted") {
        // File headings are written before any reading starts; on their own they aren't a result.
        if (r.content.replace(/^#+ .*$/gm, "").trim()) r.notice = "Cancelled. This result is incomplete.";
        else t.result = null;
      } else if (r.content.trim()) {
        r.notice = `Stopped early: ${e.message.replace(/`/g, "")} This result is incomplete.`;
      } else {
        t.result = { error: e.message.replace(/`/g, "") };
      }
      if (["backend_offline", "ollama_offline", "model_not_found", "no_models", "embed_model_not_found"].includes(e.code)) checkHealth();
    } finally {
      r.done = true;
      Object.assign(t, { running: false, abortCtrl: null, progress: null });
      if (state.view === "tools") renderTools();
    }
  }

  async function copyToolResult() {
    const copied = await copyToClipboard(state.tools.result.content);
    toast(copied ? "Copied to clipboard" : "Couldn't copy. Select the text and copy it by hand.");
  }

  /** Puts a result into a new chat, with its cited passages, so follow-up questions have it as context. */
  function sendResultToChat() {
    const r = state.tools.result;
    if (!r || !r.content) return;
    newChat({ focus: false, show: false });
    const chat = activeChat();
    chat.projectId ||= state.activeProjectId;
    chat.title = r.label.replace(/\s+/g, " ").slice(0, 60);
    chat.updatedAt = Date.now();
    chat.messages.push(
      { role: "user", content: `Research tool: ${r.label}`, time: Date.now() },
      { role: "assistant", content: r.content, model: r.model, time: Date.now(), question: r.label, fromTool: true,
        grounding: r.grounding, evidence: r.evidence.filter((p) => r.cited.includes(p.n)), cited: r.cited,
        notice: r.notice, stats: r.stats },
    );
    saveChat(chat).then(refreshProjects);
    el.contextSelect.value = "project";
    showView("chat");
    el.input.focus();
    toast("The result is in this chat. Ask a follow-up question about it.", 4500);
  }

  async function saveToolResult(button) {
    const r = state.tools.result;
    const project = activeProject();
    if (!r || !project || r.saved) return;
    button.disabled = true;
    try {
      const { finding } = await api.json(`/api/projects/${project.id}/findings`, {
        method: "POST",
        body: {
          kind: "answer", question: r.label, text: r.content,
          sources: r.evidence.filter((p) => r.cited.includes(p.n)).map(({ n, doc_id, name, page, index }) => ({ n, doc_id, name, page, index })),
        },
      });
      state.findings.unshift(finding);
      r.saved = true;
      toast("Saved to findings");
      refreshProjects();
    } catch (err) {
      toastError(err, "Couldn't save the result.");
    }
    renderToolResult();
  }

  /* ---------- 11. COMPOSER & ATTACHMENTS ---------- */
  function updateComposer() {
    const text = el.input.value;
    const words = countWords(text);
    const chars = text.length;
    el.wordCount.textContent = `${words.toLocaleString()} word${words === 1 ? "" : "s"} · ${chars.toLocaleString()} character${chars === 1 ? "" : "s"}`;

    // While a reply streams, the send button becomes a Stop button.
    if (state.sending) {
      el.send.disabled = false;
      el.send.classList.add("is-stop");
      el.send.innerHTML = ICONS.stop;
      el.send.setAttribute("aria-label", "Stop generating");
      el.send.title = "Stop generating";
    } else {
      el.send.classList.remove("is-stop");
      el.send.innerHTML = ICONS.send;
      el.send.setAttribute("aria-label", "Send message");
      el.send.title = "Send (Enter)";
      el.send.disabled = !text.trim() && state.pendingFiles.length === 0;
    }

    // Auto-grow textarea
    el.input.style.height = "auto";
    el.input.style.height = Math.min(el.input.scrollHeight, 240) + "px";
  }

  function renderAttachments() {
    el.attachments.innerHTML = "";
    state.pendingFiles.forEach((file, i) => {
      const li = document.createElement("li");
      li.className = "attachment";
      li.innerHTML = `${ICONS.file}
        <span class="attachment__name" title="${escapeHtml(file.name)}">${escapeHtml(file.name)}</span>
        <span class="attachment__size">${formatBytes(file.size)}</span>
        <button type="button" class="attachment__remove" aria-label="Remove ${escapeHtml(file.name)}">${ICONS.x}</button>`;
      li.querySelector("button").addEventListener("click", () => {
        state.pendingFiles.splice(i, 1);
        renderAttachments();
        updateComposer();
      });
      el.attachments.appendChild(li);
    });

    if (state.pendingFiles.length && el.contextSelect.value === "none") el.contextSelect.value = "project";
  }

  function addFiles(fileList) {
    for (const file of checkFiles(fileList)) {
      if (state.pendingFiles.some((f) => f.name === file.name && f.size === file.size)) continue;
      state.pendingFiles.push(file);
    }
    renderAttachments();
    updateComposer();
  }

  function ensureActiveChat() {
    let chat = activeChat();
    if (!chat) {
      chat = { id: uid(), projectId: state.activeProjectId, title: "New chat", messages: [], updatedAt: Date.now() };
      state.chats.push(chat);
      state.activeChatId = chat.id;
    }
    return chat;
  }

  /* ---------- 12. SENDING & STREAMING ---------- */

  /**
   * The conversation as the model should see it: real user/assistant turns only.
   * Exchanges that didn't come from the documents are left out: "nothing found"
   * replies always, and general-knowledge replies whenever a document-only answer
   * is requested, so nothing from outside the documents is carried into it.
   */
  function historyFor(chat, context) {
    const turns = [];
    for (const m of chat.messages) {
      if (!(m.role === "user" || m.role === "assistant") || m.pending || !m.content || m.placeholder) continue;
      const outside = m.role === "assistant" &&
        (m.grounding === "no_evidence" || (context === "project" && m.grounding === "none"));
      if (outside) {
        if (turns.at(-1)?.role === "user") turns.pop();     // and the question that led to it
        continue;
      }
      // A question that never got a reply (stopped, failed) is replaced by the next one.
      if (m.role === "user" && turns.at(-1)?.role === "user") turns.pop();
      turns.push({ role: m.role, content: m.content });
    }
    return turns;
  }

  async function handleSend(e) {
    e?.preventDefault();
    if (state.sending) { stopGenerating(); return; }

    const text = el.input.value.trim();
    const files = state.pendingFiles;
    if (!text && !files.length) {
      toast("Type a message first.");
      el.input.focus();
      return;
    }
    if (!activeProject()) {
      toast("Create a research project first. Documents and chats are kept inside a project.", 4500);
      showView("projects");
      return;
    }

    const chat = ensureActiveChat();
    chat.projectId ||= state.activeProjectId;
    const userMsg = {
      role: "user",
      content: text || (files.length === 1 ? "Added a document to the project." : `Added ${files.length} documents to the project.`),
      attachments: files.map((f) => ({ name: f.name, size: f.size })),
      time: Date.now(),
      placeholder: !text,        // files only: not a question, so it is never sent to the model
    };
    if (chat.title === "New chat") chat.title = (text || files[0].name).replace(/\s+/g, " ").slice(0, 60);
    chat.updatedAt = Date.now();

    el.input.value = "";
    state.pendingFiles = [];
    renderAttachments();
    if (chat.messages.length === 0) renderChat();
    renderTitle();
    appendMessage(chat, userMsg);
    renderHistory();

    if (files.length) {
      const added = await indexFiles(chat, files);
      // From now on this chat is about the files just attached, not older ones in the project,
      // unless a question names another file or asks to compare.
      if (added.length) chat.focusDocIds = userMsg.focusDocIds = added;
      // No question, or nothing could be read: there is nothing to answer.
      if (!text || !added.length) { saveChat(chat); return; }
    }

    await generateReply(chat);
  }

  /**
   * Reads and indexes attached files into the chat's project (locally).
   * Returns the ids of the documents that are ready (none if nothing was usable or the user stopped).
   */
  async function indexFiles(chat, files) {
    state.sending = true;
    state.abortCtrl = new AbortController();
    updateComposer();
    renderStatus();

    const indexed = [];
    try {
      for (const file of files) {
        const note = { role: "system", variant: "info", time: Date.now(), content: `Reading **${file.name}**…` };
        appendMessage(chat, note);
        try {
          const { doc, duplicate } = await uploadOne(chat.projectId, file, {
            signal: state.abortCtrl.signal,
            onProgress: (done, total) => {
              note.content = `Reading **${file.name}**… indexed ${done} of ${plural(total, "section")}`;
              updateMessageNode(note);
            },
          });
          indexed.push(doc.id);
          note.content = duplicate
            ? `**${file.name}** is already in this project as **${doc.name}**, so the existing copy is used.`
            : `Ready: **${file.name}** (${readSummary(doc)}).${doc.empty_pages ? ` Note: ${unreadWarning(doc)}` : ""}`;
        } catch (err) {
          if (err.code === "aborted") { removeMessage(chat, note); return []; }
          note.variant = "error";
          note.content = `Couldn't read **${file.name}**: ${err.message}`;
          if (["backend_offline", "ollama_offline", "embed_model_not_found"].includes(err.code)) checkHealth();
        }
        updateMessageNode(note);
        scrollToBottom();
      }
    } finally {
      state.sending = false;
      state.abortCtrl = null;
      updateComposer();
      renderStatus();
      if (indexed.length) {
        state.sources.passages = null;
        state.combined.suggestions = null;
        refreshProjects();
      }
    }
    return indexed;
  }

  /** Warnings shown under a reply, depending on what it was based on. */
  function groundingNotice(msg) {
    if (msg.grounding === "none") return "General-knowledge answer: not based on your documents.";
    if (msg.grounding !== "documents" || !msg.cited) return "";
    if (msg.invalidCitations?.length) {
      return `This reply cites sources that were not provided (${msg.invalidCitations.map((n) => `[${n}]`).join(", ")}). Treat those statements as unsupported.`;
    }
    if (msg.coverage && msg.coverage.read < msg.coverage.total) {
      return `Based on ${msg.coverage.read} of ${plural(msg.coverage.total, "section")}, spread across the documents, so some details are not covered. Ask about a specific subject to search everything.`;
    }
    if (!msg.cited.length && !msg.content.includes(NOT_IN_DOCUMENTS)) {
      return "This reply doesn't cite a passage. Check it against the source evidence below before relying on it.";
    }
    return "";
  }

  async function generateReply(chat) {
    const model = state.selectedModel;
    const context = el.contextSelect.value;
    const asked = chat.messages.filter((m) => m.role === "user" && !m.placeholder).at(-1);
    const assistantMsg = {
      role: "assistant", content: "", model, time: Date.now(), question: asked?.content || "",
      pending: true, pendingLabel: context === "project" ? "Searching your documents…" : "Thinking…",
      local: true,       // only on this page until the host has saved the finished reply
    };
    appendMessage(chat, assistantMsg);
    let question = null;   // the new question, when it is sent with this request

    state.sending = true;
    state.abortCtrl = new AbortController();
    updateComposer();
    renderStatus();

    const slowTimer = setTimeout(() => {
      if (assistantMsg.pending) {
        assistantMsg.pendingLabel = `Loading ${model || "the model"} into memory — the first reply can take a minute…`;
        updateMessageNode(assistantMsg);
      }
    }, CONFIG.SLOW_START_MS);

    let frameQueued = false;
    const queueRender = () => {
      if (frameQueued) return;
      frameQueued = true;
      requestAnimationFrame(() => {
        frameQueued = false;
        updateMessageNode(assistantMsg);
        if (chat.id === state.activeChatId) scrollToBottom();
      });
    };

    try {
      // What this chat shows that the host doesn't have yet. A lone new question travels with the chat
      // request, which stores it. Notes about attached files are stored first, so the order is kept,
      // and the request then answers the last stored question (as Retry does).
      const unsaved = chat.messages.filter((m) => !m.id && !m.local && m !== assistantMsg);
      question = unsaved.length === 1 && unsaved[0].role === "user" && !unsaved[0].placeholder ? unsaved[0] : null;
      if (question) question.sending = true;
      if (!question || Boolean(chat.memoryExcluded) !== Boolean(chat.savedMemoryExcluded)) {
        await saveChat(chat, { strict: true });
      }

      await api.chatStream({
        // The host reads the conversation's history from its own database; only the new question is sent.
        body: {
          conversation_id: chat.id, project_id: chat.projectId, model, context,
          message: question ? question.content : undefined,
          meta: question ? messageMeta(question) : undefined,
          focus_document_ids: chat.focusDocIds || [],
        },
        signal: state.abortCtrl.signal,
        onEvent(evt) {
          if (evt.type === "start") {
            assistantMsg.model = evt.model;
            if (question) question.id = evt.user_message_id;
            chat.saved = true;
            queueRender();
          } else if (evt.type === "memories") {
            assistantMsg.memories = evt.memories;
            queueRender();
          } else if (evt.type === "evidence") {
            assistantMsg.evidence = evt.evidence;
            assistantMsg.pendingLabel = `Reading ${plural(evt.evidence.length, "passage")} from your documents…`;
            queueRender();
          } else if (evt.type === "token") {
            assistantMsg.pending = false;
            assistantMsg.streaming = true;
            assistantMsg.content += evt.content;
            queueRender();
          } else if (evt.type === "done") {
            // The host has saved the finished reply; show exactly what it saved.
            if (typeof evt.content === "string") assistantMsg.content = evt.content;
            if (evt.message_id) { assistantMsg.id = evt.message_id; assistantMsg.local = false; }
            assistantMsg.stats = evt.stats;
            assistantMsg.grounding = evt.grounding;
            assistantMsg.coverage = evt.coverage || null;
            if (evt.citations) {
              assistantMsg.cited = evt.citations.used;
              assistantMsg.invalidCitations = evt.citations.invalid;
            }
            assistantMsg.notice = evt.stats && evt.stats.done_reason === "length"
              ? "The reply hit the model's length limit and may be cut off."
              : groundingNotice(assistantMsg);
          } else if (evt.type === "error") {
            throw new ApiError(evt.code, evt.message);
          }
        },
      });

      if (!assistantMsg.content.trim()) {
        throw new ApiError("empty_reply", "The model returned an empty reply. Try rephrasing, or pick another model.");
      }
    } catch (err) {
      const e = err instanceof ApiError ? err : new ApiError("unknown", "Something went wrong while generating the reply.");
      // The host may have stored the question before the model failed; then Retry must not store it again.
      if (question && e.data?.user_message_id) { question.id = e.data.user_message_id; chat.saved = true; }
      if (e.code === "aborted") {
        if (assistantMsg.content.trim()) assistantMsg.notice = "Stopped. This partial reply is not saved.";
        else removeMessage(chat, assistantMsg);
      } else if (assistantMsg.content.trim()) {
        // Keep what arrived on screen; flag it as incomplete. The host does not save incomplete replies.
        assistantMsg.notice = `Reply incomplete: ${e.message.replace(/`/g, "")}`;
        assistantMsg.retryable = true;
      } else {
        removeMessage(chat, assistantMsg);
        appendMessage(chat, { role: "system", variant: "error", content: e.message, time: Date.now(), retryable: true, local: true });
      }
      // An error may mean the connection changed; refresh status right away.
      if (["backend_offline", "ollama_offline", "model_not_found", "no_models", "stream_interrupted", "embed_model_not_found"].includes(e.code)) {
        checkHealth();
      }
    } finally {
      clearTimeout(slowTimer);
      assistantMsg.pending = false;
      assistantMsg.streaming = false;
      updateMessageNode(assistantMsg);
      state.sending = false;
      state.abortCtrl = null;
      chat.updatedAt = Date.now();
      renderHistory();
      renderStatus();
      updateComposer();
      if (chat.id === state.activeChatId && state.view === "chat") el.input.focus();
      if (question) delete question.sending;
      refreshProjects().then(() => suggestMemories(chat, asked));
    }
  }

  /* Memory in chat: suggestions are offered, never saved silently. */

  // Phrases that suggest the user is telling the assistant something lasting about themselves.
  const WORTH_REMEMBERING = /\b(remember|from now on|always|never|i prefer|i like|i want you to|please use|call me|my name is|i am|i'm|i work|i study|my research|my thesis|my project|my topic|my field|we define|refers to|stands for)\b/i;

  /** After a reply, asks the local model whether the user's message holds something worth remembering. */
  async function suggestMemories(chat, userMsg) {
    const s = state.settings;
    if (!s.memory_enabled || !s.memory_suggestions) return;
    if (chat.memoryExcluded || !userMsg || !WORTH_REMEMBERING.test(userMsg.content)) return;
    try {
      const { suggestions } = await api.json("/api/memories/suggest", {
        method: "POST",
        body: { text: userMsg.content, chat_id: chat.id, project_id: chat.projectId, model: state.selectedModel },
      });
      for (const memory of suggestions) {
        appendMessage(chat, {
          role: "system", variant: "info", time: Date.now(), memoryId: memory.id,
          content: `Remember this for future chats?\n\n**${memory.content}**`,
        });
      }
      if (suggestions.length) saveChat(chat);
    } catch {
      /* A missed suggestion is harmless; the user can always add a memory by hand. */
    }
  }

  async function answerSuggestion(msg, approve) {
    try {
      if (approve) await api.json(`/api/memories/${msg.memoryId}`, { method: "PATCH", body: { approve: true } });
      else await api.json(`/api/memories/${msg.memoryId}`, { method: "DELETE" });
    } catch (err) {
      if (err.status !== 404) { toastError(err, "Couldn't update memory."); return; }
    }
    msg.memoryDone = true;
    msg.content = msg.content.replace("Remember this for future chats?", approve ? "Saved to memory:" : "Not saved:");
    updateMessageNode(msg);
    patchMessage(msg, { memoryDone: true }, msg.content);
  }

  /** The switch that keeps memory out of one chat. Shown only when memory is turned on. */
  function renderMemoryToggle() {
    const chat = activeChat();
    const show = state.settings.memory_enabled && state.view === "chat";
    el.memoryToggle.hidden = !show;
    if (!show) return;
    const off = Boolean(chat && chat.memoryExcluded);
    el.memoryToggle.textContent = off ? "Memory off for this chat" : "Memory on";
    el.memoryToggle.classList.toggle("is-off", off);
    el.memoryToggle.setAttribute("aria-pressed", String(!off));
  }

  function toggleChatMemory() {
    const chat = ensureActiveChat();
    chat.memoryExcluded = !chat.memoryExcluded;
    renderMemoryToggle();
    toast(chat.memoryExcluded ? "Memories won't be used or suggested in this chat." : "Memories can be used in this chat.");
    saveChat(chat);
  }

  function stopGenerating() {
    if (state.abortCtrl) state.abortCtrl.abort();
  }

  /** Retry: drop the failed/incomplete reply and ask again with the same history. */
  async function retryLast(failedMsg) {
    if (state.sending) return;
    const chat = state.chats.find((c) => c.messages.includes(failedMsg));
    if (!chat) return;
    removeMessage(chat, failedMsg);
    const history = historyFor(chat, el.contextSelect.value);
    if (!history.length || history.at(-1).role !== "user") {
      toast("Nothing to retry — send a new message.");
      return;
    }
    await checkHealth();
    await generateReply(chat);
  }

  /* ---------- 13. NAVIGATION, SIDEBAR, EVENTS ---------- */
  const isMobile = () => window.matchMedia("(max-width: 860px)").matches;
  const VIEW_TITLES = {
    projects: "Research projects", sources: "Sources", findings: "Combined research", tools: "Research tools",
    paraphrase: "Paraphrase", counter: "Word counter", memory: "Memory", collab: "Collaboration",
  };
  // Views and button actions defined in modules.js register themselves here.
  const extraViews = {};
  const extraActions = {};

  function setSidebar(open) {
    el.app.classList.toggle("sidebar-open", open);
    el.scrim.hidden = !open;
    el.sidebarOpen.setAttribute("aria-expanded", String(open));
    if (open) el.newChat.focus();
  }

  function renderTitle() {
    if (state.view !== "chat") { el.chatTitle.textContent = VIEW_TITLES[state.view]; return; }
    const chat = activeChat();
    el.chatTitle.textContent = chat ? chat.title : "New chat";
  }

  /** Switches the main area between the chat and the research views. */
  function showView(view) {
    state.view = view;
    el.chat.hidden = view !== "chat";
    el.composer.hidden = view !== "chat";
    for (const [name, node] of Object.entries(el.views)) node.hidden = name !== view;
    document.querySelectorAll(".nav__item[data-view]").forEach((b) => {
      const active = b.dataset.view === view;
      b.classList.toggle("is-active", active);
      if (active) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
    });
    renderTitle();

    if (view === "chat") { renderChat(); renderHistory(); }
    else if (view === "projects") renderProjects();
    else if (view === "sources") { renderSources(); if (state.sources.docId) loadSourceTab(); }
    else if (view === "findings") renderCombined();
    else if (view === "tools") renderTools();
    else if (extraViews[view]) extraViews[view]();
    renderMemoryToggle();
    if (isMobile()) setSidebar(false);
  }

  /** Opens a chat; `seq` scrolls to one message in it (from a search result). */
  function openChat(id, seq = null) {
    state.activeChatId = id;
    showView("chat");
    const chat = activeChat();
    const target = seq !== null && chat ? chat.messages[seq]?.node : null;
    if (target) {
      requestAnimationFrame(() => {
        target.scrollIntoView({ block: "center" });
        target.classList.add("is-found");
        setTimeout(() => target.classList.remove("is-found"), 2400);
      });
    }
  }

  function newChat({ focus = true, show = true } = {}) {
    // Reuse an existing empty chat rather than piling up blanks
    const empty = state.chats.find((c) => c.messages.length === 0);
    const chat = empty || { id: uid(), projectId: state.activeProjectId, title: "New chat", messages: [], updatedAt: Date.now() };
    if (!empty) state.chats.push(chat);
    state.activeChatId = chat.id;
    if (show) showView("chat");
    if (focus) el.input.focus();
  }

  /** Buttons inside rendered views and messages, identified by data-act. */
  function handleAction(e) {
    const btn = e.target.closest("[data-act]");
    if (!btn || btn.disabled) return;
    const { act, id, key } = btn.dataset;
    const s = state.sources;

    if (act === "new-project") openProjectDialog();
    else if (act === "edit-project") openProjectDialog(state.projects.find((p) => p.id === id));
    else if (act === "delete-project") deleteProject(id);
    else if (act === "open-project") {
      if (id === state.activeProjectId) showView("chat");
      else selectProject(id, { view: "projects" });
    }
    else if (act === "add-docs") { state.fileTarget = "sources"; el.fileInput.click(); }
    else if (act === "open-doc") openDocument(id);
    else if (act === "delete-doc") deleteDocument(id);
    else if (act === "back-sources") { s.docId = null; renderSources(); }
    else if (act === "dismiss-upload") { state.uploads = state.uploads.filter((u) => u.id !== id); renderSources(); }
    else if (act === "source-tab") { s.tab = id; s.error = ""; s.highlight = null; loadSourceTab(); }
    else if (act === "retry-relevance") loadSourceTab();
    else if (act === "save-passage") savePassage(key, btn);
    else if (act === "open-source") {
      const p = passageIndex.get(key);
      if (p) openDocument(p.doc_id, { tab: "text", page: p.page, quote: p.text });
    }
    else if (act === "open-finding-source") {
      const f = state.findings.find((x) => x.id === id);
      if (f) openDocument(f.doc_id, { tab: "text", page: f.page, quote: f.quote });
    }
    else if (act === "explain-finding") explainFinding(id, btn);
    else if (act === "delete-finding") deleteFinding(id);
    else if (act === "scan-documents") scanDocuments();
    else if (act === "pick-tool") { readToolForm(); state.tools.tool = id; renderTools(); }
    else if (act === "tool-all-docs") {
      const boxes = [...el.toolsRoot.querySelectorAll("input[data-doc]")];
      const all = boxes.every((b) => b.checked);
      boxes.forEach((b) => { b.checked = !all; });
      readToolForm();
      renderTools();
    }
    else if (act === "run-tool") runTool();
    else if (act === "cancel-tool") { if (state.tools.abortCtrl) state.tools.abortCtrl.abort(); }
    else if (act === "tool-copy") copyToolResult();
    else if (act === "tool-chat") sendResultToChat();
    else if (act === "tool-save") saveToolResult(btn);
    else if (act === "rename-chat") renameChat(id);
    else if (act === "delete-chat") deleteChat(id);
    else if (extraActions[act]) extraActions[act](btn, btn.dataset);
  }

  function bindEvents() {
    el.composer.addEventListener("submit", handleSend);
    el.input.addEventListener("input", updateComposer);
    el.input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        if (!state.sending) handleSend(e);   // Enter never stops a reply by accident
      }
    });

    el.attach.addEventListener("click", () => { state.fileTarget = "chat"; el.fileInput.click(); });
    el.fileInput.addEventListener("change", () => {
      if (state.fileTarget === "sources") uploadToSources(el.fileInput.files);
      else addFiles(el.fileInput.files);
      el.fileInput.value = "";
    });

    // Drag & drop anywhere: straight into the project from Sources, otherwise onto the message being written.
    let dragDepth = 0;
    window.addEventListener("dragenter", (e) => {
      if (![...e.dataTransfer.types].includes("Files")) return;
      e.preventDefault(); dragDepth++; el.dropzone.hidden = false;
    });
    window.addEventListener("dragover", (e) => e.preventDefault());
    window.addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; el.dropzone.hidden = true; } });
    window.addEventListener("drop", (e) => {
      e.preventDefault(); dragDepth = 0; el.dropzone.hidden = true;
      if (!e.dataTransfer.files.length) return;
      if (state.view === "sources") uploadToSources(e.dataTransfer.files);
      else { if (state.view !== "chat") showView("chat"); addFiles(e.dataTransfer.files); }
    });

    el.newChat.addEventListener("click", () => newChat());
    el.historyList.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-chat-id]");
      if (btn) openChat(btn.dataset.chatId, btn.dataset.seq !== undefined ? Number(btn.dataset.seq) : null);
    });
    el.historySearch.addEventListener("input", () => { state.historyFilter = el.historySearch.value; searchHistory(); });
    el.historyRange.addEventListener("change", () => { state.historyRange = Number(el.historyRange.value); searchHistory(); });
    el.memoryToggle.addEventListener("click", toggleChatMemory);

    document.querySelectorAll(".suggestion").forEach((b) =>
      b.addEventListener("click", () => {
        el.input.value = b.dataset.prompt;
        updateComposer();
        el.input.focus();
        el.input.setSelectionRange(el.input.value.length, el.input.value.length);
      }));

    document.querySelectorAll(".nav__item[data-view]").forEach((b) =>
      b.addEventListener("click", () => {
        const view = b.dataset.view;
        showView(view);
        if (view === "chat" && !isMobile()) el.input.focus();
      }));

    el.contextSelect.addEventListener("change", () => {
      if (el.contextSelect.value === "none") {
        toast("General knowledge: replies will not use or cite your documents.", 4000);
      } else if (activeProject() && !state.documents.length && !state.pendingFiles.length) {
        toast("This project has no documents yet. Attach a file to ask about it.", 4000);
      }
    });

    // Research views: buttons, notes, filters
    document.addEventListener("click", handleAction);
    document.addEventListener("change", (e) => {
      const t = e.target;
      if (t.dataset.note) saveNote(t.dataset.note, t.value);
      else if (t.id === "source-min-score") {
        state.sources.minScore = Number(t.value);
        state.sources.passages = null;
        loadSourceTab();
      }
      else if (el.toolsRoot.contains(t)) {
        readToolForm();
        if (t.dataset.doc !== undefined) renderTools();      // the comparison option depends on how many are picked
      }
      else if (t.id === "combined-doc") { state.combined.docId = t.value; renderCombinedGroups(); }
      else if (t.id === "combined-score") { state.combined.minScore = Number(t.value); renderCombinedGroups(); }
    });
    document.addEventListener("input", (e) => {
      const t = e.target;
      if (t.id === "combined-search") { state.combined.query = t.value; renderCombinedGroups(); }
      else if (t.id === "source-min-score") $("#source-min-score-value").textContent = Number(t.value).toFixed(2);
    });

    // Projects
    el.projectSelect.addEventListener("change", () => selectProject(el.projectSelect.value));
    el.projectForm.addEventListener("submit", submitProject);
    $("#pf-cancel").addEventListener("click", () => el.projectDialog.close());

    // Model selection
    el.modelSelect.addEventListener("change", () => {
      state.selectedModel = el.modelSelect.value;
      savePref(CONFIG.MODEL_PREF_KEY, state.selectedModel);
      renderStatus();
      toast(`Next replies will use ${state.selectedModel}`);
    });
    el.refreshModels.addEventListener("click", async () => {
      el.refreshModels.classList.add("is-spinning");
      await checkHealth({ announce: true });
      el.refreshModels.classList.remove("is-spinning");
    });

    // Keep the status honest: re-check periodically and when the tab regains focus.
    setInterval(() => { if (!document.hidden && !state.sending) checkHealth(); }, CONFIG.HEALTH_POLL_MS);
    document.addEventListener("visibilitychange", () => { if (!document.hidden && !state.sending) checkHealth(); });

    el.sidebarOpen.addEventListener("click", () => setSidebar(true));
    el.sidebarClose.addEventListener("click", () => setSidebar(false));
    el.scrim.addEventListener("click", () => setSidebar(false));
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape" || document.querySelector("dialog[open]")) return;
      if (state.sending) { stopGenerating(); return; }
      if (el.app.classList.contains("sidebar-open")) { setSidebar(false); el.sidebarOpen.focus(); }
    });
    window.matchMedia("(max-width: 860px)").addEventListener("change", (m) => { if (!m.matches) setSidebar(false); });
  }

  /* ---------- 13b. ACCOUNT: SIGN IN, CREATE ACCOUNT, SIGN OUT ----------
     The server decides who is signed in, from a session cookie this script cannot read.
     Nothing secret is kept in localStorage or sessionStorage: no password, no session id. */
  const authScreen = $("#auth-screen");
  const authForms = { login: $("#login-form"), register: $("#register-form") };

  function showAuth(message = "") {
    el.app.hidden = true;
    authScreen.hidden = false;
    const note = $("#auth-message");
    note.hidden = !message;
    note.textContent = message;
    authForms[authForms.register.hidden ? "login" : "register"].querySelector("input").focus();
  }

  function showAuthTab(tab) {
    for (const [name, form] of Object.entries(authForms)) form.hidden = name !== tab;
    authScreen.querySelectorAll("[data-auth-tab]").forEach((b) => {
      b.classList.toggle("is-active", b.dataset.authTab === tab);
      b.setAttribute("aria-selected", String(b.dataset.authTab === tab));
    });
    authForms[tab].querySelector("input").focus();
  }

  /** Shows a message under each field it belongs to, and a general one under the form. */
  function showAuthErrors(form, fields = {}, general = "") {
    form.querySelectorAll("[data-error]").forEach((node) => {
      const text = node.dataset.error === "form" ? general : fields[node.dataset.error] || "";
      node.hidden = !text;
      node.textContent = text;
    });
    form.querySelectorAll("input").forEach((input) => input.classList.toggle("is-invalid", Boolean(fields[input.name])));
  }

  /** The same checks the server makes, so mistakes are caught before anything is sent. */
  function checkRegistration(values) {
    const problems = {};
    const username = values.username.trim().toLowerCase();
    if (!username) problems.username = "Choose a username.";
    else if (!/^[a-z0-9][a-z0-9._-]{2,29}$/.test(username)) {
      problems.username = "Usernames are 3 to 30 characters: letters, digits, dot, dash or underscore, starting with a letter or digit.";
    }
    if (!values.email.trim()) problems.email = "Enter your email address.";
    else if (!/^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$/.test(values.email.trim())) problems.email = "That doesn't look like an email address.";
    if (!values.password) problems.password = "Choose a password.";
    else if (values.password.length < 8) problems.password = "Use at least 8 characters.";
    else if (values.password !== values.confirm_password) problems.confirm_password = "The two passwords don't match.";
    return problems;
  }

  async function submitAuth(e) {
    e.preventDefault();
    const form = e.currentTarget;
    const registering = form === authForms.register;
    const values = Object.fromEntries(new FormData(form));
    let problems = {};
    if (registering) problems = checkRegistration(values);
    else if (!values.identifier.trim() || !values.password) {
      showAuthErrors(form, {}, "Enter your username or email and your password.");
      return;
    }
    if (Object.keys(problems).length) { showAuthErrors(form, problems); return; }

    const button = form.querySelector("button[type=submit]");
    const label = button.textContent;
    button.disabled = true;
    button.textContent = registering ? "Creating account…" : "Signing in…";
    showAuthErrors(form);
    try {
      await api.json(registering ? "/api/auth/register" : "/api/auth/login", { method: "POST", body: values });
      form.reset();                 // the password does not linger in the page
      location.reload();            // start clean, as the signed-in user
    } catch (err) {
      showAuthErrors(form, err.data?.error?.fields || {}, err.data?.error?.fields ? "" : err.message.replace(/`/g, ""));
      button.disabled = false;
      button.textContent = label;
    }
  }

  async function logout() {
    try {
      await api.json("/api/auth/logout", { method: "POST" });      // ends the session on the host
    } catch {
      /* reloading shows the true state either way */
    }
    location.reload();              // drops everything this page had in memory
  }

  /** The session expired or was ended: hide the user's data and ask them to sign in again. */
  function sessionEnded() {
    if (!state.user) return;
    state.user = null;
    state.csrf = "";
    showAuth("Your session has ended. Sign in again to continue.");
  }

  /** Asks the server whether this browser is signed in. Returns true if it is. */
  async function loadSession() {
    try {
      const me = await api.json("/api/auth/me");
      state.user = me.user;
      state.csrf = me.csrf_token;
      $("#user-name").textContent = me.user.username;
      $("#user-name").title = `Signed in as ${me.user.username} (${me.user.email})`;
      return true;
    } catch (err) {
      showAuth(err.status === 401 ? "" : err.message.replace(/`/g, ""));
      return false;
    }
  }

  /** Network details for the Collaboration page, and this user's memory settings. */
  async function loadIdentity() {
    try {
      state.identity = await api.json("/api/collab/me");
      state.settings = (await api.json("/api/settings")).settings;
    } catch {
      /* server unreachable: the status line already says so */
    }
  }

  /** True when other people may be changing the active project. */
  const isSharedProject = () => Boolean(activeProject()?.shared);

  /**
   * Checks a shared workspace for documents and findings added on other devices and reloads
   * them, without disturbing a reply or a form the user is in the middle of. Conversations
   * are private to each user, so there is nothing of other people's to reload there.
   */
  async function pollChanges() {
    const project = activeProject();
    if (!state.user || !project || !isSharedProject() || document.hidden || state.sending || state.tools.running) return;
    let revision;
    try {
      revision = (await api.json(`/api/projects/${project.id}/changes`)).revision;
    } catch (err) {
      // Access was withdrawn (removed, or sharing stopped): go back to what this user still has.
      if (err.status === 404 && project.id === state.activeProjectId) {
        toast(`You no longer have access to "${project.title}".`, 5000);
        await loadWorkspace();
      }
      return;
    }
    if (state.revision === null || revision === state.revision || project.id !== state.activeProjectId) {
      state.revision = revision;
      return;
    }
    state.revision = revision;
    try {
      const [docs, findings] = await Promise.all([
        api.json(`/api/projects/${project.id}/documents`),
        api.json(`/api/projects/${project.id}/findings`),
      ]);
      if (project.id !== state.activeProjectId) return;
      state.documents = docs.documents;
      state.findings = findings.findings;
      await refreshProjects();
      if (state.view === "sources" && !state.uploads.length && !state.sources.docId) renderSources();
      else if (state.view === "findings") renderCombinedGroups();
      else if (state.view === "collab") extraViews.collab();
    } catch {
      /* try again on the next tick */
    }
  }

  // What modules.js (paraphrase, word counter, memory, collaboration) builds on.
  window.Gihigugma = {
    CONFIG, api, ApiError, state, el, $, ICONS, extraViews, extraActions,
    escapeHtml, renderMarkdown, toast, toastError, plural, relativeDay, statsText, savePref, loadPref, copyToClipboard,
    confirmDialog, textDialog, activeProject, activeChat, noProjectHtml, showView, newChat, ensureActiveChat,
    updateComposer, renderMemoryToggle, refreshProjects, selectProject, loadWorkspace, loadIdentity, checkHealth,
    openProjectDialog, renderTools,
  };

  /* ---------- 14. INIT ---------- */
  async function init() {
    bindEvents();
    authForms.login.addEventListener("submit", submitAuth);
    authForms.register.addEventListener("submit", submitAuth);
    authScreen.querySelectorAll("[data-auth-tab]").forEach((b) =>
      b.addEventListener("click", () => showAuthTab(b.dataset.authTab)));
    $("#logout-btn").addEventListener("click", logout);
    // Earlier versions kept a workspace token in this browser's storage. It is no longer used; remove it.
    try { localStorage.removeItem("eros.workspaceToken"); } catch { /* storage unavailable */ }
    renderStatus();
    checkHealth();

    if (!(await loadSession())) return;      // not signed in: the sign-in screen is showing, and nothing else loads
    authScreen.hidden = true;
    el.app.hidden = false;
    newChat({ focus: !isMobile() });         // Chat screen is always the first thing shown
    updateComposer();
    await loadIdentity();
    await loadWorkspace();
    setInterval(pollChanges, CONFIG.CHANGES_POLL_MS);
  }

  document.addEventListener("DOMContentLoaded", init);
})();

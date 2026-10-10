/* =========================================================
   Gihigugma — workspaces added on top of the research core

   Sections:
     1. Shared helpers (loading a document's text)
     2. Paraphrase
     3. Word counter
     4. Memory
     5. Collaboration

   Builds on window.Gihigugma (script.js): the API layer, state,
   dialogs and view switching. Each view registers itself in
   extraViews, and its buttons in extraActions.
   ========================================================= */
(() => {
  "use strict";

  const G = window.Gihigugma;
  const { api, ApiError, state, el, $, escapeHtml, toast, toastError, plural, relativeDay, statsText,
          confirmDialog, textDialog, activeProject, showView, extraViews, extraActions } = G;

  const PARAPHRASE_MAX = 30000;       // must match PARAPHRASE_MAX_CHARS in backend/config.py
  const COUNTER_MAX = 400000;         // keeps the page responsive with very large documents

  /* ---------- 1. SHARED HELPERS ---------- */

  /** A <select> that loads the extracted text of one of the project's documents. */
  function documentPicker(action) {
    if (!state.documents.length) return "";
    return `
      <label class="field"><span>Use text from a document</span>
        <select class="select" data-load="${action}">
          <option value="">Choose a document…</option>
          ${state.documents.map((d) => `<option value="${d.id}">${escapeHtml(d.name)}</option>`).join("")}
        </select></label>`;
  }

  /** The text that was extracted from a document when it was uploaded. */
  async function documentText(id, limit) {
    const data = await api.json(`/api/documents/${id}`);
    const text = data.pages.map((p) => p.text).join("\n\n");
    if (text.length > limit) {
      toast(`Only the first ${limit.toLocaleString()} characters of ${data.document.name} were loaded.`, 5000);
      return text.slice(0, limit);
    }
    return text;
  }

  function countsLine(text) {
    const s = TextStats.stats(text);
    return `${plural(s.words, "word")} · ${plural(s.characters, "character")}`;
  }

  async function copyText(text) {
    toast((await G.copyToClipboard(text)) ? "Copied to clipboard" : "Couldn't copy. Select the text and copy it by hand.");
  }

  /** Puts text into the chat composer so the user can ask about it. */
  function sendToChat(text) {
    if (!text.trim()) { toast("There is no text to send."); return; }
    showView("chat");
    el.input.value = text;
    G.updateComposer();
    el.input.focus();
    el.input.setSelectionRange(0, 0);
    toast("The text is in the message box. Add your question and send.", 4500);
  }

  /* ---------- 2. PARAPHRASE ---------- */
  const STYLES = [
    ["academic", "Academic"], ["formal", "Formal"], ["simple", "Simple"],
    ["concise", "Concise"], ["expanded", "Expanded"], ["natural", "Natural"],
  ];
  const STRENGTHS = { 1: "Light: change some wording", 2: "Moderate: rephrase sentences", 3: "Strong: restructure freely" };

  const para = state.para = {
    input: "", style: "natural", strength: 2, instructions: "",
    original: "", output: "", notice: "", stats: null, model: "", error: "",
    running: false, abortCtrl: null, progress: null,
  };

  function renderParaphrase() {
    const root = $("#paraphrase-root");
    root.innerHTML = `
      <header class="view__head">
        <div><h2>Paraphrase</h2>
          <p>Rewrite text in another style with the local model. Meaning, facts, numbers and citations are kept, and nothing is added.</p></div>
      </header>
      <div class="toolform">
        <label class="field"><span>Text to rewrite</span>
          <textarea class="input" id="para-input" rows="8" maxlength="${PARAPHRASE_MAX}" placeholder="Type or paste text here, or load it from a document below.">${escapeHtml(para.input)}</textarea></label>
        <p class="hint" id="para-count">${countsLine(para.input)}</p>
        <div class="toolform__row">
          <label class="field"><span>Style</span>
            <select class="select" id="para-style">${STYLES.map(([value, label]) =>
              `<option value="${value}"${para.style === value ? " selected" : ""}>${label}</option>`).join("")}</select></label>
          <label class="field"><span>How much to change</span>
            <input type="range" id="para-strength" min="1" max="3" step="1" value="${para.strength}" />
            <span class="hint" id="para-strength-label">${STRENGTHS[para.strength]}</span></label>
          ${documentPicker("paraphrase")}
        </div>
        <label class="field"><span>Extra instructions <em>optional</em></span>
          <input class="input" id="para-instructions" maxlength="400" value="${escapeHtml(para.instructions)}" placeholder="e.g. use British spelling, keep it in the past tense" /></label>
        <div class="toolform__run">
          <button type="button" class="btn btn--primary" data-act="para-run"${para.running ? " disabled" : ""}>Paraphrase</button>
          <button type="button" class="btn" data-act="para-cancel"${para.running ? "" : " hidden"}>Cancel</button>
          <div class="progress" id="para-progress" role="status" aria-live="polite"></div>
        </div>
      </div>
      <div id="para-result"></div>`;
    renderParaphraseResult();
  }

  function renderParaphraseResult() {
    const box = $("#para-result");
    const progress = $("#para-progress");
    if (!box) return;
    if (progress) {
      const p = para.progress;
      progress.innerHTML = para.running
        ? `<span>${p && p.total > 1 ? `Rewriting part ${p.step} of ${p.total}…` : "Rewriting…"}</span>
           <div class="progress__bar"><i style="width:${p ? Math.round(((p.step - 0.5) / p.total) * 100) : 5}%"></i></div>`
        : "";
    }
    if (para.error) { box.innerHTML = `<p class="notice">${escapeHtml(para.error)}</p>`; return; }
    if (!para.output && !para.running) {
      box.innerHTML = `<div class="empty"><strong>Nothing rewritten yet</strong>
        <p>Enter some text, choose a style, and press Paraphrase. The original and the rewrite appear side by side.</p></div>`;
      return;
    }
    const done = !para.running && para.output;
    box.innerHTML = `
      <article class="result">
        <div class="result__head"><h3>${escapeHtml(STYLES.find(([v]) => v === para.usedStyle)?.[1] || "")} rewrite</h3>
          <span class="msg__stats">${escapeHtml([para.model, statsText(para.stats)].filter(Boolean).join(" · "))}</span></div>
        <div class="compare">
          <section><span class="label">Original · ${countsLine(para.original)}</span>
            <div class="compare__text">${escapeHtml(para.original)}</div></section>
          <section><span class="label">Rewritten · <span id="para-out-count">${countsLine(para.output)}</span></span>
            <div class="compare__text" id="para-output">${escapeHtml(para.output) || '<span class="hint">Working…</span>'}</div></section>
        </div>
        ${para.notice ? `<p class="notice">${escapeHtml(para.notice)}</p>` : ""}
        ${done ? `<div class="view__actions">
          <button type="button" class="btn btn--small" data-act="para-copy">Copy</button>
          <button type="button" class="btn btn--small" data-act="para-replace">Replace input with this</button>
          <button type="button" class="btn btn--small" data-act="para-run">Regenerate</button>
          <button type="button" class="btn btn--small" data-act="para-chat">Ask about it in chat</button>
          <button type="button" class="btn btn--small" data-act="para-count">Count words</button>
          <button type="button" class="btn btn--small" data-act="para-suggest">Get suggestions to improve it</button>
        </div>` : ""}
      </article>`;
  }

  /** Plain-language warning when the rewrite lost or gained a figure or citation. */
  function preservationNotice(checks) {
    const parts = [];
    const list = (items) => items.slice(0, 8).join(", ");
    if (checks.numbers_lost.length) parts.push(`Numbers in the original that are missing from the rewrite: ${list(checks.numbers_lost)}.`);
    if (checks.citations_lost.length) parts.push(`Citations missing from the rewrite: ${list(checks.citations_lost)}.`);
    if (checks.numbers_new.length) parts.push(`Numbers that appear only in the rewrite: ${list(checks.numbers_new)}.`);
    if (checks.citations_new.length) parts.push(`Citations that appear only in the rewrite: ${list(checks.citations_new)}.`);
    return parts.length ? `Check this rewrite against the original. ${parts.join(" ")}` : "";
  }

  async function runParaphrase() {
    if (para.running) return;
    const text = para.input.trim();
    if (!text) { toast("Enter some text to paraphrase."); $("#para-input")?.focus(); return; }
    if (text.length > PARAPHRASE_MAX) { toast(`That is too long. The limit is ${PARAPHRASE_MAX.toLocaleString()} characters.`); return; }

    Object.assign(para, { running: true, abortCtrl: new AbortController(), progress: null, error: "",
                          original: text, output: "", notice: "", stats: null, usedStyle: para.style, model: state.selectedModel });
    renderParaphrase();

    let frameQueued = false;
    const queueRender = () => {
      if (frameQueued) return;
      frameQueued = true;
      requestAnimationFrame(() => {
        frameQueued = false;
        const out = $("#para-output");
        if (out) out.textContent = para.output;
        const count = $("#para-out-count");
        if (count) count.textContent = countsLine(para.output);
      });
    };

    try {
      const res = await api.request("/api/paraphrase", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, style: para.style, strength: para.strength, instructions: para.instructions, model: state.selectedModel }),
        signal: para.abortCtrl.signal,
      });
      await api.readStream(res, (evt) => {
        if (evt.type === "start") para.model = evt.model;
        else if (evt.type === "progress") { para.progress = evt; renderParaphraseResult(); }
        else if (evt.type === "token") { para.output += evt.content; queueRender(); }
        else if (evt.type === "done") {
          para.stats = evt.stats;
          para.output = para.output.trim();
          const cut = evt.stats.done_reason === "length" ? "The rewrite reached the length limit and may be cut off. " : "";
          para.notice = cut + preservationNotice(evt.checks);
        }
        else if (evt.type === "error") throw new ApiError(evt.code, evt.message);
      }, para.abortCtrl.signal);
      if (!para.output.trim()) throw new ApiError("empty_reply", "The model returned nothing. Try again, or pick another model.");
    } catch (err) {
      const e = err instanceof ApiError ? err : new ApiError("unknown", "Something went wrong while paraphrasing.");
      if (e.code === "aborted") {
        para.notice = para.output.trim() ? "Cancelled. This rewrite is incomplete." : "";
      } else if (para.output.trim()) {
        para.notice = `Stopped early: ${e.message.replace(/`/g, "")} This rewrite is incomplete.`;
      } else {
        para.error = e.message.replace(/`/g, "");
      }
      if (["backend_offline", "ollama_offline", "model_not_found", "no_models"].includes(e.code)) G.checkHealth();
    } finally {
      Object.assign(para, { running: false, abortCtrl: null, progress: null });
      if (state.view === "paraphrase") renderParaphrase();
    }
  }

  extraViews.paraphrase = renderParaphrase;
  extraActions["para-run"] = runParaphrase;
  extraActions["para-cancel"] = () => para.abortCtrl?.abort();
  extraActions["para-copy"] = () => copyText(para.output);
  extraActions["para-replace"] = () => { para.input = para.output; renderParaphrase(); toast("The rewrite is now the input."); };
  extraActions["para-chat"] = () => sendToChat(para.output);
  extraActions["para-count"] = () => { counter.text = para.output; showView("counter"); };
  extraActions["para-suggest"] = () => {
    // The Suggest research tool can review the user's own draft.
    const form = state.tools.form.suggest;
    form.draft = para.output.slice(0, 6000);
    if (!form.kinds.includes("improvements")) form.kinds.push("improvements");
    state.tools.tool = "suggest";
    showView("tools");
    toast("Your text is in the Suggest tool as the draft to improve.", 4500);
  };

  /* ---------- 3. WORD COUNTER ---------- */
  const counter = state.counter = { text: "" };
  const COUNTS = [
    ["words", "Words"], ["characters", "Characters (with spaces)"], ["charactersNoSpaces", "Characters (no spaces)"],
    ["sentences", "Sentences"], ["paragraphs", "Paragraphs"],
  ];

  function renderCounter() {
    $("#counter-root").innerHTML = `
      <header class="view__head">
        <div><h2>Word counter</h2><p>Counts update as you type. Everything is counted on this device.</p></div>
      </header>
      <div class="tiles" id="counter-tiles"></div>
      <div class="toolform">
        <label class="field"><span>Text</span>
          <textarea class="input" id="counter-input" rows="12" placeholder="Type or paste text here, or load it from a document below.">${escapeHtml(counter.text)}</textarea></label>
        <div class="toolform__row">${documentPicker("counter")}</div>
        <div class="toolform__run">
          <button type="button" class="btn btn--small" data-act="counter-paraphrase">Paraphrase this text</button>
          <button type="button" class="btn btn--small" data-act="counter-chat">Ask about it in chat</button>
          <button type="button" class="btn btn--small" data-act="counter-copy">Copy</button>
          <button type="button" class="btn btn--small" data-act="counter-clear">Clear</button>
        </div>
      </div>
      <details class="evidence">
        <summary>How these are counted</summary>
        <ul class="card__list">
          <li><strong>Words:</strong> runs of letters or digits. A word with an apostrophe or hyphen inside is one word (don't, well-known); so is a number with a decimal point or separator (3.5, 12,400) and an abbreviation like e.g. or U.S. Punctuation on its own is not a word.</li>
          <li><strong>Characters:</strong> every character except line breaks. "No spaces" also leaves out spaces and tabs.</li>
          <li><strong>Sentences:</strong> text ending in . ! ? or … Dots in abbreviations (Dr., e.g.), initials, acronyms and decimals do not end a sentence. A final line without a full stop still counts.</li>
          <li><strong>Paragraphs:</strong> lines that contain text. Blank lines are not counted.</li>
        </ul>
      </details>`;
    renderCounts();
  }

  function renderCounts() {
    const tiles = $("#counter-tiles");
    if (!tiles) return;
    const s = TextStats.stats(counter.text);
    tiles.innerHTML = COUNTS.map(([key, label]) =>
      `<div class="tile"><b>${s[key].toLocaleString()}</b><span>${label}</span></div>`).join("");
  }

  extraViews.counter = renderCounter;
  extraActions["counter-clear"] = () => { counter.text = ""; renderCounter(); $("#counter-input").focus(); };
  extraActions["counter-copy"] = () => copyText(counter.text);
  extraActions["counter-chat"] = () => sendToChat(counter.text);
  extraActions["counter-paraphrase"] = () => {
    if (!counter.text.trim()) { toast("There is no text to paraphrase."); return; }
    para.input = counter.text.slice(0, PARAPHRASE_MAX);
    if (counter.text.length > PARAPHRASE_MAX) toast(`Only the first ${PARAPHRASE_MAX.toLocaleString()} characters were taken.`, 4500);
    showView("paraphrase");
  };

  // Typing in either workspace: remember the text and refresh the counts, without redrawing the page.
  document.addEventListener("input", (e) => {
    const t = e.target;
    if (t.id === "para-input") { para.input = t.value; $("#para-count").textContent = countsLine(t.value); }
    else if (t.id === "para-instructions") para.instructions = t.value;
    else if (t.id === "para-strength") { para.strength = Number(t.value); $("#para-strength-label").textContent = STRENGTHS[para.strength]; }
    else if (t.id === "counter-input") { counter.text = t.value; renderCounts(); }
  });
  document.addEventListener("change", async (e) => {
    const t = e.target;
    if (t.id === "para-style") para.style = t.value;
    else if (t.dataset.load && t.value) {
      const target = t.dataset.load;
      t.disabled = true;
      try {
        const text = await documentText(t.value, target === "paraphrase" ? PARAPHRASE_MAX : COUNTER_MAX);
        if (target === "paraphrase") { para.input = text; renderParaphrase(); }
        else { counter.text = text; renderCounter(); }
      } catch (err) {
        toastError(err, "Couldn't load that document.");
        t.disabled = false;
      }
    }
    else if (t.dataset.setting) saveSetting(t.dataset.setting, t.checked);
  });

  /* ---------- 4. MEMORY ---------- */
  const KINDS = [["preference", "Preference"], ["instruction", "Instruction"], ["topic", "Research topic"],
                 ["context", "Project context"], ["terminology", "Terminology"]];
  const kindLabel = (kind) => KINDS.find(([value]) => value === kind)?.[1] || kind;
  const memory = state.memory = { saved: [], pending: [], loading: false, error: "" };

  async function loadMemory() {
    memory.loading = true;
    memory.error = "";
    renderMemory();
    try {
      const [settings, data] = await Promise.all([api.json("/api/settings"), api.json("/api/memories")]);
      state.settings = settings.settings;
      memory.saved = data.memories;
      memory.pending = data.pending;
    } catch (err) {
      memory.error = err.message || "Couldn't load memory.";
    }
    memory.loading = false;
    if (state.view === "memory") renderMemory();
    G.renderMemoryToggle();
  }

  function memoryCard(m, pending) {
    const project = m.project_id ? state.projects.find((p) => p.id === m.project_id) : null;
    const scope = m.project_id ? `Only in ${project ? project.title : "one project"}` : "All projects";
    return `
      <article class="passage">
        <div class="passage__meta"><span class="badge">${kindLabel(m.kind)}</span>
          <span class="passage__q">${escapeHtml(scope)} · ${pending ? "suggested" : "saved"} ${relativeDay(m.updated_at * 1000).toLowerCase()}</span></div>
        <div>${escapeHtml(m.content)}</div>
        <div class="passage__actions">
          ${pending ? `<button type="button" class="msg__action" data-act="memory-approve" data-id="${m.id}">Save to memory</button>` : ""}
          <button type="button" class="msg__action" data-act="memory-edit" data-id="${m.id}">Edit</button>
          <button type="button" class="msg__action" data-act="memory-delete" data-id="${m.id}">${pending ? "Dismiss" : "Delete"}</button>
        </div>
      </article>`;
  }

  function renderMemory() {
    const root = $("#memory-root");
    const s = state.settings;
    const project = activeProject();
    let body;
    if (memory.loading) body = '<p class="hint">Loading…</p>';
    else if (memory.error) body = `<p class="notice">${escapeHtml(memory.error)}</p>`;
    else {
      body = `
        ${memory.pending.length ? `<section class="group">
          <div class="group__head"><h3>Suggested, waiting for you</h3><span class="badge">${memory.pending.length}</span></div>
          <p class="hint">These came from your messages. They are not used until you save them.</p>
          <div class="passages">${memory.pending.map((m) => memoryCard(m, true)).join("")}</div></section>` : ""}
        <section class="group">
          <div class="group__head"><h3>Add a memory</h3></div>
          <div class="toolform">
            <label class="field"><span>What should be remembered?</span>
              <input class="input" id="memory-new" maxlength="400" placeholder="e.g. I prefer short answers with bullet points" /></label>
            <div class="toolform__row">
              <label class="field"><span>Kind</span><select class="select" id="memory-kind">${KINDS.map(([value, label]) =>
                `<option value="${value}">${label}</option>`).join("")}</select></label>
              <label class="field"><span>Applies to</span><select class="select" id="memory-scope">
                <option value="">All projects</option>
                ${project ? `<option value="${project.id}">Only ${escapeHtml(project.title)}</option>` : ""}</select></label>
            </div>
            <div class="toolform__run"><button type="button" class="btn btn--primary btn--small" data-act="memory-add">Save memory</button></div>
          </div>
        </section>
        <section class="group">
          <div class="group__head"><h3>Saved memories</h3><span class="badge">${memory.saved.length}</span>
            ${memory.saved.length ? '<button type="button" class="link-btn" data-act="memory-clear">Delete all</button>' : ""}</div>
          ${memory.saved.length ? `<div class="passages">${memory.saved.map((m) => memoryCard(m, false)).join("")}</div>`
            : '<div class="empty"><strong>Nothing remembered yet</strong><p>Add a memory above, or turn on suggestions and approve the ones you want kept.</p></div>'}
        </section>`;
    }
    root.innerHTML = `
      <header class="view__head">
        <div><h2>Memory</h2>
          <p>Things Gihigugma keeps in mind across chats and restarts: your preferences, research topics, terms and standing instructions. Stored only on this computer, separate from chats and documents.</p></div>
      </header>
      <div class="toolform">
        <label class="check"><input type="checkbox" data-setting="memory_enabled"${s.memory_enabled ? " checked" : ""} />
          <span class="check__name"><strong>Use memory</strong>: saved memories that fit a question are given to the AI. Off by default.</span></label>
        <label class="check"><input type="checkbox" data-setting="memory_suggestions"${s.memory_suggestions ? " checked" : ""}${s.memory_enabled ? "" : " disabled"} />
          <span class="check__name"><strong>Suggest memories</strong> from what I write. Nothing is saved until I approve it.</span></label>
        <p class="hint">At most 5 memories are used per question, and each reply shows which ones. Passwords, contact details and ID numbers are refused.
          You can switch memory off for a single chat with the Memory button above the message box.</p>
      </div>
      ${body}`;
  }

  async function saveSetting(key, value) {
    try {
      state.settings = (await api.json("/api/settings", { method: "PUT", body: { [key]: value } })).settings;
      toast(key === "memory_enabled" ? (value ? "Memory is on." : "Memory is off. Saved memories are kept but not used.") : "Saved.");
    } catch (err) {
      toastError(err, "Couldn't save the setting.");
    }
    renderMemory();
    G.renderMemoryToggle();
  }

  async function memoryRequest(path, options, failure) {
    try {
      await api.json(path, options);
      await loadMemory();
      return true;
    } catch (err) {
      toastError(err, failure);
      return false;
    }
  }

  extraViews.memory = loadMemory;
  extraActions["memory-add"] = async () => {
    const content = $("#memory-new").value.trim();
    if (!content) { toast("Write what should be remembered."); $("#memory-new").focus(); return; }
    const saved = await memoryRequest("/api/memories", {
      method: "POST", body: { content, kind: $("#memory-kind").value, project_id: $("#memory-scope").value || null },
    }, "Couldn't save the memory.");
    if (saved) toast("Saved to memory");
  };
  extraActions["memory-approve"] = (_btn, { id }) =>
    memoryRequest(`/api/memories/${id}`, { method: "PATCH", body: { approve: true } }, "Couldn't save the memory.");
  extraActions["memory-edit"] = async (_btn, { id }) => {
    const item = [...memory.saved, ...memory.pending].find((m) => m.id === id);
    if (!item) return;
    const content = await textDialog({ title: "Edit memory", label: "What should be remembered?", value: item.content });
    if (content && content !== item.content) {
      await memoryRequest(`/api/memories/${id}`, { method: "PATCH", body: { content } }, "Couldn't update the memory.");
    }
  };
  extraActions["memory-delete"] = async (_btn, { id }) => {
    const item = memory.saved.find((m) => m.id === id);
    if (item && !(await confirmDialog({ title: "Delete this memory?", text: "It will no longer be used in any chat." }))) return;
    await memoryRequest(`/api/memories/${id}`, { method: "DELETE" }, "Couldn't delete the memory.");
  };
  extraActions["memory-clear"] = async () => {
    const ok = await confirmDialog({
      title: "Delete all memories?",
      text: `All ${plural(memory.saved.length + memory.pending.length, "memory").replace("memorys", "memories")} are permanently deleted. Chats and documents are not affected.`,
      okLabel: "Delete all",
    });
    if (ok) await memoryRequest("/api/memories", { method: "DELETE" }, "Couldn't clear memory.");
  };

  /* ---------- 5. COLLABORATION ---------- */
  const ROLES = [["viewer", "Viewer: can read"], ["contributor", "Contributor: can add and edit"],
                 ["manager", "Manager: can also delete"]];
  const collab = state.collab = { members: {}, loading: false };

  async function loadCollab() {
    collab.loading = true;
    renderCollab();
    await G.loadIdentity();
    await G.refreshProjects();
    const shared = state.projects.filter((p) => p.shared);
    const lists = await Promise.all(shared.map((p) =>
      api.json(`/api/projects/${p.id}/members`).then((d) => [p.id, d.members]).catch(() => [p.id, []])));
    collab.members = Object.fromEntries(lists);
    collab.loading = false;
    if (state.view === "collab") renderCollab();
  }

  function memberRow(m, manage) {
    const seen = m.active ? "Active now" : `Last seen ${relativeDay(m.last_seen * 1000).toLowerCase()}`;
    const you = m.user_id === state.user.id ? " (you)" : "";
    return `
      <li class="row">
        <span class="status__dot${m.active ? " is-on" : ""}" aria-hidden="true"></span>
        <div class="row__main"><span class="row__title">${escapeHtml(m.name)}${you}</span>
          <span class="row__meta">${seen}${manage ? "" : ` · ${escapeHtml(m.role)}`}</span></div>
        ${manage ? `<div class="row__actions">
          <label class="visually-hidden" for="role-${m.id}">Role of ${escapeHtml(m.name)}</label>
          <select class="select" id="role-${m.id}" data-member-role="${m.id}">${ROLES.map(([value, label]) =>
            `<option value="${value}"${m.role === value ? " selected" : ""}>${label}</option>`).join("")}</select>
          <button type="button" class="btn btn--small btn--danger" data-act="member-remove" data-id="${m.id}" data-name="${escapeHtml(m.name)}">Remove</button>
        </div>` : ""}
      </li>`;
  }

  function renderCollab() {
    const root = $("#collab-root");
    if (collab.loading && !Object.keys(collab.members).length && !state.identity.lan) {
      root.innerHTML = '<header class="view__head"><div><h2>Collaboration</h2></div></header><p class="hint">Loading…</p>';
      return;
    }

    const lan = state.identity.lan || { enabled: false, addresses: [], port: 5050 };
    const network = lan.enabled
      ? `<p><strong>Network mode is on.</strong> On another device connected to the same Wi-Fi or hotspot, open:</p>
         ${lan.addresses.length ? `<p class="codes">${lan.addresses.map((a) => `<code>${escapeHtml(a)}</code>`).join(" ")}</p>
           ${lan.addresses.length > 1 ? '<p class="hint">The host has several network adapters. The first address is the one for its current Wi-Fi or hotspot; try the others only if it doesn\'t work.</p>' : ""}`
           : '<p class="notice">No network address was found. Connect the host computer to Wi-Fi or a hotspot and reload this page.</p>'}
         <p class="notice">Anyone on this network can reach that address and create an account, but they see only their own work and the workspaces they are invited to. The connection is not encrypted (plain http), so use it on networks you trust.</p>
         <p class="hint">If another device can't connect: check both are on the same network, allow Python through the firewall for Private networks, and keep the server running.</p>`
      : `<p><strong>Network mode is off.</strong> Only the host computer can open Gihigugma.</p>
         <p class="hint">To let other devices connect: stop the server (Ctrl+C in its terminal), then start it with</p>
         <p class="codes"><code>python app.py --lan</code></p>
         <p class="hint">Then reload this page for the address to give to others. You can already choose what to share below.</p>`;

    const mine = state.projects.filter((p) => p.role === "owner").map((p) => {
      const people = collab.members[p.id] || [];
      const detail = p.shared ? `
        <div class="toolform__row">
          <div class="field"><span>Invitation code</span>
            <p class="codes"><code>${escapeHtml(p.invite_code || "")}</code>
              <button type="button" class="link-btn" data-act="share-newcode" data-id="${p.id}">New code</button></p></div>
          <label class="field"><span>Role for people who join</span>
            <select class="select" data-default-role="${p.id}">${ROLES.map(([value, label]) =>
              `<option value="${value}"${p.default_role === value ? " selected" : ""}>${label}</option>`).join("")}</select></label>
        </div>
        ${people.length ? `<ul class="rows">${people.map((m) => memberRow(m, true)).join("")}</ul>`
          : '<p class="hint">Nobody has joined yet. People sign in with their own account, open Collaboration, and enter this code.</p>'}` : "";
      return `
        <article class="card${p.shared ? " is-active" : ""}">
          <div class="card__head"><h3>${escapeHtml(p.title)}</h3>
            <button type="button" class="btn btn--small${p.shared ? " btn--danger" : " btn--primary"}" data-act="share-toggle" data-id="${p.id}">${p.shared ? "Stop sharing" : "Share"}</button></div>
          <p class="stats"><span><b>${p.stats.documents}</b> documents</span><span><b>${p.stats.findings}</b> findings</span>
            ${p.shared ? `<span><b>${people.length}</b> participants</span>` : ""}</p>
          ${detail}
        </article>`;
    }).join("");

    const joined = state.projects.filter((p) => p.role !== "owner").map((p) => {
      const people = collab.members[p.id] || [];
      return `
        <article class="card">
          <div class="card__head"><h3>${escapeHtml(p.title)}</h3>
            <button type="button" class="btn btn--small btn--danger" data-act="collab-leave" data-id="${p.id}">Leave</button></div>
          <p class="stats"><span>Your role: <b>${escapeHtml(p.role)}</b></span><span><b>${p.stats.documents}</b> documents</span>
            <span><b>${people.length}</b> participants</span></p>
          ${people.length ? `<ul class="rows">${people.map((m) => memberRow(m, false)).join("")}</ul>` : ""}
        </article>`;
    }).join("");

    root.innerHTML = `
      <header class="view__head">
        <div><h2>Collaboration</h2>
          <p>Share a project as a workspace with other people on the same Wi-Fi or hotspot. Documents and findings in a
            shared workspace are shared. Your chats and memories stay private to your account, and so do theirs.</p></div>
      </header>
      <div class="toolform">${network}</div>
      <section class="group"><div class="group__head"><h3>Join a workspace</h3></div>
        <form class="toolform" id="join-form" autocomplete="off">
          <div class="toolform__run">
            <label class="visually-hidden" for="join-code">Invitation code</label>
            <input class="input" id="join-code" maxlength="12" placeholder="Invitation code from the workspace owner" autocapitalize="characters" required />
            <button type="submit" class="btn btn--primary btn--small">Join</button>
          </div>
        </form>
      </section>
      ${joined ? `<section class="group"><div class="group__head"><h3>Workspaces you joined</h3></div><div class="passages">${joined}</div></section>` : ""}
      <section class="group"><div class="group__head"><h3>Your projects</h3></div>
        ${mine ? `<div class="passages">${mine}</div>`
          : '<div class="empty"><strong>No projects of your own yet</strong><p>Create a project, then share it here.</p><button type="button" class="btn btn--primary" data-act="new-project">New project</button></div>'}
      </section>`;
  }

  async function collabRequest(path, options, failure) {
    try {
      await api.json(path, options);
    } catch (err) {
      toastError(err, failure);
    }
    await loadCollab();
  }

  extraViews.collab = loadCollab;
  extraActions["share-toggle"] = async (_btn, { id }) => {
    const project = state.projects.find((p) => p.id === id);
    if (!project) return;
    if (project.shared) {
      const count = (collab.members[id] || []).length;
      const ok = await confirmDialog({
        title: `Stop sharing "${project.title}"?`,
        text: count ? `${plural(count, "participant")} will lose access immediately. The project and its content stay on the host.`
          : "People will no longer be able to join. The project and its content stay on the host.",
        okLabel: "Stop sharing",
      });
      if (!ok) return;
    }
    await collabRequest(`/api/projects/${id}/sharing`, { method: "PATCH", body: { shared: !project.shared } }, "Couldn't change sharing.");
  };
  extraActions["share-newcode"] = async (_btn, { id }) => {
    const ok = await confirmDialog({
      title: "Make a new invitation code?",
      text: "The old code stops working for new people. Participants who already joined keep their access.",
      okLabel: "New code",
    });
    if (ok) await collabRequest(`/api/projects/${id}/sharing`, { method: "PATCH", body: { new_code: true } }, "Couldn't make a new code.");
  };
  extraActions["member-remove"] = async (_btn, { id, name }) => {
    const ok = await confirmDialog({ title: `Remove ${name}?`, text: "They lose access to the workspace immediately.", okLabel: "Remove" });
    if (ok) await collabRequest(`/api/members/${id}`, { method: "DELETE" }, "Couldn't remove the participant.");
  };
  extraActions["collab-leave"] = async (_btn, { id }) => {
    const project = state.projects.find((p) => p.id === id);
    const ok = await confirmDialog({
      title: `Leave "${project ? project.title : "this workspace"}"?`,
      text: "You will need an invitation code to join again. Your own chats in it are kept but can't be continued.",
      okLabel: "Leave",
    });
    if (!ok) return;
    try {
      await api.json(`/api/projects/${id}/leave`, { method: "POST" });
    } catch (err) {
      toastError(err, "Couldn't leave the workspace.");
    }
    if (id === state.activeProjectId) await G.loadWorkspace();
    showView("collab");
  };
  document.addEventListener("submit", async (e) => {
    if (e.target.id !== "join-form") return;
    e.preventDefault();
    const code = $("#join-code").value.trim();
    if (!code) return;
    try {
      const joined = await api.json("/api/collab/join", { method: "POST", body: { code } });
      await G.refreshProjects();
      await G.selectProject(joined.project.id, { view: "sources" });
      toast(`Joined "${joined.project.title}"`);
    } catch (err) {
      toastError(err, "Couldn't join. Check the code.");
    }
  });
  document.addEventListener("change", (e) => {
    const t = e.target;
    if (t.dataset.memberRole) {
      collabRequest(`/api/members/${t.dataset.memberRole}`, { method: "PATCH", body: { role: t.value } }, "Couldn't change the role.");
    } else if (t.dataset.defaultRole) {
      collabRequest(`/api/projects/${t.dataset.defaultRole}/sharing`, { method: "PATCH", body: { default_role: t.value } }, "Couldn't change the role.");
    }
  });
})();

# Gihigugma — Local AI Research Assistant

A research assistant that runs on your own computer with Ollama. You create a
research project, add documents, and ask questions; Gihigugma answers only from
those documents and shows the passages it used. No cloud AI, no API keys.

Stack: Flask (Python) backend, plain HTML/CSS/JavaScript frontend, SQLite for storage.

(The project was first called Eros. The folder name, the `EROS_*` settings and the
`eros.db` data file keep that name so existing setups and data carry on working.)

## What it does

| Area | What you get |
|---|---|
| **Accounts** | Each person signs in with their own account. Their projects, conversations and memories are private to them. |
| **Chat** | Questions answered from your project's documents only, with numbered citations and the original passages. A general-knowledge mode is available and labelled. Conversations are stored on the host and searchable. |
| **Projects** | Each project keeps its own documents, findings and chats. |
| **Sources** | Upload PDF, DOCX, TXT (also MD, CSV, JSON, HTML). See relevant passages, saved findings, and the exact extracted text. |
| **Combined research** | Findings from all documents grouped by research question. |
| **Research Tools** | Extract, Summarize, Analyze (including comparing documents), Suggest. |
| **Paraphrase** | Six styles, three strengths, optional instructions, side-by-side comparison, a check that numbers and citations survived. |
| **Word Counter** | Words, characters with and without spaces, sentences, paragraphs, live as you type. |
| **Memory** | Opt-in notes the AI keeps across chats (preferences, topics, terms, instructions), always approved by you. |
| **Collaboration** | Share a project with other devices on the same Wi-Fi or hotspot, with roles and invitation codes. |

Not included: OCR for scanned PDFs, reading websites, a Chrome extension, export of reports.

## Folder structure

```
eros/
├── README.md
├── frontend/
│   ├── index.html      # Layout: sidebar, chat, views, dialogs
│   ├── style.css       # Theme tokens, components, responsive rules
│   ├── script.js       # Core: API layer, chat, projects, sources, findings, research tools
│   ├── modules.js      # Paraphrase, word counter, memory, collaboration screens
│   ├── textstats.js    # Word-counting rules (also used by the tests)
│   └── logo.png
├── backend/
│   ├── app.py          # Flask app: serves the page and /api; --lan turns on network mode
│   ├── config.py       # Settings (all overridable by environment variables)
│   ├── requirements.txt
│   ├── data/eros.db    # All data (created on first run); data/secret.key is created beside it
│   ├── routes/
│   │   ├── health.py       # Server, Ollama and model status
│   │   ├── auth.py         # Register, sign in, sign out, who am I
│   │   ├── conversations.py # Each user's conversations and messages
│   │   ├── chat.py         # Chat: saves the question, reads history from the database, streams, saves the reply
│   │   ├── projects.py     # Projects, relevance, findings, chats
│   │   ├── documents.py    # Upload (streamed progress), list, extracted text, delete
│   │   ├── tools.py        # Research tools, streamed
│   │   ├── paraphrase.py   # Paraphrasing, streamed
│   │   ├── memory.py       # Memories, settings, chat search
│   │   └── collab.py       # Sharing, joining, participants
│   └── services/
│       ├── ollama_client.py   # The only code that talks to Ollama
│       ├── access.py          # Who may call what: session, CSRF, project roles
│       ├── auth.py            # Password hashing (Argon2id), input rules, session ids, rate limits
│       ├── documents.py       # Validation, text extraction, cleaning, chunking
│       ├── retrieval.py       # Embedding and passage matching
│       ├── prompt_builder.py  # Chat prompts
│       ├── research_tools.py  # Tool planning and prompts
│       ├── paraphrase.py      # Styles, splitting long text, fact check
│       ├── store.py           # SQLite storage and similarity search
│       ├── store_extras.py    # Settings, memory, chat search, shared workspaces
│       └── store_auth.py      # Users, sessions, conversations, messages
└── tests/
    ├── test_backend.py     # 79 backend tests (no model or network needed)
    ├── test_textstats.js   # 25 word-counter accuracy cases
    └── fakes.py            # A stand-in for Ollama used by the tests
```

## Setup

### 1. Install Ollama and the two models (needs internet once)

Download Ollama from https://ollama.com/download and install it. Then, in a terminal:

```bash
ollama pull qwen2.5:3b       # answers, summaries, paraphrasing
ollama pull embeddinggemma   # finds the relevant passages
```

Check what you have with `ollama list`. A larger chat model such as `qwen3.5:4b`
gives better summaries and analysis if your computer can run it.

### 2. Install the Python packages (needs internet once)

Python 3.9 or newer.

```bash
cd eros/backend
python -m venv .venv
# Windows:      .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Start it

```bash
python app.py
```

Open **http://127.0.0.1:5050**. The terminal shows whether Ollama was found and which
models are installed. Open the page through the server, not by double-clicking `index.html`.

### 4. Create the first account

The page opens on a sign-in screen. Choose **Create account** and enter a username, email
address and password (at least 8 characters).

- **The first account must be created on the host computer** (at `http://127.0.0.1:5050`).
  It takes over everything saved before accounts existed: your projects, documents, chats and memories.
- After that, anyone who can reach the server can create their own account. A new account
  starts empty and sees nobody else's work.
- The email address is only used as a second way to sign in; nothing is ever sent to it.
- There is no "forgot password" (that would need email). See *Resetting a password* below.

To always use one chat model, set `EROS_DEFAULT_MODEL=qwen2.5:3b` before starting,
or pick it in the model menu (the choice is remembered in that browser).

## Using it

0. **Sign in.**
1. **Projects** → *New project*. Give it a topic and, if you like, research questions.
2. **Sources** → *Add documents*, or attach files to a chat message.
3. **Chat** → ask. With *Project documents only* (the default) the answer is written
   only from passages in your documents, cited `[1]`, `[2]`. Open *Source evidence* under
   a reply to read the passages, save them as findings, or jump to the source.
4. **Research Tools**, **Paraphrase**, **Word Counter**, **Memory**, **Collaboration**: see below.

### How answers stay inside your documents

- The question is matched against your documents first. If nothing is similar enough, the
  model is not asked at all and you get a fixed "not found in your documents" reply.
- Otherwise the model receives only the matching passages, with strict instructions to use
  nothing else, to cite every statement, and to do only what was asked.
- "Tell me the contents" or "summarise this" reads the document through instead of searching.
- **A file you just attached is the subject of that chat.** Older files in the project are not
  mixed in or compared unless you ask ("compare both files", "summarise flood.pdf"). If the
  new file has nothing on a question and an older file clearly does, the older file is used.
- Text inside documents is treated as quoted material; instructions written in a document
  are not followed.
- Citations are checked after the reply, and a reply that cites nothing or cites a source
  that wasn't provided is flagged.

A small local model can still misread a passage. The evidence is shown with every answer so
you can check it. **Similarity** scores say how closely a passage matches a question's wording;
they are not a measure of accuracy.

### Research tools

| Tool | What it does | Options |
|---|---|---|
| **Extract** | Facts, definitions, arguments, dates, statistics, key points, each with its source | Which kinds; something specific; pages |
| **Summarize** | Short, standard or detailed summary | Length; "only the part about…"; pages |
| **Analyze** | Themes, arguments, evidence, assumptions, strengths, weaknesses, gaps; or a comparison of documents | Aspects; compare; focus; pages |
| **Suggest** | Research questions, study topics, follow-ups, improvements to your own draft | Kinds; your draft. Documents optional |

Long documents are read a batch at a time so they fit a small model's context. Results can be
copied, saved to findings, or sent to chat for follow-up questions. In Analyze, parts labelled
*Interpretation (AI-generated)* are the model's own reading; Suggest results are AI-generated ideas.

### Paraphrase

Type or paste text, or load the extracted text of a document. Choose a style (Academic,
Formal, Simple, Concise, Expanded, Natural), how much to change (light, moderate, strong),
and optional extra instructions. The original and the rewrite are shown side by side.

- Up to 30,000 characters. Longer text is rewritten in parts of about 1,200 characters and
  put back together in order; a progress line shows the part.
- After every rewrite the numbers and citations of the original and the rewrite are compared
  mechanically. If a figure or a reference was dropped or appears only in the rewrite, you are told which.
- *Copy*, *Replace input with this*, *Regenerate*, *Ask about it in chat*, *Count words*,
  *Get suggestions to improve it*.

### Word counter

Counts are calculated in your browser as you type; nothing is sent anywhere.

| Count | Rule |
|---|---|
| Words | Runs of letters or digits. `don't`, `well-known`, `3.5`, `12,400`, `e.g.` and `U.S.` are one word each. Punctuation alone is not a word. |
| Characters | Every character except line breaks. "No spaces" also leaves out spaces and tabs. |
| Sentences | Text ending in `.` `!` `?` or `…`. Dots in abbreviations (Dr., e.g.), initials, acronyms and decimals don't end a sentence. A last line without a full stop still counts. |
| Paragraphs | Lines that contain text. Blank lines are not counted. |

The same word rule is used for the count under the chat box.

### Memory

Memory is **off until you turn it on** (Memory page → *Use memory*).

- A memory is a short note such as "The user prefers answers in bullet points". You can add,
  edit and delete memories, or delete all of them.
- With *Suggest memories* on, when a message looks like it tells the assistant something
  lasting, the local model proposes a memory and the chat asks *Save to memory* or *Don't save*.
  Nothing is saved without your answer.
- When you ask a question, saved memories that fit it (at most 5) are given to the model,
  and the reply shows which ones under *From memory*. Preferences and instructions apply to
  every question; other memories need to share a meaningful word with it.
- The *Memory on* button above the message box switches memory off for one chat.
- Passwords, email addresses, phone/card/ID numbers and similar details are refused.
- Memories are stored apart from chats and documents, and are never given to guests.

### Conversations

Every conversation is stored on the host computer, under your account. You can start a new
one, reopen and continue an old one, rename it, and delete it. Sign out and back in, restart
the server, or use another device: your conversations are there.

- The search box in the sidebar searches every message of your conversations in the project
  (full-text, word prefixes match). Click a result to open the conversation at that message.
- Hover a conversation to rename or delete it. Deleting removes its messages and search
  entries, and keeps documents, findings and memories you approved.
- A reply is saved only when the model finishes it. If you press Stop, or Ollama fails or times
  out, the partial text stays on screen but is not saved; your question is, and *Retry* answers it.
- Nobody else can open your conversations, including the owner of a shared workspace.

## Accounts and sessions: how it works

- **Passwords** are hashed with Argon2id (`argon2-cffi`) before they are stored. The password
  itself is never stored or logged.
- **Signing in** creates a random 256-bit session id. The browser gets it in a cookie marked
  `HttpOnly` (page scripts cannot read it) and `SameSite=Strict` (other sites cannot make the
  browser send it). The server stores only a keyed hash of the id, with the user, an expiry
  and the time of last use. A new id is issued at every sign-in.
- **A session lasts** up to 7 days and ends after 48 hours without use. *Sign out* deletes it
  on the server and clears the cookie.
- **Every request** is checked on the server: the user comes from the session, never from
  anything the browser sends. Requests that change data must also carry a CSRF token, and
  requests coming from other websites are refused.
- **Guessing is slowed down:** 5 wrong passwords for one account, or 20 from one device, pause
  sign-in for 5 minutes.
- **Nothing secret is kept in browser storage.** The page remembers only your chosen model and
  last project there.
- **The secret key** used to hash session ids is generated on first run into
  `backend/data/secret.key` (or taken from `EROS_SECRET_KEY`). It is not in the source code.

### What plain http does not protect

Gihigugma is served over plain `http://`. On the host computer itself (`127.0.0.1`) nothing
leaves the machine. **On a Wi-Fi network or hotspot, passwords, the session cookie and
everything you type and read travel unencrypted**, and someone on the same network who
captures traffic can read them or take over a session. `HttpOnly` and `SameSite` do not
encrypt anything; they only limit what web pages can do with the cookie.

- Use network mode only on a network you control (your own hotspot, with a password).
- Don't reuse an important password for a Gihigugma account.
- For real protection put HTTPS in front of it: a local reverse proxy such as Caddy or nginx
  with a certificate the devices trust, forwarding to `127.0.0.1:5050`. Then set
  `EROS_COOKIE_SECURE=1` so the cookie is only ever sent over HTTPS. (When a request does
  arrive over HTTPS the cookie is marked `Secure` automatically.)

### Resetting a password

There is no self-service reset. On the host computer, stop the server and run this from
`eros/backend` (it asks for the new password without showing it):

```bash
python -c "import getpass, sqlite3, time; from services.auth import hash_password; u = input('username: ').strip().lower(); p = getpass.getpass('new password: '); db = sqlite3.connect('data/eros.db'); n = db.execute('UPDATE users SET password_hash = ?, updated_at = ? WHERE username = ?', (hash_password(p), time.time(), u)).rowcount; db.execute('DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE username = ?)', (u,)); db.commit(); print('updated' if n else 'no such user')"
```

To block an account without deleting its data, set `is_active` to 0 for that user in the
`users` table; its sessions stop working at once.

## Collaboration over a local network

Other devices use the AI on your computer through your Wi-Fi or a mobile hotspot.
No internet is needed.

**On the host computer**

1. Start in network mode: `python app.py --lan` (default is this computer only).
2. The terminal and the Collaboration page show the address for other devices, for example
   `http://192.168.1.23:5050`. If several are listed, the first is the one for your current network.
   (You can also find the address with `ipconfig` on Windows.)
3. The first time, Windows Firewall asks whether to allow Python. Allow it for **Private**
   networks only. Do not use network mode on public Wi-Fi.
4. To work on something together, open Collaboration, press *Share* on one of your projects,
   and give people the address and the 8-character invitation code.

**On another device** (phone, tablet, laptop on the same network)

Open the address in a browser, create an account (or sign in), then open Collaboration and
enter the invitation code.

**What is shared and what is not**

| In a shared workspace | Shared with participants? |
|---|---|
| Documents and their extracted text | Yes |
| Saved findings and notes on them | Yes |
| Conversations | **No.** Each person's conversations are private, even here |
| Memories and settings | **No** |
| Your other projects | **No** |

**Roles**

| Role | Can do |
|---|---|
| Viewer | Read documents and findings, ask questions about them |
| Contributor (default) | Also upload documents and save findings |
| Manager | Also delete documents and findings |
| Owner (who created the project) | Everything, plus sharing, roles, removing people |

The owner can change a participant's role, remove them (their access ends at once), make a
new invitation code, or stop sharing (everyone loses access). Devices notice documents and
findings added by others within about five seconds.

**If a device can't connect:** check both are on the same network; check the firewall allowed
Python on Private networks; some guest Wi-Fi networks block devices from reaching each other
(use a hotspot instead); make sure the server is still running. Open the host by its IP
address, not by a computer name.

## Working offline

After the models and Python packages are installed, nothing needs the internet:

- The page loads only its own files (no CDNs or web fonts), and the server sends a
  Content-Security-Policy that stops the browser from contacting anything else.
- The server talks only to Ollama at `127.0.0.1:11434`.
- Ollama runs installed models without downloading anything.
- Accounts, password hashing, sessions and conversation storage all run on the host computer:
  no identity provider, no cloud database, no email.

Install everything while you are still online: `pip install -r requirements.txt` (this now
includes `argon2-cffi` for password hashing) and the two `ollama pull` commands.

To check: disconnect from the internet (or use airplane mode with Wi-Fi off) and use the app;
or open the browser's developer tools (F12 → Network) and confirm every request goes to
`127.0.0.1:5050`. Things that would need internet: installing or updating models and packages.

## Tests

```bash
cd eros
python -m unittest discover -s tests -v     # 79 backend tests, about 10 seconds, no Ollama needed
node tests/test_textstats.js                # 25 word-counter cases (needs Node.js)
```

The backend tests use a stand-in for Ollama, so they check the application logic (accounts,
sessions, who can reach what, what is sent to the model, storage, validation), not the
quality of a model's answers.

## Your data and backups

Everything is on the host computer in `eros/backend/data/`:

| File | Holds |
|---|---|
| `eros.db` | Accounts (with password hashes), sessions, projects, extracted document text, findings, conversations and messages, memories, workspace participants |
| `secret.key` | The key session ids are hashed with |

Uploaded files themselves are not kept, only their text. The folder is outside the folder the
web server serves, so neither file can be downloaded through the app.

- **Back up:** stop the server and copy the whole `data` folder somewhere safe. Treat the copy
  as private: it contains everyone's conversations.
- **Restore:** stop the server and put the folder back.
- **Start fresh:** stop the server and delete the folder.
- **Updating the app keeps your data.** New tables and columns are added to the existing file;
  nothing is deleted. Chats from before accounts existed are copied into the new conversation
  tables (the old `chats` table is left in place) and belong to the first account created.
- Chats that only ever existed in a browser tab of a very early version, and were never saved
  to the host, cannot be recovered.

## Settings (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `EROS_PORT` | `5050` | Server port |
| `EROS_LAN` | off | `1` is the same as starting with `--lan` |
| `EROS_OLLAMA_URL` | `http://127.0.0.1:11434` | Where Ollama runs |
| `EROS_DEFAULT_MODEL` | first installed | Chat model used when none is picked |
| `EROS_EMBED_MODEL` | `embeddinggemma` | Model used for semantic search |
| `EROS_DATA_DIR` | `backend/data` | Where the database is stored |
| `EROS_MIN_SCORE` | `0.25` | Passages below this similarity count as "not about the question" |
| `EROS_TOP_K` | `5` | Passages given to the model per question |
| `EROS_MAX_UPLOAD_MB` | `25` | Largest accepted file |
| `EROS_TOOL_MAX_SECTIONS` | `120` | Largest selection a research tool reads in full |
| `EROS_PARAPHRASE_MAX_CHARS` | `30000` | Longest text accepted for paraphrasing |
| `EROS_PARAPHRASE_PART_CHARS` | `1200` | Size of the parts long text is rewritten in |
| `EROS_MEMORY_LIMIT` | `5` | Memories given to the model per question |
| `EROS_SECRET_KEY` | generated | Key for hashing session ids. Empty = created in `data/secret.key` |
| `EROS_SESSION_DAYS` | `7` | Longest a sign-in lasts |
| `EROS_SESSION_IDLE_HOURS` | `48` | A session ends after this long without use |
| `EROS_COOKIE_SECURE` | `0` | `1` = send the session cookie over HTTPS only (set when serving through HTTPS) |

Changing `EROS_EMBED_MODEL` means documents indexed with the old model are no longer
searched; delete and re-add them.

## Security notes

- **Default is local only.** The server listens on `127.0.0.1` unless started with `--lan`.
- **Accounts:** Argon2id password hashes; random session ids stored hashed; HttpOnly,
  SameSite=Strict cookie; CSRF token on every change; sign-in rate limits; sessions expire.
- **Isolation:** every endpoint except the status check and sign-in requires a session.
  Projects, conversations and memories are checked against the signed-in user on every
  request; someone else's item is answered exactly like one that does not exist.
- **Cross-site requests** are refused, and the app must be opened by IP address or `localhost`.
  No CORS headers are sent, so other origins cannot read responses.
- **Uploads:** type and content are checked, size is limited, file names are reduced to their
  base name, and files are parsed in memory and never written to disk or executed.
- **Database:** all values are passed as query parameters. The database and the secret key are
  not reachable through the web server.
- **Logs:** the server logs request paths and status codes only. Passwords, session ids and
  message text are never in a URL, so they are not logged.

**Limitations and recommended improvements**

- **No encryption in transit** (see *What plain http does not protect*). Add HTTPS for any
  network you don't fully control.
- **Open registration in network mode:** anyone on the network can create an account and use
  the host's AI. They cannot see other people's data, but they use the host's computing time.
  If that matters, run network mode only while you need it.
- **The database is not encrypted on disk.** Anyone with access to the host computer's files
  can read all conversations. Use the operating system's disk encryption and account lock.
- **Rate limits are kept in memory** and reset when the server restarts.
- **No email verification or password reset by email** (both would need internet).
- **The built-in Flask server** is meant for a handful of users on a local network, not the internet.
- **A small model can still be wrong.** Check answers against the evidence shown.

## Troubleshooting

| Problem | What to do |
|---|---|
| "This page is out of date" | Reload the page (your session's security token changed) |
| Asked to sign in again | Sessions end after 7 days, or 48 hours without use |
| "Too many failed attempts" | Wait 5 minutes, or restart the server |
| "Gihigugma server offline" | Start `python app.py` in `eros/backend` and open the address it prints |
| "Ollama offline" | Open the Ollama app or run `ollama serve` |
| "No models installed" | `ollama pull qwen2.5:3b` |
| Documents can't be read | `ollama pull embeddinggemma` |
| A PDF says no readable text | It is scanned or image-only; OCR is not included |
| Old behaviour after an update | Restart the server and hard-refresh the page (Ctrl+F5) |
| The first reply is slow | The model is loading into memory; later replies are faster |
| Answers are weak or wrong | Try a larger model, name the file you mean, or ask a more specific question |

## API overview

| Endpoint | Purpose |
|---|---|
| `GET /api/health` | Server, Ollama, chat models, embedding model |
| `POST /api/auth/register`, `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/auth/me` | Accounts and sessions |
| `GET/POST /api/conversations`, `GET/PATCH/DELETE /api/conversations/<id>` | The signed-in user's conversations |
| `POST /api/conversations/<id>/messages`, `PATCH /api/conversations/<id>/messages/<n>` | Notes added to a conversation |
| `GET/POST /api/projects`, `GET/PATCH/DELETE /api/projects/<id>` | Projects (own, and joined workspaces) |
| `POST /api/projects/<id>/documents` | Upload one file (NDJSON progress) |
| `GET /api/projects/<id>/documents`, `GET/DELETE /api/documents/<id>` | Documents and extracted text |
| `GET /api/projects/<id>/relevance` | Passages matching the topic and questions |
| `GET/POST /api/projects/<id>/findings`, `PATCH/DELETE /api/findings/<id>`, `POST /api/findings/<id>/explain` | Findings |
| `GET /api/projects/<id>/search?q=` | Search messages and chat titles |
| `POST /api/chat` | `{ message, conversation_id, project_id, model, context, focus_document_ids }`, NDJSON stream. Saves the question and, when finished, the reply |
| `POST /api/tools/run` | `{ tool, project_id, document_ids, model, options }`, NDJSON stream |
| `POST /api/paraphrase` | `{ text, style, strength, instructions, model }`, NDJSON stream |
| `GET/PUT /api/settings`, `GET/POST/DELETE /api/memories`, `PATCH/DELETE /api/memories/<id>`, `POST /api/memories/suggest` | Memory |
| `GET /api/collab/me`, `POST /api/collab/join`, `POST /api/projects/<id>/leave` | Network details; joining and leaving a workspace |
| `PATCH /api/projects/<id>/sharing`, `GET /api/projects/<id>/members`, `GET /api/projects/<id>/changes`, `PATCH/DELETE /api/members/<id>` | Sharing and participants |

Every endpoint except `/api/health` and `/api/auth/*` needs a signed-in session (401 otherwise), and every request that changes something needs the `X-CSRF-Token` header.
Errors come back as JSON `{ error: { code, message } }`.

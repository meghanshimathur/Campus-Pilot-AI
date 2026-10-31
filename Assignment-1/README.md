# CampusPilot AI — Intelligent Student Support Agent

Assignment 1 for *Agentic AI Engineering*: **one working agent** that answers student questions by deciding, per question, whether to answer directly, call a tool, retrieve handbook policy, or use remembered information — with a chat dashboard, execution traces and an evaluation.

All data (students, courses, handbook, …) is **fictional**. The project is Python (FastAPI) + vanilla HTML/CSS/JS. There is no LangChain, React or Streamlit.

> **Honest status.** Everything is tested with a scripted stand-in for the LLM and the real local embedding model, and **one real Gemini round trip has now been observed** (`gemini-3.6-flash` called `check_fee_status` and answered from its result: 2578 input / 269 output tokens, 3.4 s). That is a single request, and a first live evaluation attempt was cut short by Google's quota errors (HTTP 429): only 3 of its 13 runs reached the model (a fee question, a scholarship question and a handbook policy question, all answered correctly), so it is **not** a quality measurement (read with the current rules: 3 completed runs, all passed, and 10 runs blocked by quota; three runs say very little).
>
> Beyond those few requests: real-model accuracy, tool-choice quality on the other scenarios, memory behaviour and the "do not invent policy" instruction are still **unmeasured** — see [Evaluation](#evaluation) for exactly what is and is not known, and [Running a live evaluation](#running-a-live-evaluation) to produce them.

---

## What it demonstrates (Assignment 1 checklist)

| Requirement | Where it lives |
|---|---|
| Agent loop that decides when a tool is needed and uses the result before answering | `campuspilot/agent.py` |
| At least two useful tools (here: 9) | `campuspilot/tools.py`, `campus_tools.py`, `tool_registry.py` |
| Persistent memory across sessions; temporary/sensitive info is not stored | `campuspilot/memory.py` (SQLite) |
| Retrieval from one handbook; only relevant chunks reach the model | `campuspilot/rag.py` (ChromaDB), `data/handbook/student_handbook.md` |
| Context selection: current question + only what is needed | `Agent._build_context` (recent turns of this session, this student's saved preferences, tool/handbook results on demand) |
| Missing information, tool errors, unanswerable questions | tools return `{"error": …}`; handbook returns "not found"; agent has step cap and safe fallbacks |
| Trace per run: tools, evidence, latency, tokens | `campuspilot/tracing.py` → SQLite `traces`, `GET /api/traces/{id}` |
| Accuracy, tool appropriateness, latency, tokens, evidence | `campuspilot/evaluation.py`, `scripts/run_evaluation.py` |
| Chat-style interface | `frontend/` (served at `/`) |
| Sample data / policy document | `data/*.json`, `data/handbook/student_handbook.md` |
| Evaluation table | [below](#evaluation) and `reports/` |

Demo scenarios from the brief and how to try them in the dashboard:

1. *"What is my current fee status?"* — tool call (`check_fee_status`).
2. *"The payment portal failed. Can I pay on the next working day?"* — handbook retrieval (section 2).
3. *"Please call me Dev from now on."* then **New session** and ask something — saved preference is recalled across sessions (Memory tab).
4. A sensitive or temporary statement (e.g. a bank account number, "just for today …") — refused by the memory validator and redacted from stored history.
5. *"Where can I park my car on campus?"* — handbook has nothing relevant → "not found", no invented policy.

---

## Architecture

```mermaid
flowchart LR
    subgraph Browser["Browser — frontend/ (HTML + CSS + vanilla JS)"]
        UI["Chat · Evidence · Agent activity · Memory · Evaluation"]
    end

    subgraph API["FastAPI — campuspilot/api.py"]
        CHAT["POST /api/chat"]
        OTHER["GET /api/traces/{id}<br/>GET · DELETE /api/memory/…<br/>GET /api/evaluation/reports[/…]<br/>GET /api/health + direct tool endpoints"]
    end

    subgraph Core["Agent core"]
        AGENT["agent.py<br/>reason → tool calls → answer<br/>(step cap, safe fallbacks)"]
        REG["tool_registry.py<br/>schemas + safe dispatch<br/>student_id injected by server"]
        PROV["provider.py<br/>Gemini (primary) · OpenAI (optional)"]
    end

    subgraph Tools["Tools"]
        T1["fees · scholarship · support request"]
        T2["courses · office hours · rooms · events"]
        T3["search_handbook"]
        T4["remember_preference"]
    end

    DATA[("data/*.json<br/>fictional records")]
    RAG["rag.py<br/>ChromaDB + keyword coverage"]
    HB[("student_handbook.md")]
    MEM["memory.py"]
    TRC["tracing.py"]
    SQL[("SQLite<br/>memories · conversations<br/>support_requests · traces")]
    REPORTS[("reports/*.json<br/>evaluation results")]

    UI --> CHAT & OTHER
    CHAT --> AGENT
    AGENT <--> PROV
    AGENT --> REG --> T1 & T2 & T3 & T4
    T1 & T2 --> DATA
    T3 --> RAG --> HB
    T4 --> MEM
    AGENT --> MEM
    AGENT --> TRC
    MEM & TRC --> SQL
    T1 --> SQL
    OTHER --> SQL & REPORTS
```

### One request, step by step

1. The browser sends `{message, student_id, session_id?}` to `POST /api/chat`.
2. `Agent` builds the context: system prompt, **this student's** saved preferences (as data, not instructions), the last few turns of **this session**, and the new question. It never sends the handbook.
3. The LLM either answers or asks for tools. Each call goes through `ToolRegistry.execute`, which validates arguments, injects the student ID from the request (the model cannot choose whose records to read), catches every exception and returns `{"error": …}` instead of failing.
4. Tool results go back to the LLM, repeated up to `max_steps` (4) times, then the final text is returned.
5. The turn is saved (redacted) to conversation history, and a **sanitised trace** is written to SQLite. The response includes latency, tokens (only if the provider reported them), tools called, evidence, memory used/saved and a `trace_id`.

### Modules

| Module | Responsibility |
|---|---|
| `config.py` | Environment/`.env` settings; API keys are hidden from `repr` |
| `db.py` | SQLite schema + guarded migrations |
| `provider.py` | `LLMProvider` interface; `GeminiProvider` (google-genai, manual function calling, thought-signature round-trip) and `OpenAIProvider`; normalised responses with optional token counts |
| `agent.py` | The agent loop, context building, evidence collection |
| `tool_registry.py` | Tool schemas, dispatch, error containment |
| `tools.py`, `campus_tools.py` | Student-record tools; courses, office hours, rooms, events |
| `rag.py` | Handbook chunking, ChromaDB index, hybrid relevance rule |
| `memory.py` | Allow-listed preferences, sensitive-content screening, redacted history |
| `tracing.py` | Sanitised per-run traces |
| `evaluation.py` | Test cases, deterministic scoring, metrics, report rendering |
| `api.py` | FastAPI app, report endpoints, static dashboard |

### Tools (9)

| Tool | Purpose | Notes |
|---|---|---|
| `check_fee_status` | Fee total/paid/due/status | uses the request's student ID |
| `check_scholarship_status` | Scholarship name/amount/status | same |
| `create_support_request` | Opens a ticket (`TKT-…`) in SQLite | validated category + description |
| `search_course_catalog` | Courses by name/ID/keyword, optional department | |
| `get_professor_office_hours` | Office room and weekly hours | partial, case-insensitive name |
| `check_room_availability` | Bookings on a weekday; optional time-window check | 24h `HH:MM`, validated |
| `get_campus_events` | Upcoming events by category/date | |
| `search_handbook` | Relevant handbook excerpts with chunk ids, or `found=false` | never returns the whole handbook |
| `remember_preference` | Saves one lasting, non-sensitive preference | five allowed keys; see below |

### Handbook retrieval

The handbook is split into section-aware chunks (14 chunks, ids like `student_handbook.md#s2-1`) and indexed in ChromaDB with a **local** embedding model (Chroma's default MiniLM; no API key). A chunk is returned only if the relevance rule accepts it, at most 3 per question. The rule combines **semantic similarity** with **keyword coverage** (how much of the question's information-weighted vocabulary appears in the chunk):

* similarity ≥ 0.50 on its own, or
* ≥ 2 distinctive matched terms, coverage ≥ 0.60 and similarity ≥ 0.10 (rescues exact-term queries the embedding dilutes), or
* ≥ 1 distinctive term, similarity ≥ 0.40 and coverage ≥ 0.18.

If nothing is accepted the tool answers `found=false` with an instruction not to invent a policy, and the agent says the information was not found. Each returned chunk carries its similarity, keyword coverage and the reason it matched. A single global similarity threshold was tried first and failed in both directions (see [Evaluation](#evaluation)).

### Memory and privacy

* Only five keys can be stored across sessions: `preferred_name`, `preferred_language`, `preferred_contact_channel`, `study_preference`, `interested_topic`.
* Values are screened **in code**, not by trusting the model: credentials/OTP/PIN, card/bank/UPI/identity details, long digit runs, email addresses, health terms, money amounts, temporary phrases ("today", "this week") and instruction-like text are rejected.
* Conversation history is stored per student **and** session; a message that looks sensitive is replaced with `[redacted: …]` before it is written. Tool outputs are never stored.
* Traces store a *summary* of each tool call (outcome, field names, chunk ids, ticket id), never raw tool outputs; LLM errors are stored as the exception type plus, for recognised API errors only, the HTTP status and a short message with key-like text masked (any other exception: type name only), so keys cannot leak.
* The dashboard keeps its state in memory only (nothing in browser storage) and inserts all dynamic text via `textContent`.

---

## Quick start

**Prerequisites:** Python 3 with `venv` (developed and tested on Python 3.14.2 / macOS; other versions are untested). Optional: Node.js and Google Chrome, only for the browser tests.

```bash
git clone <this repo> && cd CampusPilot-AI
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

**Configure a key** (needed only for `/api/chat`; everything else works without one):

```bash
cp .env.example .env
# edit .env and set GEMINI_API_KEY=...   (or OPENAI_API_KEY=... and LLM_PROVIDER=openai)
```

Never commit `.env` or paste keys into the code — `.env` is git-ignored and the app never prints keys.

**Run:**

```bash
.venv/bin/python main.py
```

Open **http://127.0.0.1:8000/** for the dashboard (interactive API docs at `/docs`).

* The Student ID box defaults to `STU001`; the fictional students are `STU001`–`STU004` (`data/students.json`).
* Without a key the page shows a "no LLM key configured" chip and chat replies with the server's own 503 message — no fake answers. The memory and trace views, the Evaluation tab and the offline evaluation still work.
* The first handbook search (or `scripts/ingest_handbook.py`) downloads Chroma's default local embedding model (~80 MB) once to `~/.cache/chroma/`, so it needs internet that first time.
* To pre-build the handbook index: `.venv/bin/python scripts/ingest_handbook.py` (add `--force` to rebuild).

### Dashboard

| Tab / area | Shows |
|---|---|
| **Conversation** | Chat with tool badges, latency, tokens (only if reported), memory used/saved, retry on errors. Assistant replies are rendered as **safe Markdown** (bold, lists, code, links); your own messages stay literal text |
| **Evidence** | The handbook chunks the agent was given for the selected turn (id, similarity, keyword coverage, match reason, exact excerpt) or "nothing relevant found" — taken only from the real API response |
| **Agent activity** | The stored trace for the selected turn: action, status, latency, tokens, provider/model, each tool step and its outcome |
| **Memory** | The student's saved preferences; delete with confirmation |
| **Evaluation** | Real saved reports, clearly labelled **OFFLINE** (pipeline check) or **LIVE** (real model). The tab only *displays* report files; it never runs an evaluation, and it says so when no LIVE report exists. The server checks that each report's label matches who produced its runs (an OFFLINE report must be all scripted, a LIVE one must contain none); a mismatch is shown as a red **INCONSISTENT** warning and counted as "unverified", never as live |

### API

| Endpoint | Purpose |
|---|---|
| `POST /api/chat` | `{message, student_id, session_id?}` → reply, `session_id`, `trace_id`, tools called, evidence, memory used/saved, latency, tokens, error |
| `GET /api/traces/{trace_id}` | One sanitised execution trace |
| `GET /api/memory/{student_id}` · `DELETE /api/memory/{student_id}/{key}` | View / delete saved preferences |
| `GET /api/evaluation/reports` · `GET /api/evaluation/reports/{name}` | Saved evaluation reports |
| `GET /api/health` | Status and whether an LLM provider is configured |
| `GET /api/students/{id}/fees` · `…/scholarship` · `POST /api/support-requests` | Direct tool endpoints (no LLM) |

There is deliberately **no** endpoint to list all traces, students or conversations: there is no authentication in this assignment (see [Limitations](#limitations)).

### Configuration

| Variable | Default | Meaning |
|---|---|---|
| `LLM_PROVIDER` | auto | `gemini` or `openai`; empty = Gemini if its key is set, else OpenAI |
| `GEMINI_API_KEY` / `OPENAI_API_KEY` | — | Provider keys (only the one you use) |
| `GEMINI_MODEL` / `OPENAI_MODEL` | `gemini-3.6-flash` / `gpt-4o-mini` | Model availability changes over time: with the author's key `gemini-3.6-flash` worked, `gemini-2.5-flash` returned HTTP 404 and `gemini-3.7/3.8-flash` returned 503 "high demand" on the day of testing. `gpt-4o-mini` is unverified |
| `DATABASE_PATH` | `data/campuspilot.sqlite3` | SQLite file |
| `HANDBOOK_MIN_SCORE` / `HANDBOOK_STRONG_SCORE` | `0.40` / `0.50` | Retrieval thresholds (see above) |

---

## Evaluation

`scripts/run_evaluation.py` runs 12 fictional cases (13 agent turns): fee status, scholarship, two handbook-policy questions, two missing-evidence questions, an unknown-student tool error, memory recall across two sessions, sensitive info not stored, a direct answer, a support request and office hours. Scoring is **rule-based** (tool choice must include the expected tools and nothing outside the allowed set; checks on tool results, retrieved evidence and answer keywords) — there is no LLM judge, and keyword checks can misjudge unusual but correct wording. Each run uses a throw-away database, and a report is written to `reports/` as Markdown and JSON.

### Latest results — **OFFLINE** mode (`reports/evaluation_offline_20261022_000402.md`)

> Offline mode replaces the LLM with a script, so these numbers show that **tools, handbook retrieval, memory and tracing work end to end** — they say **nothing** about how well a real model chooses tools or words answers. Latency excludes any LLM/API time, and no tokens are reported because nothing was called.

| Measure | Result |
|---|---|
| Cases fully passed | 12 / 12 |
| Runs passed | 13 / 13 |
| Tool choice appropriate | 13 / 13 |
| Traces recorded in SQLite | 13 / 13 |
| Handbook answers citing a chunk id | 2 / 2 |
| Tool errors observed | 1 (the intended unknown-student case) |
| Latency, local pipeline only | min 0 · median 1 · mean 71 · max 654 ms (the max includes loading the embedding model on the first handbook query) |
| Token usage | not available (0 / 13 runs reported usage) |

Note that an earlier offline run scored 11/12: one failure was retrieval returning evidence for an out-of-scope question, and after that was fixed a second failure turned out to be the evaluator's own "not found" wording check, which was then corrected. So part of the 11→12 change is evaluator fixing, not only retrieval.

### Running a live evaluation

```bash
.venv/bin/python scripts/run_evaluation.py --offline  # scripted LLM, no API calls (safe)
.venv/bin/python scripts/run_evaluation.py --live     # real model; needs a key in .env; spends API tokens
```

> **Careful:** running the script with **no flag** uses the real model whenever an API key is configured (it only falls back to an explanation when no key exists). Use `--offline` if you want no API calls.

What a live run does (from the script and test cases):

| | |
|---|---|
| Cases / turns | 12 fictional cases, 13 agent turns (the memory case has two turns in two sessions) |
| API calls | About **24 model calls** if the model behaves like the script (2 per tool turn: choose the tool, then write the answer; 1 for the two direct turns). Hard upper bound **52** (4 steps × 13 turns). Requests are paced (see below); a call that gets HTTP 429 is retried up to 3 times and a 408/5xx up to 2 times, but rejected attempts return no tokens. If Google keeps refusing, the run stops after 2 consecutive blocked turns (about 8 requests over roughly 2½ minutes) instead of working through all 13 |
| Tokens (estimate) | Each call carries about 1.2k tokens of fixed prompt (system prompt + 9 tool schemas) plus the conversation: roughly **30–40k input and 3–7k output tokens in total** (thinking tokens count as output). One measured real turn used 2,578 input / 269 output tokens for 2 calls. This is an estimate, not a measurement |
| Cost | Depends on your plan and the model's current price, which this project does not know; multiply the token estimate by your rates (the free tier may cover it) |
| Time | About 2½–3 minutes with the default pacing (≈24 requests, at least 6 s apart); longer if Google returns 429/503 |
| Files written | `reports/evaluation_live_<timestamp>.md` and `.json` (the JSON contains the model's replies to the fictional test questions). Nothing else |
| Database | Uses a throw-away temporary SQLite database; your real `data/campuspilot.sqlite3` (memory, history, traces, tickets) is **not** modified. It reads the handbook index in `data/chroma/` and does not change `.env` |

**Pacing, quota handling and honest reporting.** A first live run showed that Google's quota can start rejecting requests (HTTP 429) part-way through. Live runs are therefore paced and quota-aware (offline runs ignore all of this and never touch a provider or the network):

| Option | Default | Meaning |
|---|---|---|
| `--min-interval SEC` | 6 | At least this long between the start of two model requests, retries included (6 s = at most 10 per minute; 0 disables) |
| `--quota-retries N` / `--quota-base-delay SEC` | 3 / 10 | Waits after an HTTP 429 double each time: 10, 20, 40 s. The server's own retry hint (Retry-After header, `retryDelay`, "please retry in 34s") is honoured and never undercut |
| `--max-wait SEC` | 90 | Longest single wait. If the server asks for longer (for example a daily quota: "retry in hours") the run does **not** wait it out |
| `--stop-after N` | 2 | After N consecutive provider-blocked turns the remaining turns are **skipped** rather than hammering a provider that is refusing us |

In the report, every run ends as **completed** (it reached the model and finished: PASS or genuinely FAIL), **blocked by provider** (HTTP 429 quota, 408/5xx outage, connection error, or 401/403/404 key/permission/model problem) or **skipped** (not run). Success rates, tool-choice rates, latency and token statistics use **only completed runs**; blocked and skipped runs are listed separately and are never counted as agent failures. Errors that may be the project's own fault (HTTP 400, unexpected exceptions, `max_steps_exceeded`) still count as agent failures. If no run completed, the report says the evaluation measured nothing about the agent. The dashboard shows the same split (banner, "Runs completed", "Blocked or not run", BLOCKED/SKIPPED badges). Reports saved before this change are read with the same rules at display time (the saved file is never rewritten).

Exit codes: **0** every run passed · **1** a completed run failed · **2** no provider configured · **3** inconclusive (the provider blocked some runs and none of the completed ones failed).

Without a configured key the script refuses to run live and exits with an explanation instead of faking results. Live reports are labelled LIVE in the dashboard and include real latency (end-to-end, including the API) and provider-reported token usage. It is one run per case on a non-deterministic model, so treat rates as indicative, and read any failure before trusting it: the checks are keyword-based and can reject a correct answer worded differently.

### Retrieval quality (real embedding model; `scripts/evaluate_retrieval.py`)

The evaluation exposed a retrieval weakness (a query about "student visa extension" — a topic the handbook never mentions — returned a chunk because of the generic word "student", while a literal "re-evaluation of answer script" query scored too low). The relevance rule was changed from a single similarity threshold to the hybrid rule above. Labelled queries (in-scope answered / out-of-scope rejected), before → after:

| Query set | In-scope answered | Out-of-scope rejected | Provenance |
|---|---|---|---|
| design (32 + 25) | 27 → **32** / 32 | 22 → **25** / 25 | looked at while designing: **in-sample** |
| held-out 1 (12 + 12) | 10 → 10 / 12 | 10 → 11 / 12 | informed one tuning step |
| held-out 2 (14 + 13) | 13 → 13 / 14 | 10 → 10 / 13 | informed one tuning step |
| held-out 3 (12 + 12) | 11 → 11 / 12 | 10 → 10 / 12 | informed one tuning step |
| held-out 4 (12 + 12) | 10 → 10 / 12 | 7 → 7 / 12 | scored once after the rules were frozen |

Honest reading: the change removes the reported failure class and caused no measured loss, but on genuinely unseen queries it is **no better** than the old rule (held-out 4). Adjacent-topic questions — where the handbook covers the topic but not the fact asked ("Is smoking allowed in the hostel?") — still return chunks; preventing a wrong answer then depends on the LLM obeying the prompt ("answer only from the excerpts"), which is unverified with a live model. Three strict `xfail` tests document this.

---

## Troubleshooting LLM errors

When a reply fails, the chat shows a generic apology and an **agent error** badge; the same detail is stored in the trace and logged on the server as a warning (always key-masked).

| Error text | Meaning | What to do |
|---|---|---|
| `ClientError 404 NOT_FOUND: … no longer available to new users` | The configured model is closed to new keys | Set `GEMINI_MODEL` in `.env` to a current model (Google's message names one) and restart |
| `ClientError 400/401/403 …` | Bad key, key not allowed for this API, or a malformed request | Check the key in Google AI Studio; make sure it is in `.env` (not `.env.example`) |
| `ServerError 503 UNAVAILABLE: … high demand` / `429 RESOURCE_EXHAUSTED` | Temporary overload or rate limit | The app already retries these twice (after 1 s and 2 s); if it still fails, try again later or another model |

Transient failures (HTTP 408/429/500/502/503/504 and connection errors) are retried automatically; 4xx client errors are not, because retrying cannot fix them.

## Tests

```bash
.venv/bin/python -m pytest -q                      # backend, retrieval, evaluation, static audits, browser tests
node --test tests/frontend/*.test.mjs              # pure frontend modules (built-in Node runner, nothing to install)
```

Latest full run, freshly re-run offline on 2026-10-28: **423 passed, 0 failed, 1 skipped, 3 xfailed** (+ 41 Node tests passed, 0 failed, 0 skipped; `pytest --collect-only` lists 427 tests). The skip is the opt-in live Gemini test (`tests/test_live_gemini.py`); it makes real API requests, so it runs only with `RUN_LIVE_TESTS=1` **and** a key — a plain `pytest` never calls the API; the xfails are the documented retrieval limitation. The suite takes about 30 s because retrieval tests load the real local embedding model. The two browser tests drive headless Chrome against a test server (`tests/e2e/demo_server.py`: the real app with a clearly named **scripted** LLM, or with all keys stripped) and are skipped if Node or Chrome is missing. No test can make a live API call.

---

## Project structure

```
CampusPilot-AI/
├── main.py                    # uvicorn entry point (dashboard + API)
├── requirements.txt  .env.example  .gitignore
├── AGENTS.md  CLAUDE.md       # rules for AI coding assistants (CLAUDE.md only points to AGENTS.md)
├── PROJECT_STATE.md           # detailed build log: decisions, verified results, limitations
├── campuspilot/               # agent, tools, retrieval, memory, tracing, evaluation, API
├── frontend/
│   ├── index.html
│   ├── css/  base.css · layout.css · components.css
│   └── js/   api · format · dom · markdown · state · chat · evidence · activity · memory · evaluation · app
├── data/
│   ├── students.json courses.json professors.json rooms.json events.json    # fictional
│   ├── handbook/student_handbook.md                                          # fictional policy document
│   ├── retrieval_queries.json                                                # labelled retrieval test queries
│   └── (campuspilot.sqlite3, chroma/ — generated, git-ignored)
├── scripts/  ingest_handbook.py · run_evaluation.py · evaluate_retrieval.py
├── reports/                   # saved evaluation reports (.md + .json)
└── tests/                     # pytest suite, tests/frontend (Node), tests/e2e (browser)
```

`PROJECT_STATE.md` records every phase, the exact verification performed, and what remains unverified.

---

## Limitations

* **Live-model evidence is still very limited.** Only one real Gemini round trip and 3 completed live evaluation runs exist (fee, scholarship and handbook-policy questions, all passed); the other 10 runs of that evaluation were blocked by Google's HTTP 429 quota errors, so they say nothing about the agent. Three runs are not enough to claim broad accuracy, latency or token-usage validation, and how a real model follows the "do not invent policy" instruction, uses memory and handles the other scenarios is unmeasured. `gemini-3.6-flash` is the only model verified to generate with the author's key; `gpt-4o-mini` is unverified.
* **No authentication** (that is Assignment 2). Anyone who supplies a `student_id` can read or delete that student's memory and receive their records through `/api/chat`; this is why only low-risk preferences are stored and why there are no list-all endpoints. Do not expose this server publicly.
* **Rule-based safety screening is not exhaustive.** Unusual phrasings of sensitive data can slip through, and harmless text can be redacted (assistant replies containing money amounts are stored as the redaction placeholder, which weakens follow-up context after fee answers).
* **Retrieval:** adjacent-topic questions and vocabulary-mismatch paraphrases ("complaint" vs "grievance") are not handled by the scores alone (see above).
* **Markdown in replies** is a deliberately small, safe subset (bold/italic, lists, headings, code, quotes, links to http/https/mailto). Tables are shown as monospaced text, images are never loaded, and raw HTML is shown as literal text.
* **Frontend** was tested in Chrome only and not with a real screen reader. It shows only the current page session's messages (a reload starts a new session; server-side history is kept per student and session).
* Memory and history have no expiry; event dates in the sample data are fixed (Oct–Dec 2026), so "upcoming" events will eventually all be in the past.
* Developed and tested on one machine (macOS, Python 3.14.2).

# AL

Conversational analytics over the dbt Semantic Layer. A web chat app built on the Claude Agent SDK
that uses the dbt MCP server and Socratic rules to sharpen vague questions into precise
semantic-layer queries. It can also run the same question in three configurations ("arms") to show
what a governed metric and typed business context add over raw Text2SQL.

## Contents
- [Setup](#setup)
- [Start the server](#start-the-server)
- [Make targets](#make-targets)
- [The three modes](#the-three-modes)
- [Warming up before a demo](#warming-up-before-a-demo)
- [Mock mode and fallback](#mock-mode-and-fallback)
- [Configuration reference](#configuration-reference)
- [Auth](#auth)
- [Troubleshooting](#troubleshooting)
- [Development](#development)

## Setup
Prerequisites: Python 3.10+, [`uv`](https://docs.astral.sh/uv/) (provides `uvx`, which launches `dbt-mcp`),
a dbt platform account with the Semantic Layer, and either a Claude Code login or an Anthropic API key.

```bash
make install             # or: python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
# then edit .env: set DBT_HOST at minimum (see Auth below)
```

## Start the server
AL reads plain environment variables and does not load `.env` itself, so export it first. Do this
in every new terminal, from the repo root:

```bash
set -a; source .env; set +a
.venv/bin/uvicorn sparky.server:app
```
or simply `make run` (foreground) or `make start` (background).

Open http://localhost:8000.

- **Do not use `--reload` when presenting.** A code change restarts the server and throws away every
  warm session, so the next question pays the cold start again.
- On startup the server connects one session for the default mode in the background. With dbt OAuth,
  a browser window may open for the first login; finish it before the first question.
- Stop with Ctrl+C (or `make stop` if you used `make start`). Port 8000 busy? Add `--port 8001` and use that port everywhere below (`make start PORT=8001`).

## Make targets
A [Makefile](Makefile) wraps the commands in this README. It loads `.env` for you, so there is no
`source .env` step. Run `make` to list every target.

| Target | What it does |
|---|---|
| `make install` | Create `.venv`, install dependencies, create `.env` from the example |
| `make run` | Run the server in the foreground (Ctrl+C to stop) |
| `make dev` | Foreground with auto-reload (development only) |
| `make run-mock` | Foreground in mock mode: replay recorded transcripts, no model calls |
| `make start` | Start the server in the **background**, wait until healthy, log to `.sparky-<port>.log` |
| `make stop` / `make restart` | Stop, or stop then start (do this after changing `.env` or code) |
| `make status` / `make logs` | Check the server is up / follow its log |
| `make warm` | Pre-connect a session for each arm on the running server (fast, no model calls) |
| `make warmup` | Run each arm's demo question once through `scripts/warmup.py` |
| `make preflight` | `warm` then `warmup`: the pre-demo check |
| `make open-arm1` / `open-arm2` / `open-arm3` | Open that arm's page in the browser |
| `make test` / `make bench` / `make bench-cold` | Unit tests / live benchmark / benchmark without the warm pool |
| `make clean` | Remove caches, logs and benchmark output (keeps `.venv` and `.env`) |

Override settings on the command line:

```bash
make start MODE=arm2                      # default arm for the server
make start MOCK=1                         # mock mode (replay recorded transcripts)
make start PORT=8001                      # another port; pid and log files are kept per port
make warm ARMS=arm2,arm3                  # only some arms
make warmup ARMS=arm3 QUESTION="How many students are enrolled by term season?"
make open-arm3 MOCK=1                     # opens ?mode=arm3&mock=1
```

Typical demo day:

```bash
make start && make preflight && make open-arm3     # ... present ... then:
make stop
```

`make start` is idempotent: if the server is already running it says so and does nothing. Run
`make restart` to apply new settings. Targets that talk to the server use the same `PORT`, so pass it
consistently.

## The three modes
The same question runs in three configurations. The active mode is always shown as the colored pill
in the top right.

| Mode | Pill | Tools and prompt | Business context |
|---|---|---|---|
| `arm1` Text2SQL | gray | read-only `execute_sql` against the warehouse; minimal prompt, no metric definitions | none |
| `arm2` Semantic layer | maroon outline | dbt Semantic Layer tools; stripped prompt (list metrics, pick closest, query, answer); no clarifying questions | none |
| `arm3` Context-aware | gold | Semantic Layer tools, Socratic rules, clarifying questions, `cite_context` | context cards from [data/context_cards.json](data/context_cards.json) |

### Choosing a mode
There are three ways, in order of precedence:

1. **The pill.** Click it and pick an arm. This starts a brand-new chat in that arm (the previous chat stays
   in the history sidebar), so one arm's context can never leak into another arm's answer.
2. **The URL.** `http://localhost:8000/?mode=arm2` opens straight into that arm. Open one browser tab per arm
   (`?mode=arm1`, `?mode=arm2`, `?mode=arm3`) if you want to flip between them without re-typing.
3. **The server default.** `SPARKY_MODE=arm2` in `.env` sets the arm used when the URL does not specify one
   (default `arm3`).

Every conversation is tied to one mode. Sending a message to an existing session under a different mode is
rejected (HTTP 409); the UI avoids this by starting a new chat when you switch.

### Chat history

The sidebar lists past chats. They are stored in the browser only (`localStorage`, nothing on the server), so
they survive a refresh and reopen on the same machine and browser profile. **Clear history** wipes them all.
Reopening a chat lets you keep talking in it. The agent's memory of a chat lives in the server process, so
after `make restart` a reopened chat shows the transcript but the agent starts fresh; the UI says so with a
"Server restarted" note on the next answer.

### Privacy: aggregates only

AL never shows individual student identifiers (EMPLID, student id, ASURITE), names, emails or
contact details. The rules live in `src/sparky/pii.py` and are applied at four layers:

1. **Before a query runs** (PreToolUse hooks in `agent.py`): arm 1 SQL may use identifier columns only
   inside `COUNT(...)`, and `SELECT *` is refused. In arms 2 and 3, `query_metrics` and related tools can't group by,
   filter on or list values of the `student` entity or any identifier dimension. The model is told why,
   so it answers with an aggregate or explains that individual records aren't available.
2. **Before the model reads a result** (PostToolUse hook): identifier columns are dropped and ID-like
   values (10-digit ids, SSNs, emails, phone numbers) are redacted.
3. **Before anything reaches the browser** (`EventGuard` in `server.py`): every event, live or replayed
   from `demo/transcripts.yaml`, is scrubbed the same way. Tool inputs are not sent, and unexpected
   errors show a generic message (details go to the server log).
4. **In the prompts** (`rules/*.md`): the model is told to answer with aggregates only.

To block another identifier, add its column name to `_PERSON_FIELD` in `pii.py` and a case to
`tests/test_pii.py`. Small counts are not suppressed.

### Light and dark mode

The page follows the device's light/dark setting. The sun/moon button in the header overrides it, and the choice
is remembered in this browser. Toggling back to match the device clears the override.

### What each arm should look like on screen
- **Arm 1:** the model discovers tables with SQL, so it makes many `execute_sql` calls. Only the final,
  user-facing query gets a result card. Expect it to be the slowest arm and the least consistent.
- **Arm 2:** one `list_metrics`, one `query_metrics`, a short answer, and a Chart/Table/SQL result card.
- **Result cards (all arms):** each query result gets a Chart/Table/SQL card. The **chart type dropdown** offers the types that fit the
  data: bar, horizontal bar, line, area, stacked bar, pie (non-negative values, up to 12 slices) and scatter (two numeric metrics). Time
  series default to a line, categories to bars. With two dimensions and one metric, the second dimension becomes the series (for example
  term on the x-axis with one bar per program). The logic is in [web/assets/charts.js](web/assets/charts.js).
- **Arm 3:** like Arm 2, plus Socratic clarifying questions when the question is ambiguous. When a context-card field
  changed its conclusion, a dark chip with a paperclip (for example "Investigations: Fall B census offset") appears under
  the answer; click it to read the card text.
  Every Arm 3 answer ends with 2-3 **Suggested follow-ups**, rendered as buttons: click one and it is sent as your next question.
  For **comparison questions** (A versus B, "why is X higher than Y"), Arm 3 does not walk through intermediate results: it replies with one short summary
  (the compared values, the likely explanation, any comparability caveat) and its follow-ups are concrete next analyses. If a turn runs several
  queries, their cards are tucked into a collapsed "Supporting queries (N)" expander so they stay available for validation without cluttering the answer.

### Context pack (Arm 3 only)
Arm 3 loads [data/context_cards.json](data/context_cards.json) at startup. Arms 1 and 2 never see it, which keeps
them clean baselines. The real file is generated in the dbt repo from `manifest.json` and synced here (see
[data/README.md](data/README.md)); the checked-in file is a sample. Point AL at another file with
`SPARKY_CONTEXT_PACK`. Regenerate and re-sync whenever `metrics.yml` changes, or it goes stale.

## Warming up before a demo
Cold connections are the usual cause of dead air. Starting a chat means launching the Claude CLI and a
`dbt-mcp` process, and a first-time dbt OAuth login. AL hides most of that with a **warm session pool**
(one ready session per mode, refilled after each new chat) and a warm-up script that proves every arm works.

### How warming works
- On startup the server warms the **default** mode only.
- The page calls `POST /warm?mode=<arm>` when it loads and whenever you switch modes, so the arm you are about
  to use is usually ready by the time you type.
- A new chat takes the warm session instead of building one, so the first event appears in about 1-3 seconds
  instead of 8 or more.

### Pre-demo checklist
Do this 10-15 minutes before presenting. Shortcut: `make start && make preflight` covers steps 1-3.

1. **Start the server without `--reload`** (see above; `make start`) and finish any dbt OAuth browser login.
2. **Warm every arm**, not just the default:
   ```bash
   make warm        # or: for a in arm1 arm2 arm3; do curl -s -X POST "localhost:8000/warm?mode=$a"; done
   ```
   Opening each arm's tab (`?mode=arm1`, `?mode=arm2`, `?mode=arm3`) does the same thing.
3. **Run the warm-up script** in a second terminal (with `.env` exported). It runs each arm's demo question
   once, answers any clarifying question with its first option, and prints the time, the steps taken, and the
   start of the answer:
   ```bash
   make warmup                                                 # same as the next line
   .venv/bin/python scripts/warmup.py                          # all three arms, default demo question
   .venv/bin/python scripts/warmup.py --arms arm2,arm3         # only some arms
   .venv/bin/python scripts/warmup.py --question "How many students are enrolled by term season?"
   ```
   Look for `ERROR` lines or an arm with no steps; that arm is not healthy. Typical live times: Arm 2 and Arm 3
   about 25-30s, Arm 1 about 70-85s.
4. **Ask each arm's demo question once in the browser, off-camera**, so the exact conversation path you will
   show is exercised. Use the pill to move between arms (this resets the chat each time).
5. **Hard-refresh** (Cmd+Shift+R) and leave the arm you open with selected.
6. **Decide your safety net:** confirm `demo/transcripts.yaml` has an entry for each arm and question you will
   ask (see below), and know the `?mock=1` escape hatch.

Notes on what the script does and does not do:
- It starts its own sessions in a separate process. It confirms your credentials, network and the warehouse
  are working and primes the backends, but the **server's** warm sessions come from the server itself
  (steps 1-2), not from this script.
- In testing, scripts reused the existing dbt OAuth login without a new prompt after the first login, but
  confirm this on your machine before relying on it.
- The default demo question is about "Program Q" and "Program S". Those do not exist in the current dbt
  project, so live answers to it will not match the recorded ones until the star schema and metrics are built.
  Use `--question` with a question your current project can answer to check plumbing.

## Mock mode and fallback
Mock mode replays a recorded transcript with a simulated streaming delay and shows a **RECORDED** tag, so
nobody mistakes it for a live answer.

- **Force it for the whole server:** `SPARKY_MOCK_MODE=1` in `.env`. No sessions are warmed in this mode and no
  model calls are made.
- **Force it per page:** add `?mock=1`, for example `http://localhost:8000/?mode=arm3&mock=1`.
- **Automatic fallback:** if a live call returns an error before any answer content appears, or sends nothing
  within `SPARKY_LIVE_TIMEOUT` seconds (default 30), and a transcript exists for that arm and question, AL
  replays the transcript instead. The timeout applies to the **first event only**. A call that is working but
  slow (Arm 1 often is) will not fall back; use `?mock=1` if you need to avoid waiting on it.

Transcripts live in [demo/transcripts.yaml](demo/transcripts.yaml), keyed by arm and then question. A question
matches when it is the same ignoring case and punctuation. An unmatched question in mock mode shows an
"No recorded transcript" error.

```yaml
arm3:
  - question: "Why is Program Q's melt rate so much higher than Program S's this term?"
    events:
      - {type: tool_use, id: m1, name: mcp__dbt__query_metrics, input: {metrics: [melt_rate]}}
      - {type: result_table, key: k, id: m1, metrics: [melt_rate], columns: [program, melt_rate], group_by: [],
         rows: [{program: Program Q, melt_rate: 0.114}]}
      - {type: text, text: "The answer text."}
      - {type: cite, citations: [{metric: enrollment_headcount, path: investigations.known_structural_causes,
         label: Fall B census offset, text: "Card text shown when the chip is opened"}]}
```

The checked-in transcripts are **seed data from the planning mockup**. There is no recorder yet: after the real
queries run, copy the events into this file by hand and update the numbers.

## Configuration reference
Set these in `.env` (see [.env.example](.env.example)).

| Variable | Default | Purpose |
|---|---|---|
| `DBT_HOST` | `cloud.getdbt.com` | dbt platform host. For OAuth, your static-subdomain Access URL, hostname only |
| `MULTICELL_ACCOUNT_PREFIX` | empty | Multi-cell accounts: set `DBT_HOST=us1.dbt.com` and the prefix here |
| `DBT_TOKEN`, `DBT_PROD_ENV_ID` | empty | Service-token auth. Leave `DBT_TOKEN` empty to use OAuth |
| `ANTHROPIC_API_KEY` | empty | Leave empty to use your Claude Code login |
| `SPARKY_MODE` | `arm3` | Default arm: `arm1`, `arm2` or `arm3` |
| `SPARKY_MOCK_MODE` | off | `1` replays recorded transcripts for every request |
| `SPARKY_LIVE_TIMEOUT` | `30` | Seconds to wait for a first live event before falling back to a transcript |
| `SPARKY_CONTEXT_PACK` | `data/context_cards.json` | Context pack loaded by Arm 3 |
| `SPARKY_MODEL` | SDK default | Override the model |

## Auth
### Anthropic
AL drives the bundled Claude Code CLI, so it uses whatever login Claude Code has.
- **OAuth (default when `ANTHROPIC_API_KEY` is empty):** run `claude` once and `/login`, then start AL
  from a shell with the same `CLAUDE_CONFIG_DIR` (if you use one) so it finds the credentials.
- **API key:** set `ANTHROPIC_API_KEY` in `.env`. A set key takes precedence over the login.

### dbt
- **OAuth (default when `DBT_TOKEN` is empty):** set only `DBT_HOST` to your static-subdomain Access
  URL. On the first query `dbt-mcp` opens a browser on the machine running the server for login
  and project selection. Needs an Enterprise/Enterprise+ account with AI features enabled.
  The login belongs to whoever is at the server's browser, so this suits local, single-user use.
- **Service token:** set `DBT_TOKEN` and `DBT_PROD_ENV_ID` as well. Use this for a shared or deployed app.

## Troubleshooting
| Symptom | Likely cause and fix |
|---|---|
| Page shows an old design or behavior | Hard-refresh (Cmd+Shift+R); the browser cached the old script |
| "Working…" never clears | Check the terminal running uvicorn for an error; if you have a transcript, retry with `?mock=1` |
| Charts missing but tables work | Chart.js loads from cdnjs; it needs internet access. The Table and SQL views still work |
| First question is slow (8s+ to first event) | The arm was not warmed. Run the warm step above, and avoid `--reload` |
| `uvx: command not found` | Install `uv` (`brew install uv`) |
| Agent says the dbt tools are not available, or `CERTIFICATE_VERIFY_FAILED` from dbt-mcp | dbt-mcp is launched via `sparky.dbt_mcp_launcher`, which drops empty `DBT_*` values and sets `SSL_CERT_FILE` to certifi; run `.venv/bin/python -m sparky.dbt_mcp_launcher` by hand to see its startup errors |
| Browser login prompt appears mid-demo | dbt OAuth is not logged in on the server; finish the login, then retry |
| `No recorded transcript` | The question does not match an entry in `demo/transcripts.yaml` for that arm |
| Arm 1 takes over a minute | Expected: it discovers the schema with many SQL calls. Use `?mock=1` for the live demo |

## Development
```bash
.venv/bin/pytest -q                  # unit tests (no network or credentials needed; also runs the chart and chat-history checks under Node if installed)
.venv/bin/python scripts/bench.py    # live timing/cost benchmark over 6 questions; writes bench.json
.venv/bin/python scripts/bench.py --cold   # fresh session per question, no warm pool
```
Customize behavior:
- [src/sparky/modes.py](src/sparky/modes.py): the three arms (tools, prompts, which arms get context and citations)
- [src/sparky/rules/](src/sparky/rules/): `socratic.md` and `system.md` (Arm 3), `arm1.md`, `arm2.md`
- [src/sparky/config.py](src/sparky/config.py): settings and the always-blocked tool list (read-only by default)

# logscribe

[![CI](https://github.com/infoshikharpathak/logscribe/actions/workflows/ci.yml/badge.svg)](https://github.com/infoshikharpathak/logscribe/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)

Tail-based log monitoring with AI-powered error analysis using RAG.

logscribe watches a log file, catches errors as they happen, and tells you the
likely root cause by comparing the new error against semantically similar
errors it has seen before.

## Architecture

```
                    ┌─────────────┐
   log file  ─────▶ │  sampler.py │  rolling buffer (last 50 lines)
                    │             │  regex error detection
                    └──────┬──────┘
                           │ RawErrorChunk
                           ▼
                    ┌─────────────┐
                    │processor.py │  error_type, message, stack_trace,
                    │             │  key_variables, timestamp
                    └──────┬──────┘
                           │ ErrorEvent
                           ▼
              ┌────────────┴────────────┐
              │                         │
              ▼                         ▼
       ┌─────────────┐           ┌─────────────┐
       │  memory.py  │◀──similar─│ analyzer.py │
       │  ChromaDB   │  errors   │ (pluggable) │──▶ root-cause analysis
       │(store+query)│           │             │
       └──────┬──────┘           └─────────────┘
              │ first time this error_type is seen
              ▼
       ┌─────────────┐
       │ incident.py │──▶ short on-call summary (when/what/why, no deep dive)
       └─────────────┘
```

1. **sampler.py** tails one or more log files (each with its own rolling
   buffer of the last N lines and an optional service name) and flags a line
   as an error when it matches a configurable regex pattern. Structured JSON
   lines are judged by their `level` field instead (ERROR, CRITICAL, FATAL).
2. **processor.py** turns the raw window of lines into a structured
   `ErrorEvent` (error type, message, stack trace, key variables, timestamp).
   Handles both plaintext/traceback-style logs and structured JSON log lines.
3. **memory.py** embeds the event and stores it in a local ChromaDB
   collection; on each new error it retrieves the top-k most semantically
   similar past errors, each with a similarity score (cosine, 1.0 =
   identical). Only the error's identity is embedded (type, message, service,
   endpoint, the exception's last line), so boilerplate shared by unrelated
   errors doesn't decide the match. An event's own context is the failing
   request's log lines (same `request_id`, else the same service's latest
   lines), not the whole buffer, so another request's error is never taken
   as this one's cause. It can also hold **seeded incidents**:
   resolved examples with a symptom, cause and fix, shown to the analyzer
   when they match. Also tracks which `error_type`s have been seen before.
4. **analyzer.py** sends the new error plus similar past errors to an LLM for
   a root-cause hypothesis. It's defined behind an `ErrorAnalyzer` protocol,
   with two implementations: `OpenAIAnalyzer` (default, a direct chat
   completion) and `AgentForgeAnalyzer` (routes the same request through
   [agent-forge](../agent-forge)'s registered `logscribe` app — see
   "agent-forge backend" below). `build_analyzer()` picks between them based
   on the `LOGSCRIBE_ANALYZER` env var.
5. **incident.py** curates a short on-call notice — 2-4 sentences, when/what/
   likely-why, deliberately *not* a deep investigation — but only the first
   time a given `error_type` shows up. Repeat occurrences of an already-known
   error skip curation entirely, so the signal stays about genuinely new
   incidents rather than every recurrence of something already understood.

## Quick start

```bash
# from the logscribe/ directory
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install -e .

cp .env.example .env   # then fill in OPENAI_API_KEY

# tail a log file, auto-analyze errors as they happen
logscribe watch --file /var/log/myapp.log

# semantic search over previously captured errors
logscribe query "database connection pool exhausted"

# list recently captured errors
logscribe history
```

## Commands

| Command | Description |
|---|---|
| `logscribe watch --file <path>` | Tail a log file (repeat `--file` for several), capture errors, auto-analyze each one |
| `logscribe watch --config <app.toml>` | Same, for the files, service names and history a watch config names |
| `logscribe seed --config <app.toml>` | Load seeded example incidents (symptom, cause, fix) into the history |
| `logscribe query "<description>"` | Semantic search over past errors |
| `logscribe history` | List recently captured errors |
| `logscribe incidents` | List curated on-call summaries for newly-seen error types |

Run `logscribe <command> --help` for the full set of options (buffer size,
top-k, tailing from the start of the file, etc). `watch --report out.jsonl`
also appends each analysis (the error, its matches with similarity scores,
the root cause and any on-call summary) as one JSON line.

`query`, `history` and `incidents` take `--config` too, to read that
config's history instead of the default one.

## Watch configs

A TOML file names the files to tail, their service names, and the ChromaDB
collection their history lives in (so one app's errors aren't matched
against another's):

```toml
name = "ironclad"
collection = "ironclad_errors"
top_k = 5
min_similarity = 0.3          # drop weaker matches
seeds = "seed_incidents.json" # for `logscribe seed --config ...`
repo_path = "${IRONCLAD_REPO_PATH:-..}" # the app's git repo (Agent Forge analyzer)

[[sources]]
service = "bank-service"
path = "${IRONCLAD_LOG_DIR:-../logs}/bank-service.log"

[[sources]]
service = "agent-service"
path = "${IRONCLAD_LOG_DIR:-../logs}/agent-service.log"
```

Paths are relative to the config file; `${VAR}` and `${VAR:-default}` are
expanded. `repo_path` is the watched app's git repo: with the Agent Forge
analyzer, each analysis checks its recent commits and cites any it looked at.
It wins over `LOGSCRIBE_REPO_PATH`, which stays the default for watches that
don't set one. Agent Forge's git and file MCP servers must be allowed to read
that repo (`mcp_servers.json` there; the git server takes one `--repository`). A file that doesn't exist yet is picked up once it's created; a
truncated or rotated file is reread from the start. The Ironclad config and
its seeded incidents live in the Ironclad repo (`logscribe/`).

## Seeded incidents

RAG over past errors needs past errors. A seed file is a list of resolved
example incidents:

```json
{"seeded_examples": true,
 "incidents": [{"id": "postgres-down", "service_name": "bank-service",
                "error_type": "ConnectionRefusedError",
                "message": "Database error: ConnectionRefusedError: Connect call failed",
                "symptom": "Every database endpoint returns 500",
                "cause": "Postgres was stopped", "fix": "Start Postgres; the pool reconnects"}]}
```

`logscribe seed` upserts them (loading again updates, never duplicates),
marked `seeded`; watch output and reports show them as "seeded example",
and the analyzer prompt includes their cause and fix. They don't count as
"seen before" for on-call curation, so the first real occurrence of a type
still gets a card.

## Tests

```bash
pip install -e ".[test]"
pytest -q
```

No network or model download: tests use a small deterministic embedding.

## UI

A small Streamlit app (`app.py`) gives a visual way to try logscribe without
tailing a live file — paste a log snippet, watch it get detected/analyzed, and
browse or search everything already stored.

```bash
streamlit run app.py
```

Five tabs, mirroring the CLI commands:
- **Analyze** — paste raw log lines (including a traceback if you have one) and
  hit "Detect & analyze"; runs the same detection logic as `logscribe watch`
  and stores the result. Shows a curated on-call summary inline the first time
  a given error type is seen.
- **Search** — same as `logscribe query`, semantic search over past errors.
- **History** — same as `logscribe history`, a table of recently captured errors.
- **Incidents** — same as `logscribe incidents`, curated on-call summaries.
- **Agent Forge Traces** — reads agent-forge's `/traces` API directly (filtered
  to `app_id=logscribe`), so when `LOGSCRIBE_ANALYZER=agent_forge` is set you
  can see exactly how each analysis was produced — routing tier, which agents
  ran, their full output, git/filesystem tool use, cost/latency — without
  leaving logscribe's UI. Empty unless the agent-forge backend is running and
  reachable at `AGENT_FORGE_URL`.

## agent-forge backend

By default, root-cause analysis calls OpenAI directly (`OpenAIAnalyzer`). To
route it through [agent-forge](../agent-forge) instead:

```bash
# in .env
LOGSCRIBE_ANALYZER=agent_forge
AGENT_FORGE_URL=http://localhost:8000   # default, only needed if agent-forge runs elsewhere
```

With agent-forge running and its `logscribe` app registered
(`agent-forge/src/agent_forge/apps/logscribe.json`), the exact same
`event`/`similar_events` this analyzer always built its prompt from is now
sent as agent-forge's `goal`. Because the app is registered as a **locked**
app, agent-forge skips its own dynamic planning (no extra goal-clarity or
plan-generation LLM calls) and runs a single fixed root-cause-analyst agent —
cheaper and faster than dynamic planning, with the door open to grow into a
real multi-agent pipeline later purely via that config file, with zero
changes to logscribe itself.

## Current limitations / out of scope (for now)

- **Local files, one process** — `logscribe watch` tails local files by
  polling (several at once). It's not a centralized ingestion pipeline;
  services on other hosts need their logs shipped to where it runs.
- No batch/EOD mode, no resolution tracking, no alerting/PagerDuty integration.

## Roadmap / future plans

Roughly in priority order:

1. **Multi-host ingestion** — several local files are supported (`--config`);
   for many hosts or containers, front `logscribe` with a log shipper
   (Filebeat, Fluentd, or Vector) forwarding to one stream it consumes.
2. **watchdog-based tailing** — swap the polling loop in `sampler.py` for
   filesystem events (`watchdog`) if poll latency ever becomes noticeable at
   real log volumes. No API change needed — `LogSampler.tail()`'s interface
   stays the same.
3. **Resolution tracking** — seeded incidents carry a cause and fix; captured
   errors can't be marked resolved yet. Letting them (e.g. "this looks like
   issue #42, already fixed by X") would make real history as useful as the
   seeds.
4. **Alerting delivery** — the new-error-type detection and on-call curation
   already exist (`incident.py`, surfaced via `logscribe incidents` and the
   UI's Incidents tab); what's still missing is pushing that curated summary
   somewhere external — Slack/PagerDuty/email — instead of only being visible
   when someone checks the CLI/UI.
5. **Better error-type classification** — the current regex-based
   `error_type`/`stack_trace` extraction in `processor.py` is tuned for
   Python-style tracebacks; extend it (or make it pluggable per log format)
   for other languages/frameworks.

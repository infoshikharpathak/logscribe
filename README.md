# logscribe

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

1. **sampler.py** tails a log file with a rolling buffer of the last N lines
   and flags a line as an error when it matches a configurable regex pattern.
2. **processor.py** turns the raw window of lines into a structured
   `ErrorEvent` (error type, message, stack trace, key variables, timestamp).
   Handles both plaintext/traceback-style logs and structured JSON log lines.
3. **memory.py** embeds the event and stores it in a local ChromaDB
   collection; on each new error it retrieves the top-k most semantically
   similar past errors. Also tracks which `error_type`s have been seen before.
4. **analyzer.py** sends the new error plus similar past errors to an LLM for
   a root-cause hypothesis. It's defined behind an `ErrorAnalyzer` protocol so
   the direct OpenAI call can later be swapped for
   [agent-forge](../agent-forge) without touching any other module.
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
| `logscribe watch --file <path>` | Tail a log file, capture errors, auto-analyze each one |
| `logscribe query "<description>"` | Semantic search over past errors |
| `logscribe history` | List recently captured errors |
| `logscribe incidents` | List curated on-call summaries for newly-seen error types |

Run `logscribe <command> --help` for the full set of options (buffer size,
top-k, tailing from the start of the file, etc).

## UI

A small Streamlit app (`app.py`) gives a visual way to try logscribe without
tailing a live file — paste a log snippet, watch it get detected/analyzed, and
browse or search everything already stored.

```bash
streamlit run app.py
```

Four tabs, mirroring the CLI commands:
- **Analyze** — paste raw log lines (including a traceback if you have one) and
  hit "Detect & analyze"; runs the same detection logic as `logscribe watch`
  and stores the result. Shows a curated on-call summary inline the first time
  a given error type is seen.
- **Search** — same as `logscribe query`, semantic search over past errors.
- **History** — same as `logscribe history`, a table of recently captured errors.
- **Incidents** — same as `logscribe incidents`, curated on-call summaries.

## Current limitations / out of scope (for now)

- **Single file, single process** — `logscribe watch` tails one local file via
  polling. It's not a centralized ingestion pipeline; watching many
  services/hosts today means running one `logscribe watch` per file.
- No batch/EOD mode, no resolution tracking, no alerting/PagerDuty integration.
- No agent-forge wiring yet — `analyzer.py` is deliberately kept pluggable so
  that swap is drop-in later.

## Roadmap / future plans

Roughly in priority order:

1. **agent-forge as the analysis backend** — implement a second
   `ErrorAnalyzer` that calls [agent-forge](../agent-forge)'s `/run/stream`
   instead of OpenAI directly, so root-cause analysis can use
   agent-forge's orchestration (multi-agent, tool use, etc.) instead of a
   single chat completion. `analyzer.py` was built around this from day one.
2. **Multi-source ingestion** — front `logscribe` with a log shipper
   (Filebeat, Fluentd, or Vector) that tails many files/containers/hosts and
   forwards to a single stream or endpoint that `logscribe` consumes, instead
   of one `watch` process per file. Only worth doing once there's an actual
   multi-service use case — the current single-file tailer is intentional for
   the MVP.
3. **watchdog-based tailing** — swap the polling loop in `sampler.py` for
   filesystem events (`watchdog`) if poll latency ever becomes noticeable at
   real log volumes. No API change needed — `LogSampler.tail()`'s interface
   stays the same.
4. **Resolution tracking** — mark a captured error as resolved, and factor
   resolution status into what `analyzer.py` surfaces (e.g. "this looks like
   issue #42, already fixed by X").
5. **Alerting delivery** — the new-error-type detection and on-call curation
   already exist (`incident.py`, surfaced via `logscribe incidents` and the
   UI's Incidents tab); what's still missing is pushing that curated summary
   somewhere external — Slack/PagerDuty/email — instead of only being visible
   when someone checks the CLI/UI.
6. **Better error-type classification** — the current regex-based
   `error_type`/`stack_trace` extraction in `processor.py` is tuned for
   Python-style tracebacks; extend it (or make it pluggable per log format)
   for other languages/frameworks.

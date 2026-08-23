from __future__ import annotations

"""
logscribe Streamlit UI — a visual companion to the CLI for pasting log
snippets, watching them get analyzed, and browsing/searching past errors.

Run:
    streamlit run app.py
"""

import os

import httpx
import streamlit as st
from dotenv import load_dotenv

from logscribe.analyzer import build_analyzer
from logscribe.incident import OnCallCurator
from logscribe.memory import ErrorMemory
from logscribe.processor import ErrorProcessor
from logscribe.sampler import scan_lines

load_dotenv()

st.set_page_config(page_title="logscribe", page_icon="📜", layout="wide")

AGENT_FORGE_URL = os.getenv("AGENT_FORGE_URL", "http://localhost:8000")


def fetch_agent_forge_traces(
    *, limit: int = 20, app_id: str | None = "logscribe", status: str | None = None,
) -> list[dict]:
    params: dict = {"limit": limit}
    if app_id:
        params["app_id"] = app_id
    if status:
        params["status"] = status
    try:
        resp = httpx.get(f"{AGENT_FORGE_URL}/traces", params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        st.error(f"Could not reach agent-forge at {AGENT_FORGE_URL}: {exc}")
        return []


@st.cache_resource
def get_memory() -> ErrorMemory:
    return ErrorMemory()


@st.cache_resource
def get_analyzer():
    return build_analyzer()


@st.cache_resource
def get_curator() -> OnCallCurator:
    return OnCallCurator()


processor = ErrorProcessor()
memory = get_memory()
analyzer = get_analyzer()
curator = get_curator()

st.title("logscribe")
st.caption("Tail-based log monitoring with RAG-powered error analysis.")

tab_analyze, tab_search, tab_history, tab_incidents, tab_traces = st.tabs(
    ["Analyze", "Search", "History", "Incidents", "Agent Forge Traces"]
)

# ── Analyze ──────────────────────────────────────────────────────────────────

with tab_analyze:
    st.subheader("Paste a log snippet")
    raw_text = st.text_area(
        "Log lines — same detection logic as `logscribe watch` (error keywords + traceback capture)",
        height=250,
        placeholder=(
            "2026-08-15T10:00:05 ERROR Failed to process job job_id=abc123\n"
            "Traceback (most recent call last):\n"
            '  File "worker.py", line 42, in process\n'
            "    conn = pool.get(timeout=5)\n"
            "ConnectionError: connection pool exhausted"
        ),
    )
    analyze_top_k = st.slider("Similar errors to retrieve", 1, 10, 5, key="analyze_top_k")

    if st.button("Detect & analyze", type="primary"):
        if not raw_text.strip():
            st.warning("Paste some log lines first.")
        else:
            chunks = scan_lines(raw_text.splitlines())
            if not chunks:
                st.info("No error patterns matched in the pasted text.")

            for chunk in chunks:
                event = processor.process(chunk)
                similar = memory.query_similar(event.to_document(), top_k=analyze_top_k)

                with st.container(border=True):
                    st.markdown(f"**{event.error_type}** — {event.timestamp}")
                    st.code(event.message, language=None)

                    if event.stack_trace:
                        with st.expander("Stack trace"):
                            st.code(event.stack_trace, language="python")

                    if event.key_variables:
                        st.json(event.key_variables)

                    with st.spinner("Analyzing..."):
                        analysis = analyzer.analyze(event, similar)
                    st.markdown("**Root-cause analysis**")
                    st.markdown(analysis)

                    extra_metadata = {}
                    if not memory.has_error_type(event.error_type):
                        with st.spinner("Curating on-call summary for this new error type..."):
                            card = curator.curate(event)
                        extra_metadata = card.to_metadata()
                        st.warning(f"🆕 **New error type — on-call summary**\n\n{card.summary}")

                    memory.add(event, extra_metadata=extra_metadata)
                    st.success("Stored — this error is now searchable and will inform future analyses.")

# ── Search ───────────────────────────────────────────────────────────────────

with tab_search:
    st.subheader("Semantic search over past errors")
    query_text = st.text_input("Describe the error you're looking for")
    search_top_k = st.slider("Results", 1, 10, 5, key="search_top_k")

    if st.button("Search"):
        if not query_text.strip():
            st.warning("Enter a description first.")
        else:
            results = memory.query_similar(query_text, top_k=search_top_k)
            if not results:
                st.info("No matching errors found.")
            for item in results:
                meta = item["metadata"]
                with st.container(border=True):
                    st.markdown(
                        f"**{meta.get('error_type', '')}** — {meta.get('timestamp', '')} "
                        f"(distance {item['distance']:.4f})"
                    )
                    st.write(meta.get("message", ""))

# ── History ──────────────────────────────────────────────────────────────────

with tab_history:
    st.subheader("Recently captured errors")
    limit = st.slider("Show last N", 5, 100, 20)
    items = memory.recent(limit=limit)

    if not items:
        st.info("No errors captured yet.")
    else:
        st.dataframe(
            [
                {
                    "timestamp": item["metadata"].get("timestamp", ""),
                    "error_type": item["metadata"].get("error_type", ""),
                    "message": item["metadata"].get("message", ""),
                    "source": (
                        f"{item['metadata'].get('source_file', '')}:"
                        f"{item['metadata'].get('line_number', '')}"
                    ),
                }
                for item in items
            ],
            use_container_width=True,
        )

# ── Incidents ────────────────────────────────────────────────────────────────

with tab_incidents:
    st.subheader("On-call incident summaries")
    st.caption("One curated summary per new error type — not every occurrence.")
    incident_limit = st.slider("Show last N", 5, 50, 20, key="incident_limit")
    incident_items = memory.list_incidents(limit=incident_limit)

    if not incident_items:
        st.info("No new error types curated yet.")
    else:
        for item in incident_items:
            meta = item["metadata"]
            with st.container(border=True):
                st.markdown(f"**{meta.get('error_type', '')}** — {meta.get('timestamp', '')}")
                st.write(meta.get("incident_summary", ""))

# ── Agent Forge Traces ──────────────────────────────────────────────────────
# Only meaningful when LOGSCRIBE_ANALYZER=agent_forge — reads agent-forge's own
# traceability API directly, so you can see exactly how it reasoned (which
# agents ran, what each one produced, git/filesystem tool use) without leaving
# logscribe's UI.

with tab_traces:
    st.subheader("Agent Forge run history")
    st.caption(
        f"Reading from {AGENT_FORGE_URL} — only populated when "
        "LOGSCRIBE_ANALYZER=agent_forge is set."
    )

    col_a, col_b, col_c = st.columns([2, 2, 1])
    with col_a:
        traces_app_filter = st.text_input("App ID", value="logscribe", key="af_app_filter")
    with col_b:
        traces_status_filter = st.selectbox(
            "Status", ["", "success", "partial", "failed"], key="af_status_filter"
        )
    with col_c:
        st.write("")
        st.button("🔄 Refresh", key="af_refresh")

    af_traces = fetch_agent_forge_traces(
        app_id=traces_app_filter or None, status=traces_status_filter or None,
    )

    if not af_traces:
        st.info(
            "No agent-forge traces yet — analyze an error with "
            "LOGSCRIBE_ANALYZER=agent_forge set, or clear the App ID filter above."
        )
    else:
        st.dataframe(
            [
                {
                    "run_id": t["run_id"][:8],
                    "timestamp": t["timestamp"][:19],
                    "tier": t["routing_tier"],
                    "framework": t.get("framework_used") or "-",
                    "outcome": t["outcome"],
                    "latency_ms": t["total_latency_ms"],
                    "cost_usd": t["total_cost_usd"],
                    "tokens_in": t["total_input_tokens"],
                    "tokens_out": t["total_output_tokens"],
                }
                for t in af_traces
            ],
            use_container_width=True,
            hide_index=True,
        )

        st.markdown("##### Run detail")
        af_run_options = {
            f"{t['run_id'][:8]} — {t['timestamp'][:19]} ({t['outcome']})": t for t in af_traces
        }
        af_selected = st.selectbox(
            "Select a run to inspect", list(af_run_options.keys()), key="af_trace_detail_select"
        )
        if af_selected:
            af_detail = af_run_options[af_selected]
            with st.expander("Goal sent to agent-forge"):
                st.text(af_detail["task"])
            if af_detail.get("error"):
                st.error(af_detail["error"])
            if af_detail["guardrails_triggered"]:
                st.warning(f"Guardrails triggered: {', '.join(af_detail['guardrails_triggered'])}")

            if af_detail.get("report"):
                st.markdown("**Final report**")
                st.markdown(af_detail["report"])
                st.divider()

            if af_detail["agents_spawned"]:
                st.markdown("**Agents spawned**")
                st.dataframe(
                    [{k: v for k, v in a.items() if k != "content"} for a in af_detail["agents_spawned"]],
                    use_container_width=True, hide_index=True,
                )
                st.markdown("**Agent output**")
                for a in af_detail["agents_spawned"]:
                    with st.expander(f"`{a['agent_id']}` — {a['model']} ({a['status']})"):
                        st.markdown(a.get("content") or "_(no content recorded)_")
            else:
                st.caption("No agent-level data recorded for this run.")

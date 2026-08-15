from __future__ import annotations

"""
logscribe Streamlit UI — a visual companion to the CLI for pasting log
snippets, watching them get analyzed, and browsing/searching past errors.

Run:
    streamlit run app.py
"""

import streamlit as st
from dotenv import load_dotenv

from logscribe.analyzer import OpenAIAnalyzer
from logscribe.memory import ErrorMemory
from logscribe.processor import ErrorProcessor
from logscribe.sampler import scan_lines

load_dotenv()

st.set_page_config(page_title="logscribe", page_icon="📜", layout="wide")


@st.cache_resource
def get_memory() -> ErrorMemory:
    return ErrorMemory()


@st.cache_resource
def get_analyzer() -> OpenAIAnalyzer:
    return OpenAIAnalyzer()


processor = ErrorProcessor()
memory = get_memory()
analyzer = get_analyzer()

st.title("logscribe")
st.caption("Tail-based log monitoring with RAG-powered error analysis.")

tab_analyze, tab_search, tab_history = st.tabs(["Analyze", "Search", "History"])

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

                    memory.add(event)
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

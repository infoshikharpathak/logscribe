from __future__ import annotations

import json
import os
from typing import Any, Protocol

import httpx
from openai import OpenAI

from logscribe.processor import ErrorEvent

SYSTEM_PROMPT = """You are a senior site reliability engineer performing root-cause \
analysis on a production error. You are given the newly captured error along with \
semantically similar errors seen in the past. Use the past context to spot recurring \
patterns, but focus your analysis on the new error.

Respond with:
1. Likely root cause
2. Supporting evidence from the log context
3. Suggested next steps to confirm/fix
Keep it concise."""


class ErrorAnalyzer(Protocol):
    """Interface for root-cause analysis backends.

    Implement this to swap in agent-forge (or any other orchestration layer)
    later without touching sampler.py, processor.py, or memory.py.
    """

    def analyze(self, event: ErrorEvent, similar_events: list[dict[str, Any]]) -> str:
        ...


class OpenAIAnalyzer:
    """Default analyzer: a direct OpenAI chat completion call."""

    def __init__(self, model: str = "gpt-4o-mini", client: OpenAI | None = None) -> None:
        self.model = model
        self._client = client or OpenAI()

    def analyze(self, event: ErrorEvent, similar_events: list[dict[str, Any]]) -> str:
        prompt = self._build_prompt(event, similar_events)
        response = self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        return response.choices[0].message.content or ""

    @staticmethod
    def _build_prompt(event: ErrorEvent, similar_events: list[dict[str, Any]]) -> str:
        lines = [
            "## New error",
            event.to_document(),
            "",
            f"Timestamp: {event.timestamp}",
            f"Source: {event.source_file}:{event.line_number}",
        ]
        if event.related_lines:
            lines += ["", "## Log lines for this request", event.related_context()]

        if similar_events:
            lines.append("\n## Similar past errors (most similar first)")
            for i, item in enumerate(similar_events, start=1):
                meta = item.get("metadata", {})
                score = item.get("similarity")
                score_text = f" (similarity {score:.2f})" if isinstance(score, (int, float)) else ""
                if meta.get("seeded"):
                    lines.append(
                        f"\n{i}. [seeded example, resolved]{score_text} "
                        f"{meta.get('error_type', 'UnknownError')}: {meta.get('message', '')}"
                    )
                    for label, key in (("Symptom", "symptom"), ("Cause", "cause"), ("Fix", "fix")):
                        if meta.get(key):
                            lines.append(f"   {label}: {meta[key]}")
                else:
                    lines.append(
                        f"\n{i}. [{meta.get('timestamp', 'unknown time')}]{score_text} "
                        f"{meta.get('error_type', 'UnknownError')}: {meta.get('message', '')}"
                    )
        else:
            lines.append("\n## Similar past errors\nNone found — this is the first occurrence.")

        return "\n".join(lines)


class AgentForgeAnalyzer:
    """Alternative analyzer backend: root-cause analysis via agent-forge's /run/stream.

    Pairs with the "logscribe" app registered in agent-forge's App Registry
    (agent-forge/src/agent_forge/apps/logscribe.json) — when the goal matches,
    agent-forge skips dynamic planning entirely and runs a single locked
    root-cause-analyst agent instead of inventing a team from scratch.
    """

    def __init__(
        self, base_url: str | None = None, provider: str = "openai", repo_path: str | None = None,
    ) -> None:
        self.base_url = (base_url or os.getenv("AGENT_FORGE_URL", "http://localhost:8000")).rstrip("/")
        self.provider = provider
        # Optional: path to the git repo of the service being monitored. When set,
        # it's included in the goal so agent-forge's locked "logscribe" app can use
        # its git tools (git_log/git_show/git_diff) to check whether a recent commit
        # correlates with the error — see apps/logscribe.json in agent-forge.
        self.repo_path = repo_path or os.getenv("LOGSCRIBE_REPO_PATH") or None

    def analyze(self, event: ErrorEvent, similar_events: list[dict[str, Any]]) -> str:
        goal = self._build_goal(event, similar_events, self.repo_path)
        result_parts: list[str] = []
        with httpx.Client(timeout=120) as client:
            with client.stream(
                "POST",
                f"{self.base_url}/run/stream",
                json={"goal": goal, "max_rounds": 1, "provider": self.provider},
                headers={"Accept": "text/event-stream"},
            ) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line.startswith("data: "):
                        continue
                    payload = json.loads(line[6:])
                    if payload.get("type") == "done":
                        result_parts.append(payload.get("result", ""))
                    elif payload.get("type") == "error":
                        raise RuntimeError(f"agent-forge error: {payload.get('message')}")
        return "".join(result_parts) or "(agent-forge returned no analysis)"

    @staticmethod
    def _build_goal(
        event: ErrorEvent, similar_events: list[dict[str, Any]], repo_path: str | None = None,
    ) -> str:
        context = OpenAIAnalyzer._build_prompt(event, similar_events)
        goal = (
            "Perform root-cause analysis on the following production error, using the "
            "similar past errors as historical context if any are listed.\n\n" + context
        )
        if repo_path:
            goal += (
                f"\n\nRepository path for git correlation: {repo_path}\n"
                "Before concluding, check this repository's recent commits (git_log, then "
                "git_show on any that touch the failing service or code path). Cite the "
                "commit hash of any you looked at, and say whether one is implicated or not."
            )
        return goal


def build_analyzer(repo_path: str | os.PathLike | None = None) -> ErrorAnalyzer:
    """Factory: OpenAIAnalyzer by default, or AgentForgeAnalyzer when
    LOGSCRIBE_ANALYZER=agent_forge is set — the drop-in swap analyzer.py was
    built around from day one (see the ErrorAnalyzer Protocol above).

    *repo_path* (a watch config's repo_path) is the monitored app's git repo
    for commit correlation; it wins over LOGSCRIBE_REPO_PATH, which is only
    the default for watches that don't set one."""
    backend = os.getenv("LOGSCRIBE_ANALYZER", "openai").strip().lower()
    if backend == "agent_forge":
        return AgentForgeAnalyzer(repo_path=str(repo_path) if repo_path else None)
    return OpenAIAnalyzer()

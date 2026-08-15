from __future__ import annotations

from typing import Any, Protocol

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

        if similar_events:
            lines.append("\n## Similar past errors (most similar first)")
            for i, item in enumerate(similar_events, start=1):
                meta = item.get("metadata", {})
                lines.append(
                    f"\n{i}. [{meta.get('timestamp', 'unknown time')}] "
                    f"{meta.get('error_type', 'UnknownError')}: {meta.get('message', '')}"
                )
        else:
            lines.append("\n## Similar past errors\nNone found — this is the first occurrence.")

        return "\n".join(lines)

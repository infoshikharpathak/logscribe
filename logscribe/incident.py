from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from openai import OpenAI

from logscribe.processor import ErrorEvent

SYSTEM_PROMPT = """You are writing a short on-call incident notice, not a root-cause \
investigation. In 2-4 sentences, tell an on-call engineer what just happened and your \
best quick guess at why — no deep analysis, no multi-step investigation, no \
recommendations list. Just enough for them to understand the incident at a glance."""


@dataclass
class IncidentCard:
    """A curated on-call summary for a newly-seen (not previously stored) error type."""

    event_id: str
    error_type: str
    occurred_at: str
    summary: str

    def to_metadata(self) -> dict[str, Any]:
        return {"is_incident": True, "incident_summary": self.summary}


class OnCallCurator:
    """Curates a quick incident notice for a new error type. Deliberately shallow —
    the deep root-cause analysis (with similar-past-error context) is analyzer.py's
    job; this only fires once per new error type, for a fast at-a-glance summary."""

    def __init__(self, model: str = "gpt-4o-mini", client: OpenAI | None = None) -> None:
        self.model = model
        self._client = client or OpenAI()

    def curate(self, event: ErrorEvent) -> IncidentCard:
        prompt = (
            f"A new error type was just seen for the first time:\n\n{event.to_document()}\n\n"
            f"Occurred at: {event.timestamp}\n"
            f"Source: {event.source_file}:{event.line_number}"
        )
        response = self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        summary = response.choices[0].message.content or ""
        return IncidentCard(
            event_id=event.id,
            error_type=event.error_type,
            occurred_at=event.timestamp,
            summary=summary,
        )

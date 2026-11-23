"""Seeded incident history: resolved example incidents (symptom, cause,
fix) loaded into ErrorMemory so RAG over past errors has something to match
before the first real failure. Each is stored with seeded=True and shown as
a seeded example wherever it's matched.

File format (JSON):

    {"seeded_examples": true,
     "incidents": [{"id": "...", "service_name": "...", "error_type": "...",
                    "message": "...", "symptom": "...", "cause": "...", "fix": "..."}]}
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REQUIRED = ("error_type", "message", "symptom", "cause", "fix")


def load_seed_file(path: str | Path) -> list[dict[str, Any]]:
    data = json.loads(Path(path).read_text())
    incidents = data.get("incidents") if isinstance(data, dict) else data
    if not isinstance(incidents, list) or not incidents:
        raise ValueError(f"{path}: expected a non-empty 'incidents' list")
    for i, incident in enumerate(incidents):
        missing = [f for f in REQUIRED if not isinstance(incident, dict) or not incident.get(f)]
        if missing:
            raise ValueError(f"{path}: incidents[{i}] is missing {', '.join(missing)}")
    return incidents


def load_into(memory, path: str | Path) -> list[str]:
    """Upsert every incident in *path* into *memory*; returns their IDs."""
    return [memory.add_seed(incident) for incident in load_seed_file(path)]

"""Watch configs: which log files to tail (with their service names) and
where their error history lives, in one TOML file.

    name = "ironclad"
    collection = "ironclad_errors"      # ChromaDB collection (default: error_events)
    persist_dir = "~/.logscribe/chroma" # default
    buffer_size = 20
    top_k = 5
    min_similarity = 0.3                # drop weaker matches (cosine collections)
    seeds = "seed_incidents.json"       # for `logscribe seed --config ...`

    [[sources]]
    service = "bank-service"
    path = "logs/bank-service.log"

Relative paths are relative to the config file. Environment variables in
paths (``${IRONCLAD_LOG_DIR}``) are expanded, with ``${VAR:-default}``
falling back to the default when VAR is unset.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from logscribe.memory import COLLECTION_NAME, DEFAULT_PERSIST_DIR


@dataclass
class Source:
    path: Path
    service: str | None = None


@dataclass
class WatchConfig:
    name: str = "default"
    sources: list[Source] = field(default_factory=list)
    collection: str = COLLECTION_NAME
    persist_dir: Path = DEFAULT_PERSIST_DIR
    buffer_size: int = 50
    top_k: int = 5
    min_similarity: float | None = None
    seeds: Path | None = None


_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand(value: str) -> str:
    return _VAR.sub(lambda m: os.environ.get(m.group(1)) or (m.group(2) or ""), value)


def _path(value: str, base: Path) -> Path:
    path = Path(_expand(value)).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def load_config(path: str | Path) -> WatchConfig:
    path = Path(path)
    data = tomllib.loads(path.read_text())
    base = path.resolve().parent
    known = {"name", "sources", "collection", "persist_dir", "buffer_size", "top_k", "min_similarity", "seeds"}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"{path}: unknown setting(s) {', '.join(sorted(unknown))}")
    sources = []
    for i, src in enumerate(data.get("sources") or []):
        if not isinstance(src, dict) or not src.get("path"):
            raise ValueError(f"{path}: sources[{i}] needs a path")
        sources.append(Source(path=_path(src["path"], base), service=src.get("service")))
    if not sources:
        raise ValueError(f"{path}: add at least one [[sources]] entry")
    return WatchConfig(
        name=str(data.get("name") or path.stem),
        sources=sources,
        collection=str(data.get("collection") or COLLECTION_NAME),
        persist_dir=_path(data["persist_dir"], base) if data.get("persist_dir") else DEFAULT_PERSIST_DIR,
        buffer_size=int(data.get("buffer_size", 50)),
        top_k=int(data.get("top_k", 5)),
        min_similarity=float(data["min_similarity"]) if data.get("min_similarity") is not None else None,
        seeds=_path(data["seeds"], base) if data.get("seeds") else None,
    )

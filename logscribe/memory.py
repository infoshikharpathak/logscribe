from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import chromadb
from chromadb.utils import embedding_functions

from logscribe.processor import ErrorEvent

DEFAULT_PERSIST_DIR = Path.home() / ".logscribe" / "chroma"
COLLECTION_NAME = "error_events"

# Metadata fields a seeded incident carries (see load_seeds / `logscribe seed`).
SEED_FIELDS = ("error_type", "service_name", "message", "symptom", "cause", "fix")


class ErrorMemory:
    """ChromaDB-backed store for structured error events, with semantic retrieval.

    New collections use cosine distance, so each match gets a similarity
    score (1 - distance, 1.0 = identical). A collection created earlier with
    Chroma's default L2 distance keeps it; its matches report the raw
    distance and a similarity of None.
    """

    def __init__(
        self,
        persist_dir: str | Path = DEFAULT_PERSIST_DIR,
        embedding_model: str = "all-MiniLM-L6-v2",
        collection_name: str = COLLECTION_NAME,
        embedding_function: Any = None,
    ) -> None:
        Path(persist_dir).expanduser().mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(Path(persist_dir).expanduser()))
        self._embedding_fn = embedding_function or embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name=embedding_model
        )
        self._collection = self._client.get_or_create_collection(
            name=collection_name,
            embedding_function=self._embedding_fn,
            metadata={"hnsw:space": "cosine"},  # only applies when the collection is new
        )
        self.space = (self._collection.metadata or {}).get("hnsw:space", "l2")

    def add(self, event: ErrorEvent, extra_metadata: dict[str, Any] | None = None) -> None:
        metadata = {
            "error_type": event.error_type,
            "message": event.message,
            "timestamp": event.timestamp,
            "source_file": event.source_file,
            "line_number": event.line_number,
        }
        if event.service_name:
            metadata["service_name"] = event.service_name
        if extra_metadata:
            metadata.update(extra_metadata)

        self._collection.add(
            ids=[event.id],
            documents=[event.to_document()],
            metadatas=[metadata],
        )

    def add_seed(self, incident: dict[str, Any]) -> str:
        """Store a resolved example incident (symptom, cause, fix), marked
        seeded=True. Its ID comes from its error type and service, so loading
        the same seeds again updates them instead of adding copies."""
        missing = [f for f in ("error_type", "message", "symptom", "cause", "fix") if not incident.get(f)]
        if missing:
            raise ValueError(f"seeded incident is missing {', '.join(missing)}")
        service = incident.get("service_name") or ""
        slug = re.sub(r"[^a-z0-9]+", "-", f"{service}-{incident['error_type']}".lower()).strip("-")
        seed_id = f"seed-{incident.get('id') or slug}"
        document = "\n".join(filter(None, [
            f"service: {service}" if service else "",
            f"error_type: {incident['error_type']}",
            f"message: {incident['message']}",
            f"symptom: {incident['symptom']}",
        ]))
        metadata = {f: str(incident[f]) for f in SEED_FIELDS if incident.get(f)}
        metadata.update({"seeded": True, "timestamp": "seeded example", "source_file": "seed",
                         "line_number": 0})
        self._collection.upsert(ids=[seed_id], documents=[document], metadatas=[metadata])
        return seed_id

    def has_error_type(self, error_type: str) -> bool:
        """Whether a previously *captured* error already has this error_type — used to
        detect a genuinely new/unfamiliar error type worth curating an on-call
        summary for, versus the Nth occurrence of something already seen.
        Seeded examples don't count: the first real occurrence still gets a card."""
        if self._collection.count() == 0:
            return False
        results = self._collection.get(where={"error_type": error_type}, limit=50)
        return any(not meta.get("seeded") for meta in results["metadatas"])

    def list_incidents(self, limit: int = 20) -> list[dict[str, Any]]:
        """Entries with a curated on-call summary attached (see incident.py)."""
        results = self._collection.get(where={"is_incident": True}, limit=limit)
        items = [
            {"id": id_, "document": doc, "metadata": meta}
            for id_, doc, meta in zip(results["ids"], results["documents"], results["metadatas"])
        ]
        items.sort(key=lambda item: item["metadata"].get("timestamp", ""), reverse=True)
        return items

    def query_similar(
        self, text: str, top_k: int = 5, min_similarity: float | None = None
    ) -> list[dict[str, Any]]:
        """The top_k most similar stored errors, most similar first. With
        *min_similarity* (cosine collections only), weaker matches are dropped."""
        if self._collection.count() == 0:
            return []
        results = self._collection.query(query_texts=[text], n_results=min(top_k, self._collection.count()))
        items = self._flatten(results)
        for item in items:
            item["similarity"] = round(1 - item["distance"], 4) if self.space == "cosine" else None
        if min_similarity is not None and self.space == "cosine":
            items = [item for item in items if item["similarity"] >= min_similarity]
        return items

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        results = self._collection.get(limit=limit)
        items = [
            {"id": id_, "document": doc, "metadata": meta}
            for id_, doc, meta in zip(results["ids"], results["documents"], results["metadatas"])
        ]
        items.sort(key=lambda item: item["metadata"].get("timestamp", ""), reverse=True)
        return items

    @staticmethod
    def _flatten(results: dict[str, Any]) -> list[dict[str, Any]]:
        ids = results.get("ids", [[]])[0]
        documents = results.get("documents", [[]])[0]
        metadatas = results.get("metadatas", [[]])[0]
        distances = results.get("distances", [[]])[0]
        return [
            {"id": id_, "document": doc, "metadata": meta, "distance": dist}
            for id_, doc, meta, dist in zip(ids, documents, metadatas, distances)
        ]

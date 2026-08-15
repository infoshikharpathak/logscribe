from __future__ import annotations

from pathlib import Path
from typing import Any

import chromadb
from chromadb.utils import embedding_functions

from logscribe.processor import ErrorEvent

DEFAULT_PERSIST_DIR = Path.home() / ".logscribe" / "chroma"
COLLECTION_NAME = "error_events"


class ErrorMemory:
    """ChromaDB-backed store for structured error events, with semantic retrieval."""

    def __init__(
        self,
        persist_dir: str | Path = DEFAULT_PERSIST_DIR,
        embedding_model: str = "all-MiniLM-L6-v2",
    ) -> None:
        Path(persist_dir).mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(persist_dir))
        self._embedding_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name=embedding_model
        )
        self._collection = self._client.get_or_create_collection(
            name=COLLECTION_NAME,
            embedding_function=self._embedding_fn,
        )

    def add(self, event: ErrorEvent) -> None:
        self._collection.add(
            ids=[event.id],
            documents=[event.to_document()],
            metadatas=[
                {
                    "error_type": event.error_type,
                    "message": event.message,
                    "timestamp": event.timestamp,
                    "source_file": event.source_file,
                    "line_number": event.line_number,
                }
            ],
        )

    def query_similar(self, text: str, top_k: int = 5) -> list[dict[str, Any]]:
        if self._collection.count() == 0:
            return []
        results = self._collection.query(query_texts=[text], n_results=top_k)
        return self._flatten(results)

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

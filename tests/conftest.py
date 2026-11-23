"""Shared test helpers. No network and no model downloads: ChromaDB gets a
small deterministic bag-of-words embedding instead of MiniLM."""

from __future__ import annotations

import hashlib
import re

import numpy as np
import pytest
from chromadb.api.types import Documents, EmbeddingFunction, Embeddings


class BagOfWords(EmbeddingFunction[Documents]):
    DIMS = 256

    def __init__(self) -> None:
        pass

    def __call__(self, input: Documents) -> Embeddings:
        out = []
        for text in input:
            vec = np.zeros(self.DIMS, dtype=np.float32)
            for word in re.findall(r"[a-z0-9_]+", text.lower()):
                vec[int(hashlib.md5(word.encode()).hexdigest(), 16) % self.DIMS] += 1.0
            norm = np.linalg.norm(vec)
            out.append(vec / norm if norm else vec)
        return out

    @staticmethod
    def name() -> str:
        return "logscribe-test-bag-of-words"

    def get_config(self) -> dict:
        return {}

    @staticmethod
    def build_from_config(config: dict) -> "BagOfWords":
        return BagOfWords()


@pytest.fixture
def memory(tmp_path):
    from logscribe.memory import ErrorMemory

    return ErrorMemory(persist_dir=tmp_path / "chroma", collection_name="test_errors",
                       embedding_function=BagOfWords())

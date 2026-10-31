import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from campuspilot.config import ROOT, Settings
from campuspilot.db import init_db


@pytest.fixture
def settings(tmp_path):
    s = Settings(data_dir=ROOT / "data", database_path=tmp_path / "test.sqlite3", llm_provider=None)
    init_db(s.database_path)
    return s


# ---- offline embedding for retrieval tests (no model download, deterministic) ----
import re  # noqa: E402
import zlib  # noqa: E402

import chromadb  # noqa: E402
from chromadb import Documents, EmbeddingFunction, Embeddings  # noqa: E402

from campuspilot.rag import HandbookRetriever  # noqa: E402

_STOP = set("a an and the of to in on at for is are be can i my me do does what how when where which who "
            "it this that with or if as by from not no any have has will may must should campus student".split())


class BagOfWordsEmbedding(EmbeddingFunction):
    """Hashed bag-of-words vectors: texts sharing content words score high, others ~0."""

    def __init__(self, dim: int = 8192):
        self.dim = dim

    @staticmethod
    def name() -> str:
        return "test-bag-of-words"

    def get_config(self) -> dict:
        return {"dim": self.dim}

    @staticmethod
    def build_from_config(config: dict):
        return BagOfWordsEmbedding(config.get("dim", 8192))

    def __call__(self, input: Documents) -> Embeddings:
        out = []
        for text in input:
            v = [0.0] * self.dim
            for w in re.findall(r"[a-z]+", text.lower()):
                w = w.rstrip("s")
                if w and w not in _STOP:
                    v[zlib.crc32(w.encode()) % self.dim] += 1.0
            out.append(v)
        return out


@pytest.fixture
def make_retriever(tmp_path):
    def _make(handbook_path=None, min_score=0.15, top_k=3, **rule):
        from campuspilot.config import ROOT
        return HandbookRetriever(handbook_path or ROOT / "data" / "handbook" / "student_handbook.md",
                                 tmp_path / "chroma", embedding_function=BagOfWordsEmbedding(),
                                 top_k=top_k, min_score=min_score, **rule)
    return _make


@pytest.fixture
def retriever(make_retriever):
    return make_retriever()

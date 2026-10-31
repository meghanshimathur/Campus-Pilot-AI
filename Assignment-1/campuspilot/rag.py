"""Handbook retrieval: split the handbook into section-aware chunks, store them in
ChromaDB, and return only the few chunks relevant to a question (with source refs)."""
import hashlib
import math
import re
import threading
from pathlib import Path

CHUNK_VERSION = "1"  # bump when chunking logic changes so stale indexes are rebuilt
MAX_CHUNK_CHARS = 700
COLLECTION = "handbook"

_SECTION_RE = re.compile(r"^##\s+(\d+)\.\s+(.*)$")
_CLAUSE_RE = re.compile(r"^(\d+\.\d+)\s+(.*)$")


def chunk_handbook(text: str, source: str = "student_handbook.md") -> list[dict]:
    """Split by `## N. Title` sections, then group consecutive numbered clauses up to
    MAX_CHUNK_CHARS. Each chunk keeps its section title for context and a stable id."""
    sections, current = [], None
    for line in text.splitlines():
        m = _SECTION_RE.match(line)
        if m:
            current = {"number": m.group(1), "title": m.group(2).strip(), "clauses": []}
            sections.append(current)
        elif current is not None and (c := _CLAUSE_RE.match(line.strip())):
            current["clauses"].append((c.group(1), c.group(2).strip()))

    chunks = []
    for sec in sections:
        group, size = [], 0
        groups = []
        for num, body in sec["clauses"]:
            if group and size + len(body) > MAX_CHUNK_CHARS:
                groups.append(group)
                group, size = [], 0
            group.append((num, body))
            size += len(body)
        if group:
            groups.append(group)
        for i, g in enumerate(groups, 1):
            first, last = g[0][0], g[-1][0]
            body = "\n".join(f"{n} {b}" for n, b in g)
            chunks.append({
                "chunk_id": f"{source}#s{sec['number']}-{i}",
                "source": source,
                "section": f"{sec['number']}. {sec['title']}",
                "clauses": first if first == last else f"{first}-{last}",
                "text": f"{sec['title']}\n{body}",  # title prefix helps retrieval
            })
    return chunks



# ---------------------------------------------------------------- keyword coverage
_STOPWORDS = set(
    "a an and the of to in on at for is are be can i my me do does did what how when where which who whom it its this "
    "that these those with or if as by from not no any have has had will may must should would could about into than "
    "then there their they them we you your our us am been being was were so such also very just "
    # generic filler that carries no topic information (otherwise unknown filler words swamp the real terms)
    "get got getting go going come bring take make give need want please tell know help use using one two three "
    "first back out up down over under again some all other someone somebody else mine yours he she her him his "
    "many much more most long soon often always never ever still only even".split())
MIN_PREFIX = 7          # terms sharing this many leading letters count as one word (plagiaris-m/-ing, evaluat-e/-ion)
DISTINCTIVE_IDF = 1.6   # a matched term is 'distinctive' if it appears in at most about half of the chunks


def _stem(word: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def _terms(text: str) -> list[str]:
    return [_stem(w) for w in re.findall(r"[a-z]+", text.lower()) if w not in _STOPWORDS and len(w) > 1]


def _same_term(a: str, b: str) -> bool:
    """Same word allowing inflection: equal, a long shared prefix, or the shorter (>=5 letters) is a prefix of the
    longer (attend/attendance). Deliberately strict: 'elective' and 'election' must NOT match."""
    if a == b:
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) >= 5 and long_.startswith(short):
        return True
    return len(short) >= MIN_PREFIX and a[:MIN_PREFIX] == b[:MIN_PREFIX]


class KeywordScorer:
    """Fraction of a query's information-weighted terms found in a chunk (0..1).

    Terms are weighted by inverse chunk frequency; a query term that appears nowhere in the
    handbook still counts in the denominator at maximum weight, which is what exposes questions
    about things the handbook never mentions (e.g. 'visa'). Complements embeddings, which
    dilute exact terms inside long chunks and over-reward generic shared words like 'student'.
    """

    def __init__(self, chunks: list[dict]):
        self.n = len(chunks)
        self.chunk_terms = {c["chunk_id"]: set(_terms(c["text"])) for c in chunks}
        self._idf_cache: dict[str, float] = {}  # corpus is fixed per scorer, so weights can be cached

    def _idf(self, term: str) -> float:
        if term not in self._idf_cache:
            df = sum(any(_same_term(term, t) for t in terms) for terms in self.chunk_terms.values())
            self._idf_cache[term] = math.log((self.n + 1) / (df + 1)) + 1
        return self._idf_cache[term]

    def score(self, query: str, chunk_id: str) -> tuple[float, int]:
        """(coverage, number of distinctive matched terms). (0.0, 0) for a query with no content words."""
        query_terms = set(_terms(query))
        if not query_terms:
            return 0.0, 0
        chunk_terms = self.chunk_terms[chunk_id]
        total = matched = 0.0
        distinctive = 0
        for term in query_terms:
            weight = self._idf(term)
            total += weight
            if any(_same_term(term, t) for t in chunk_terms):
                matched += weight
                distinctive += weight >= DISTINCTIVE_IDF
        return matched / total, distinctive


def accept_chunk(semantic: float, coverage: float, distinctive: int, *, min_score: float, strong_score: float,
                 min_coverage: float, keyword_coverage: float, keyword_min_score: float,
                 keyword_min_terms: int = 2) -> str | None:
    """Return why a chunk is relevant ('semantic' | 'keyword' | 'semantic+keyword') or None to reject.

    - semantic:         similarity alone is high enough to trust.
    - keyword:          the chunk contains most of the question's key terms (at least `keyword_min_terms` distinctive
                        ones, so one accidental word-form match is not enough), even though the embedding is weak
                        (long chunks dilute exact terms).
    - semantic+keyword: moderate similarity AND real term overlap; moderate similarity with almost no
                        distinctive term overlap (only generic words like 'student') is rejected.
    """
    if semantic >= strong_score:
        return "semantic"
    if distinctive >= keyword_min_terms and coverage >= keyword_coverage and semantic >= keyword_min_score:
        return "keyword"
    if distinctive >= 1 and semantic >= min_score and coverage >= min_coverage:
        return "semantic+keyword"
    return None


NOT_FOUND_MESSAGE = ("No relevant information was found in the student handbook for this question. "
                     "Tell the student it was not found; do not guess or invent a policy.")


def search_handbook(retriever, query) -> dict:
    """Tool entry point. Returns only the relevant chunks, or found=False."""
    if retriever is None:
        return {"error": "Handbook search is not available."}
    if not isinstance(query, str) or not 3 <= len(query.strip()) <= 300:
        return {"error": "Please provide a question of 3 to 300 characters."}
    chunks = retriever.search(query)
    if not chunks:
        return {"found": False, "query": query.strip(), "chunks": [], "message": NOT_FOUND_MESSAGE}
    return {"found": True, "query": query.strip(), "chunks": chunks}


class HandbookRetriever:
    def __init__(self, handbook_path, persist_dir, embedding_function=None, top_k: int = 3,
                 min_score: float = 0.40, strong_score: float = 0.50, min_coverage: float = 0.18,
                 keyword_coverage: float = 0.60, keyword_min_score: float = 0.10):
        self.handbook_path = Path(handbook_path)
        self.persist_dir = Path(persist_dir)
        self.embedding_function = embedding_function
        self.top_k = top_k
        self.rule = dict(min_score=min_score, strong_score=strong_score, min_coverage=min_coverage,
                         keyword_coverage=keyword_coverage, keyword_min_score=keyword_min_score)
        self._collection = None
        self._keywords = None
        # FastAPI runs sync endpoints in a thread pool; the lock only guards the one-time lazy index build.
        self._lock = threading.Lock()

    def _client(self):
        import chromadb
        from chromadb.config import Settings
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        return chromadb.PersistentClient(path=str(self.persist_dir),
                                         settings=Settings(anonymized_telemetry=False))

    def _ef(self):
        if self.embedding_function is None:
            from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
            self.embedding_function = DefaultEmbeddingFunction()  # local model, no API key
        return self.embedding_function

    def ingest(self, force: bool = False) -> int:
        """Build (or reuse) the index. Rebuilds if the handbook or chunking changed."""
        text = self.handbook_path.read_text(encoding="utf-8")
        digest = hashlib.sha256((CHUNK_VERSION + text).encode()).hexdigest()
        client = self._client()
        existing = [c.name for c in client.list_collections()]
        if COLLECTION in existing and not force:
            col = client.get_collection(COLLECTION, embedding_function=self._ef())
            if (col.metadata or {}).get("content_hash") == digest and col.count() > 0:
                self._collection = col
                self._keywords = KeywordScorer(chunk_handbook(text, self.handbook_path.name))
                return col.count()
        if COLLECTION in existing:
            client.delete_collection(COLLECTION)
        col = client.create_collection(COLLECTION, embedding_function=self._ef(),
                                       metadata={"hnsw:space": "cosine", "content_hash": digest})
        chunks = chunk_handbook(text, self.handbook_path.name)
        self._keywords = KeywordScorer(chunks)
        if chunks:
            col.add(ids=[c["chunk_id"] for c in chunks],
                    documents=[c["text"] for c in chunks],
                    metadatas=[{"source": c["source"], "section": c["section"], "clauses": c["clauses"]}
                               for c in chunks])
        self._collection = col
        return len(chunks)

    def search(self, query: str) -> list[dict]:
        """Return at most top_k relevant chunks, best first. A chunk is kept only if accept_chunk()
        approves its (semantic similarity, keyword coverage) pair; see accept_chunk for the rules."""
        if not isinstance(query, str) or not query.strip():
            return []
        with self._lock:
            if self._collection is None:
                self.ingest()
        query = query.strip()
        # Ask for every chunk (the handbook is small): accept_chunk() needs each chunk's similarity AND keyword
        # coverage to decide, so ranking by embedding distance alone would hide chunks the keyword rule rescues.
        res = self._collection.query(query_texts=[query], n_results=max(self._collection.count(), 1))
        out = []
        for cid, doc, meta, dist in zip(res["ids"][0], res["documents"][0],
                                        res["metadatas"][0], res["distances"][0]):
            semantic = 1.0 - dist  # cosine distance -> similarity
            coverage, distinctive = self._keywords.score(query, cid)
            why = accept_chunk(semantic, coverage, distinctive, **self.rule)
            if why:
                out.append({"chunk_id": cid, "source": meta["source"], "section": meta["section"],
                            "clauses": meta["clauses"], "score": round(semantic, 4),
                            "keyword_coverage": round(coverage, 4), "match": why, "text": doc})
        out.sort(key=lambda c: c["score"] + c["keyword_coverage"], reverse=True)
        return out[: self.top_k]

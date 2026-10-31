import json
import os
from pathlib import Path

import pytest

from campuspilot.config import ROOT
from campuspilot.rag import MAX_CHUNK_CHARS, NOT_FOUND_MESSAGE, chunk_handbook, search_handbook

HANDBOOK = ROOT / "data" / "handbook" / "student_handbook.md"


def test_chunking_is_meaningful():
    text = HANDBOOK.read_text()
    chunks = chunk_handbook(text)
    ids = [c["chunk_id"] for c in chunks]
    assert len(ids) == len(set(ids)) and len(chunks) >= 12
    assert len({c["section"] for c in chunks}) == 12  # every handbook section is represented
    for c in chunks:
        assert c["source"] == "student_handbook.md" and c["clauses"]
        assert len(c["text"]) < MAX_CHUNK_CHARS + 150
        assert len(c["text"]) < len(text) / 4  # no chunk is anything like the whole handbook
    assert "payment portal" in "".join(c["text"] for c in chunks).lower()


@pytest.mark.parametrize("question,section_prefix,expected_text", [
    ("The payment portal failed. Can I pay on the next working day?", "2.", "24 hours"),
    ("What is the minimum attendance required for exams?", "6.", "75%"),
    ("How many books can I borrow from the library?", "7.", "4 books"),
    ("What time does the hostel gate close?", "8.", "22:30"),
    ("How do I appeal a grievance decision?", "9.", "Dean of Students"),
])
def test_relevant_chunk_is_top_result(retriever, question, section_prefix, expected_text):
    hits = retriever.search(question)
    assert hits, "expected at least one relevant chunk"
    # the section holding the answer must be among the (few) chunks returned (neighbouring sections may rank first,
    # e.g. exam rules also mention attendance)
    assert any(h["section"].startswith(section_prefix) for h in hits)
    assert any(expected_text in h["text"] for h in hits)


def test_evidence_has_source_references(retriever):
    hit = retriever.search("late fee after the due date")[0]
    assert set(hit) == {"chunk_id", "source", "section", "clauses", "score", "keyword_coverage", "match", "text"}
    assert hit["match"] in ("semantic", "keyword", "semantic+keyword") and 0 <= hit["keyword_coverage"] <= 1
    assert hit["chunk_id"].startswith("student_handbook.md#s") and hit["source"] == "student_handbook.md"
    assert 0 < hit["score"] <= 1


def test_only_top_k_sorted_and_not_whole_handbook(make_retriever):
    r = make_retriever(min_score=-1, top_k=3, strong_score=-1)  # accept anything, so top_k is what limits
    hits = r.search("fees and payment")
    assert len(hits) == 3
    assert [h["score"] for h in hits] == sorted((h["score"] for h in hits), reverse=True)
    assert sum(len(h["text"]) for h in hits) < len(HANDBOOK.read_text()) / 3


@pytest.mark.parametrize("q", ["Where can I park my car on campus?", "What is the capital of France?",
                               "Is there a swimming pool?"])
def test_irrelevant_queries_return_nothing(retriever, q):
    assert retriever.search(q) == []


def test_blank_query_returns_nothing(retriever):
    assert retriever.search("   ") == [] and retriever.search(None) == []


def test_search_tool_found_and_missing(retriever):
    found = search_handbook(retriever, "How long can I keep a library book?")
    assert found["found"] is True and found["chunks"][0]["section"].startswith("7.")
    missing = search_handbook(retriever, "Where can I park my car on campus?")
    assert missing == {"found": False, "query": "Where can I park my car on campus?",
                       "chunks": [], "message": NOT_FOUND_MESSAGE}
    assert "do not guess" in NOT_FOUND_MESSAGE


def test_search_tool_input_validation(retriever):
    assert "error" in search_handbook(retriever, "")
    assert "error" in search_handbook(retriever, "hi")
    assert "error" in search_handbook(retriever, None)
    assert "error" in search_handbook(retriever, "x" * 301)
    assert "error" in search_handbook(None, "valid question here")


def test_ingest_is_idempotent_and_detects_changes(make_retriever, tmp_path):
    hb = tmp_path / "hb.md"
    hb.write_text(HANDBOOK.read_text())
    r = make_retriever(hb)
    n = r.ingest()
    assert r.ingest() == n  # reuses the existing index
    hb.write_text(hb.read_text() + "\n## 13. Parking\n\n13.1 Student parking permits cost Rs. 100 per month.\n")
    r2 = make_retriever(hb)
    assert r2.ingest() == n + 1  # rebuilt with the new section
    assert r2.search("parking permit cost")[0]["section"].startswith("13.")


# Real local embedding model: runs only if Chroma's model is already cached (never downloads in tests).
_MODEL = Path.home() / ".cache" / "chroma" / "onnx_models" / "all-MiniLM-L6-v2"


@pytest.mark.skipif(not _MODEL.exists(), reason="Chroma default embedding model not cached")
def test_real_embedder_threshold_separates_relevant_from_irrelevant(tmp_path):
    from campuspilot.rag import HandbookRetriever
    r = HandbookRetriever(HANDBOOK, tmp_path / "chroma", top_k=3, min_score=0.40)
    top = r.search("The payment portal failed. Can I pay on the next working day?")
    assert top and top[0]["section"].startswith("2.")
    assert r.search("What time does the hostel gate close?")[0]["section"].startswith("8.")
    for q in ("Where can I park my car on campus?", "What is the capital of France?",
              "How do I apply for a student visa?", "What is the bus schedule to the airport?"):
        assert r.search(q) == [], q

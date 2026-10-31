"""Measure handbook retrieval quality: baseline (semantic similarity >= 0.40 only) vs the current hybrid rule,
on the labelled queries in data/retrieval_queries.json. Uses the local embedding model (no API calls).

  .venv/bin/python scripts/evaluate_retrieval.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from campuspilot.config import load_settings  # noqa: E402


def evaluate(retriever, ins, outs, top=3):
    """Returns (answered_in_scope, leaked_out_scope, missed_queries, leaked_queries)."""
    missed, leaked = [], []
    for item in ins:
        hits = retriever.search(item["query"])[:top]
        if not any(h["section"].startswith(p) for h in hits for p in item["sections"]):
            missed.append(item["query"])
    for q in outs:
        if retriever.search(q)[:top]:
            leaked.append(q)
    return len(ins) - len(missed), len(leaked), missed, leaked


def make_retrievers(settings, **kw):
    from campuspilot.rag import HandbookRetriever
    new = HandbookRetriever(settings.handbook_path, settings.chroma_dir, top_k=3, min_score=settings.handbook_min_score,
                            strong_score=settings.handbook_strong_score, **kw)
    # baseline = the pre-hybrid rule: accept a chunk iff semantic similarity >= 0.40
    old = HandbookRetriever(settings.handbook_path, settings.chroma_dir, top_k=3, strong_score=0.40, min_score=9,
                            keyword_coverage=9, **kw)
    return old, new


if __name__ == "__main__":
    s = load_settings()
    data = json.loads((Path(__file__).resolve().parent.parent / "data" / "retrieval_queries.json").read_text())
    old, new = make_retrievers(s)
    print(f"{'set':12} {'queries':>9} | {'BEFORE in-scope answered':>25} {'out-scope rejected':>19} | {'AFTER in-scope answered':>24} {'out-scope rejected':>19}")
    for name, d in data.items():
        if name.startswith("_"):
            continue
        n_in, n_out = len(d["in_scope"]), len(d["out_of_scope"])
        a = evaluate(old, d["in_scope"], d["out_of_scope"])
        b = evaluate(new, d["in_scope"], d["out_of_scope"])
        print(f"{name:12} {n_in:>3}+{n_out:<5} | {a[0]:>20}/{n_in:<4} {n_out - a[1]:>14}/{n_out:<4} | {b[0]:>19}/{n_in:<4} {n_out - b[1]:>14}/{n_out:<4}")
        for q in b[2]:
            print(f"    AFTER missed in-scope : {q}")
        for q in b[3]:
            print(f"    AFTER leaked out-scope: {q}")

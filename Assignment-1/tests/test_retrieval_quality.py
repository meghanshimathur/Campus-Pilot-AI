"""Regression tests for hybrid (semantic + keyword) handbook retrieval.

Origin: the Phase 6 evaluation showed 'student visa extension' returned handbook section 10 (similarity 0.41,
just over the old 0.40 cut-off) although the handbook never mentions visas, while in-scope queries such as
're-evaluation of answer script' scored only 0.24.
"""
import json
from pathlib import Path

import pytest

from campuspilot.config import ROOT, load_settings
from campuspilot.rag import KeywordScorer, _same_term, _terms, accept_chunk, chunk_handbook

HANDBOOK = ROOT / "data" / "handbook" / "student_handbook.md"
RULE = dict(min_score=0.40, strong_score=0.50, min_coverage=0.18, keyword_coverage=0.60, keyword_min_score=0.10)
_MODEL = Path.home() / ".cache" / "chroma" / "onnx_models" / "all-MiniLM-L6-v2"
needs_model = pytest.mark.skipif(not _MODEL.exists(), reason="Chroma default embedding model not cached")


@pytest.fixture(scope="module")
def scorer():
    return KeywordScorer(chunk_handbook(HANDBOOK.read_text()))


# ---- the acceptance rule, with the real (similarity, coverage, distinctive-terms) values measured in the eval ----
@pytest.mark.parametrize("sem,cov,n,expected", [
    (0.41, 0.13, 1, None),                  # 'student visa extension': only the generic word 'student' overlaps
    (0.43, 0.10, 1, None),                  # 'student accommodation off campus'
    (0.42, 0.00, 0, None),                  # 'education loan': similar-sounding, zero overlap
    (0.24, 1.00, 3, "keyword"),             # 're-evaluation of answer script': weak embedding, exact terms
    (0.13, 1.00, 2, "keyword"),             # 'change my elective'
    (0.25, 1.00, 1, None),                  # 'work permit': ONE accidental word-form match must not be enough
    (0.52, 0.00, 0, "semantic"),            # pure paraphrase ('money back' ~ 'refund') with strong similarity
    (0.49, 0.20, 1, "semantic+keyword"),    # 'grades drop' ~ scholarship CGPA rule
    (0.47, 0.29, 1, "semantic+keyword"),    # 'smartwatch to the exam'
    (0.30, 0.48, 2, None),                  # weak similarity and partial overlap: not enough
    (0.05, 1.00, 5, None),                  # keyword match with the embedding disagreeing entirely
])
def test_accept_rule(sem, cov, n, expected):
    assert accept_chunk(sem, cov, n, **RULE) == expected


def test_old_global_threshold_is_not_simply_raised():
    # in-scope queries below the OLD 0.40 cut-off are still answerable (this is what raising the threshold would break)
    assert accept_chunk(0.24, 1.0, 3, **RULE) is not None and accept_chunk(0.13, 1.0, 2, **RULE) is not None
    # ...and a query ABOVE the old cut-off is now rejected when keywords disagree
    assert accept_chunk(0.41, 0.13, 1, **RULE) is None


# ---- keyword coverage on the real handbook chunks ----
def test_visa_query_has_no_keyword_support_anywhere(scorer):
    for cid in scorer.chunk_terms:
        cov, n = scorer.score("student visa extension", cid)
        assert cov < 0.18 and n == 0, cid  # 'visa' and 'extension' are absent from the handbook


def test_reevaluation_query_matches_exam_chunk_strongly(scorer):
    cov, n = scorer.score("re-evaluation of answer script", "student_handbook.md#s5-1")
    assert cov >= 0.9 and n >= 2
    other, _ = scorer.score("re-evaluation of answer script", "student_handbook.md#s8-1")
    assert other < cov / 2


def test_unknown_terms_count_against_coverage(scorer):
    full, _ = scorer.score("hostel gate", "student_handbook.md#s8-1")
    diluted, _ = scorer.score("hostel gate zeppelin", "student_handbook.md#s8-1")
    assert full > 0.9 and diluted < full - 0.2


def test_generic_filler_words_do_not_swamp_real_terms(scorer):
    with_filler, _ = scorer.score("can I get a refund if I leave in the first week", "student_handbook.md#s3-1")
    assert "get" not in _terms("can I get a refund") and "first" not in _terms("in the first week")
    assert with_filler > 0.2


def test_empty_or_stopword_query_has_zero_coverage(scorer):
    assert scorer.score("", "student_handbook.md#s1-1") == (0.0, 0)
    assert scorer.score("what is the", "student_handbook.md#s1-1") == (0.0, 0)


@pytest.mark.parametrize("a,b,same", [
    ("plagiarism", "plagiaris", True),   # plagiarising -> plagiaris vs plagiarism
    ("attendance", "attend", True), ("scholarship", "scholar", True), ("evaluation", "evaluate", True),
    ("registration", "register", False),  # only 6 shared letters: a known, accepted miss of the strict matcher
    ("elective", "election", False),     # the false match that briefly made 'student council elections' look relevant
    ("permit", "permission", False), ("fee", "feedback", False), ("exam", "example", False), ("library", "liberty", False),
])
def test_term_matching_is_strict_enough(a, b, same):
    assert _same_term(a, b) is same


# ---- retriever with the offline test embedder (no model needed) ----
def test_visa_query_returns_nothing_offline(retriever):
    assert retriever.search("student visa extension") == []
    assert retriever.search("student council elections") == []


def test_low_similarity_exact_term_query_found_offline(retriever):
    hits = retriever.search("re-evaluation of answer script")
    assert hits and hits[0]["section"].startswith("5.") and hits[0]["keyword_coverage"] >= 0.6


def test_hits_explain_why_they_matched(retriever):
    hit = retriever.search("hostel gate closing time")[0]
    assert hit["match"] in ("semantic", "keyword", "semantic+keyword") and hit["section"].startswith("8.")


# ---- real embedding model (skipped unless already cached; never downloads) ----
from chromadb import EmbeddingFunction  # noqa: E402


class CachedEmbedding(EmbeddingFunction):
    """Wraps Chroma's default local model and memoizes by text, so each query is embedded once per test session
    (speed only: it returns exactly what the wrapped model returns)."""
    _cache: dict = {}

    def __init__(self):
        from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
        self._inner = DefaultEmbeddingFunction()

    @staticmethod
    def name() -> str:
        return "cached-default-minilm"

    def get_config(self) -> dict:
        return {}

    @staticmethod
    def build_from_config(config: dict):
        return CachedEmbedding()

    def __call__(self, input):
        missing = [t for t in dict.fromkeys(input) if t not in self._cache]
        if missing:
            for t, v in zip(missing, self._inner(missing)):
                self._cache[t] = v
        return [self._cache[t] for t in input]


@pytest.fixture(scope="module")
def real(tmp_path_factory):
    from campuspilot.rag import HandbookRetriever
    s = load_settings()
    return HandbookRetriever(HANDBOOK, tmp_path_factory.mktemp("chroma"), embedding_function=CachedEmbedding(), top_k=3,
                             min_score=s.handbook_min_score, strong_score=s.handbook_strong_score)


@needs_model
def test_failed_evaluation_case_now_not_found(real):
    assert real.search("student visa extension") == []  # was: s10-1 at 0.41 -> evidence returned


@needs_model
@pytest.mark.parametrize("q,section", [
    ("re-evaluation of answer script", "5."),                       # similarity 0.24 (< old 0.40)
    ("How do I apply for re-evaluation of my answer script?", "5."),  # 0.27
    ("change my elective", "10."),                                  # 0.13
    ("what if I am caught plagiarising", "11."),                    # morphology: plagiaris-ing vs plagiarism
    ("How do I get my money back if I quit the course in the first two weeks?", "3."),  # pure paraphrase, 0.52
    ("Will I lose my scholarship if my grades drop?", "4."),
    ("Can I bring my smartwatch to the exam?", "5."),
    ("The payment portal failed. Can I pay on the next working day?", "2."),
    ("What time does the hostel gate close?", "8."),
    ("How many books can I borrow from the library?", "7."),
])
def test_low_and_high_scoring_in_scope_queries_still_answered(real, q, section):
    assert any(h["section"].startswith(section) for h in real.search(q)), q


@needs_model
@pytest.mark.parametrize("q", ["student visa extension", "visa renewal for international students", "student council elections",
                               "student accommodation off campus", "How do I get a work permit?", "Where can I park my car on campus?",
                               "What is the capital of France?", "How do I apply for an education loan?"])
def test_out_of_scope_queries_rejected(real, q):
    assert real.search(q) == [], q


@pytest.fixture(scope="module")
def labelled():
    return json.loads((ROOT / "data" / "retrieval_queries.json").read_text())


@needs_model
def test_design_set_regression_floor(real, labelled):
    from scripts_eval import evaluate
    d = labelled["design"]
    answered, leaked, missed, leaks = evaluate(real, d["in_scope"], d["out_of_scope"])
    assert answered >= 31 and leaked == 0, (missed, leaks)  # measured: 32/32 answered, 0 leaks


@needs_model
def test_hybrid_not_worse_than_semantic_only_baseline_on_any_set(real, labelled, tmp_path_factory):
    from campuspilot.rag import HandbookRetriever
    from scripts_eval import evaluate
    old = HandbookRetriever(HANDBOOK, tmp_path_factory.mktemp("c2"), embedding_function=CachedEmbedding(), top_k=3,
                            strong_score=0.40, min_score=9, keyword_coverage=9)
    for name, d in labelled.items():
        if name.startswith("_"):
            continue
        a_old, l_old, *_ = evaluate(old, d["in_scope"], d["out_of_scope"])
        a_new, l_new, *_ = evaluate(real, d["in_scope"], d["out_of_scope"])
        assert a_new + (len(d["out_of_scope"]) - l_new) >= a_old + (len(d["out_of_scope"]) - l_old), name
        assert a_new >= a_old - 1, name  # recall must not collapse on any set


@needs_model
@pytest.mark.xfail(strict=True, reason="KNOWN LIMITATION: adjacent-topic questions (the handbook covers the topic but not "
                                       "the fact asked) share real words and similarity; retrieval scores cannot reject them. "
                                       "The LLM must say 'not in the excerpt'. strict=True: remove this xfail if it is ever fixed.")
@pytest.mark.parametrize("q", ["Is smoking allowed in the hostel?", "What is the fee for the MBA programme?",
                               "Can I bring my pet to the hostel?"])
def test_known_limitation_adjacent_topic_questions_still_return_chunks(real, q):
    assert real.search(q) == []

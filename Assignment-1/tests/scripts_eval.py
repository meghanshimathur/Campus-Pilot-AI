"""Shared helper: load scripts/evaluate_retrieval.py's evaluate() for tests."""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "evaluate_retrieval", Path(__file__).resolve().parent.parent / "scripts" / "evaluate_retrieval.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
evaluate = _mod.evaluate

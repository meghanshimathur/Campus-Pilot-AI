"""Pre-build the handbook index (optional; the app also builds it on first search).
Usage: .venv/bin/python scripts/ingest_handbook.py [--force]
The first run downloads Chroma's local embedding model (~80MB)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from campuspilot.config import load_settings  # noqa: E402
from campuspilot.rag import HandbookRetriever  # noqa: E402

if __name__ == "__main__":
    s = load_settings()
    n = HandbookRetriever(s.handbook_path, s.chroma_dir).ingest(force="--force" in sys.argv)
    print(f"Handbook index ready: {n} chunks in {s.chroma_dir}")

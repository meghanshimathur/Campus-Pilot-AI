"""Configuration loaded from environment variables (and an optional .env file)."""
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_path: Path
    llm_provider: str | None
    # repr=False keeps keys out of logs and tracebacks
    gemini_api_key: str | None = field(default=None, repr=False)
    openai_api_key: str | None = field(default=None, repr=False)
    openai_model: str = "gpt-4o-mini"  # override with OPENAI_MODEL
    gemini_model: str = "gemini-3.6-flash"  # override with GEMINI_MODEL (2.5-flash: 404 for new keys; 3.7/3.8-flash: 503 overload when tested)
    # Handbook retrieval (see rag.accept_chunk). Scores were calibrated on Chroma's default MiniLM embeddings.
    # min_score: similarity needed together with keyword overlap; strong: similarity trusted on its own.
    handbook_path: Path = ROOT / "data" / "handbook" / "student_handbook.md"
    chroma_dir: Path = ROOT / "data" / "chroma"
    handbook_top_k: int = 3
    reports_dir: Path = ROOT / "reports"        # evaluation reports written by scripts/run_evaluation.py
    frontend_dir: Path = ROOT / "frontend"      # static dashboard served at /
    handbook_min_score: float = 0.40  # override with HANDBOOK_MIN_SCORE
    handbook_strong_score: float = 0.50  # override with HANDBOOK_STRONG_SCORE


def _detect_provider(explicit, gemini, openai):
    if explicit:
        return explicit.lower()
    if gemini:
        return "gemini"
    if openai:
        return "openai"
    return None


def load_settings() -> Settings:
    load_dotenv(ROOT / ".env")
    gemini = os.getenv("GEMINI_API_KEY") or None
    openai = os.getenv("OPENAI_API_KEY") or None
    db = Path(os.getenv("DATABASE_PATH") or "data/campuspilot.sqlite3")
    if not db.is_absolute():
        db = ROOT / db
    return Settings(
        data_dir=ROOT / "data",
        database_path=db,
        llm_provider=_detect_provider(os.getenv("LLM_PROVIDER") or None, gemini, openai),
        gemini_api_key=gemini,
        openai_api_key=openai,
        openai_model=os.getenv("OPENAI_MODEL") or "gpt-4o-mini",
        gemini_model=os.getenv("GEMINI_MODEL") or "gemini-3.6-flash",
        handbook_min_score=float(os.getenv("HANDBOOK_MIN_SCORE") or 0.40),
        handbook_strong_score=float(os.getenv("HANDBOOK_STRONG_SCORE") or 0.50),
    )

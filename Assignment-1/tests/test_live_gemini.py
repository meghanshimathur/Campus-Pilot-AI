"""Opt-in live check against the real Gemini API. It spends real API calls, so it runs ONLY when you ask for it:

    RUN_LIVE_TESTS=1 .venv/bin/python -m pytest tests/test_live_gemini.py -rs

A key in .env is not enough (a plain `pytest` must never make paid/real requests). Prints no secrets."""
import os
from dataclasses import replace

import pytest

from campuspilot.agent import Agent
from campuspilot.config import load_settings
from campuspilot.db import init_db
from campuspilot.provider import GeminiProvider
from campuspilot.tool_registry import ToolRegistry

settings = load_settings()
pytestmark = pytest.mark.skipif(
    os.getenv("RUN_LIVE_TESTS") != "1" or not os.getenv("GEMINI_API_KEY"),
    reason="live test: set RUN_LIVE_TESTS=1 and a GEMINI_API_KEY to run (makes real API requests)")


def test_live_fee_question_calls_tool(tmp_path):
    s = replace(settings, database_path=tmp_path / "live.sqlite3")
    init_db(s.database_path)
    provider = GeminiProvider(s.gemini_api_key, s.gemini_model)
    registry = ToolRegistry(s.data_dir, s.database_path)
    r = Agent(provider, registry).run("What is my current fee status?", "STU003")
    assert r.error is None
    assert "check_fee_status" in [t["name"] for t in r.tools_called]
    assert r.response

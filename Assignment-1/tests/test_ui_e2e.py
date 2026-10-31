"""Real-browser tests of the dashboard (headless Chrome over the DevTools Protocol, driven by Node; nothing to install).

Skipped automatically when Node or Chrome is missing. They NEVER call a live LLM: one server uses a scripted test LLM
(tests/e2e/demo_server.py), the other runs the real app with provider keys stripped.
"""
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from campuspilot.config import ROOT

CHROME = os.environ.get("CHROME_BIN", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
pytestmark = pytest.mark.skipif(shutil.which("node") is None or not Path(CHROME).exists(),
                                reason="needs Node and Google Chrome")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_against_server(script: str, mode: str, tmp_path):
    port = free_port()
    env = {**os.environ, "GEMINI_API_KEY": "", "OPENAI_API_KEY": "", "LLM_PROVIDER": ""}  # belt and braces: no live calls
    server = subprocess.Popen([sys.executable, str(ROOT / "tests" / "e2e" / "demo_server.py"), str(port), mode],
                              cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for _ in range(80):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1).read()
                break
            except Exception:
                if server.poll() is not None:
                    raise RuntimeError(f"demo server died:\n{server.stdout.read()}")
                time.sleep(0.25)
        else:
            raise RuntimeError("demo server did not start")
        shots = tmp_path / "shots"
        out = subprocess.run(["node", str(ROOT / "tests" / "e2e" / script), f"http://127.0.0.1:{port}", str(shots)],
                             capture_output=True, text=True, cwd=ROOT, timeout=300, env={**os.environ, "CHROME_BIN": CHROME})
        return out
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()


def test_dashboard_full_flow_with_scripted_llm(tmp_path):
    out = run_against_server("ui_smoke.mjs", "scripted", tmp_path)
    assert out.returncode == 0, out.stdout[-3000:] + out.stderr[-1500:]
    assert "browser checks passed" in out.stdout and "FAILED" not in out.stdout


def test_dashboard_real_backend_without_api_key(tmp_path):
    out = run_against_server("ui_no_key.mjs", "nokey", tmp_path)
    assert out.returncode == 0, out.stdout[-3000:] + out.stderr[-1500:]
    assert "no-key browser checks passed" in out.stdout

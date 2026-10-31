"""TEST-ONLY server for browser tests: the real FastAPI app (real tools, real handbook retrieval, real memory and
traces, real endpoints) with a keyword-driven SCRIPTED LLM named 'scripted-e2e'. It exists so the dashboard can be
exercised without an API key. Its assistant text is test data, not model output.

  .venv/bin/python tests/e2e/demo_server.py PORT            # scripted test LLM
  .venv/bin/python tests/e2e/demo_server.py PORT nokey      # real app with NO provider (what you see before adding a key)
"""
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import uvicorn  # noqa: E402

from campuspilot.api import create_app  # noqa: E402
from campuspilot.config import load_settings  # noqa: E402
from campuspilot.provider import ChatResponse, LLMProvider, ToolCall  # noqa: E402


MARKDOWN_SAMPLE = """Here is your current fee status:

* **Student Name:** Rohan Mehta (ID: STU003)
* **Amount Due:** ₹3,00,000
* **Status:** Overdue

## Next steps
1. Use the `check_fee_status` tool for details.
2. See the [fee policy](https://example.com/fees) or *ask* for help.
   * nested point with **bold**
   * another nested point

> Tip: tool names like check_fee_status and ids like student_handbook.md#s2-1 stay literal.

```
code line 1
code <b>line</b> 2
```

| col a | col b |
|---|---|
| 1 | 2 |
"""

UNSAFE_SAMPLE = """Unsafe test: <script>window.__xss=2</script> <img src=x onerror="window.__xss=3"> <b id="pwn2">raw b</b>
[bad link](javascript:window.__xss=4) and ![tracking pixel](http://127.0.0.1:1/pixel.png) and [good link](https://example.com/page) and **still bold**"""


class ScriptedE2E(LLMProvider):
    name = "scripted-e2e"

    def chat(self, messages, tool_schemas):
        last = messages[-1]
        user = next(m["content"] for m in reversed(messages) if m["role"] == "user").lower()
        tokens = dict(input_tokens=12, output_tokens=7) if "tokens please" in user else {}
        if "markdown demo" in user:
            return ChatResponse(content=MARKDOWN_SAMPLE, **tokens)
        if "unsafe markdown" in user:
            return ChatResponse(content=UNSAFE_SAMPLE, **tokens)
        if "slow" in user:
            time.sleep(1.2)
        if "boom" in user:
            raise RuntimeError("simulated provider failure with secret-key-123")
        if last["role"] == "tool":
            import json
            result = json.loads(last["content"])
            if "error" in result:
                return ChatResponse(content=f"That did not work: {result['error']}", **tokens)
            if "chunks" in result and "found" in result:
                if not result["found"]:
                    return ChatResponse(content="I could not find that in the handbook, so I won't guess a policy. "
                                                "You can raise a support request.", **tokens)
                ids = ", ".join(c["chunk_id"] for c in result["chunks"])
                return ChatResponse(content=f"According to the handbook [{ids}]: {result['chunks'][0]['text'].splitlines()[1]}", **tokens)
            if "saved" in result:
                return ChatResponse(content="Noted. I will remember that." if result["saved"] else "I can't store that.", **tokens)
            if "due" in result:
                return ChatResponse(content=f"Your fee status is {result['status']}; {result['due']} is due.", **tokens)
            return ChatResponse(content="Here is what I found.", **tokens)
        if "call me" in user:
            name = messages[-1]["content"].lower().split("call me")[1].split()[0].strip(".,!").capitalize()
            return ChatResponse(tool_calls=[ToolCall("c1", "remember_preference", {"key": "preferred_name", "value": name})], **tokens)
        if "fee" in user:
            return ChatResponse(tool_calls=[ToolCall("c1", "check_fee_status", {})], **tokens)
        if "office hours" in user:
            return ChatResponse(tool_calls=[ToolCall("c1", "get_professor_office_hours", {"professor_name": "Kavita Rao"})], **tokens)
        if any(w in user for w in ("portal", "policy", "park", "visa", "attendance", "library", "hostel")):
            return ChatResponse(tool_calls=[ToolCall("c1", "search_handbook", {"query": messages[-1]["content"]})], **tokens)
        return ChatResponse(content="Hello! I can help with fees, scholarships, courses and the handbook.", **tokens)


def make_reports_dir(tmp: Path) -> Path:
    """Temp reports dir: copies of the real saved reports plus ONE synthetic 'live' report, clearly labelled
    TEST-FIXTURE, and ONE deliberately inconsistent 'live' report (scripted runs), so the browser test can exercise the LIVE rendering path (no real live run exists yet)."""
    import json
    import shutil

    from campuspilot.config import ROOT
    from campuspilot.evaluation import compute_metrics

    out = tmp / "reports"
    out.mkdir()
    real = sorted((ROOT / "reports").glob("evaluation_offline_*.json"))
    for f in real:
        shutil.copy(f, out / f.name)
    if real:
        data = json.loads(real[-1].read_text())
        scripted = json.loads(json.dumps(data))  # untouched copy of a real scripted report, used for the bad fixture below
        for i, run in enumerate(data["runs"]):
            run["provider"] = "TEST-FIXTURE"        # a LIVE report's runs must come from a real (here: fixture) provider
            if i < 4:                                # give a few runs provider-style token usage
                run["input_tokens"], run["output_tokens"] = 100 + i, 20 + i
        data.update(mode="live", meta={"timestamp": "2020-01-01 00:00:00", "provider": "TEST-FIXTURE", "model": "fixture-model"},
                    metrics=compute_metrics(data["runs"]))
        (out / "evaluation_live_20200101_000000.json").write_text(json.dumps(data))
        # A live report in the NEW format where the provider blocked part of the run (quota), then the run was stopped early
        blocked = json.loads(json.dumps(scripted))
        for i, run in enumerate(blocked["runs"]):
            run["provider"] = "TEST-FIXTURE"
            if i < 4:
                run["outcome"] = "completed"
            else:
                gone = i >= 6
                run.update(outcome="skipped" if gone else "blocked_by_provider", error_kind=None if gone else "quota", success=False,
                           trace_recorded=not gone,                       # a skipped run was never executed, so it has no trace
                           tool_choice_appropriate=False, check_passed=False, called_tools=[], evidence_ids=[], latency_ms=0 if gone else 9000,
                           run_error="not run: provider unavailable" if gone else "llm_error: ClientError 429 RESOURCE_EXHAUSTED: quota",
                           note="not run: stopped after 2 consecutive provider errors" if gone else "blocked by provider (quota): the model was not evaluated")
        blocked.update(mode="live", meta={"timestamp": "2018-01-01 00:00:00", "provider": "TEST-FIXTURE", "model": "fixture-model",
                                          "pacing": {"min_interval": 6.0, "quota_delays": [10.0, 20.0, 40.0], "max_wait": 90.0, "stop_after": 2}},
                       metrics=compute_metrics(blocked["runs"]))
        (out / "evaluation_live_20180101_000000.json").write_text(json.dumps(blocked))
        # DELIBERATELY INCONSISTENT fixture: scripted runs saved under mode "live" (what the integrity check must catch)
        scripted["mode"] = "live"
        (out / "evaluation_live_20190101_000000.json").write_text(json.dumps(scripted))
    return out


def main(port: int, mode: str = "scripted"):
    base = load_settings()
    tmp = Path(tempfile.mkdtemp(prefix="cp_e2e_"))
    if mode == "nokey":
        # Strip any real keys from the loaded settings so a browser test can never trigger a live API call.
        settings = replace(base, database_path=tmp / "e2e.sqlite3", llm_provider=None, gemini_api_key=None, openai_api_key=None)
        app = create_app(settings)
    else:
        settings = replace(base, database_path=tmp / "e2e.sqlite3", llm_provider="scripted-e2e", reports_dir=make_reports_dir(tmp))
        app = create_app(settings, provider=ScriptedE2E())
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main(int(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else "scripted")

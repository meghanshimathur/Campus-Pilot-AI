"""Run the Assignment 1 evaluation.

  .venv/bin/python scripts/run_evaluation.py            # live if an API key is configured, else explains and exits
  .venv/bin/python scripts/run_evaluation.py --live     # require a real provider (makes real API calls)
  .venv/bin/python scripts/run_evaluation.py --offline  # scripted LLM: pipeline check only, no API calls

Live runs are paced and quota-aware (all optional): --min-interval SECONDS between model requests, --quota-retries N and
--quota-base-delay SECONDS for HTTP 429 (exponential: base, 2x, 4x ...; the server's retry hint is honoured, capped at
--max-wait), and --stop-after N consecutive provider-blocked turns, after which the remaining turns are skipped.
Runs blocked by the provider (quota/outage) are reported separately and never counted as agent failures.
Exit codes: 0 every run passed | 1 a completed run failed | 2 no provider configured | 3 inconclusive (some runs were
blocked by the provider and none of the completed ones failed).

Uses a throw-away SQLite database (your real data/campuspilot.sqlite3 is not touched) and writes a
Markdown + JSON report to reports/. Fictional data only; never prints API keys.
"""
import argparse
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from campuspilot.config import ROOT, load_settings  # noqa: E402
from campuspilot.db import init_db  # noqa: E402
from campuspilot.evaluation import build_cases, compute_metrics, render_markdown, run_cases  # noqa: E402
from campuspilot.memory import MemoryStore  # noqa: E402
from campuspilot.provider import build_provider  # noqa: E402
from campuspilot.rag import HandbookRetriever  # noqa: E402
from campuspilot.tool_registry import ToolRegistry  # noqa: E402
from campuspilot.tracing import TraceStore  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true", help="require a configured real LLM provider")
    mode.add_argument("--offline", action="store_true", help="scripted LLM; validates the pipeline only")
    ap.add_argument("--out-dir", default=str(ROOT / "reports"))
    live_opts = ap.add_argument_group("live-run pacing and quota handling (ignored by --offline)")
    live_opts.add_argument("--min-interval", type=float, default=6.0, metavar="SEC",
                           help="minimum seconds between model requests (default 6 = at most 10 per minute; 0 disables)")
    live_opts.add_argument("--quota-retries", type=int, default=3, metavar="N",
                           help="retries after an HTTP 429 (default 3; 0 disables)")
    live_opts.add_argument("--quota-base-delay", type=float, default=10.0, metavar="SEC",
                           help="first 429 wait; doubles each retry (default 10 -> 10, 20, 40)")
    live_opts.add_argument("--max-wait", type=float, default=90.0, metavar="SEC",
                           help="longest single wait; if the server asks for more, give up (default 90)")
    live_opts.add_argument("--stop-after", type=int, default=2, metavar="N",
                           help="skip the remaining turns after N consecutive provider-blocked turns (default 2; 0 = never)")
    args = ap.parse_args(argv)
    for name in ("min_interval", "quota_retries", "quota_base_delay", "max_wait", "stop_after"):
        if getattr(args, name) < 0:
            ap.error(f"--{name.replace('_', '-')} must not be negative")

    settings = load_settings()
    quota_delays = tuple(args.quota_base_delay * 2 ** i for i in range(args.quota_retries))
    pacing = {"min_interval": args.min_interval, "quota_delays": list(quota_delays), "max_wait": args.max_wait,
              "stop_after": args.stop_after}
    # Offline mode must never construct a real provider (and so can never make an API call): build_provider is not reached.
    provider = None if args.offline else build_provider(
        settings, min_interval=args.min_interval, quota_delays=quota_delays, max_wait=args.max_wait, jitter=1.0)
    if not args.offline and provider is None:
        print("No LLM provider configured (set GEMINI_API_KEY or OPENAI_API_KEY in .env), so a live evaluation "
              "cannot run.\nRe-run with --offline for a pipeline check (it does NOT measure model quality).")
        return 2
    mode_name = "offline" if provider is None else "live"

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "evaluation.sqlite3"
        init_db(db)
        memory, tracer = MemoryStore(db), TraceStore(db)
        retriever = HandbookRetriever(settings.handbook_path, settings.chroma_dir,
                                      top_k=settings.handbook_top_k, min_score=settings.handbook_min_score,
                                      strong_score=settings.handbook_strong_score)
        registry = ToolRegistry(settings.data_dir, db, retriever, memory)
        if provider is not None:
            print(f"Live evaluation: model {settings.gemini_model if settings.llm_provider == 'gemini' else settings.openai_model}; "
                  f">= {args.min_interval:g}s between model requests; HTTP 429 backoff {list(quota_delays)}s "
                  f"(max {args.max_wait:g}s per wait); stop after {args.stop_after} consecutive provider-blocked turns.", flush=True)
        rows = run_cases(build_cases(), registry, memory, tracer, provider,
                         stop_after_provider_errors=args.stop_after if provider is not None else 0)

    metrics = compute_metrics(rows)
    meta = {"timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "provider": rows[0]["provider"] if rows else "n/a",
            "model": next((r["model"] for r in rows if r["model"]), "n/a (none reported)")}
    if provider is not None:
        meta["pacing"] = pacing
    report = render_markdown(mode_name, rows, metrics, meta)

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"evaluation_{mode_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    (out / f"{stem}.md").write_text(report, encoding="utf-8")
    (out / f"{stem}.json").write_text(json.dumps({"mode": mode_name, "meta": meta, "metrics": metrics, "runs": rows},
                                                 indent=2), encoding="utf-8")
    print(report)
    print(f"Saved: {out / (stem + '.md')}\n       {out / (stem + '.json')}")
    if metrics["runs_completed"] and metrics["runs_passed"] < metrics["runs_completed"]:
        return 1                      # a run that reached the model genuinely failed
    if metrics["runs_blocked"] or metrics["runs_skipped"] or not metrics["runs_completed"]:
        return 3                      # inconclusive: the provider blocked some runs
    return 0


if __name__ == "__main__":
    sys.exit(main())

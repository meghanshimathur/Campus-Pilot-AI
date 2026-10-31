"""Run the Assignment 2 security evaluation (OFFLINE only: a scripted model, temporary databases, fictional users).

  .venv/bin/python scripts/run_security_evaluation.py                 # all scenarios + the Assignment 1 cases re-run, report -> reports/
  .venv/bin/python scripts/run_security_evaluation.py --out-dir /tmp/x
  .venv/bin/python scripts/run_security_evaluation.py --skip-assignment1-cases   # skip the part that loads the local embedding model

There is no live mode: this script never builds an LLM provider, never reads `.env`, and never touches the real database. It
reads the saved Assignment 1 reports in reports/ (read-only) for the comparison and writes assignment2_evaluation_<time>.md/.json
without overwriting any existing file.
Exit codes: 0 every scenario passed | 1 a scenario or check failed.
"""
import argparse
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from campuspilot.config import ROOT  # noqa: E402
from campuspilot.security_evaluation import compute_security_metrics, run_all  # noqa: E402
from campuspilot.security_report import (build_report, build_retriever, load_assignment1_baseline, render_markdown,  # noqa: E402
                                         run_assignment1_cases, save_report)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out-dir", default=str(ROOT / "reports"))
    ap.add_argument("--baseline-dir", default=str(ROOT / "reports"), help="where the saved Assignment 1 reports are read from (read-only)")
    ap.add_argument("--skip-assignment1-cases", action="store_true", help="do not re-run the 12 Assignment 1 cases (they load the local embedding model)")
    args = ap.parse_args(argv)

    results = run_all()
    metrics = compute_security_metrics(results)
    rerun = None
    if not args.skip_assignment1_cases:
        with tempfile.TemporaryDirectory(prefix="cp_eval_chroma_") as chroma:      # a throw-away index: data/chroma is not touched
            rerun = run_assignment1_cases(build_retriever(Path(chroma)))
    meta = {"timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "provider": "offline-scripted", "model": "n/a (no model called)"}
    report = build_report(results, metrics, load_assignment1_baseline(Path(args.baseline_dir)), rerun, meta)
    print(render_markdown(report))
    md, js = save_report(report, Path(args.out_dir))
    print(f"Saved: {md}\n       {js}")
    failed = metrics["failed_checks"] or metrics["errors"] or (rerun and rerun["failed_runs"])
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

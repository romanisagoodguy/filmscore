#!/usr/bin/env python3
"""
Full Approach 2 gather (inherit Approach 1) + 1 hour cool-down + Wikipedia recheck.

Procedure (user-requested):
  1) LOOP all plan requests site-by-site:
       for each request: pause (throttle) → launch command → next
     until main gather finishes.
  2) WAIT 1 hour (wiki IP cool-down).
  3) RECHECK all non-success Wikipedia rows (re-open → loop again with pauses).
  4) Write final reports.

Usage:
  python scripts/run_full_gather_wiki_recheck.py
  python scripts/run_full_gather_wiki_recheck.py --wait-hours 1
  python scripts/run_full_gather_wiki_recheck.py --wait-hours 0   # recheck immediately (debug)
  python scripts/run_full_gather_wiki_recheck.py --recheck-only  # skip gather, only wait+wiki
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

# project root on path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from psychofilm_analyzer.utils.localtime import now_str


def _now() -> str:
    return now_str()


def main() -> int:
    ap = argparse.ArgumentParser(description="Full A2 gather + 1h wiki recheck")
    ap.add_argument(
        "-i",
        "--input",
        default="Список-фильмов v1.xlsx",
        help="Catalog Excel",
    )
    ap.add_argument(
        "--wait-hours",
        type=float,
        default=1.0,
        help="Hours to wait after main gather before wiki recheck (default 1)",
    )
    ap.add_argument(
        "--recheck-only",
        action="store_true",
        help="Skip main gather; only cool-down + wikipedia recheck on existing plan",
    )
    ap.add_argument(
        "--no-resume",
        action="store_true",
        help="Rebuild plan (ignore existing request_plan.jsonl)",
    )
    ap.add_argument(
        "--request-debug",
        action="store_true",
        default=True,
        help="Enable request debug note (default on)",
    )
    ap.add_argument(
        "--skip-skipped",
        action="store_true",
        help="Do not re-open wiki skipped (not_found) rows on recheck",
    )
    args = ap.parse_args()

    from psychofilm_analyzer.config import load_config
    from psychofilm_analyzer.gather_v2.plan_store import PlanStore
    from psychofilm_analyzer.gather_v2.runner import run_gather_v2
    from psychofilm_analyzer.gather_v2.wiki_recheck import wait_and_recheck_wikipedia
    from psychofilm_analyzer.gather_v2.wiki_report import write_wikipedia_pipeline_report
    from psychofilm_analyzer.io.input_loader import load_titles
    from psychofilm_analyzer.pipeline import configure_logging

    configure_logging(False)
    config = load_config(None)
    out_dir = Path((config.get("output") or {}).get("dir", "output"))
    a2 = config.get("gather_v2") or {}
    plan_dir = Path(a2.get("plan_dir") or (out_dir / "gather_v2"))
    reports_dir = Path(a2.get("reports_dir") or (plan_dir / "reports"))
    plan_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)

    log_path = plan_dir / "run_full_with_wiki_recheck.log"
    wait_sec = max(0.0, float(args.wait_hours) * 3600.0)

    def log(msg: str) -> None:
        line = f"[{_now()}] {msg}"
        print(line, flush=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    log("=" * 72)
    log("FULL GATHER + WIKI RECHECK ORCHESTRATOR")
    log(f"  input={args.input}")
    log(f"  wait_hours={args.wait_hours} ({wait_sec:.0f}s)")
    log(f"  recheck_only={args.recheck_only}")
    log(f"  plan_dir={plan_dir}")
    log("=" * 72)

    gather_summary: dict = {}
    if not args.recheck_only:
        log("PHASE 1: full Approach 2 gather (inherit A1, ALL remaining pending films)")
        log("  Loop per site: claim request → pause → HTTP command → next until empty")
        items = load_titles(args.input)
        log(f"  catalog loaded: {len(items)} titles")
        try:
            gather_summary = run_gather_v2(
                items,
                config,
                resume=not args.no_resume,
                plan_only=False,
                request_debug=bool(args.request_debug),
                inherit_approach1=True,
                pending_limit=None,  # ALL remaining
            )
            log(f"PHASE 1 complete: {json.dumps(gather_summary, ensure_ascii=False)[:800]}")
        except Exception as exc:  # noqa: BLE001
            log(f"PHASE 1 FAILED: {exc}")
            log(traceback.format_exc())
            return 1
    else:
        log("PHASE 1 skipped (--recheck-only)")

    log(
        f"PHASE 2: wait {wait_sec:.0f}s ({args.wait_hours}h) for Wikipedia cool-down, "
        "then recheck all non-success wiki requests"
    )
    store = PlanStore(plan_dir)
    n = store.load()
    log(f"  plan loaded: {n} requests")
    try:
        recheck = wait_and_recheck_wikipedia(
            store,
            config=config,
            reports_dir=reports_dir,
            wait_sec=wait_sec,
            include_skipped=not args.skip_skipped,
        )
        log(f"PHASE 2 complete: {json.dumps(recheck, ensure_ascii=False)[:800]}")
    except Exception as exc:  # noqa: BLE001
        log(f"PHASE 2 FAILED: {exc}")
        log(traceback.format_exc())
        return 2

    # Final wiki report
    store.load()
    write_wikipedia_pipeline_report(
        store,
        reports_dir / "pipeline_wikipedia.txt",
        final=True,
    )
    final = {
        "finished_at": _now(),
        "gather": gather_summary,
        "wiki_recheck": recheck,
        "wikipedia_final": store.counts_by_status("wikipedia"),
        "all_sites": store.counts_by_status(),
    }
    out = plan_dir / "full_gather_wiki_recheck_summary.json"
    out.write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"DONE. Summary → {out}")
    log(f"  wiki: {final['wikipedia_final']}")
    log(f"  reports: {reports_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

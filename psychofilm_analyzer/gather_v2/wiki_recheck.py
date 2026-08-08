"""
Wikipedia problem recheck pass (Approach 2).

After the main gather finishes and the IP has cooled (default 1 hour):
  1) Re-open non-success wiki plan rows as pending
  2) Loop: claim next → pause (throttle) → execute → next
  3) Until no open wiki work remains

This is the second half of the user procedure:
  full gather → wait 1h → recheck wiki problems → try again
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from psychofilm_analyzer.gather_v2.adaptive_rpm import AdaptiveRpmController
from psychofilm_analyzer.gather_v2.executor import SitePipeline
from psychofilm_analyzer.gather_v2.models import (
    OPEN_WORK,
    STATUS_DEFERRED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RETRY,
    STATUS_RUNNING,
    STATUS_SKIPPED,
    STATUS_SUCCESS,
)
from psychofilm_analyzer.gather_v2.plan_store import PlanStore
from psychofilm_analyzer.gather_v2.progress import ProgressReporter
from psychofilm_analyzer.gather_v2.wiki_report import write_wikipedia_pipeline_report

logger = logging.getLogger(__name__)


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + "Z"


def reopen_wikipedia_problems(
    store: PlanStore,
    *,
    include_skipped: bool = True,
    log_path: Optional[Path] = None,
) -> list[str]:
    """
    Reset non-success Wikipedia rows to pending so the loop retries them.

    Returns list of reopened request_ids.
    """
    reopened: list[str] = []
    for req in store.by_site("wikipedia"):
        if req.status == STATUS_SUCCESS:
            continue
        if req.status == STATUS_SKIPPED and not include_skipped:
            continue
        if req.status not in (
            STATUS_DEFERRED,
            STATUS_FAILED,
            STATUS_RETRY,
            STATUS_PENDING,
            STATUS_RUNNING,
            STATUS_SKIPPED,
        ):
            continue
        prev = req.status
        prev_cls = req.deferred_reason or ""
        store.update_fields(
            req.request_id,
            status=STATUS_PENDING,
            deferred="",
            deferred_reason="",
            error=f"reopened for post-cool recheck (was {prev}/{prev_cls}) at {_utc()}",
            # keep reproducible_command / result_path as audit trail until overwritten
            started_at="",
            finished_at="",
            http_status=None,
        )
        reopened.append(req.request_id)

    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            f"WIKI RECHECK reopen @ {_utc()}",
            f"reopened={len(reopened)}",
            *[f"  {rid}" for rid in reopened[:500]],
            "" if len(reopened) <= 500 else f"  ... +{len(reopened) - 500} more",
            "",
        ]
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines))

    logger.info("Wikipedia recheck: reopened %s problem requests", len(reopened))
    return reopened


def run_wikipedia_loop(
    store: PlanStore,
    *,
    config: dict[str, Any],
    reports_dir: str | Path,
    progress_path: str | Path | None = None,
    delay_sec: Optional[float] = None,
) -> dict[str, Any]:
    """
    Single-site loop: for each pending/deferred wiki request,
    pause (throttle / adaptive), then launch next command, until done.
    """
    a2 = config.get("gather_v2") or {}
    delays = dict(a2.get("site_delays_sec") or {})
    d = float(delay_sec if delay_sec is not None else delays.get("wikipedia", 15.0))
    timeout = float((config.get("http") or {}).get("timeout_sec", 20))
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    progress_path = Path(progress_path or (store.plan_dir / "pipeline_progress_wiki_recheck.txt"))

    wiki_adapt = dict(a2.get("wikipedia_adaptive_rpm") or {})
    wiki_adapt.setdefault("cool_base_sec", 30.0)
    wiki_adapt.setdefault("cool_step_sec", 15.0)
    wiki_adapt.setdefault("decrease_pct", 0.20)
    wiki_adapt.setdefault("increase_pct", 0.0)
    wiki_adapt.setdefault("step_rpm", 10.0)
    wiki_adapt.setdefault("success_batch", 8)
    wiki_adapt.setdefault("min_rpm", 5.0)
    wiki_adapt.setdefault("max_rpm", 200.0)
    if "initial_rpm" not in wiki_adapt:
        wiki_adapt["initial_rpm"] = max(5.0, min(40.0, 60.0 / max(d, 0.3)))

    stop = threading.Event()
    progress = ProgressReporter(
        progress_path,
        store,
        site_delays={"wikipedia": d},
        interval_sec=3.0,
        reports_dir=reports_dir,
        catalog_total=0,
        approach1_done=0,
        approach2_film_total=0,
        inherit_from_a1=True,
    )
    progress.start()

    # Clean wiki command dumps for this recheck session only
    from psychofilm_analyzer.gather_v2.executor import reset_session_command_logs

    reset_session_command_logs(reports_dir, sites=["wikipedia"])

    adaptive = AdaptiveRpmController(
        "wikipedia",
        initial_rpm=float(wiki_adapt["initial_rpm"]),
        min_rpm=float(wiki_adapt["min_rpm"]),
        max_rpm=float(wiki_adapt["max_rpm"]),
        step_rpm=float(wiki_adapt.get("step_rpm", 10.0)),
        cool_base_sec=float(wiki_adapt["cool_base_sec"]),
        cool_step_sec=float(wiki_adapt["cool_step_sec"]),
        decrease_pct=float(wiki_adapt["decrease_pct"]),
        increase_pct=float(wiki_adapt["increase_pct"]),
        success_batch=int(wiki_adapt["success_batch"]),
        log_path=reports_dir / "adaptive_rpm_wikipedia_recheck.txt",
        live_rpm_path=reports_dir / "CURRENT_RPM_wikipedia.txt",
    )
    progress.set_adaptive_snapshot("wikipedia", adaptive.snapshot())

    pipe = SitePipeline(
        "wikipedia",
        store,
        delay_sec=d,
        timeout_sec=timeout,
        progress=progress,
        stop_event=stop,
        excel_every=int(a2.get("excel_every", 40)),
        adaptive=adaptive,
        rate_limit_max_attempts=int(wiki_adapt.get("rate_limit_max_attempts", 50)),
        error_commands_path=None,
    )

    print(f"Wikipedia recheck loop starting @ {_utc()}")
    print(f"  delay={d}s  adaptive_rpm={wiki_adapt['initial_rpm']:.2f}")
    print(f"  open wiki work: {sum(store.counts_by_status('wikipedia').get(s, 0) for s in OPEN_WORK)}")
    print(f"  full report → {reports_dir / 'WIKI_REPORT.txt'}")

    pipe.start()
    try:
        pipe.join()
    except KeyboardInterrupt:
        stop.set()
        pipe.join(timeout=10)
    finally:
        progress.set_adaptive_snapshot("wikipedia", adaptive.snapshot())
        progress.stop()
        write_wikipedia_pipeline_report(
            store,
            reports_dir / "WIKI_REPORT.txt",
            adaptive=adaptive.snapshot(),
            final=True,
        )
        try:
            store.write_excel_and_csv()
        except Exception as exc:  # noqa: BLE001
            logger.warning("excel write after wiki recheck: %s", exc)

    counts = store.counts_by_status("wikipedia")
    summary = {
        "phase": "wiki_recheck",
        "finished_at": _utc(),
        "wikipedia_status": counts,
        "success": counts.get(STATUS_SUCCESS, 0),
        "pending": counts.get(STATUS_PENDING, 0),
        "deferred": counts.get(STATUS_DEFERRED, 0),
        "skipped": counts.get(STATUS_SKIPPED, 0),
        "failed": counts.get(STATUS_FAILED, 0),
    }
    (reports_dir / "wiki_recheck_summary.json").write_text(
        __import__("json").dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Wikipedia recheck done: {summary}")
    return summary


def wait_and_recheck_wikipedia(
    store: PlanStore,
    *,
    config: dict[str, Any],
    reports_dir: str | Path,
    wait_sec: float = 3600.0,
    include_skipped: bool = True,
) -> dict[str, Any]:
    """
    Sleep wait_sec (default 1 hour), reopen wiki problems, run wiki loop again.
    """
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    # Recheck notes appended to the single WIKI_REPORT.txt
    log_path = reports_dir / "WIKI_REPORT.txt"

    msg = (
        f"\n{'='*72}\n"
        f"WIKI COOL-DOWN WAIT starting {_utc()}  duration={wait_sec:.0f}s "
        f"({wait_sec/3600.0:.2f} h)\n"
        f"{'='*72}\n"
    )
    print(msg, end="")
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(msg)

    # Interruptible sleep in chunks so process is visible
    end = time.monotonic() + max(0.0, float(wait_sec))
    while time.monotonic() < end:
        left = end - time.monotonic()
        chunk = min(60.0, left)
        time.sleep(chunk)
        if left > 60:
            print(f"  wiki cool-down: {left/60.0:.1f} min remaining...")

    print(f"Cool-down finished @ {_utc()} — reopening wiki problems")
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(f"cool-down finished {_utc()}\n")

    # Reload plan from disk (in case of external updates)
    store.load()
    reopened = reopen_wikipedia_problems(
        store,
        include_skipped=include_skipped,
        log_path=log_path,
    )
    print(f"  reopened {len(reopened)} wikipedia requests")

    if not reopened and sum(store.counts_by_status("wikipedia").get(s, 0) for s in OPEN_WORK) == 0:
        print("  nothing to recheck — all wiki already success or empty")
        return {"reopened": 0, "skipped_run": True}

    result = run_wikipedia_loop(
        store,
        config=config,
        reports_dir=reports_dir,
    )
    result["reopened"] = len(reopened)
    result["wait_sec"] = wait_sec
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(f"recheck finished {_utc()} summary={result}\n")
    return result

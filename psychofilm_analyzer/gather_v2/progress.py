"""Unified + per-pipeline progress reports for Approach 2/3.

Performance model (backyard snapshot):
  - SAMPLE thread (default ~2s): cheap in-memory snapshot of O(1) status
    counts, adaptive RPM, completion RPM — never writes large files.
  - WRITE thread (default 30s): dumps the latest snapshot to UNIFIED_REPORT
    + pipeline_*_live.txt. Workers are not involved.
  - DETAIL thread (default 120s): heavy pipeline_*.txt + WIKI_REPORT only.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any, Optional

from psychofilm_analyzer.utils.localtime import now_str
from psychofilm_analyzer.gather_v2.models import (
    STATUS_DEFERRED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RETRY,
    STATUS_RUNNING,
    STATUS_SKIPPED,
    STATUS_SUCCESS,
    PipelineStats,
)
from psychofilm_analyzer.gather_v2.plan_store import PlanStore

logger = logging.getLogger(__name__)


def _now() -> str:
    """Local system time for reports."""
    return now_str()


def _pct(done: int, total: int) -> float:
    if total <= 0:
        return 100.0
    return round(100.0 * done / total, 2)


def _bar(pct: float, width: int = 28) -> str:
    filled = int(round(width * min(max(pct, 0.0), 100.0) / 100.0))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def _fmt_eta(sec: Optional[float]) -> str:
    if sec is None:
        return "n/a"
    if sec < 60:
        return f"{sec:.0f}s"
    if sec < 3600:
        return f"{sec / 60.0:.1f} min"
    return f"{sec / 3600.0:.2f} h"


class ProgressReporter:
    """
    Backyard snapshot model:
      - sample thread gathers cheap counters into memory
      - write thread dumps UNIFIED every interval_sec (default 30s)
      - detail thread builds heavy reports on a slower cadence
    """

    def __init__(
        self,
        store: PlanStore,
        *,
        site_delays: Optional[dict[str, float]] = None,
        interval_sec: float = 30.0,
        sample_interval_sec: float = 2.0,
        detail_interval_sec: float = 120.0,
        film_progress_interval_sec: float = 60.0,
        reports_dir: Optional[str | Path] = None,
        # Overall catalog progress (Approach 1 + 2)
        catalog_total: int = 0,
        approach1_done: int = 0,
        approach2_film_total: int = 0,
        inherit_from_a1: bool = True,
        smart_progress: bool = False,
        approach: int = 2,
        parallel_reporting: bool = True,
    ):
        self.store = store
        self.site_delays = site_delays or {}
        # Disk write cadence for UNIFIED / live status (from backyard snapshot)
        self.interval_sec = max(5.0, float(interval_sec))
        # How often the backyard sampler refreshes the in-memory snapshot
        self.sample_interval_sec = max(0.5, min(float(sample_interval_sec), self.interval_sec))
        # DETAIL is expensive on huge plans — default slower than UNIFIED
        self.detail_interval_sec = max(self.interval_sec, float(detail_interval_sec))
        self.film_progress_interval_sec = max(15.0, float(film_progress_interval_sec))
        self.reports_dir = Path(reports_dir or (store.plan_dir / "reports"))
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.unified_path = self.reports_dir / "UNIFIED_REPORT.txt"
        self.catalog_total = int(catalog_total or 0)
        self.approach1_done = int(approach1_done or 0)
        self.approach2_film_total = int(approach2_film_total or 0)
        self.inherit_from_a1 = inherit_from_a1
        self.smart_progress = bool(smart_progress)
        self.approach = int(approach or 2)
        self.parallel_reporting = bool(parallel_reporting)
        self._stop = threading.Event()
        self._sample_thread: Optional[threading.Thread] = None
        self._write_thread: Optional[threading.Thread] = None
        self._fast_thread: Optional[threading.Thread] = None  # alias of write
        self._detail_thread: Optional[threading.Thread] = None
        self._thread: Optional[threading.Thread] = None  # legacy alias
        self._completions: dict[str, deque[float]] = defaultdict(deque)
        self._last_activity: dict[str, str] = {}
        self._last_request_id: dict[str, str] = {}
        self._lock = threading.Lock()
        self._started = time.monotonic()
        self._session_start = _now()
        self._adaptive: dict[str, dict[str, Any]] = {}
        self._last_detail_mono = 0.0
        self._last_film_prog_mono = 0.0
        self._cached_film_prog: Optional[dict[str, Any]] = None
        self._last_write_ms: float = 0.0
        self._last_detail_ms: float = 0.0
        self._last_sample_ms: float = 0.0
        self._last_error: str = ""
        self._detail_tick_n: int = 0
        self._detail_busy = threading.Event()  # set while DETAIL running
        self._snapshot: Optional[dict[str, Any]] = None
        self._snapshot_mono: float = 0.0
        self._write_bootstrap()

    def set_adaptive_snapshot(self, site: str, snap: dict[str, Any]) -> None:
        with self._lock:
            self._adaptive[site] = dict(snap)

    def _write_bootstrap(self) -> None:
        note = (
            "PsychoFilm Approach 2/3 — reports\n"
            f"session_started: {self._session_start}\n"
            f"unified: {self.unified_path}  (WRITE every {self.interval_sec:.0f}s "
            f"from backyard snapshot sampled every {self.sample_interval_sec:.0f}s)\n"
            f"detail:  {self.reports_dir}/pipeline_*.txt + WIKI_REPORT.txt  "
            f"(every {self.detail_interval_sec:.0f}s, separate thread)\n"
            f"plan: {self.store.jsonl_path}\n"
            f"excel: {self.store.excel_path}\n"
            f"inherit_approach1: {self.inherit_from_a1}\n"
            f"catalog_total: {self.catalog_total}\n"
            f"approach1_done: {self.approach1_done}\n"
            f"approach2_films_planned: {self.approach2_film_total}\n"
            "=" * 72
            + "\n"
        )
        self.unified_path.write_text(note, encoding="utf-8")

    def note_completion(self, site: str, request_id: str = "") -> None:
        with self._lock:
            now = time.monotonic()
            q = self._completions[site]
            q.append(now)
            while q and now - q[0] > 60.0:
                q.popleft()
            self._last_activity[site] = _now()
            if request_id:
                self._last_request_id[site] = request_id

    def start(self) -> None:
        if self.parallel_reporting:
            self._sample_thread = threading.Thread(
                target=self._sample_loop, name="a2-progress-sample", daemon=True
            )
            self._write_thread = threading.Thread(
                target=self._write_loop, name="a2-progress-write", daemon=True
            )
            self._detail_thread = threading.Thread(
                target=self._detail_loop, name="a2-progress-detail", daemon=True
            )
            self._sample_thread.start()
            self._write_thread.start()
            self._detail_thread.start()
            self._fast_thread = self._write_thread
            self._thread = self._write_thread
            # Prime one snapshot + one light write so files exist immediately
            try:
                self._refresh_snapshot()
                self.write_once(final=False, force_detail=False, from_snapshot=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning("progress bootstrap write failed: %s", exc)
        else:
            self._thread = threading.Thread(target=self._loop, name="a2-progress", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        for t in (
            self._sample_thread,
            self._write_thread,
            self._fast_thread,
            self._detail_thread,
            self._thread,
        ):
            if t and t.is_alive():
                t.join(timeout=15.0)
        try:
            self._refresh_snapshot(force_film=True)
            self.write_once(final=True, force_detail=False, from_snapshot=True)
        except Exception as exc:  # noqa: BLE001
            logger.exception("final progress write failed: %s", exc)

    def _sample_loop(self) -> None:
        """Backyard: refresh in-memory snapshot only (no heavy disk reports)."""
        while not self._stop.wait(self.sample_interval_sec):
            try:
                self._refresh_snapshot()
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"sample:{type(exc).__name__}: {exc}"
                logger.exception("progress SAMPLE failed: %s", exc)

    def _write_loop(self) -> None:
        """Disk write of UNIFIED + live status from latest backyard snapshot."""
        while not self._stop.wait(self.interval_sec):
            try:
                # Ensure snapshot is not ancient if sampler stalled
                if (
                    self._snapshot is None
                    or (time.monotonic() - self._snapshot_mono) > self.interval_sec
                ):
                    self._refresh_snapshot()
                self.write_once(final=False, force_detail=False, from_snapshot=True)
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"write:{type(exc).__name__}: {exc}"
                logger.exception("progress WRITE failed: %s", exc)

    def _detail_loop(self) -> None:
        """Heavy pipeline_*.txt / WIKI_REPORT — separate thread, skip if still busy."""
        while not self._stop.wait(self.detail_interval_sec):
            if self._detail_busy.is_set():
                logger.warning("progress DETAIL still busy — skip this cycle")
                continue
            self._detail_busy.set()
            try:
                self._refresh_snapshot(force_film=True)
                self.write_once(final=False, force_detail=True, from_snapshot=True)
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"detail:{type(exc).__name__}: {exc}"
                logger.exception("progress DETAIL write failed: %s", exc)
            finally:
                self._detail_busy.clear()

    def _loop(self) -> None:
        """Legacy single-thread loop (parallel_reporting=False)."""
        while not self._stop.wait(self.interval_sec):
            try:
                self._refresh_snapshot()
                self.write_once(final=False, force_detail=False, from_snapshot=True)
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("progress write_once failed: %s", exc)

    def _refresh_snapshot(self, *, force_film: bool = False) -> dict[str, Any]:
        """Gather cheap counters into memory (backyard). O(sites), not O(plan)."""
        t0 = time.perf_counter()
        by_site_counts = self.store.counts_by_site_and_status()
        with self._lock:
            adaptive = {k: dict(v) for k, v in self._adaptive.items()}
            completions = {
                site: float(len(q)) for site, q in self._completions.items()
            }
            last_activity = dict(self._last_activity)
            last_request_id = dict(self._last_request_id)
        # Film-level scan is rare and cached
        cat_p = self._overall_gather_progress(force=force_film)
        req_p = self._request_progress(by_site_counts)
        # Attach observed RPM from completion deques (last 60s)
        site_stats: dict[str, PipelineStats] = {}
        for site, counts in by_site_counts.items():
            st = self._stats_from_counts(site, counts)
            site_stats[site] = st
        snap = {
            "mono": time.monotonic(),
            "sampled_at": _now(),
            "by_site_counts": by_site_counts,
            "adaptive": adaptive,
            "completions_rpm": completions,
            "last_activity": last_activity,
            "last_request_id": last_request_id,
            "cat_p": cat_p,
            "req_p": req_p,
            "site_stats": site_stats,
            "wall": time.monotonic() - self._started,
        }
        with self._lock:
            self._snapshot = snap
            self._snapshot_mono = snap["mono"]
            self._last_sample_ms = (time.perf_counter() - t0) * 1000.0
        return snap

    def _get_snapshot(self) -> dict[str, Any]:
        with self._lock:
            if self._snapshot is not None:
                return dict(self._snapshot)
        return self._refresh_snapshot()

    def _stats_from_counts(self, site: str, counts: dict[str, int]) -> PipelineStats:
        total = sum(counts.values())
        with self._lock:
            rpm = float(len(self._completions.get(site, ())))
            last = self._last_activity.get(site, "")
            last_id = self._last_request_id.get(site, "")
        pending = (
            counts.get(STATUS_PENDING, 0)
            + counts.get(STATUS_RETRY, 0)
            + counts.get(STATUS_DEFERRED, 0)
        )
        done = (
            counts.get(STATUS_SUCCESS, 0)
            + counts.get(STATUS_FAILED, 0)
            + counts.get(STATUS_SKIPPED, 0)
        )
        eta = (pending / rpm) * 60.0 if rpm > 0 and pending > 0 else None
        return PipelineStats(
            site=site,
            pending=counts.get(STATUS_PENDING, 0),
            running=counts.get(STATUS_RUNNING, 0),
            success=counts.get(STATUS_SUCCESS, 0),
            failed=counts.get(STATUS_FAILED, 0),
            skipped=counts.get(STATUS_SKIPPED, 0),
            retry=counts.get(STATUS_RETRY, 0),
            completed=done,
            total=total,
            rpm=round(rpm, 2),
            eta_sec=round(eta, 1) if eta is not None else None,
            last_activity=last,
            last_request_id=last_id,
            delay_sec=float(self.site_delays.get(site, 0.0)),
        )

    def _stats_for_site(self, site: str, *, light: bool = True) -> PipelineStats:
        counts = self.store.counts_by_status(site)
        st = self._stats_from_counts(site, counts)
        if not light and (not st.last_activity or not st.last_request_id):
            last, last_id = st.last_activity, st.last_request_id
            for req in reversed(self.store.by_site(site)):
                if req.finished_at or req.started_at:
                    last = req.finished_at or req.started_at or last
                    last_id = req.request_id or last_id
                    break
            st.last_activity = last
            st.last_request_id = last_id
        return st

    def _overall_gather_progress(self, *, force: bool = False) -> dict[str, Any]:
        """Catalog-level progress; cached so 50k scans do not block every 3s tick."""
        now = time.monotonic()
        if (
            not force
            and self._cached_film_prog is not None
            and (now - self._last_film_prog_mono) < self.film_progress_interval_sec
        ):
            return dict(self._cached_film_prog)

        by_film: dict[int, set[str]] = defaultdict(set)
        for req in self.store.all():
            by_film[int(req.film_index)].add(req.status)

        terminal = {STATUS_SUCCESS, STATUS_FAILED, STATUS_SKIPPED}
        a2_films_done = 0
        a2_films_partial = 0
        for statuses in by_film.values():
            if statuses and statuses <= terminal:
                a2_films_done += 1
            elif statuses:
                a2_films_partial += 1

        a2_total = self.approach2_film_total or len(by_film)
        catalog_done = self.approach1_done + a2_films_done
        catalog_total = self.catalog_total or (self.approach1_done + a2_total)
        remaining = max(0, catalog_total - catalog_done)
        out = {
            "catalog_total": catalog_total,
            "approach1_done": self.approach1_done,
            "approach2_film_total": a2_total,
            "approach2_films_done": a2_films_done,
            "approach2_films_partial": a2_films_partial,
            "approach2_films_pending": max(0, a2_total - a2_films_done - a2_films_partial),
            "catalog_done": catalog_done,
            "catalog_remaining": remaining,
            "catalog_pct": _pct(catalog_done, catalog_total),
            "approach2_pct": _pct(a2_films_done, a2_total),
        }
        self._cached_film_prog = dict(out)
        self._last_film_prog_mono = now
        return out

    def _request_progress(
        self, by_site: Optional[dict[str, dict[str, int]]] = None
    ) -> dict[str, Any]:
        if by_site is None:
            by_site = self.store.counts_by_site_and_status()
        all_c: dict[str, int] = {}
        for counts in by_site.values():
            for st, n in counts.items():
                all_c[st] = all_c.get(st, 0) + n
        total = sum(all_c.values())
        done = (
            all_c.get(STATUS_SUCCESS, 0)
            + all_c.get(STATUS_FAILED, 0)
            + all_c.get(STATUS_SKIPPED, 0)
        )
        pending = (
            all_c.get(STATUS_PENDING, 0)
            + all_c.get(STATUS_RETRY, 0)
            + all_c.get(STATUS_DEFERRED, 0)
        )
        with self._lock:
            rpm_all = float(sum(len(q) for q in self._completions.values()))

        # Approach 3 smart progress (FAST-safe, O(status counts) — no full plan scan)
        # success + skipped/failed = settled; deferred+retry ≈ recoverable open;
        # pending+running = other open.
        smart_pct = None
        accomplishable = total
        terminal_n = 0
        recoverable_open = 0
        if self.smart_progress:
            success_n = all_c.get(STATUS_SUCCESS, 0)
            terminal_n = all_c.get(STATUS_FAILED, 0) + all_c.get(STATUS_SKIPPED, 0)
            recoverable_open = all_c.get(STATUS_DEFERRED, 0) + all_c.get(STATUS_RETRY, 0)
            other_open = all_c.get(STATUS_PENDING, 0) + all_c.get(STATUS_RUNNING, 0)
            settled = success_n + terminal_n
            still = recoverable_open + other_open
            accomplishable = max(1, settled + still)
            smart_pct = _pct(settled, accomplishable)
            pending = still
            done = settled

        eta = (pending / rpm_all) * 60.0 if rpm_all > 0 and pending > 0 else None
        return {
            "total": total,
            "done": done,
            "pending": pending,
            "running": all_c.get(STATUS_RUNNING, 0),
            "success": all_c.get(STATUS_SUCCESS, 0),
            "failed": all_c.get(STATUS_FAILED, 0),
            "skipped": all_c.get(STATUS_SKIPPED, 0),
            "retry": all_c.get(STATUS_RETRY, 0),
            "deferred": all_c.get(STATUS_DEFERRED, 0),
            "pct": smart_pct if smart_pct is not None else _pct(done, total),
            "pct_mode": (
                "smart_accomplishable_counts" if self.smart_progress else "raw"
            ),
            "terminal_settled": terminal_n,
            "recoverable_open": recoverable_open,
            "accomplishable": accomplishable,
            "rpm": round(rpm_all, 2),
            "eta_sec": round(eta, 1) if eta is not None else None,
            "counts": all_c,
        }

    def _command_for_request(self, r) -> str:
        """Always produce a pasteable python -c command for this plan row."""
        cmd = (getattr(r, "reproducible_command", None) or "").strip()
        if cmd:
            return cmd
        err = r.error or ""
        if "REPRO:" in err:
            return err.split("REPRO:", 1)[-1].strip()
        # Build from url/headers/params stored on the plan row
        try:
            import json

            from psychofilm_analyzer.gather_v2.executor import build_reproducible_command

            params = {}
            headers = {}
            try:
                params = json.loads(r.params_json or "{}") or {}
            except Exception:
                params = {}
            try:
                headers = json.loads(r.headers_json or "{}") or {}
            except Exception:
                headers = {}
            if not headers and r.site == "wikipedia":
                headers = {
                    "User-Agent": "PsychoFilmAnalyzer/1.0 (+research; educational; contact: local)",
                    "Accept": "application/json, text/html, */*",
                }
            if not r.url:
                return ""
            return build_reproducible_command(
                method=r.method or "GET",
                url=r.url,
                params=params,
                headers=headers,
                timeout=20.0,
            )
        except Exception:
            return ""

    def _site_errors(self, site: str, limit: int = 40) -> list[str]:
        """Problem rows with 1:1 CAPTURED_HTTP ↔ THIS_COMMAND binding."""
        lines = []
        failed = [
            r
            for r in self.store.by_site(site)
            if r.status
            in (STATUS_FAILED, STATUS_SKIPPED, STATUS_RETRY, STATUS_DEFERRED)
            and (r.error or r.http_status or r.site == "wikipedia" or r.deferred == "yes")
        ]
        failed = list(reversed(failed))[:limit]
        for r in failed:
            err_short = (r.error or "").split(" | REPRO:")[0][:160]
            def_mark = (
                " DEFERRED_TO_END"
                if (r.deferred == "yes" or r.status == STATUS_DEFERRED)
                else ""
            )
            # Prefer structured attribution from response JSON (wiki multi-step)
            attr: dict = {}
            try:
                data = self.store.load_response(r.request_id)
                if isinstance(data, dict):
                    attr = dict(data.get("attribution") or {})
            except Exception:
                attr = {}
            cls = (
                attr.get("classification")
                or r.deferred_reason
                or "-"
            )
            cap_http = attr.get("http_status", r.http_status)
            cap_at = attr.get("captured_at") or r.finished_at or "-"
            lines.append(
                f"  [{r.status}{def_mark}] {r.request_id} film_idx={r.film_index} "
                f"title={r.film_title!r} ({r.year}) excel_row={r.excel_row} "
                f"endpoint={r.endpoint_type}"
            )
            lines.append(
                f"    CLASSIFICATION={cls}  CAPTURED_HTTP={cap_http}  "
                f"CAPTURED_AT={cap_at}"
            )
            lines.append(f"    NOTE: {err_short}")
            lines.append(f"    url: {attr.get('url') or r.url or ''}")
            cmd = (attr.get("reproducible_command") or "").strip() or self._command_for_request(r)
            if cmd:
                lines.append(
                    "    BINDING: CAPTURED_HTTP is bound ONLY to THIS_COMMAND at CAPTURED_AT"
                )
                lines.append("    THIS_COMMAND_ONLY (paste into PowerShell):")
                lines.append(f"    {cmd}")
            else:
                lines.append("    THIS_COMMAND_ONLY: (unavailable)")
        return lines

    def _endpoint_breakdown(self, site: str) -> list[str]:
        rows = self.store.by_site(site)
        by_ep: dict[str, Counter] = defaultdict(Counter)
        for r in rows:
            by_ep[r.endpoint_type or "?"][r.status] += 1
        lines = []
        for ep, ctr in sorted(by_ep.items()):
            parts = " ".join(f"{k}={v}" for k, v in sorted(ctr.items()))
            lines.append(f"  {ep}: {parts}")
        return lines

    def _write_pipeline_report(self, site: str, st: PipelineStats, *, final: bool) -> None:
        """DETAIL write: full report for every site (wiki + tmdb/omdb/kp/lb)."""
        with self._lock:
            adapt = dict(self._adaptive.get(site) or {})

        # Wikipedia: ONE all-in-one text report (WIKI_REPORT.txt + pipeline_wikipedia mirror)
        if site == "wikipedia":
            from psychofilm_analyzer.gather_v2.wiki_report import (
                WIKI_REPORT_NAME,
                write_wikipedia_pipeline_report,
            )

            write_wikipedia_pipeline_report(
                self.store,
                self.reports_dir / WIKI_REPORT_NAME,
                adaptive=adapt,
                final=final,
                reports_dir=self.reports_dir,
            )
            return

        from psychofilm_analyzer.gather_v2.site_report import write_site_pipeline_report

        write_site_pipeline_report(
            self.store,
            site,
            self.reports_dir / f"pipeline_{site}.txt",
            delay_sec=float(self.site_delays.get(site, st.delay_sec or 0.0)),
            final=final,
        )

    def _write_fast_site_status(self, site: str, st: PipelineStats) -> None:
        """FAST write: small live status for every site so files are not wiki-only."""
        from psychofilm_analyzer.gather_v2.site_report import write_light_site_status

        write_light_site_status(
            self.store,
            site,
            self.reports_dir / f"pipeline_{site}_live.txt",
            delay_sec=float(self.site_delays.get(site, st.delay_sec or 0.0)),
            last_activity=st.last_activity or "",
            last_id=st.last_request_id or "",
            rpm=float(st.rpm or 0.0),
        )

    def write_once(
        self,
        *,
        final: bool = False,
        force_detail: bool = False,
        from_snapshot: bool = True,
    ) -> None:
        """
        WRITE path (default every 30s): UNIFIED from backyard snapshot.
        DETAIL path (default every 120s or final): heavy per-site + WIKI_REPORT.
        """
        t0 = time.perf_counter()
        now = time.monotonic()
        if final or force_detail:
            do_detail = True
        elif getattr(self, "parallel_reporting", False):
            do_detail = False
        else:
            do_detail = (now - self._last_detail_mono) >= self.detail_interval_sec

        if from_snapshot:
            snap = self._get_snapshot()
            by_site_counts = snap.get("by_site_counts") or {}
            cat_p = snap.get("cat_p") or {}
            req_p = snap.get("req_p") or self._request_progress(by_site_counts)
            wall = float(snap.get("wall") or (now - self._started))
            sampled_at = snap.get("sampled_at") or _now()
            sample_ms = self._last_sample_ms
        else:
            by_site_counts = self.store.counts_by_site_and_status()
            req_p = self._request_progress(by_site_counts)
            cat_p = self._overall_gather_progress(force=final or do_detail)
            wall = now - self._started
            sampled_at = _now()
            sample_ms = 0.0

        sites = list(by_site_counts.keys()) or self.store.sites()

        lines: list[str] = [
            "PsychoFilm UNIFIED REPORT — Approach 2 gather",
            f"updated: {_now()}  wall={wall:.0f}s  {'FINAL' if final else 'live'}",
            f"session_started: {self._session_start}",
            f"refresh: WRITE every {self.interval_sec:.0f}s from backyard snapshot "
            f"(sample every {self.sample_interval_sec:.0f}s)  |  "
            f"DETAIL every {self.detail_interval_sec:.0f}s  "
            f"(this write: {'DETAIL' if do_detail else 'SNAPSHOT'})",
            f"snapshot_at: {sampled_at}  sample_ms={sample_ms:.0f}",
            f"last_write_ms={self._last_write_ms:.0f}  "
            f"last_detail_ms={self._last_detail_ms:.0f}"
            + (f"  last_error={self._last_error}" if self._last_error else ""),
            f"plan_jsonl: {self.store.jsonl_path}",
            f"plan_excel: {self.store.excel_path}",
            f"reports_dir: {self.reports_dir}",
            "",
            "=" * 72,
            "ENTIRE GATHERING PROCESS (Approach 1 + Approach 2)",
            "=" * 72,
            f"  inherit_from_approach1: {self.inherit_from_a1}",
            f"  catalog_total_films:    {cat_p['catalog_total']}",
            f"  approach1_done:         {cat_p['approach1_done']}  (inherited, not re-fetched)",
            f"  approach2_films_total:  {cat_p['approach2_film_total']}",
            f"  approach2_films_done:   {cat_p['approach2_films_done']}",
            f"  approach2_partial:      {cat_p['approach2_films_partial']}",
            f"  approach2_pending:      {cat_p['approach2_films_pending']}",
            f"  catalog_done:           {cat_p['catalog_done']}",
            f"  catalog_remaining:      {cat_p['catalog_remaining']}",
            f"  CATALOG PROGRESS: {_bar(cat_p['catalog_pct'])} {cat_p['catalog_pct']}%",
            f"  APPROACH2 PROGRESS: {_bar(cat_p['approach2_pct'])} {cat_p['approach2_pct']}%",
            "",
            "=" * 72,
            "REQUEST-LEVEL PROGRESS (Approach 2 HTTP plan)",
            "=" * 72,
            f"  {_bar(req_p['pct'])} {req_p['pct']}%",
            f"  done={req_p['done']}/{req_p['total']}  "
            f"pending={req_p['pending']} running={req_p['running']}",
            f"  success={req_p['success']} failed={req_p['failed']} "
            f"skipped={req_p['skipped']} retry={req_p['retry']} "
            f"deferred={req_p.get('deferred', 0)}",
            f"  observed_finish_rpm={req_p['rpm']} req/min  "
            f"ETA={_fmt_eta(req_p['eta_sec'])}",
            f"  progress_mode: {req_p.get('pct_mode', 'raw')}  approach={self.approach}",
        ]
        if self.smart_progress:
            lines += [
                f"  terminal_settled (impossible, not retried): {req_p.get('terminal_settled', 0)}",
                f"  recoverable_open (end-queue only): {req_p.get('recoverable_open', 0)}",
                f"  accomplishable base: {req_p.get('accomplishable', req_p['total'])}",
            ]
        lines += [
            "",
            "=" * 72,
            "PER-PIPELINE SUMMARY",
            "=" * 72,
            "",
        ]

        for site in sites:
            st = self._stats_from_counts(site, by_site_counts.get(site) or {})
            pct = _pct(st.completed, st.total)
            with self._lock:
                adapt = dict(self._adaptive.get(site) or {})
            delay = float(self.site_delays.get(site, st.delay_sec or 0.0))
            lines += [
                f"[{site.upper()}]",
                f"  {_bar(pct)} {pct}%  completed={st.completed}/{st.total}",
                f"  pending={st.pending} running={st.running} success={st.success} "
                f"failed={st.failed} skipped={st.skipped} retry={st.retry}",
                f"  delay={delay}s  "
                f"observed_finish_rpm={st.rpm}  "
                f"ETA={_fmt_eta(st.eta_sec)}",
                f"  last={st.last_activity or '-'}  id={st.last_request_id or '-'}",
                f"  live_status:   {self.reports_dir / f'pipeline_{site}_live.txt'}",
                f"  detail_report: {self.reports_dir / f'pipeline_{site}.txt'}",
            ]
            if site == "wikipedia":
                lines.append(f"  wiki_report:   {self.reports_dir / 'WIKI_REPORT.txt'}")
            if adapt:
                lines += [
                    f"  adaptive_target_rpm={adapt.get('current_rpm', adapt.get('rpm'))}  "
                    f"STABLE_RPM={adapt.get('stable_rpm')}  "
                    f"PEAK_RPM={adapt.get('peak_rpm')}  "
                    f"DELAY_SEC={adapt.get('delay_sec')}  "
                    f"STEP={adapt.get('step_rpm')}  "
                    f"MAX={adapt.get('max_rpm')}",
                    f"  cool_next={adapt.get('next_cool_pause_sec')}s "
                    f"streak={adapt.get('success_streak')}/{adapt.get('success_batch')} "
                    f"ok={adapt.get('total_success')} 429={adapt.get('total_429')}",
                    "  NOTE: observed_finish_rpm = plan rows finished/min; "
                    "adaptive_target_rpm = spacing policy (not the same)",
                    f"  live_rpm: {self.reports_dir / f'CURRENT_RPM_{site}.txt'}",
                    f"  adaptive_log: {self.reports_dir / f'adaptive_rpm_{site}.txt'}",
                ]
            lines.append("")
            # FAST: refresh live status for EVERY site (not only wiki)
            if not do_detail:
                try:
                    st2 = PipelineStats(
                        site=st.site,
                        pending=st.pending,
                        running=st.running,
                        success=st.success,
                        failed=st.failed,
                        skipped=st.skipped,
                        retry=st.retry,
                        completed=st.completed,
                        total=st.total,
                        rpm=st.rpm,
                        eta_sec=st.eta_sec,
                        last_activity=st.last_activity,
                        last_request_id=st.last_request_id,
                        delay_sec=delay,
                    )
                    self._write_fast_site_status(site, st2)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("fast site status %s failed: %s", site, exc)

        # Heavy detail files on slower cadence only (never block FAST path)
        if do_detail:
            t_detail = time.perf_counter()
            self._detail_tick_n += 1
            for site in sites:
                st = self._stats_from_counts(site, by_site_counts.get(site) or {})
                try:
                    self._write_pipeline_report(site, st, final=final)
                    # also refresh live status on detail tick
                    delay = float(self.site_delays.get(site, st.delay_sec or 0.0))
                    st.delay_sec = delay
                    self._write_fast_site_status(site, st)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("pipeline report %s failed: %s", site, exc)
                    self._last_error = f"pipeline_{site}: {type(exc).__name__}: {exc}"
            # Plan Excel is VERY expensive on 100k+ rows — never every DETAIL.
            # Only on final, or every 10th DETAIL (~ every detail_interval*10).
            if final or (self._detail_tick_n % 10 == 0 and self._detail_tick_n > 0):
                try:
                    self.store.write_excel_and_csv()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("plan excel export on detail tick failed: %s", exc)
            self._last_detail_mono = time.monotonic()
            self._last_detail_ms = (time.perf_counter() - t_detail) * 1000.0
            if self._last_detail_ms > 5000:
                logger.warning(
                    "UNIFIED_REPORT DETAIL write slow: %.0f ms (workers keep running)",
                    self._last_detail_ms,
                )
        elif self._last_detail_mono <= 0:
            # Mark so subsequent FAST ticks stay FAST until detail interval
            self._last_detail_mono = time.monotonic()

        # Top errors: short on fast path, fuller on detail
        lines += [
            "=" * 72,
            "TOP ERRORS / PROBLEMS (all pipelines, recent)",
            "=" * 72,
        ]
        if do_detail:
            all_err: list[str] = []
            for site in sites:
                all_err.extend(self._site_errors(site, limit=10))
            if all_err:
                lines.extend(all_err[:40])
            else:
                lines.append("  (none)")
        else:
            lines.append(
                f"  (detail refresh in "
                f"{max(0.0, self.detail_interval_sec - (now - self._last_detail_mono)):.0f}s "
                f"— open pipeline_*.txt / WIKI_REPORT.txt for full commands)"
            )

        with self._lock:
            adapt_all = {k: dict(v) for k, v in self._adaptive.items()}
        if adapt_all:
            lines += [
                "",
                "=" * 72,
                "CURRENT RPM (all adaptive pipelines)",
                "=" * 72,
            ]
            for site, adapt in sorted(adapt_all.items()):
                lines.append(
                    f"  [{site}] CURRENT_RPM={adapt.get('current_rpm', adapt.get('rpm'))}  "
                    f"STABLE_RPM={adapt.get('stable_rpm')}  "
                    f"PEAK={adapt.get('peak_rpm')}  "
                    f"DELAY={adapt.get('delay_sec')}s  "
                    f"MAX={adapt.get('max_rpm')}  "
                    f"file={self.reports_dir / f'CURRENT_RPM_{site}.txt'}"
                )

        lines += [
            "",
            "=" * 72,
            "FILES (all pipelines — not wiki-only)",
            f"  unified:      {self.unified_path}  (WRITE every {self.interval_sec:.0f}s "
            f"from backyard snapshot; sample every {self.sample_interval_sec:.0f}s)",
            f"  live status:  {self.reports_dir}/pipeline_*_live.txt  (same WRITE cadence)",
            f"  full reports: {self.reports_dir}/pipeline_*.txt  (DETAIL every "
            f"{self.detail_interval_sec:.0f}s, separate thread)",
            f"  wiki_report:  {self.reports_dir / 'WIKI_REPORT.txt'}  (DETAIL)",
            f"  site errors:  {self.reports_dir}/{{tmdb,omdb,kinopoisk,letterboxd}}_ERROR_COMMANDS.txt",
            f"  current_rpm:  {self.reports_dir / 'CURRENT_RPM_wikipedia.txt'}  (live)",
            f"  plan export:  {self.store.excel_path}  (rare DETAIL only; not on hot path)",
            f"  profiles:     output/profile_a2_live.xlsx  (all sources merged)",
            "=" * 72,
            "",
        ]

        text = "\n".join(lines)
        self.unified_path.write_text(text, encoding="utf-8")
        self._last_write_ms = (time.perf_counter() - t0) * 1000.0
        if self._last_write_ms > 5000:
            logger.warning(
                "UNIFIED_REPORT write slow: %.0f ms (detail=%s)",
                self._last_write_ms,
                do_detail,
            )

"""Unified + per-pipeline progress reports for Approach 2."""

from __future__ import annotations

import threading
import time
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

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


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


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
    Writes:
      - unified report (all pipelines + overall gather progress)
      - one detailed report file per pipeline (errors, endpoints, samples)
    """

    def __init__(
        self,
        path: str | Path,
        store: PlanStore,
        *,
        site_delays: Optional[dict[str, float]] = None,
        interval_sec: float = 3.0,
        reports_dir: Optional[str | Path] = None,
        # Overall catalog progress (Approach 1 + 2)
        catalog_total: int = 0,
        approach1_done: int = 0,
        approach2_film_total: int = 0,
        inherit_from_a1: bool = True,
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.store = store
        self.site_delays = site_delays or {}
        self.interval_sec = interval_sec
        self.reports_dir = Path(reports_dir or (self.path.parent / "reports"))
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.unified_path = self.reports_dir / "UNIFIED_REPORT.txt"
        # also keep alias at progress path
        self.catalog_total = int(catalog_total or 0)
        self.approach1_done = int(approach1_done or 0)
        self.approach2_film_total = int(approach2_film_total or 0)
        self.inherit_from_a1 = inherit_from_a1
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._completions: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self._started = time.monotonic()
        self._session_start_utc = _utc()
        self._adaptive: dict[str, dict[str, Any]] = {}
        self._write_bootstrap()

    def set_adaptive_snapshot(self, site: str, snap: dict[str, Any]) -> None:
        with self._lock:
            self._adaptive[site] = dict(snap)

    def _write_bootstrap(self) -> None:
        note = (
            "PsychoFilm Approach 2 — reports\n"
            f"session_started: {self._session_start_utc}\n"
            f"unified: {self.unified_path}\n"
            f"per_pipeline: {self.reports_dir}/pipeline_<site>.txt\n"
            f"live_alias: {self.path}\n"
            f"plan: {self.store.jsonl_path}\n"
            f"excel: {self.store.excel_path}\n"
            f"inherit_approach1: {self.inherit_from_a1}\n"
            f"catalog_total: {self.catalog_total}\n"
            f"approach1_done: {self.approach1_done}\n"
            f"approach2_films_planned: {self.approach2_film_total}\n"
            "=" * 72
            + "\n"
        )
        self.path.write_text(note, encoding="utf-8")
        self.unified_path.write_text(note, encoding="utf-8")

    def note_completion(self, site: str) -> None:
        with self._lock:
            now = time.monotonic()
            q = self._completions[site]
            q.append(now)
            while q and now - q[0] > 60.0:
                q.popleft()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="a2-progress", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.write_once(final=True)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_sec):
            try:
                self.write_once(final=False)
            except Exception:
                pass

    def _stats_for_site(self, site: str) -> PipelineStats:
        counts = self.store.counts_by_status(site)
        total = sum(counts.values())
        with self._lock:
            rpm = float(len(self._completions.get(site, ())))
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
        eta = None
        if rpm > 0 and pending > 0:
            eta = (pending / rpm) * 60.0
        last = ""
        last_id = ""
        for req in reversed(self.store.by_site(site)):
            if req.finished_at or req.started_at:
                last = req.finished_at or req.started_at
                last_id = req.request_id
                break
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

    def _overall_gather_progress(self) -> dict[str, Any]:
        """Catalog-level progress: A1 done + A2 film completion estimate from requests."""
        # Estimate A2 films completed: film_index where all requests terminal
        by_film: dict[int, list] = defaultdict(list)
        for req in self.store.all():
            by_film[int(req.film_index)].append(req)
        a2_films_done = 0
        a2_films_partial = 0
        for fi, reqs in by_film.items():
            statuses = {r.status for r in reqs}
            if statuses <= {STATUS_SUCCESS, STATUS_FAILED, STATUS_SKIPPED}:
                a2_films_done += 1
            elif any(
                s in statuses
                for s in (
                    STATUS_SUCCESS,
                    STATUS_FAILED,
                    STATUS_SKIPPED,
                    STATUS_RUNNING,
                    STATUS_DEFERRED,
                    STATUS_RETRY,
                    STATUS_PENDING,
                )
            ):
                a2_films_partial += 1
        a2_total = self.approach2_film_total or len(by_film)
        catalog_done = self.approach1_done + a2_films_done
        catalog_total = self.catalog_total or (self.approach1_done + a2_total)
        remaining = max(0, catalog_total - catalog_done)
        return {
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

    def _request_progress(self) -> dict[str, Any]:
        all_c = self.store.counts_by_status()
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
            "pct": _pct(done, total),
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
        path = self.reports_dir / f"pipeline_{site}.txt"
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
                adaptive=adapt or None,
                final=final,
            )
            return

        pct = _pct(st.completed, st.total)
        err_lines = self._site_errors(site)
        ep_lines = self._endpoint_breakdown(site)
        site_counts = self.store.counts_by_status(site)
        # duration stats
        durs = [float(r.duration_ms) for r in self.store.by_site(site) if r.duration_ms]
        avg_ms = sum(durs) / len(durs) if durs else 0.0
        max_ms = max(durs) if durs else 0.0
        # http status histogram
        http_c: Counter = Counter()
        for r in self.store.by_site(site):
            if r.http_status is not None:
                http_c[str(r.http_status)] += 1
            elif r.error:
                http_c["ERR"] += 1

        body = [
            f"PIPELINE REPORT — {site.upper()}",
            f"updated: {_utc()}  {'FINAL' if final else 'live'}",
            f"session_started: {self._session_start_utc}",
            "",
            "PROGRESS",
            f"  { _bar(pct) } {pct}%",
            f"  completed={st.completed}/{st.total}",
            f"  pending={st.pending}  running={st.running}  success={st.success}",
            f"  failed={st.failed}  skipped={st.skipped}  retry={st.retry}  "
            f"deferred={site_counts.get(STATUS_DEFERRED, 0)}",
            f"  delay_sec={st.delay_sec}  speed={st.rpm} req/min  ETA={_fmt_eta(st.eta_sec)}",
            f"  last_activity={st.last_activity or '-'}  last_id={st.last_request_id or '-'}",
            f"  latency_ms avg={avg_ms:.0f} max={max_ms:.0f} (n={len(durs)})",
            "",
        ]
        if adapt:
            body += [
                "ADAPTIVE RPM (live) — CURRENT VALUES",
                f"  CURRENT_RPM={adapt.get('current_rpm', adapt.get('rpm'))}  "
                f"STABLE_RPM={adapt.get('stable_rpm')}  "
                f"PEAK_RPM={adapt.get('peak_rpm')}",
                f"  DELAY_SEC={adapt.get('delay_sec')}  "
                f"STEP_RPM={adapt.get('step_rpm')}  "
                f"BOUNDS=[{adapt.get('min_rpm')}..{adapt.get('max_rpm')}]",
                f"  last_cool_pause_sec={adapt.get('last_cool_pause_sec')}  "
                f"next_cool_pause_sec={adapt.get('next_cool_pause_sec')}",
                f"  success_streak={adapt.get('success_streak')}/{adapt.get('success_batch')}  "
                f"total_success={adapt.get('total_success')}  total_429={adapt.get('total_429')}",
                f"  banner: {adapt.get('banner') or '-'}",
                f"  live_file: {self.reports_dir / f'CURRENT_RPM_{site}.txt'}",
                f"  detail_log: {self.reports_dir / f'adaptive_rpm_{site}.txt'}",
                "",
                "  RULES:",
                "    - After every success_batch OK: lock STABLE_RPM, then CURRENT_RPM += STEP",
                "    - Climb stepwise until MAX_RPM (default 200)",
                "    - On 429: global cool-down + ROLL BACK CURRENT_RPM → STABLE_RPM",
                "    - If already at stable on 429: step DOWN and lower STABLE_RPM",
                "",
            ]
        body += [
            "HTTP STATUS BREAKDOWN",
            f"  {dict(http_c) if http_c else '{}'}",
            "",
            "ENDPOINT BREAKDOWN",
        ]
        body += ep_lines or ["  (none)"]
        body += [
            "",
            f"ERRORS / PROBLEMS (last {len(err_lines)}, failed+skipped with info)",
        ]
        body += err_lines or ["  (none)"]
        body.append("")
        body.append("NOTES")
        body.append("  - status skipped often means dependency failed or alternate path not needed")
        body.append("  - 429: THIS request is parked (retry later); pipeline continues other requests")
        body.append("  - 404/other client errors: FAILED permanently; pipeline moves on")
        body.append("  - wikipedia: single report reports/WIKI_REPORT.txt (all info + commands)")
        body.append("  - full bodies: responses/<request_id>.json")
        body.append("  - master plan: request_plan.xlsx / request_plan.jsonl")
        body.append("")
        path.write_text("\n".join(body) + "\n", encoding="utf-8")

    def write_once(self, *, final: bool = False) -> None:
        sites = self.store.sites()
        wall = time.monotonic() - self._started
        req_p = self._request_progress()
        cat_p = self._overall_gather_progress()

        lines: list[str] = [
            "PsychoFilm UNIFIED REPORT — Approach 2 gather",
            f"updated: {_utc()}  wall={wall:.0f}s  {'FINAL' if final else 'live'}",
            f"session_started: {self._session_start_utc}",
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
            f"skipped={req_p['skipped']} retry={req_p['retry']}",
            f"  speed={req_p['rpm']} req/min  ETA={_fmt_eta(req_p['eta_sec'])}",
            "",
            "=" * 72,
            "PER-PIPELINE SUMMARY",
            "=" * 72,
            "",
        ]

        for site in sites:
            st = self._stats_for_site(site)
            pct = _pct(st.completed, st.total)
            with self._lock:
                adapt = dict(self._adaptive.get(site) or {})
            lines += [
                f"[{site.upper()}]",
                f"  {_bar(pct)} {pct}%  completed={st.completed}/{st.total}",
                f"  pending={st.pending} running={st.running} success={st.success} "
                f"failed={st.failed} skipped={st.skipped} retry={st.retry}",
                f"  delay={st.delay_sec}s  rpm={st.rpm}  ETA={_fmt_eta(st.eta_sec)}",
                f"  last={st.last_activity or '-'}  id={st.last_request_id or '-'}",
                f"  detail_report: {self.reports_dir / f'pipeline_{site}.txt'}",
            ]
            if adapt:
                lines += [
                    f"  CURRENT_RPM={adapt.get('current_rpm', adapt.get('rpm'))}  "
                    f"STABLE_RPM={adapt.get('stable_rpm')}  "
                    f"PEAK_RPM={adapt.get('peak_rpm')}  "
                    f"DELAY_SEC={adapt.get('delay_sec')}  "
                    f"STEP={adapt.get('step_rpm')}  "
                    f"MAX={adapt.get('max_rpm')}",
                    f"  cool_next={adapt.get('next_cool_pause_sec')}s "
                    f"streak={adapt.get('success_streak')}/{adapt.get('success_batch')} "
                    f"ok={adapt.get('total_success')} 429={adapt.get('total_429')}",
                    f"  live_rpm: {self.reports_dir / f'CURRENT_RPM_{site}.txt'}",
                    f"  adaptive_log: {self.reports_dir / f'adaptive_rpm_{site}.txt'}",
                ]
            lines.append("")
            self._write_pipeline_report(site, st, final=final)

        # Global top errors
        lines += [
            "=" * 72,
            "TOP ERRORS / PROBLEMS (all pipelines, recent)",
            "=" * 72,
        ]
        all_err = []
        for site in sites:
            all_err.extend(self._site_errors(site, limit=15))
        if all_err:
            lines.extend(all_err[:50])
        else:
            lines.append("  (none)")

        # Global CURRENT_RPM banner for wikipedia (and any adaptive site)
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
            "FILES",
            f"  unified:     {self.unified_path}",
            f"  live_alias:  {self.path}",
            f"  per_pipeline:{self.reports_dir}/pipeline_*.txt",
            f"  wiki_report: {self.reports_dir / 'WIKI_REPORT.txt'}",
            f"  current_rpm: {self.reports_dir / 'CURRENT_RPM_wikipedia.txt'}",
            f"  plan:        {self.store.excel_path}",
            "=" * 72,
            "",
        ]

        text = "\n".join(lines)
        self.unified_path.write_text(text, encoding="utf-8")
        self.path.write_text(text, encoding="utf-8")  # alias for existing path

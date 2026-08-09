"""
Single all-in-one Wikipedia text report for Approach 2.

Canonical file:  output/gather_v2/reports/WIKI_REPORT.txt
Also mirrored to: pipeline_wikipedia.txt  (same content; for older links)

Contains: dashboard, sequence, policy, adaptive RPM, event table,
and FULL detail for every finished wiki request (all attempts + FULL_COMMAND_LINE).
"""

from __future__ import annotations

import json
from collections import Counter
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
)
from psychofilm_analyzer.gather_v2.plan_store import PlanStore

# Canonical single output name
WIKI_REPORT_NAME = "WIKI_REPORT.txt"
WIKI_REPORT_ALIAS = "pipeline_wikipedia.txt"


def _now() -> str:
    return now_str()


def _pct(n: int, d: int) -> float:
    return round(100.0 * n / d, 1) if d else 0.0


def _bar(pct: float, width: int = 24) -> str:
    f = int(round(width * min(max(pct, 0), 100) / 100))
    return "[" + "#" * f + "." * (width - f) + "]"


def _load_payload(store: PlanStore, r) -> dict[str, Any]:
    data = store.load_response(r.request_id)
    return data if isinstance(data, dict) else {}


def _load_attempts(store: PlanStore, r) -> list[dict[str, Any]]:
    data = _load_payload(store, r)
    if isinstance(data.get("attempts"), list):
        return data["attempts"]
    return []


def _cmd_for_row(store: PlanStore, r) -> str:
    a = _attr_from_row(store, r)
    cmd = (a.get("command") or "").strip()
    if cmd:
        return cmd
    cmd = (getattr(r, "reproducible_command", None) or "").strip()
    if cmd:
        return cmd
    if not r.url:
        return ""
    try:
        from psychofilm_analyzer.gather_v2.executor import build_reproducible_command
        from psychofilm_analyzer.utils.wikipedia_auth import wikipedia_headers

        headers = {}
        try:
            headers = json.loads(r.headers_json or "{}") or {}
        except Exception:
            headers = {}
        # Prefer live OAuth headers for retestability
        try:
            headers = {**wikipedia_headers(), **headers}
            # force current auth on top
            auth = wikipedia_headers()
            headers["Authorization"] = auth.get("Authorization", headers.get("Authorization", ""))
            headers["User-Agent"] = auth.get("User-Agent", headers.get("User-Agent", ""))
        except Exception:
            pass
        params = {}
        try:
            params = json.loads(r.params_json or "{}") or {}
        except Exception:
            params = {}
        return build_reproducible_command(
            method=r.method or "GET",
            url=r.url,
            params=params,
            headers=headers,
            timeout=20.0,
        )
    except Exception:
        return ""


def _attr_from_row(store: PlanStore, r) -> dict[str, Any]:
    data = _load_payload(store, r)
    attr: dict[str, Any] = {}
    if data:
        attr = dict(data.get("attribution") or {})
        if not attr and data.get("attempts"):
            last = data["attempts"][-1]
            attr = {
                "step": last.get("step"),
                "http_status": last.get("http_status"),
                "captured_at": last.get("captured_at"),
                "url": last.get("url"),
                "reproducible_command": last.get("reproducible_command"),
                "classification": last.get("classification")
                or data.get("classification"),
                "note": last.get("note") or data.get("message"),
                "page_title": last.get("page_title"),
            }
    cmd = (getattr(r, "reproducible_command", None) or "").strip()
    if not cmd:
        cmd = (attr.get("reproducible_command") or "").strip()
    cls = attr.get("classification") or r.deferred_reason or ""
    if not cls and r.error:
        cls = (r.error or "").split()[0]
    if not cls:
        cls = "-"
    return {
        "classification": cls,
        "captured_http": attr.get("http_status", r.http_status),
        "captured_at": attr.get("captured_at") or r.finished_at or r.started_at or "-",
        "url": attr.get("url") or r.url or "",
        "command": cmd,
        "step": attr.get("step"),
        "page_title": attr.get("page_title") or data.get("final_title") or "",
        "note": attr.get("note") or (r.error or "")[:300],
        "n_attempts": len(_load_attempts(store, r)),
        "final_title": data.get("final_title") or "",
        "ok": data.get("ok"),
        "message": data.get("message") or "",
    }


def load_adaptive_rpm_live(reports_dir: str | Path) -> dict[str, Any]:
    """
    Read CURRENT_RPM_wikipedia.txt written by AdaptiveRpmController.
    Used so WIKI_REPORT always shows RPM even if the progress-thread
    snapshot was empty / stale / written offline.
    """
    p = Path(reports_dir) / "CURRENT_RPM_wikipedia.txt"
    if not p.exists():
        # also try adaptive log header lines
        return {}
    out: dict[str, Any] = {"source": str(p)}
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            key = k.lower()
            if key in {
                "current_rpm",
                "stable_rpm",
                "peak_rpm",
                "delay_sec",
                "min_rpm",
                "max_rpm",
                "step_rpm",
                "next_cool_sec",
                "last_cool_sec",
                "total_ok",
                "total_429",
            }:
                try:
                    out[key if key != "current_rpm" else "rpm"] = float(v)
                    if key == "current_rpm":
                        out["current_rpm"] = float(v)
                    elif key == "total_ok":
                        out["total_success"] = int(float(v))
                    elif key == "total_429":
                        out["total_429"] = int(float(v))
                    elif key == "next_cool_sec":
                        out["next_cool_pause_sec"] = float(v)
                    elif key == "last_cool_sec":
                        out["last_cool_pause_sec"] = float(v)
                    else:
                        out[key] = float(v)
                except ValueError:
                    out[key] = v
            elif key == "success_streak":
                # format 2/8
                if "/" in v:
                    a, b = v.split("/", 1)
                    try:
                        out["success_streak"] = int(a)
                        out["success_batch"] = int(b)
                    except ValueError:
                        out["success_streak"] = v
                else:
                    out["success_streak"] = v
            elif key == "banner":
                out["banner"] = v
            elif key == "updated":
                out["live_file_updated"] = v
            else:
                out[key] = v
    except OSError:
        return {}
    return out


def write_wikipedia_pipeline_report(
    store: PlanStore,
    path: str | Path | None = None,
    *,
    adaptive: Optional[dict[str, Any]] = None,
    final: bool = False,
    reports_dir: str | Path | None = None,
    max_full_detail: int = 40,
    max_event_log: int = 40,
) -> Path:
    """
    Write the ONE Wikipedia text report with all information.

    If path is omitted: reports_dir/WIKI_REPORT.txt
    Always mirrors the same content to pipeline_wikipedia.txt next to it.
    """
    if path is None:
        base = Path(reports_dir or "output/gather_v2/reports")
        path = base / WIKI_REPORT_NAME
    path = Path(path)
    # If caller passed pipeline_wikipedia.txt, still treat WIKI_REPORT as canonical
    if path.name == WIKI_REPORT_ALIAS:
        path = path.parent / WIKI_REPORT_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    reports_base = path.parent

    rows = store.by_site("wikipedia")
    counts = Counter(r.status for r in rows)
    http = Counter(
        str(r.http_status) if r.http_status is not None else "-" for r in rows
    )
    by_ep: dict[str, Counter] = {}
    by_class: Counter = Counter()
    for r in rows:
        by_ep.setdefault(r.endpoint_type or "?", Counter())[r.status] += 1
        if r.status in (
            STATUS_FAILED,
            STATUS_DEFERRED,
            STATUS_SKIPPED,
            STATUS_RETRY,
            STATUS_SUCCESS,
        ):
            a = _attr_from_row(store, r)
            by_class[str(a.get("classification") or r.deferred_reason or r.status)] += 1

    total = len(rows)
    success = counts.get(STATUS_SUCCESS, 0)
    failed = counts.get(STATUS_FAILED, 0)
    deferred = counts.get(STATUS_DEFERRED, 0)
    pending = counts.get(STATUS_PENDING, 0)
    running = counts.get(STATUS_RUNNING, 0)
    retry = counts.get(STATUS_RETRY, 0)
    skipped = counts.get(STATUS_SKIPPED, 0)
    done = success + failed + skipped
    open_n = pending + running + deferred + retry
    pct = _pct(done, total)
    n429 = http.get("429", 0)
    n404 = http.get("404", 0)
    n200 = http.get("200", 0)

    # Prefer in-memory controller snapshot; always merge live file as fallback
    adapt = dict(adaptive or {})
    live = load_adaptive_rpm_live(reports_base)
    if live:
        # live file fills missing keys; snapshot values win when present & non-empty
        merged = dict(live)
        for k, v in adapt.items():
            if v is not None and v != "":
                merged[k] = v
        adapt = merged
    health = "OK"
    if n429 > max(success, 1) * 2 and success < 5:
        health = "BAD — many rate_limit (429) captures"
    elif success > 0 and open_n == 0:
        health = "OK — batch complete"
    elif success > 0:
        health = "RUNNING"
    elif total and done == 0:
        health = "STARTING / STALLED"

    finished = [
        r
        for r in rows
        if r.status
        in (
            STATUS_SUCCESS,
            STATUS_FAILED,
            STATUS_DEFERRED,
            STATUS_RETRY,
            STATUS_SKIPPED,
        )
        or (r.finished_at and r.status != STATUS_PENDING)
    ]
    # chronological: oldest first for reading through the run
    finished_chrono = sorted(finished, key=lambda r: r.finished_at or r.started_at or "")
    finished_recent = list(reversed(finished_chrono))

    lines: list[str] = [
        "================================================================================",
        "  WIKI_REPORT.txt  —  SINGLE ALL-IN-ONE WIKIPEDIA REPORT (Approach 2)",
        f"  updated: {_now()}   mode: {'FINAL' if final else 'LIVE'}",
        f"  path: {path}",
        "================================================================================",
        "",
        "  THIS IS THE ONLY WIKI TEXT REPORT YOU NEED.",
        "  Includes: dashboard + sequence + policy + event table + FULL commands",
        "  for every finished request (all attempts). Machine bodies: responses/<id>.json",
        "",
        "--------------------------------------------------------------------------------",
        "0) HOW TO READ ERRORS / COMMANDS",
        "--------------------------------------------------------------------------------",
        "  For each finished request below you get:",
        "    STATUS / CLASSIFICATION / CAPTURED_HTTP / CAPTURED_AT",
        "    OUTCOME FULL_COMMAND_LINE  → paste into PowerShell (bound to CAPTURED_HTTP)",
        "    ALL ATTEMPTS IN CHAIN       → every HTTP call with its own FULL_COMMAND_LINE",
        "",
        "  200 = found  |  404 = title missing  |  429 = transient (should be rare as outcome)",
        "  Retest of a past 429 may return 404/200 after cool-down.",
        "",
        "--------------------------------------------------------------------------------",
        "1) DASHBOARD",
        "--------------------------------------------------------------------------------",
        f"  Health:     {health}",
        f"  Progress:   {_bar(pct)}  {pct}%   ({done}/{total} finished)",
        f"  Open work:  {open_n}   (pending={pending} deferred={deferred} "
        f"retry={retry} running={running})",
        f"  Results:    success={success}  skipped={skipped}  failed={failed}",
        f"  HTTP (attributed):  200={n200}  404={n404}  429={n429}",
        f"  Classes:    {dict(by_class) if by_class else '{}'}",
        "",
    ]
    has_rpm = adapt.get("current_rpm") is not None or adapt.get("rpm") is not None
    if has_rpm:
        src = adapt.get("source") or "controller_snapshot"
        lines += [
            "  Adaptive RPM (live)",
            f"    CURRENT_RPM = {adapt.get('current_rpm', adapt.get('rpm'))}",
            f"    STABLE_RPM  = {adapt.get('stable_rpm')}",
            f"    PEAK_RPM    = {adapt.get('peak_rpm')}",
            f"    DELAY_SEC   = {adapt.get('delay_sec')}",
            f"    STEP_RPM    = {adapt.get('step_rpm')}   "
            f"MAX_RPM = {adapt.get('max_rpm')}   "
            f"MIN_RPM = {adapt.get('min_rpm')}",
            f"    streak={adapt.get('success_streak')}/{adapt.get('success_batch')}  "
            f"ok={adapt.get('total_success')}  429={adapt.get('total_429')}  "
            f"cool_next={adapt.get('next_cool_pause_sec')}s",
            f"    banner: {adapt.get('banner') or '-'}",
            f"    source: {src}"
            + (
                f"  file_updated={adapt.get('live_file_updated')}"
                if adapt.get("live_file_updated")
                else ""
            ),
            f"    live:   CURRENT_RPM_wikipedia.txt",
            f"    detail: adaptive_rpm_wikipedia.txt",
            "",
            "  RPM POLICY:",
            "    - After every success_batch OK → lock STABLE, then CURRENT += STEP",
            "    - Climb stepwise until MAX_RPM (200)",
            "    - On 429 → cool-down + ROLL BACK CURRENT → STABLE",
            "",
        ]
    else:
        lines += [
            "  Adaptive RPM: no CURRENT_RPM yet",
            "    (controller writes reports/CURRENT_RPM_wikipedia.txt once wiki pipeline starts;",
            "     open that file for live RPM; this section fills on next DETAIL refresh)",
            "",
        ]

    lines += [
        "--------------------------------------------------------------------------------",
        "2) SEQUENCE  (how each film/lang is searched)",
        "--------------------------------------------------------------------------------",
        "  STEP 1  Direct REST summary: bare title → (film) → (YEAR film)",
        "  STEP 2  MediaWiki search: srsearch = \"<title> <year> film\"",
        "  STEP 3  REST summary on each search hit",
        "  STEP 4  Outcome: success | not_found(skip) | rate_limit/network(defer)",
        "  Auth:   Authorization Bearer from WIKIMEDIA_ACCESS_TOKEN (.env) when set",
        "",
        "--------------------------------------------------------------------------------",
        "3) POLICY  (TRIGGER → ACTION)",
        "--------------------------------------------------------------------------------",
        "  200 + extract     → SUCCESS (command = winning GET)",
        "  404 / not_found   → next candidate; after search → SKIPPED",
        "  429               → cool + re-fetch SAME url until durable status",
        "  5xx / network     → DEFER (retry later)",
        "",
        "--------------------------------------------------------------------------------",
        "4) OUTCOMES BY LANGUAGE",
        "--------------------------------------------------------------------------------",
    ]
    for ep in sorted(by_ep.keys()):
        c = by_ep[ep]
        parts = "  ".join(f"{k}={v}" for k, v in sorted(c.items()))
        n = sum(c.values())
        lines.append(f"  {ep:14s}  n={n:4d}   {parts}")

    ev_cap = max(10, int(max_event_log))
    lines += [
        "",
        "--------------------------------------------------------------------------------",
        f"5) EVENT LOG  (newest first, last {ev_cap} — compact)",
        "--------------------------------------------------------------------------------",
        f"  {'WHEN':22s}  {'STAT':8s}  {'HTTP':4s}  {'CLASS':14s}  {'ENDP':12s}  FILM",
        "  " + "-" * 78,
    ]
    if not finished_recent:
        lines.append("  (no finished wiki requests yet)")
    else:
        for r in finished_recent[:ev_cap]:
            a = _attr_from_row(store, r)
            when = str(a.get("captured_at") or r.finished_at or "-")[:22]
            film = f"{r.film_title} ({r.year})"
            if len(film) > 36:
                film = film[:33] + "..."
            lines.append(
                f"  {when:22s}  {r.status:8s}  {str(a.get('captured_http') or '-'):4s}  "
                f"{str(a.get('classification') or '-'):14s}  "
                f"{(r.endpoint_type or '-'):12s}  {film}"
            )
        if len(finished_recent) > ev_cap:
            lines.append(f"  ... +{len(finished_recent) - ev_cap} more finished (see responses/)")

    # Cap full dumps so DETAIL refresh does not take 2+ minutes and stall pipelines
    detail_cap = max(10, int(max_full_detail))
    # Prefer newest problems + newest overall
    problems = [
        r
        for r in finished_recent
        if r.status in (STATUS_FAILED, STATUS_SKIPPED, STATUS_DEFERRED, STATUS_RETRY)
    ]
    detail_rows = problems[: detail_cap // 2]
    seen_ids = {r.request_id for r in detail_rows}
    for r in finished_recent:
        if len(detail_rows) >= detail_cap:
            break
        if r.request_id not in seen_ids:
            detail_rows.append(r)
            seen_ids.add(r.request_id)

    lines += [
        "",
        "--------------------------------------------------------------------------------",
        f"6) FULL DETAIL + FULL_COMMAND_LINE  "
        f"(showing {len(detail_rows)} of {len(finished_chrono)} finished; newest/problems first)",
        "--------------------------------------------------------------------------------",
        "  Cap keeps DETAIL report fast. Full bodies always in responses/<request_id>.json.",
        "  Paste FULL_COMMAND_LINE into PowerShell to retest independently.",
        "",
    ]
    if not detail_rows:
        lines.append("  (none yet)")
    else:
        for i, r in enumerate(detail_rows, 1):
            a = _attr_from_row(store, r)
            cmd = _cmd_for_row(store, r)
            attempts = _load_attempts(store, r)
            lines += [
                f"{'=' * 72}",
                f"#{i}  {r.request_id}",
                f"  FILM:            {r.film_title!r} ({r.year})  excel_row={r.excel_row}",
                f"  ENDPOINT:        {r.endpoint_type}",
                f"  STATUS:          {r.status}",
                f"  CLASSIFICATION:  {a.get('classification')}",
                f"  CAPTURED_HTTP:   {a.get('captured_http')}",
                f"  CAPTURED_AT:     {a.get('captured_at')}",
                f"  FINAL_TITLE:     {a.get('final_title') or a.get('page_title') or '-'}",
                f"  STEP:            {a.get('step')}  (of {a.get('n_attempts') or len(attempts)} attempts)",
                f"  URL:             {a.get('url')}",
                f"  NOTE:            {str(a.get('note') or a.get('message') or '')[:240]}",
                f"  OUTCOME FULL_COMMAND_LINE (bound to CAPTURED_HTTP={a.get('captured_http')}):",
                f"  {cmd or '(MISSING COMMAND)'}",
                "",
            ]
            if attempts:
                lines.append(
                    "  --- ALL ATTEMPTS (each line has its own CAPTURED_HTTP + FULL_COMMAND_LINE) ---"
                )
                for att in attempts:
                    lines += [
                        f"  [step {att.get('step')}] kind={att.get('kind')} "
                        f"CAPTURED_HTTP={att.get('http_status')} "
                        f"CLASS={att.get('classification')} "
                        f"TITLE={att.get('page_title')!r}",
                        f"  CAPTURED_AT={att.get('captured_at')}",
                        f"  NOTE={str(att.get('note') or '')[:160]}",
                        f"  BODY={(att.get('body_preview') or '')[:120]!r}",
                        f"  FULL_COMMAND_LINE:",
                        f"  {att.get('reproducible_command') or '(MISSING)'}",
                        "",
                    ]
            else:
                lines.append("  (no multi-step attempts stored — only outcome command above)")
                lines.append("")

    # Pending still open (ids only — not full commands yet)
    still_open = [
        r
        for r in rows
        if r.status in (STATUS_PENDING, STATUS_RUNNING, STATUS_DEFERRED, STATUS_RETRY)
    ]
    lines += [
        "--------------------------------------------------------------------------------",
        f"7) STILL OPEN  ({len(still_open)} requests)",
        "--------------------------------------------------------------------------------",
    ]
    if not still_open:
        lines.append("  (none)")
    else:
        by_st = Counter(r.status for r in still_open)
        lines.append(f"  by status: {dict(by_st)}")
        for r in still_open[:50]:
            lines.append(
                f"  {r.status:10s}  {r.request_id}  {r.endpoint_type}  "
                f"{r.film_title!r} ({r.year})"
            )
        if len(still_open) > 50:
            lines.append(f"  ... +{len(still_open) - 50} more")

    lines += [
        "",
        "--------------------------------------------------------------------------------",
        "8) PROCEDURE SUMMARY",
        "--------------------------------------------------------------------------------",
        "  P1  Search: bare → (film) → (YEAR film) → MediaWiki search → hit summaries.",
        "  P2  Each HTTP call recorded with FULL_COMMAND_LINE + CAPTURED_HTTP + time.",
        "  P3  429: cool + same URL re-fetch until durable 404/200.",
        "  P4  OAuth Bearer from .env when configured (highvolume).",
        "  P5  This single file is regenerated live during the run.",
        "",
        "================================================================================",
        f"  END WIKI_REPORT  |  total_rows={total}  finished={len(finished_chrono)}  "
        f"open={len(still_open)}  |  {_now()}",
        "================================================================================",
        "",
    ]

    text = "\n".join(lines)
    path.write_text(text, encoding="utf-8")
    # Mirror for progress.py path pipeline_wikipedia.txt
    alias = path.parent / WIKI_REPORT_ALIAS
    try:
        alias.write_text(text, encoding="utf-8")
    except Exception:
        pass
    return path

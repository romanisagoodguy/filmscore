"""Per-site Approach 2 pipeline reports (TMDB / OMDb / Kinopoisk / Letterboxd / generic).

Mirrors the usefulness of WIKI_REPORT for every other site:
  dashboard + classes + endpoints + event log + FULL_COMMAND_LINE blocks.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
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
)
from psychofilm_analyzer.gather_v2.plan_store import PlanStore
from psychofilm_analyzer.utils.localtime import now_str


def _pct(n: int, d: int) -> float:
    return round(100.0 * n / d, 1) if d else 0.0


def _bar(pct: float, width: int = 24) -> str:
    f = int(round(width * min(max(pct, 0), 100) / 100))
    return "[" + "#" * f + "." * (width - f) + "]"


def _cmd_for_row(r) -> str:
    cmd = (getattr(r, "reproducible_command", None) or "").strip()
    if cmd:
        return cmd
    err = r.error or ""
    if "REPRO:" in err:
        return err.split("REPRO:", 1)[-1].strip()
    if not r.url:
        return ""
    try:
        from psychofilm_analyzer.gather_v2.executor import build_reproducible_command

        params = json.loads(r.params_json or "{}") or {}
        headers = json.loads(r.headers_json or "{}") or {}
        return build_reproducible_command(
            method=r.method or "GET",
            url=r.url,
            params=params,
            headers=headers,
            timeout=20.0,
        )
    except Exception:
        return ""


def _class_for_row(r) -> str:
    if r.deferred_reason:
        return str(r.deferred_reason)
    if r.status == STATUS_SUCCESS:
        return "success"
    if r.status == STATUS_SKIPPED:
        err = (r.error or "").lower()
        if "not found" in err or "empty" in err or "omdb_not_found" in err:
            return "not_found"
        return "skipped"
    if r.status == STATUS_DEFERRED:
        if r.http_status == 429:
            return "rate_limit"
        return "deferred"
    if r.status == STATUS_FAILED:
        if r.http_status == 429:
            return "rate_limit"
        if r.http_status and int(r.http_status) >= 500:
            return "server_error"
        return "failed"
    return str(r.status)


def write_site_pipeline_report(
    store: PlanStore,
    site: str,
    path: str | Path,
    *,
    delay_sec: float = 0.0,
    final: bool = False,
    max_detail_rows: int = 80,
    max_success_cmds: int = 15,
) -> Path:
    """Write full pipeline_{site}.txt for a non-wikipedia site."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = store.by_site(site)
    counts = Counter(r.status for r in rows)
    http = Counter(
        str(r.http_status) if r.http_status is not None else ("ERR" if r.error else "-")
        for r in rows
    )
    by_ep: dict[str, Counter] = defaultdict(Counter)
    by_class: Counter = Counter()
    for r in rows:
        by_ep[r.endpoint_type or "?"][r.status] += 1
        if r.status in (
            STATUS_SUCCESS,
            STATUS_FAILED,
            STATUS_SKIPPED,
            STATUS_DEFERRED,
            STATUS_RETRY,
        ):
            by_class[_class_for_row(r)] += 1

    total = len(rows)
    success = counts.get(STATUS_SUCCESS, 0)
    failed = counts.get(STATUS_FAILED, 0)
    skipped = counts.get(STATUS_SKIPPED, 0)
    deferred = counts.get(STATUS_DEFERRED, 0)
    pending = counts.get(STATUS_PENDING, 0)
    running = counts.get(STATUS_RUNNING, 0)
    retry = counts.get(STATUS_RETRY, 0)
    done = success + failed + skipped
    open_n = pending + running + deferred + retry
    pct = _pct(done, total)

    durs = [float(r.duration_ms) for r in rows if r.duration_ms]
    avg_ms = sum(durs) / len(durs) if durs else 0.0
    max_ms = max(durs) if durs else 0.0

    finished = [
        r
        for r in rows
        if r.status
        in (STATUS_SUCCESS, STATUS_FAILED, STATUS_SKIPPED, STATUS_DEFERRED, STATUS_RETRY)
    ]
    finished_recent = sorted(
        finished, key=lambda r: r.finished_at or r.started_at or "", reverse=True
    )
    problems = [
        r
        for r in finished_recent
        if r.status
        in (STATUS_FAILED, STATUS_SKIPPED, STATUS_DEFERRED, STATUS_RETRY)
    ]
    successes = [r for r in finished_recent if r.status == STATUS_SUCCESS]

    lines: list[str] = [
        "================================================================================",
        f"  PIPELINE REPORT — {site.upper()}  (Approach 2)",
        f"  updated: {now_str()}   mode: {'FINAL' if final else 'LIVE'}",
        f"  path: {path}",
        "================================================================================",
        "",
        "--------------------------------------------------------------------------------",
        "1) DASHBOARD",
        "--------------------------------------------------------------------------------",
        f"  Progress:   {_bar(pct)}  {pct}%   ({done}/{total} finished)",
        f"  Open work:  {open_n}   (pending={pending} deferred={deferred} "
        f"retry={retry} running={running})",
        f"  Results:    success={success}  skipped={skipped}  failed={failed}",
        f"  HTTP:       {dict(http) if http else '{}'}",
        f"  Classes:    {dict(by_class) if by_class else '{}'}",
        f"  delay_sec:  {delay_sec}   latency_ms avg={avg_ms:.0f} max={max_ms:.0f} (n={len(durs)})",
        "",
        "  Classes are REQUEST COUNTS (not RPM). e.g. rate_limit=N means N requests got 429.",
        "",
        "--------------------------------------------------------------------------------",
        "2) ENDPOINTS",
        "--------------------------------------------------------------------------------",
    ]
    for ep in sorted(by_ep.keys()):
        c = by_ep[ep]
        parts = "  ".join(f"{k}={v}" for k, v in sorted(c.items()))
        lines.append(f"  {ep:16s}  n={sum(c.values()):5d}   {parts}")

    lines += [
        "",
        "--------------------------------------------------------------------------------",
        "3) EVENT LOG (finished, newest first — compact)",
        "--------------------------------------------------------------------------------",
        f"  {'WHEN':22s}  {'STAT':8s}  {'HTTP':4s}  {'CLASS':14s}  {'ENDP':14s}  FILM",
        "  " + "-" * 78,
    ]
    for r in finished_recent[: min(40, max_detail_rows)]:
        when = (r.finished_at or r.started_at or "-")[:22]
        lines.append(
            f"  {when:22s}  {r.status:8s}  {str(r.http_status or '-'):4s}  "
            f"{_class_for_row(r):14s}  {(r.endpoint_type or '?'):14s}  "
            f"{(r.film_title or '')[:40]!r}"
        )

    lines += [
        "",
        "--------------------------------------------------------------------------------",
        "4) PROBLEMS + FULL_COMMAND_LINE (newest first)",
        "--------------------------------------------------------------------------------",
        "  Each block: CAPTURED_HTTP is bound ONLY to THIS_COMMAND_ONLY at CAPTURED_AT.",
        "",
    ]
    for r in problems[:max_detail_rows]:
        cmd = _cmd_for_row(r)
        lines += [
            f"  --- {r.request_id} ---",
            f"  STATUS={r.status}  CLASS={_class_for_row(r)}  "
            f"CAPTURED_HTTP={r.http_status}  CAPTURED_AT={r.finished_at or r.started_at or '-'}",
            f"  film={r.film_title!r} ({r.year}) excel_row={r.excel_row}  "
            f"endpoint={r.endpoint_type}",
            f"  url: {r.url or ''}",
            f"  note: {(r.error or '')[:200]}",
        ]
        if cmd:
            lines += [
                "  THIS_COMMAND_ONLY (paste into PowerShell):",
                f"  {cmd}",
            ]
        else:
            lines.append("  THIS_COMMAND_ONLY: (unavailable — no URL / soft-skip)")
        lines.append("")

    if not problems:
        lines.append("  (none)")
        lines.append("")

    lines += [
        "--------------------------------------------------------------------------------",
        "5) SAMPLE SUCCESS COMMANDS (newest)",
        "--------------------------------------------------------------------------------",
    ]
    for r in successes[:max_success_cmds]:
        cmd = _cmd_for_row(r)
        lines.append(
            f"  OK {r.request_id}  http={r.http_status}  "
            f"{r.endpoint_type}  {r.film_title!r}"
        )
        if cmd:
            lines.append(f"  {cmd}")
        lines.append("")
    if not successes:
        lines.append("  (none yet)")
        lines.append("")

    lines += [
        "--------------------------------------------------------------------------------",
        "6) FILES",
        "--------------------------------------------------------------------------------",
        f"  this report:     {path}",
        f"  error commands:  {path.parent / f'{site}_ERROR_COMMANDS.txt'}",
        f"  response JSON:   responses/<request_id>.json",
        f"  master plan:     request_plan.xlsx / request_plan.jsonl",
        f"  profiles:        profile_a2_live.xlsx (all sites merged)",
        f"  written:         {now_str()}",
        "",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_light_site_status(
    store: PlanStore,
    site: str,
    path: str | Path,
    *,
    delay_sec: float = 0.0,
    last_activity: str = "",
    last_id: str = "",
    rpm: float = 0.0,
) -> Path:
    """Fast small status file so non-wiki pipelines update every few seconds."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    counts = store.counts_by_status(site)
    total = sum(counts.values())
    done = (
        counts.get(STATUS_SUCCESS, 0)
        + counts.get(STATUS_FAILED, 0)
        + counts.get(STATUS_SKIPPED, 0)
    )
    pending = (
        counts.get(STATUS_PENDING, 0)
        + counts.get(STATUS_RETRY, 0)
        + counts.get(STATUS_DEFERRED, 0)
    )
    pct = _pct(done, total)
    lines = [
        f"PIPELINE STATUS (FAST) — {site.upper()}",
        f"updated: {now_str()}  (full DETAIL report every ~30s)",
        f"progress: {_bar(pct)} {pct}%  done={done}/{total}",
        f"pending={counts.get(STATUS_PENDING, 0)}  running={counts.get(STATUS_RUNNING, 0)}  "
        f"success={counts.get(STATUS_SUCCESS, 0)}  failed={counts.get(STATUS_FAILED, 0)}  "
        f"skipped={counts.get(STATUS_SKIPPED, 0)}  deferred={counts.get(STATUS_DEFERRED, 0)}  "
        f"retry={counts.get(STATUS_RETRY, 0)}",
        f"open={pending}  delay_sec={delay_sec}  observed_finish_rpm~{rpm}",
        f"last={last_activity or '-'}  id={last_id or '-'}",
        f"detail: {path.parent / f'pipeline_{site}.txt'} (DETAIL rewrite)",
        f"errors: {path.parent / f'{site}_ERROR_COMMANDS.txt'}",
        "",
    ]
    # Fast status lives next to full report: pipeline_{site}_live.txt
    path.write_text("\n".join(lines), encoding="utf-8")
    return path

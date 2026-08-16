"""Empirical speed diagnosis for spisok02_hdd gather — no opinions, numbers only."""
from __future__ import annotations

import json
import os
import socket
import statistics
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "spisok02_hdd" / "gather_v2" / "request_plan.jsonl"
RESP = ROOT / "spisok02_hdd" / "gather_v2" / "responses"
REPORT = ROOT / "spisok02_hdd" / "gather_v2" / "reports" / "UNIFIED_REPORT.txt"


def parse_finished_at(fa: str):
    if not fa:
        return None
    core = fa.split("+")[0].strip().split("Z")[0].strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(core[:26] if "." in core else core[:19], fmt)
        except ValueError:
            continue
    return None


def pct(xs, p):
    if not xs:
        return None
    s = sorted(xs)
    i = min(int(len(s) * p / 100), len(s) - 1)
    return s[i]


def main() -> None:
    print("=== A) PLAN DURATION STATS (duration_ms on finished rows) ===")
    by_site: dict[str, list[float]] = defaultdict(list)
    status_c: Counter = Counter()
    http_c: dict[str, Counter] = defaultdict(Counter)
    def_reason: Counter = Counter()
    times: list[tuple[datetime, str, str]] = []
    n_total = 0

    with PLAN.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            n_total += 1
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            site = r.get("site") or "?"
            st = r.get("status") or ""
            status_c[(site, st)] += 1
            hs = r.get("http_status")
            if hs is not None:
                http_c[site][str(hs)] += 1
            d = r.get("duration_ms")
            if d is not None and st in ("success", "failed", "skipped", "deferred"):
                try:
                    by_site[site].append(float(d))
                except (TypeError, ValueError):
                    pass
            if st == "deferred":
                def_reason[f"{site}:{(r.get('deferred_reason') or '')[:50]}"] += 1
            fa = parse_finished_at(r.get("finished_at") or "")
            if fa:
                times.append((fa, site, st))

    print(f"plan_rows={n_total} plan_MB={PLAN.stat().st_size / 1e6:.1f}")
    for site in sorted(by_site):
        xs = by_site[site]
        print(f"\n[{site}] n_with_duration={len(xs)}")
        print(
            f"  duration_ms mean={statistics.mean(xs):.0f} median={statistics.median(xs):.0f} "
            f"p90={pct(xs, 90):.0f} p99={pct(xs, 99):.0f} max={max(xs):.0f}"
        )
        med = statistics.median(xs) or 1
        print(f"  theoretical 1-worker ceiling from median: {60000 / med:.1f} plan-rows/min")
        sc = {st: c for (s, st), c in status_c.items() if s == site}
        print(f"  status={sc}")
        print(f"  http={dict(http_c[site])}")
    print("\nTOP deferred:", def_reason.most_common(12))

    print("\n=== B) FINISH RATE FROM finished_at TIMESTAMPS ===")
    times.sort()
    print(f"parsed_finished_at={len(times)}")
    if times:
        end = times[-1][0]
        for mins in (5, 10, 15, 30, 60):
            start = end - timedelta(minutes=mins)
            win = [t for t in times if t[0] >= start]
            by = Counter(t[1] for t in win)
            print(
                f"  last {mins:>2}m: n={len(win):4d} overall_rpm={len(win) / mins:5.1f} "
                f"by_site={dict(by)}"
            )

    print("\n=== C) RECENT RESPONSE HTTP ATTEMPT MS (disk) ===")
    files = sorted(RESP.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:120]
    http_ms: list[float] = []
    steps = Counter()
    for p in files:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(d, list):
            continue
        if not isinstance(d, dict):
            continue
        atts = d.get("attempts")
        if isinstance(atts, list) and atts:
            steps[len(atts)] += 1
            for a in atts:
                if isinstance(a, dict) and a.get("duration_ms") is not None:
                    http_ms.append(float(a["duration_ms"]))
        elif d.get("duration_ms") is not None:
            http_ms.append(float(d["duration_ms"]))
    if http_ms:
        print(
            f"  n={len(http_ms)} mean={statistics.mean(http_ms):.0f} "
            f"median={statistics.median(http_ms):.0f} max={max(http_ms):.0f}"
        )
    print(f"  multi-step hist (recent wiki-like)={dict(steps)}")

    print("\n=== D) LIVE NETWORK PROBE (this machine, now) ===")
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
        if os.environ.get(k):
            print(f"  ENV {k}={os.environ[k][:100]}")
    hosts = [
        "api.themoviedb.org",
        "www.omdbapi.com",
        "kinopoiskapiunofficial.tech",
        "en.wikipedia.org",
        "ru.wikipedia.org",
        "de.wikipedia.org",
        "letterboxd.com",
    ]
    for h in hosts:
        t0 = time.perf_counter()
        try:
            infos = socket.getaddrinfo(h, 443, type=socket.SOCK_STREAM)
            ip = infos[0][4][0]
            dns_ms = (time.perf_counter() - t0) * 1000
            s = socket.socket()
            s.settimeout(10)
            t1 = time.perf_counter()
            s.connect((ip, 443))
            tcp_ms = (time.perf_counter() - t1) * 1000
            s.close()
            print(f"  {h:36s} dns={dns_ms:6.0f}ms tcp={tcp_ms:6.0f}ms ip={ip}")
        except Exception as e:
            print(f"  {h:36s} FAIL {type(e).__name__}: {e}")

    ua = {
        "User-Agent": "PsychoFilmAnalyzer/1.0 (+diag)",
        "Accept": "application/json",
    }
    urls = [
        ("wiki_en", "https://en.wikipedia.org/api/rest_v1/page/summary/Inception"),
        ("wiki_ru", "https://ru.wikipedia.org/api/rest_v1/page/summary/Inception"),
        ("tmdb_cfg", "https://api.themoviedb.org/3/configuration"),
    ]
    print("\n  HTTP samples (3x):")
    for name, url in urls:
        ms = []
        codes = []
        for _ in range(3):
            t0 = time.perf_counter()
            try:
                r = requests.get(url, headers=ua, timeout=20)
                codes.append(r.status_code)
                ms.append((time.perf_counter() - t0) * 1000)
            except Exception as e:
                codes.append(type(e).__name__)
                ms.append((time.perf_counter() - t0) * 1000)
        print(f"  {name:10s} codes={codes} ms={[round(x) for x in ms]} mean={statistics.mean(ms):.0f}")

    print("\n  Parallel 6x wiki EN GET:")

    def one(i: int):
        t0 = time.perf_counter()
        try:
            r = requests.get(urls[0][1], headers=ua, timeout=20)
            return (i, r.status_code, (time.perf_counter() - t0) * 1000)
        except Exception as e:
            return (i, type(e).__name__, (time.perf_counter() - t0) * 1000)

    t0 = time.perf_counter()
    with ThreadPoolExecutor(6) as ex:
        res = list(ex.map(one, range(6)))
    wall = (time.perf_counter() - t0) * 1000
    print(f"  results={[(a, b, round(c)) for a, b, c in res]}")
    print(f"  wall_ms={wall:.0f}")

    print("\n=== E) REPORT HEADER (live) ===")
    if REPORT.exists():
        for i, line in enumerate(REPORT.read_text(encoding="utf-8", errors="replace").splitlines()):
            if i >= 40:
                break
            print(" ", line)

    print("\n=== F) BOTTLENECK MATH ===")
    # Per-site theoretical with N workers = 1 for most, 3 for wiki langs but shared RPM
    print("  Pipeline design: 1 thread per site (except wiki 3 lang workers).")
    print("  Sites ARE parallel with each other.")
    print("  Within a site (except wiki langs), requests are SERIAL.")
    if "tmdb" in by_site and by_site["tmdb"]:
        m = statistics.median(by_site["tmdb"])
        print(f"  TMDB median {m:.0f}ms => ~{60000/m:.0f} rows/min max with 1 worker")
    if "kinopoisk" in by_site and by_site["kinopoisk"]:
        m = statistics.median(by_site["kinopoisk"])
        print(f"  KP median {m:.0f}ms => ~{60000/m:.0f} rows/min max with 1 worker")
    if "wikipedia" in by_site and by_site["wikipedia"]:
        m = statistics.median(by_site["wikipedia"])
        print(f"  Wiki plan-row median {m:.0f}ms => ~{60000/m:.0f} rows/min per lang worker")
        print(f"  With 3 lang workers (ideal, no shared delay): ~{3*60000/m:.0f} rows/min")
    print(f"  Total plan 170065 / 60 rpm overall => {170065/60/60:.1f} hours")
    print(f"  Total plan 170065 / 200 rpm overall => {170065/200/60:.1f} hours")


if __name__ == "__main__":
    main()

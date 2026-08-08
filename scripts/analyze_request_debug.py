#!/usr/bin/env python3
"""Analyze request_debug_FULLRUN tables after a gather stop."""

from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output"


def main() -> None:
    prog = OUT / "gather_progress.json"
    if prog.exists():
        print("=== GATHER PROGRESS ===")
        print(prog.read_text(encoding="utf-8"))

    ckpt = OUT / "gather_checkpoint.jsonl"
    if ckpt.exists():
        n = sum(1 for line in ckpt.open(encoding="utf-8") if line.strip())
        print(f"checkpoint_lines={n}")

    req_p = OUT / "request_debug_FULLRUN_requests.csv"
    films_p = OUT / "request_debug_FULLRUN_films.csv"
    sites_p = OUT / "request_debug_FULLRUN_sites.csv"
    txt_p = OUT / "request_debug_FULLRUN.txt"

    print("\n=== FILE SIZES ===")
    for p in [req_p, films_p, sites_p, txt_p, OUT / "request_debug_FULLRUN.xlsx"]:
        print(f"{p.name}: {p.stat().st_size if p.exists() else 0:,}")

    rows = list(csv.DictReader(req_p.open(encoding="utf-8-sig")))
    films = list(csv.DictReader(films_p.open(encoding="utf-8-sig")))
    print(f"\ntotal_requests={len(rows)} total_films={len(films)}")
    if films:
        print(
            f"film_seq {films[0].get('film_seq')}..{films[-1].get('film_seq')} "
            f"excel_row {films[0].get('excel_row')}..{films[-1].get('excel_row')}"
        )
        print(f"first: {films[0].get('title')} ({films[0].get('year')})")
        print(f"last:  {films[-1].get('title')} ({films[-1].get('year')})")

    by_site: Counter[str] = Counter()
    ok_site: Counter[str] = Counter()
    fail_site: Counter[str] = Counter()
    status_site: dict[str, Counter[str]] = defaultdict(Counter)
    dur_site: dict[str, list[float]] = defaultdict(list)
    errors: Counter[str] = Counter()
    err_ex: dict[str, list[dict]] = defaultdict(list)
    urls_kw = 0
    urls_lb_search = 0

    for r in rows:
        site = r.get("site") or "?"
        ok = str(r.get("ok", "")).lower() in ("true", "1")
        sc = r.get("status_code") or ""
        err = (r.get("error") or "").strip()
        url = r.get("url") or ""
        try:
            d = float(r.get("duration_ms") or 0)
        except ValueError:
            d = 0.0
        by_site[site] += 1
        dur_site[site].append(d)
        sk = str(sc) if sc != "" else ("ERR:" + err[:50] if err else "NONE")
        status_site[site][sk] += 1
        if ok:
            ok_site[site] += 1
        else:
            fail_site[site] += 1
            key = f"{site}|{sk}|{err[:100]}"
            errors[key] += 1
            if len(err_ex[key]) < 2:
                err_ex[key].append(
                    {
                        "title": r.get("title"),
                        "year": r.get("year"),
                        "excel_row": r.get("excel_row"),
                        "url": url[:140],
                        "preview": (r.get("response_preview") or "")[:160],
                    }
                )
        if "/keywords" in url and "kinopoisk" in url:
            urls_kw += 1
        if "letterboxd.com/search" in url:
            urls_lb_search += 1

    print("\n=== BY SITE ===")
    for s, n in by_site.most_common():
        durs = dur_site[s]
        avg = sum(durs) / len(durs) if durs else 0
        print(
            f"{s}: n={n} ok={ok_site[s]} fail={fail_site[s]} "
            f"avg_ms={avg:.0f} max_ms={max(durs) if durs else 0:.0f}"
        )
        print(f"  statuses: {dict(status_site[s])}")

    print(f"\nkp_keywords_calls={urls_kw}  lb_search_calls={urls_lb_search}")

    print("\n=== TOP FAIL PATTERNS ===")
    for k, c in errors.most_common(15):
        print(f"  n={c} {k}")
        for ex in err_ex[k][:1]:
            print(f"    eg: {ex}")

    print("\n=== FILMS SUMMARY ===")
    src_miss: Counter[str] = Counter()
    src_found: Counter[str] = Counter()
    fail_sum: Counter[str] = Counter()
    n_req: list[int] = []
    qms: list[float] = []
    wms: list[float] = []
    for fr in films:
        for src in ["tmdb", "omdb", "kinopoisk", "wikipedia", "letterboxd"]:
            v = (fr.get(f"src_{src}") or "").upper()
            if v == "MISS":
                src_miss[src] += 1
            elif v == "FOUND":
                src_found[src] += 1
            try:
                fail_sum[src] += int(float(fr.get(f"fail_{src}") or 0))
            except ValueError:
                pass
        try:
            n_req.append(int(float(fr.get("http_requests") or 0)))
        except ValueError:
            pass
        try:
            qms.append(float(fr.get("film_query_ms") or 0))
        except ValueError:
            pass
        try:
            wms.append(float(fr.get("film_wall_ms") or 0))
        except ValueError:
            pass

    print("FOUND:", dict(src_found))
    print("MISS:", dict(src_miss))
    print("sum fail_*:", dict(fail_sum))
    if films:
        print(
            f"avg http/film={sum(n_req)/len(n_req):.1f}  "
            f"avg query_ms={sum(qms)/len(qms):.0f}  "
            f"avg wall_ms={sum(wms)/len(wms):.0f}"
        )
        print(f"total query_s={sum(qms)/1000:.1f}  total wall_s={sum(wms)/1000:.1f}")

    lb_calls: dict[str, int] = defaultdict(int)
    for r in rows:
        if r.get("site") == "letterboxd":
            lb_calls[str(r.get("film_seq"))] += 1
    if lb_calls:
        vals = list(lb_calls.values())
        print(
            f"letterboxd calls/film: avg={sum(vals)/len(vals):.2f} max={max(vals)} "
            f"films_with_2plus={sum(1 for v in vals if v >= 2)}"
        )

    wiki_rows = [r for r in rows if r.get("site") == "wikipedia"]
    print(
        f"wiki calls={len(wiki_rows)} films={len(films)} "
        f"ratio={len(wiki_rows)/max(len(films),1):.2f}"
    )
    print(
        "wiki statuses",
        Counter((r.get("status_code") or (r.get("error") or "")[:40]) for r in wiki_rows),
    )

    # Cooldown skips: films with n_wikipedia=0 and MISS
    cool = sum(
        1
        for fr in films
        if (fr.get("src_wikipedia") or "").upper() == "MISS"
        and int(float(fr.get("n_wikipedia") or 0)) == 0
    )
    print(f"wiki MISS with 0 HTTP (cooldown skip)={cool}")

    print("\n=== SITES CSV ===")
    if sites_p.exists():
        for row in csv.DictReader(sites_p.open(encoding="utf-8-sig")):
            print(dict(row))

    print("\n=== SLOWEST REQUESTS ===")
    slow = sorted(rows, key=lambda r: float(r.get("duration_ms") or 0), reverse=True)[:10]
    for r in slow:
        print(
            f"  {float(r.get('duration_ms') or 0):.0f}ms {r.get('site')} "
            f"sc={r.get('status_code')} {(r.get('url') or '')[:100]}"
        )

    # LB 404 then 200 pattern waste
    waste_404 = sum(1 for r in rows if r.get("site") == "letterboxd" and str(r.get("status_code")) == "404")
    print(f"\nletterboxd 404s (soft): {waste_404}")

    # Wall vs query overhead
    if films and qms and wms:
        overhead = sum(wms) - sum(qms)
        print(f"delay/retry overhead wall-query: {overhead/1000:.1f}s ({100*overhead/sum(wms):.0f}% of wall)")


if __name__ == "__main__":
    main()

"""Compare finish RPM early vs late in spisok02_hdd run."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean, median

PLAN = Path("spisok02_hdd/gather_v2/request_plan.jsonl")


def parse_fa(fa: str):
    if not fa:
        return None
    core = fa.split("+")[0].strip().split("Z")[0].strip()
    for fmt, n in (("%Y-%m-%d %H:%M:%S.%f", 26), ("%Y-%m-%d %H:%M:%S", 19)):
        try:
            return datetime.strptime(core[:n], fmt)
        except ValueError:
            continue
    return None


def main() -> None:
    times: list[tuple[datetime, str, str, float | None, object]] = []
    n429 = 0
    deferred_by_hour: dict[str, Counter] = defaultdict(Counter)
    status_by_hour: dict[str, Counter] = defaultdict(Counter)
    dur_by_hour: dict[str, list[float]] = defaultdict(list)

    with PLAN.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            fa = parse_fa(r.get("finished_at") or "")
            if not fa:
                continue
            site = r.get("site") or "?"
            st = r.get("status") or "?"
            hs = r.get("http_status")
            if hs == 429 or hs == "429":
                n429 += 1
            d = r.get("duration_ms")
            dval = None
            try:
                if d is not None:
                    dval = float(d)
            except (TypeError, ValueError):
                pass
            times.append((fa, site, st, dval, hs))
            hour = fa.strftime("%Y-%m-%d %H:00")
            status_by_hour[hour][st] += 1
            if st == "deferred":
                deferred_by_hour[hour][f"{site}:{(r.get('deferred_reason') or '')[:30]}"] += 1
            if dval is not None and st in ("success", "failed", "skipped", "deferred"):
                dur_by_hour[f"{hour}|{site}"].append(dval)

    times.sort()
    print(f"finished_with_timestamp={len(times)}  http_429_in_those={n429}")
    if not times:
        return
    t0, t1 = times[0][0], times[-1][0]
    print(f"first_finish={t0}  last_finish={t1}  span_h={(t1-t0).total_seconds()/3600:.2f}")

    # RPM in successive 30-min buckets
    print("\n=== Overall finish RPM per 30-min bucket ===")
    bucket = timedelta(minutes=30)
    start = t0.replace(minute=(t0.minute // 30) * 30, second=0, microsecond=0)
    end = t1
    cur = start
    while cur <= end:
        nxt = cur + bucket
        win = [t for t in times if cur <= t[0] < nxt]
        by = Counter(t[1] for t in win)
        mins = 30
        print(
            f"{cur.strftime('%H:%M')}-{nxt.strftime('%H:%M')}  "
            f"n={len(win):5d}  rpm={len(win)/mins:6.1f}  "
            f"tmdb={by.get('tmdb',0):4d} omdb={by.get('omdb',0):4d} "
            f"kp={by.get('kinopoisk',0):4d} lb={by.get('letterboxd',0):4d} "
            f"wiki={by.get('wikipedia',0):4d}"
        )
        cur = nxt

    # First hour vs last hour
    print("\n=== First 60 min vs last 60 min ===")
    for label, win in (
        ("FIRST_60m", [t for t in times if t[0] < t0 + timedelta(hours=1)]),
        ("LAST_60m", [t for t in times if t[0] >= t1 - timedelta(hours=1)]),
    ):
        by = Counter(t[1] for t in win)
        durs = [t[3] for t in win if t[3] is not None]
        print(
            f"{label}: n={len(win)} rpm={len(win)/60:.1f} by_site={dict(by)} "
            f"median_ms={median(durs) if durs else 'n/a'} mean_ms={mean(durs) if durs else 'n/a'}"
        )

    print("\n=== Deferred reasons by hour (top) ===")
    for hour in sorted(deferred_by_hour)[-8:]:
        print(hour, deferred_by_hour[hour].most_common(5))

    print("\n=== Duration median by hour (all sites with data) ===")
    hours = sorted({k.split("|")[0] for k in dur_by_hour})
    for hour in hours:
        parts = []
        for site in ("tmdb", "omdb", "kinopoisk", "letterboxd", "wikipedia"):
            xs = dur_by_hour.get(f"{hour}|{site}") or []
            if xs:
                parts.append(f"{site[:4]}={median(xs):.0f}ms(n={len(xs)})")
        if parts:
            print(hour, " | ".join(parts))


if __name__ == "__main__":
    main()

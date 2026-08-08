#!/usr/bin/env python3
"""Quick DNS + HTTP speed diagnostics and RPM cap check."""

from __future__ import annotations

import socket
import time
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from psychofilm_analyzer.config import load_config
from psychofilm_analyzer.utils.http import HostRateLimiter, HttpClient


def dns_probe(host: str) -> tuple[bool, float, str]:
    t0 = time.perf_counter()
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        ips = sorted({i[4][0] for i in infos})[:3]
        ms = (time.perf_counter() - t0) * 1000
        return True, ms, f"ips={ips}"
    except Exception as exc:  # noqa: BLE001
        ms = (time.perf_counter() - t0) * 1000
        return False, ms, f"{type(exc).__name__}: {exc}"


def main() -> int:
    cfg = load_config()
    http_cfg = cfg.get("http") or {}
    keys = cfg.get("api_keys") or {}

    print("=== CONFIG ===")
    print("global delay_sec:", http_cfg.get("delay_sec"))
    print("timeout_sec:", http_cfg.get("timeout_sec"))
    print("max_retries:", http_cfg.get("max_retries"))
    print("host_rate_limits_per_min:", http_cfg.get("host_rate_limits_per_min"))
    print(
        "keys: tmdb=",
        bool(keys.get("tmdb")),
        "omdb=",
        bool(keys.get("omdb")),
        "kinopoisk=",
        bool(keys.get("kinopoisk")),
    )

    hosts = [
        "api.themoviedb.org",
        "www.omdbapi.com",
        "kinopoiskapiunofficial.tech",
        "en.wikipedia.org",
        "ru.wikipedia.org",
    ]
    print("\n=== DNS PROBE ===")
    for h in hosts:
        ok, ms, detail = dns_probe(h)
        status = "OK  " if ok else "FAIL"
        print(f"  {status} {h:35s} {ms:7.0f} ms  {detail}")

    client = HttpClient(
        delay_sec=float(http_cfg.get("delay_sec", 0.25)),
        timeout_sec=float(http_cfg.get("timeout_sec", 20)),
        max_retries=2,
        user_agent=http_cfg.get("user_agent", "PsychoFilmAnalyzer/1.0"),
        host_rate_limits_per_min=http_cfg.get("host_rate_limits_per_min"),
    )

    print("\n=== HTTP PROBES (live) ===")
    probes: list[tuple[str, str, dict | None, dict | None]] = []
    if keys.get("tmdb"):
        probes.append(
            (
                "tmdb",
                "https://api.themoviedb.org/3/configuration",
                {"api_key": keys["tmdb"]},
                None,
            )
        )
    if keys.get("omdb"):
        probes.append(
            (
                "omdb",
                "https://www.omdbapi.com/",
                {"apikey": keys["omdb"], "t": "Inception", "y": "2010"},
                None,
            )
        )
    if keys.get("kinopoisk"):
        probes.append(
            (
                "kinopoisk",
                "https://kinopoiskapiunofficial.tech/api/v2.2/films/301",
                None,
                {
                    "X-API-KEY": keys["kinopoisk"],
                    "Content-Type": "application/json",
                },
            )
        )
    probes.append(
        (
            "wikipedia_en",
            "https://en.wikipedia.org/api/rest_v1/page/summary/Inception",
            None,
            None,
        )
    )
    probes.append(
        (
            "wikipedia_ru",
            "https://ru.wikipedia.org/api/rest_v1/page/summary/Inception",
            None,
            None,
        )
    )

    for name, url, params, headers in probes:
        t0 = time.perf_counter()
        try:
            if headers:
                client._throttle(url)
                resp = client.session.get(
                    url, params=params, headers=headers, timeout=client.timeout
                )
                client._last_request = time.monotonic()
                ms = (time.perf_counter() - t0) * 1000
                ok = resp.status_code < 400
                print(
                    f"  {'OK' if ok else 'FAIL':4s} {name:14s} {ms:7.0f} ms  "
                    f"status={resp.status_code} bytes={len(resp.content)}"
                )
            else:
                data = client.get(url, params=params)
                ms = (time.perf_counter() - t0) * 1000
                size = len(str(data)) if data is not None else 0
                print(f"  OK   {name:14s} {ms:7.0f} ms  payload~{size} chars")
        except Exception as exc:  # noqa: BLE001
            ms = (time.perf_counter() - t0) * 1000
            print(f"  FAIL {name:14s} {ms:7.0f} ms  {type(exc).__name__}: {exc}")

    print("\n=== REQUEST STATS (this diagnostic) ===")
    snap = client.stats_snapshot()
    for h, st in snap["hosts"].items():
        print(
            f"  {h}: ok={st['ok']} fail={st['fail']} n={st['n']} "
            f"avg_ms={st['latency_ms_avg']} max_ms={st['latency_ms_max']}"
        )
    print("RPM caps:", snap["host_rpm"])

    print("\n=== PER-FILM REQUEST MAP (gather, no letterboxd, cold cache) ===")
    print(
        """
  TMDB
    GET /3/search/movie|tv          x1–3 (title/year variants)
    GET /3/movie|tv/{id} EN append  x1  (credits+keywords+external_ids)
    GET /3/movie|tv/{id} RU         x1
  OMDb
    GET /?i=tt... or t=title        x1
  Kinopoisk  [capped 30 req/min]
    GET search-by-keyword           x1–2
    GET /v2.2/films/{id}            x1
    GET /v1/staff                   x1
    GET keywords                    x1 (often soft-fail 400)
  Wikipedia  [capped 25 req/min shared en+ru]
    REST page/summary candidates    x0–3 EN + x0–3 RU
    optional search API             x0–1 per lang
  TOTAL cold: ~10–18 HTTP GETs / film
  With disk cache: often 0–3 live calls / film

  Global delay_sec between ANY requests + host RPM caps above.
"""
    )

    # Limiter self-test (small limit)
    lim = HostRateLimiter({"wikipedia.org": 100})
    for _ in range(3):
        lim.wait_if_needed("https://en.wikipedia.org/wiki/Test")
    print("Limiter OK, snapshot:", lim.snapshot())
    print("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

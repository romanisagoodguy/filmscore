"""Full connectivity check after hosts-file fix."""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from psychofilm_analyzer.config import load_config
from psychofilm_analyzer.utils.http import HttpClient


def resolve(host: str) -> str:
    try:
        return socket.gethostbyname(host)
    except OSError as exc:
        return f"FAIL {exc}"


def main() -> int:
    print("=== DNS resolution ===")
    for host in [
        "api.themoviedb.org",
        "www.themoviedb.org",
        "themoviedb.org",
        "image.tmdb.org",
        "www.omdbapi.com",
        "kinopoiskapiunofficial.tech",
        "en.wikipedia.org",
    ]:
        ip = resolve(host)
        flag = ""
        if ip.startswith("127.") or ip in {"::1", "0.0.0.0"}:
            flag = "  << LOOPBACK / BLOCKED"
        print(f"  {host:35s} -> {ip}{flag}")

    cfg = load_config()
    keys = cfg["api_keys"]
    print(
        "\n=== API keys loaded ===\n"
        f"  TMDB={'yes' if keys.get('tmdb') else 'no'}  "
        f"OMDb={'yes' if keys.get('omdb') else 'no'}  "
        f"Kinopoisk={'yes' if keys.get('kinopoisk') else 'no'}"
    )

    rows: list[tuple[str, str, str]] = []

    # TMDB direct
    try:
        r = requests.get(
            "https://api.themoviedb.org/3/configuration",
            params={"api_key": keys["tmdb"]},
            timeout=20,
        )
        rows.append(("TMDB config", str(r.status_code), "OK" if r.status_code == 200 else r.text[:100]))
    except Exception as exc:  # noqa: BLE001
        rows.append(("TMDB config", "ERR", str(exc)[:140]))

    try:
        r = requests.get(
            "https://api.themoviedb.org/3/search/movie",
            params={"api_key": keys["tmdb"], "query": "Persona", "year": 1966},
            timeout=20,
        )
        data = r.json() if r.ok else {}
        results = data.get("results") or []
        title = results[0].get("title") if results else None
        rows.append(("TMDB search", str(r.status_code), f"n={len(results)} first={title}"))
    except Exception as exc:  # noqa: BLE001
        rows.append(("TMDB search", "ERR", str(exc)[:140]))

    # OMDb
    try:
        r = requests.get(
            "https://www.omdbapi.com/",
            params={"apikey": keys["omdb"], "t": "Persona", "y": "1966", "plot": "full"},
            timeout=20,
        )
        j = r.json()
        awards = str(j.get("Awards") or "")[:50]
        rows.append(("OMDb", str(r.status_code), f"{j.get('Title')} ({j.get('Year')}) awards={awards}"))
    except Exception as exc:  # noqa: BLE001
        rows.append(("OMDb", "ERR", str(exc)[:140]))

    # Kinopoisk
    try:
        r = requests.get(
            "https://kinopoiskapiunofficial.tech/api/v2.1/films/search-by-keyword",
            params={"keyword": "Persona", "page": 1},
            headers={"X-API-KEY": keys["kinopoisk"]},
            timeout=20,
        )
        films = (r.json() or {}).get("films") or []
        first = None
        if films:
            first = films[0].get("nameEn") or films[0].get("nameRu")
        rows.append(("Kinopoisk", str(r.status_code), f"n={len(films)} first={first}"))
    except Exception as exc:  # noqa: BLE001
        rows.append(("Kinopoisk", "ERR", str(exc)[:140]))

    # Wikipedia
    try:
        r = requests.get(
            "https://en.wikipedia.org/api/rest_v1/page/summary/Persona_(1966_film)",
            headers={"User-Agent": "PsychoFilmAnalyzer/1.0"},
            timeout=20,
        )
        j = r.json() if r.ok else {}
        rows.append(("Wikipedia", str(r.status_code), str(j.get("title"))))
    except Exception as exc:  # noqa: BLE001
        rows.append(("Wikipedia", "ERR", str(exc)[:140]))

    # HttpClient (app path)
    http = HttpClient(delay_sec=0.2, max_retries=2)
    print(f"\n=== HttpClient ===\n  tmdb_bypass_ip={http.tmdb_bypass_ip}")
    try:
        data = http.get(
            "https://api.themoviedb.org/3/search/movie",
            params={"api_key": keys["tmdb"], "query": "Mulholland Drive", "year": 2001},
        )
        title = ((data or {}).get("results") or [{}])[0].get("title")
        rows.append(("HttpClient TMDB", "OK", f"title={title}"))
    except Exception as exc:  # noqa: BLE001
        rows.append(("HttpClient TMDB", "ERR", str(exc)[:140]))

    print("\n=== Endpoint checks ===")
    ok = 0
    for name, status, detail in rows:
        good = status in {"200", "OK"}
        if good:
            ok += 1
        mark = "PASS" if good else "FAIL"
        print(f"  [{mark}] {name:18s} {status:5s}  {detail}")

    print(f"\nSummary: {ok}/{len(rows)} checks passed")
    return 0 if ok >= 5 else 1


if __name__ == "__main__":
    raise SystemExit(main())

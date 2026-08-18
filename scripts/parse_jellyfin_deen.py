#!/usr/bin/env python3
"""Parse the Jellyfin library dump into a gather-ready film list."""

from __future__ import annotations

import csv
import re
from pathlib import Path

SRC = Path(r"C:\Scripts\Grk\hdhdd.ru\jellyfin deen\films jellyfin deen.txt")
OUT_DIR = Path(r"C:\Scripts\Grk\hdhdd.ru\jellyfin deen")
OUT_CSV = OUT_DIR / "films_jellyfin_deen.csv"
OUT_TXT = OUT_DIR / "films_jellyfin_deen_list.txt"

YEAR_RE = re.compile(r"^(19|20)\d{2}$")
PAGE_RE = re.compile(r"^[\d.]+-[\d.]+ von [\d.]+$")
# Only UI chrome — single letters (X, M, Z) can be real film titles.
CHROME_TITLES = {"medien", "benutzer", "movies", "#"}

EXPECTED = 2783


def is_year(s: str) -> bool:
    return bool(YEAR_RE.fullmatch(s))


def is_chrome_title(s: str) -> bool:
    t = s.strip().lower()
    if t in CHROME_TITLES:
        return True
    if len(s) == 1 and s.isalpha():
        # A–Z filter row is chrome unless later paired with a year.
        # Treated as unsafe pending (overwritten by next letter / title).
        return True
    return False


def parse_pages(text: str) -> list[str]:
    pages: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if PAGE_RE.fullmatch(s):
            pages.append(s)
    return pages


def parse_films(text: str) -> tuple[list[tuple[str, int]], list[str]]:
    films: list[tuple[str, int]] = []
    leftovers: list[str] = []
    pending: str | None = None
    pending_is_safe = False

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if PAGE_RE.fullmatch(line):
            if pending and pending_is_safe:
                leftovers.append(f"UNPAIRED_BEFORE_PAGE: {pending}")
            pending = None
            pending_is_safe = False
            continue
        if is_year(line):
            year = int(line)
            if pending and pending_is_safe:
                films.append((pending, year))
                pending = None
                pending_is_safe = False
            elif pending and len(pending) == 1 and pending.isalpha():
                # Real one-letter title (e.g. X, 2022)
                films.append((pending, year))
                pending = None
                pending_is_safe = False
            else:
                # Year-as-title (1917, 1992, 2012) after nav chrome
                pending = line
                pending_is_safe = True
            continue
        if pending and pending_is_safe and pending.isdigit() and len(pending) <= 2:
            # Split display: "3" + "Fear Street: 1666" → Fear Street Part 3: 1666
            if "part" not in line.lower():
                merged = f"{line.split(':', 1)[0].strip()} Part {pending}: {line.split(':', 1)[-1].strip()}" if ":" in line else f"{line} {pending}"
            else:
                merged = f"{line} {pending}"
            leftovers.append(f"MERGED_SPLIT_TITLE: {pending!r} + {line!r} -> {merged!r}")
            pending = merged
            pending_is_safe = True
            continue
        if pending and pending_is_safe:
            leftovers.append(f"TITLE_OVERWRITE: {pending} -> {line}")
        pending = line
        pending_is_safe = not is_chrome_title(line)

    if pending and pending_is_safe:
        leftovers.append(f"TRAILING_UNPAIRED: {pending}")

    return films, leftovers


def main() -> int:
    raw = SRC.read_text(encoding="utf-8-sig")
    films, leftovers = parse_films(raw)
    pages = parse_pages(raw)

    unique = {(t.lower(), y) for t, y in films}
    years = [y for _, y in films]

    print(f"source: {SRC}")
    print(f"parsed films: {len(films)}")
    print(f"expected:     {EXPECTED}")
    print(f"unique (title,year): {len(unique)}")
    print(f"page markers: {len(pages)}")
    if years:
        print(f"year range: {min(years)}–{max(years)}")
    print(f"leftovers: {len(leftovers)}")
    for item in leftovers[:30]:
        print(f"  leftover: {item}")

    # Keep library order (newest-first as in the dump)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["title", "english_title", "year", "type"])
        for title, year in films:
            w.writerow([title, title, year, "film"])

    with OUT_TXT.open("w", encoding="utf-8") as fh:
        fh.write(f"# Jellyfin Deen film list — {len(films)} titles\n")
        fh.write("# title\\tyear\n")
        for title, year in films:
            fh.write(f"{title}\t{year}\n")

    # XLSX via pandas (already a project dependency)
    try:
        import pandas as pd

        df = pd.DataFrame(
            {
                "title": [t for t, _ in films],
                "english_title": [t for t, _ in films],
                "year": [y for _, y in films],
                "type": ["film"] * len(films),
            }
        )
        xlsx = OUT_DIR / "films_jellyfin_deen.xlsx"
        df.to_excel(xlsx, index=False)
        print(f"wrote: {xlsx}")
    except Exception as exc:
        print(f"xlsx skipped: {exc}")

    print(f"wrote: {OUT_CSV}")
    print(f"wrote: {OUT_TXT}")

    if len(films) != EXPECTED:
        print(f"WARNING: count {len(films)} != expected {EXPECTED}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

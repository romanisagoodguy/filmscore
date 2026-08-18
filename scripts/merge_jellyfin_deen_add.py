#!/usr/bin/env python3
"""Append page 501-600 (film jelly add more.txt) to the Jellyfin Deen list."""

from __future__ import annotations

import csv
import importlib.util
from pathlib import Path

ROOT = Path(r"C:\Scripts\Grk\hdhdd.ru")
DEEN = ROOT / "jellyfin deen"
ADD_SRC = DEEN / "film jelly add more.txt"
MASTER_CSV = DEEN / "films_jellyfin_deen.csv"
ADD_CSV = DEEN / "films_jellyfin_deen_add.csv"
CATALOG_CSV = DEEN / "films_jellyfin_deen_catalog.csv"
LIST_TXT = DEEN / "films_jellyfin_deen_list.txt"
REPORT = DEEN / "PARSE_REPORT.txt"

ANCHOR_AFTER = ("Lara Croft: Tomb Raider - The Cradle of Life", 2003)


def _load_parser():
    spec = importlib.util.spec_from_file_location(
        "parse_jellyfin_deen", ROOT / "scripts" / "parse_jellyfin_deen.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_csv(path: Path, films: list[tuple[str, int]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["title", "english_title", "year", "type"])
        for title, year in films:
            w.writerow([title, title, year, "film"])


def _write_xlsx(path: Path, films: list[tuple[str, int]]) -> None:
    import pandas as pd

    pd.DataFrame(
        {
            "title": [t for t, _ in films],
            "english_title": [t for t, _ in films],
            "year": [y for _, y in films],
            "type": ["film"] * len(films),
        }
    ).to_excel(path, index=False)


def main() -> int:
    parser = _load_parser()
    add_films, leftovers = parser.parse_films(ADD_SRC.read_text(encoding="utf-8-sig"))
    if leftovers:
        print("add-more leftovers:")
        for item in leftovers:
            print(f"  {item}")
    if len(add_films) != 100:
        print(f"WARNING: expected 100 add-more films, got {len(add_films)}")

    existing: list[tuple[str, int]] = []
    with MASTER_CSV.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            existing.append((row["title"], int(row["year"])))

    exist_keys = {(t.lower(), y) for t, y in existing}
    new_only = [(t, y) for t, y in add_films if (t.lower(), y) not in exist_keys]
    print(f"existing: {len(existing)}")
    print(f"add-more parsed: {len(add_films)}")
    print(f"add-more new: {len(new_only)}")

    # Gather-stable list: keep original 2683 rows, append the 100.
    gather_films = existing + new_only

    # Human catalog order: insert page 501-600 after the 601-700 tail.
    catalog = list(existing)
    insert_at = None
    for i, pair in enumerate(catalog):
        if pair == ANCHOR_AFTER:
            insert_at = i + 1
            break
    if insert_at is None:
        catalog.extend(new_only)
        print("WARNING: catalog anchor not found; appended add-more at end")
    else:
        catalog[insert_at:insert_at] = new_only
        print(f"catalog insert at index {insert_at} (after {ANCHOR_AFTER})")

    _write_csv(MASTER_CSV, gather_films)
    _write_csv(ADD_CSV, new_only)
    _write_csv(CATALOG_CSV, catalog)
    _write_xlsx(DEEN / "films_jellyfin_deen.xlsx", gather_films)
    _write_xlsx(DEEN / "films_jellyfin_deen_add.xlsx", new_only)
    _write_xlsx(DEEN / "films_jellyfin_deen_catalog.xlsx", catalog)

    with LIST_TXT.open("w", encoding="utf-8") as fh:
        fh.write(f"# Jellyfin Deen film list — {len(gather_films)} titles\n")
        fh.write("# first 2683 = original dump (row keys stable for gather inherit)\n")
        fh.write("# last 100 = page 501-600 from film jelly add more.txt\n")
        fh.write("# title\\tyear\n")
        for title, year in gather_films:
            fh.write(f"{title}\t{year}\n")

    report = f"""Jellyfin Deen dump parse
source 1: films jellyfin deen.txt
source 2: film jelly add more.txt  (page 501-600 von 2.783)
list:     films_jellyfin_deen.csv / .xlsx / _list.txt
add-only: films_jellyfin_deen_add.csv / .xlsx
catalog:  films_jellyfin_deen_catalog.csv / .xlsx  (page order)

Jellyfin footer said 2.783 films.
Original dump: 2683 title+year pairs (page 501-600 was missing).
Add-more page: {len(add_films)} films, {len(new_only)} new (overlap 0).
Combined gather list: {len(gather_films)}
Catalog-order list:   {len(catalog)}
Year range add-more: 1999–2001

Gather CSV keeps the original 2683 rows first so Approach 3 inherit
keys stay stable. The 100 new titles are appended (rows 2685–2784).
"""
    REPORT.write_text(report, encoding="utf-8")
    print(f"wrote {MASTER_CSV} ({len(gather_films)} films)")
    print(f"wrote {ADD_CSV} ({len(new_only)} films)")
    print(f"wrote {CATALOG_CSV} ({len(catalog)} films)")
    print(f"wrote {REPORT}")
    return 0 if len(gather_films) == 2783 else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Rebuild jellyfin_deen_hdd profiles from existing gather responses (no HTTP).

The add-more batch reused film_index 1–100, so original titles 1–100 no longer
have wiki response files. Those 100 keep their existing TMDB/OMDb/KP/LB
profiles; titles 101–2683 and the 100 add-more films are reassembled with
wiki-resolve unwrap (EN/RU/DE plot bags).
"""

from __future__ import annotations

import json
from pathlib import Path

from psychofilm_analyzer.enrichment.export import write_profile_dicts
from psychofilm_analyzer.gather_v2.assemble import assemble_profiles
from psychofilm_analyzer.gather_v2.plan_store import PlanStore
from psychofilm_analyzer.io.input_loader import load_titles

CSV = Path(r"C:\Scripts\Grk\hdhdd.ru\jellyfin deen\films_jellyfin_deen.csv")
OUT = Path(r"C:\Scripts\Grk\hdhdd.ru\jellyfin_deen_hdd")
PLAN = OUT / "gather_v2"
LIVE = OUT / "profile_a2_live.json"
N_ORIGINAL = 2683
N_ADD = 100


def _load_live() -> list[dict]:
    data = json.loads(LIVE.read_text(encoding="utf-8"))
    rows = data["profiles"] if isinstance(data, dict) and "profiles" in data else data
    if len(rows) != N_ORIGINAL + N_ADD:
        raise SystemExit(f"expected {N_ORIGINAL + N_ADD} live profiles, got {len(rows)}")
    return rows


def main() -> int:
    items = load_titles(CSV)
    print(f"loaded {len(items)} titles from {CSV}")
    if len(items) != N_ORIGINAL + N_ADD:
        raise SystemExit(f"expected {N_ORIGINAL + N_ADD} catalog titles, got {len(items)}")

    store = PlanStore(PLAN)
    n = store.load()
    print(f"plan loaded {n} requests from {PLAN}")

    live = _load_live()
    # Original 1–100: keep existing (wiki responses overwritten by add-more)
    head = [dict(p) for p in live[:100]]
    print(f"kept first 100 live profiles (wiki responses overwritten)")

    all_sites = {"tmdb", "omdb", "kinopoisk", "letterboxd", "wikipedia"}
    tail_orig = assemble_profiles(
        items[100:N_ORIGINAL],
        store,
        film_indices=list(range(101, N_ORIGINAL + 1)),
        force_sites=all_sites,
    )
    print(f"reassembled original 101–{N_ORIGINAL}: {len(tail_orig)}")

    added = assemble_profiles(
        items[N_ORIGINAL:],
        store,
        film_indices=list(range(1, N_ADD + 1)),
    )
    print(f"reassembled add-more 1–{N_ADD}: {len(added)}")

    profiles = head + tail_orig + added
    if len(profiles) != len(items):
        raise SystemExit(f"merge size mismatch {len(profiles)} != {len(items)}")

    ckpt = PLAN / "gather_v2_checkpoint.jsonl"
    with ckpt.open("w", encoding="utf-8") as fh:
        for p in profiles:
            fh.write(json.dumps(p, ensure_ascii=False) + "\n")

    written = write_profile_dicts(
        profiles,
        output_dir=str(OUT),
        prefix="profile_a2",
        write_excel=len(profiles) <= 15000,
    )
    wiki = de = bags_de = 0
    for p in profiles:
        cov = p.get("coverage") or {}
        if cov.get("wikipedia"):
            wiki += 1
        if cov.get("has_plot_de"):
            de += 1
        if any(b.get("name") == "plot_de" for b in (p.get("evidence_bags") or [])):
            bags_de += 1
    print(f"wrote {ckpt} rows={len(profiles)}")
    print(f"wikipedia found={wiki}  has_plot_de={de}  plot_de bags={bags_de}")
    for k, v in written.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

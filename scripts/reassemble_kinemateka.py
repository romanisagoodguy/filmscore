#!/usr/bin/env python3
"""Rebuild kinemateka_hdd profiles from existing gather responses (no HTTP)."""

from __future__ import annotations

import json
from pathlib import Path

from psychofilm_analyzer.enrichment.export import write_profile_dicts
from psychofilm_analyzer.gather_v2.assemble import assemble_profiles
from psychofilm_analyzer.gather_v2.plan_store import PlanStore
from psychofilm_analyzer.io.input_loader import load_titles

CSV = Path(r"C:\Scripts\Grk\hdhdd.ru\kinemateka\media_occurrences_en.csv")
PLAN_DIR = Path(r"C:\Users\Roman\.grok\worktrees\grk-hdhddru\kinemateka\kinemateka_hdd\gather_v2")
OUT_DIR = Path(r"C:\Users\Roman\.grok\worktrees\grk-hdhddru\kinemateka\kinemateka_hdd")


def main() -> int:
    items = load_titles(CSV)
    print(f"loaded {len(items)} titles from {CSV}")
    store = PlanStore(PLAN_DIR)
    n = store.load()
    print(f"plan loaded {n} requests from {PLAN_DIR}")
    profiles = assemble_profiles(items, store, film_indices=list(range(1, len(items) + 1)))
    ckpt = PLAN_DIR / "gather_v2_checkpoint.jsonl"
    with ckpt.open("w", encoding="utf-8") as fh:
        for p in profiles:
            fh.write(json.dumps(p, ensure_ascii=False) + "\n")
    print(f"wrote {ckpt} rows={len(profiles)}")
    written = write_profile_dicts(
        profiles,
        output_dir=str(OUT_DIR),
        prefix="profile_a2",
        write_excel=len(profiles) <= 15000,
    )
    wiki = 0
    de = 0
    bags_de = 0
    for p in profiles:
        cov = p.get("coverage") or {}
        if cov.get("wikipedia"):
            wiki += 1
        if cov.get("has_plot_de"):
            de += 1
        for b in p.get("evidence_bags") or []:
            if b.get("name") == "plot_de":
                bags_de += 1
                break
        src = (p.get("sources") or {}).get("wikipedia") or {}
        extra = src.get("extra") or {}
        if extra.get("overview_de") or "de" in (extra.get("langs") or []):
            pass
    print(f"wikipedia found={wiki}  has_plot_de={de}  plot_de bags={bags_de}")
    for k, v in written.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

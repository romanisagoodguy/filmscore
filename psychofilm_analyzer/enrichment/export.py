"""Export gather-only enrichment profiles."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import pandas as pd

from psychofilm_analyzer.enrichment.profile import EnrichmentProfile

PROFILE_COLUMN_ORDER = [
    "rank",
    "imported_file",
    "imported_sheet",
    "imported_row",
    "imported_title",
    "imported_year",
    "film_uid",
    "input_id",
    "imdb_id",
    "tmdb_id",
    "kinopoisk_id",
    "title_en",
    "title_ru",
    "title_original",
    "year",
    "media_type",
    "content_type",
    "season",
    "runtime_min",
    "countries_en",
    "countries_ru",
    "plot_en",
    "plot_ru",
    "overview_en",
    "overview_ru",
    "genres",
    "genres_en",
    "genres_ru",
    "keywords",
    "keywords_count",
    "directors_en",
    "directors_ru",
    "writers_en",
    "writers_ru",
    "composers_en",
    "composers_ru",
    "actors_en",
    "actors_ru",
    "imdb_rating",
    "kinopoisk_rating",
    "tmdb_rating",
    "awards_text",
    "link_imdb",
    "link_tmdb",
    "link_kinopoisk",
    "link_wikipedia_en",
    "link_wikipedia_ru",
    "link_wikipedia_de",
    "link_letterboxd",
    "cov_tmdb",
    "cov_omdb",
    "cov_kinopoisk",
    "cov_wikipedia",
    "cov_letterboxd",
    "cov_plot_en",
    "cov_plot_ru",
    "cov_keywords_n",
    "cov_bags",
    "cov_sources_found",
    "flag_spectacle",
    "flag_arthouse",
    "flag_animation",
    "flag_documentary",
    "flag_series",
    "bag_plot_en_preview",
    "bag_plot_ru_preview",
    "bag_keywords_en_preview",
    "error",
]


def _ordered(df: pd.DataFrame) -> pd.DataFrame:
    preferred = [c for c in PROFILE_COLUMN_ORDER if c in df.columns]
    extras = [c for c in df.columns if c not in preferred]
    return df[preferred + extras]


def write_profiles(
    profiles: Iterable[EnrichmentProfile],
    output_dir: str | Path = "output",
    *,
    prefix: str = "profile",
) -> dict[str, Path]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    profiles = list(profiles)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    written: dict[str, Path] = {}

    rows = [p.to_flat_dict() for p in profiles]
    df = _ordered(pd.DataFrame(rows))
    if not df.empty:
        df.insert(0, "rank", range(1, len(df) + 1))

    xlsx = output_dir / f"{prefix}_{stamp}.xlsx"
    with pd.ExcelWriter(xlsx, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="profiles", index=False)
        # Coverage sheet
        cov_cols = [c for c in df.columns if c.startswith("cov_") or c.startswith("flag_") or c in {
            "rank", "imported_title", "title_en", "film_uid", "year"
        }]
        if cov_cols:
            df[cov_cols].to_excel(writer, sheet_name="coverage", index=False)
        # Identity + links
        id_cols = [
            c
            for c in [
                "rank",
                "imported_file",
                "imported_sheet",
                "imported_row",
                "imported_title",
                "imported_year",
                "film_uid",
                "imdb_id",
                "tmdb_id",
                "kinopoisk_id",
                "title_en",
                "title_ru",
                "link_imdb",
                "link_tmdb",
                "link_kinopoisk",
                "link_wikipedia_en",
                "link_wikipedia_ru",
            ]
            if c in df.columns
        ]
        if id_cols:
            df[id_cols].to_excel(writer, sheet_name="identity_links", index=False)
        # Evidence bag index
        bag_rows = []
        for p in profiles:
            for b in p.bags:
                bag_rows.append(
                    {
                        "film_uid": p.film_uid,
                        "imported_title": p.input.import_title or p.input.title,
                        "title_en": p.title_en,
                        "bag": b.name,
                        "source": b.source,
                        "language": b.language,
                        "weight": b.weight,
                        "chars": b.char_len,
                        "words": b.word_count,
                        "preview": (b.text or "")[:400],
                    }
                )
        if bag_rows:
            pd.DataFrame(bag_rows).to_excel(writer, sheet_name="evidence_bags", index=False)
    written["excel"] = xlsx

    js = output_dir / f"{prefix}_{stamp}.json"
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "gather_only",
        "count": len(profiles),
        "note": "No psych scores or clusters. Review evidence bags before scoring.",
        "profiles": [p.to_json_dict() for p in profiles],
    }
    js.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    written["json"] = js

    latest = output_dir / f"{prefix}_latest.json"
    latest.write_text(
        json.dumps([p.to_flat_dict() for p in profiles], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    written["latest"] = latest
    return written

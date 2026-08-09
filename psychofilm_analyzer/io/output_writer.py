"""Write Excel, JSON, and Markdown outputs."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import pandas as pd

from psychofilm_analyzer.models import EnrichedResult
from psychofilm_analyzer.utils.localtime import now_str, stamp_local

# Preferred Excel column order: import provenance first, then enrichment
EXCEL_COLUMN_ORDER = [
    "rank",
    # from the imported file (imported_ prefix)
    "imported_file",
    "imported_sheet",
    "imported_row",
    "imported_title",
    "imported_year",
    # then all enrichment columns
    "film_uid",
    "input_id",
    "imdb_id",
    "tmdb_id",
    "kinopoisk_id",
    "title_en",
    "title_ru",
    "title_original",
    "year",
    "season",
    "media_type",
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
    "link_imdb",
    "link_tmdb",
    "link_kinopoisk",
    "link_wikipedia_en",
    "link_wikipedia_ru",
    "link_wikipedia_de",
    "link_letterboxd",
    "awards_text",
    "psycho_score",
    "primary_cluster",
    "secondary_cluster",
    "confidence",
    "description_en",
    "factor_thematic",
    "factor_narrative",
    "factor_awards",
    "factor_discourse",
    "factor_director",
    "factor_discussability",
    "caps_applied",
    "collection",
    "error",
]


def _ordered_dataframe(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    preferred = [c for c in EXCEL_COLUMN_ORDER if c in df.columns]
    extras = [c for c in df.columns if c not in preferred]
    return df[preferred + extras]


def write_outputs(
    results: Iterable[EnrichedResult],
    output_dir: str | Path = "output",
    *,
    excel: bool = True,
    json_out: bool = True,
    markdown_top_n: int = 25,
    markdown_min_score: float = 7.0,
    prefix: str = "psychofilm",
) -> dict[str, Path]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    results = list(results)
    results_sorted = sorted(results, key=lambda r: r.psycho_score, reverse=True)
    stamp = stamp_local()
    written: dict[str, Path] = {}

    if excel:
        path = output_dir / f"{prefix}_{stamp}.xlsx"
        rows = [r.to_flat_dict() for r in results_sorted]
        df = _ordered_dataframe(rows)
        if not df.empty:
            if "rank" in df.columns:
                df = df.drop(columns=["rank"])
            df.insert(0, "rank", range(1, len(df) + 1))
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name="all", index=False)
            top = df[df["psycho_score"] >= markdown_min_score] if not df.empty and "psycho_score" in df.columns else df
            top.to_excel(writer, sheet_name="top_psych", index=False)
            # Identity + links sheet for quick joins
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
                    "year",
                    "link_imdb",
                    "link_tmdb",
                    "link_kinopoisk",
                    "link_wikipedia_en",
                    "link_wikipedia_ru",
                    "link_letterboxd",
                    "psycho_score",
                ]
                if c in df.columns
            ]
            if id_cols:
                df[id_cols].to_excel(writer, sheet_name="identity_links", index=False)
            crew_cols = [
                c
                for c in [
                    "rank",
                    "film_uid",
                    "title_en",
                    "title_ru",
                    "directors_en",
                    "directors_ru",
                    "writers_en",
                    "writers_ru",
                    "composers_en",
                    "composers_ru",
                    "actors_en",
                    "actors_ru",
                ]
                if c in df.columns
            ]
            if crew_cols:
                df[crew_cols].to_excel(writer, sheet_name="crew_cast", index=False)
            factor_cols = [
                c
                for c in df.columns
                if c.startswith("factor_")
                or c
                in {
                    "rank",
                    "film_uid",
                    "title_en",
                    "year",
                    "psycho_score",
                    "primary_cluster",
                    "confidence",
                }
            ]
            if factor_cols:
                df[factor_cols].to_excel(writer, sheet_name="factors", index=False)
        written["excel"] = path

    if json_out:
        path = output_dir / f"{prefix}_{stamp}.json"
        payload = {
            "generated_at": now_str(),
            "count": len(results_sorted),
            "results": [r.to_json_dict() for r in results_sorted],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        written["json"] = path

    if markdown_top_n and markdown_top_n > 0:
        path = output_dir / f"{prefix}_{stamp}_top.md"
        top = [r for r in results_sorted if r.psycho_score >= markdown_min_score][:markdown_top_n]
        if not top:
            top = results_sorted[: min(10, len(results_sorted))]
        lines = [
            "# PsychoFilm Analyzer — Top titles",
            "",
            f"Generated: {now_str()}",
            f"Threshold: score ≥ {markdown_min_score} (showing up to {markdown_top_n})",
            "",
        ]
        for i, r in enumerate(top, 1):
            title = r.title_en or r.input.display_title()
            lines.append(f"## {i}. {title} — **{r.psycho_score:.1f}/10**")
            lines.append("")
            lines.append(f"- **UID:** `{r.film_uid or '—'}`")
            lines.append(f"- **IDs:** IMDb={r.imdb_id or '—'} · TMDB={r.tmdb_id or '—'} · KP={r.kinopoisk_id or '—'}")
            lines.append(f"- **Title RU:** {r.title_ru or '—'}")
            lines.append(f"- **Cluster:** {r.primary_cluster or '—'} / {r.secondary_cluster or '—'}")
            lines.append(f"- **Confidence:** {r.confidence.value}")
            lines.append(f"- **Year:** {r.year or '—'}")
            dirs = r.directors_en or r.directors
            lines.append(f"- **Directors (EN):** {', '.join(dirs) if dirs else '—'}")
            if r.directors_ru:
                lines.append(f"- **Directors (RU):** {', '.join(r.directors_ru)}")
            if r.writers_en:
                lines.append(f"- **Writers (EN):** {', '.join(r.writers_en)}")
            if r.composers_en:
                lines.append(f"- **Composers (EN):** {', '.join(r.composers_en)}")
            if r.composers_ru:
                lines.append(f"- **Composers (RU):** {', '.join(r.composers_ru)}")
            if r.actors_en:
                lines.append(f"- **Actors (EN):** {', '.join(r.actors_en[:8])}")
            if r.actors_ru:
                lines.append(f"- **Actors (RU):** {', '.join(r.actors_ru[:8])}")
            links = [
                ("IMDb", r.link_imdb),
                ("TMDB", r.link_tmdb),
                ("Kinopoisk", r.link_kinopoisk),
                ("Wikipedia EN", r.link_wikipedia_en),
                ("Wikipedia RU", r.link_wikipedia_ru),
                ("Letterboxd", r.link_letterboxd),
            ]
            link_bits = [f"[{name}]({url})" for name, url in links if url]
            if link_bits:
                lines.append(f"- **Links:** {' · '.join(link_bits)}")
            lines.append("")
            lines.append(r.description_en or r.description or "")
            lines.append("")
            lines.append("---")
            lines.append("")
        path.write_text("\n".join(lines), encoding="utf-8")
        written["markdown"] = path

    latest = output_dir / f"{prefix}_latest.json"
    latest.write_text(
        json.dumps([r.to_flat_dict() for r in results_sorted], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    written["latest"] = latest
    return written

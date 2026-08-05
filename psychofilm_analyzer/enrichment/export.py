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


def _join_list(values: list | None, sep: str = "; ") -> str | None:
    if not values:
        return None
    cleaned = [str(v).strip() for v in values if v and str(v).strip()]
    return sep.join(cleaned) if cleaned else None


def profile_dict_to_flat(d: dict) -> dict:
    """Flatten a to_json_dict() profile for Excel/latest export."""
    imported = d.get("imported") or {}
    ids = d.get("ids") or {}
    titles = d.get("titles") or {}
    countries = d.get("countries") or {}
    plots = d.get("plots") or {}
    overviews = d.get("overviews") or {}
    genres = d.get("genres") or {}
    crew = d.get("crew") or {}
    cast = d.get("cast") or {}
    ratings = d.get("ratings") or {}
    links = d.get("links") or {}
    cov = d.get("coverage") or {}
    flags = d.get("type_flags") or {}
    bags = d.get("evidence_bags") or []
    bag_names = sorted({b.get("name") for b in bags if b.get("name")})

    def _bag_preview(name: str, n: int = 300) -> str | None:
        parts = [b.get("text") for b in bags if b.get("name") == name and b.get("text")]
        if not parts:
            return None
        text = " | ".join(str(p) for p in parts)
        return text[:n] + ("…" if len(text) > n else "")

    keywords = d.get("keywords") or []
    return {
        "imported_file": imported.get("file"),
        "imported_sheet": imported.get("sheet"),
        "imported_row": imported.get("row"),
        "imported_title": imported.get("title"),
        "imported_year": imported.get("year"),
        "film_uid": d.get("film_uid"),
        "input_id": ids.get("input_id"),
        "imdb_id": ids.get("imdb_id"),
        "tmdb_id": ids.get("tmdb_id"),
        "kinopoisk_id": ids.get("kinopoisk_id"),
        "title_en": titles.get("en"),
        "title_ru": titles.get("ru"),
        "title_original": titles.get("original"),
        "year": d.get("year"),
        "media_type": d.get("media_type"),
        "content_type": d.get("content_type"),
        "season": d.get("season"),
        "runtime_min": d.get("runtime_min"),
        "countries_en": _join_list(countries.get("en")),
        "countries_ru": _join_list(countries.get("ru")),
        "plot_en": plots.get("en"),
        "plot_ru": plots.get("ru"),
        "overview_en": overviews.get("en"),
        "overview_ru": overviews.get("ru"),
        "genres": _join_list(genres.get("all")),
        "genres_en": _join_list(genres.get("en")),
        "genres_ru": _join_list(genres.get("ru")),
        "keywords": _join_list(keywords[:80] if isinstance(keywords, list) else []),
        "keywords_count": len(keywords) if isinstance(keywords, list) else 0,
        "directors_en": _join_list(crew.get("directors_en")),
        "directors_ru": _join_list(crew.get("directors_ru")),
        "writers_en": _join_list(crew.get("writers_en")),
        "writers_ru": _join_list(crew.get("writers_ru")),
        "composers_en": _join_list(crew.get("composers_en")),
        "composers_ru": _join_list(crew.get("composers_ru")),
        "actors_en": _join_list(cast.get("actors_en")),
        "actors_ru": _join_list(cast.get("actors_ru")),
        "imdb_rating": ratings.get("imdb"),
        "kinopoisk_rating": ratings.get("kinopoisk"),
        "tmdb_rating": ratings.get("tmdb"),
        "awards_text": d.get("awards_text"),
        "link_imdb": links.get("imdb"),
        "link_tmdb": links.get("tmdb"),
        "link_kinopoisk": links.get("kinopoisk"),
        "link_wikipedia_en": links.get("wikipedia_en"),
        "link_wikipedia_ru": links.get("wikipedia_ru"),
        "link_wikipedia_de": links.get("wikipedia_de"),
        "link_letterboxd": links.get("letterboxd"),
        "cov_tmdb": cov.get("tmdb"),
        "cov_omdb": cov.get("omdb"),
        "cov_kinopoisk": cov.get("kinopoisk"),
        "cov_wikipedia": cov.get("wikipedia"),
        "cov_letterboxd": cov.get("letterboxd"),
        "cov_plot_en": cov.get("has_plot_en"),
        "cov_plot_ru": cov.get("has_plot_ru"),
        "cov_keywords_n": cov.get("keywords_n"),
        "cov_bags": "; ".join(bag_names),
        "cov_sources_found": cov.get("sources_found"),
        "flag_spectacle": flags.get("is_spectacle"),
        "flag_arthouse": flags.get("is_arthouse"),
        "flag_animation": flags.get("is_animation"),
        "flag_documentary": flags.get("is_documentary"),
        "flag_series": flags.get("is_series"),
        "error": d.get("error"),
        "bag_plot_en_preview": _bag_preview("plot_en"),
        "bag_plot_ru_preview": _bag_preview("plot_ru"),
        "bag_keywords_en_preview": _bag_preview("keywords_en"),
    }


def write_profile_dicts(
    profile_dicts: list[dict],
    output_dir: str | Path = "output",
    *,
    prefix: str = "profile",
    write_excel: bool = True,
) -> dict[str, Path]:
    """Write gather outputs from profile JSON dicts (checkpoint / full catalog)."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    written: dict[str, Path] = {}

    rows = [profile_dict_to_flat(d) for d in profile_dicts]
    df = _ordered(pd.DataFrame(rows))
    if not df.empty:
        df.insert(0, "rank", range(1, len(df) + 1))

    if write_excel:
        xlsx = output_dir / f"{prefix}_{stamp}.xlsx"
        with pd.ExcelWriter(xlsx, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name="profiles", index=False)
            cov_cols = [
                c
                for c in df.columns
                if c.startswith("cov_")
                or c.startswith("flag_")
                or c in {"rank", "imported_title", "title_en", "film_uid", "year"}
            ]
            if cov_cols:
                df[cov_cols].to_excel(writer, sheet_name="coverage", index=False)
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
            bag_rows = []
            for d in profile_dicts:
                imported = d.get("imported") or {}
                titles = d.get("titles") or {}
                for b in d.get("evidence_bags") or []:
                    text = b.get("text") or ""
                    bag_rows.append(
                        {
                            "film_uid": d.get("film_uid"),
                            "imported_title": imported.get("title"),
                            "title_en": titles.get("en"),
                            "bag": b.get("name"),
                            "source": b.get("source"),
                            "language": b.get("language"),
                            "weight": b.get("weight"),
                            "chars": len(text),
                            "words": len(text.split()),
                            "preview": text[:400],
                        }
                    )
            if bag_rows:
                # Cap evidence sheet for very large catalogs (Excel ~1M rows)
                max_bag_rows = 200_000
                pd.DataFrame(bag_rows[:max_bag_rows]).to_excel(
                    writer, sheet_name="evidence_bags", index=False
                )
        written["excel"] = xlsx

    js = output_dir / f"{prefix}_{stamp}.json"
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "gather_only",
        "count": len(profile_dicts),
        "note": "No psych scores or clusters. Review evidence bags before scoring.",
        "profiles": profile_dicts,
    }
    # Compact JSON for large catalogs (indent only when small)
    indent = 2 if len(profile_dicts) <= 500 else None
    js.write_text(json.dumps(payload, ensure_ascii=False, indent=indent), encoding="utf-8")
    written["json"] = js

    latest = output_dir / f"{prefix}_latest.json"
    latest.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2 if len(rows) <= 500 else None),
        encoding="utf-8",
    )
    written["latest"] = latest
    return written


def write_profiles(
    profiles: Iterable[EnrichmentProfile],
    output_dir: str | Path = "output",
    *,
    prefix: str = "profile",
) -> dict[str, Path]:
    profiles = list(profiles)
    return write_profile_dicts(
        [p.to_json_dict() for p in profiles],
        output_dir=output_dir,
        prefix=prefix,
    )


# ---------------------------------------------------------------------------
# Live / incremental exports (after each film)
# ---------------------------------------------------------------------------

LIVE_CSV_COLUMNS = [
    "n",
    "imported_row",
    "imported_title",
    "imported_year",
    "title_en",
    "title_ru",
    "year",
    "imdb_id",
    "tmdb_id",
    "kinopoisk_id",
    "genres_en",
    "directors_en",
    "composers_en",
    "imdb_rating",
    "kinopoisk_rating",
    "tmdb_rating",
    "cov_sources_found",
    "cov_plot_en",
    "cov_plot_ru",
    "cov_keywords_n",
    "cov_bags",
    "link_imdb",
    "link_tmdb",
    "link_kinopoisk",
    "link_wikipedia_en",
    "error",
]


def format_profile_text(d: dict, *, index: int | None = None) -> str:
    """Human-readable multi-line record for one gather profile."""
    flat = profile_dict_to_flat(d)
    imported = d.get("imported") or {}
    titles = d.get("titles") or {}
    plots = d.get("plots") or {}
    cov = d.get("coverage") or {}
    bags = d.get("evidence_bags") or []
    hdr = f"#{index}" if index is not None else "#?"
    lines = [
        "=" * 72,
        f"{hdr}  {flat.get('imported_title') or titles.get('en') or '?'}"
        f"  ({flat.get('year') or imported.get('year') or '?'})",
        f"import: file={imported.get('file')} sheet={imported.get('sheet')} "
        f"row={imported.get('row')}",
        f"titles: en={titles.get('en')!r}  ru={titles.get('ru')!r}",
        f"ids: imdb={flat.get('imdb_id')} tmdb={flat.get('tmdb_id')} "
        f"kp={flat.get('kinopoisk_id')} uid={flat.get('film_uid')}",
        f"crew: dir={flat.get('directors_en')}  composers={flat.get('composers_en')}",
        f"genres: {flat.get('genres_en') or flat.get('genres')}",
        f"ratings: imdb={flat.get('imdb_rating')} kp={flat.get('kinopoisk_rating')} "
        f"tmdb={flat.get('tmdb_rating')}",
        f"coverage: sources={cov.get('sources_found')} plot_en={cov.get('has_plot_en')} "
        f"plot_ru={cov.get('has_plot_ru')} kw={cov.get('keywords_n')} bags={len(bags)}",
        f"links: imdb={flat.get('link_imdb')}",
        f"       tmdb={flat.get('link_tmdb')}",
        f"       kp={flat.get('link_kinopoisk')}",
        f"       wiki_en={flat.get('link_wikipedia_en')}",
    ]
    if flat.get("error"):
        lines.append(f"ERROR: {flat.get('error')}")
    pe = (plots.get("en") or "")[:500]
    pr = (plots.get("ru") or "")[:500]
    if pe:
        lines.append(f"plot_en: {pe}")
    if pr:
        lines.append(f"plot_ru: {pr}")
    bag_names = sorted({b.get("name") for b in bags if b.get("name")})
    if bag_names:
        lines.append(f"evidence_bags: {', '.join(bag_names)}")
    lines.append("")
    return "\n".join(lines)


def append_live_text(d: dict, path: str | Path, *, index: int) -> Path:
    """Append one film record to the running text report."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(format_profile_text(d, index=index))
    return path


def append_live_csv(d: dict, path: str | Path, *, index: int) -> Path:
    """Append one flat CSV row (Excel-friendly). Creates header on first write."""
    import csv

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    flat = profile_dict_to_flat(d)
    row = {"n": index}
    for col in LIVE_CSV_COLUMNS:
        if col == "n":
            continue
        row[col] = flat.get(col)
    new_file = not path.exists() or path.stat().st_size == 0
    with path.open("a", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=LIVE_CSV_COLUMNS, extrasaction="ignore")
        if new_file:
            writer.writeheader()
        writer.writerow(row)
    return path


def write_live_excel(
    profile_dicts: list[dict],
    path: str | Path,
    *,
    include_evidence: bool = False,
) -> Path:
    """Rewrite the rolling live Excel workbook from all gathered profiles so far."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [profile_dict_to_flat(d) for d in profile_dicts]
    df = _ordered(pd.DataFrame(rows))
    if not df.empty:
        df.insert(0, "rank", range(1, len(df) + 1))
    tmp = path.with_suffix(".tmp.xlsx")
    with pd.ExcelWriter(tmp, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="profiles", index=False)
        cov_cols = [
            c
            for c in df.columns
            if c.startswith("cov_")
            or c.startswith("flag_")
            or c in {"rank", "imported_title", "title_en", "film_uid", "year"}
        ]
        if cov_cols:
            df[cov_cols].to_excel(writer, sheet_name="coverage", index=False)
        id_cols = [
            c
            for c in [
                "rank",
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
            ]
            if c in df.columns
        ]
        if id_cols:
            df[id_cols].to_excel(writer, sheet_name="identity_links", index=False)
        if include_evidence:
            bag_rows = []
            for d in profile_dicts:
                imported = d.get("imported") or {}
                titles = d.get("titles") or {}
                for b in (d.get("evidence_bags") or [])[:20]:
                    text = b.get("text") or ""
                    bag_rows.append(
                        {
                            "film_uid": d.get("film_uid"),
                            "imported_title": imported.get("title"),
                            "title_en": titles.get("en"),
                            "bag": b.get("name"),
                            "source": b.get("source"),
                            "chars": len(text),
                            "preview": text[:300],
                        }
                    )
            if bag_rows:
                pd.DataFrame(bag_rows[:100_000]).to_excel(
                    writer, sheet_name="evidence_bags", index=False
                )
    tmp.replace(path)
    return path


def bootstrap_live_exports_from_checkpoint(
    checkpoint_path: str | Path,
    *,
    text_path: str | Path,
    csv_path: str | Path,
    excel_path: str | Path,
    rebuild_excel: bool = True,
) -> int:
    """
    Rebuild live text/csv/excel from an existing JSONL checkpoint
    (used on resume so live files stay complete).
    """
    checkpoint_path = Path(checkpoint_path)
    if not checkpoint_path.exists():
        return 0
    dicts: list[dict] = []
    with checkpoint_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            row.pop("_resume_key", None)
            dicts.append(row)

    text_path = Path(text_path)
    csv_path = Path(csv_path)
    excel_path = Path(excel_path)
    text_path.parent.mkdir(parents=True, exist_ok=True)

    # Rewrite text + csv fully so resume stays consistent
    with text_path.open("w", encoding="utf-8") as tf:
        tf.write(
            f"PsychoFilm gather live report\n"
            f"profiles: {len(dicts)}\n"
            f"generated: {datetime.now(timezone.utc).isoformat()}\n\n"
        )
        for i, d in enumerate(dicts, start=1):
            tf.write(format_profile_text(d, index=i))

    if csv_path.exists():
        csv_path.unlink()
    for i, d in enumerate(dicts, start=1):
        append_live_csv(d, csv_path, index=i)

    if rebuild_excel and dicts:
        write_live_excel(dicts, excel_path, include_evidence=False)
    return len(dicts)

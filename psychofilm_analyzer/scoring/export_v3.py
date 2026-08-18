"""Export v3 scored results (final + live per-film)."""

from __future__ import annotations

import csv
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from psychofilm_analyzer.utils.localtime import now_str, stamp_local

logger = logging.getLogger(__name__)

# Excel cell limit ~32767; keep full-ish text for verification
_EXCEL_TEXT_MAX = 32000

# Must match keys written by v3_engine flat["score_{name}"] (dictionaries_v3.yaml)
LIVE_SCORE_CSV_COLUMNS = [
    "n",
    "imported_row",
    "imported_title",
    "imported_year",
    "title_en",
    "title_ru",
    "year",
    "primary_theme",
    "secondary_theme",
    "theme_confidence",
    "score_Podcast_Priority",
    "score_Overall_Priority_for_Podcast",
    "Overall_Priority_for_Podcast",
    "score_Psychological_Depth",
    "score_Trauma_Clinical_Relevance",
    "score_Identity_Transformation",
    "score_Madness_Altered_States",
    "score_Family_Systems_Complexity",
    "score_Existential_Weight",
    "score_Collective_Historical_Psychotype",
    "score_Narrative_Craft",
    "score_Symbolism_Ambiguity",
    "score_Discussability_Podcast_Potential",
    "score_Awards_Prestige",
    "Awards_Prestige",
    "score_Spiritual_Religious_Mystical_Depth",
    "score_Purpose_Destiny_Meaning_of_Life",
    "score_Archetypal_Mythic_Resonance",
    "score_Fairy_Tale_Folklore_Mystical_Density",
    "score_Intellectual_Scientific_Complexity",
    "score_Human_Nature_Spectrum",
    "score_Easy_to_Watch",
    "score_Interesting_to_Watch_Engagement",
    "score_Modern_Viewer_Deliverability",
    "argumentation_Modern_Viewer_Deliverability",
    "evidence_Modern_Viewer_Deliverability",
    "Spoiler_Free_Psychological_Hook",
    # Source text always exported for judgment verification
    "plot_en",
    "plot_ru",
    "plot_de",
    "has_plot_de",
    "overview_en",
    "overview_ru",
    "overview_de",
    "link_wikipedia_de",
    "awards_text",
    "keywords",
    "genres_en",
    "genres_ru",
    "bag_inventory",
    "evidence_top",
    "evidence_Psychological_Depth",
    "evidence_Symbolism_Ambiguity",
    "evidence_Discussability_Podcast_Potential",
    "evidence_Awards_Prestige",
    "evidence_Narrative_Craft",
    "argumentation_summary",
    "argumentation_primary_theme",
    "argumentation_Podcast_Priority",
    "argumentation_Overall_Priority_for_Podcast",
    "argumentation_Psychological_Depth",
    "argumentation_Symbolism_Ambiguity",
    "argumentation_Discussability_Podcast_Potential",
    "argumentation_Awards_Prestige",
    "argumentation_Narrative_Craft",
    "argumentation_Trauma_Clinical_Relevance",
    "argumentation_Identity_Transformation",
    "argumentation_Madness_Altered_States",
    "argumentation_Existential_Weight",
    "imdb_id",
    "tmdb_id",
    "kinopoisk_id",
    "imdb_rating",
    "kinopoisk_rating",
    "film_uid",
]

# Column order for verify sheet: scores next to source text
VERIFY_SHEET_COLUMNS = [
    "rank",
    "imported_row",
    "imported_title",
    "title_en",
    "title_ru",
    "year",
    "primary_theme",
    "secondary_theme",
    "theme_confidence",
    "score_Podcast_Priority",
    "score_Overall_Priority_for_Podcast",
    "Overall_Priority_for_Podcast",
    "score_Psychological_Depth",
    "score_Symbolism_Ambiguity",
    "score_Discussability_Podcast_Potential",
    "score_Awards_Prestige",
    "Awards_Prestige",
    "score_Narrative_Craft",
    "score_Trauma_Clinical_Relevance",
    "score_Easy_to_Watch",
    "score_Interesting_to_Watch_Engagement",
    "score_Modern_Viewer_Deliverability",
    "Spoiler_Free_Psychological_Hook",
    "plot_en",
    "plot_ru",
    "plot_de",
    "has_plot_de",
    "overview_en",
    "overview_ru",
    "overview_de",
    "link_wikipedia_de",
    "awards_text",
    "keywords",
    "genres_en",
    "genres_ru",
    "bag_inventory",
    "evidence_top",
    "evidence_Psychological_Depth",
    "evidence_Symbolism_Ambiguity",
    "evidence_Discussability_Podcast_Potential",
    "evidence_Awards_Prestige",
    "evidence_Narrative_Craft",
    "evidence_Existential_Weight",
    "evidence_Identity_Transformation",
    "evidence_Madness_Altered_States",
    "argumentation_summary",
    "argumentation_primary_theme",
    "argumentation_Podcast_Priority",
    "argumentation_Overall_Priority_for_Podcast",
    "argumentation_Psychological_Depth",
    "argumentation_Symbolism_Ambiguity",
    "argumentation_Discussability_Podcast_Potential",
    "argumentation_Awards_Prestige",
    "argumentation_Narrative_Craft",
    "argumentation_Trauma_Clinical_Relevance",
    "argumentation_Identity_Transformation",
    "argumentation_Madness_Altered_States",
    "argumentation_Existential_Weight",
    "argumentation_Family_Systems_Complexity",
    "argumentation_Collective_Historical_Psychotype",
    "argumentation_Spiritual_Religious_Mystical_Depth",
    "argumentation_Easy_to_Watch",
    "argumentation_Interesting_to_Watch_Engagement",
    "argumentation_Modern_Viewer_Deliverability",
    "evidence_Modern_Viewer_Deliverability",
    "link_imdb",
    "link_tmdb",
    "link_kinopoisk",
    "link_wikipedia_en",
    "link_wikipedia_ru",
    "link_wikipedia_de",
    "film_uid",
    "imdb_id",
    "tmdb_id",
    "kinopoisk_id",
]


def _excel_safe_text(value: Any, max_len: int = _EXCEL_TEXT_MAX) -> Any:
    """Keep full text for verification; only trim at Excel hard limit."""
    if value is None or not isinstance(value, str):
        return value
    if len(value) <= max_len:
        return value
    return value[: max_len - 20] + "\n…[truncated for Excel]"


def _result_flat(r: dict[str, Any]) -> dict[str, Any]:
    row = dict(r.get("flat") or {})
    row["primary_theme"] = r.get("primary_theme") or row.get("primary_theme")
    row["secondary_theme"] = r.get("secondary_theme") or row.get("secondary_theme")
    row["theme_confidence"] = r.get("theme_confidence") or row.get("theme_confidence")
    return row


def _flats_from_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flats = [_result_flat(r) for r in results]
    return sorted(
        flats,
        key=lambda x: float(
            x.get("score_Podcast_Priority") or x.get("Overall_Priority_for_Podcast") or 0
        ),
        reverse=True,
    )


def _prepare_df_for_excel(flats_sorted: list[dict[str, Any]]) -> pd.DataFrame:
    """Sanitize text cells for Excel while keeping as much source text as possible."""
    rows = []
    for row in flats_sorted:
        clean = {}
        for k, v in row.items():
            if isinstance(v, str) and len(v) > 200:
                clean[k] = _excel_safe_text(v)
            else:
                clean[k] = v
        rows.append(clean)
    df = pd.DataFrame(rows)
    if not df.empty and "rank" not in df.columns:
        df.insert(0, "rank", range(1, len(df) + 1))
    elif not df.empty:
        # ensure rank is first
        cols = ["rank"] + [c for c in df.columns if c != "rank"]
        df = df[cols]
    return df


def _write_excel_workbook(
    flats_sorted: list[dict[str, Any]],
    results: list[dict[str, Any]],
    xlsx: Path,
    *,
    include_evidence: bool = True,
) -> None:
    df = _prepare_df_for_excel(flats_sorted)
    if not df.empty and "rank" not in list(df.columns)[:1]:
        if "rank" not in df.columns:
            df.insert(0, "rank", range(1, len(df) + 1))

    tmp = xlsx.with_suffix(".tmp.xlsx")
    with pd.ExcelWriter(tmp, engine="openpyxl") as writer:
        # Full dump (all columns including source text + evidence_*)
        df.to_excel(writer, sheet_name="all", index=False)

        # Primary verification sheet: scores beside full source texts + argumentation
        verify_cols = [c for c in VERIFY_SHEET_COLUMNS if c in df.columns]
        # also include any extra evidence_* / argumentation_* / plot_* columns
        for c in df.columns:
            if (
                c.startswith("evidence_")
                or c.startswith("argumentation_")
                or c.startswith("plot_")
                or c.startswith("overview_")
            ):
                if c not in verify_cols:
                    verify_cols.append(c)
        if verify_cols:
            df[verify_cols].to_excel(writer, sheet_name="text_reference", index=False)

        # Dedicated argumentation sheet: every vector explanation next to its score
        arg_cols = [
            c
            for c in df.columns
            if c
            in {
                "rank",
                "imported_title",
                "title_en",
                "title_ru",
                "year",
                "primary_theme",
                "secondary_theme",
                "argumentation_summary",
                "argumentation_primary_theme",
            }
            or c.startswith("score_")
            or c.startswith("argumentation_")
            or c.startswith("cluster_score_")
        ]
        # stable-ish order: title, summary, then score/argumentation pairs preferred by name
        if arg_cols:
            preferred = [
                "rank",
                "title_en",
                "title_ru",
                "year",
                "primary_theme",
                "secondary_theme",
                "argumentation_summary",
                "argumentation_primary_theme",
            ]
            rest = [c for c in arg_cols if c not in preferred]
            # interleave score_X then argumentation_X when both exist
            paired: list[str] = []
            seen_rest: set[str] = set()
            score_names = [c[6:] for c in rest if c.startswith("score_")]
            for name in score_names:
                sc = f"score_{name}"
                ar = f"argumentation_{name}"
                if sc in rest and sc not in seen_rest:
                    paired.append(sc)
                    seen_rest.add(sc)
                if ar in rest and ar not in seen_rest:
                    paired.append(ar)
                    seen_rest.add(ar)
            for c in rest:
                if c not in seen_rest:
                    paired.append(c)
                    seen_rest.add(c)
            ordered_arg = [c for c in preferred if c in df.columns] + paired
            df[ordered_arg].to_excel(writer, sheet_name="argumentation", index=False)

        # Compact scores only (still keep title for join)
        score_cols = [
            c
            for c in df.columns
            if c.startswith("score_")
            or c.startswith("evidence_")
            or c.startswith("argumentation_")
            or c
            in {
                "rank",
                "imported_title",
                "title_en",
                "title_ru",
                "year",
                "primary_theme",
                "secondary_theme",
                "theme_confidence",
                "Overall_Priority_for_Podcast",
                "Awards_Prestige",
                "plot_en",
                "plot_ru",
                "plot_de",
                "has_plot_de",
                "awards_text",
                "keywords",
                "evidence_top",
            }
        ]
        if score_cols:
            df[score_cols].to_excel(writer, sheet_name="scores", index=False)

        cluster_cols = [
            c
            for c in df.columns
            if c.startswith("cluster_score_")
            or c.startswith("cluster_evidence_")
            or c in {"rank", "title_en", "primary_theme", "secondary_theme", "plot_en", "plot_ru"}
        ]
        if cluster_cols:
            df[cluster_cols].to_excel(writer, sheet_name="clusters", index=False)

        field_prefixes = (
            "Primary_",
            "Secondary_",
            "Archetypes",
            "Trauma_",
            "Defense_",
            "Character_",
            "Attachment_",
            "Core_",
            "Conflict_",
            "Internal_",
            "Resolution_",
            "Narrative_",
            "Subtext_",
            "Symbolic_",
            "Ambiguity_",
            "Unreliable_",
            "Visual_",
            "Religious_",
            "Rites_",
            "Themes_of_",
            "Hidden_",
            "Secret_",
            "Mystical_",
            "Silence_",
            "View_of_",
            "Human_Nature_",
            "Soul_",
            "Free_Will_",
            "Target_",
            "Typical_",
            "How_the_",
            "Recommended_",
            "Reflective_",
            "Is_Fairytale",
            "Cultural_",
            "Folklore_",
            "Initiation_",
            "Historical_",
            "Historiographical_",
            "Alternative_",
            "Ideological_",
            "Psychological_Truth",
            "Modern_",
            "Truth_Validation",
            "Scientific_",
            "Dramatic_",
            "Consensus_",
            "Main_Scientific",
            "Competing_",
            "Outlook_",
            "Perspective_",
            "Invitation_",
            "Propaganda_",
            "subj_",
            "Spoiler_",
            "Podcast_",
            "Best_Audience",
            "Trigger_",
            "Overall_",
            "Awards_",
        )
        field_cols = [
            c
            for c in df.columns
            if c in {"rank", "title_en", "year", "plot_en", "plot_ru", "evidence_top"}
            or any(c.startswith(p) for p in field_prefixes)
        ]
        if field_cols:
            df[field_cols].to_excel(writer, sheet_name="fields_BJ", index=False)

        if include_evidence:
            ev_rows: list[dict] = []
            for r in results:
                for row in r.get("evidence_index") or []:
                    # attach titles for filtering
                    enriched = dict(row)
                    flat = r.get("flat") or {}
                    enriched.setdefault("imported_title", flat.get("imported_title"))
                    enriched.setdefault("film_uid", flat.get("film_uid") or r.get("film_uid"))
                    ev_rows.append(enriched)
            if ev_rows:
                pd.DataFrame(ev_rows[:150_000]).to_excel(
                    writer, sheet_name="evidence", index=False
                )

        id_cols = [
            c
            for c in df.columns
            if c.startswith("imported_")
            or c.startswith("link_")
            or c
            in {
                "rank",
                "film_uid",
                "imdb_id",
                "tmdb_id",
                "kinopoisk_id",
                "title_en",
                "title_ru",
                "bag_inventory",
                "has_plot_en",
                "has_plot_ru",
                "has_plot_de",
                "has_awards_text",
            }
        ]
        if id_cols:
            df[id_cols].to_excel(writer, sheet_name="identity_links", index=False)

    tmp.replace(xlsx)


def write_v3_results(
    results: list[dict[str, Any]],
    output_dir: str | Path = "output",
    *,
    prefix: str = "psychofilm_v3",
) -> dict[str, Path]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = stamp_local()
    written: dict[str, Path] = {}

    flats_sorted = _flats_from_results(results)

    xlsx = output_dir / f"{prefix}_{stamp}.xlsx"
    _write_excel_workbook(flats_sorted, results, xlsx, include_evidence=True)
    written["excel"] = xlsx

    js = output_dir / f"{prefix}_{stamp}.json"
    payload = {
        "generated_at": now_str(),
        "mode": "score_v3",
        "count": len(results),
        "results": results,
    }
    indent = 2 if len(results) <= 200 else None
    js.write_text(json.dumps(payload, ensure_ascii=False, indent=indent), encoding="utf-8")
    written["json"] = js

    latest = output_dir / f"{prefix}_latest.json"
    latest.write_text(
        json.dumps(flats_sorted, ensure_ascii=False, indent=2 if len(flats_sorted) <= 200 else None),
        encoding="utf-8",
    )
    written["latest"] = latest

    # short markdown top
    md = output_dir / f"{prefix}_{stamp}_top.md"
    lines = ["# PsychoFilm v3 — Podcast priority ranking", ""]
    for i, row in enumerate(flats_sorted[:15], 1):
        lines.append(
            f"## {i}. {row.get('title_en') or row.get('imported_title')} — "
            f"**{float(row.get('score_Podcast_Priority') or 0):.1f}** podcast priority"
        )
        lines.append(f"- Theme: {row.get('primary_theme')} / {row.get('secondary_theme')}")
        lines.append(
            f"- Psych depth: {row.get('score_Psychological_Depth')} · "
            f"Trauma: {row.get('score_Trauma_Clinical_Relevance')} · "
            f"Easy: {row.get('score_Easy_to_Watch')}"
        )
        lines.append(f"- Hook: {row.get('Spoiler_Free_Psychological_Hook')}")
        lines.append("")
    md.write_text("\n".join(lines), encoding="utf-8")
    written["markdown"] = md
    return written


# ---------------------------------------------------------------------------
# Live / incremental score-v3 exports (after each film)
# ---------------------------------------------------------------------------


def format_score_text(r: dict[str, Any], *, index: int | None = None) -> str:
    """Human-readable multi-line card for one scored film."""
    flat = _result_flat(r)
    scores = r.get("scores") or {}
    hdr = f"#{index}" if index is not None else "#?"
    title = flat.get("title_en") or flat.get("imported_title") or "?"
    year = flat.get("year") or flat.get("imported_year") or "?"

    def sc(name: str) -> str:
        block = scores.get(name) or {}
        if isinstance(block, dict) and "score" in block:
            return f"{float(block['score']):.1f}"
        v = flat.get(f"score_{name}")
        return f"{float(v):.1f}" if v is not None else "—"

    lines = [
        "=" * 72,
        f"{hdr}  {title}  ({year})",
        f"import: row={flat.get('imported_row')}  title={flat.get('imported_title')!r}",
        f"theme: {r.get('primary_theme')} / {r.get('secondary_theme')}  "
        f"(conf={r.get('theme_confidence')})",
        f"scores: Podcast={sc('Podcast_Priority')}  Overall={sc('Overall_Priority_for_Podcast')}  "
        f"Depth={sc('Psychological_Depth')}  Trauma={sc('Trauma_Clinical_Relevance')}",
        f"        Symbol={sc('Symbolism_Ambiguity')}  Discuss={sc('Discussability_Podcast_Potential')}  "
        f"Awards={sc('Awards_Prestige')}  Narrative={sc('Narrative_Craft')}",
        f"        Easy={sc('Easy_to_Watch')}  Engage={sc('Interesting_to_Watch_Engagement')}  "
        f"Deliver={sc('Modern_Viewer_Deliverability')}",
        f"        Identity={sc('Identity_Transformation')}",
        f"hook: {flat.get('Spoiler_Free_Psychological_Hook') or '—'}",
        f"ids: imdb={flat.get('imdb_id')} tmdb={flat.get('tmdb_id')} "
        f"kp={flat.get('kinopoisk_id')}",
        f"ratings: imdb={flat.get('imdb_rating')} kp={flat.get('kinopoisk_rating')}",
        f"bags: {flat.get('bag_inventory') or '—'}",
        f"awards_text: {(flat.get('awards_text') or '—')[:400]}",
        f"plot_en: {(flat.get('plot_en') or '—')[:600]}",
        f"plot_ru: {(flat.get('plot_ru') or '—')[:600]}",
        f"plot_de: {(flat.get('plot_de') or '—')[:600]}",
        f"keywords: {(flat.get('keywords') or '—')[:300]}",
        f"evidence_top: {(flat.get('evidence_top') or '—')[:800]}",
        f"argumentation_summary: {(flat.get('argumentation_summary') or '—')[:900]}",
        f"argumentation_theme: {(flat.get('argumentation_primary_theme') or '—')[:700]}",
        f"argumentation_Podcast: {(flat.get('argumentation_Podcast_Priority') or '—')[:500]}",
        f"argumentation_Overall: {(flat.get('argumentation_Overall_Priority_for_Podcast') or '—')[:500]}",
        "",
    ]
    return "\n".join(lines)


def append_score_live_text(r: dict[str, Any], path: str | Path, *, index: int) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(format_score_text(r, index=index))
    return path


def append_score_live_csv(r: dict[str, Any], path: str | Path, *, index: int) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    flat = _result_flat(r)
    row = {"n": index}
    for col in LIVE_SCORE_CSV_COLUMNS:
        if col == "n":
            continue
        row[col] = flat.get(col)
    new_file = not path.exists() or path.stat().st_size == 0
    with path.open("a", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=LIVE_SCORE_CSV_COLUMNS, extrasaction="ignore")
        if new_file:
            writer.writeheader()
        writer.writerow(row)
    return path


def write_score_live_excel(
    results: list[dict[str, Any]],
    path: str | Path,
    *,
    include_evidence: bool = False,
) -> Path:
    """Rewrite rolling live Excel from all scored results so far."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    flats_sorted = _flats_from_results(results)
    _write_excel_workbook(flats_sorted, results, path, include_evidence=include_evidence)
    return path


def score_resume_key(profile: dict[str, Any]) -> str:
    """Stable key for score checkpoint (prefer import row)."""
    imported = profile.get("imported") or {}
    if imported.get("file") or imported.get("sheet") or imported.get("row") is not None:
        return "|".join(
            [
                str(imported.get("file") or ""),
                str(imported.get("sheet") or ""),
                str(imported.get("row") or ""),
                str(imported.get("title") or "").strip().lower(),
                str(imported.get("year") or profile.get("year") or ""),
            ]
        )
    # flat format
    if profile.get("imported_file") or profile.get("imported_row") is not None:
        return "|".join(
            [
                str(profile.get("imported_file") or ""),
                str(profile.get("imported_sheet") or ""),
                str(profile.get("imported_row") or ""),
                str(profile.get("imported_title") or "").strip().lower(),
                str(profile.get("imported_year") or profile.get("year") or ""),
            ]
        )
    titles = profile.get("titles") or {}
    title = titles.get("en") or profile.get("title_en") or profile.get("film_uid") or "unknown"
    year = profile.get("year") or ""
    return f"{str(title).strip().lower()}|{year}"


def load_score_checkpoint(path: str | Path) -> list[dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            row.pop("_resume_key", None)
            out.append(row)
    return out


def bootstrap_score_live_from_checkpoint(
    checkpoint_path: str | Path,
    *,
    text_path: str | Path,
    csv_path: str | Path,
    excel_path: str | Path,
    rebuild_excel: bool = True,
) -> list[dict[str, Any]]:
    """Rebuild live score text/csv/excel from score_checkpoint.jsonl."""
    results = load_score_checkpoint(checkpoint_path)
    text_path = Path(text_path)
    csv_path = Path(csv_path)
    excel_path = Path(excel_path)
    text_path.parent.mkdir(parents=True, exist_ok=True)

    with text_path.open("w", encoding="utf-8") as tf:
        tf.write(
            f"PsychoFilm score-v3 live report\n"
            f"scored: {len(results)}\n"
            f"generated: {now_str()}\n\n"
        )
        for i, r in enumerate(results, start=1):
            tf.write(format_score_text(r, index=i))

    if csv_path.exists():
        csv_path.unlink()
    for i, r in enumerate(results, start=1):
        append_score_live_csv(r, csv_path, index=i)

    if rebuild_excel and results:
        write_score_live_excel(results, excel_path, include_evidence=False)
    return results

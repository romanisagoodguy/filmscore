"""Export v3 scored results."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


def write_v3_results(
    results: list[dict[str, Any]],
    output_dir: str | Path = "output",
    *,
    prefix: str = "psychofilm_v3",
) -> dict[str, Path]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    written: dict[str, Path] = {}

    flats = []
    for r in results:
        row = dict(r.get("flat") or {})
        row["primary_theme"] = r.get("primary_theme")
        row["secondary_theme"] = r.get("secondary_theme")
        row["theme_confidence"] = r.get("theme_confidence")
        flats.append(row)

    # rank by Podcast_Priority if present
    flats_sorted = sorted(
        flats,
        key=lambda x: float(x.get("score_Podcast_Priority") or x.get("Overall_Priority_for_Podcast") or 0),
        reverse=True,
    )
    df = pd.DataFrame(flats_sorted)
    if not df.empty:
        df.insert(0, "rank", range(1, len(df) + 1))

    xlsx = output_dir / f"{prefix}_{stamp}.xlsx"
    with pd.ExcelWriter(xlsx, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="all", index=False)

        score_cols = [c for c in df.columns if c.startswith("score_") or c in {
            "rank", "imported_title", "title_en", "year", "primary_theme", "secondary_theme",
            "theme_confidence", "Overall_Priority_for_Podcast",
        }]
        if score_cols:
            df[score_cols].to_excel(writer, sheet_name="scores", index=False)

        cluster_cols = [c for c in df.columns if c.startswith("cluster_score_") or c in {
            "rank", "title_en", "primary_theme", "secondary_theme",
        }]
        if cluster_cols:
            df[cluster_cols].to_excel(writer, sheet_name="clusters", index=False)

        field_prefixes = (
            "Primary_", "Secondary_", "Archetypes", "Trauma_", "Defense_", "Character_",
            "Attachment_", "Core_", "Conflict_", "Internal_", "Resolution_", "Narrative_",
            "Subtext_", "Symbolic_", "Ambiguity_", "Unreliable_", "Visual_", "Religious_",
            "Rites_", "Themes_of_", "Hidden_", "Secret_", "Mystical_", "Silence_",
            "View_of_", "Human_Nature_", "Soul_", "Free_Will_", "Target_", "Typical_",
            "How_the_", "Recommended_", "Reflective_", "Is_Fairytale", "Cultural_",
            "Folklore_", "Initiation_", "Historical_", "Historiographical_", "Alternative_",
            "Ideological_", "Psychological_Truth", "Modern_", "Truth_Validation",
            "Scientific_", "Dramatic_", "Consensus_", "Main_Scientific", "Competing_",
            "Outlook_", "Perspective_", "Invitation_", "Propaganda_", "subj_",
            "Spoiler_", "Podcast_", "Best_Audience", "Trigger_", "Overall_", "Awards_",
        )
        field_cols = [
            c for c in df.columns
            if c in {"rank", "title_en", "year"} or any(c.startswith(p) for p in field_prefixes)
        ]
        if field_cols:
            df[field_cols].to_excel(writer, sheet_name="fields_BJ", index=False)

        ev_rows = []
        for r in results:
            ev_rows.extend(r.get("evidence_index") or [])
        if ev_rows:
            pd.DataFrame(ev_rows).to_excel(writer, sheet_name="evidence", index=False)

        id_cols = [c for c in df.columns if c.startswith("imported_") or c.startswith("link_") or c in {
            "rank", "film_uid", "imdb_id", "tmdb_id", "kinopoisk_id", "title_en", "title_ru",
        }]
        if id_cols:
            df[id_cols].to_excel(writer, sheet_name="identity_links", index=False)

    written["excel"] = xlsx

    js = output_dir / f"{prefix}_{stamp}.json"
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "score_v3",
        "count": len(results),
        "results": results,
    }
    js.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    written["json"] = js

    latest = output_dir / f"{prefix}_latest.json"
    latest.write_text(json.dumps(flats_sorted, ensure_ascii=False, indent=2), encoding="utf-8")
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
        lines.append(f"- Psych depth: {row.get('score_Psychological_Depth')} · Trauma: {row.get('score_Trauma_Clinical_Relevance')} · Easy: {row.get('score_Easy_to_Watch')}")
        lines.append(f"- Hook: {row.get('Spoiler_Free_Psychological_Hook')}")
        lines.append("")
    md.write_text("\n".join(lines), encoding="utf-8")
    written["markdown"] = md
    return written

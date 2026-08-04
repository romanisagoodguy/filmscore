"""Generate podcast-ready structured descriptions (200–400 words target)."""

from __future__ import annotations

import re
from typing import Any

from psychofilm_analyzer.models import EnrichedResult
from psychofilm_analyzer.utils.text import truncate_words, word_count


def generate_description(result: EnrichedResult, dictionaries: dict[str, Any] | None = None) -> str:
    title = result.title_en or result.normalized_title_en or result.input.display_title()
    year = result.year or result.input.year
    title_bit = f"{title}" + (f" ({year})" if year else "")

    plot = result.plot_en or result.overview_en or _best_plot(result)
    plot = _spoiler_light(plot)

    themes = []
    if result.primary_cluster:
        themes.append(result.primary_cluster)
    if result.secondary_cluster:
        themes.append(result.secondary_cluster)
    # keyword themes
    for k in (result.keywords or [])[:8]:
        if k not in themes:
            themes.append(k)
    themes_txt = ", ".join(themes[:8]) if themes else "limited explicit psychological tagging in available sources"

    factors = result.factors
    why = _why_discuss(result)
    awards = _awards_blurb(result)
    angles = _podcast_angles(result)
    directors = ", ".join((result.directors_en or result.directors)[:3]) if (result.directors_en or result.directors) else "unknown director"
    genre_pool = result.genres or result.genres_en or result.genres_ru or []
    genres = ", ".join(genre_pool[:12]) if genre_pool else "unspecified genre"
    score = result.psycho_score
    conf = result.confidence.value if result.confidence else "Low"

    parts = [
        f"{title_bit} is a {genres} work directed/created by {directors}. "
        f"PsychoFilm Analyzer assigns a Psycho_Score of {score:.1f}/10 "
        f"(confidence: {conf})"
        + (f", primary psychological cluster: {result.primary_cluster}" if result.primary_cluster else "")
        + (
            f", with secondary resonance in {result.secondary_cluster}."
            if result.secondary_cluster
            else "."
        ),
        "",
        f"Plot (spoiler-light): {plot}" if plot else "Plot: detailed synopsis was not available from connected sources.",
        "",
        f"Key psychological themes: {themes_txt}.",
        "",
        f"Why it is (or isn’t) interesting for deep discussion: {why}",
        "",
        f"Awards & prestige: {awards}",
        "",
        f"Suggested podcast angles: {angles}",
        "",
        "Feature breakdown: "
        f"thematic { _fmt(factors.thematic_keyword_density) }, "
        f"narrative depth { _fmt(factors.narrative_character_depth) }, "
        f"awards { _fmt(factors.awards_prestige) }, "
        f"discourse { _fmt(factors.critical_intellectual_discourse) }, "
        f"director reputation { _fmt(factors.director_creator_reputation) }, "
        f"discussability { _fmt(factors.discussability_podcast) }.",
    ]
    if result.caps_applied:
        parts.append(f"Scoring caps applied: {'; '.join(result.caps_applied)}.")
    if result.source_links:
        parts.append("Sources: " + " | ".join(result.source_links[:6]) + ".")

    text = "\n".join(parts)
    # Expand lightly if too short
    if word_count(text) < 180:
        text += (
            "\n\nFor podcast preparation, treat ratings and plot summaries as starting points only. "
            "Cross-check character arcs, historical context, and audience polarization before recording. "
            "When data is sparse, confidence drops and the score should be read as provisional. "
            "Re-run enrichment after adding API keys (TMDB, OMDb, Kinopoisk) for denser theme detection."
        )
    if word_count(text) > 420:
        text = truncate_words(text, 400)
    return text


def _fmt(v: float | None) -> str:
    return f"{v:.1f}/10" if v is not None else "n/a"


def _best_plot(result: EnrichedResult) -> str:
    for name in ("omdb", "tmdb", "kinopoisk", "wikipedia", "letterboxd"):
        s = result.sources.get(name)
        if s and s.found:
            for field in (s.plot, s.overview):
                if field and len(field) > 40:
                    return field.strip()
    return ""


def _spoiler_light(plot: str) -> str:
    if not plot:
        return ""
    # keep first ~2 sentences
    sentences = re.split(r"(?<=[.!?])\s+", plot.strip())
    kept = []
    for s in sentences:
        kept.append(s)
        if len(" ".join(kept).split()) > 70:
            break
    text = " ".join(kept[:3])
    # remove late-twist phrasing if present in longer plots
    text = re.sub(r"(?i)\b(in the end|twist|spoiler|reveals that)\b.*", "", text).strip()
    return text


def _why_discuss(result: EnrichedResult) -> str:
    score = result.psycho_score
    f = result.factors
    bits = []
    if score >= 7.5:
        bits.append("strong candidate for a long-form episode")
    elif score >= 5.5:
        bits.append("solid mid-tier discussion piece with selective angles")
    elif score >= 3.5:
        bits.append("limited psychological density; better as a comparison title than a centerpiece")
    else:
        bits.append("weak discussability for a psychology-focused podcast; primarily entertainment or thin data")

    if (f.narrative_character_depth or 0) >= 6.5:
        bits.append("character arcs and moral ambiguity appear rich enough for layered conversation")
    if (f.critical_intellectual_discourse or 0) >= 6.5:
        bits.append("existing critical/cultural discourse can seed research notes quickly")
    if (f.thematic_keyword_density or 0) >= 6.5:
        bits.append("theme density maps cleanly onto the fixed psychological cluster taxonomy")
    if result.caps_applied:
        bits.append("algorithmic caps constrained the score due to genre/quality heuristics")
    if result.confidence.value == "Low":
        bits.append("low data confidence — verify manually before committing show resources")
    return "; ".join(bits) + "."


def _awards_blurb(result: EnrichedResult) -> str:
    for name in ("omdb", "wikipedia", "tmdb"):
        s = result.sources.get(name)
        if s and s.found and s.awards_text:
            return s.awards_text
        if s and s.found and s.awards:
            return "; ".join(s.awards[:5])
    if (result.factors.awards_prestige or 0) <= 2:
        return "no major awards signal detected in available sources."
    return "some prestige signal detected, but free-text awards details were sparse."


def _podcast_angles(result: EnrichedResult) -> str:
    angles = []
    if result.primary_cluster:
        angles.append(f"open with the {result.primary_cluster} frame and test it against character choices")
    if result.secondary_cluster:
        angles.append(f"mid-episode pivot into {result.secondary_cluster}")
    if (result.factors.director_creator_reputation or 0) >= 6:
        angles.append("compare this title to the creator’s wider psychological filmography")
    if (result.factors.awards_prestige or 0) >= 6:
        angles.append("contrast awards narrative (prestige) vs audience emotional reaction")
    angles.append("invite a guest clinician or critic to challenge the algorithmic cluster assignment")
    if result.psycho_score < 5:
        angles.append("use as a foil episode: what is missing that deeper psych cinema usually provides")
    return "; ".join(angles[:5]) + "."

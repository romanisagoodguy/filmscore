"""Critical & intellectual discourse (20%).

Without live web search, estimates discourse from Wikipedia depth,
Letterboxd psych tags, awards analytical prestige, and thematic density.
Optional web mode can be added later via config sources.discourse_web.
"""

from __future__ import annotations

from typing import Any

from psychofilm_analyzer.features.text_aggregate import all_text
from psychofilm_analyzer.models import SourcePayload


def score_discourse(
    sources: dict[str, SourcePayload],
    dictionaries: dict[str, Any],
    thematic_score: float | None = None,
    cluster_hits: dict[str, float] | None = None,
) -> float | None:
    found = [s for s in sources.values() if s and s.found]
    if not found:
        return None

    score = 2.0
    text = all_text(sources)

    # Wikipedia multi-language coverage ≈ cultural discourse
    wiki = sources.get("wikipedia")
    if wiki and wiki.found:
        langs = (wiki.extra or {}).get("langs") or []
        score += 1.0 + 0.7 * len(langs)
        extract_len = len(wiki.overview or "")
        if extract_len > 800:
            score += 1.2
        elif extract_len > 300:
            score += 0.6
        # analytical language
        analysis_terms = (
            "analysis",
            "interpreted",
            "psycho",
            "trauma",
            "existential",
            "symbolic",
            "allegor",
            "criticism",
            "themes",
            "анализ",
            "психолог",
            "травм",
            "символ",
            "философ",
        )
        hits = sum(1 for t in analysis_terms if t in text)
        score += min(2.5, hits * 0.35)

    # Letterboxd psych list/tag presence
    lb = sources.get("letterboxd")
    if lb and lb.found:
        score += 0.8
        score += min(2.0, 0.35 * len(lb.tags or []))

    # Thematic richness proxy for discussability of ideas
    if thematic_score is not None:
        score += thematic_score * 0.15
    if cluster_hits:
        active = sum(1 for v in cluster_hits.values() if v > 0)
        score += min(1.5, active * 0.35)

    # High vote counts / popularity can proxy cultural conversation
    for name in ("tmdb", "omdb", "kinopoisk"):
        s = sources.get(name)
        if s and s.found and s.votes:
            if s.votes > 200_000:
                score += 1.0
            elif s.votes > 50_000:
                score += 0.6
            elif s.votes > 10_000:
                score += 0.3

    return round(max(0.0, min(10.0, score)), 2)

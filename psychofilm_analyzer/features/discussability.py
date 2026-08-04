"""Discussability for podcast (10%)."""

from __future__ import annotations

from typing import Any

from psychofilm_analyzer.features.text_aggregate import all_genres, all_text
from psychofilm_analyzer.models import SourcePayload


def score_discussability(
    sources: dict[str, SourcePayload],
    dictionaries: dict[str, Any],
    *,
    thematic: float | None,
    narrative: float | None,
    discourse: float | None,
    awards: float | None,
) -> float | None:
    found = any(s and s.found for s in sources.values())
    if not found and thematic is None:
        return None

    parts = [p for p in (thematic, narrative, discourse) if p is not None]
    base = sum(parts) / len(parts) if parts else 3.0
    score = base * 0.7

    text = all_text(sources)
    genres = [g.lower() for g in all_genres(sources)]

    # Ambiguity / multiple interpretations signals
    for term in (
        "ambiguous",
        "open ending",
        "controversial",
        "polarizing",
        "debated",
        "interpretation",
        "unreliable",
        "divisive",
        "cult",
        "спорн",
        "неоднознач",
        "интерпретац",
    ):
        if term in text:
            score += 0.45

    # Emotional provocation
    for term in ("disturbing", "shocking", "harrowing", "devastating", "provocative", "шокир", "тяжел"):
        if term in text:
            score += 0.3

    if awards is not None and awards >= 6:
        score += 0.8  # cultural weight sustains conversation

    # Pure spectacle penalty
    spectacle = (dictionaries.get("genres") or {}).get("spectacle") or []
    high = (dictionaries.get("genres") or {}).get("high_psych") or []
    spectacle_hits = sum(1 for g in genres if any(s.lower() in g for s in spectacle))
    high_hits = sum(1 for g in genres if any(h.lower() in g for h in high))
    if spectacle_hits >= 2 and high_hits == 0:
        score -= 1.5

    return round(max(0.0, min(10.0, score + 1.0)), 2)

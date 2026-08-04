"""Narrative & character depth (20%)."""

from __future__ import annotations

import re
from typing import Any

from psychofilm_analyzer.features.text_aggregate import all_genres, all_text
from psychofilm_analyzer.models import SourcePayload


def score_narrative_depth(
    sources: dict[str, SourcePayload],
    dictionaries: dict[str, Any],
) -> float | None:
    text = all_text(sources)
    genres = [g.lower() for g in all_genres(sources)]
    if not text and not genres:
        return None

    signals = dictionaries.get("narrative_signals") or {}
    high = [s.lower() for s in signals.get("high") or []]
    medium = [s.lower() for s in signals.get("medium") or []]
    low = [s.lower() for s in signals.get("low_spectacle") or []]

    score = 3.0  # baseline when some text exists
    hits_high = 0
    for s in high:
        if s in text:
            hits_high += 1
            score += 0.55
    for s in medium:
        if s in text or any(s in g for g in genres):
            score += 0.25

    spectacle_hits = sum(1 for s in low if s in text or any(s in g for g in genres))
    score -= spectacle_hits * 0.45

    # plot length / richness proxy
    words = len(re.findall(r"\b\w+\b", text))
    if words > 400:
        score += 1.0
    elif words > 200:
        score += 0.6
    elif words > 80:
        score += 0.3

    # multi-source agreement on drama-ish genres
    if any("psychological" in g for g in genres):
        score += 1.2
    if any(g in ("drama", "драма") or "drama" in g for g in genres):
        score += 0.6
    if any("thriller" in g or "триллер" in g for g in genres):
        score += 0.3

    # Letterboxd psych tags
    lb = sources.get("letterboxd")
    if lb and lb.found and lb.tags:
        score += min(2.0, 0.4 * len(lb.tags))

    if hits_high >= 4:
        score += 0.8

    return round(max(0.0, min(10.0, score)), 2)

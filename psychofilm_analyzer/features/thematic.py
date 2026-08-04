"""Thematic & keyword density (25%)."""

from __future__ import annotations

import re
from typing import Any

from psychofilm_analyzer.features.text_aggregate import all_genres, all_keywords, all_text
from psychofilm_analyzer.models import SourcePayload


def score_thematic_density(
    sources: dict[str, SourcePayload],
    dictionaries: dict[str, Any],
) -> tuple[float | None, dict[str, float], list[str]]:
    """
    Returns (score 0-10, cluster_keyword_hits, matched_keywords).
    """
    text = all_text(sources)
    keywords = [k.lower() for k in all_keywords(sources)]
    genres = [g.lower() for g in all_genres(sources)]
    blob = text + " " + " ".join(keywords) + " " + " ".join(genres)

    if not blob.strip():
        return None, {}, []

    clusters = dictionaries.get("clusters") or []
    cluster_scores: dict[str, float] = {}
    matched: list[str] = []

    total_hits = 0
    for cluster in clusters:
        name = cluster.get("name")
        hits = 0
        for kw in cluster.get("keywords") or []:
            kw_l = kw.lower()
            # word-ish match
            if len(kw_l) <= 3:
                continue
            pattern = re.escape(kw_l)
            n = len(re.findall(pattern, blob, flags=re.IGNORECASE))
            if n:
                hits += min(n, 3)
                matched.append(kw)
        cluster_scores[name] = float(hits)
        total_hits += hits

    # genre boosts
    high_psych = [g.lower() for g in (dictionaries.get("genres") or {}).get("high_psych") or []]
    genre_boost = 0.0
    for g in genres:
        if any(h in g for h in high_psych):
            genre_boost += 0.4
        if "psychological" in g or "психолог" in g:
            genre_boost += 1.0

    # density score
    unique_matches = len(set(m.lower() for m in matched))
    raw = min(10.0, unique_matches * 0.85 + min(total_hits, 20) * 0.15 + min(genre_boost, 2.5))

    # Strong free-text psych markers even when not in cluster dict stems
    extra_markers = (
        "psychological",
        "psychoanalytic",
        "surreal",
        "avant-garde",
        "unreliable narrator",
        "stream of consciousness",
        "existential",
        "jungian",
        "trauma",
        "identity crisis",
        "mental illness",
        "psychosis",
        "психолог",
        "сюрреал",
        "экзистен",
    )
    extra_hits = sum(1 for m in extra_markers if m in blob)
    raw += min(2.5, extra_hits * 0.45)

    # if almost no psych signals
    if unique_matches == 0 and genre_boost < 0.5 and extra_hits == 0:
        # still give small score if drama present
        if any("drama" in g or "драма" in g for g in genres):
            raw = max(raw, 2.5)
        else:
            raw = max(raw, 1.0)

    return round(min(10.0, raw), 2), cluster_scores, matched[:40]

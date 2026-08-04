"""Awards & prestige (15%)."""

from __future__ import annotations

from typing import Any

from psychofilm_analyzer.features.text_aggregate import all_awards, all_text
from psychofilm_analyzer.models import SourcePayload


def score_awards_prestige(
    sources: dict[str, SourcePayload],
    dictionaries: dict[str, Any],
) -> float | None:
    awards = all_awards(sources)
    text = " ".join(awards).lower() + " " + all_text(sources)
    if not text.strip():
        # no award info found — neutral low score rather than None (we did check sources)
        found_any = any(s and s.found for s in sources.values())
        return 1.5 if found_any else None

    prestige = dictionaries.get("prestige_awards") or {}
    major = [a.lower() for a in prestige.get("major") or []]
    notable = [a.lower() for a in prestige.get("notable") or []]

    score = 1.0
    major_hits = 0
    for a in major:
        if a in text:
            major_hits += 1
            score += 1.3
    for a in notable:
        if a in text:
            score += 0.45

    # OMDb often has "Won X Oscars. Another Y wins & Z nominations."
    import re

    m = re.search(r"won (\d+)", text)
    if m:
        score += min(3.0, int(m.group(1)) * 0.8)
    m = re.search(r"(\d+) wins?", text)
    if m:
        score += min(2.0, int(m.group(1)) * 0.15)
    m = re.search(r"(\d+) nominations?", text)
    if m:
        score += min(1.5, int(m.group(1)) * 0.05)

    if major_hits >= 2:
        score += 0.8

    return round(max(0.0, min(10.0, score)), 2)

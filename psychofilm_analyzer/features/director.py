"""Director / creator reputation for psychological depth (10%)."""

from __future__ import annotations

from typing import Any, Iterable

from psychofilm_analyzer.features.text_aggregate import all_directors
from psychofilm_analyzer.models import SourcePayload


def _norm(name: str) -> str:
    return " ".join(name.lower().replace("ё", "е").split())


def score_director_reputation(
    sources: dict[str, SourcePayload],
    dictionaries: dict[str, Any],
    director_hints: Iterable[str] | None = None,
) -> float | None:
    directors = all_directors(sources, director_hints)
    if not directors:
        return 3.0  # unknown creator — neutral-low

    creators = dictionaries.get("high_psych_creators") or {}
    known = [_norm(n) for n in (creators.get("directors") or []) + (creators.get("showrunners") or [])]
    known_set = set(known)

    score = 2.5
    matches = 0
    for d in directors:
        dn = _norm(d)
        if dn in known_set:
            matches += 1
            score += 3.5
            continue
        # partial match (surname)
        parts = dn.split()
        surname = parts[-1] if parts else dn
        if len(surname) > 3 and any(surname == k.split()[-1] or surname in k for k in known):
            matches += 1
            score += 2.5

    if matches >= 2:
        score += 1.0

    return round(max(0.0, min(10.0, score)), 2)

"""Aggregate free-text and structured signals from all sources."""

from __future__ import annotations

from typing import Iterable

from psychofilm_analyzer.models import SourcePayload


def all_text(sources: dict[str, SourcePayload]) -> str:
    chunks: list[str] = []
    for s in sources.values():
        if not s or not s.found:
            continue
        for field in (s.overview, s.plot, s.awards_text):
            if field:
                chunks.append(field)
        chunks.extend(s.genres or [])
        chunks.extend(s.keywords or [])
        chunks.extend(s.tags or [])
        if s.extra:
            if s.extra.get("combined_text"):
                chunks.append(str(s.extra["combined_text"]))
            if s.extra.get("tagline"):
                chunks.append(str(s.extra["tagline"]))
            if s.extra.get("slogan"):
                chunks.append(str(s.extra["slogan"]))
    return "\n".join(chunks).lower()


def all_keywords(sources: dict[str, SourcePayload]) -> list[str]:
    out: list[str] = []
    seen = set()
    for s in sources.values():
        if not s or not s.found:
            continue
        for k in list(s.keywords or []) + list(s.tags or []) + list(s.genres or []):
            key = k.strip().lower()
            if key and key not in seen:
                seen.add(key)
                out.append(k.strip())
    return out


def all_genres(sources: dict[str, SourcePayload]) -> list[str]:
    """Union of genres/tags from every source (EN + RU + wiki/letterboxd)."""
    out: list[str] = []
    seen = set()
    for s in sources.values():
        if not s or not s.found:
            continue
        for g in list(s.genres or []) + list(s.genres_en or []) + list(s.genres_ru or []) + list(
            s.tags or []
        ):
            key = g.strip().lower()
            if key and key not in seen:
                seen.add(key)
                out.append(g.strip())
    return out


def all_genre_like_terms(sources: dict[str, SourcePayload]) -> list[str]:
    """Genres + thematic keywords that behave as genre/style signals."""
    base = all_genres(sources)
    seen = {g.lower() for g in base}
    out = list(base)
    genre_ish = (
        "psychological",
        "psycho",
        "noir",
        "neo-noir",
        "surreal",
        "avant-garde",
        "arthouse",
        "experimental",
        "mystery",
        "thriller",
        "drama",
        "horror",
        "comedy",
        "crime",
        "romance",
        "war",
        "biography",
        "documentary",
        "fantasy",
        "sci-fi",
        "science fiction",
        "action",
        "adventure",
        "identity",
        "trauma",
        "dream",
        "nightmare",
        "madness",
        "insane",
        "psychiatric",
        "existential",
        "coming of age",
        "family",
        "political",
        "satire",
        "melodrama",
        "detective",
        "триллер",
        "драма",
        "детектив",
        "ужасы",
        "комедия",
        "мелодрама",
        "военный",
        "биография",
        "фантастика",
        "фэнтези",
        "психолог",
        "сюрреал",
        "нуар",
    )
    for s in sources.values():
        if not s or not s.found:
            continue
        for k in list(s.keywords or []) + list(s.tags or []):
            kl = k.strip().lower()
            if not kl or kl in seen:
                continue
            if any(sig in kl for sig in genre_ish):
                seen.add(kl)
                out.append(k.strip())
    return out


def all_directors(sources: dict[str, SourcePayload], hints: Iterable[str] | None = None) -> list[str]:
    out: list[str] = []
    seen = set()
    for name in list(hints or []):
        if name and name.strip().lower() not in seen:
            seen.add(name.strip().lower())
            out.append(name.strip())
    for s in sources.values():
        if not s or not s.found:
            continue
        for d in list(s.directors or []) + list(s.creators or []):
            if d and d.strip().lower() not in seen:
                seen.add(d.strip().lower())
                out.append(d.strip())
    return out


def all_awards(sources: dict[str, SourcePayload]) -> list[str]:
    out: list[str] = []
    for s in sources.values():
        if not s or not s.found:
            continue
        out.extend(s.awards or [])
        if s.awards_text:
            out.append(s.awards_text)
    return out


def best_rating(sources: dict[str, SourcePayload], *source_names: str) -> float | None:
    for name in source_names:
        s = sources.get(name)
        if s and s.found and s.rating is not None:
            return float(s.rating)
    return None


def count_found_sources(sources: dict[str, SourcePayload]) -> int:
    return sum(1 for s in sources.values() if s and s.found)

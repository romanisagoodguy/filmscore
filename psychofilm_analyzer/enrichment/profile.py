"""Build gather-only EnrichmentProfile with named evidence bags."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from psychofilm_analyzer.features.metadata import assemble_metadata, build_film_uid
from psychofilm_analyzer.models import EnrichedResult, InputTitle, MediaType, SourcePayload
from psychofilm_analyzer.utils.text import safe_int


def _join(values: list[str] | None, sep: str = "; ") -> Optional[str]:
    if not values:
        return None
    cleaned = [v.strip() for v in values if v and str(v).strip()]
    return sep.join(cleaned) if cleaned else None


def _looks_cyrillic(text: str) -> bool:
    if not text:
        return False
    cyr = len(re.findall(r"[А-Яа-яЁё]", text))
    lat = len(re.findall(r"[A-Za-z]", text))
    return cyr > lat


@dataclass
class EvidenceBag:
    """One named text bag with provenance (fuel for later scoring)."""

    name: str
    text: str
    source: str
    language: str
    weight: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def char_len(self) -> int:
        return len(self.text or "")

    @property
    def word_count(self) -> int:
        return len(re.findall(r"\b\w+\b", self.text or "", flags=re.UNICODE))


@dataclass
class EnrichmentProfile:
    """Multi-source film profile without psych scores or clusters."""

    input: InputTitle
    film_uid: Optional[str] = None
    sources: dict[str, SourcePayload] = field(default_factory=dict)
    bags: list[EvidenceBag] = field(default_factory=list)

    # Identity
    input_id: Optional[str] = None
    imdb_id: Optional[str] = None
    tmdb_id: Optional[int] = None
    kinopoisk_id: Optional[int] = None
    title_en: Optional[str] = None
    title_ru: Optional[str] = None
    title_original: Optional[str] = None
    year: Optional[int] = None
    media_type: str = "unknown"
    content_type: str = "feature"  # feature | animation | series | season | documentary
    season: Optional[int] = None
    runtime_min: Optional[int] = None
    countries_en: list[str] = field(default_factory=list)
    countries_ru: list[str] = field(default_factory=list)

    # Text (language-separated)
    plot_en: Optional[str] = None
    plot_ru: Optional[str] = None
    overview_en: Optional[str] = None
    overview_ru: Optional[str] = None
    genres: list[str] = field(default_factory=list)
    genres_en: list[str] = field(default_factory=list)
    genres_ru: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)

    # Crew
    directors_en: list[str] = field(default_factory=list)
    directors_ru: list[str] = field(default_factory=list)
    writers_en: list[str] = field(default_factory=list)
    writers_ru: list[str] = field(default_factory=list)
    composers_en: list[str] = field(default_factory=list)
    composers_ru: list[str] = field(default_factory=list)
    actors_en: list[str] = field(default_factory=list)
    actors_ru: list[str] = field(default_factory=list)

    # Ratings / awards / links
    imdb_rating: Optional[float] = None
    kinopoisk_rating: Optional[float] = None
    tmdb_rating: Optional[float] = None
    awards_text: Optional[str] = None
    link_imdb: Optional[str] = None
    link_tmdb: Optional[str] = None
    link_kinopoisk: Optional[str] = None
    link_wikipedia_en: Optional[str] = None
    link_wikipedia_ru: Optional[str] = None
    link_wikipedia_de: Optional[str] = None
    link_letterboxd: Optional[str] = None

    # Coverage / type priors (gather-time only; not psych scores)
    coverage: dict[str, Any] = field(default_factory=dict)
    type_flags: dict[str, bool] = field(default_factory=dict)
    error: Optional[str] = None

    def bag_summary(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for b in self.bags:
            key = f"{b.name}@{b.source}"
            out[key] = {
                "name": b.name,
                "source": b.source,
                "language": b.language,
                "weight": b.weight,
                "chars": b.char_len,
                "words": b.word_count,
                "preview": (b.text or "")[:240],
            }
        return out

    def to_flat_dict(self) -> dict[str, Any]:
        bags = self.bag_summary()
        bag_names = sorted({b.name for b in self.bags})
        return {
            # Imported provenance first
            "imported_file": self.input.source_file,
            "imported_sheet": self.input.source_sheet,
            "imported_row": self.input.source_row,
            "imported_title": self.input.import_title or self.input.title,
            "imported_year": self.input.import_year
            if self.input.import_year is not None
            else self.input.year,
            # Identity
            "film_uid": self.film_uid,
            "input_id": self.input_id or self.input.input_id,
            "imdb_id": self.imdb_id,
            "tmdb_id": self.tmdb_id,
            "kinopoisk_id": self.kinopoisk_id,
            "title_en": self.title_en,
            "title_ru": self.title_ru,
            "title_original": self.title_original,
            "year": self.year,
            "media_type": self.media_type,
            "content_type": self.content_type,
            "season": self.season,
            "runtime_min": self.runtime_min,
            "countries_en": _join(self.countries_en),
            "countries_ru": _join(self.countries_ru),
            # Evidence texts
            "plot_en": self.plot_en,
            "plot_ru": self.plot_ru,
            "overview_en": self.overview_en,
            "overview_ru": self.overview_ru,
            "genres": _join(self.genres),
            "genres_en": _join(self.genres_en),
            "genres_ru": _join(self.genres_ru),
            "keywords": _join(self.keywords[:80]),
            "keywords_count": len(self.keywords),
            # Crew
            "directors_en": _join(self.directors_en),
            "directors_ru": _join(self.directors_ru),
            "writers_en": _join(self.writers_en),
            "writers_ru": _join(self.writers_ru),
            "composers_en": _join(self.composers_en),
            "composers_ru": _join(self.composers_ru),
            "actors_en": _join(self.actors_en),
            "actors_ru": _join(self.actors_ru),
            # Ratings
            "imdb_rating": self.imdb_rating,
            "kinopoisk_rating": self.kinopoisk_rating,
            "tmdb_rating": self.tmdb_rating,
            "awards_text": self.awards_text,
            # Links (one per column)
            "link_imdb": self.link_imdb,
            "link_tmdb": self.link_tmdb,
            "link_kinopoisk": self.link_kinopoisk,
            "link_wikipedia_en": self.link_wikipedia_en,
            "link_wikipedia_ru": self.link_wikipedia_ru,
            "link_wikipedia_de": self.link_wikipedia_de,
            "link_letterboxd": self.link_letterboxd,
            # Coverage
            "cov_tmdb": self.coverage.get("tmdb"),
            "cov_omdb": self.coverage.get("omdb"),
            "cov_kinopoisk": self.coverage.get("kinopoisk"),
            "cov_wikipedia": self.coverage.get("wikipedia"),
            "cov_letterboxd": self.coverage.get("letterboxd"),
            "cov_plot_en": self.coverage.get("has_plot_en"),
            "cov_plot_ru": self.coverage.get("has_plot_ru"),
            "cov_keywords_n": self.coverage.get("keywords_n"),
            "cov_bags": "; ".join(bag_names),
            "cov_sources_found": self.coverage.get("sources_found"),
            # Type priors (boolean flags only — not psych scores)
            "flag_spectacle": self.type_flags.get("is_spectacle"),
            "flag_arthouse": self.type_flags.get("is_arthouse"),
            "flag_animation": self.type_flags.get("is_animation"),
            "flag_documentary": self.type_flags.get("is_documentary"),
            "flag_series": self.type_flags.get("is_series"),
            "error": self.error,
            # Previews of bags for human review
            "bag_plot_en_preview": self._preview("plot_en"),
            "bag_plot_ru_preview": self._preview("plot_ru"),
            "bag_keywords_en_preview": self._preview("keywords_en"),
        }

    def _preview(self, bag_name: str, n: int = 300) -> Optional[str]:
        parts = [b.text for b in self.bags if b.name == bag_name and b.text]
        if not parts:
            return None
        text = " | ".join(parts)
        return text[:n] + ("…" if len(text) > n else "")

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "imported": {
                "file": self.input.source_file,
                "sheet": self.input.source_sheet,
                "row": self.input.source_row,
                "title": self.input.import_title or self.input.title,
                "year": self.input.import_year
                if self.input.import_year is not None
                else self.input.year,
            },
            "film_uid": self.film_uid,
            "ids": {
                "input_id": self.input_id or self.input.input_id,
                "imdb_id": self.imdb_id,
                "tmdb_id": self.tmdb_id,
                "kinopoisk_id": self.kinopoisk_id,
            },
            "titles": {
                "en": self.title_en,
                "ru": self.title_ru,
                "original": self.title_original,
            },
            "year": self.year,
            "media_type": self.media_type,
            "content_type": self.content_type,
            "season": self.season,
            "runtime_min": self.runtime_min,
            "countries": {"en": self.countries_en, "ru": self.countries_ru},
            "plots": {"en": self.plot_en, "ru": self.plot_ru},
            "overviews": {"en": self.overview_en, "ru": self.overview_ru},
            "genres": {"all": self.genres, "en": self.genres_en, "ru": self.genres_ru},
            "keywords": self.keywords,
            "crew": {
                "directors_en": self.directors_en,
                "directors_ru": self.directors_ru,
                "writers_en": self.writers_en,
                "writers_ru": self.writers_ru,
                "composers_en": self.composers_en,
                "composers_ru": self.composers_ru,
            },
            "cast": {"actors_en": self.actors_en, "actors_ru": self.actors_ru},
            "ratings": {
                "imdb": self.imdb_rating,
                "kinopoisk": self.kinopoisk_rating,
                "tmdb": self.tmdb_rating,
            },
            "awards_text": self.awards_text,
            "links": {
                "imdb": self.link_imdb,
                "tmdb": self.link_tmdb,
                "kinopoisk": self.link_kinopoisk,
                "wikipedia_en": self.link_wikipedia_en,
                "wikipedia_ru": self.link_wikipedia_ru,
                "wikipedia_de": self.link_wikipedia_de,
                "letterboxd": self.link_letterboxd,
            },
            "evidence_bags": [b.to_dict() for b in self.bags],
            "bag_summary": self.bag_summary(),
            "coverage": self.coverage,
            "type_flags": self.type_flags,
            "sources": {k: v.to_dict() for k, v in self.sources.items()},
            "error": self.error,
            "note": "Gather-only profile. No psych scores or clusters assigned.",
        }


def _add_bag(
    bags: list[EvidenceBag],
    name: str,
    text: Optional[str],
    source: str,
    language: str,
    weight: float,
) -> None:
    if not text or not str(text).strip():
        return
    t = str(text).strip()
    # skip tiny noise
    if len(t) < 8:
        return
    bags.append(
        EvidenceBag(name=name, text=t, source=source, language=language, weight=weight)
    )


def _detect_content_type(result: EnrichedResult, sources: dict[str, SourcePayload]) -> str:
    genres = " ".join(g.lower() for g in (result.genres or []))
    if result.media_type in {MediaType.SERIES.value, MediaType.SEASON.value, "series", "season"}:
        if result.season:
            return "season"
        return "series"
    if "animation" in genres or "мульт" in genres or "anime" in genres:
        return "animation"
    if "documentary" in genres or "документал" in genres:
        return "documentary"
    for s in sources.values():
        if s and s.found and s.type and "tv" in str(s.type).lower():
            return "series"
    return "feature"


def _type_flags(result: EnrichedResult, keywords: list[str]) -> dict[str, bool]:
    g = " ".join(x.lower() for x in (result.genres or []) + (result.genres_en or []))
    kw = " ".join(k.lower() for k in keywords)
    blob = g + " " + kw
    spectacle_markers = (
        "superhero",
        "action",
        "adventure",
        "spy",
        "martial arts",
        "comic book",
        "боевик",
        "супергерой",
    )
    arthouse_markers = (
        "arthouse",
        "art house",
        "character study",
        "slow burn",
        "surreal",
        "experimental",
        "psychological",
        "драма",
    )
    is_spectacle = any(m in blob for m in spectacle_markers) and not any(
        m in blob for m in ("psychological drama", "character study", "arthouse")
    )
    is_arthouse = any(m in blob for m in arthouse_markers)
    is_animation = "animation" in g or "мульт" in g
    is_doc = "documentary" in g or "документал" in g
    is_series = result.media_type in {"series", "season"}
    return {
        "is_spectacle": bool(is_spectacle),
        "is_arthouse": bool(is_arthouse),
        "is_animation": bool(is_animation),
        "is_documentary": bool(is_doc),
        "is_series": bool(is_series),
    }


def build_enrichment_profile(
    item: InputTitle,
    sources: dict[str, SourcePayload],
) -> EnrichmentProfile:
    """
    Assemble identity + evidence bags from source payloads.
    Does NOT assign psych scores or clusters.
    """
    # Reuse metadata assembly for IDs, titles, plots, crew, links
    shell = EnrichedResult(input=item, sources=sources)
    assemble_metadata(shell, item, sources)

    bags: list[EvidenceBag] = []
    tmdb = sources.get("tmdb")
    omdb = sources.get("omdb")
    kp = sources.get("kinopoisk")
    wiki = sources.get("wikipedia")
    lb = sources.get("letterboxd")

    # --- plot / overview bags ---
    if omdb and omdb.found:
        _add_bag(bags, "plot_en", omdb.plot_en or omdb.plot or omdb.overview, "omdb", "en", 1.0)
    if tmdb and tmdb.found:
        _add_bag(bags, "plot_en", tmdb.overview_en or tmdb.plot_en or tmdb.overview, "tmdb", "en", 1.0)
        _add_bag(bags, "plot_ru", tmdb.overview_ru or tmdb.plot_ru, "tmdb", "ru", 1.0)
    if kp and kp.found:
        _add_bag(bags, "plot_ru", kp.plot_ru or kp.plot or kp.overview, "kinopoisk", "ru", 1.0)
    if wiki and wiki.found:
        _add_bag(bags, "plot_en", wiki.overview_en or (wiki.overview if wiki.language == "en" else None), "wikipedia", "en", 0.9)
        _add_bag(bags, "plot_ru", wiki.overview_ru, "wikipedia", "ru", 0.9)
        if wiki.awards_text:
            _add_bag(bags, "awards_en", wiki.awards_text, "wikipedia", "en", 0.5)

    # --- keywords / genres ---
    if tmdb and tmdb.found and tmdb.keywords:
        _add_bag(bags, "keywords_en", "; ".join(tmdb.keywords), "tmdb", "en", 1.2)
    if lb and lb.found and (lb.keywords or lb.tags):
        _add_bag(
            bags,
            "keywords_en",
            "; ".join((lb.keywords or []) + (lb.tags or [])),
            "letterboxd",
            "en",
            1.1,
        )
    if shell.genres_en:
        _add_bag(bags, "genres_en", "; ".join(shell.genres_en), "merged", "en", 0.8)
    if shell.genres_ru:
        _add_bag(bags, "genres_ru", "; ".join(shell.genres_ru), "merged", "ru", 0.8)

    if omdb and omdb.found and omdb.awards_text:
        _add_bag(bags, "awards_en", omdb.awards_text, "omdb", "en", 0.5)

    # credits context (names only — weak bag)
    dirs = shell.directors_en or shell.directors or []
    if dirs:
        _add_bag(bags, "credits_context", "Directors: " + "; ".join(dirs), "merged", "en", 0.6)
    comps = shell.composers_en or []
    if comps:
        _add_bag(bags, "credits_context", "Composers: " + "; ".join(comps), "merged", "en", 0.5)

    # imported hints
    hints = []
    if item.genre_hint:
        hints.append(f"genre:{item.genre_hint}")
    if item.notes:
        hints.append(f"notes:{item.notes}")
    if item.country:
        hints.append(f"country:{item.country}")
    if hints:
        lang = "ru" if _looks_cyrillic(" ".join(hints)) else "en"
        _add_bag(bags, "imported_hints", " | ".join(hints), "import", lang, 0.4)

    # ratings from sources
    imdb_rating = omdb.rating if omdb and omdb.found else item.imdb_rating_hint
    kp_rating = kp.rating if kp and kp.found else item.kinopoisk_rating_hint
    tmdb_rating = tmdb.rating if tmdb and tmdb.found else None

    content_type = _detect_content_type(shell, sources)
    keywords = shell.keywords or []
    flags = _type_flags(shell, keywords)

    sources_found = [name for name, s in sources.items() if s and s.found]
    coverage = {
        "tmdb": bool(tmdb and tmdb.found),
        "omdb": bool(omdb and omdb.found),
        "kinopoisk": bool(kp and kp.found),
        "wikipedia": bool(wiki and wiki.found),
        "letterboxd": bool(lb and lb.found),
        "has_plot_en": any(b.name == "plot_en" for b in bags),
        "has_plot_ru": any(b.name == "plot_ru" for b in bags),
        "keywords_n": len(keywords),
        "sources_found": len(sources_found),
        "sources_list": sources_found,
        "bags_n": len(bags),
    }

    film_uid = shell.film_uid or build_film_uid(
        imdb_id=shell.imdb_id,
        tmdb_id=shell.tmdb_id,
        kinopoisk_id=shell.kinopoisk_id,
        title_en=shell.title_en,
        year=shell.year,
        media_type=shell.media_type,
        season=shell.season,
    )

    return EnrichmentProfile(
        input=item,
        film_uid=film_uid,
        sources=sources,
        bags=bags,
        input_id=shell.input_id or item.input_id,
        imdb_id=shell.imdb_id,
        tmdb_id=shell.tmdb_id,
        kinopoisk_id=shell.kinopoisk_id,
        title_en=shell.title_en,
        title_ru=shell.title_ru,
        title_original=shell.title_original,
        year=shell.year,
        media_type=shell.media_type,
        content_type=content_type,
        season=shell.season,
        runtime_min=shell.runtime_min,
        countries_en=shell.countries_en,
        countries_ru=shell.countries_ru,
        plot_en=shell.plot_en,
        plot_ru=shell.plot_ru,
        overview_en=shell.overview_en,
        overview_ru=shell.overview_ru,
        genres=shell.genres,
        genres_en=shell.genres_en,
        genres_ru=shell.genres_ru,
        keywords=keywords,
        directors_en=shell.directors_en,
        directors_ru=shell.directors_ru,
        writers_en=shell.writers_en,
        writers_ru=shell.writers_ru,
        composers_en=shell.composers_en,
        composers_ru=shell.composers_ru,
        actors_en=shell.actors_en,
        actors_ru=shell.actors_ru,
        imdb_rating=imdb_rating,
        kinopoisk_rating=kp_rating,
        tmdb_rating=tmdb_rating,
        awards_text=shell.awards_text,
        link_imdb=shell.link_imdb,
        link_tmdb=shell.link_tmdb,
        link_kinopoisk=shell.link_kinopoisk,
        link_wikipedia_en=shell.link_wikipedia_en,
        link_wikipedia_ru=shell.link_wikipedia_ru,
        link_wikipedia_de=shell.link_wikipedia_de,
        link_letterboxd=shell.link_letterboxd,
        coverage=coverage,
        type_flags=flags,
    )

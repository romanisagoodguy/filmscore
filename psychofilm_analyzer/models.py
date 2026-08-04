"""Domain models for PsychoFilm Analyzer."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional


class MediaType(str, Enum):
    FILM = "film"
    SERIES = "series"
    SEASON = "season"
    UNKNOWN = "unknown"


class Confidence(str, Enum):
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"


CLUSTER_NAMES = [
    "Adolescence & Identity Formation",
    "Childhood / Transgenerational Trauma",
    "Madness, Psychosis & Borderline States",
    "Jungian Shadow, Persona & Individuation",
    "Family Systems, Attachment & Parental Complexes",
    "Existential Crisis, Meaning, Death & Midlife",
    "Collective Unconscious, Power & Historical Psychotypes",
]


def _join(values: list[str] | None, sep: str = "; ") -> Optional[str]:
    if not values:
        return None
    cleaned = [v.strip() for v in values if v and str(v).strip()]
    return sep.join(cleaned) if cleaned else None


@dataclass
class InputTitle:
    """Normalized input row from Excel/CSV/list."""

    title: str
    year: Optional[int] = None
    media_type: MediaType = MediaType.UNKNOWN
    season: Optional[int] = None
    original_language: Optional[str] = None
    existing_rating: Optional[float] = None
    notes: Optional[str] = None
    english_title: Optional[str] = None
    russian_title: Optional[str] = None
    genre_hint: Optional[str] = None
    director_hint: Optional[str] = None
    imdb_rating_hint: Optional[float] = None
    kinopoisk_rating_hint: Optional[float] = None
    country: Optional[str] = None
    # IDs from the user's original spreadsheet / list (never drop these)
    input_id: Optional[str] = None
    imdb_id_hint: Optional[str] = None
    tmdb_id_hint: Optional[int] = None
    kinopoisk_id_hint: Optional[int] = None
    actors_hint: Optional[str] = None
    # Provenance: where this title came from in the user's list
    source_file: Optional[str] = None  # file name of import
    source_sheet: Optional[str] = None  # Excel sheet name (if applicable)
    source_row: Optional[int] = None  # 1-based row number in that sheet/file
    import_title: Optional[str] = None  # title as written in the import file
    import_year: Optional[int] = None  # year as written in the import file
    collection: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)

    def display_title(self) -> str:
        if self.media_type == MediaType.SEASON and self.season:
            return f"{self.title} S{self.season:02d}"
        return self.title

    def cache_key(self) -> str:
        parts = [
            (self.english_title or self.title or "").strip().lower(),
            str(self.year or ""),
            self.media_type.value,
            str(self.season or ""),
        ]
        return "|".join(parts)


@dataclass
class SourcePayload:
    """Raw enrichment payload from one data source."""

    source: str
    found: bool = False
    title: Optional[str] = None
    title_en: Optional[str] = None
    title_ru: Optional[str] = None
    original_title: Optional[str] = None
    year: Optional[int] = None
    overview: Optional[str] = None
    overview_en: Optional[str] = None
    overview_ru: Optional[str] = None
    plot: Optional[str] = None
    plot_en: Optional[str] = None
    plot_ru: Optional[str] = None
    genres: list[str] = field(default_factory=list)
    genres_en: list[str] = field(default_factory=list)
    genres_ru: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    directors: list[str] = field(default_factory=list)
    directors_en: list[str] = field(default_factory=list)
    directors_ru: list[str] = field(default_factory=list)
    writers: list[str] = field(default_factory=list)
    writers_en: list[str] = field(default_factory=list)
    writers_ru: list[str] = field(default_factory=list)
    composers: list[str] = field(default_factory=list)
    composers_en: list[str] = field(default_factory=list)
    composers_ru: list[str] = field(default_factory=list)
    creators: list[str] = field(default_factory=list)
    cast: list[str] = field(default_factory=list)
    cast_en: list[str] = field(default_factory=list)
    cast_ru: list[str] = field(default_factory=list)
    rating: Optional[float] = None
    votes: Optional[int] = None
    awards_text: Optional[str] = None
    awards: list[str] = field(default_factory=list)
    runtime: Optional[int] = None
    type: Optional[str] = None
    imdb_id: Optional[str] = None
    tmdb_id: Optional[int] = None
    kinopoisk_id: Optional[int] = None
    url: Optional[str] = None
    language: Optional[str] = None
    popularity: Optional[float] = None
    extra: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FactorScores:
    thematic_keyword_density: Optional[float] = None
    narrative_character_depth: Optional[float] = None
    awards_prestige: Optional[float] = None
    critical_intellectual_discourse: Optional[float] = None
    director_creator_reputation: Optional[float] = None
    discussability_podcast: Optional[float] = None

    def as_dict(self) -> dict[str, Optional[float]]:
        return asdict(self)

    def available(self) -> dict[str, float]:
        return {k: float(v) for k, v in asdict(self).items() if v is not None}


@dataclass
class EnrichedResult:
    """Final scored result for one title."""

    input: InputTitle
    psycho_score: float = 0.0
    primary_cluster: Optional[str] = None
    secondary_cluster: Optional[str] = None
    confidence: Confidence = Confidence.LOW
    description: str = ""
    description_en: str = ""
    factors: FactorScores = field(default_factory=FactorScores)
    cluster_scores: dict[str, float] = field(default_factory=dict)
    sources: dict[str, SourcePayload] = field(default_factory=dict)

    # Unique identity (keep input + all external DBs)
    film_uid: Optional[str] = None
    input_id: Optional[str] = None  # from original Excel/CSV if present
    imdb_id: Optional[str] = None
    tmdb_id: Optional[int] = None
    kinopoisk_id: Optional[int] = None

    # Titles (language-separated)
    title_input: Optional[str] = None
    title_en: Optional[str] = None
    title_ru: Optional[str] = None
    title_original: Optional[str] = None
    normalized_title_en: Optional[str] = None  # alias compatibility
    normalized_title_ru: Optional[str] = None

    year: Optional[int] = None
    media_type: str = "unknown"
    season: Optional[int] = None
    runtime_min: Optional[int] = None
    countries: list[str] = field(default_factory=list)
    countries_en: list[str] = field(default_factory=list)
    countries_ru: list[str] = field(default_factory=list)

    # Plots / overviews (language-separated)
    plot_en: Optional[str] = None
    plot_ru: Optional[str] = None
    overview_en: Optional[str] = None
    overview_ru: Optional[str] = None

    # Genres / keywords
    genres: list[str] = field(default_factory=list)
    genres_en: list[str] = field(default_factory=list)
    genres_ru: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)

    # Crew / cast (language-separated)
    directors: list[str] = field(default_factory=list)
    directors_en: list[str] = field(default_factory=list)
    directors_ru: list[str] = field(default_factory=list)
    writers_en: list[str] = field(default_factory=list)
    writers_ru: list[str] = field(default_factory=list)
    composers_en: list[str] = field(default_factory=list)
    composers_ru: list[str] = field(default_factory=list)
    actors_en: list[str] = field(default_factory=list)
    actors_ru: list[str] = field(default_factory=list)

    # Ratings
    imdb_rating: Optional[float] = None
    kinopoisk_rating: Optional[float] = None
    tmdb_rating: Optional[float] = None

    # External links (separate fields)
    link_imdb: Optional[str] = None
    link_tmdb: Optional[str] = None
    link_kinopoisk: Optional[str] = None
    link_wikipedia_en: Optional[str] = None
    link_wikipedia_ru: Optional[str] = None
    link_wikipedia_de: Optional[str] = None
    link_letterboxd: Optional[str] = None
    source_links: list[str] = field(default_factory=list)

    awards_text: Optional[str] = None
    caps_applied: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    error: Optional[str] = None

    def to_flat_dict(self) -> dict[str, Any]:
        """One Excel-friendly row: import provenance first, then enrichment columns."""
        f = self.factors.as_dict()
        return {
            # --- Imported provenance (always first; prefix imported_) ---
            "imported_file": self.input.source_file,
            "imported_sheet": self.input.source_sheet,
            "imported_row": self.input.source_row,
            "imported_title": self.input.import_title or self.input.title or self.title_input,
            "imported_year": self.input.import_year if self.input.import_year is not None else self.input.year,
            # --- Identity / enrichment ---
            "film_uid": self.film_uid,
            "input_id": self.input_id or self.input.input_id,
            "imdb_id": self.imdb_id,
            "tmdb_id": self.tmdb_id,
            "kinopoisk_id": self.kinopoisk_id,
            # Titles (resolved from databases)
            "title_input": self.title_input or self.input.display_title(),
            "title_en": self.title_en or self.normalized_title_en,
            "title_ru": self.title_ru or self.normalized_title_ru,
            "title_original": self.title_original,
            "year": self.year or self.input.year,
            "season": self.season or self.input.season,
            "media_type": self.media_type,
            "runtime_min": self.runtime_min,
            # Countries EN / RU separate
            "countries_en": _join(self.countries_en),
            "countries_ru": _join(self.countries_ru),
            # Plots (EN / RU separate)
            "plot_en": self.plot_en or self.overview_en,
            "plot_ru": self.plot_ru or self.overview_ru,
            "overview_en": self.overview_en,
            "overview_ru": self.overview_ru,
            # Genres
            "genres": _join(self.genres),
            "genres_en": _join(self.genres_en),
            "genres_ru": _join(self.genres_ru),
            "keywords": _join(self.keywords[:80]),
            # Crew / cast (EN / RU separate)
            "directors_en": _join(self.directors_en) or _join(self.directors),
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
            # External website links — ONE URL PER COLUMN (never concatenated)
            "link_imdb": self.link_imdb,
            "link_tmdb": self.link_tmdb,
            "link_kinopoisk": self.link_kinopoisk,
            "link_wikipedia_en": self.link_wikipedia_en,
            "link_wikipedia_ru": self.link_wikipedia_ru,
            "link_wikipedia_de": self.link_wikipedia_de,
            "link_letterboxd": self.link_letterboxd,
            # Awards
            "awards_text": self.awards_text,
            # Psycho scoring
            "psycho_score": round(self.psycho_score, 2),
            "primary_cluster": self.primary_cluster,
            "secondary_cluster": self.secondary_cluster,
            "confidence": self.confidence.value,
            "description_en": self.description_en or self.description,
            "factor_thematic": f.get("thematic_keyword_density"),
            "factor_narrative": f.get("narrative_character_depth"),
            "factor_awards": f.get("awards_prestige"),
            "factor_discourse": f.get("critical_intellectual_discourse"),
            "factor_director": f.get("director_creator_reputation"),
            "factor_discussability": f.get("discussability_podcast"),
            "caps_applied": _join(self.caps_applied),
            "collection": self.input.collection,
            "error": self.error,
            # convenience (same single film row)
            "title": self.title_en or self.input.display_title(),
            "directors": _join(self.directors_en) or _join(self.directors_ru) or _join(self.directors),
        }

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "imported": {
                "file": self.input.source_file,
                "sheet": self.input.source_sheet,
                "row": self.input.source_row,
                "title": self.input.import_title or self.input.title,
                "year": self.input.import_year if self.input.import_year is not None else self.input.year,
            },
            "input": asdict(self.input),
            "film_uid": self.film_uid,
            "ids": {
                "input_id": self.input_id or self.input.input_id,
                "imdb_id": self.imdb_id,
                "tmdb_id": self.tmdb_id,
                "kinopoisk_id": self.kinopoisk_id,
            },
            "titles": {
                "input": self.title_input,
                "en": self.title_en,
                "ru": self.title_ru,
                "original": self.title_original,
            },
            "year": self.year,
            "season": self.season,
            "media_type": self.media_type,
            "runtime_min": self.runtime_min,
            "countries": {"en": self.countries_en, "ru": self.countries_ru, "all": self.countries},
            "plots": {"en": self.plot_en, "ru": self.plot_ru},
            "overviews": {"en": self.overview_en, "ru": self.overview_ru},
            "genres": {"en": self.genres_en, "ru": self.genres_ru, "all": self.genres},
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
            "links": {
                "imdb": self.link_imdb,
                "tmdb": self.link_tmdb,
                "kinopoisk": self.link_kinopoisk,
                "wikipedia_en": self.link_wikipedia_en,
                "wikipedia_ru": self.link_wikipedia_ru,
                "wikipedia_de": self.link_wikipedia_de,
                "letterboxd": self.link_letterboxd,
            },
            "awards_text": self.awards_text,
            "psycho_score": round(self.psycho_score, 2),
            "primary_cluster": self.primary_cluster,
            "secondary_cluster": self.secondary_cluster,
            "confidence": self.confidence.value,
            "description_en": self.description_en or self.description,
            "factors": self.factors.as_dict(),
            "cluster_scores": self.cluster_scores,
            "caps_applied": self.caps_applied,
            "notes": self.notes,
            "sources": {k: v.to_dict() for k, v in self.sources.items()},
            "error": self.error,
            # compatibility
            "normalized_title_en": self.title_en,
            "normalized_title_ru": self.title_ru,
            "directors": self.directors_en or self.directors,
        }

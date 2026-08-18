"""Assemble unique film identity and bilingual metadata from sources."""

from __future__ import annotations

import re
from typing import Iterable, Optional

from psychofilm_analyzer.models import EnrichedResult, InputTitle, SourcePayload
from psychofilm_analyzer.utils.text import slugify_key


def _looks_cyrillic(text: str) -> bool:
    if not text:
        return False
    cyr = len(re.findall(r"[А-Яа-яЁё]", text))
    lat = len(re.findall(r"[A-Za-z]", text))
    return cyr > lat


def _uniq(seq: Iterable[str], limit: int = 20) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in seq:
        if not item:
            continue
        name = str(item).strip()
        if not name:
            continue
        key = re.sub(r"\s+", " ", name).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
        if len(out) >= limit:
            break
    return out


def _names_from_source(
    s: SourcePayload,
    *,
    en_attrs: tuple[str, ...],
    ru_attrs: tuple[str, ...],
    fallback_attrs: tuple[str, ...] = (),
) -> tuple[list[str], list[str]]:
    en: list[str] = []
    ru: list[str] = []
    for attr in en_attrs:
        en.extend(getattr(s, attr, None) or [])
    for attr in ru_attrs:
        ru.extend(getattr(s, attr, None) or [])
    # Language-tagged fallback lists
    for attr in fallback_attrs:
        vals = getattr(s, attr, None) or []
        for v in vals:
            if _looks_cyrillic(v):
                ru.append(v)
            else:
                en.append(v)
    # If source is explicitly Russian and only generic list filled
    if s.language == "ru" and not ru:
        for attr in fallback_attrs:
            ru.extend(getattr(s, attr, None) or [])
    if s.language in {None, "en"} and not en:
        for attr in fallback_attrs:
            for v in getattr(s, attr, None) or []:
                if not _looks_cyrillic(v):
                    en.append(v)
    return _uniq(en), _uniq(ru)


def build_film_uid(
    *,
    imdb_id: Optional[str],
    tmdb_id: Optional[int],
    kinopoisk_id: Optional[int],
    title_en: Optional[str],
    year: Optional[int],
    media_type: str,
    season: Optional[int],
) -> str:
    """Stable unique identity string for cross-run join."""
    if imdb_id:
        base = f"imdb:{imdb_id.strip()}"
    elif tmdb_id:
        base = f"tmdb:{tmdb_id}"
    elif kinopoisk_id:
        base = f"kp:{kinopoisk_id}"
    else:
        slug = slugify_key(title_en or "unknown")
        base = f"slug:{slug}:{year or 'na'}"
    if media_type in {"series", "season"} and season:
        return f"{base}:s{int(season):02d}"
    if media_type == "series":
        return f"{base}:series"
    return base


def assemble_metadata(result: EnrichedResult, item: InputTitle, sources: dict[str, SourcePayload]) -> None:
    """Fill language-separated fields, IDs, and external links on result."""
    tmdb = sources.get("tmdb")
    omdb = sources.get("omdb")
    kp = sources.get("kinopoisk")
    wiki = sources.get("wikipedia")
    lb = sources.get("letterboxd")

    # --- IDs: keep input IDs + merge every database id found ---
    result.input_id = item.input_id
    imdb_id = item.imdb_id_hint
    tmdb_id = item.tmdb_id_hint
    kp_id = item.kinopoisk_id_hint
    for s in (tmdb, omdb, kp, wiki, lb):
        if not s or not s.found:
            continue
        imdb_id = imdb_id or s.imdb_id
        tmdb_id = tmdb_id or s.tmdb_id
        kp_id = kp_id or s.kinopoisk_id
    result.imdb_id = imdb_id
    result.tmdb_id = tmdb_id
    result.kinopoisk_id = kp_id

    # --- Titles ---
    title_en = (
        (tmdb.title_en if tmdb and tmdb.found else None)
        or (tmdb.title if tmdb and tmdb.found and tmdb.language != "ru" else None)
        or (omdb.title if omdb and omdb.found else None)
        or (kp.title_en if kp and kp.found else None)
        or (kp.original_title if kp and kp.found else None)
        or item.english_title
    )
    title_ru = (
        (kp.title_ru if kp and kp.found else None)
        or (kp.title if kp and kp.found else None)
        or (tmdb.title_ru if tmdb and tmdb.found else None)
        or item.russian_title
    )
    # Prefer not to put Russian in EN column
    if title_en and _looks_cyrillic(title_en) and title_ru:
        title_en = item.english_title or (omdb.title if omdb and omdb.found else None)
    if title_ru and not _looks_cyrillic(title_ru) and item.russian_title:
        title_ru = item.russian_title

    title_original = (
        (tmdb.original_title if tmdb and tmdb.found else None)
        or (kp.original_title if kp and kp.found else None)
        or title_en
    )

    result.title_input = item.display_title()
    result.title_en = title_en or item.title
    result.title_ru = title_ru
    result.title_original = title_original
    result.normalized_title_en = result.title_en
    result.normalized_title_ru = result.title_ru

    result.year = (
        item.year
        or (tmdb.year if tmdb and tmdb.found else None)
        or (omdb.year if omdb and omdb.found else None)
        or (kp.year if kp and kp.found else None)
    )
    result.media_type = item.media_type.value
    result.season = item.season
    result.runtime_min = (
        (omdb.runtime if omdb and omdb.found else None)
        or (tmdb.runtime if tmdb and tmdb.found else None)
        or (kp.runtime if kp and kp.found else None)
    )

    # Countries — EN and RU in separate columns (never mixed into one field only)
    countries_en: list[str] = []
    countries_ru: list[str] = []
    if omdb and omdb.found and omdb.extra.get("country"):
        for c in str(omdb.extra["country"]).split(","):
            c = c.strip()
            if not c:
                continue
            if _looks_cyrillic(c):
                countries_ru.append(c)
            else:
                countries_en.append(c)
    if tmdb and tmdb.found:
        countries_en.extend(tmdb.extra.get("countries_en") or [])
        countries_ru.extend(tmdb.extra.get("countries_ru") or [])
    if kp and kp.found and kp.extra.get("countries"):
        for c in kp.extra["countries"] if isinstance(kp.extra["countries"], list) else []:
            c = str(c).strip()
            if not c:
                continue
            if _looks_cyrillic(c):
                countries_ru.append(c)
            else:
                countries_en.append(c)
    # Input spreadsheet country (often Russian list)
    if item.country:
        for c in re.split(r"[,;/|]", str(item.country)):
            c = c.strip()
            if not c:
                continue
            if _looks_cyrillic(c):
                countries_ru.append(c)
            else:
                countries_en.append(c)
    # Force script purity: EN = non-Cyrillic, RU = Cyrillic only
    result.countries_en = _uniq([c for c in countries_en + countries_ru if not _looks_cyrillic(c)], 20)
    result.countries_ru = _uniq([c for c in countries_ru + countries_en if _looks_cyrillic(c)], 20)
    result.countries = _uniq(result.countries_en + result.countries_ru, 30)

    # --- Plots / overviews ---
    plot_en = None
    plot_ru = None
    plot_de = None
    overview_en = None
    overview_ru = None
    overview_de = None
    if omdb and omdb.found:
        plot_en = omdb.plot_en or omdb.plot or omdb.overview
        overview_en = omdb.overview_en or omdb.overview or plot_en
    if tmdb and tmdb.found:
        overview_en = overview_en or tmdb.overview_en or (tmdb.overview if tmdb.language != "ru" else None)
        overview_ru = tmdb.overview_ru
        plot_en = plot_en or tmdb.plot_en or overview_en
        plot_ru = tmdb.plot_ru or overview_ru
    if kp and kp.found:
        plot_ru = plot_ru or kp.plot_ru or kp.plot or kp.overview
        overview_ru = overview_ru or kp.overview_ru or kp.overview or plot_ru
        if not plot_en and kp.plot_en:
            plot_en = kp.plot_en
    if wiki and wiki.found:
        langs = (wiki.extra or {}).get("langs") or []
        page_titles = (wiki.extra or {}).get("page_titles") or {}
        extra = wiki.extra or {}
        if wiki.overview_en or "en" in langs or wiki.language == "en":
            overview_en = overview_en or wiki.overview_en or wiki.overview
            plot_en = plot_en or wiki.plot_en or wiki.overview_en or wiki.overview
        if wiki.overview_ru or "ru" in langs:
            overview_ru = overview_ru or wiki.overview_ru
            plot_ru = plot_ru or wiki.plot_ru or wiki.overview_ru
            if not overview_ru:
                m = re.search(r"\[ru\]\s*(.+?)(?:\n\n\[|$)", extra.get("combined_text") or "", flags=re.S)
                if m:
                    ru_text = m.group(1).strip()
                    overview_ru = ru_text[:4000]
                    plot_ru = plot_ru or ru_text[:4000]
        de_text = wiki.overview_de or extra.get("overview_de") or wiki.plot_de
        if de_text or "de" in langs:
            overview_de = overview_de or de_text
            plot_de = plot_de or wiki.plot_de or de_text
        _ = page_titles

    result.plot_en = plot_en
    result.plot_ru = plot_ru
    result.plot_de = plot_de
    result.overview_en = overview_en or plot_en
    result.overview_ru = overview_ru or plot_ru
    result.overview_de = overview_de or plot_de

    # --- Genres (extensive: ALL sources; EN/RU are splits, not replacements) ---
    from psychofilm_analyzer.features.text_aggregate import all_genre_like_terms, all_genres

    genres_en: list[str] = []
    genres_ru: list[str] = []
    genres_all: list[str] = []

    # Pull every genre/tag field from every source (do not drop Wikipedia / Letterboxd)
    for s in sources.values():
        if not s or not s.found:
            continue
        pool = (
            list(s.genres or [])
            + list(s.genres_en or [])
            + list(s.genres_ru or [])
            + list(s.tags or [])
        )
        for g in pool:
            if not g or not str(g).strip():
                continue
            g = str(g).strip()
            genres_all.append(g)
            if _looks_cyrillic(g):
                genres_ru.append(g)
            else:
                genres_en.append(g)

    if item.genre_hint:
        for g in [x.strip() for x in item.genre_hint.split(",") if x.strip()]:
            genres_all.append(g)
            if _looks_cyrillic(g):
                genres_ru.append(g)
            else:
                genres_en.append(g)

    # Official + wiki labels
    result.genres_en = _uniq(genres_en, 60)
    result.genres_ru = _uniq(genres_ru, 60)
    # Full combined list (language-mixed OK — this is the extensive field users expect)
    combined = all_genres(sources) + genres_all
    # Add genre-like keywords/themes so the list stays rich (neo-noir, psychological, …)
    combined.extend(all_genre_like_terms(sources))
    result.genres = _uniq(combined, 80)

    # --- Directors / writers / composers / actors ---
    dir_en, dir_ru = [], []
    wr_en, wr_ru = [], []
    comp_en, comp_ru = [], []
    ac_en, ac_ru = [], []

    for s in sources.values():
        if not s or not s.found:
            continue
        e, r = _names_from_source(
            s,
            en_attrs=("directors_en",),
            ru_attrs=("directors_ru",),
            fallback_attrs=("directors",),
        )
        dir_en.extend(e)
        dir_ru.extend(r)
        e, r = _names_from_source(
            s,
            en_attrs=("writers_en",),
            ru_attrs=("writers_ru",),
            fallback_attrs=("writers", "creators"),
        )
        wr_en.extend(e)
        wr_ru.extend(r)
        e, r = _names_from_source(
            s,
            en_attrs=("composers_en",),
            ru_attrs=("composers_ru",),
            fallback_attrs=("composers",),
        )
        comp_en.extend(e)
        comp_ru.extend(r)
        e, r = _names_from_source(
            s,
            en_attrs=("cast_en",),
            ru_attrs=("cast_ru",),
            fallback_attrs=("cast",),
        )
        ac_en.extend(e)
        ac_ru.extend(r)

    if item.director_hint:
        for d in [x.strip() for x in item.director_hint.split(",") if x.strip()]:
            if _looks_cyrillic(d):
                dir_ru.append(d)
            else:
                dir_en.append(d)

    def _en_only(names: list[str]) -> list[str]:
        return [n for n in names if not _looks_cyrillic(n)]

    def _ru_only(names: list[str]) -> list[str]:
        return [n for n in names if _looks_cyrillic(n)]

    # Force language purity in EN/RU columns (no mixed scripts)
    result.directors_en = _uniq(_en_only(dir_en), 12)
    result.directors_ru = _uniq(_ru_only(dir_ru) + _ru_only(dir_en), 12)
    result.directors = result.directors_en or result.directors_ru
    result.writers_en = _uniq(_en_only(wr_en), 15)
    result.writers_ru = _uniq(_ru_only(wr_ru) + _ru_only(wr_en), 15)
    result.composers_en = _uniq(_en_only(comp_en), 12)
    result.composers_ru = _uniq(_ru_only(comp_ru) + _ru_only(comp_en), 12)
    result.actors_en = _uniq(_en_only(ac_en), 20)
    result.actors_ru = _uniq(_ru_only(ac_ru) + _ru_only(ac_en), 20)

    # Keywords from all sources (keep extensive; do not collapse into genres only)
    kws: list[str] = []
    for s in sources.values():
        if s and s.found:
            kws.extend(s.keywords or [])
            kws.extend(s.tags or [])
    result.keywords = _uniq(kws, 80)

    # Awards
    awards: list[str] = []
    for s in (omdb, wiki, tmdb, kp):
        if s and s.found:
            if s.awards_text:
                awards.append(s.awards_text)
            awards.extend(s.awards or [])
    result.awards_text = "; ".join(_uniq(awards, 8)) if awards else None

    # --- Links (separate fields) ---
    if imdb_id:
        result.link_imdb = f"https://www.imdb.com/title/{imdb_id}/"
    elif omdb and omdb.found and omdb.url:
        result.link_imdb = omdb.url

    if tmdb_id:
        kind = "tv" if result.media_type in {"series", "season"} or (tmdb and tmdb.type == "tv") else "movie"
        result.link_tmdb = f"https://www.themoviedb.org/{kind}/{tmdb_id}"
    elif tmdb and tmdb.found and tmdb.url:
        result.link_tmdb = tmdb.url

    if kp_id:
        result.link_kinopoisk = f"https://www.kinopoisk.ru/film/{kp_id}/"
    elif kp and kp.found and kp.url:
        result.link_kinopoisk = kp.url

    if wiki and wiki.found:
        links_map = (wiki.extra or {}).get("links_by_lang") or {}
        if links_map:
            result.link_wikipedia_en = links_map.get("en")
            result.link_wikipedia_ru = links_map.get("ru")
            result.link_wikipedia_de = links_map.get("de")
        elif wiki.url:
            # single primary
            if wiki.language == "ru":
                result.link_wikipedia_ru = wiki.url
            elif wiki.language == "de":
                result.link_wikipedia_de = wiki.url
            else:
                result.link_wikipedia_en = wiki.url
        # rebuild from page titles if needed
        page_titles = (wiki.extra or {}).get("page_titles") or {}
        for lang, title in page_titles.items():
            if not title:
                continue
            url = f"https://{lang}.wikipedia.org/wiki/{title.replace(' ', '_')}"
            if lang == "en" and not result.link_wikipedia_en:
                result.link_wikipedia_en = url
            if lang == "ru" and not result.link_wikipedia_ru:
                result.link_wikipedia_ru = url
            if lang == "de" and not result.link_wikipedia_de:
                result.link_wikipedia_de = url

    if lb and lb.found and lb.url:
        result.link_letterboxd = lb.url

    result.source_links = [
        u
        for u in [
            result.link_imdb,
            result.link_tmdb,
            result.link_kinopoisk,
            result.link_wikipedia_en,
            result.link_wikipedia_ru,
            result.link_wikipedia_de,
            result.link_letterboxd,
        ]
        if u
    ]

    result.film_uid = build_film_uid(
        imdb_id=result.imdb_id,
        tmdb_id=result.tmdb_id,
        kinopoisk_id=result.kinopoisk_id,
        title_en=result.title_en,
        year=result.year,
        media_type=result.media_type,
        season=result.season,
    )

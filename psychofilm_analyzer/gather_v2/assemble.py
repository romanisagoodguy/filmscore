"""Assemble film-level profiles from Approach 2 request results."""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from psychofilm_analyzer.enrichment.profile import (
    EnrichmentProfile,
    EvidenceBag,
    build_enrichment_profile,
)
from psychofilm_analyzer.gather_v2.models import STATUS_SUCCESS
from psychofilm_analyzer.gather_v2.plan_store import PlanStore
from psychofilm_analyzer.models import InputTitle, MediaType, SourcePayload
from psychofilm_analyzer.utils.text import safe_float, safe_int

logger = logging.getLogger(__name__)


def _pick_tmdb(results: list[dict], year: Optional[int]) -> Optional[dict]:
    if not results:
        return None
    if not year:
        return results[0]
    scored = []
    for r in results:
        date = r.get("release_date") or r.get("first_air_date") or ""
        y = safe_int(date[:4]) if date else None
        dist = abs(y - year) if y else 99
        scored.append((dist, -(r.get("popularity") or 0), r))
    scored.sort(key=lambda x: (x[0], x[1]))
    return scored[0][2]


def _pick_kp(films: list[dict], year: Optional[int]) -> Optional[dict]:
    if not films:
        return None
    if not year:
        return films[0]
    best = films[0]
    best_d = 999
    for f in films:
        y = safe_int(f.get("year") or (str(f.get("year") or "").split("-")[0]))
        if y is None:
            continue
        d = abs(y - year)
        if d < best_d:
            best_d = d
            best = f
    return best


def _payload_tmdb(store: PlanStore, film_index: int, item: InputTitle) -> SourcePayload:
    search = store.load_response(f"f{film_index:05d}_tmdb_search")
    details = store.load_response(f"f{film_index:05d}_tmdb_details_en")
    details_ru = store.load_response(f"f{film_index:05d}_tmdb_details_ru")
    if not details and not search:
        return SourcePayload(source="tmdb", found=False, error="no tmdb data")
    media = "movie"
    result = None
    if isinstance(search, dict):
        result = _pick_tmdb(search.get("results") or [], item.year)
    details = details if isinstance(details, dict) else {}
    details_ru = details_ru if isinstance(details_ru, dict) else {}
    src = details or result or {}
    if not src:
        return SourcePayload(source="tmdb", found=False, error="not found")
    credits = src.get("credits") or {}
    cast = [c.get("name") for c in (credits.get("cast") or [])[:15] if c.get("name")]
    directors = []
    writers = []
    composers = []
    for c in credits.get("crew") or []:
        name = c.get("name")
        job = c.get("job") or ""
        if not name:
            continue
        if job in {"Director", "Co-Director"} and name not in directors:
            directors.append(name)
        if job in {"Writer", "Screenplay", "Story"} and name not in writers:
            writers.append(name)
        if "Music" in job and name not in composers:
            composers.append(name)
    kw_block = src.get("keywords") or {}
    kws = kw_block.get("keywords") or kw_block.get("results") or []
    keywords = [k.get("name") for k in kws if isinstance(k, dict) and k.get("name")]
    genres_en = [g.get("name") for g in (src.get("genres") or []) if g.get("name")]
    genres_ru = [g.get("name") for g in (details_ru.get("genres") or []) if g.get("name")]
    date = src.get("release_date") or src.get("first_air_date") or ""
    year = safe_int(date[:4]) if date else item.year
    title_en = src.get("title") or src.get("name")
    title_ru = details_ru.get("title") or details_ru.get("name")
    ext = src.get("external_ids") or {}
    imdb = ext.get("imdb_id") or src.get("imdb_id")
    tmdb_id = src.get("id") or (result or {}).get("id")
    return SourcePayload(
        source="tmdb",
        found=True,
        title=title_en or title_ru,
        title_en=title_en,
        title_ru=title_ru,
        original_title=src.get("original_title") or src.get("original_name"),
        year=year,
        overview=src.get("overview"),
        overview_en=src.get("overview"),
        overview_ru=details_ru.get("overview"),
        plot=src.get("overview"),
        plot_en=src.get("overview"),
        plot_ru=details_ru.get("overview"),
        genres=genres_en or genres_ru,
        genres_en=genres_en,
        genres_ru=genres_ru,
        keywords=keywords,
        directors=directors[:8],
        directors_en=directors[:8],
        writers=writers[:10],
        writers_en=writers[:10],
        composers=composers[:8],
        composers_en=composers[:8],
        cast=cast,
        cast_en=cast,
        rating=safe_float(src.get("vote_average")),
        votes=safe_int(src.get("vote_count")),
        runtime=safe_int(src.get("runtime")),
        type=media,
        tmdb_id=safe_int(tmdb_id),
        imdb_id=imdb,
        url=f"https://www.themoviedb.org/{media}/{tmdb_id}" if tmdb_id else None,
        language="en",
        extra={"name_en": title_en, "name_ru": title_ru},
    )


def _payload_omdb(store: PlanStore, film_index: int) -> SourcePayload:
    data = store.load_response(f"f{film_index:05d}_omdb_by_id")
    if not isinstance(data, dict) or data.get("Response") == "False":
        data = store.load_response(f"f{film_index:05d}_omdb_by_title")
    if not isinstance(data, dict) or data.get("Response") == "False":
        return SourcePayload(source="omdb", found=False, error="not found")
    genres = [g.strip() for g in (data.get("Genre") or "").split(",") if g.strip()]
    directors = [d.strip() for d in (data.get("Director") or "").split(",") if d.strip() and d != "N/A"]
    writers = [w.strip() for w in (data.get("Writer") or "").split(",") if w.strip() and w != "N/A"]
    cast = [c.strip() for c in (data.get("Actors") or "").split(",") if c.strip() and c != "N/A"]
    plot = data.get("Plot") if data.get("Plot") not in (None, "N/A") else None
    awards = data.get("Awards") if data.get("Awards") not in (None, "N/A") else None
    imdb = data.get("imdbID") if data.get("imdbID") not in (None, "N/A") else None
    return SourcePayload(
        source="omdb",
        found=True,
        title=data.get("Title"),
        title_en=data.get("Title"),
        year=safe_int(str(data.get("Year") or "")[:4]),
        overview=plot,
        overview_en=plot,
        plot=plot,
        plot_en=plot,
        genres=genres,
        genres_en=genres,
        directors=directors,
        directors_en=directors,
        writers=writers,
        writers_en=writers,
        cast=cast,
        cast_en=cast,
        rating=safe_float(data.get("imdbRating")) if data.get("imdbRating") not in (None, "N/A") else None,
        awards_text=awards,
        imdb_id=imdb,
        url=f"https://www.imdb.com/title/{imdb}/" if imdb else None,
        language="en",
    )


def _payload_kp(store: PlanStore, film_index: int, item: InputTitle) -> SourcePayload:
    search = store.load_response(f"f{film_index:05d}_kinopoisk_search")
    details = store.load_response(f"f{film_index:05d}_kinopoisk_details")
    staff = store.load_response(f"f{film_index:05d}_kinopoisk_staff")
    if not isinstance(details, dict) and not isinstance(search, dict):
        return SourcePayload(source="kinopoisk", found=False, error="not found")
    film = None
    if isinstance(search, dict):
        film = _pick_kp(search.get("films") or [], item.year)
    src = details if isinstance(details, dict) else (film or {})
    if not src:
        return SourcePayload(source="kinopoisk", found=False, error="not found")
    kp_id = src.get("kinopoiskId") or src.get("filmId") or src.get("id") or (film or {}).get("filmId")
    genres = []
    for g in src.get("genres") or []:
        if isinstance(g, dict) and g.get("genre"):
            genres.append(g["genre"])
    directors_ru, directors_en = [], []
    cast_ru, cast_en = [], []
    if isinstance(staff, list):
        for person in staff:
            key = (person.get("professionKey") or "").upper()
            if key == "DIRECTOR":
                if person.get("nameRu"):
                    directors_ru.append(person["nameRu"])
                if person.get("nameEn"):
                    directors_en.append(person["nameEn"])
            elif key in {"ACTOR", "HERO"}:
                if person.get("nameRu") and len(cast_ru) < 15:
                    cast_ru.append(person["nameRu"])
                if person.get("nameEn") and len(cast_en) < 15:
                    cast_en.append(person["nameEn"])
    desc = src.get("description") or src.get("shortDescription") or (film or {}).get("description")
    name_ru = src.get("nameRu") or (film or {}).get("nameRu")
    name_en = src.get("nameEn") or (film or {}).get("nameEn")
    return SourcePayload(
        source="kinopoisk",
        found=True,
        title=name_ru or name_en,
        title_ru=name_ru,
        title_en=name_en,
        year=safe_int(src.get("year")) or item.year,
        overview=desc,
        overview_ru=desc,
        plot=desc,
        plot_ru=desc,
        genres=genres,
        genres_ru=genres,
        keywords=list(genres),
        directors=directors_ru[:8] or directors_en[:8],
        directors_ru=directors_ru[:8],
        directors_en=directors_en[:8],
        cast=cast_ru[:15] or cast_en[:15],
        cast_ru=cast_ru[:15],
        cast_en=cast_en[:15],
        rating=safe_float(src.get("ratingKinopoisk")) or safe_float(src.get("rating")),
        kinopoisk_id=safe_int(kp_id),
        imdb_id=src.get("imdbId"),
        url=f"https://www.kinopoisk.ru/film/{kp_id}/" if kp_id else None,
        language="ru",
        extra={"name_en": name_en, "name_ru": name_ru},
    )


def _payload_lb(store: PlanStore, film_index: int) -> SourcePayload:
    for i in range(3):
        rid = f"f{film_index:05d}_letterboxd_slug_{i}"
        req = store.get(rid)
        data = store.load_response(rid)
        if not req or req.status != STATUS_SUCCESS or not isinstance(data, dict):
            continue
        html = data.get("_html") or ""
        if not html:
            continue
        # lightweight extract
        import re

        title = None
        m = re.search(r'property="og:title" content="([^"]+)"', html)
        if m:
            title = m.group(1).replace(" — Letterboxd", "").strip()
        overview = None
        m2 = re.search(r'name="description" content="([^"]+)"', html)
        if m2:
            overview = m2.group(1)
        return SourcePayload(
            source="letterboxd",
            found=True,
            title=title,
            overview=overview,
            url=req.url,
            language="en",
            extra={"slug_mode": True},
        )
    return SourcePayload(source="letterboxd", found=False, error="not found")


def _load_wiki_lang(store: PlanStore, film_index: int, lang: str) -> Optional[dict]:
    for rid in (
        f"f{film_index:05d}_wikipedia_summary_{lang}",
        f"f{film_index:05d}_wikipedia_summary",  # legacy single-lang id
    ):
        data = store.load_response(rid)
        if isinstance(data, dict) and data.get("extract"):
            if data.get("type") in {
                "https://mediawiki.org/wiki/HyperSwitch/errors/not_found",
                "disambiguation",
            }:
                continue
            return data
    return None


def _payload_wiki(store: PlanStore, film_index: int) -> SourcePayload:
    """Merge EN/RU/DE Wikipedia summaries into one SourcePayload."""
    en = _load_wiki_lang(store, film_index, "en")
    ru = _load_wiki_lang(store, film_index, "ru")
    de = _load_wiki_lang(store, film_index, "de")
    if not en and not ru and not de:
        return SourcePayload(source="wikipedia", found=False, error="not found")

    def _extract(d: Optional[dict]) -> Optional[str]:
        if not d:
            return None
        t = d.get("extract")
        return str(t) if t else None

    def _url(d: Optional[dict]) -> Optional[str]:
        if not d:
            return None
        return (d.get("content_urls") or {}).get("desktop", {}).get("page")

    overview_en = _extract(en)
    overview_ru = _extract(ru)
    overview_de = _extract(de)
    primary = en or ru or de or {}
    links = {
        "en": _url(en),
        "ru": _url(ru),
        "de": _url(de),
    }
    combined = "\n\n".join(
        f"[{lang}] {(txt or '')[:3000]}"
        for lang, txt in (("en", overview_en), ("ru", overview_ru), ("de", overview_de))
        if txt
    )
    return SourcePayload(
        source="wikipedia",
        found=True,
        title=primary.get("title"),
        title_en=(en or {}).get("title") if en else None,
        title_ru=(ru or {}).get("title") if ru else None,
        overview=overview_en or overview_ru or overview_de,
        overview_en=overview_en,
        overview_ru=overview_ru,
        plot=(overview_en or overview_ru or overview_de or "")[:1500] or None,
        plot_en=(overview_en or "")[:1500] or None,
        plot_ru=(overview_ru or "")[:1500] or None,
        url=links.get("en") or links.get("ru") or links.get("de"),
        language="en" if en else ("ru" if ru else "de"),
        extra={
            "langs": [lang for lang, blob in (("en", en), ("ru", ru), ("de", de)) if blob],
            "links_by_lang": {k: v for k, v in links.items() if v},
            "combined_text": combined[:10000],
            "overview_de": overview_de,
            "title_de": (de or {}).get("title") if de else None,
        },
    )


def assemble_profiles(
    items: list[InputTitle],
    store: PlanStore,
    *,
    film_indices: Optional[list[int]] = None,
) -> list[dict[str, Any]]:
    """Build gather-style profile dicts (items order). film_indices map to plan film_index."""
    out: list[dict[str, Any]] = []
    for i, item in enumerate(items):
        idx = int(film_indices[i]) if film_indices is not None else (i + 1)
        payloads: dict[str, SourcePayload] = {}
        # only include sources that had any request
        sites = {r.site for r in store.all() if int(r.film_index) == idx}
        if "tmdb" in sites:
            payloads["tmdb"] = _payload_tmdb(store, idx, item)
        if "omdb" in sites:
            payloads["omdb"] = _payload_omdb(store, idx)
        if "kinopoisk" in sites:
            payloads["kinopoisk"] = _payload_kp(store, idx, item)
        if "letterboxd" in sites:
            payloads["letterboxd"] = _payload_lb(store, idx)
        if "wikipedia" in sites:
            payloads["wikipedia"] = _payload_wiki(store, idx)
        try:
            profile = build_enrichment_profile(item, payloads)
            d = profile.to_json_dict()
        except Exception as exc:  # noqa: BLE001
            logger.warning("assemble failed for %s: %s", item.title, exc)
            d = {
                "imported": {
                    "file": item.source_file,
                    "sheet": item.source_sheet,
                    "row": item.source_row,
                    "title": item.import_title or item.title,
                    "year": item.year,
                },
                "titles": {"en": item.english_title, "ru": item.russian_title},
                "year": item.year,
                "error": str(exc),
                "sources": {k: v.to_dict() for k, v in payloads.items()},
                "note": "Approach 2 gather profile (assemble error)",
            }
        d["gather_approach"] = 2
        out.append(d)
    return out

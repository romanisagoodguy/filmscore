"""Build a complete Request Plan from the film list (Approach 2)."""

from __future__ import annotations

import json
from typing import Any, Optional
from urllib.parse import quote

from psychofilm_analyzer.gather_v2.models import PlanRequest, STATUS_PENDING
from psychofilm_analyzer.models import InputTitle, MediaType
from psychofilm_analyzer.utils.text import slugify_key


TMDB_API = "https://api.themoviedb.org/3"
OMDB_API = "https://www.omdbapi.com/"
KP_API = "https://kinopoiskapiunofficial.tech/api"


def _rid(film_index: int, site: str, endpoint: str) -> str:
    return f"f{film_index:05d}_{site}_{endpoint}"


def _primary_title(item: InputTitle) -> str:
    return (item.english_title or item.title or item.import_title or "").strip()


def _search_titles(item: InputTitle) -> list[str]:
    out: list[str] = []
    for t in (item.english_title, item.title, item.russian_title, item.import_title):
        if t and str(t).strip() and str(t).strip() not in out:
            out.append(str(t).strip())
    return out[:3]


def _lb_slugs(item: InputTitle) -> list[str]:
    titles: list[str] = []
    for t in (item.english_title, item.title, item.import_title):
        if not t or not str(t).strip():
            continue
        s = str(t).strip()
        # skip pure Cyrillic
        if any("\u0400" <= ch <= "\u04FF" for ch in s) and not any(ch.isascii() and ch.isalpha() for ch in s):
            continue
        if s not in titles:
            titles.append(s)
    slugs: list[str] = []
    for t in titles:
        base = slugify_key(t).replace("_", "-").strip("-")
        if not base:
            continue
        slugs.append(base)
        if item.year:
            slugs.append(f"{base}-{item.year}")
    # unique, max 2
    seen = set()
    out = []
    for s in slugs:
        if s not in seen:
            seen.add(s)
            out.append(s)
        if len(out) >= 2:
            break
    return out


def _wiki_candidate_title(item: InputTitle, lang: str, is_tv: bool) -> str:
    """Best page title form for Wikipedia REST summary per language.

    EN pages usually use \"Title (film)\" not \"Title (YYYY film)\" — year form 404s often.
    """
    if lang == "ru":
        # Prefer clean EN title for better hit rate when RU list titles are marketing names
        base = (item.english_title or item.russian_title or item.title or "").strip()
    elif lang == "de":
        base = (item.english_title or item.title or item.import_title or "").strip()
    else:
        base = (item.english_title or item.title or item.import_title or "").strip()
    if not base:
        base = "Unknown"
    year = item.year
    if is_tv:
        if lang == "ru":
            return f"{base} (сериал)"
        if lang == "de":
            return f"{base}"
        return f"{base} (TV series)"
    # film — bare title first (executor also tries (film) + search)
    if lang == "ru":
        return base
    if lang == "de":
        return base
    # EN: bare first — many pages have no "(film)" suffix
    return base


def _append_wikipedia_requests(
    requests: list[PlanRequest],
    *,
    idx: int,
    item: InputTitle,
    title: str,
    en: str,
    year: Optional[int],
    sheet: Optional[str],
    row: Optional[int],
    is_tv: bool,
    base_headers: dict[str, str],
    langs: list[str],
) -> None:
    """
    Add independent Wikipedia resolve requests per language (en/ru/de).

    Each request is executed as multi-step search (bare → disambiguators →
    MediaWiki search → summary). The URL is only the first candidate seed;
    executor may change URL to the attributed attempt.
    """
    for lang in langs:
        lang = (lang or "").strip().lower()
        if lang in {"ge", "german", "deu"}:
            lang = "de"
        if lang not in {"en", "ru", "de"}:
            continue
        page = _wiki_candidate_title(item, lang, is_tv)
        host = f"{lang}.wikipedia.org"
        requests.append(
            PlanRequest(
                request_id=_rid(idx, "wikipedia", f"summary_{lang}"),
                film_index=idx,
                excel_row=row,
                excel_sheet=sheet,
                film_title=title,
                year=year,
                english_title=en or None,
                site="wikipedia",
                endpoint_type=f"summary_{lang}",
                url=f"https://{host}/api/rest_v1/page/summary/{quote(page, safe='')}",
                params_json="{}",
                headers_json=json.dumps(base_headers, ensure_ascii=False),
                depends_on="",
                status=STATUS_PENDING,
                # Multi-step resolve with 1:1 command attribution
                resolve_hint="wiki_resolve_v2",
                max_attempts=3,
            )
        )


def build_plan_for_items(
    items: list[InputTitle],
    *,
    config: dict[str, Any],
    sources_enabled: Optional[dict[str, bool]] = None,
    film_indices: Optional[list[int]] = None,
    full_sources_for_keys: Optional[set[str]] = None,
    wikipedia_langs: Optional[list[str]] = None,
) -> list[PlanRequest]:
    """Create all HTTP request nodes (with dependency edges) for Approach 2.

    Args:
      items: films to plan (often full catalog).
      film_indices: optional catalog indices (same length as items); default 1..N.
      full_sources_for_keys: if set, only those resume_keys get TMDB/OMDb/KP/LB;
          Wikipedia still planned for every film when enabled.
      wikipedia_langs: default en, ru, de.
    """
    from psychofilm_analyzer.pipeline import Pipeline

    keys = config.get("api_keys") or {}
    src = sources_enabled or (config.get("sources") or {})
    a2 = config.get("gather_v2") or {}
    use_tmdb = bool(src.get("tmdb", True) and keys.get("tmdb"))
    use_omdb = bool(src.get("omdb", True) and keys.get("omdb"))
    use_kp = bool(src.get("kinopoisk", True) and keys.get("kinopoisk"))
    use_lb = bool(src.get("letterboxd", True))
    # Approach 2 defaults Wikipedia ON (EN/RU/DE) unless explicitly disabled
    use_wiki = bool(src.get("wikipedia", a2.get("include_wikipedia", True)))
    wiki_langs = list(
        wikipedia_langs
        or a2.get("wikipedia_langs")
        or ["en", "ru", "de"]
    )

    tmdb_key = keys.get("tmdb") or ""
    omdb_key = keys.get("omdb") or ""
    kp_key = keys.get("kinopoisk") or ""
    from psychofilm_analyzer.utils.wikipedia_auth import wikipedia_headers, wikipedia_user_agent

    ua = wikipedia_user_agent(config) or (config.get("http") or {}).get(
        "user_agent", "PsychoFilmAnalyzer/1.0 (+research; educational; contact: local)"
    )
    base_headers = {
        "User-Agent": ua,
        "Accept": "application/json, text/html, */*",
        "Accept-Language": "en-US,en;q=0.9,ru;q=0.8,de;q=0.7",
    }
    # Wikipedia plan rows get OAuth Bearer + contact UA (highvolume token when set)
    wiki_headers = wikipedia_headers(config)
    kp_headers = {
        **base_headers,
        "X-API-KEY": kp_key,
        "Content-Type": "application/json",
    }

    requests: list[PlanRequest] = []

    for i, item in enumerate(items):
        idx = int(film_indices[i]) if film_indices is not None else (i + 1)
        resume_key = Pipeline.resume_key(item)
        # When full_sources_for_keys is None → all films get full source stack
        do_full = full_sources_for_keys is None or resume_key in full_sources_for_keys

        title = item.title or _primary_title(item)
        en = item.english_title or ""
        year = item.year
        sheet = item.source_sheet
        row = item.source_row
        is_tv = item.media_type in {MediaType.SERIES, MediaType.SEASON}
        media = "tv" if is_tv else "movie"
        q_title = _primary_title(item) or title

        tmdb_search_id = None
        tmdb_details_en_id = None
        kp_search_id = None

        # --- TMDB ---
        if do_full and use_tmdb:
            tmdb_search_id = _rid(idx, "tmdb", "search")
            params = {
                "api_key": tmdb_key,
                "language": "en-US",
                "query": q_title,
                "include_adult": "false",
            }
            if year and media == "movie":
                params["year"] = year
            if year and media == "tv":
                params["first_air_date_year"] = year
            requests.append(
                PlanRequest(
                    request_id=tmdb_search_id,
                    film_index=idx,
                    excel_row=row,
                    excel_sheet=sheet,
                    film_title=title,
                    year=year,
                    english_title=en or None,
                    site="tmdb",
                    endpoint_type="search",
                    url=f"{TMDB_API}/search/{media}",
                    params_json=json.dumps(params, ensure_ascii=False),
                    headers_json=json.dumps(base_headers, ensure_ascii=False),
                    depends_on="",
                    status=STATUS_PENDING,
                    resolve_hint="pick_tmdb_search",
                )
            )
            tmdb_details_en_id = _rid(idx, "tmdb", "details_en")
            requests.append(
                PlanRequest(
                    request_id=tmdb_details_en_id,
                    film_index=idx,
                    excel_row=row,
                    excel_sheet=sheet,
                    film_title=title,
                    year=year,
                    english_title=en or None,
                    site="tmdb",
                    endpoint_type="details_en",
                    url="",  # filled after search
                    params_json=json.dumps(
                        {
                            "api_key": tmdb_key,
                            "language": "en-US",
                            "append_to_response": "external_ids,credits,keywords",
                        },
                        ensure_ascii=False,
                    ),
                    headers_json=json.dumps(base_headers, ensure_ascii=False),
                    depends_on=tmdb_search_id,
                    status=STATUS_PENDING,
                    resolve_hint="tmdb_details",
                )
            )
            requests.append(
                PlanRequest(
                    request_id=_rid(idx, "tmdb", "details_ru"),
                    film_index=idx,
                    excel_row=row,
                    excel_sheet=sheet,
                    film_title=title,
                    year=year,
                    english_title=en or None,
                    site="tmdb",
                    endpoint_type="details_ru",
                    url="",
                    params_json=json.dumps(
                        {
                            "api_key": tmdb_key,
                            "language": "ru-RU",
                            "append_to_response": "external_ids",
                        },
                        ensure_ascii=False,
                    ),
                    headers_json=json.dumps(base_headers, ensure_ascii=False),
                    depends_on=tmdb_search_id,
                    status=STATUS_PENDING,
                    resolve_hint="tmdb_details",
                )
            )

        # --- OMDb ---
        if do_full and use_omdb:
            # by_id only when TMDB details can supply imdb_id
            if tmdb_details_en_id:
                requests.append(
                    PlanRequest(
                        request_id=_rid(idx, "omdb", "by_id"),
                        film_index=idx,
                        excel_row=row,
                        excel_sheet=sheet,
                        film_title=title,
                        year=year,
                        english_title=en or None,
                        site="omdb",
                        endpoint_type="by_id",
                        url=OMDB_API,
                        params_json=json.dumps(
                            {"apikey": omdb_key, "i": "", "plot": "full"},
                            ensure_ascii=False,
                        ),
                        headers_json=json.dumps(base_headers, ensure_ascii=False),
                        depends_on=tmdb_details_en_id,
                        status=STATUS_PENDING,
                        resolve_hint="omdb_imdb_from_tmdb",
                    )
                )
            # Independent title search (always planned)
            omdb_type = "series" if is_tv else "movie"
            params_t = {"apikey": omdb_key, "t": q_title, "plot": "full", "type": omdb_type}
            if year:
                params_t["y"] = str(year)
            requests.append(
                PlanRequest(
                    request_id=_rid(idx, "omdb", "by_title"),
                    film_index=idx,
                    excel_row=row,
                    excel_sheet=sheet,
                    film_title=title,
                    year=year,
                    english_title=en or None,
                    site="omdb",
                    endpoint_type="by_title",
                    url=OMDB_API,
                    params_json=json.dumps(params_t, ensure_ascii=False),
                    headers_json=json.dumps(base_headers, ensure_ascii=False),
                    depends_on="",
                    status=STATUS_PENDING,
                    resolve_hint="",
                )
            )

        # --- Kinopoisk ---
        if do_full and use_kp:
            kp_search_id = _rid(idx, "kinopoisk", "search")
            requests.append(
                PlanRequest(
                    request_id=kp_search_id,
                    film_index=idx,
                    excel_row=row,
                    excel_sheet=sheet,
                    film_title=title,
                    year=year,
                    english_title=en or None,
                    site="kinopoisk",
                    endpoint_type="search",
                    url=f"{KP_API}/v2.1/films/search-by-keyword",
                    params_json=json.dumps(
                        {"keyword": q_title, "page": 1}, ensure_ascii=False
                    ),
                    headers_json=json.dumps(kp_headers, ensure_ascii=False),
                    depends_on="",
                    status=STATUS_PENDING,
                    resolve_hint="pick_kp_search",
                )
            )
            requests.append(
                PlanRequest(
                    request_id=_rid(idx, "kinopoisk", "details"),
                    film_index=idx,
                    excel_row=row,
                    excel_sheet=sheet,
                    film_title=title,
                    year=year,
                    english_title=en or None,
                    site="kinopoisk",
                    endpoint_type="details",
                    url="",
                    params_json="{}",
                    headers_json=json.dumps(kp_headers, ensure_ascii=False),
                    depends_on=kp_search_id,
                    status=STATUS_PENDING,
                    resolve_hint="kp_details",
                )
            )
            requests.append(
                PlanRequest(
                    request_id=_rid(idx, "kinopoisk", "staff"),
                    film_index=idx,
                    excel_row=row,
                    excel_sheet=sheet,
                    film_title=title,
                    year=year,
                    english_title=en or None,
                    site="kinopoisk",
                    endpoint_type="staff",
                    url="",
                    params_json="{}",
                    headers_json=json.dumps(kp_headers, ensure_ascii=False),
                    depends_on=kp_search_id,
                    status=STATUS_PENDING,
                    resolve_hint="kp_staff",
                )
            )

        # --- Letterboxd (slug only, no search) ---
        if do_full and use_lb:
            for si, slug in enumerate(_lb_slugs(item)):
                # slug_0 free; slug_N+ skipped by resolver if earlier slug succeeded
                requests.append(
                    PlanRequest(
                        request_id=_rid(idx, "letterboxd", f"slug_{si}"),
                        film_index=idx,
                        excel_row=row,
                        excel_sheet=sheet,
                        film_title=title,
                        year=year,
                        english_title=en or None,
                        site="letterboxd",
                        endpoint_type=f"slug_{si}",
                        url=f"https://letterboxd.com/film/{slug}/",
                        params_json="{}",
                        headers_json=json.dumps(base_headers, ensure_ascii=False),
                        depends_on=(
                            _rid(idx, "letterboxd", f"slug_{si - 1}") if si > 0 else ""
                        ),
                        status=STATUS_PENDING,
                        resolve_hint="letterboxd_slug_chain" if si > 0 else "",
                    )
                )

        # --- Wikipedia EN + RU + DE (all films when enabled) ---
        if use_wiki:
            _append_wikipedia_requests(
                requests,
                idx=idx,
                item=item,
                title=title,
                en=en,
                year=year,
                sheet=sheet,
                row=row,
                is_tv=is_tv,
                base_headers=wiki_headers,
                langs=wiki_langs,
            )

    return requests

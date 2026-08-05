"""TMDB (The Movie Database) client."""

from __future__ import annotations

import logging
from typing import Any, Optional

from psychofilm_analyzer.data_sources.base import BaseSource
from psychofilm_analyzer.models import InputTitle, MediaType, SourcePayload
from psychofilm_analyzer.utils.text import safe_float, safe_int

logger = logging.getLogger(__name__)

TMDB_API = "https://api.themoviedb.org/3"


class TmdbSource(BaseSource):
    name = "tmdb"

    def __init__(self, *args, api_key: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self.api_key = api_key

    def _fetch(self, item: InputTitle) -> SourcePayload:
        if not self.api_key:
            return SourcePayload(source=self.name, found=False, error="TMDB_API_KEY not set")

        is_tv = item.media_type in {MediaType.SERIES, MediaType.SEASON}
        result = self._search(item, media="tv" if is_tv else "movie")
        if not result and not is_tv:
            result = self._search(item, media="tv")
            is_tv = bool(result)
        if not result and is_tv:
            result = self._search(item, media="movie")
            is_tv = False if result else is_tv
        if not result:
            # multi search fallback
            result = self._multi_search(item)
            if result:
                is_tv = result.get("media_type") == "tv"

        if not result:
            return SourcePayload(source=self.name, found=False, error="not found")

        tmdb_id = result.get("id")
        media = "tv" if is_tv else "movie"
        # One EN details call with credits+keywords+external_ids (saves 2 round-trips)
        details = self._details(
            tmdb_id,
            media,
            append="external_ids,credits,keywords",
        )
        details_ru = self._details(tmdb_id, media, language="ru-RU")
        keywords = self._keywords_from_details(details, media)
        credits = (details or {}).get("credits") or {}

        genres_en = [g.get("name", "") for g in (details or {}).get("genres", []) if g.get("name")]
        genres_ru = [g.get("name", "") for g in (details_ru or {}).get("genres", []) if g.get("name")]
        directors_en: list[str] = []
        writers_en: list[str] = []
        composers_en: list[str] = []
        creators: list[str] = []
        cast_en: list[str] = []
        writer_jobs = {
            "Writer",
            "Screenplay",
            "Story",
            "Teleplay",
            "Author",
            "Novel",
            "Characters",
            "Original Story",
        }
        composer_jobs = {
            "Original Music Composer",
            "Music",
            "Music Score",
            "Composer",
            "Music Composer",
            "Original Music",
            "Soundtrack",
            "Theme Song Performance",
        }
        if credits:
            cast_en = [c.get("name") for c in credits.get("cast", [])[:15] if c.get("name")]
            if is_tv:
                creators = [c.get("name") for c in (details or {}).get("created_by", []) if c.get("name")]
                writers_en.extend(creators)
            for c in credits.get("crew", []):
                name = c.get("name")
                job = c.get("job") or ""
                dept = (c.get("department") or "").lower()
                if not name:
                    continue
                if job in {"Director", "Series Director", "Co-Director"}:
                    if name not in directors_en:
                        directors_en.append(name)
                if job in writer_jobs or "Writer" in job or "Screenplay" in job:
                    if name not in writers_en:
                        writers_en.append(name)
                if (
                    job in composer_jobs
                    or "Music Composer" in job
                    or "Original Music" in job
                    or (dept == "sound" and "music" in job.lower() and "editor" not in job.lower())
                ):
                    if name not in composers_en:
                        composers_en.append(name)

        title_en = (details or result).get("title") or (details or result).get("name")
        title_ru = (details_ru or {}).get("title") or (details_ru or {}).get("name")
        original = (details or result).get("original_title") or (details or result).get("original_name")
        overview_en = (details or result).get("overview")
        overview_ru = (details_ru or {}).get("overview")
        date = (details or result).get("release_date") or (details or result).get("first_air_date") or ""
        year = safe_int(date[:4]) if date else item.year
        rating = safe_float((details or result).get("vote_average"))
        votes = safe_int((details or result).get("vote_count"))
        runtime = safe_int((details or {}).get("runtime")) or None
        if is_tv and not runtime:
            runtimes = (details or {}).get("episode_run_time") or []
            runtime = safe_int(runtimes[0]) if runtimes else None

        url = f"https://www.themoviedb.org/{media}/{tmdb_id}"

        # season-specific overview if requested
        if is_tv and item.season and details:
            season_data = self._season(tmdb_id, item.season)
            if season_data and season_data.get("overview"):
                overview_en = season_data.get("overview") or overview_en

        countries_en = [
            c.get("name")
            for c in (details or {}).get("production_countries") or []
            if c.get("name")
        ]
        countries_ru = [
            c.get("name")
            for c in (details_ru or {}).get("production_countries") or []
            if c.get("name")
        ]

        return SourcePayload(
            source=self.name,
            found=True,
            title=title_en,
            title_en=title_en,
            title_ru=title_ru,
            original_title=original,
            year=year,
            overview=overview_en,
            overview_en=overview_en,
            overview_ru=overview_ru,
            plot=overview_en,
            plot_en=overview_en,
            plot_ru=overview_ru,
            genres=genres_en,
            genres_en=genres_en,
            genres_ru=genres_ru,
            keywords=keywords,
            directors=directors_en,
            directors_en=directors_en,
            writers=writers_en,
            writers_en=writers_en,
            composers=composers_en,
            composers_en=composers_en,
            creators=creators,
            cast=cast_en,
            cast_en=cast_en,
            rating=rating,
            votes=votes,
            runtime=runtime,
            type=media,
            tmdb_id=tmdb_id,
            imdb_id=(details or {}).get("imdb_id") or ((details or {}).get("external_ids") or {}).get("imdb_id"),
            url=url,
            language="en",
            popularity=safe_float((details or result).get("popularity")),
            extra={
                "tagline": (details or {}).get("tagline"),
                "tagline_ru": (details_ru or {}).get("tagline"),
                "status": (details or {}).get("status"),
                "number_of_seasons": (details or {}).get("number_of_seasons"),
                "countries_en": countries_en,
                "countries_ru": countries_ru,
            },
        )

    def _params(self, extra: Optional[dict] = None) -> dict[str, Any]:
        p: dict[str, Any] = {"api_key": self.api_key, "language": "en-US"}
        if extra:
            p.update(extra)
        return p

    def _search(self, item: InputTitle, media: str) -> Optional[dict]:
        for title in self._search_titles(item):
            data = self.http.get(
                f"{TMDB_API}/search/{media}",
                params=self._params(
                    {
                        "query": title,
                        "year": item.year if media == "movie" and item.year else None,
                        "first_air_date_year": item.year if media == "tv" and item.year else None,
                        "include_adult": "false",
                    }
                ),
            )
            results = (data or {}).get("results") or []
            if not results and item.year:
                # retry without year
                data = self.http.get(
                    f"{TMDB_API}/search/{media}",
                    params=self._params({"query": title, "include_adult": "false"}),
                )
                results = (data or {}).get("results") or []
            if results:
                return self._pick_best(results, item)
        return None

    def _multi_search(self, item: InputTitle) -> Optional[dict]:
        for title in self._search_titles(item):
            data = self.http.get(
                f"{TMDB_API}/search/multi",
                params=self._params({"query": title, "include_adult": "false"}),
            )
            results = [
                r
                for r in ((data or {}).get("results") or [])
                if r.get("media_type") in {"movie", "tv"}
            ]
            if results:
                return self._pick_best(results, item)
        return None

    def _pick_best(self, results: list[dict], item: InputTitle) -> dict:
        if not item.year:
            return results[0]
        scored = []
        for r in results:
            date = r.get("release_date") or r.get("first_air_date") or ""
            y = safe_int(date[:4]) if date else None
            dist = abs(y - item.year) if y else 99
            scored.append((dist, -(r.get("popularity") or 0), r))
        scored.sort(key=lambda x: (x[0], x[1]))
        return scored[0][2]

    def _details(
        self,
        tmdb_id: int,
        media: str,
        language: str = "en-US",
        append: str = "external_ids",
    ) -> Optional[dict]:
        params = self._params({"append_to_response": append})
        params["language"] = language
        return self.http.get(f"{TMDB_API}/{media}/{tmdb_id}", params=params)

    @staticmethod
    def _keywords_from_details(details: Optional[dict], media: str) -> list[str]:
        if not details:
            return []
        block = details.get("keywords") or {}
        if media == "tv":
            kws = block.get("results") or block.get("keywords") or []
        else:
            kws = block.get("keywords") or block.get("results") or []
        if isinstance(kws, list):
            return [k.get("name") for k in kws if isinstance(k, dict) and k.get("name")]
        return []

    def _keywords(self, tmdb_id: int, media: str) -> list[str]:
        data = self.http.get(f"{TMDB_API}/{media}/{tmdb_id}/keywords", params=self._params())
        if not data:
            return []
        if media == "tv":
            kws = data.get("results") or []
        else:
            kws = data.get("keywords") or []
        return [k.get("name") for k in kws if k.get("name")]

    def _credits(self, tmdb_id: int, media: str) -> Optional[dict]:
        return self.http.get(f"{TMDB_API}/{media}/{tmdb_id}/credits", params=self._params())

    def _season(self, tmdb_id: int, season: int) -> Optional[dict]:
        return self.http.get(f"{TMDB_API}/tv/{tmdb_id}/season/{season}", params=self._params())

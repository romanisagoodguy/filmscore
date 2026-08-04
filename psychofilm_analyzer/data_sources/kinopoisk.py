"""Kinopoisk enrichment via unofficial API (kinopoiskapiunofficial.tech).

Falls back gracefully when API key is missing or title is not found.
Also accepts pre-filled ratings from input Excel.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from psychofilm_analyzer.data_sources.base import BaseSource
from psychofilm_analyzer.models import InputTitle, SourcePayload
from psychofilm_analyzer.utils.text import safe_float, safe_int

logger = logging.getLogger(__name__)

KP_API = "https://kinopoiskapiunofficial.tech/api"


class KinopoiskSource(BaseSource):
    name = "kinopoisk"

    def __init__(self, *args, api_key: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self.api_key = api_key

    def _headers(self) -> dict[str, str]:
        return {"X-API-KEY": self.api_key, "Content-Type": "application/json"}

    def _fetch(self, item: InputTitle) -> SourcePayload:
        # Always preserve Excel-provided KP rating even without API
        hint_rating = item.kinopoisk_rating_hint

        if not self.api_key:
            if hint_rating is not None or item.genre_hint or item.russian_title:
                return SourcePayload(
                    source=self.name,
                    found=True,
                    title=item.russian_title or item.title,
                    year=item.year,
                    rating=hint_rating,
                    genres=[g.strip() for g in (item.genre_hint or "").split(",") if g.strip()],
                    directors=[d.strip() for d in (item.director_hint or "").split(",") if d.strip()],
                    extra={"from_input_only": True},
                    error="KINOPOISK_API_KEY not set; used input hints only",
                )
            return SourcePayload(source=self.name, found=False, error="KINOPOISK_API_KEY not set")

        film = self._search_film(item)
        if not film:
            if hint_rating is not None:
                return SourcePayload(
                    source=self.name,
                    found=True,
                    title=item.russian_title or item.title,
                    year=item.year,
                    rating=hint_rating,
                    extra={"from_input_only": True},
                )
            return SourcePayload(source=self.name, found=False, error="not found")

        kp_id = film.get("filmId") or film.get("kinopoiskId") or film.get("id")
        details = self._details(kp_id) if kp_id else None
        src = details or film

        genres = []
        for g in src.get("genres") or []:
            if isinstance(g, dict) and g.get("genre"):
                genres.append(g["genre"])
            elif isinstance(g, str):
                genres.append(g)

        directors_ru: list[str] = []
        directors_en: list[str] = []
        writers_ru: list[str] = []
        writers_en: list[str] = []
        composers_ru: list[str] = []
        composers_en: list[str] = []
        cast_ru: list[str] = []
        cast_en: list[str] = []
        staff = self._staff(kp_id) if kp_id else None
        if staff:
            for person in staff:
                key = (person.get("professionKey") or "").upper()
                name_ru_p = person.get("nameRu")
                name_en_p = person.get("nameEn")
                if key == "DIRECTOR":
                    if name_ru_p:
                        directors_ru.append(name_ru_p)
                    if name_en_p:
                        directors_en.append(name_en_p)
                elif key in {"WRITER", "SCREENWRITER"}:
                    if name_ru_p:
                        writers_ru.append(name_ru_p)
                    if name_en_p:
                        writers_en.append(name_en_p)
                elif key in {"COMPOSER", "MUSIC", "MUSIC_COMPOSER", "OPERATOR"}:
                    # Kinopoisk uses COMPOSER for score; skip camera operators if mis-tagged
                    prof = (person.get("professionText") or person.get("description") or "").lower()
                    if key == "OPERATOR" and "музык" not in prof and "composer" not in prof:
                        continue
                    if key == "OPERATOR":
                        continue
                    if name_ru_p:
                        composers_ru.append(name_ru_p)
                    if name_en_p:
                        composers_en.append(name_en_p)
                elif key in {"ACTOR", "HERO"}:
                    if name_ru_p and len(cast_ru) < 15:
                        cast_ru.append(name_ru_p)
                    if name_en_p and len(cast_en) < 15:
                        cast_en.append(name_en_p)

        keywords = self._keywords(kp_id) if kp_id else []
        description = src.get("description") or src.get("shortDescription") or film.get("description")
        rating = (
            safe_float(src.get("ratingKinopoisk"))
            or safe_float(src.get("rating"))
            or safe_float(film.get("rating"))
            or hint_rating
        )
        year = safe_int(src.get("year")) or item.year
        name_ru = src.get("nameRu") or film.get("nameRu")
        name_en = src.get("nameEn") or film.get("nameEn") or src.get("nameOriginal")
        url = f"https://www.kinopoisk.ru/film/{kp_id}/" if kp_id else None
        countries = []
        for c in src.get("countries") or []:
            if isinstance(c, dict) and c.get("country"):
                countries.append(c["country"])
            elif isinstance(c, str):
                countries.append(c)
        runtime = safe_int(src.get("filmLength"))

        return SourcePayload(
            source=self.name,
            found=True,
            title=name_ru or name_en,
            title_ru=name_ru,
            title_en=name_en,
            original_title=name_en or src.get("nameOriginal"),
            year=year,
            overview=description,
            overview_ru=description,
            plot=description,
            plot_ru=description,
            genres=genres,
            genres_ru=genres,
            keywords=keywords,
            directors=directors_ru[:8] or directors_en[:8],
            directors_ru=directors_ru[:8],
            directors_en=directors_en[:8],
            writers=writers_ru[:10] or writers_en[:10],
            writers_ru=writers_ru[:10],
            writers_en=writers_en[:10],
            composers=composers_ru[:8] or composers_en[:8],
            composers_ru=composers_ru[:8],
            composers_en=composers_en[:8],
            cast=cast_ru[:15] or cast_en[:15],
            cast_ru=cast_ru[:15],
            cast_en=cast_en[:15],
            rating=rating,
            runtime=runtime,
            type=str(src.get("type") or "").lower() or None,
            kinopoisk_id=safe_int(kp_id),
            imdb_id=src.get("imdbId"),
            url=url,
            language="ru",
            extra={
                "name_en": name_en,
                "name_ru": name_ru,
                "rating_imdb": safe_float(src.get("ratingImdb")),
                "film_length": src.get("filmLength"),
                "slogan": src.get("slogan"),
                "countries": countries,
            },
        )

    def _search_film(self, item: InputTitle) -> Optional[dict]:
        for title in self._search_titles(item):
            data = self.http.get(
                f"{KP_API}/v2.1/films/search-by-keyword",
                params={"keyword": title, "page": 1},
                headers=self._headers(),
            )
            films = (data or {}).get("films") or []
            if not films:
                # v2.1 alternative
                data = self.http.get(
                    f"{KP_API}/v2.1/films/search-by-keyword",
                    params={"keyword": title},
                    headers=self._headers(),
                )
                films = (data or {}).get("films") or []
            if films:
                return self._pick(films, item)
        return None

    def _pick(self, films: list[dict], item: InputTitle) -> dict:
        if not item.year:
            return films[0]
        best = films[0]
        best_dist = 999
        for f in films:
            y = safe_int(f.get("year") or (str(f.get("year") or "").split("-")[0]))
            if y is None:
                continue
            dist = abs(y - item.year)
            if dist < best_dist:
                best_dist = dist
                best = f
        return best

    def _details(self, kp_id: Any) -> Optional[dict]:
        try:
            return self.http.get(f"{KP_API}/v2.2/films/{kp_id}", headers=self._headers())
        except Exception:  # noqa: BLE001
            return None

    def _staff(self, kp_id: Any) -> Optional[list]:
        try:
            data = self.http.get(f"{KP_API}/v1/staff", params={"filmId": kp_id}, headers=self._headers())
            return data if isinstance(data, list) else None
        except Exception:  # noqa: BLE001
            return None

    def _keywords(self, kp_id: Any) -> list[str]:
        # Keywords endpoint is flaky / 400 on some IDs — fail soft without retries noise
        try:
            data = self.http.get(
                f"{KP_API}/v2.1/films/{kp_id}/keywords",
                headers=self._headers(),
            )
            items = (data or {}).get("keywords") or []
            return [k.get("keyword") for k in items if k.get("keyword")]
        except Exception:  # noqa: BLE001
            return []

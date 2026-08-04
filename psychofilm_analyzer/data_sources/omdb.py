"""OMDb / IMDb enrichment."""

from __future__ import annotations

import logging
import re
from typing import Optional

from psychofilm_analyzer.data_sources.base import BaseSource
from psychofilm_analyzer.models import InputTitle, MediaType, SourcePayload
from psychofilm_analyzer.utils.text import safe_float, safe_int

logger = logging.getLogger(__name__)

OMDB_API = "https://www.omdbapi.com/"


class OmdbSource(BaseSource):
    name = "omdb"

    def __init__(self, *args, api_key: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self.api_key = api_key

    def _fetch(self, item: InputTitle) -> SourcePayload:
        if not self.api_key:
            return SourcePayload(source=self.name, found=False, error="OMDB_API_KEY not set")

        data = None
        # Prefer IMDb id if already known via extra
        imdb_id = (item.extra or {}).get("imdb_id")
        if imdb_id:
            data = self._by_id(imdb_id)

        if not data:
            omdb_type = None
            if item.media_type == MediaType.FILM:
                omdb_type = "movie"
            elif item.media_type in {MediaType.SERIES, MediaType.SEASON}:
                omdb_type = "series"
            for title in self._search_titles(item):
                data = self._by_title(title, item.year, omdb_type)
                if data:
                    break
                if item.year:
                    data = self._by_title(title, None, omdb_type)
                    if data:
                        break

        if not data or data.get("Response") == "False":
            return SourcePayload(
                source=self.name,
                found=False,
                error=(data or {}).get("Error", "not found"),
            )

        genres = [g.strip() for g in (data.get("Genre") or "").split(",") if g.strip()]
        directors = [d.strip() for d in (data.get("Director") or "").split(",") if d.strip() and d.strip() != "N/A"]
        writers = [w.strip() for w in (data.get("Writer") or "").split(",") if w.strip() and w.strip() != "N/A"]
        cast = [c.strip() for c in (data.get("Actors") or "").split(",") if c.strip() and c.strip() != "N/A"]
        awards_text = data.get("Awards") if data.get("Awards") not in (None, "N/A") else None
        awards = self._parse_awards(awards_text or "")
        plot = data.get("Plot") if data.get("Plot") not in (None, "N/A") else None
        rating = safe_float(data.get("imdbRating")) if data.get("imdbRating") not in (None, "N/A") else None
        votes = None
        if data.get("imdbVotes") and data.get("imdbVotes") != "N/A":
            votes = safe_int(str(data.get("imdbVotes")).replace(",", ""))
        year = self._parse_year(data.get("Year"), item.year)
        imdb_id = data.get("imdbID") if data.get("imdbID") not in (None, "N/A") else None
        url = f"https://www.imdb.com/title/{imdb_id}/" if imdb_id else None

        return SourcePayload(
            source=self.name,
            found=True,
            title=data.get("Title"),
            title_en=data.get("Title"),
            year=year,
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
            creators=writers[:5],
            cast=cast,
            cast_en=cast,
            rating=rating,
            votes=votes,
            awards_text=awards_text,
            awards=awards,
            runtime=self._parse_runtime(data.get("Runtime")),
            type=(data.get("Type") or "").lower() or None,
            imdb_id=imdb_id,
            url=url,
            language="en",
            extra={
                "rated": data.get("Rated"),
                "metascore": safe_float(data.get("Metascore")) if data.get("Metascore") not in (None, "N/A") else None,
                "ratings": data.get("Ratings") or [],
                "country": data.get("Country"),
                "languages": data.get("Language"),
                "box_office": data.get("BoxOffice"),
            },
        )

    def _by_id(self, imdb_id: str) -> Optional[dict]:
        return self.http.get(OMDB_API, params={"apikey": self.api_key, "i": imdb_id, "plot": "full"})

    def _by_title(self, title: str, year: Optional[int], omdb_type: Optional[str]) -> Optional[dict]:
        params = {"apikey": self.api_key, "t": title, "plot": "full"}
        if year:
            params["y"] = str(year)
        if omdb_type:
            params["type"] = omdb_type
        data = self.http.get(OMDB_API, params=params)
        if data and data.get("Response") == "True":
            return data
        return None

    @staticmethod
    def _parse_year(raw: Optional[str], fallback: Optional[int]) -> Optional[int]:
        if not raw or raw == "N/A":
            return fallback
        m = re.search(r"(\d{4})", str(raw))
        return int(m.group(1)) if m else fallback

    @staticmethod
    def _parse_runtime(raw: Optional[str]) -> Optional[int]:
        if not raw or raw == "N/A":
            return None
        m = re.search(r"(\d+)", str(raw))
        return int(m.group(1)) if m else None

    @staticmethod
    def _parse_awards(text: str) -> list[str]:
        if not text:
            return []
        # Keep whole string + split on periods for granular matching
        parts = [text.strip()]
        for chunk in re.split(r"[.;]", text):
            chunk = chunk.strip()
            if chunk and chunk not in parts:
                parts.append(chunk)
        return parts

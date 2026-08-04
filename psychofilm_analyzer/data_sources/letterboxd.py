"""Letterboxd lightweight enrichment (public film page scrape).

Polite, cached, and fully optional — failures never break the pipeline.
"""

from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import quote

from bs4 import BeautifulSoup

from psychofilm_analyzer.data_sources.base import BaseSource
from psychofilm_analyzer.models import InputTitle, SourcePayload
from psychofilm_analyzer.utils.text import slugify_key

logger = logging.getLogger(__name__)


class LetterboxdSource(BaseSource):
    name = "letterboxd"

    PSYCH_TAG_HINTS = (
        "psychological",
        "trauma",
        "jungian",
        "existential",
        "depression",
        "mental illness",
        "identity",
        "family",
        "coming of age",
        "surreal",
        "mindfuck",
        "character study",
        "slow cinema",
        "arthouse",
        "philosophy",
        "psychoanalysis",
        "grief",
        "loneliness",
        "madness",
        "abuse",
        "ptsd",
    )

    def _fetch(self, item: InputTitle) -> SourcePayload:
        slug_candidates = self._slugs(item)
        html = None
        url = None
        for slug in slug_candidates:
            url = f"https://letterboxd.com/film/{slug}/"
            try:
                html = self.http.get(url, as_json=False)
            except Exception:  # noqa: BLE001
                html = None
            if html and "Sorry, we can’t find the page" not in html and (
                "film-title" in html.lower() or 'property="og:title"' in html
            ):
                break
            html = None

        if not html:
            # try search page
            for title in self._search_titles(item)[:2]:
                search_url = f"https://letterboxd.com/search/films/{quote(title)}/"
                try:
                    search_html = self.http.get(search_url, as_json=False)
                except Exception:  # noqa: BLE001
                    continue
                slug = self._first_search_slug(search_html or "")
                if not slug:
                    continue
                url = f"https://letterboxd.com/film/{slug}/"
                try:
                    html = self.http.get(url, as_json=False)
                except Exception:  # noqa: BLE001
                    html = None
                if html:
                    break

        if not html:
            return SourcePayload(source=self.name, found=False, error="not found")

        soup = BeautifulSoup(html, "lxml")
        title_el = soup.select_one("h1.headline-1 span.name") or soup.select_one("h1.headline-1")
        title = title_el.get_text(strip=True) if title_el else None
        year_el = soup.select_one("small.number a") or soup.select_one(".film-title-wrapper small a")
        year = None
        if year_el:
            m = re.search(r"(\d{4})", year_el.get_text())
            year = int(m.group(1)) if m else None

        # genres / themes from tab
        tags: list[str] = []
        for a in soup.select("a[href*='/films/genre/'], a[href*='/films/theme/'], a[href*='/films/mini-theme/']"):
            t = a.get_text(strip=True)
            if t and t.lower() not in {x.lower() for x in tags}:
                tags.append(t)

        # popular tags sometimes in footer or js — scrape text mentions
        page_text = soup.get_text(" ", strip=True).lower()
        psych_hits = [h for h in self.PSYCH_TAG_HINTS if h in page_text]

        # description / tagline
        desc_el = soup.select_one(".truncate p") or soup.select_one("div.review p") or soup.select_one("meta[name=description]")
        overview = None
        if desc_el:
            overview = desc_el.get("content") if desc_el.name == "meta" else desc_el.get_text(" ", strip=True)

        # rating (average)
        rating = None
        rating_el = soup.select_one("meta[name='twitter:data2']") or soup.select_one(".average-rating .display-rating")
        if rating_el:
            raw = rating_el.get("content") or rating_el.get_text()
            m = re.search(r"(\d+(?:\.\d+)?)", raw or "")
            if m:
                rating = float(m.group(1))

        # directors
        directors = []
        for a in soup.select("a[href*='/director/']"):
            name = a.get_text(strip=True)
            if name and name not in directors:
                directors.append(name)

        keywords = tags + psych_hits
        return SourcePayload(
            source=self.name,
            found=True,
            title=title,
            year=year or item.year,
            overview=overview,
            genres=[t for t in tags if t],
            keywords=keywords,
            tags=psych_hits,
            directors=directors[:6],
            rating=rating,
            url=url,
            extra={"psych_tag_hits": psych_hits, "all_tags": tags},
        )

    def _slugs(self, item: InputTitle) -> list[str]:
        slugs = []
        for t in self._search_titles(item):
            base = slugify_key(t).replace("_", "-")
            if base:
                slugs.append(base)
            if item.year and base:
                slugs.append(f"{base}-{item.year}")
        # English preferred first
        return list(dict.fromkeys(slugs))

    @staticmethod
    def _first_search_slug(html: str) -> Optional[str]:
        m = re.search(r'href="/film/([a-z0-9-]+)/"', html)
        return m.group(1) if m else None

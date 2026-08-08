"""Letterboxd lightweight enrichment (public film page scrape).

Polite, cached, and fully optional — failures never break the pipeline.

Bulk policy (after Cloudflare blocks on /search/):
  - slug-only resolution (no search pages)
  - English title preferred
  - few candidates: bare slug first, then base-year (remakes)
  - soft-fail on empty/403/404 without retries
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from bs4 import BeautifulSoup

from psychofilm_analyzer.data_sources.base import BaseSource
from psychofilm_analyzer.models import InputTitle, SourcePayload
from psychofilm_analyzer.utils.text import slugify_key

logger = logging.getLogger(__name__)

# Cloudflare / bot interstitial markers
_BLOCK_MARKERS = (
    "just a moment",
    "cf-browser-verification",
    "attention required",
    "enable javascript and cookies",
)


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
        lb_cfg = (self.config.get("letterboxd") or {}) if isinstance(self.config, dict) else {}
        use_search = bool(lb_cfg.get("use_search", False))  # default OFF — Cloudflare blocks search
        max_slugs = int(lb_cfg.get("max_slugs", 3))

        slug_candidates = self._slugs(item)[: max(1, max_slugs)]
        html = None
        url = None
        last_error = "not found"

        for slug in slug_candidates:
            url = f"https://letterboxd.com/film/{slug}/"
            try:
                html = self.http.get(url, as_json=False)
            except Exception as exc:  # noqa: BLE001
                last_error = f"request failed: {type(exc).__name__}"
                html = None
                continue
            if not html:
                last_error = "empty response (404/403)"
                continue
            if self._is_blocked(html):
                last_error = "cloudflare/bot block"
                # Do not hammer more slugs after bot wall — same IP policy
                logger.debug("Letterboxd blocked on %s — skipping remaining slugs", url)
                html = None
                break
            if self._looks_like_film_page(html):
                break
            last_error = "page not a film"
            html = None

        # Search intentionally disabled by default (403 "Just a moment...")
        if not html and use_search:
            logger.debug("Letterboxd search fallback disabled by default; set letterboxd.use_search=true to enable")

        if not html:
            return SourcePayload(source=self.name, found=False, error=last_error)

        soup = BeautifulSoup(html, "lxml")
        title_el = soup.select_one("h1.headline-1 span.name") or soup.select_one("h1.headline-1")
        title = title_el.get_text(strip=True) if title_el else None
        year_el = soup.select_one("small.number a") or soup.select_one(".film-title-wrapper small a")
        year = None
        if year_el:
            m = re.search(r"(\d{4})", year_el.get_text())
            year = int(m.group(1)) if m else None

        tags: list[str] = []
        for a in soup.select(
            "a[href*='/films/genre/'], a[href*='/films/theme/'], a[href*='/films/mini-theme/']"
        ):
            t = a.get_text(strip=True)
            if t and t.lower() not in {x.lower() for x in tags}:
                tags.append(t)

        page_text = soup.get_text(" ", strip=True).lower()
        psych_hits = [h for h in self.PSYCH_TAG_HINTS if h in page_text]

        desc_el = (
            soup.select_one(".truncate p")
            or soup.select_one("div.review p")
            or soup.select_one("meta[name=description]")
        )
        overview = None
        if desc_el:
            overview = (
                desc_el.get("content") if desc_el.name == "meta" else desc_el.get_text(" ", strip=True)
            )

        rating = None
        rating_el = soup.select_one("meta[name='twitter:data2']") or soup.select_one(
            ".average-rating .display-rating"
        )
        if rating_el:
            raw = rating_el.get("content") or rating_el.get_text()
            m = re.search(r"(\d+(?:\.\d+)?)", raw or "")
            if m:
                rating = float(m.group(1))

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
            extra={"psych_tag_hits": psych_hits, "all_tags": tags, "slug_mode": True},
        )

    def _slugs(self, item: InputTitle) -> list[str]:
        """English-first slug candidates only (avoid RU transliteration / search)."""
        titles: list[str] = []
        for t in (item.english_title, item.title, item.import_title):
            if not t or not str(t).strip():
                continue
            s = str(t).strip()
            # Skip clearly Cyrillic-only titles for slug generation
            if re.search(r"[А-Яа-яЁё]", s) and not re.search(r"[A-Za-z]", s):
                continue
            if s not in titles:
                titles.append(s)

        slugs: list[str] = []
        for t in titles:
            base = slugify_key(t).replace("_", "-").strip("-")
            if not base:
                continue
            # Bare slug first (most Letterboxd pages); year form second for remakes.
            # Year-first caused ~50% waste 404→200 in bulk debug.
            slugs.append(base)
            if item.year:
                slugs.append(f"{base}-{item.year}")
        return list(dict.fromkeys(slugs))

    @staticmethod
    def _looks_like_film_page(html: str) -> bool:
        if not html:
            return False
        low = html.lower()
        if "sorry, we can’t find the page" in low or "sorry, we can't find the page" in low:
            return False
        return "film-title" in low or 'property="og:title"' in low or 'property="og:type" content="video.movie"' in low

    @staticmethod
    def _is_blocked(html: str) -> bool:
        if not html:
            return False
        low = html[:4000].lower()
        return any(m in low for m in _BLOCK_MARKERS)

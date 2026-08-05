"""Wikipedia enrichment (English + Russian, optional German) via MediaWiki API.

Designed to be polite: few candidates, early exit on strong match, cache-friendly.
Bulk mode: circuit-breaker on 429 so one throttle does not stall the catalog.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Optional
from urllib.parse import quote

from psychofilm_analyzer.data_sources.base import BaseSource
from psychofilm_analyzer.models import InputTitle, MediaType, SourcePayload
from psychofilm_analyzer.utils.http import RateLimitedError
from psychofilm_analyzer.utils.text import safe_int

logger = logging.getLogger(__name__)

# After a 429, pause further Wikipedia calls for this many seconds
WIKI_COOLDOWN_SEC = 45.0

FILM_MARKERS = (
    "film",
    "movie",
    "directed by",
    "television series",
    "tv series",
    "miniseries",
    "screenplay",
    "starring",
    "фильм",
    "сериал",
    "режиссёр",
    "режиссер",
    "телесериал",
    "fernsehserie",
    "spielfilm",
)

NON_FILM_MARKERS = (
    "is a street",
    "is a road",
    "is a highway",
    "is a city",
    "is a village",
    "is a town",
    "is a river",
    "is a mountain",
    "is a surname",
    "may refer to",
    "is a given name",
    "is an album",
    "is a song",
    "is an american actor",
    "is a british actor",
    "is a german actor",
    "is a producer",
    "is an editor",
    "award for",
    "премия",
)


class WikipediaSource(BaseSource):
    name = "wikipedia"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._cooldown_until = 0.0
        self._rate_limited = False

    def _on_rate_limit(self) -> None:
        self._rate_limited = True
        self._cooldown_until = time.monotonic() + WIKI_COOLDOWN_SEC
        logger.warning("Wikipedia cooldown %.0fs after rate limit", WIKI_COOLDOWN_SEC)

    def _wiki_available(self) -> bool:
        if time.monotonic() < self._cooldown_until:
            return False
        self._rate_limited = False
        return True

    def _fetch(self, item: InputTitle) -> SourcePayload:
        if not self._wiki_available():
            return SourcePayload(
                source=self.name,
                found=False,
                error="wikipedia cooldown after rate limit",
            )

        # Bulk-friendly: English is primary narrative bag; Russian only when EN found
        # or when the import title is clearly Russian (cheaper than always dual-lang).
        pages: dict[str, dict] = {}
        self._rate_limited = False
        langs = list((self.config.get("wikipedia") or {}).get("langs") or ["en", "ru"])
        # Prefer en first if present
        ordered = [l for l in ("en", "ru", "de") if l in langs] + [
            l for l in langs if l not in ("en", "ru", "de")
        ]

        for lang in ordered:
            if self._rate_limited or not self._wiki_available():
                break
            # Skip RU/DE probe when English already has a strong page (saves rate budget)
            if lang != "en" and "en" in pages and len((pages["en"].get("extract") or "")) > 200:
                # still try RU once if import is Russian-heavy
                if lang == "ru" and item.russian_title:
                    pass
                else:
                    continue
            page = self._find_page(item, lang)
            if page:
                pages[lang] = page

        if not pages:
            err = "rate limited" if self._rate_limited else "not found"
            return SourcePayload(source=self.name, found=False, error=err)

        primary = pages.get("en") or pages.get("ru") or next(iter(pages.values()))
        extract = primary.get("extract") or ""
        title = primary.get("title")
        url = primary.get("url")
        year = self._guess_year(extract, title) or item.year

        awards: list[str] = []
        directors: list[str] = []
        genres: list[str] = []
        keywords: list[str] = []
        # Prefer English page for people names (avoids cross-language false positives)
        ordered_pages = [pages[k] for k in ("en", "ru", "de") if k in pages]
        for p in ordered_pages:
            text = p.get("extract") or ""
            awards.extend(self._extract_awards(text))
            # only take directors from first good page to reduce contamination
            if not directors:
                directors = self._extract_directors(text)
            for g in self._extract_genres(text):
                if g not in genres:
                    genres.append(g)
            for k in self._extract_theme_keywords(text):
                if k not in keywords:
                    keywords.append(k)

        creators = self._extract_creators(extract)
        links = [p["url"] for p in pages.values() if p.get("url")]
        links_by_lang = {lang: p.get("url") for lang, p in pages.items() if p.get("url")}
        combined = "\n\n".join(f"[{lang}] {p.get('extract', '')[:3000]}" for lang, p in pages.items())
        overview_en = (pages.get("en") or {}).get("extract")
        overview_ru = (pages.get("ru") or {}).get("extract")
        directors_en = self._extract_directors((pages.get("en") or {}).get("extract") or "") if pages.get("en") else []
        directors_ru = self._extract_directors((pages.get("ru") or {}).get("extract") or "") if pages.get("ru") else []

        return SourcePayload(
            source=self.name,
            found=True,
            title=title,
            title_en=(pages.get("en") or {}).get("title") or (title if "en" in pages else None),
            title_ru=(pages.get("ru") or {}).get("title"),
            year=year,
            overview=extract[:2500] if extract else None,
            overview_en=(overview_en or "")[:2500] or None,
            overview_ru=(overview_ru or "")[:2500] or None,
            plot=extract[:1500] if extract else None,
            plot_en=(overview_en or "")[:1500] or None,
            plot_ru=(overview_ru or "")[:1500] or None,
            genres=genres,
            genres_en=genres,
            keywords=keywords,
            directors=directors[:8],
            directors_en=directors_en[:8] or directors[:8],
            directors_ru=directors_ru[:8],
            creators=creators[:6],
            writers=creators[:6],
            writers_en=creators[:6],
            awards=_dedupe(awards),
            awards_text="; ".join(_dedupe(awards)[:12]) or None,
            url=url,
            language="en" if "en" in pages else next(iter(pages)),
            type=(
                "tv"
                if re.search(r"television series|tv series|сериал|fernsehserie", extract, re.I)
                else "movie"
            ),
            extra={
                "langs": list(pages.keys()),
                "links": links,
                "links_by_lang": links_by_lang,
                "combined_text": combined[:10000],
                "page_titles": {lang: p.get("title") for lang, p in pages.items()},
            },
        )

    def _find_page(self, item: InputTitle, lang: str) -> Optional[dict]:
        if not self._wiki_available() or self._rate_limited:
            return None
        for title in self._candidate_titles(item, lang):
            if self._rate_limited:
                return None
            page = self._summary(lang, title)
            if not page:
                continue
            score = self._relevance_score(page, item)
            if score >= 5:
                return page

        # One search fallback (skip if already throttled)
        if self._rate_limited or not self._wiki_available():
            return None
        query = item.english_title or item.title
        if item.year:
            query = f"{query} {item.year}"
        if item.media_type in {MediaType.SERIES, MediaType.SEASON}:
            query = f"{query} television series"
        else:
            query = f"{query} film"
        hit = self._search(lang, query)
        if hit and not self._rate_limited:
            page = self._summary(lang, hit)
            if page and self._relevance_score(page, item) >= 4:
                return page
        return None

    def _candidate_titles(self, item: InputTitle, lang: str) -> list[str]:
        base = None
        if lang == "ru" and item.russian_title:
            base = item.russian_title
        elif item.english_title:
            base = item.english_title
        else:
            base = item.title
        if not base:
            return []

        is_tv = item.media_type in {MediaType.SERIES, MediaType.SEASON}
        out: list[str] = []
        # Best disambiguated form first, then bare title — max 3 tries
        if item.year and not is_tv:
            if lang == "ru":
                out.append(f"{base} (фильм, {item.year})")
            out.append(f"{base} ({item.year} film)")
        if is_tv:
            if lang == "ru":
                out.append(f"{base} (сериал)")
            out.append(f"{base} (TV series)")
            out.append(base)
        else:
            if lang == "ru":
                out.append(f"{base} (фильм)")
            else:
                out.append(f"{base} (film)")
            out.append(base)

        seen = set()
        uniq = []
        for t in out:
            k = t.lower()
            if k not in seen:
                seen.add(k)
                uniq.append(t)
        return uniq[:3]

    def _summary(self, lang: str, title: str) -> Optional[dict]:
        url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{quote(title, safe='')}"
        try:
            data = self.http.get(url)
        except RateLimitedError:
            self._on_rate_limit()
            return None
        except Exception:  # noqa: BLE001
            return None
        if not data or data.get("type") in {
            "https://mediawiki.org/wiki/HyperSwitch/errors/not_found",
            "disambiguation",
        }:
            return None
        extract = data.get("extract")
        if not extract:
            return None
        # REST summary is enough for bulk; skip extra MediaWiki extract call
        page_title = data.get("title") or title
        page_url = (data.get("content_urls") or {}).get("desktop", {}).get("page")
        return {
            "title": page_title,
            "extract": extract,
            "url": page_url or f"https://{lang}.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}",
            "lang": lang,
            "description": data.get("description") or "",
        }

    def _plain_extract(self, lang: str, title: str) -> Optional[str]:
        url = f"https://{lang}.wikipedia.org/w/api.php"
        try:
            data = self.http.get(
                url,
                params={
                    "action": "query",
                    "prop": "extracts",
                    "exintro": 1,
                    "explaintext": 1,
                    "titles": title,
                    "format": "json",
                    "redirects": 1,
                },
            )
        except Exception:  # noqa: BLE001
            return None
        pages = ((data or {}).get("query") or {}).get("pages") or {}
        for page in pages.values():
            if page.get("extract"):
                return page["extract"]
        return None

    def _search(self, lang: str, query: str) -> Optional[str]:
        url = f"https://{lang}.wikipedia.org/w/api.php"
        try:
            data = self.http.get(
                url,
                params={
                    "action": "query",
                    "list": "search",
                    "srsearch": query,
                    "srlimit": 3,
                    "format": "json",
                    "utf8": 1,
                },
            )
        except RateLimitedError:
            self._on_rate_limit()
            return None
        except Exception:  # noqa: BLE001
            return None
        hits = ((data or {}).get("query") or {}).get("search") or []
        for h in hits:
            title = h.get("title") or ""
            snippet = re.sub(r"<[^>]+>", "", h.get("snippet") or "").lower()
            blob = f"{title} {snippet}".lower()
            if any(m in blob for m in NON_FILM_MARKERS):
                continue
            if any(m in blob for m in FILM_MARKERS) or "film" in title.lower() or "фильм" in title.lower():
                return title
        return None

    def _relevance_score(self, page: dict, item: InputTitle) -> int:
        extract = (page.get("extract") or "").lower()
        title = (page.get("title") or "").lower()
        desc = (page.get("description") or "").lower()
        blob = f"{title} {desc} {extract[:700]}"
        score = 0

        if any(m in blob for m in NON_FILM_MARKERS):
            return -5
        # Person pages often start with birth year patterns without film framing
        if re.search(r"\bis an? (?:american|british|french|german|russian|italian).*(?:actor|director|producer)\b", extract[:200]):
            if "directed" not in extract[:200] and "film" not in title:
                return -3

        if any(m in blob for m in FILM_MARKERS):
            score += 5
        if "film" in title or "фильм" in title or "series" in title or "сериал" in title:
            score += 4
        if item.year and str(item.year) in blob:
            score += 3

        q = (item.english_title or item.title or "").lower()
        q_tokens = [t for t in re.findall(r"\w+", q) if len(t) > 2]
        if q_tokens:
            overlap = sum(1 for t in q_tokens if t in title)
            if overlap == 0 and item.russian_title:
                # allow russian page titles
                score += 1
            else:
                score += min(4, overlap * 2)
                if overlap == 0:
                    score -= 4

        if "directed by" in extract or "режиссёр" in extract or "режиссер" in extract or "created by" in extract:
            score += 2
        return score

    @staticmethod
    def _guess_year(extract: str, title: Optional[str]) -> Optional[int]:
        for text in (title or "", extract[:500]):
            m = re.search(r"\b((?:19|20)\d{2})\b", text or "")
            if m:
                return safe_int(m.group(1))
        return None

    @staticmethod
    def _extract_awards(text: str) -> list[str]:
        if not text:
            return []
        patterns = [
            r"((?:won|wins|winner of|awarded)\s+[^.]+(?:oscar|academy award|palme|golden (?:globe|lion|bear|eagle)|bafta|emmy|cesar|nika)[^.]*\.)",
            r"((?:Academy Award|Oscar|Palme d'Or|Golden Lion|Golden Bear|BAFTA|Golden Globe|Emmy|César|Nika)[^.]*\.)",
            r"((?:премия|оскар|золотой (?:глоб|лев|медведь|орёл)|ника|сезар)[^.]*\.)",
        ]
        found = []
        for pat in patterns:
            for m in re.finditer(pat, text, flags=re.IGNORECASE):
                found.append(m.group(1).strip())
        return found[:20]

    @staticmethod
    def _extract_genres(text: str) -> list[str]:
        genres = []
        for g in (
            "psychological thriller",
            "psychological drama",
            "avant-garde",
            "neo-noir",
            "crime drama",
            "superhero",
            "drama",
            "thriller",
            "horror",
            "comedy",
            "crime",
            "mystery",
            "biography",
            "documentary",
            "science fiction",
            "fantasy",
            "war",
            "romance",
            "action",
            "animation",
            "anthology",
        ):
            if re.search(rf"\b{re.escape(g)}\b", text or "", re.I):
                genres.append(g)
        return genres

    @staticmethod
    def _extract_directors(text: str) -> list[str]:
        if not text:
            return []
        patterns = [
            r"directed by ([A-ZА-ЯЁ][A-Za-zА-Яа-яЁё.'’\-]+(?:\s+[A-ZА-ЯЁ][A-Za-zА-Яа-яЁё.'’\-]+){0,3})",
            r"режисс[её]р(?:ом|а|ы)?\s+([A-ZА-ЯЁ][A-Za-zА-Яа-яЁё.'’\-]+(?:\s+[A-ZА-ЯЁ][A-Za-zА-Яа-яЁё.'’\-]+){0,3})",
            r"created by ([A-ZА-ЯЁ][A-Za-zА-Яа-яЁё.'’\-]+(?:\s+[A-ZА-ЯЁ][A-Za-zА-Яа-яЁё.'’\-]+){0,3})",
        ]
        names: list[str] = []
        for pat in patterns:
            for m in re.finditer(pat, text):
                name = m.group(1).strip().rstrip(".,;:")
                # stop at sentence boundary artifacts
                name = re.split(r"[.!?]", name)[0].strip()
                name = re.split(r"\s+(?:and|who|which|from|in|with|Its|The|A)\s+", name, maxsplit=1)[0]
                if len(name) > 2 and name not in names:
                    names.append(name)
        return names

    @staticmethod
    def _extract_creators(text: str) -> list[str]:
        if not text:
            return []
        m = re.search(
            r"created by ([A-ZА-ЯЁ][\w.'’\-]+(?:\s+[A-ZА-ЯЁ][\w.'’\-]+(?:\s+[A-ZА-ЯЁ][\w.'’\-]+)?)?)",
            text,
        )
        return [m.group(1).strip()] if m else []

    @staticmethod
    def _extract_theme_keywords(text: str) -> list[str]:
        if not text:
            return []
        signals = [
            "psychological",
            "psychoanalytic",
            "identity",
            "trauma",
            "dream",
            "nightmare",
            "memory",
            "unconscious",
            "surreal",
            "existential",
            "madness",
            "insanity",
            "depression",
            "grief",
            "family",
            "mother",
            "father",
            "childhood",
            "coming of age",
            "persona",
            "double life",
            "unreliable",
            "alienation",
            "loneliness",
            "guilt",
            "power",
            "totalitarian",
            "philosophical",
            "consciousness",
            "avant-garde",
            "dissociative",
            "split personality",
            "psychosis",
            "nihilism",
            "психолог",
            "травм",
            "идентичн",
            "экзистен",
            "безумие",
            "детств",
            "память",
        ]
        low = text.lower()
        return [s for s in signals if s in low]


def _dedupe(items: list[str]) -> list[str]:
    seen = set()
    out = []
    for a in items:
        key = a.lower()
        if key not in seen:
            seen.add(key)
            out.append(a)
    return out

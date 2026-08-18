#!/usr/bin/env python3
"""Resolve English / Russian / original titles with year + name confidence.

Writes a title only when the candidate is likely the same film:
  - year must match when we know the production year (exact, or inside a collection range)
  - name similarity must clear a high threshold
  - title_en / title_ru / title_original are filled from the correct API fields
    and are never swapped.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CSV_DEFAULT = Path(r"C:\Scripts\Grk\hdhdd.ru\kinemateka\media_occurrences.csv")
LOOKUP_DEFAULT = Path(r"C:\Scripts\Grk\hdhdd.ru\kinemateka\title_match_lookup.json")

CYR = re.compile(r"[А-Яа-яЁёІіЇїЄєҐґ]")
LAT = re.compile(r"[A-Za-z]")
YEAR_RE = re.compile(r"(?:19|20)\d{2}")
HTMLISH = re.compile(r"(class=|href=|<em|<a |<img|download_to_disk|next_button|film_poster|</?[a-z])", re.I)
FILEISH = re.compile(r"(dvb|x264|x265|hdtv|webrip|bluray|1080p|720p|mkv|mp4|_кум_)", re.I)
NON_EN = re.compile(
    r"[А-Яа-яЁёІіЇїЄєҐґąčęėįšųūžĄČĘĖĮŠŲŪŽäöüßÄÖÜéèêëàâùûôîïñáíóúãõąćęłńśźżÁÉÍÓÚÀÈÌÒÙÂÊÎÔÛÃÕÄËÏÖÜ]"
)
LATIN_RUN = r"[A-Za-z][A-Za-z0-9'’:,!?\-&().]*(?:[ \t]+[A-Za-z0-9'’:,!?\-&().]+)*"

JUNK_EXACT = {
    "мультфильмы", "документальные", "фильмы", "качество", "новинки",
    "концерты", "сериалы", "меню", "бонус", "bonus",
}
JUNK_SUBSTR = ("рублей", "телеграм", "звонки", "скачать", "на диск", "великого режиссера")

# Accept if combined score >= this. Year mismatch is a hard reject.
MIN_SCORE_WITH_YEAR = 0.62
MIN_SCORE_NO_YEAR = 0.78
MIN_NAME = 0.42
YEAR_SLACK = 1  # ±1 year allowed when a single year is known


@dataclass
class Hit:
    title_en: str | None = None
    title_ru: str | None = None
    title_original: str | None = None
    year: int | None = None
    original_language: str | None = None
    source: str = ""
    score: float = 0.0
    name_score: float = 0.0
    year_score: float = 0.0
    tmdb_id: int | None = None
    kinopoisk_id: int | None = None
    query: str = ""


def norm(text: str | None) -> str:
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", str(text)).strip().lower().replace("ё", "е")
    s = s.replace("і", "и").replace("ї", "и")
    s = re.sub(r"[«»“”\"'`]", "", s)
    s = re.sub(r"\b3[dд]\b", " ", s, flags=re.I)
    s = re.sub(r"[^\w\s]+", " ", s, flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip()


def tokens(text: str | None) -> set[str]:
    return {t for t in norm(text).split() if len(t) > 1}


def name_sim(a: str | None, b: str | None) -> float:
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        shorter = min(len(na), len(nb))
        return 0.88 if shorter >= 5 else 0.62
    seq = SequenceMatcher(None, na, nb).ratio()
    ta, tb = tokens(a), tokens(b)
    jacc = (len(ta & tb) / len(ta | tb)) if ta and tb else 0.0
    return max(seq, jacc)


def looks_cyr(text: str | None) -> bool:
    return bool(text) and len(CYR.findall(str(text))) > len(LAT.findall(str(text)))


def looks_latin(text: str | None) -> bool:
    return bool(text) and len(LAT.findall(str(text))) >= 2 and len(LAT.findall(str(text))) >= len(CYR.findall(str(text)))


def is_english(text: str | None) -> bool:
    if not text or not looks_latin(text):
        return False
    if CYR.search(str(text)) or NON_EN.search(str(text)):
        return False
    if FILEISH.search(str(text)) or HTMLISH.search(str(text)):
        return False
    return len(LAT.findall(str(text))) >= 2


def clean_text(text: str | None) -> str | None:
    if not text:
        return None
    s = unicodedata.normalize("NFKC", str(text)).strip()
    s = re.sub(r"\s+", " ", s).strip(" .-–—/")
    return s or None


def is_junk(raw: str | None) -> bool:
    if not raw or not str(raw).strip():
        return True
    s = str(raw).strip()
    if HTMLISH.search(s) or FILEISH.search(s):
        return True
    k = norm(s)
    if k in JUNK_EXACT:
        return True
    if any(tok in k for tok in JUNK_SUBSTR):
        return True
    if re.fullmatch(r"\d+", k or ""):
        return True
    return False


def extract_embedded_en(title: str | None) -> str | None:
    if not title:
        return None
    s = unicodedata.normalize("NFKC", str(title)).strip()
    s = re.sub(r"\s+", " ", s)
    if FILEISH.search(s) or not (CYR.search(s) and LAT.search(s)):
        return None
    cands: list[str] = []
    for rx in (
        rf"(?:3[dд]\s*[.]|[.])\s*({LATIN_RUN})",
        rf"/\s*({LATIN_RUN})\s*/?",
        rf"\)\s*({LATIN_RUN})",
        rf"3[dд]\s+({LATIN_RUN})",
        rf"[А-Яа-яЁё][^A-Za-z/]{{0,10}}\s+({LATIN_RUN})\s*$",
        rf"^({LATIN_RUN})\s*\([^)]*[А-Яа-яЁё]",
        rf"^({LATIN_RUN})\s+[А-Яа-яЁё]",
        rf"\(({LATIN_RUN})\)",
    ):
        for m in re.finditer(rx, s, flags=re.I):
            cands.append(m.group(1))
    best = None
    for raw in cands:
        c = clean_text(re.sub(r"\b3[dD]\b", "", raw))
        if is_english(c) and (best is None or len(c) > len(best)):
            best = c
    return best


def ru_only(title: str | None) -> str:
    if not title:
        return ""
    s = str(title).strip()
    en = extract_embedded_en(s)
    if en:
        s = re.sub(re.escape(en), " ", s, count=1, flags=re.I)
    s = re.sub(r"\b3[dд]\b", " ", s, flags=re.I)
    s = re.sub(r"\(\+?18!?\)", " ", s)
    s = re.sub(r"\(бонус\)", " ", s, flags=re.I)
    s = re.sub(r"\s*/\s*$", "", s)
    return re.sub(r"\s+", " ", s).strip(" ./|-–—")


def years_from_collection(collection: str | None) -> tuple[int | None, int | None]:
    if not collection:
        return None, None
    s = str(collection)
    # 2010-2012 or 2018 - 2019
    m = re.search(r"(19|20)(\d{2})\s*[-–—]\s*(?:(19|20))?(\d{2})", s)
    if m:
        y1 = int(m.group(1) + m.group(2))
        y2 = int((m.group(3) or m.group(1)) + m.group(4))
        if y2 < y1:
            y2 = int(str(y1)[:2] + m.group(4))
        return y1, y2
    # '09-'10 or '15
    m = re.search(r"'(\d{2})\s*[-–—]\s*'(\d{2})", s)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        return 2000 + a, 2000 + b
    m = re.search(r"'(\d{2})\b", s)
    if m:
        y = 2000 + int(m.group(1))
        return y, y
    years = [int(x) for x in YEAR_RE.findall(s)]
    if len(years) == 1:
        return years[0], years[0]
    if len(years) >= 2:
        return min(years), max(years)
    return None, None


def year_from_text(*parts: str | None) -> int | None:
    for p in parts:
        if not p or not isinstance(p, str):
            continue
        found = [int(x) for x in YEAR_RE.findall(p) if 1880 <= int(x) <= 2035]
        if found:
            return found[-1]
    return None


def year_score(cand_year: int | None, want: int | None, y0: int | None, y1: int | None) -> float:
    if cand_year is None:
        return 0.35 if want is None and y0 is None else 0.15
    if want is not None:
        d = abs(cand_year - want)
        if d == 0:
            return 1.0
        if d <= YEAR_SLACK:
            return 0.72
        return 0.0  # hard miss
    if y0 is not None and y1 is not None:
        if y0 <= cand_year <= y1:
            return 1.0
        if cand_year == y0 - 1 or cand_year == y1 + 1:
            return 0.55
        return 0.0
    return 0.45  # no year constraint


def parse_year(val) -> int | None:
    try:
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return None
        y = int(float(val))
        return y if 1880 <= y <= 2035 else None
    except (TypeError, ValueError):
        return None


def pick_en(original: str | None, localized_en: str | None, orig_lang: str | None) -> str | None:
    """English field only. Never put FR/LT/DE/RU here."""
    lang = (orig_lang or "").lower()
    if lang == "en" and is_english(original):
        return clean_text(original)
    if is_english(localized_en):
        return clean_text(localized_en)
    if lang == "en" and is_english(localized_en):
        return clean_text(localized_en)
    return None


def pick_ru(localized_ru: str | None, listed_ru: str | None, original: str | None, orig_lang: str | None) -> str | None:
    if localized_ru and looks_cyr(localized_ru):
        return clean_text(localized_ru)
    if listed_ru and looks_cyr(listed_ru):
        return clean_text(ru_only(listed_ru) or listed_ru)
    if (orig_lang or "").lower() == "ru" and original and looks_cyr(original):
        return clean_text(original)
    return clean_text(listed_ru) if listed_ru and looks_cyr(listed_ru) else None


def accept(hit: Hit, have_year: bool) -> bool:
    if hit.score <= 0:
        return False
    if hit.year_score == 0.0 and have_year:
        return False
    if hit.name_score < MIN_NAME:
        return False
    need = MIN_SCORE_WITH_YEAR if have_year else MIN_SCORE_NO_YEAR
    if hit.score < need:
        return False
    # must have at least one trustworthy name field
    if not (hit.title_en or hit.title_ru or hit.title_original):
        return False
    return True


def score_candidate(
    *,
    query_ru: str,
    query_en: str | None,
    cand_ru: str | None,
    cand_en: str | None,
    cand_orig: str | None,
    cand_year: int | None,
    want_year: int | None,
    range0: int | None,
    range1: int | None,
) -> tuple[float, float, float]:
    ns = max(
        name_sim(query_ru, cand_ru),
        name_sim(query_ru, cand_orig),
        name_sim(query_ru, cand_en),
        name_sim(query_en, cand_en) if query_en else 0.0,
        name_sim(query_en, cand_orig) if query_en else 0.0,
    )
    ys = year_score(cand_year, want_year, range0, range1)
    have_year = want_year is not None or range0 is not None
    if have_year:
        combined = 0.50 * ns + 0.50 * ys
        if ys == 0.0:
            combined = 0.0
    else:
        combined = ns
    return combined, ns, ys


def tmdb_search(session: requests.Session, api_key: str, query: str, year: int | None, is_tv: bool, lang: str) -> list[dict]:
    media = "tv" if is_tv else "movie"
    params = {
        "api_key": api_key,
        "query": query,
        "include_adult": "false",
        "language": lang,
    }
    if year and media == "movie":
        params["year"] = year
        params["primary_release_year"] = year
    if year and media == "tv":
        params["first_air_date_year"] = year
    url = f"https://api.themoviedb.org/3/search/{media}"
    r = session.get(url, params=params, timeout=20)
    if r.status_code == 429:
        time.sleep(1.5)
        r = session.get(url, params=params, timeout=20)
    r.raise_for_status()
    return (r.json() or {}).get("results") or []


def tmdb_details(session: requests.Session, api_key: str, tmdb_id: int, is_tv: bool, lang: str) -> dict | None:
    media = "tv" if is_tv else "movie"
    r = session.get(
        f"https://api.themoviedb.org/3/{media}/{tmdb_id}",
        params={"api_key": api_key, "language": lang},
        timeout=20,
    )
    if r.status_code == 429:
        time.sleep(1.5)
        r = session.get(
            f"https://api.themoviedb.org/3/{media}/{tmdb_id}",
            params={"api_key": api_key, "language": lang},
            timeout=20,
        )
    if not r.ok:
        return None
    return r.json()


def tmdb_resolve(
    session: requests.Session,
    api_key: str,
    query_ru: str,
    query_en: str | None,
    listed_ru: str | None,
    want_year: int | None,
    range0: int | None,
    range1: int | None,
    is_tv: bool,
) -> Hit | None:
    # Search in Russian first (better for this catalog), then English if needed
    results = []
    q = query_ru or query_en or ""
    if not q:
        return None
    try:
        results = tmdb_search(session, api_key, q, want_year, is_tv, "ru-RU")
        if not results and query_en and query_en != q:
            results = tmdb_search(session, api_key, query_en, want_year, is_tv, "en-US")
        if not results and want_year:
            results = tmdb_search(session, api_key, q, None, is_tv, "ru-RU")
    except Exception:
        return None
    if not results:
        return None

    scored: list[tuple[float, float, float, dict, int | None]] = []
    for it in results[:8]:
        date = it.get("release_date") or it.get("first_air_date") or ""
        cy = int(date[:4]) if date[:4].isdigit() else None
        cand_ru = it.get("title") or it.get("name")
        cand_orig = it.get("original_title") or it.get("original_name")
        comb, ns, ys = score_candidate(
            query_ru=query_ru or q,
            query_en=query_en,
            cand_ru=cand_ru,
            cand_en=None,
            cand_orig=cand_orig,
            cand_year=cy,
            want_year=want_year,
            range0=range0,
            range1=range1,
        )
        scored.append((comb, ns, ys, it, cy))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    comb, ns, ys, pick, cy = scored[0]
    have_year = want_year is not None or range0 is not None
    if not accept(Hit(score=comb, name_score=ns, year_score=ys, title_ru="x"), have_year):
        return None

    tmdb_id = pick.get("id")
    orig_lang = (pick.get("original_language") or "").lower() or None
    ru_title = pick.get("title") or pick.get("name")
    original = pick.get("original_title") or pick.get("original_name")
    en_title = None
    if orig_lang == "en":
        en_title = original
    else:
        det = tmdb_details(session, api_key, int(tmdb_id), is_tv, "en-US") if tmdb_id else None
        if det:
            en_title = det.get("title") or det.get("name")
            if not original:
                original = det.get("original_title") or det.get("original_name")
            if not cy:
                date = det.get("release_date") or det.get("first_air_date") or ""
                cy = int(date[:4]) if date[:4].isdigit() else None

    hit = Hit(
        title_en=pick_en(original, en_title, orig_lang),
        title_ru=pick_ru(ru_title, listed_ru, original, orig_lang),
        title_original=clean_text(original),
        year=cy,
        original_language=orig_lang,
        source="tmdb",
        score=round(comb, 3),
        name_score=round(ns, 3),
        year_score=round(ys, 3),
        tmdb_id=int(tmdb_id) if tmdb_id else None,
        query=q,
    )
    return hit if accept(hit, have_year) else None


def kp_resolve(
    session: requests.Session,
    api_key: str,
    query_ru: str,
    query_en: str | None,
    listed_ru: str | None,
    want_year: int | None,
    range0: int | None,
    range1: int | None,
) -> Hit | None:
    q = query_ru or query_en or ""
    if not q:
        return None
    r = session.get(
        "https://kinopoiskapiunofficial.tech/api/v2.1/films/search-by-keyword",
        params={"keyword": q, "page": 1},
        headers={"X-API-KEY": api_key},
        timeout=20,
    )
    if r.status_code == 429:
        time.sleep(3)
        r = session.get(
            "https://kinopoiskapiunofficial.tech/api/v2.1/films/search-by-keyword",
            params={"keyword": q, "page": 1},
            headers={"X-API-KEY": api_key},
            timeout=20,
        )
    r.raise_for_status()
    films = (r.json() or {}).get("films") or []
    if not films:
        return None
    scored = []
    for f in films[:8]:
        try:
            cy = int(str(f.get("year") or "").split("-")[0])
        except ValueError:
            cy = None
        comb, ns, ys = score_candidate(
            query_ru=query_ru or q,
            query_en=query_en,
            cand_ru=f.get("nameRu"),
            cand_en=f.get("nameEn"),
            cand_orig=f.get("nameOriginal") or f.get("nameEn"),
            cand_year=cy,
            want_year=want_year,
            range0=range0,
            range1=range1,
        )
        scored.append((comb, ns, ys, f, cy))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    comb, ns, ys, pick, cy = scored[0]
    have_year = want_year is not None or range0 is not None
    name_en = pick.get("nameEn")
    name_orig = pick.get("nameOriginal") or name_en
    hit = Hit(
        title_en=pick_en(name_orig, name_en, None) or (clean_text(name_en) if is_english(name_en) else None),
        title_ru=pick_ru(pick.get("nameRu"), listed_ru, name_orig, "ru" if looks_cyr(pick.get("nameRu")) else None),
        title_original=clean_text(name_orig),
        year=cy,
        original_language=None,
        source="kinopoisk",
        score=round(comb, 3),
        name_score=round(ns, 3),
        year_score=round(ys, 3),
        kinopoisk_id=pick.get("filmId") or pick.get("kinopoiskId"),
        query=q,
    )
    return hit if accept(hit, have_year) else None


def row_queries(row) -> tuple[str, str | None, str | None, bool]:
    ru_raw = row.get("title_ru") if pd.notna(row.get("title_ru")) else None
    en_raw = row.get("title_en") if pd.notna(row.get("title_en")) else None
    raw = row.get("raw") if pd.notna(row.get("raw")) else None
    listed = ru_raw or raw or en_raw
    q_ru = ru_only(ru_raw or raw or "")
    q_en = extract_embedded_en(ru_raw) or extract_embedded_en(raw)
    if en_raw and is_english(str(en_raw)) and not is_junk(str(en_raw)):
        q_en = q_en or clean_text(str(en_raw))
    is_tv = str(row.get("media_type") or "") in {"series", "animated_series", "tv_program"}
    return q_ru, q_en, listed, is_tv


def row_years(row) -> tuple[int | None, int | None, int | None]:
    want = parse_year(row.get("year"))
    if want is None:
        want = year_from_text(str(row.get("title_ru") or ""), str(row.get("title_en") or ""), str(row.get("raw") or ""))
    r0, r1 = years_from_collection(row.get("collection") if pd.notna(row.get("collection")) else None)
    return want, r0, r1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=str(CSV_DEFAULT))
    ap.add_argument("--lookup", default=str(LOOKUP_DEFAULT))
    ap.add_argument("--out-csv", default="")
    ap.add_argument("--gather-csv", default="")
    ap.add_argument("--no-api", action="store_true")
    ap.add_argument("--max-api", type=int, default=0)
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    load_dotenv(Path(r"C:\Scripts\Grk\hdhdd.ru\.env"))
    tmdb_key = os.getenv("TMDB_API_KEY", "").strip()
    kp_key = os.getenv("KINOPOISK_API_KEY", "").strip()

    src = Path(args.csv)
    bak = src.with_suffix(src.suffix + ".bak")
    if bak.exists():
        df = pd.read_csv(bak)
        print(f"loaded backup {bak} rows={len(df)}", flush=True)
    else:
        df = pd.read_csv(src)
        bak.write_bytes(src.read_bytes())
        print(f"loaded {src} rows={len(df)}; wrote {bak}", flush=True)

    lookup_path = Path(args.lookup)
    lookup: dict[str, dict] = {}
    if lookup_path.exists():
        lookup = json.loads(lookup_path.read_text(encoding="utf-8"))
        print(f"resume lookup {len(lookup)} keys", flush=True)

    # unique work items
    jobs = []
    seen = set()
    for i, row in df.iterrows():
        listed = row.get("title_ru") if pd.notna(row.get("title_ru")) else row.get("title_en")
        if is_junk(None if pd.isna(listed) else str(listed)) and is_junk(str(row.get("raw") or "")):
            continue
        q_ru, q_en, listed_s, is_tv = row_queries(row)
        want, r0, r1 = row_years(row)
        key = f"{norm(q_ru or q_en or '')}|{want or ''}|{r0 or ''}-{r1 or ''}|{int(is_tv)}"
        if not (q_ru or q_en):
            continue
        if key in seen:
            continue
        seen.add(key)
        prev = lookup.get(key)
        if prev and prev.get("done"):
            continue
        jobs.append((key, q_ru, q_en, listed_s, want, r0, r1, is_tv))

    print(f"unique titles to resolve via API: {len(jobs)} (already cached {sum(1 for v in lookup.values() if v.get('done'))})", flush=True)

    if not args.no_api and jobs:
        todo = jobs[: args.max_api] if args.max_api else jobs
        session = requests.Session()
        session.headers.update({"User-Agent": "PsychoFilmAnalyzer/1.1 (strict-title-resolve)"})

        def one(job):
            key, q_ru, q_en, listed_s, want, r0, r1, is_tv = job
            hit = None
            err = None
            try:
                if tmdb_key:
                    hit = tmdb_resolve(session, tmdb_key, q_ru, q_en, listed_s, want, r0, r1, is_tv)
            except Exception as exc:
                err = f"tmdb:{exc}"
            return key, q_ru, q_en, listed_s, want, r0, r1, hit, err

        misses = []
        done_n = 0
        accepted = 0
        with ThreadPoolExecutor(max_workers=6) as pool:
            futs = [pool.submit(one, j) for j in todo]
            for fut in as_completed(futs):
                key, q_ru, q_en, listed_s, want, r0, r1, hit, err = fut.result()
                done_n += 1
                if hit:
                    lookup[key] = {**asdict(hit), "done": True}
                    accepted += 1
                else:
                    misses.append((key, q_ru, q_en, listed_s, want, r0, r1))
                    lookup[key] = {
                        "done": False,
                        "query": q_ru or q_en,
                        "source": "tmdb_miss",
                        "error": err,
                    }
                if done_n % 80 == 0 or done_n == len(todo):
                    lookup_path.write_text(json.dumps(lookup, ensure_ascii=False, indent=2), encoding="utf-8")
                    print(f"  [tmdb {done_n}/{len(todo)}] accepted={accepted} miss={len(misses)}", flush=True)

        if kp_key and misses:
            print(f"Kinopoisk for {len(misses)} TMDB misses (year-aware)…", flush=True)
            kp_ok = 0
            for n, (key, q_ru, q_en, listed_s, want, r0, r1) in enumerate(misses, 1):
                hit = None
                try:
                    hit = kp_resolve(session, kp_key, q_ru, q_en, listed_s, want, r0, r1)
                    time.sleep(2.1)
                except Exception as exc:
                    print(f"  [kp {n}] FAIL {q_ru!r}: {exc}", flush=True)
                if hit:
                    lookup[key] = {**asdict(hit), "done": True}
                    kp_ok += 1
                else:
                    lookup[key] = {
                        "done": True,
                        "accepted": False,
                        "query": q_ru or q_en,
                        "source": "no_confident_match",
                    }
                if n % 20 == 0 or n == len(misses):
                    lookup_path.write_text(json.dumps(lookup, ensure_ascii=False, indent=2), encoding="utf-8")
                    print(f"  [kp {n}/{len(misses)}] accepted={kp_ok}", flush=True)

    # apply to every row
    out_en, out_ru, out_orig, out_year, out_score, out_src, skip = [], [], [], [], [], [], []
    for _, row in df.iterrows():
        listed = row.get("title_ru") if pd.notna(row.get("title_ru")) else row.get("title_en")
        raw = row.get("raw") if pd.notna(row.get("raw")) else None
        if is_junk(None if pd.isna(listed) else str(listed)) and is_junk(str(raw or "")):
            skip.append(True)
            out_en.append(None)
            out_ru.append(None)
            out_orig.append(None)
            out_year.append(parse_year(row.get("year")))
            out_score.append(None)
            out_src.append("junk")
            continue
        skip.append(False)
        q_ru, q_en, listed_s, is_tv = row_queries(row)
        want, r0, r1 = row_years(row)
        key = f"{norm(q_ru or q_en or '')}|{want or ''}|{r0 or ''}-{r1 or ''}|{int(is_tv)}"
        rec = lookup.get(key) or {}

        title_en = rec.get("title_en") if rec.get("done") and rec.get("score", 0) else None
        title_ru = rec.get("title_ru") if rec.get("done") else None
        title_orig = rec.get("title_original") if rec.get("done") else None
        year = rec.get("year") or want
        score = rec.get("score")
        source = rec.get("source") or ""

        # embedded English is allowed only as title_en, and only if it looks English
        embedded = q_en
        if embedded and is_english(embedded):
            if not title_en:
                title_en = embedded
                source = source or "embedded"
                score = score or 0.75
            elif not is_english(title_en):
                title_en = embedded

        # never put non-English into title_en
        if title_en and not is_english(title_en):
            title_en = None
        # never put Latin-only into title_ru
        if title_ru and not looks_cyr(title_ru):
            title_ru = ru_only(listed_s) if listed_s and looks_cyr(listed_s) else None
        if not title_ru and listed_s and looks_cyr(listed_s):
            title_ru = ru_only(listed_s) or clean_text(listed_s)

        # if API rejected, keep listed Russian only — do not invent English
        if rec.get("source") == "no_confident_match" and not embedded:
            title_en = None
            score = 0.0
            source = "unresolved"

        out_en.append(title_en)
        out_ru.append(title_ru)
        out_orig.append(title_orig)
        out_year.append(year)
        out_score.append(score)
        out_src.append(source or "unresolved")

    df["title_en"] = out_en
    df["title_ru"] = out_ru
    df["title_original"] = out_orig
    df["year"] = out_year
    df["match_score"] = out_score
    df["match_source"] = out_src
    df["gather_skip"] = skip

    out_csv = Path(args.out_csv) if args.out_csv else src.with_name("media_occurrences_en.csv")
    gather_csv = Path(args.gather_csv) if args.gather_csv else src.with_name("media_occurrences_gather.csv")
    out_csv.write_text("", encoding="utf-8")  # touch
    df.to_csv(out_csv, index=False, encoding="utf-8-sig")

    # update the user file: title_en / title_ru / year, plus original as extra column
    keep = list(pd.read_csv(bak, nrows=0).columns)
    if "title_original" not in keep:
        keep = keep + ["title_original"]
    df[keep].to_csv(src, index=False, encoding="utf-8-sig")

    g = df[~df["gather_skip"]].copy()
    g = g[g["title_ru"].notna() | g["title_en"].notna()]
    g.to_csv(gather_csv, index=False, encoding="utf-8-sig")
    lookup_path.write_text(json.dumps(lookup, ensure_ascii=False, indent=2), encoding="utf-8")

    filled = int(pd.Series(out_en).notna().sum())
    print("\n=== STRICT RESOLVE ===")
    print(f"rows {len(df)}  title_en filled {filled} ({filled/len(df):.1%})")
    print(f"title_original filled {pd.Series(out_orig).notna().sum()}")
    print(f"gather rows {len(g)}")
    print(pd.Series(out_src).value_counts().to_string())
    print("check samples:")
    for q in [
        "Начальник Чукотки",
        "Неподдающиеся",
        "Никто не хотел умирать",
        "Новая Москва",
        "Жизнь Пи",
        "Холодное Сердце",
        "Планета людей",
    ]:
        hit = df[df["title_ru"].fillna("").astype(str).str.contains(q, regex=False)]
        if len(hit) == 0:
            hit = df[df["match_source"].notna() & df.apply(lambda r: q.lower() in str(r.get("title_ru") or "").lower(), axis=1)]
        if len(hit):
            r = hit.iloc[0]
            print(
                f"  ru={r.get('title_ru')!r}  en={r.get('title_en')!r}  "
                f"orig={r.get('title_original')!r}  year={r.get('year')}  "
                f"score={r.get('match_score')}  src={r.get('match_source')}"
            )
    print(f"wrote {out_csv}")
    print(f"wrote {gather_csv}")
    print(f"updated {src}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

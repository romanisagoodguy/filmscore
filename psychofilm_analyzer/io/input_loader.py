"""Flexible Excel/CSV/list input loaders for film collections."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

from psychofilm_analyzer.models import InputTitle, MediaType
from psychofilm_analyzer.utils.text import normalize_title, parse_season_from_title, safe_float, safe_int

# Flexible column aliases (ORDERED lists — first match wins; never use unordered sets)
TITLE_ALIASES = [
    "название",
    "названиe",
    "title",
    "name",
    "film",
    "movie",
    "фильм",
    "original title",
]
YEAR_ALIASES = ["год выхода", "year", "год", "release year", "release_year"]
TYPE_ALIASES = ["type", "media_type", "тип", "format"]
LANG_ALIASES = ["language", "original language", "original_language", "язык", "язык оригинала"]
RATING_ALIASES = ["rating", "existing rating", "рейтинг", "score"]
NOTES_ALIASES = ["notes", "note", "про что это?", "comment", "comments", "заметки"]
EN_ALIASES = ["английское название", "english title", "english_title", "title_en", "en", "eng"]
RU_ALIASES = ["русское название", "russian title", "russian_title", "title_ru", "ru", "название"]
GENRE_ALIASES = ["genre", "genres", "жанр"]
DIR_ALIASES = ["director", "directors", "режиссер", "режиссёр", "режис-серы", "режиссеры"]
ACTOR_ALIASES = ["actors", "актеры", "актёры", "cast"]
ID_ALIASES = ["id", "input_id", "source_id", "film_id"]
IMDB_ID_ALIASES = ["imdb_id", "imdbid", "imdb id", "tt"]
TMDB_ID_ALIASES = ["tmdb_id", "tmdbid", "tmdb id"]
KP_ID_ALIASES = ["kinopoisk_id", "kp_id", "кинопоиск id"]
IMDB_ALIASES = ["imdb", "imdb rating", "imdb_rating", "imdbrating"]  # rating, not id
KP_ALIASES = ["kinopoisk", "kp", "кино-поиск", "кинопоиск", "kinopoisk_rating"]  # rating
COUNTRY_ALIASES = ["country", "страна"]
SEASON_ALIASES = ["season", "сезон", "s"]


def _norm_col(c: str) -> str:
    return re.sub(r"\s+", " ", str(c).strip().lower())


def _map_columns(columns: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    norms = {_norm_col(c): c for c in columns}
    groups = {
        "title": TITLE_ALIASES,
        "year": YEAR_ALIASES,
        "type": TYPE_ALIASES,
        "language": LANG_ALIASES,
        "rating": RATING_ALIASES,
        "notes": NOTES_ALIASES,
        "english_title": EN_ALIASES,
        "russian_title": RU_ALIASES,
        "genre": GENRE_ALIASES,
        "director": DIR_ALIASES,
        "actors": ACTOR_ALIASES,
        "input_id": ID_ALIASES,
        "imdb_id": IMDB_ID_ALIASES,
        "tmdb_id": TMDB_ID_ALIASES,
        "kinopoisk_id": KP_ID_ALIASES,
        "imdb": IMDB_ALIASES,
        "kinopoisk": KP_ALIASES,
        "country": COUNTRY_ALIASES,
        "season": SEASON_ALIASES,
    }
    for field, aliases in groups.items():
        for alias in aliases:
            if alias in norms:
                mapping[field] = norms[alias]
                break
    # If title and russian_title both point at "название", keep both
    if "title" in mapping and "russian_title" not in mapping:
        # when primary title is already Cyrillic column, also expose as russian_title
        if _looks_russian(str(mapping["title"])) or _norm_col(mapping["title"]) == "название":
            mapping["russian_title"] = mapping["title"]
    return mapping


def _detect_media_type(raw: Optional[str], title: str, season: Optional[int]) -> MediaType:
    if season is not None:
        return MediaType.SEASON
    t = (raw or "").strip().lower()
    if t in {"film", "movie", "фильм", "кино", "cartoon", "3d_film", "3d_cartoon", "concert", "documentary", "3d_documentary"}:
        return MediaType.FILM
    if t in {"series", "tv", "show", "сериал", "animated_series", "tv_program"}:
        return MediaType.SERIES
    if t in {"season", "сезон"}:
        return MediaType.SEASON
    tl = title.lower()
    if re.search(r"\b(s\d{1,2}|season\s*\d|сезон\s*\d)\b", tl):
        return MediaType.SEASON
    if "сериал" in tl:
        return MediaType.SERIES
    return MediaType.FILM


def _stamp_source(
    items: list[InputTitle],
    source_file: str | None,
    *,
    source_sheet: str | None = None,
) -> list[InputTitle]:
    """Attach import file/sheet and freeze import_title / import_year from raw input."""
    name = Path(source_file).name if source_file else None
    for it in items:
        if source_file and not it.source_file:
            it.source_file = name
        if source_sheet and not it.source_sheet:
            it.source_sheet = source_sheet
        if it.import_title is None:
            it.import_title = it.title
        if it.import_year is None:
            it.import_year = it.year
    return items


def load_titles(
    path: str | Path | None = None,
    *,
    titles: Optional[Iterable[str]] = None,
    limit: Optional[int] = None,
    sheet: Optional[str] = None,
) -> list[InputTitle]:
    if titles is not None:
        items = [_from_plain_title(t, i) for i, t in enumerate(titles) if str(t).strip()]
        items = _stamp_source(items, "cli", source_sheet="cli")
        return items[:limit] if limit else items

    if path is None:
        raise ValueError("Either path or titles must be provided")

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)

    if path.suffix.lower() in {".xlsx", ".xls"}:
        items = _load_excel(path, limit=limit, sheet=sheet)
        return _stamp_source(items, str(path))
    if path.suffix.lower() == ".csv":
        df = pd.read_csv(path)
        items = _from_dataframe(
            df,
            limit=limit,
            source_file=str(path),
            source_sheet="csv",
            row_offset=2,
        )
        return _stamp_source(items, str(path), source_sheet="csv")
    raise ValueError(f"Unsupported input format: {path.suffix}")


def _from_plain_title(title: str, idx: int = 0) -> InputTitle:
    raw_title = str(title).strip()
    title = normalize_title(title)
    base, season = parse_season_from_title(title)
    # year in parentheses or trailing year
    year = None
    m = re.search(r"\((\d{4})\)", base)
    if m:
        year = int(m.group(1))
        base = re.sub(r"\s*\(\d{4}\)\s*", " ", base).strip()
    else:
        m = re.search(r"\b((?:19|20)\d{2})\b\s*$", base)
        if m:
            year = int(m.group(1))
            base = base[: m.start()].strip(" -–—")
    media = _detect_media_type(None, title, season)
    return InputTitle(
        title=base or title,
        year=year,
        media_type=media,
        season=season,
        source_file="cli",
        source_sheet="cli",
        source_row=idx + 1,  # 1-based position among CLI titles
        import_title=raw_title,
        import_year=year,
    )


def _load_excel(path: Path, limit: Optional[int], sheet: Optional[str]) -> list[InputTitle]:
    # Probe first sheet(s)
    xl = pd.ExcelFile(path)
    sheet_name = sheet or xl.sheet_names[0]
    # Read without header first for messy multi-section files
    raw = pd.read_excel(path, sheet_name=sheet_name, header=None)

    # Heuristic: structured v1-like if row0 has many named columns
    header_row = _find_header_row(raw)
    if header_row is not None:
        df = pd.read_excel(path, sheet_name=sheet_name, header=header_row)
        df = df.dropna(how="all")
        # Excel 1-based: header is row header_row+1, first data row is header_row+2
        items = _from_dataframe(
            df,
            limit=limit,
            source_file=str(path),
            source_sheet=str(sheet_name),
            row_offset=header_row + 2,
        )
        if items:
            return items

    # Fallback: multi-section Russian collection format
    return _from_multisection(
        raw,
        limit=limit,
        source_file=str(path),
        source_sheet=str(sheet_name),
    )


def _find_header_row(raw: pd.DataFrame, max_scan: int = 30) -> Optional[int]:
    for i in range(min(max_scan, len(raw))):
        vals = [str(v).strip().lower() for v in raw.iloc[i].tolist() if pd.notna(v)]
        joined = " ".join(vals)
        score = 0
        for token in ("название", "title", "год", "year", "жанр", "genre", "imdb", "рейтинг", "английское"):
            if token in joined:
                score += 1
        if score >= 2:
            return i
    return None


def _from_dataframe(
    df: pd.DataFrame,
    limit: Optional[int] = None,
    *,
    source_file: str | None = None,
    source_sheet: str | None = None,
    row_offset: int = 2,
) -> list[InputTitle]:
    # Drop fully empty columns
    df = df.loc[:, ~df.columns.astype(str).str.match(r"^Unnamed")]
    colmap = _map_columns([str(c) for c in df.columns])
    if "title" not in colmap and "english_title" not in colmap and "russian_title" not in colmap:
        # try first object column as title
        for c in df.columns:
            if df[c].dtype == object:
                colmap["title"] = c
                break
    items: list[InputTitle] = []
    file_name = Path(source_file).name if source_file else None
    # sequential position among data rows for stable import_row when index is not integer
    data_pos = 0
    for idx, row in df.iterrows():
        title = None
        for key in ("title", "russian_title", "english_title"):
            if key in colmap and pd.notna(row.get(colmap[key])):
                title = str(row.get(colmap[key])).strip()
                break
        if not title or title.lower() in {"nan", "none", "название", "title"}:
            # section headers often only in title col
            continue
        # skip category-like rows (no year and very long category text without film pattern)
        year = safe_int(row.get(colmap["year"])) if "year" in colmap else None
        en = str(row[colmap["english_title"]]).strip() if "english_title" in colmap and pd.notna(row.get(colmap["english_title"])) else None
        ru = str(row[colmap["russian_title"]]).strip() if "russian_title" in colmap and pd.notna(row.get(colmap["russian_title"])) else None
        # If title looks like a section header
        if year is None and en is None and len(title) > 60:
            continue
        if year is None and re.search(r"фильмы|сборник|коллекция|антолог", title, re.I):
            continue

        base_title, season = parse_season_from_title(title)
        if "season" in colmap and season is None:
            season = safe_int(row.get(colmap["season"]))
        media_raw = str(row.get(colmap["type"])) if "type" in colmap and pd.notna(row.get(colmap["type"])) else None
        media = _detect_media_type(media_raw, title, season)

        genre = str(row.get(colmap["genre"])).strip() if "genre" in colmap and pd.notna(row.get(colmap["genre"])) else None
        director = str(row.get(colmap["director"])).strip() if "director" in colmap and pd.notna(row.get(colmap["director"])) else None
        notes = str(row.get(colmap["notes"])).strip() if "notes" in colmap and pd.notna(row.get(colmap["notes"])) else None
        lang = str(row.get(colmap["language"])).strip() if "language" in colmap and pd.notna(row.get(colmap["language"])) else None
        country = str(row.get(colmap["country"])).strip() if "country" in colmap and pd.notna(row.get(colmap["country"])) else None
        imdb = safe_float(row.get(colmap["imdb"])) if "imdb" in colmap else None
        kp = safe_float(row.get(colmap["kinopoisk"])) if "kinopoisk" in colmap else None
        rating = safe_float(row.get(colmap["rating"])) if "rating" in colmap else None
        if rating is None:
            rating = imdb or kp

        # Prefer russian title field if generic title is Russian and en exists
        russian_title = ru or (title if _looks_russian(title) else None)
        english_title = en or (title if not _looks_russian(title) else None)

        input_id = None
        if "input_id" in colmap and pd.notna(row.get(colmap["input_id"])):
            input_id = str(row.get(colmap["input_id"])).strip()
        imdb_id_hint = None
        if "imdb_id" in colmap and pd.notna(row.get(colmap["imdb_id"])):
            imdb_id_hint = str(row.get(colmap["imdb_id"])).strip()
        tmdb_id_hint = safe_int(row.get(colmap["tmdb_id"])) if "tmdb_id" in colmap else None
        kp_id_hint = safe_int(row.get(colmap["kinopoisk_id"])) if "kinopoisk_id" in colmap else None
        actors_hint = None
        if "actors" in colmap and pd.notna(row.get(colmap["actors"])):
            actors_hint = str(row.get(colmap["actors"])).strip()

        # 1-based Excel row: header_row offset + position in dataframe
        if isinstance(idx, (int, float)) and not isinstance(idx, bool):
            # When df uses default RangeIndex after header, idx 0 => first data row
            import_row = int(idx) + row_offset
        else:
            import_row = data_pos + row_offset
        data_pos += 1

        items.append(
            InputTitle(
                title=normalize_title(base_title or title),
                year=year,
                media_type=media,
                season=season,
                original_language=lang,
                existing_rating=rating,
                notes=notes,
                english_title=english_title,
                russian_title=russian_title,
                genre_hint=genre,
                director_hint=director,
                imdb_rating_hint=imdb,
                kinopoisk_rating_hint=kp,
                country=country,
                input_id=input_id,
                imdb_id_hint=imdb_id_hint,
                tmdb_id_hint=tmdb_id_hint,
                kinopoisk_id_hint=kp_id_hint,
                actors_hint=actors_hint,
                source_file=file_name,
                source_sheet=source_sheet,
                source_row=import_row,
                import_title=title,  # as written in the source column of the file
                import_year=year,
            )
        )
        if limit and len(items) >= limit:
            break
    return items


def _from_multisection(
    raw: pd.DataFrame,
    limit: Optional[int] = None,
    *,
    source_file: str | None = None,
    source_sheet: str | None = None,
) -> list[InputTitle]:
    """Parse files like 'Список фильмов.xlsx' with section headers and НАЗВАНИЕ/ЖАНР/ГОД/РЕЙТИНГ blocks."""
    items: list[InputTitle] = []
    collection = None
    mode = "grid"  # grid vs freeform single-column
    file_name = Path(source_file).name if source_file else None
    for i in range(len(raw)):
        row = raw.iloc[i].tolist()
        cells = [c if pd.notna(c) else None for c in row]
        c0 = cells[0] if cells else None
        c1 = cells[1] if len(cells) > 1 else None
        c2 = cells[2] if len(cells) > 2 else None
        c3 = cells[3] if len(cells) > 3 else None

        if c0 is None:
            continue
        s0 = str(c0).strip()
        if not s0:
            continue

        # Header row
        if s0.upper() in {"НАЗВАНИЕ", "TITLE", "NAME"}:
            mode = "grid"
            continue

        # Section title: first cell set, others empty, and not a film with year in text
        if c1 is None and c2 is None and c3 is None:
            # freeform entries like "12 стульев (1971,комедия)" under Soviet section
            m = re.match(r"^(?P<title>.+?)\s*\((?P<meta>[^)]+)\)\s*$", s0)
            if m and collection:
                meta = m.group("meta")
                year = None
                ym = re.search(r"(19|20)\d{2}", meta)
                if ym:
                    year = int(ym.group(0))
                genre = None
                parts = [p.strip() for p in meta.split(",")]
                genres = [p for p in parts if not re.fullmatch(r"(19|20)\d{2}", p)]
                if genres:
                    genre = ", ".join(genres)
                base, season = parse_season_from_title(m.group("title").strip())
                raw_title = m.group("title").strip()
                items.append(
                    InputTitle(
                        title=normalize_title(base),
                        year=year,
                        media_type=_detect_media_type(None, base, season),
                        season=season,
                        genre_hint=genre,
                        russian_title=normalize_title(base) if _looks_russian(base) else None,
                        collection=collection,
                        source_file=file_name,
                        source_sheet=source_sheet,
                        source_row=i + 1,  # 1-based spreadsheet row
                        import_title=raw_title,
                        import_year=year,
                    )
                )
                if limit and len(items) >= limit:
                    return items
                continue
            # otherwise treat as collection header
            collection = s0
            mode = "freeform"
            continue

        # Grid film row
        if mode == "grid" or (c1 is not None or c2 is not None):
            title = s0
            if title.upper() in {"НАЗВАНИЕ", "TITLE"}:
                continue
            genre = str(c1).strip() if c1 is not None else None
            year = safe_int(c2)
            # Some freeform rows put year in c1
            if year is None and c1 is not None and safe_int(c1) and safe_int(c1) > 1900:
                year = safe_int(c1)
                genre = None
            rating = safe_float(c3) if c3 is not None else safe_float(c2) if year and c2 is not None and safe_int(c2) != year else None
            # if c2 was rating not year
            if year is not None and year < 100:
                rating = float(year)
                year = None

            base, season = parse_season_from_title(title)
            items.append(
                InputTitle(
                    title=normalize_title(base),
                    year=year,
                    media_type=_detect_media_type(None, title, season),
                    season=season,
                    existing_rating=rating,
                    genre_hint=genre,
                    russian_title=normalize_title(base) if _looks_russian(base) else None,
                    english_title=normalize_title(base) if not _looks_russian(base) else None,
                    collection=collection,
                    source_file=file_name,
                    source_sheet=source_sheet,
                    source_row=i + 1,  # 1-based spreadsheet row
                    import_title=title,
                    import_year=year,
                )
            )
            if limit and len(items) >= limit:
                return items
    return items


def _looks_russian(text: str) -> bool:
    if not text:
        return False
    cyr = len(re.findall(r"[А-Яа-яЁё]", text))
    lat = len(re.findall(r"[A-Za-z]", text))
    return cyr > lat

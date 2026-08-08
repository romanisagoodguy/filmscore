"""Request-level debug log for 100% reproducible HTTP diagnostics.

Outputs (same stem as the text log path):
  - <stem>.txt              human-readable text log
  - <stem>_requests.csv     one row per HTTP call (live append)
  - <stem>_films.csv        one row per film (live append)
  - <stem>_sites.csv        per-site totals (rewritten periodically)
  - <stem>.xlsx             sheets: requests, films, sites, summary
"""

from __future__ import annotations

import csv
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode, urlsplit


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + "Z"


def _site_bucket(host: str) -> str:
    host = (host or "").lower()
    if "wikipedia.org" in host:
        return "wikipedia"
    if "kinopoiskapiunofficial.tech" in host or "kinopoisk" in host:
        return "kinopoisk"
    if "themoviedb.org" in host or "tmdb" in host:
        return "tmdb"
    if "omdbapi.com" in host:
        return "omdb"
    if "letterboxd.com" in host:
        return "letterboxd"
    return host or "unknown"


def _py_lit(obj: Any) -> str:
    """Python literal safe for embedding in: python -c \"...\" on PowerShell."""
    if obj is None:
        return "None"
    if isinstance(obj, bool):
        return "True" if obj else "False"
    if isinstance(obj, (int, float)) and not isinstance(obj, bool):
        return repr(obj)
    if isinstance(obj, str):
        if "'" not in obj:
            return "'" + obj + "'"
        if '"' not in obj:
            return '"' + obj + '"'
        return repr(obj)
    if isinstance(obj, dict):
        inner = ", ".join(f"{_py_lit(k)}: {_py_lit(v)}" for k, v in obj.items())
        return "{" + inner + "}"
    if isinstance(obj, (list, tuple)):
        inner = ", ".join(_py_lit(v) for v in obj)
        return "[" + inner + "]"
    return repr(obj)


REQUEST_CSV_FIELDS = [
    "request_n",
    "time_utc",
    "film_seq",
    "title",
    "year",
    "english_title",
    "import_title",
    "excel_sheet",
    "excel_row",
    "live_excel_row",
    "catalog_index",
    "resume_key",
    "site",
    "host",
    "method",
    "url",
    "full_url",
    "params",
    "headers_sent",
    "status_code",
    "ok",
    "error",
    "attempt",
    "max_attempts",
    "duration_ms",
    "throttle_wait_ms",
    "cumulative_query_ms",
    "cumulative_wall_ms",
    "response_body_len",
    "response_preview",
    "reproducible_command",
]

FILM_CSV_FIELDS = [
    "film_seq",
    "title",
    "year",
    "english_title",
    "russian_title",
    "import_title",
    "import_year",
    "media_type",
    "source_file",
    "excel_sheet",
    "excel_row",
    "live_excel_sheet",
    "live_excel_row",
    "catalog_index",
    "resume_key",
    "gather_checkpoint_line",
    "imdb_id_hint",
    "tmdb_id_hint",
    "kinopoisk_id_hint",
    "http_requests",
    "cache_hits",
    "film_query_ms",
    "film_wall_ms",
    "started_utc",
    "ended_utc",
    "profile_error",
    "note",
    "src_tmdb",
    "src_omdb",
    "src_kinopoisk",
    "src_wikipedia",
    "src_letterboxd",
    "n_tmdb",
    "ok_tmdb",
    "fail_tmdb",
    "query_ms_tmdb",
    "n_omdb",
    "ok_omdb",
    "fail_omdb",
    "query_ms_omdb",
    "n_kinopoisk",
    "ok_kinopoisk",
    "fail_kinopoisk",
    "query_ms_kinopoisk",
    "n_wikipedia",
    "ok_wikipedia",
    "fail_wikipedia",
    "query_ms_wikipedia",
    "n_letterboxd",
    "ok_letterboxd",
    "fail_letterboxd",
    "query_ms_letterboxd",
    "statuses_by_site",
]

SITE_CSV_FIELDS = [
    "site",
    "commands",
    "ok",
    "fail",
    "pct_of_all",
    "query_ms_total",
    "query_sec_total",
    "first_time_utc",
    "last_time_utc",
    "status_breakdown",
]


@dataclass
class FilmContext:
    film_seq: int  # 1-based within this debug run
    catalog_index: Optional[int]  # 1-based position among input titles (if known)
    excel_sheet: Optional[str]
    excel_row: Optional[int]  # 1-based row in source Excel
    live_excel_sheet: str = "profiles"
    live_excel_row: Optional[int] = None  # 1-based data row in gather_live.xlsx
    title: str = ""
    year: Optional[int] = None
    english_title: Optional[str] = None
    russian_title: Optional[str] = None
    import_title: Optional[str] = None
    import_year: Optional[int] = None
    media_type: Optional[str] = None
    source_file: Optional[str] = None
    resume_key: Optional[str] = None
    imdb_id_hint: Optional[str] = None
    tmdb_id_hint: Optional[int] = None
    kinopoisk_id_hint: Optional[int] = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class SiteStats:
    requests: int = 0
    ok: int = 0
    fail: int = 0
    status_counts: dict[str, int] = field(default_factory=dict)
    duration_ms_total: float = 0.0
    first_time_utc: Optional[str] = None
    last_time_utc: Optional[str] = None

    def record(self, *, ok: bool, status_key: str, duration_ms: float, time_utc: str) -> None:
        self.requests += 1
        if ok:
            self.ok += 1
        else:
            self.fail += 1
        self.status_counts[status_key] = self.status_counts.get(status_key, 0) + 1
        self.duration_ms_total += duration_ms
        if not self.first_time_utc:
            self.first_time_utc = time_utc
        self.last_time_utc = time_utc


class RequestDebugLog:
    """Thread-safe text + table (CSV/Excel) request debug writer."""

    def __init__(
        self,
        path: str | Path,
        *,
        user_agent: str = "",
        delay_sec: float = 0.0,
        timeout_sec: float = 0.0,
        max_retries: int = 0,
        include_secrets: bool = True,
        meta: Optional[dict[str, Any]] = None,
        write_tables: bool = True,
        excel_every_films: int = 25,
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Normalize stem: if user passed request_debug.txt → tables beside it
        if self.path.suffix.lower() != ".txt":
            self.path = self.path.with_suffix(".txt")
        self.stem = self.path.with_suffix("")  # path without .txt
        self.requests_csv = Path(str(self.stem) + "_requests.csv")
        self.films_csv = Path(str(self.stem) + "_films.csv")
        self.sites_csv = Path(str(self.stem) + "_sites.csv")
        self.excel_path = Path(str(self.stem) + ".xlsx")

        self.user_agent = user_agent
        self.delay_sec = delay_sec
        self.timeout_sec = timeout_sec
        self.max_retries = max_retries
        self.include_secrets = include_secrets
        self.meta = meta or {}
        self.write_tables = write_tables
        self.excel_every_films = max(1, int(excel_every_films))

        self._lock = threading.Lock()
        self._t0 = time.monotonic()
        self._cum_query_ms = 0.0
        self._req_n = 0
        self._films_done = 0
        self._global_sites: dict[str, SiteStats] = defaultdict(SiteStats)
        self._film: Optional[FilmContext] = None
        self._film_sites: dict[str, SiteStats] = defaultdict(SiteStats)
        self._film_t0: Optional[float] = None
        self._film_query_ms = 0.0
        self._film_req_n = 0
        self._film_cache_hits = 0
        self._film_started_utc: Optional[str] = None
        self._closed = False

        self._write_header()
        if self.write_tables:
            self._init_csv(self.requests_csv, REQUEST_CSV_FIELDS)
            self._init_csv(self.films_csv, FILM_CSV_FIELDS)
            self._write_sites_csv_unlocked()

    # ------------------------------------------------------------------ paths
    def output_paths(self) -> dict[str, Path]:
        out = {"text": self.path}
        if self.write_tables:
            out.update(
                {
                    "requests_csv": self.requests_csv,
                    "films_csv": self.films_csv,
                    "sites_csv": self.sites_csv,
                    "excel": self.excel_path,
                }
            )
        return out

    @staticmethod
    def _init_csv(path: Path, fields: list[str]) -> None:
        # Fresh file each run (debug run is self-contained)
        with path.open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            w.writeheader()

    def _append_csv_row(self, path: Path, fields: list[str], row: dict[str, Any]) -> None:
        with path.open("a", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            w.writerow({k: row.get(k, "") for k in fields})

    # ------------------------------------------------------------------ open/close
    def _write_header(self) -> None:
        lines = [
            "=" * 88,
            "PSYCHOFILM REQUEST DEBUG LOG",
            "Purpose: every network call as a copy-paste command + status + timing",
            "so errors can be reproduced 100% outside the pipeline.",
            "=" * 88,
            f"started_utc:     {_utc_now()}",
            f"log_path:        {self.path.resolve()}",
            f"user_agent:      {self.user_agent}",
            f"delay_sec:       {self.delay_sec}",
            f"timeout_sec:     {self.timeout_sec}",
            f"max_retries:     {self.max_retries}",
            f"include_secrets: {self.include_secrets}  "
            "(API keys written into commands when True — treat file as secret)",
            f"tables:          {self.write_tables}",
        ]
        if self.write_tables:
            lines += [
                f"requests_csv:    {self.requests_csv.resolve()}",
                f"films_csv:       {self.films_csv.resolve()}",
                f"sites_csv:       {self.sites_csv.resolve()}",
                f"excel:           {self.excel_path.resolve()}",
            ]
        for k, v in self.meta.items():
            lines.append(f"{k}: {v}")
        lines += [
            "",
            "HOW TO REPRODUCE A SINGLE REQUEST",
            "  1) Open the text log OR the requests sheet/CSV",
            "  2) Copy `reproducible_command` / `reproducible_command_powershell:`",
            "  3) Paste into PowerShell (same machine / network / DNS)",
            "  4) Compare status_code / body prefix / exception text",
            "",
            "CELL / ROW MAP",
            "  excel_sheet + excel_row  = cell in the INPUT catalog workbook",
            "  live_excel_row          = row in output/gather_live.xlsx sheet 'profiles'",
            "                            (row 1 = header; first film = row 2)",
            "  gather_checkpoint_line  = 1-based line number in gather_checkpoint.jsonl",
            "                            after this film is appended",
            "",
            "TABLES",
            "  requests  = one row per HTTP call (command + status + duration)",
            "  films     = one row per film (cells + per-site counts + times)",
            "  sites     = aggregate by site for the whole run",
            "  summary   = run totals (Excel only)",
            "",
            "=" * 88,
            "",
        ]
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._film is not None:
                self._end_film_unlocked(result_note="(log closed mid-film)")
            self._write_global_stats_unlocked()
            if self.write_tables:
                self._write_sites_csv_unlocked()
                self._write_excel_unlocked()
            self._closed = True

    def __enter__(self) -> "RequestDebugLog":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ film
    def begin_film(self, ctx: FilmContext) -> None:
        with self._lock:
            if self._film is not None:
                self._end_film_unlocked(result_note="(previous film closed implicitly)")
            self._film = ctx
            self._film_sites = defaultdict(SiteStats)
            self._film_t0 = time.monotonic()
            self._film_query_ms = 0.0
            self._film_req_n = 0
            self._film_cache_hits = 0
            self._film_started_utc = _utc_now()
            f = ctx
            block = [
                "",
                "#" * 88,
                f"FILM #{f.film_seq}",
                "#" * 88,
                "FILM SPECIFICATION",
                f"  title:              {f.title!r}",
                f"  year:               {f.year}",
                f"  english_title:      {f.english_title!r}",
                f"  russian_title:      {f.russian_title!r}",
                f"  import_title:       {f.import_title!r}",
                f"  import_year:        {f.import_year}",
                f"  media_type:         {f.media_type}",
                f"  imdb_id_hint:       {f.imdb_id_hint}",
                f"  tmdb_id_hint:       {f.tmdb_id_hint}",
                f"  kinopoisk_id_hint:  {f.kinopoisk_id_hint}",
                "CELL / INSERT TARGETS",
                f"  source_file:        {f.source_file}",
                f"  excel_sheet:        {f.excel_sheet}",
                f"  excel_row:          {f.excel_row}   "
                f"(INPUT workbook row; open sheet and go to this row)",
                f"  catalog_index:      {f.catalog_index}   "
                f"(1-based position in loaded input list)",
                f"  live_excel_sheet:   {f.live_excel_sheet}",
                f"  live_excel_row:     {f.live_excel_row}   "
                f"(profiles sheet: header=1, data starts at 2)",
                f"  resume_key:         {f.resume_key}",
                f"film_started_utc:     {self._film_started_utc}",
                "-" * 88,
                "",
            ]
            self._append("\n".join(block))

    def end_film(
        self,
        *,
        found_sources: Optional[dict[str, bool]] = None,
        error: Optional[str] = None,
        gather_checkpoint_line: Optional[int] = None,
        live_excel_row: Optional[int] = None,
        note: Optional[str] = None,
    ) -> None:
        with self._lock:
            if self._film is None:
                return
            if live_excel_row is not None:
                self._film.live_excel_row = live_excel_row
            self._end_film_unlocked(
                found_sources=found_sources,
                error=error,
                gather_checkpoint_line=gather_checkpoint_line,
                note=note,
            )

    def _site_counts(self, site: str) -> tuple[int, int, int, float]:
        st = self._film_sites.get(site)
        if not st:
            return 0, 0, 0, 0.0
        return st.requests, st.ok, st.fail, round(st.duration_ms_total, 1)

    def _end_film_unlocked(
        self,
        *,
        found_sources: Optional[dict[str, bool]] = None,
        error: Optional[str] = None,
        gather_checkpoint_line: Optional[int] = None,
        note: Optional[str] = None,
        result_note: Optional[str] = None,
    ) -> None:
        f = self._film
        if f is None:
            return
        wall_ms = (time.monotonic() - (self._film_t0 or time.monotonic())) * 1000.0
        ended = _utc_now()
        found_sources = found_sources or {}
        note_final = note or result_note

        lines = [
            "",
            f"FILM #{f.film_seq} SUMMARY",
            f"  title:                   {f.title!r} ({f.year})",
            f"  excel:                   sheet={f.excel_sheet!r} row={f.excel_row}",
            f"  live_excel:              sheet={f.live_excel_sheet!r} row={f.live_excel_row}",
            f"  gather_checkpoint_line:  {gather_checkpoint_line}",
            f"  http_requests:           {self._film_req_n}",
            f"  cache_hits_no_http:      {self._film_cache_hits}",
            f"  film_query_time_ms:      {self._film_query_ms:.1f}  "
            f"(sum of HTTP durations only)",
            f"  film_wall_time_ms:       {wall_ms:.1f}  "
            f"(includes delays/retries/CPU)",
            f"  film_ended_utc:          {ended}",
        ]
        if error:
            lines.append(f"  profile_error:           {error}")
        if note_final:
            lines.append(f"  note:                    {note_final}")
        if found_sources:
            lines.append("  sources_found:")
            for name, ok in sorted(found_sources.items()):
                lines.append(f"    - {name}: {'FOUND' if ok else 'MISS'}")
        lines.append("  per_site_this_film:")
        statuses_by_site: dict[str, Any] = {}
        for site, st in sorted(self._film_sites.items()):
            lines.append(
                f"    - {site}: n={st.requests} ok={st.ok} fail={st.fail} "
                f"query_ms={st.duration_ms_total:.1f} statuses={dict(st.status_counts)}"
            )
            statuses_by_site[site] = dict(st.status_counts)
        lines += ["#" * 88, ""]
        self._append("\n".join(lines))

        if self.write_tables:
            def _found(name: str) -> str:
                if name not in found_sources:
                    return ""
                return "FOUND" if found_sources[name] else "MISS"

            n_t, ok_t, fail_t, q_t = self._site_counts("tmdb")
            n_o, ok_o, fail_o, q_o = self._site_counts("omdb")
            n_k, ok_k, fail_k, q_k = self._site_counts("kinopoisk")
            n_w, ok_w, fail_w, q_w = self._site_counts("wikipedia")
            n_l, ok_l, fail_l, q_l = self._site_counts("letterboxd")

            film_row = {
                "film_seq": f.film_seq,
                "title": f.title,
                "year": f.year,
                "english_title": f.english_title or "",
                "russian_title": f.russian_title or "",
                "import_title": f.import_title or "",
                "import_year": f.import_year if f.import_year is not None else "",
                "media_type": f.media_type or "",
                "source_file": f.source_file or "",
                "excel_sheet": f.excel_sheet or "",
                "excel_row": f.excel_row if f.excel_row is not None else "",
                "live_excel_sheet": f.live_excel_sheet,
                "live_excel_row": f.live_excel_row if f.live_excel_row is not None else "",
                "catalog_index": f.catalog_index if f.catalog_index is not None else "",
                "resume_key": f.resume_key or "",
                "gather_checkpoint_line": (
                    gather_checkpoint_line if gather_checkpoint_line is not None else ""
                ),
                "imdb_id_hint": f.imdb_id_hint or "",
                "tmdb_id_hint": f.tmdb_id_hint if f.tmdb_id_hint is not None else "",
                "kinopoisk_id_hint": (
                    f.kinopoisk_id_hint if f.kinopoisk_id_hint is not None else ""
                ),
                "http_requests": self._film_req_n,
                "cache_hits": self._film_cache_hits,
                "film_query_ms": round(self._film_query_ms, 1),
                "film_wall_ms": round(wall_ms, 1),
                "started_utc": self._film_started_utc or "",
                "ended_utc": ended,
                "profile_error": error or "",
                "note": note_final or "",
                "src_tmdb": _found("tmdb"),
                "src_omdb": _found("omdb"),
                "src_kinopoisk": _found("kinopoisk"),
                "src_wikipedia": _found("wikipedia"),
                "src_letterboxd": _found("letterboxd"),
                "n_tmdb": n_t,
                "ok_tmdb": ok_t,
                "fail_tmdb": fail_t,
                "query_ms_tmdb": q_t,
                "n_omdb": n_o,
                "ok_omdb": ok_o,
                "fail_omdb": fail_o,
                "query_ms_omdb": q_o,
                "n_kinopoisk": n_k,
                "ok_kinopoisk": ok_k,
                "fail_kinopoisk": fail_k,
                "query_ms_kinopoisk": q_k,
                "n_wikipedia": n_w,
                "ok_wikipedia": ok_w,
                "fail_wikipedia": fail_w,
                "query_ms_wikipedia": q_w,
                "n_letterboxd": n_l,
                "ok_letterboxd": ok_l,
                "fail_letterboxd": fail_l,
                "query_ms_letterboxd": q_l,
                "statuses_by_site": repr(statuses_by_site),
            }
            self._append_csv_row(self.films_csv, FILM_CSV_FIELDS, film_row)
            self._films_done += 1
            # Live sites table; Excel less often (heavy for large runs)
            self._write_sites_csv_unlocked()
            if self._films_done == 1 or (self._films_done % self.excel_every_films == 0):
                self._write_excel_unlocked()

        self._film = None

    def log_cache_hit(self, source_name: str, cache_key: str) -> None:
        with self._lock:
            self._film_cache_hits += 1
            film_label = f"FILM #{self._film.film_seq}" if self._film else "NO_FILM"
            block = [
                f"--- CACHE HIT ({film_label}) ---",
                f"time_utc:     {_utc_now()}",
                f"source:       {source_name}",
                f"cache_key:    {cache_key}",
                "note:         No HTTP was executed for this source (disk cache).",
                "              To force network: disable cache or delete cache entry.",
                "",
            ]
            self._append("\n".join(block))

    def log_source_event(self, source_name: str, message: str) -> None:
        with self._lock:
            film_label = f"FILM #{self._film.film_seq}" if self._film else "NO_FILM"
            self._append(
                f"[source-event] {film_label} source={source_name} {_utc_now()} {message}\n"
            )

    # ------------------------------------------------------------------ HTTP
    def log_http(
        self,
        *,
        method: str,
        url: str,
        params: Optional[dict[str, Any]] = None,
        headers: Optional[dict[str, str]] = None,
        session_headers: Optional[dict[str, str]] = None,
        status_code: Optional[int] = None,
        ok: bool = False,
        error: Optional[str] = None,
        duration_ms: float = 0.0,
        throttle_wait_ms: float = 0.0,
        attempt: int = 1,
        max_attempts: int = 1,
        response_preview: str = "",
        body_len: Optional[int] = None,
    ) -> None:
        with self._lock:
            self._req_n += 1
            self._film_req_n += 1
            self._cum_query_ms += duration_ms
            self._film_query_ms += duration_ms
            wall_ms = (time.monotonic() - self._t0) * 1000.0
            host = urlsplit(url).netloc
            site = _site_bucket(host)
            time_utc = _utc_now()
            status_key = (
                str(status_code) if status_code is not None else f"ERR:{error or 'unknown'}"
            )
            if len(status_key) > 80:
                status_key = status_key[:80]

            for bucket in (self._global_sites, self._film_sites):
                bucket[site].record(
                    ok=ok, status_key=status_key, duration_ms=duration_ms, time_utc=time_utc
                )

            effective: dict[str, str] = {}
            if session_headers:
                effective.update({str(k): str(v) for k, v in session_headers.items()})
            if headers:
                effective.update({str(k): str(v) for k, v in headers.items()})

            full_url = url
            if params:
                qs = urlencode(
                    [(str(k), "" if v is None else str(v)) for k, v in params.items()],
                    doseq=True,
                )
                sep = "&" if ("?" in url) else "?"
                full_url = f"{url}{sep}{qs}" if qs else url

            cmd = self._build_python_command(
                method=method,
                url=url,
                params=params,
                headers=effective,
                timeout=self.timeout_sec or 20.0,
            )
            ps_cmd = self._build_powershell_oneliner(cmd)

            f = self._film
            film_label = f"FILM #{f.film_seq}" if f else "NO_FILM_CONTEXT"
            excel_info = (
                f"sheet={f.excel_sheet!r} excel_row={f.excel_row} live_row={f.live_excel_row}"
                if f
                else "n/a"
            )

            block = [
                f"--- REQUEST {self._req_n:04d} ({film_label}) ---",
                f"time_utc:              {time_utc}",
                f"site:                  {site}",
                f"host:                  {host}",
                f"method:                {method}",
                f"url:                   {url}",
                f"params:                {self._fmt_params(params)}",
                f"full_url:              {full_url}",
                f"headers_sent:          {self._fmt_headers(effective)}",
                f"film_title:            {repr(f.title) if f else None}",
                f"film_year:             {f.year if f else None}",
                f"cell:                  {excel_info}",
                f"attempt:               {attempt}/{max_attempts}",
                f"throttle_wait_ms:      {throttle_wait_ms:.1f}",
                f"duration_ms:           {duration_ms:.1f}",
                f"cumulative_query_ms:   {self._cum_query_ms:.1f}  "
                f"(sum of all HTTP durations so far)",
                f"cumulative_wall_ms:    {wall_ms:.1f}  "
                f"(wall clock since debug log start)",
                f"status_code:           {status_code if status_code is not None else 'NONE'}",
                f"ok:                    {ok}",
                f"error:                 {error or ''}",
                f"response_body_len:     {body_len if body_len is not None else ''}",
                f"response_preview:      {response_preview[:500]!r}",
                "reproducible_command_python:",
                f"  {cmd}",
                "reproducible_command_powershell:",
                f"  {ps_cmd}",
                "",
            ]
            self._append("\n".join(block))

            if self.write_tables:
                req_row = {
                    "request_n": self._req_n,
                    "time_utc": time_utc,
                    "film_seq": f.film_seq if f else "",
                    "title": f.title if f else "",
                    "year": f.year if f else "",
                    "english_title": (f.english_title or "") if f else "",
                    "import_title": (f.import_title or "") if f else "",
                    "excel_sheet": (f.excel_sheet or "") if f else "",
                    "excel_row": f.excel_row if f and f.excel_row is not None else "",
                    "live_excel_row": (
                        f.live_excel_row if f and f.live_excel_row is not None else ""
                    ),
                    "catalog_index": (
                        f.catalog_index if f and f.catalog_index is not None else ""
                    ),
                    "resume_key": (f.resume_key or "") if f else "",
                    "site": site,
                    "host": host,
                    "method": method,
                    "url": url,
                    "full_url": full_url,
                    "params": self._fmt_params(params),
                    "headers_sent": self._fmt_headers(effective),
                    "status_code": status_code if status_code is not None else "",
                    "ok": ok,
                    "error": error or "",
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "duration_ms": round(duration_ms, 1),
                    "throttle_wait_ms": round(throttle_wait_ms, 1),
                    "cumulative_query_ms": round(self._cum_query_ms, 1),
                    "cumulative_wall_ms": round(wall_ms, 1),
                    "response_body_len": body_len if body_len is not None else "",
                    "response_preview": (response_preview or "")[:500],
                    "reproducible_command": cmd,
                }
                self._append_csv_row(self.requests_csv, REQUEST_CSV_FIELDS, req_row)

    def _fmt_params(self, params: Optional[dict[str, Any]]) -> str:
        if not params:
            return "{}"
        if self.include_secrets:
            return repr(dict(params))
        redacted = {}
        for k, v in params.items():
            lk = str(k).lower()
            if lk in {"api_key", "apikey", "api-key", "token", "password", "x-api-key"}:
                redacted[k] = "***"
            else:
                redacted[k] = v
        return repr(redacted)

    def _fmt_headers(self, headers: dict[str, str]) -> str:
        if not headers:
            return "{}"
        parts = []
        for k, v in headers.items():
            lk = k.lower()
            if not self.include_secrets and lk in {"x-api-key", "authorization"}:
                parts.append(f"{k}=***")
            else:
                parts.append(f"{k}={v}")
        return "; ".join(parts)

    def _build_python_command(
        self,
        *,
        method: str,
        url: str,
        params: Optional[dict[str, Any]],
        headers: dict[str, str],
        timeout: float,
    ) -> str:
        use_headers = dict(headers)
        use_params = dict(params or {})
        if not self.include_secrets:
            for k in list(use_params):
                if str(k).lower() in {"api_key", "apikey", "api-key", "token", "password"}:
                    use_params[k] = "YOUR_KEY"
            for k in list(use_headers):
                if k.lower() in {"x-api-key", "authorization"}:
                    use_headers[k] = "YOUR_KEY"

        h_lit = _py_lit(use_headers)
        params_arg = f", params={_py_lit(use_params)}" if use_params else ""
        method_u = method.upper()
        if method_u != "GET":
            return (
                f'python -c "import requests; r=requests.request({_py_lit(method_u)}, '
                f'{_py_lit(url)}, headers={h_lit}{params_arg}, timeout={timeout}); '
                f'print(r.status_code); print(r.text[:500])"'
            )
        return (
            f'python -c "import requests; r=requests.get({_py_lit(url)}, '
            f'headers={h_lit}{params_arg}, timeout={timeout}); '
            f'print(r.status_code); print(r.text[:500])"'
        )

    @staticmethod
    def _build_powershell_oneliner(python_cmd: str) -> str:
        return python_cmd

    def _write_sites_csv_unlocked(self) -> None:
        if not self.write_tables:
            return
        total_n = max(self._req_n, 1)
        with self.sites_csv.open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=SITE_CSV_FIELDS, extrasaction="ignore")
            w.writeheader()
            for site, st in sorted(self._global_sites.items(), key=lambda x: -x[1].requests):
                w.writerow(
                    {
                        "site": site,
                        "commands": st.requests,
                        "ok": st.ok,
                        "fail": st.fail,
                        "pct_of_all": round(100.0 * st.requests / total_n, 2),
                        "query_ms_total": round(st.duration_ms_total, 1),
                        "query_sec_total": round(st.duration_ms_total / 1000.0, 3),
                        "first_time_utc": st.first_time_utc or "",
                        "last_time_utc": st.last_time_utc or "",
                        "status_breakdown": repr(dict(st.status_counts)),
                    }
                )

    def _write_excel_unlocked(self) -> None:
        if not self.write_tables:
            return
        try:
            import pandas as pd
        except ImportError:
            self._append(
                "[warn] pandas not available — Excel table skipped; use CSV files\n"
            )
            return
        try:
            wall_ms = (time.monotonic() - self._t0) * 1000.0
            req_df = (
                pd.read_csv(self.requests_csv, encoding="utf-8-sig")
                if self.requests_csv.exists() and self.requests_csv.stat().st_size > 0
                else pd.DataFrame(columns=REQUEST_CSV_FIELDS)
            )
            films_df = (
                pd.read_csv(self.films_csv, encoding="utf-8-sig")
                if self.films_csv.exists() and self.films_csv.stat().st_size > 0
                else pd.DataFrame(columns=FILM_CSV_FIELDS)
            )
            sites_df = (
                pd.read_csv(self.sites_csv, encoding="utf-8-sig")
                if self.sites_csv.exists() and self.sites_csv.stat().st_size > 0
                else pd.DataFrame(columns=SITE_CSV_FIELDS)
            )
            summary_rows = [
                {"metric": "started_utc", "value": self.meta.get("started_utc", "")},
                {"metric": "ended_utc_so_far", "value": _utc_now()},
                {"metric": "total_http_requests", "value": self._req_n},
                {"metric": "total_films", "value": self._films_done},
                {"metric": "cumulative_query_ms", "value": round(self._cum_query_ms, 1)},
                {
                    "metric": "cumulative_query_sec",
                    "value": round(self._cum_query_ms / 1000.0, 3),
                },
                {"metric": "cumulative_wall_ms", "value": round(wall_ms, 1)},
                {"metric": "cumulative_wall_sec", "value": round(wall_ms / 1000.0, 3)},
                {
                    "metric": "avg_query_ms_per_request",
                    "value": round(self._cum_query_ms / self._req_n, 1) if self._req_n else 0,
                },
                {"metric": "user_agent", "value": self.user_agent},
                {"metric": "delay_sec", "value": self.delay_sec},
                {"metric": "timeout_sec", "value": self.timeout_sec},
                {"metric": "max_retries", "value": self.max_retries},
                {"metric": "include_secrets", "value": self.include_secrets},
                {"metric": "text_log", "value": str(self.path.resolve())},
                {"metric": "requests_csv", "value": str(self.requests_csv.resolve())},
                {"metric": "films_csv", "value": str(self.films_csv.resolve())},
                {"metric": "sites_csv", "value": str(self.sites_csv.resolve())},
            ]
            for k, v in self.meta.items():
                summary_rows.append({"metric": str(k), "value": v})
            summary_df = pd.DataFrame(summary_rows)

            # Excel cell limit ~32767 for text; truncate long command/preview columns
            for col in ("reproducible_command", "response_preview", "headers_sent", "params"):
                if col in req_df.columns:
                    req_df[col] = req_df[col].astype(str).str.slice(0, 32000)

            with pd.ExcelWriter(self.excel_path, engine="openpyxl") as writer:
                summary_df.to_excel(writer, sheet_name="summary", index=False)
                films_df.to_excel(writer, sheet_name="films", index=False)
                sites_df.to_excel(writer, sheet_name="sites", index=False)
                # Cap requests sheet if enormous (Excel ~1M rows; keep practical)
                max_req_rows = 200_000
                if len(req_df) > max_req_rows:
                    req_df.tail(max_req_rows).to_excel(
                        writer, sheet_name="requests", index=False
                    )
                else:
                    req_df.to_excel(writer, sheet_name="requests", index=False)
        except Exception as exc:  # noqa: BLE001
            self._append(f"[warn] Excel table write failed: {exc}\n")

    def _write_global_stats_unlocked(self) -> None:
        wall_ms = (time.monotonic() - self._t0) * 1000.0
        lines = [
            "",
            "=" * 88,
            "GLOBAL STATISTICS",
            "=" * 88,
            f"ended_utc:                 {_utc_now()}",
            f"total_http_requests:       {self._req_n}",
            f"total_films:               {self._films_done}",
            f"cumulative_query_ms:       {self._cum_query_ms:.1f}",
            f"cumulative_query_sec:      {self._cum_query_ms / 1000.0:.3f}",
            f"cumulative_wall_ms:        {wall_ms:.1f}",
            f"cumulative_wall_sec:       {wall_ms / 1000.0:.3f}",
            f"avg_query_ms_per_request:  "
            f"{(self._cum_query_ms / self._req_n) if self._req_n else 0:.1f}",
            "",
            "BY SITE",
        ]
        total_n = max(self._req_n, 1)
        for site, st in sorted(self._global_sites.items(), key=lambda x: -x[1].requests):
            pct = 100.0 * st.requests / total_n
            lines.append(
                f"  [{site}]"
                f"  commands={st.requests} ({pct:.1f}%)"
                f"  ok={st.ok} fail={st.fail}"
                f"  query_ms_total={st.duration_ms_total:.1f}"
                f"  query_sec_total={st.duration_ms_total / 1000.0:.3f}"
                f"  first={st.first_time_utc}"
                f"  last={st.last_time_utc}"
            )
            lines.append(f"    status_breakdown: {dict(st.status_counts)}")
        if self.write_tables:
            lines += [
                "",
                "TABLE FILES",
                f"  text:          {self.path.resolve()}",
                f"  requests_csv:  {self.requests_csv.resolve()}",
                f"  films_csv:     {self.films_csv.resolve()}",
                f"  sites_csv:     {self.sites_csv.resolve()}",
                f"  excel:         {self.excel_path.resolve()}",
            ]
        lines += [
            "",
            "NOTE: query_ms is pure HTTP time; wall includes delay_sec, RPM waits, retries, CPU.",
            "=" * 88,
            "",
        ]
        self._append("\n".join(lines))

    def _append(self, text: str) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(text if text.endswith("\n") else text + "\n")
            fh.flush()

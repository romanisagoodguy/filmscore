"""Main enrichment & scoring pipeline."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from tqdm import tqdm

from psychofilm_analyzer.config import load_config, load_dictionaries
from psychofilm_analyzer.data_sources import (
    KinopoiskSource,
    LetterboxdSource,
    OmdbSource,
    TmdbSource,
    WikipediaSource,
)
from psychofilm_analyzer.enrichment.export import write_profiles
from psychofilm_analyzer.enrichment.profile import EnrichmentProfile, build_enrichment_profile
from psychofilm_analyzer.io.input_loader import load_titles
from psychofilm_analyzer.io.output_writer import write_outputs
from psychofilm_analyzer.models import EnrichedResult, InputTitle, SourcePayload
from psychofilm_analyzer.scoring.engine import ScoringEngine
from psychofilm_analyzer.utils.cache import CacheStore
from psychofilm_analyzer.utils.http import HttpClient

logger = logging.getLogger(__name__)


class Pipeline:
    def __init__(self, config: Optional[dict[str, Any]] = None, dictionaries: Optional[dict] = None):
        self.config = config or load_config()
        self.dictionaries = dictionaries or load_dictionaries()
        cache_cfg = self.config.get("cache") or {}
        http_cfg = self.config.get("http") or {}
        self.cache = CacheStore(
            cache_dir=cache_cfg.get("dir", "cache"),
            ttl_days=int(cache_cfg.get("ttl_days", 30)),
            enabled=bool(cache_cfg.get("enabled", True)),
        )
        from psychofilm_analyzer.utils.wikipedia_auth import (
            wikipedia_auth_status,
            wikipedia_headers,
            wikipedia_user_agent,
        )

        ua = wikipedia_user_agent(self.config) or http_cfg.get(
            "user_agent", "PsychoFilmAnalyzer/1.0"
        )
        self.http = HttpClient(
            delay_sec=float(http_cfg.get("delay_sec", 0.6)),
            timeout_sec=float(http_cfg.get("timeout_sec", 25)),
            max_retries=int(http_cfg.get("max_retries", 3)),
            user_agent=ua,
            host_rate_limits_per_min=http_cfg.get("host_rate_limits_per_min"),
        )
        # Wikimedia OAuth Bearer for *.wikipedia.org (Approach 1 data source)
        self.http.set_wikipedia_auth(wikipedia_headers(self.config))
        st = wikipedia_auth_status(self.config)
        if st.get("access_token_set"):
            import logging as _logging

            _logging.getLogger(__name__).info(
                "Wikipedia OAuth configured (email=%s token=%s client_id=%s)",
                st.get("email"),
                st.get("access_token_prefix"),
                "yes" if st.get("client_id_set") else "no",
            )
        keys = self.config.get("api_keys") or {}
        src_flags = self.config.get("sources") or {}
        self.sources = []
        if src_flags.get("tmdb", True):
            self.sources.append(TmdbSource(self.http, self.cache, self.config, api_key=keys.get("tmdb", "")))
        if src_flags.get("omdb", True):
            self.sources.append(OmdbSource(self.http, self.cache, self.config, api_key=keys.get("omdb", "")))
        if src_flags.get("kinopoisk", True):
            self.sources.append(
                KinopoiskSource(self.http, self.cache, self.config, api_key=keys.get("kinopoisk", ""))
            )
        if src_flags.get("wikipedia", True):
            self.sources.append(WikipediaSource(self.http, self.cache, self.config))
        if src_flags.get("letterboxd", True):
            self.sources.append(LetterboxdSource(self.http, self.cache, self.config))
        self.engine = ScoringEngine(self.config, self.dictionaries)
        self.state_file = Path((self.config.get("pipeline") or {}).get("state_file", "output/pipeline_state.json"))

    def fetch_sources(self, item: InputTitle) -> dict[str, SourcePayload]:
        """Call all enabled data sources; no scoring."""
        payloads: dict[str, SourcePayload] = {}
        for src in self.sources:
            try:
                payload = src.fetch(item)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Source %s failed", src.name)
                payload = SourcePayload(source=src.name, found=False, error=str(exc))
            payloads[src.name] = payload
            if payload.found and payload.imdb_id:
                item.extra["imdb_id"] = payload.imdb_id
            if payload.found and payload.extra and payload.extra.get("name_en") and not item.english_title:
                item.english_title = payload.extra.get("name_en")
        return payloads

    def process_item(self, item: InputTitle) -> EnrichedResult:
        payloads = self.fetch_sources(item)
        return self.engine.score(item, payloads)

    def gather_item(self, item: InputTitle) -> EnrichmentProfile:
        """Phase A: multi-source profile only (no psych scores / clusters)."""
        try:
            payloads = self.fetch_sources(item)
            return build_enrichment_profile(item, payloads)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Gather failed for %s", item.title)
            return EnrichmentProfile(input=item, error=str(exc))

    def attach_request_debug(
        self,
        path: str | Path,
        *,
        include_secrets: bool = True,
        write_tables: bool = True,
        excel_every_films: int = 25,
        meta: Optional[dict[str, Any]] = None,
    ) -> Any:
        """Enable full request reproduction log (text + CSV/Excel tables)."""
        from psychofilm_analyzer.utils.request_debug import RequestDebugLog

        http_cfg = self.config.get("http") or {}
        pipe_cfg = self.config.get("pipeline") or {}
        dbg = RequestDebugLog(
            path,
            user_agent=http_cfg.get("user_agent", "PsychoFilmAnalyzer/1.0"),
            delay_sec=float(http_cfg.get("delay_sec", 0.6)),
            timeout_sec=float(http_cfg.get("timeout_sec", 25)),
            max_retries=int(http_cfg.get("max_retries", 3)),
            include_secrets=include_secrets,
            write_tables=write_tables,
            excel_every_films=int(
                excel_every_films
                if excel_every_films is not None
                else pipe_cfg.get("request_debug_excel_every", 25)
            ),
            meta=meta or {},
        )
        self.http.attach_debug_log(dbg)
        self._request_debug = dbg  # type: ignore[attr-defined]
        return dbg

    @staticmethod
    def resume_key(item: InputTitle) -> str:
        """Stable key for gather checkpoint (prefer import row uniqueness)."""
        if item.source_file or item.source_sheet or item.source_row is not None:
            return "|".join(
                [
                    str(item.source_file or ""),
                    str(item.source_sheet or ""),
                    str(item.source_row or ""),
                    str(item.import_title or item.title or "").strip().lower(),
                    str(item.import_year if item.import_year is not None else item.year or ""),
                ]
            )
        return item.cache_key()

    def gather(
        self,
        items: list[InputTitle],
        *,
        show_progress: bool = True,
        resume: bool = True,
        checkpoint_path: Optional[str | Path] = None,
        progress_every: int = 25,
        live_export: bool = True,
    ) -> list[EnrichmentProfile]:
        """
        Phase A gather with JSONL checkpoint resume.

        Each finished title is:
          1) appended to gather_checkpoint.jsonl
          2) appended to live text + CSV reports
          3) periodically written into the rolling live Excel workbook
        """
        from psychofilm_analyzer.enrichment.export import (
            append_live_csv,
            append_live_text,
            bootstrap_live_exports_from_checkpoint,
            write_live_excel,
        )

        pipe_cfg = self.config.get("pipeline") or {}
        out_dir = Path((self.config.get("output") or {}).get("dir", "output"))
        ckpt = Path(
            checkpoint_path
            or pipe_cfg.get("gather_checkpoint", "output/gather_checkpoint.jsonl")
        )
        progress_file = Path(pipe_cfg.get("gather_progress", "output/gather_progress.json"))
        live_txt = Path(pipe_cfg.get("gather_live_txt", out_dir / "gather_live.txt"))
        live_csv = Path(pipe_cfg.get("gather_live_csv", out_dir / "gather_live.csv"))
        live_xlsx = Path(pipe_cfg.get("gather_live_xlsx", out_dir / "gather_live.xlsx"))
        excel_every = int(pipe_cfg.get("gather_excel_every", 10))
        ckpt.parent.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)

        done_keys: set[str] = set()
        if resume and ckpt.exists():
            try:
                with ckpt.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            row = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        key = row.get("_resume_key")
                        if key:
                            done_keys.add(str(key))
                logger.info("Gather resume: %s titles already in checkpoint", len(done_keys))
            except OSError as exc:
                logger.warning("Could not read gather checkpoint: %s", exc)

        # Rebuild live text/csv/excel from checkpoint so files match completed work
        total_done = len(done_keys)
        if live_export and total_done:
            try:
                n = bootstrap_live_exports_from_checkpoint(
                    ckpt,
                    text_path=live_txt,
                    csv_path=live_csv,
                    excel_path=live_xlsx,
                    rebuild_excel=True,
                )
                logger.info("Live exports rebuilt from checkpoint (%s profiles)", n)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not bootstrap live exports: %s", exc)
        elif live_export:
            # Fresh run: start empty text header
            live_txt.write_text(
                "PsychoFilm gather live report\n"
                f"started: {datetime.now(timezone.utc).isoformat()}\n\n",
                encoding="utf-8",
            )
            if live_csv.exists():
                live_csv.unlink()

        profiles: list[EnrichmentProfile] = []
        self._gather_checkpoint_path = ckpt  # type: ignore[attr-defined]
        self._gather_precompleted = len(done_keys)  # type: ignore[attr-defined]
        self._gather_live_paths = {  # type: ignore[attr-defined]
            "txt": live_txt,
            "csv": live_csv,
            "xlsx": live_xlsx,
        }

        iterator = items
        if show_progress:
            iterator = tqdm(items, desc="Gather", unit="title")

        processed_new = 0
        dbg = getattr(self, "_request_debug", None) or getattr(self.http, "debug_log", None)
        # Map catalog position (1-based) for cell reporting
        catalog_pos: dict[int, int] = {id(it): i + 1 for i, it in enumerate(items)}

        for item in iterator:
            key = self.resume_key(item)
            if key in done_keys:
                continue

            # Live Excel row after append: header=1, film N → row N+1
            predicted_live_row = total_done + 1 + 1  # next film index + header

            if dbg is not None:
                from psychofilm_analyzer.utils.request_debug import FilmContext

                film_seq = processed_new + 1
                dbg.begin_film(
                    FilmContext(
                        film_seq=film_seq,
                        catalog_index=catalog_pos.get(id(item)),
                        excel_sheet=item.source_sheet,
                        excel_row=item.source_row,
                        live_excel_sheet="profiles",
                        live_excel_row=predicted_live_row,
                        title=item.title,
                        year=item.year,
                        english_title=item.english_title,
                        russian_title=item.russian_title,
                        import_title=item.import_title,
                        import_year=item.import_year,
                        media_type=item.media_type.value if item.media_type else None,
                        source_file=item.source_file,
                        resume_key=key,
                        imdb_id_hint=item.imdb_id_hint,
                        tmdb_id_hint=item.tmdb_id_hint,
                        kinopoisk_id_hint=item.kinopoisk_id_hint,
                    )
                )

            profile = self.gather_item(item)
            profiles.append(profile)
            done_keys.add(key)
            processed_new += 1
            total_done = len(done_keys)
            payload = profile.to_json_dict()
            ckpt_line: Optional[int] = None
            try:
                payload["_resume_key"] = key
                with ckpt.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
                # 1-based line count in checkpoint after write
                try:
                    with ckpt.open("r", encoding="utf-8") as rh:
                        ckpt_line = sum(1 for ln in rh if ln.strip())
                except OSError:
                    ckpt_line = total_done
            except OSError as exc:
                logger.warning("Checkpoint append failed: %s", exc)

            if live_export:
                try:
                    # strip resume key for export dict
                    export_d = {k: v for k, v in payload.items() if k != "_resume_key"}
                    append_live_text(export_d, live_txt, index=total_done)
                    append_live_csv(export_d, live_csv, index=total_done)
                    # Rolling Excel: every N films + always first film of session
                    if processed_new == 1 or (excel_every and total_done % excel_every == 0):
                        all_dicts = self.load_gather_checkpoint(ckpt)
                        write_live_excel(all_dicts, live_xlsx, include_evidence=False)
                        logger.info(
                            "Live Excel updated (%s films) → %s", total_done, live_xlsx
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Live export failed for %s: %s", item.title, exc)

            if dbg is not None:
                found_map = {}
                try:
                    sources = (payload.get("sources") or {}) if isinstance(payload, dict) else {}
                    if isinstance(sources, dict):
                        for sn, sv in sources.items():
                            if isinstance(sv, dict):
                                found_map[str(sn)] = bool(sv.get("found"))
                    # fallback from profile object
                    if not found_map and hasattr(profile, "sources"):
                        for sn, sp in (profile.sources or {}).items():
                            found_map[str(sn)] = bool(getattr(sp, "found", False))
                except Exception:  # noqa: BLE001
                    pass
                dbg.end_film(
                    found_sources=found_map or None,
                    error=getattr(profile, "error", None),
                    gather_checkpoint_line=ckpt_line,
                    live_excel_row=total_done + 1,  # header + N films
                )

            if progress_every and processed_new % progress_every == 0:
                self._write_gather_progress(
                    progress_file,
                    total=len(items),
                    done=len(done_keys),
                    new=processed_new,
                    last_title=item.display_title(),
                )

        # Final live Excel flush
        if live_export and processed_new:
            try:
                all_dicts = self.load_gather_checkpoint(ckpt)
                write_live_excel(all_dicts, live_xlsx, include_evidence=False)
                logger.info("Final live Excel (%s films) → %s", len(all_dicts), live_xlsx)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Final live Excel failed: %s", exc)

        self._write_gather_progress(
            progress_file,
            total=len(items),
            done=len(done_keys),
            new=processed_new,
            last_title=profiles[-1].input.display_title() if profiles else None,
            finished=True,
        )
        logger.info(
            "Gather finished: %s new this run, %s total in checkpoint",
            processed_new,
            len(done_keys),
        )
        if live_export:
            logger.info("Live files: txt=%s csv=%s xlsx=%s", live_txt, live_csv, live_xlsx)
        if dbg is not None:
            try:
                dbg.close()
                logger.info("Request debug log closed → %s", getattr(dbg, "path", "?"))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Request debug close failed: %s", exc)
        return profiles

    def load_gather_checkpoint(
        self, checkpoint_path: Optional[str | Path] = None
    ) -> list[dict[str, Any]]:
        """Load full profile dicts from gather JSONL checkpoint (for export / score-v3)."""
        pipe_cfg = self.config.get("pipeline") or {}
        ckpt = Path(
            checkpoint_path
            or getattr(self, "_gather_checkpoint_path", None)
            or pipe_cfg.get("gather_checkpoint", "output/gather_checkpoint.jsonl")
        )
        if not ckpt.exists():
            return []
        out: list[dict[str, Any]] = []
        with ckpt.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                row.pop("_resume_key", None)
                out.append(row)
        return out

    @staticmethod
    def _write_gather_progress(
        path: Path,
        *,
        total: int,
        done: int,
        new: int,
        last_title: Optional[str] = None,
        finished: bool = False,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "total_input": total,
            "done_checkpoint": done,
            "new_this_run": new,
            "remaining": max(0, total - done),
            "pct": round(100.0 * done / total, 2) if total else 0.0,
            "last_title": last_title,
            "finished": finished,
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def run(
        self,
        items: list[InputTitle],
        *,
        resume: bool = True,
        show_progress: bool = True,
    ) -> list[EnrichedResult]:
        done_keys = set()
        results: list[EnrichedResult] = []
        if resume:
            prev = self._load_state()
            if prev:
                results = prev
                done_keys = {r.input.cache_key() for r in results}
                logger.info("Resume: %s results already completed", len(results))

        iterator = items
        if show_progress:
            iterator = tqdm(items, desc="PsychoFilm", unit="title")

        for item in iterator:
            key = item.cache_key()
            if key in done_keys:
                continue
            try:
                result = self.process_item(item)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Failed processing %s", item.title)
                result = EnrichedResult(input=item, error=str(exc))
            results.append(result)
            done_keys.add(key)
            if resume:
                self._save_state(results)
        return results

    def run_file(
        self,
        path: str | Path,
        *,
        limit: Optional[int] = None,
        resume: bool = True,
        write: bool = True,
    ) -> list[EnrichedResult]:
        items = load_titles(path, limit=limit)
        logger.info("Loaded %s titles from %s", len(items), path)
        results = self.run(items, resume=resume)
        if write:
            out_cfg = self.config.get("output") or {}
            written = write_outputs(
                results,
                output_dir=out_cfg.get("dir", "output"),
                excel=bool(out_cfg.get("excel", True)),
                json_out=bool(out_cfg.get("json", True)),
                markdown_top_n=int(out_cfg.get("markdown_top_n", 25)),
                markdown_min_score=float(out_cfg.get("markdown_min_score", 7.0)),
            )
            logger.info("Wrote outputs: %s", {k: str(v) for k, v in written.items()})
        return results

    def _load_state(self) -> list[EnrichedResult]:
        if not self.state_file.exists():
            return []
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            # store flat minimal resume: re-hydrate lightly
            from psychofilm_analyzer.models import Confidence, FactorScores, MediaType

            results = []
            for row in data:
                inp = row.get("input") or {}
                item = InputTitle(
                    title=inp.get("title", "unknown"),
                    year=inp.get("year"),
                    media_type=MediaType(inp.get("media_type", "unknown"))
                    if inp.get("media_type") in MediaType._value2member_map_
                    else MediaType.UNKNOWN,
                    season=inp.get("season"),
                    english_title=inp.get("english_title"),
                    russian_title=inp.get("russian_title"),
                    genre_hint=inp.get("genre_hint"),
                    director_hint=inp.get("director_hint"),
                    imdb_rating_hint=inp.get("imdb_rating_hint"),
                    kinopoisk_rating_hint=inp.get("kinopoisk_rating_hint"),
                    collection=inp.get("collection"),
                    source_row=inp.get("source_row"),
                )
                factors = FactorScores(**(row.get("factors") or {}))
                conf = row.get("confidence", "Low")
                results.append(
                    EnrichedResult(
                        input=item,
                        psycho_score=float(row.get("psycho_score") or 0),
                        primary_cluster=row.get("primary_cluster"),
                        secondary_cluster=row.get("secondary_cluster"),
                        confidence=Confidence(conf) if conf in Confidence._value2member_map_ else Confidence.LOW,
                        description=row.get("description") or "",
                        factors=factors,
                        cluster_scores=row.get("cluster_scores") or {},
                        normalized_title_en=row.get("normalized_title_en"),
                        normalized_title_ru=row.get("normalized_title_ru"),
                        year=row.get("year"),
                        media_type=row.get("media_type") or item.media_type.value,
                        season=row.get("season"),
                        imdb_rating=row.get("imdb_rating"),
                        kinopoisk_rating=row.get("kinopoisk_rating"),
                        tmdb_rating=row.get("tmdb_rating"),
                        genres=row.get("genres") or [],
                        directors=row.get("directors") or [],
                        keywords=row.get("keywords") or [],
                        source_links=row.get("source_links") or [],
                        caps_applied=row.get("caps_applied") or [],
                        notes=row.get("notes") or [],
                        error=row.get("error"),
                    )
                )
            return results
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not load state: %s", exc)
            return []

    def _save_state(self, results: list[EnrichedResult]) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = [r.to_json_dict() for r in results]
        # strip heavy source payloads from state to keep file smaller? keep for provenance
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.state_file)


def configure_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quiet noisy libs
    logging.getLogger("urllib3").setLevel(logging.WARNING)

"""Main enrichment & scoring pipeline."""

from __future__ import annotations

import json
import logging
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
        self.http = HttpClient(
            delay_sec=float(http_cfg.get("delay_sec", 0.6)),
            timeout_sec=float(http_cfg.get("timeout_sec", 25)),
            max_retries=int(http_cfg.get("max_retries", 3)),
            user_agent=http_cfg.get("user_agent", "PsychoFilmAnalyzer/1.0"),
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

    def gather(
        self,
        items: list[InputTitle],
        *,
        show_progress: bool = True,
    ) -> list[EnrichmentProfile]:
        iterator = items
        if show_progress:
            iterator = tqdm(items, desc="Gather", unit="title")
        profiles: list[EnrichmentProfile] = []
        for item in iterator:
            profiles.append(self.gather_item(item))
        return profiles

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

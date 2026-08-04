"""Weighted Psycho_Score engine with hard caps."""

from __future__ import annotations

from typing import Any, Optional

from psychofilm_analyzer.features import (
    score_awards_prestige,
    score_director_reputation,
    score_discourse,
    score_discussability,
    score_narrative_depth,
    score_thematic_density,
)
from psychofilm_analyzer.features.metadata import assemble_metadata
from psychofilm_analyzer.features.text_aggregate import (
    best_rating,
    count_found_sources,
)
from psychofilm_analyzer.models import (
    Confidence,
    EnrichedResult,
    FactorScores,
    InputTitle,
    SourcePayload,
)
from psychofilm_analyzer.scoring.cluster import assign_clusters
from psychofilm_analyzer.scoring.description import generate_description


class ScoringEngine:
    def __init__(self, config: dict[str, Any], dictionaries: dict[str, Any]):
        self.config = config
        self.dictionaries = dictionaries
        self.weights = config.get("weights") or {}
        self.caps = config.get("caps") or {}
        self.conf_cfg = config.get("confidence") or {}

    def score(self, item: InputTitle, sources: dict[str, SourcePayload]) -> EnrichedResult:
        result = EnrichedResult(input=item, sources=sources)

        # Language-separated metadata, unique IDs, external links
        assemble_metadata(result, item, sources)
        result.imdb_rating = best_rating(sources, "omdb") or item.imdb_rating_hint
        result.kinopoisk_rating = best_rating(sources, "kinopoisk") or item.kinopoisk_rating_hint
        result.tmdb_rating = best_rating(sources, "tmdb")

        # Factors
        thematic, cluster_hits, matched = score_thematic_density(sources, self.dictionaries)
        narrative = score_narrative_depth(sources, self.dictionaries)
        awards = score_awards_prestige(sources, self.dictionaries)
        discourse = score_discourse(sources, self.dictionaries, thematic, cluster_hits)
        director = score_director_reputation(
            sources,
            self.dictionaries,
            result.directors_en or result.directors or result.directors_ru,
        )
        discuss = score_discussability(
            sources,
            self.dictionaries,
            thematic=thematic,
            narrative=narrative,
            discourse=discourse,
            awards=awards,
        )

        factors = FactorScores(
            thematic_keyword_density=thematic,
            narrative_character_depth=narrative,
            awards_prestige=awards,
            critical_intellectual_discourse=discourse,
            director_creator_reputation=director,
            discussability_podcast=discuss,
        )
        result.factors = factors
        result.cluster_scores = cluster_hits
        result.primary_cluster, result.secondary_cluster = assign_clusters(cluster_hits)

        # Weighted score over available factors (renormalize if some missing)
        weight_map = {
            "thematic_keyword_density": self.weights.get("thematic_keyword_density", 0.25),
            "narrative_character_depth": self.weights.get("narrative_character_depth", 0.20),
            "awards_prestige": self.weights.get("awards_prestige", 0.15),
            "critical_intellectual_discourse": self.weights.get("critical_intellectual_discourse", 0.20),
            "director_creator_reputation": self.weights.get("director_creator_reputation", 0.10),
            "discussability_podcast": self.weights.get("discussability_podcast", 0.10),
        }
        available = factors.available()
        if not available:
            result.psycho_score = 0.0
            result.confidence = Confidence.LOW
            result.description = generate_description(result, self.dictionaries)
            result.description_en = result.description
            result.notes.append("No factor data available")
            return result

        w_sum = sum(weight_map[k] for k in available)
        psycho = sum(available[k] * weight_map[k] for k in available) / w_sum
        psycho, caps = self._apply_caps(psycho, result, discourse)
        result.psycho_score = round(max(0.0, min(10.0, psycho)), 2)
        result.caps_applied = caps
        result.confidence = self._confidence(sources, available)
        if matched:
            result.notes.append(f"Matched theme keywords: {', '.join(matched[:12])}")
        result.description = generate_description(result, self.dictionaries)
        result.description_en = result.description
        return result

    def _apply_caps(
        self,
        psycho: float,
        result: EnrichedResult,
        discourse: Optional[float],
    ) -> tuple[float, list[str]]:
        caps_applied: list[str] = []
        genres_l = [g.lower() for g in result.genres]
        genre_cfg = self.dictionaries.get("genres") or {}
        spectacle = [s.lower() for s in genre_cfg.get("spectacle") or []]
        high_psych = [s.lower() for s in genre_cfg.get("high_psych") or []]
        docs = [s.lower() for s in genre_cfg.get("documentary") or []]

        is_spectacle = any(any(s in g for s in spectacle) for g in genres_l)
        is_high = any(any(h in g for h in high_psych) for g in genres_l) or (
            (result.factors.thematic_keyword_density or 0) >= 5.0
        )
        is_doc = any(any(d in g for d in docs) for g in genres_l)

        pure_max = float(self.caps.get("pure_spectacle_max", 4.0))
        doc_max = float(self.caps.get("documentary_no_psych_max", 2.0))
        low_max = float(self.caps.get("low_rating_max", 3.5))
        low_thr = float(self.caps.get("low_rating_threshold", 6.0))
        disc_bypass = float(self.caps.get("exceptional_discourse_bypass", 7.5))

        if is_spectacle and not is_high:
            if psycho > pure_max:
                psycho = pure_max
                caps_applied.append(f"pure_spectacle_max={pure_max}")

        if is_doc and (result.factors.thematic_keyword_density or 0) < 4.0:
            if psycho > doc_max:
                psycho = doc_max
                caps_applied.append(f"documentary_no_psych_max={doc_max}")

        ratings = [r for r in (result.imdb_rating, result.kinopoisk_rating, result.tmdb_rating) if r is not None]
        if ratings:
            base_quality = max(ratings)
            exceptional_discourse = discourse is not None and discourse >= disc_bypass
            if base_quality < low_thr and not exceptional_discourse and psycho > low_max:
                psycho = low_max
                caps_applied.append(f"low_rating_max={low_max} (rating={base_quality})")

        return psycho, caps_applied

    def _confidence(self, sources: dict[str, SourcePayload], available: dict[str, float]) -> Confidence:
        n_sources = count_found_sources(sources)
        n_factors = len(available)
        high_min = int(self.conf_cfg.get("high_min_sources", 4))
        med_min = int(self.conf_cfg.get("medium_min_sources", 2))
        if n_sources >= high_min and n_factors >= 5:
            return Confidence.HIGH
        if n_sources >= med_min and n_factors >= 3:
            return Confidence.MEDIUM
        return Confidence.LOW

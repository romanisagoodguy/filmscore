"""v3 multi-score engine: profiles → scores + clusters + fields A–J."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml

from psychofilm_analyzer.config import ROOT
from psychofilm_analyzer.scoring.fields_v3 import extract_all_fields
from psychofilm_analyzer.scoring.phrase_engine import (
    ScoreResult,
    score_dictionary,
    spectrum_score,
)


def load_dictionaries_v3(path: Optional[Path] = None) -> dict:
    path = path or (ROOT / "config" / "dictionaries_v3.yaml")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _profile_bags(profile: dict) -> list[dict]:
    bags = list(profile.get("evidence_bags") or [])
    # rebuild minimal bags from flat fields if JSON was flattened-only
    if bags:
        return bags
    rebuilt = []
    for name, key, lang, w in [
        ("plot_en", "plot_en", "en", 1.0),
        ("plot_ru", "plot_ru", "ru", 1.0),
        ("keywords_en", "keywords", "en", 1.2),
        ("genres_en", "genres_en", "en", 0.8),
        ("genres_ru", "genres_ru", "ru", 0.8),
        ("awards_en", "awards_text", "en", 0.5),
    ]:
        # flat list format from profile_latest
        text = profile.get(key)
        if isinstance(text, list):
            text = "; ".join(str(x) for x in text)
        if text:
            rebuilt.append({"name": name, "text": str(text), "source": "profile", "language": lang, "weight": w})
    # nested format from full JSON
    plots = profile.get("plots") or {}
    if plots.get("en"):
        rebuilt.append({"name": "plot_en", "text": plots["en"], "source": "profile", "language": "en", "weight": 1.0})
    if plots.get("ru"):
        rebuilt.append({"name": "plot_ru", "text": plots["ru"], "source": "profile", "language": "ru", "weight": 1.0})
    kws = profile.get("keywords")
    if kws:
        text = "; ".join(kws) if isinstance(kws, list) else str(kws)
        rebuilt.append({"name": "keywords_en", "text": text, "source": "profile", "language": "en", "weight": 1.2})
    return rebuilt


def _is_spectacle(profile: dict, bags: list[dict]) -> bool:
    flags = profile.get("type_flags") or {}
    if flags.get("is_spectacle"):
        return True
    # flat format
    if profile.get("flag_spectacle"):
        return True
    return False


def _is_doc(profile: dict) -> bool:
    if profile.get("content_type") == "documentary":
        return True
    if (profile.get("type_flags") or {}).get("is_documentary"):
        return True
    return bool(profile.get("flag_documentary"))


def score_profile(profile: dict, dicts: Optional[dict] = None) -> dict[str, Any]:
    """Score one gather profile; returns full v3 result dict."""
    dicts = dicts or load_dictionaries_v3()
    bags = _profile_bags(profile)
    # ensure bags attached for field extractor
    profile = dict(profile)
    profile["evidence_bags"] = bags

    caps = dicts.get("caps") or {}
    score_defs = dicts.get("scores") or {}
    cluster_defs = dicts.get("clusters") or {}

    spectacle = _is_spectacle(profile, bags)
    documentary = _is_doc(profile)
    psych_cap = None
    cap_rule = None
    if spectacle:
        psych_cap = float(caps.get("spectacle_psych_max", 4.0))
        cap_rule = "spectacle_psych_max"
    elif documentary:
        psych_cap = float(caps.get("documentary_psych_max", 5.5))
        cap_rule = "documentary_psych_max"

    scores: dict[str, ScoreResult] = {}

    # multi-scores
    for sname, sdef in score_defs.items():
        if sname in ("machine_nature", "spiritual_nature"):
            continue
        phrases = (sdef or {}).get("phrases") or sdef
        # psych family gets spectacle/doc caps
        is_psych = sname in {
            "Psychological_Depth",
            "Trauma_Clinical_Relevance",
            "Identity_Transformation",
            "Madness_Altered_States",
            "Family_Systems_Complexity",
            "Existential_Weight",
            "Collective_Historical_Psychotype",
        }
        # Higher scale => less saturation at 10 for common drama keywords
        scale = 3.4 if sname == "Psychological_Depth" else (2.4 if is_psych else 2.5)
        scores[sname] = score_dictionary(
            sname,
            bags,
            phrases,
            scale=scale,
            cap=psych_cap if is_psych else None,
            cap_rule=cap_rule if is_psych else None,
        )

    # Human nature spectrum
    scores["Human_Nature_Spectrum"] = spectrum_score(
        bags,
        (score_defs.get("machine_nature") or {}).get("phrases"),
        (score_defs.get("spiritual_nature") or {}).get("phrases"),
    )

    # Watchability (hybrid rules)
    easy = 6.5
    if spectacle:
        easy += 1.5
    if documentary:
        easy -= 1.0
    if profile.get("content_type") == "animation":
        easy += 0.8
    runtime = profile.get("runtime_min")
    if runtime and runtime > 150:
        easy -= 1.2
    elif runtime and runtime > 120:
        easy -= 0.5
    # arthouse / dense psych lowers easy
    pd = scores.get("Psychological_Depth")
    if pd and pd.score >= 7:
        easy -= 1.5
    elif pd and pd.score >= 5:
        easy -= 0.7
    nc = scores.get("Narrative_Craft")
    if nc and nc.score >= 6:
        easy -= 0.5
    easy = max(0.0, min(10.0, easy))
    scores["Easy_to_Watch"] = ScoreResult(
        name="Easy_to_Watch",
        score=easy,
        confidence="Medium",
        evidence=[],
        negatives=[],
        raw_hits=easy,
    )

    # Interesting / engagement from ratings + discussability
    ratings = []
    for k in ("imdb_rating", "kinopoisk_rating", "tmdb_rating"):
        # nested
        pass
    rblock = profile.get("ratings") or {}
    for k in ("imdb", "kinopoisk", "tmdb"):
        if rblock.get(k) is not None:
            ratings.append(float(rblock[k]))
    for k in ("imdb_rating", "kinopoisk_rating", "tmdb_rating"):
        if profile.get(k) is not None:
            ratings.append(float(profile[k]))
    avg_r = sum(ratings) / len(ratings) if ratings else 6.5
    disc = scores.get("Discussability_Podcast_Potential")
    interesting = min(10.0, max(0.0, (avg_r - 4.0) * 1.4 + (disc.score * 0.35 if disc else 0)))
    scores["Interesting_to_Watch_Engagement"] = ScoreResult(
        name="Interesting_to_Watch_Engagement",
        score=interesting,
        confidence="Medium" if ratings else "Low",
        evidence=[],
        raw_hits=interesting,
    )

    # Podcast priority composite
    def g(n: str) -> float:
        s = scores.get(n)
        return s.score if s else 0.0

    podcast_priority = (
        0.35 * g("Psychological_Depth")
        + 0.25 * g("Discussability_Podcast_Potential")
        + 0.15 * g("Narrative_Craft")
        + 0.15 * g("Existential_Weight")
        + 0.10 * g("Spiritual_Religious_Mystical_Depth")
    )
    scores["Podcast_Priority"] = ScoreResult(
        name="Podcast_Priority",
        score=min(10.0, podcast_priority),
        confidence="Medium",
        evidence=[],
        raw_hits=podcast_priority,
    )

    # Clusters
    cluster_scores: dict[str, ScoreResult] = {}
    for cid, cdef in cluster_defs.items():
        name = cdef.get("name") or cid
        cluster_scores[name] = score_dictionary(
            name,
            bags,
            cdef.get("phrases"),
            scale=2.0,
            cap=psych_cap,
            cap_rule=cap_rule,
        )

    # Primary / secondary
    primary_min = float(caps.get("primary_min", 3.0))
    secondary_min = float(caps.get("secondary_min", 2.0))
    secondary_ratio = float(caps.get("secondary_ratio", 0.5))
    ranking = sorted(
        ((n, s.score, s) for n, s in cluster_scores.items()),
        key=lambda x: -x[1],
    )
    primary = "Underspecified / Low psych signal"
    secondary = None
    theme_confidence = "Low"
    psych_depth = g("Psychological_Depth")
    # Require real psych density before naming a primary taxonomy cluster
    allow_primary = psych_depth >= 2.5 or g("Trauma_Clinical_Relevance") >= 4.0 or g("Madness_Altered_States") >= 4.0
    if ranking and allow_primary:
        top_name, top_score, top_sr = ranking[0]
        strong = top_score >= primary_min and (
            len(top_sr.evidence) >= 2 or any(e.tier >= 3 for e in top_sr.evidence)
        )
        if strong:
            primary = top_name
            theme_confidence = top_sr.confidence
            if len(ranking) > 1:
                n2, s2, sr2 = ranking[1]
                if s2 >= secondary_min and s2 >= top_score * secondary_ratio:
                    secondary = n2
    elif spectacle or psych_depth < 2.5:
        primary = "Underspecified / Low psych signal"
        theme_confidence = "Medium"

    fields = extract_all_fields(
        profile,
        dicts,
        scores,
        [(n, sc) for n, sc, _ in ranking],
        primary,
        secondary,
    )

    # Identity flat helpers
    titles = profile.get("titles") or {}
    imported = profile.get("imported") or {}
    ids = profile.get("ids") or {}
    links = profile.get("links") or {}
    countries = profile.get("countries") or {}
    plots = profile.get("plots") or {}
    genres = profile.get("genres") or {}
    crew = profile.get("crew") or {}
    cast = profile.get("cast") or {}
    ratings = profile.get("ratings") or {}

    def pick(*keys):
        for k in keys:
            if profile.get(k) is not None:
                return profile.get(k)
        return None

    flat = {
        "imported_file": imported.get("file") or pick("imported_file"),
        "imported_sheet": imported.get("sheet") or pick("imported_sheet"),
        "imported_row": imported.get("row") or pick("imported_row"),
        "imported_title": imported.get("title") or pick("imported_title"),
        "imported_year": imported.get("year") or pick("imported_year"),
        "film_uid": profile.get("film_uid"),
        "imdb_id": ids.get("imdb_id") or pick("imdb_id"),
        "tmdb_id": ids.get("tmdb_id") or pick("tmdb_id"),
        "kinopoisk_id": ids.get("kinopoisk_id") or pick("kinopoisk_id"),
        "title_en": titles.get("en") or pick("title_en"),
        "title_ru": titles.get("ru") or pick("title_ru"),
        "title_original": titles.get("original") or pick("title_original"),
        "year": profile.get("year"),
        "content_type": profile.get("content_type"),
        "media_type": profile.get("media_type"),
        "runtime_min": profile.get("runtime_min"),
        "countries_en": "; ".join(countries.get("en") or []) if isinstance(countries.get("en"), list) else pick("countries_en"),
        "countries_ru": "; ".join(countries.get("ru") or []) if isinstance(countries.get("ru"), list) else pick("countries_ru"),
        "plot_en": plots.get("en") or pick("plot_en"),
        "plot_ru": plots.get("ru") or pick("plot_ru"),
        "genres_en": "; ".join(genres.get("en") or []) if isinstance(genres.get("en"), list) else pick("genres_en"),
        "genres_ru": "; ".join(genres.get("ru") or []) if isinstance(genres.get("ru"), list) else pick("genres_ru"),
        "keywords": "; ".join(profile.get("keywords") or []) if isinstance(profile.get("keywords"), list) else pick("keywords"),
        "directors_en": "; ".join(crew.get("directors_en") or []) if isinstance(crew.get("directors_en"), list) else pick("directors_en"),
        "directors_ru": "; ".join(crew.get("directors_ru") or []) if isinstance(crew.get("directors_ru"), list) else pick("directors_ru"),
        "writers_en": "; ".join(crew.get("writers_en") or []) if isinstance(crew.get("writers_en"), list) else pick("writers_en"),
        "composers_en": "; ".join(crew.get("composers_en") or []) if isinstance(crew.get("composers_en"), list) else pick("composers_en"),
        "composers_ru": "; ".join(crew.get("composers_ru") or []) if isinstance(crew.get("composers_ru"), list) else pick("composers_ru"),
        "actors_en": "; ".join(cast.get("actors_en") or []) if isinstance(cast.get("actors_en"), list) else pick("actors_en"),
        "actors_ru": "; ".join(cast.get("actors_ru") or []) if isinstance(cast.get("actors_ru"), list) else pick("actors_ru"),
        "imdb_rating": ratings.get("imdb") or pick("imdb_rating"),
        "kinopoisk_rating": ratings.get("kinopoisk") or pick("kinopoisk_rating"),
        "tmdb_rating": ratings.get("tmdb") or pick("tmdb_rating"),
        "link_imdb": links.get("imdb") or pick("link_imdb"),
        "link_tmdb": links.get("tmdb") or pick("link_tmdb"),
        "link_kinopoisk": links.get("kinopoisk") or pick("link_kinopoisk"),
        "link_wikipedia_en": links.get("wikipedia_en") or pick("link_wikipedia_en"),
        "link_wikipedia_ru": links.get("wikipedia_ru") or pick("link_wikipedia_ru"),
        "primary_theme": primary,
        "secondary_theme": secondary,
        "theme_confidence": theme_confidence,
    }
    for n, s in scores.items():
        flat[f"score_{n}"] = round(s.score, 2)
        flat[f"conf_{n}"] = s.confidence
    for n, s in cluster_scores.items():
        key = "cluster_" + n.split()[0].lower().replace("/", "_").replace("&", "and")
        # stable keys
        flat[f"cluster_score_{n}"] = round(s.score, 2)
    flat.update(fields)

    return {
        "film_uid": profile.get("film_uid"),
        "title_en": flat.get("title_en"),
        "primary_theme": primary,
        "secondary_theme": secondary,
        "theme_confidence": theme_confidence,
        "scores": {k: v.to_dict() for k, v in scores.items()},
        "cluster_scores": {k: v.to_dict() for k, v in cluster_scores.items()},
        "fields": fields,
        "flat": flat,
        "evidence_index": _evidence_index(flat.get("title_en"), scores, cluster_scores),
    }


def _evidence_index(title, scores, clusters) -> list[dict]:
    rows = []
    for name, s in list(scores.items()) + list(clusters.items()):
        for e in s.evidence[:8]:
            rows.append(
                {
                    "title_en": title,
                    "score_or_cluster": name,
                    "phrase": e.phrase,
                    "bag": e.bag,
                    "source": e.source,
                    "tier": e.tier,
                    "weight": round(e.weight, 3),
                }
            )
    return rows


def score_profiles(profiles: list[dict], dicts: Optional[dict] = None) -> list[dict]:
    dicts = dicts or load_dictionaries_v3()
    return [score_profile(p, dicts) for p in profiles]

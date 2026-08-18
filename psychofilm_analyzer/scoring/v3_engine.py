"""v3 multi-score engine: profiles → scores + clusters + fields A–J."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

import yaml

from psychofilm_analyzer.config import ROOT
from psychofilm_analyzer.scoring.fields_v3 import extract_all_fields
from psychofilm_analyzer.scoring.phrase_engine import (
    AWARD_TOKENS_ON_DE,
    ScoreResult,
    _normalize_tiers,
    de_match_denylist,
    is_real_de_psych_hit,
    score_dictionary,
    spectrum_score,
)
from psychofilm_analyzer.utils.localtime import now_str, stamp_local


def collect_de_lexicon(dicts: dict) -> set[str]:
    """Union of explicit German packs + umlaut phrases already in the EN/RU lists.

    Award / genre / encyclopedia-generic tokens are stripped so they cannot
    sneak onto plot_de as psych evidence.
    """
    out: set[str] = set()
    de_block = dicts.get("de_psych_lexicon") or {}
    for p, _t in _normalize_tiers(de_block):
        out.add(p)
    for group in ((dicts.get("clusters") or {}), (dicts.get("scores") or {})):
        for block in group.values():
            if not isinstance(block, dict):
                continue
            for p, _t in _normalize_tiers(block.get("phrases_de")):
                out.add(p)
            for p, _t in _normalize_tiers(block.get("phrases")):
                if re.search(r"[äöüß]", p, flags=re.I):
                    out.add(p)
    deny = de_match_denylist() | {t.lower() for t in AWARD_TOKENS_ON_DE}
    return {p for p in out if p not in deny}


def _merge_phrases(sdef: Any) -> Any:
    if not isinstance(sdef, dict):
        return sdef
    base = sdef.get("phrases")
    extra = sdef.get("phrases_de")
    if not extra:
        return base if base is not None else sdef
    if isinstance(base, dict) and isinstance(extra, dict):
        out: dict[str, list[str]] = {}
        for key in ("t3", "t2", "t1"):
            out[key] = list(base.get(key) or []) + list(extra.get(key) or [])
        return out
    return base


def load_dictionaries_v3(path: Optional[Path] = None) -> dict:
    path = path or (ROOT / "config" / "dictionaries_v3.yaml")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _looks_cyrillic(text: str) -> bool:
    if not text:
        return False
    cyr = len(re.findall(r"[А-Яа-яЁё]", text))
    lat = len(re.findall(r"[A-Za-z]", text))
    return cyr > lat


def _profile_bags(profile: dict) -> list[dict]:
    """Collect evidence bags; ensure RU plot/awards from profile fields are present."""
    bags_in = list(profile.get("evidence_bags") or [])
    bags: list[dict] = []
    for b in bags_in:
        bb = dict(b)
        text = bb.get("text") or ""
        if text and _looks_cyrillic(text):
            bb["language"] = "ru"
            if bb.get("name") == "awards_en":
                bb["name"] = "awards_ru"
        # Kinopoisk RU narrative is first-class for this catalog
        if bb.get("source") == "kinopoisk" and bb.get("language") == "ru":
            bb["weight"] = max(float(bb.get("weight") or 1.0), 1.15)
        if bb.get("source") == "wikipedia" and bb.get("language") in {"en", "ru", "de"}:
            bb["weight"] = max(float(bb.get("weight") or 1.0), 1.1)
        bags.append(bb)

    def _has(name: str) -> bool:
        return any(b.get("name") == name for b in bags)

    def _add(name: str, text: Any, source: str, language: str, weight: float) -> None:
        if not text:
            return
        if isinstance(text, list):
            text = "; ".join(str(x) for x in text if x)
        t = str(text).strip()
        if len(t) < 8:
            return
        bags.append(
            {"name": name, "text": t, "source": source, "language": language, "weight": weight}
        )

    plots = profile.get("plots") or {}
    overviews = profile.get("overviews") or {}
    genres = profile.get("genres") or {}
    if not _has("plot_en") and (plots.get("en") or overviews.get("en") or profile.get("plot_en")):
        _add(
            "plot_en",
            plots.get("en") or overviews.get("en") or profile.get("plot_en"),
            "profile",
            "en",
            1.0,
        )
    if not _has("plot_ru") and (plots.get("ru") or overviews.get("ru") or profile.get("plot_ru")):
        _add(
            "plot_ru",
            plots.get("ru") or overviews.get("ru") or profile.get("plot_ru"),
            "profile",
            "ru",
            1.15,
        )
    if not _has("plot_de") and (plots.get("de") or overviews.get("de") or profile.get("plot_de")):
        _add(
            "plot_de",
            plots.get("de") or overviews.get("de") or profile.get("plot_de"),
            "profile",
            "de",
            1.1,
        )
    if not _has("keywords_en"):
        kws = profile.get("keywords")
        if kws:
            _add(
                "keywords_en",
                kws if isinstance(kws, str) else "; ".join(list(kws)[:80]),
                "profile",
                "en",
                1.2,
            )
    if not _has("genres_en") and (genres.get("en") or profile.get("genres_en")):
        g = genres.get("en") or profile.get("genres_en")
        _add("genres_en", g if isinstance(g, str) else "; ".join(g or []), "profile", "en", 0.8)
    if not _has("genres_ru") and (genres.get("ru") or profile.get("genres_ru")):
        g = genres.get("ru") or profile.get("genres_ru")
        _add("genres_ru", g if isinstance(g, str) else "; ".join(g or []), "profile", "ru", 0.9)

    awards = profile.get("awards_text")
    if awards:
        # Always split merged awards into EN/RU bags (even if awards_en already present)
        parts = [p.strip() for p in re.split(r"[;|/\n]+", str(awards)) if p and str(p).strip()]
        en_parts = [p for p in parts if not _looks_cyrillic(p)]
        ru_parts = [p for p in parts if _looks_cyrillic(p)]
        if not ru_parts and _looks_cyrillic(str(awards)):
            ru_parts = [str(awards).strip()]
            en_parts = []
        if en_parts and not _has("awards_en"):
            _add("awards_en", "; ".join(en_parts), "profile", "en", 0.7)
        if ru_parts:
            # Prefer always having awards_ru when Cyrillic awards exist
            already_ru = any(
                (b.get("name") == "awards_ru")
                and any(rp in (b.get("text") or "") for rp in ru_parts)
                for b in bags
            )
            if not already_ru:
                _add("awards_ru", "; ".join(ru_parts), "profile", "ru", 0.75)

    return bags


def _score_modern_viewer_deliverability(
    profile: dict,
    bags: list[dict],
    dicts: dict,
    scores: dict[str, ScoreResult],
    *,
    avg_rating: float,
    spectacle: bool,
    runtime: Optional[int],
) -> ScoreResult:
    """
    Modern_Viewer_Deliverability (0–10)

    Purpose: select films that are still worth watching *today* — content + delivery.
    High when: paced, suspenseful/engaging language, solid ratings, watchable length,
               and some substance (not empty fireworks).
    Low when: long/empty/slow signals, no pace, boring markers, or pure spectacle
              without psychological/narrative content.

    Components:
      - Pace/engagement phrases (positive lexicon)
      - Drag/slow/boring phrases (negative)
      - Easy_to_Watch + Interesting_to_Watch (existing hybrids)
      - Audience ratings
      - Content floor (Depth + Narrative) so empty thrills cannot max out
    """
    score_defs = dicts.get("scores") or {}
    de_lexicon = collect_de_lexicon(dicts)
    pace_phrases = _merge_phrases(score_defs.get("modern_deliverability_pace") or {})
    drag_phrases = _merge_phrases(score_defs.get("modern_deliverability_drag") or {})

    pace_sr = score_dictionary(
        "modern_deliverability_pace",
        bags,
        pace_phrases,
        scale=2.6,
        allow_t1_alone=True,
        de_lexicon=de_lexicon,
        allow_awards_on_de=False,
        de_corroborator=True,
    )
    drag_sr = score_dictionary(
        "modern_deliverability_drag",
        bags,
        drag_phrases,
        scale=2.4,
        allow_t1_alone=True,
        de_lexicon=de_lexicon,
        allow_awards_on_de=False,
        de_corroborator=True,
    )

    def g(n: str) -> float:
        s = scores.get(n)
        return float(s.score) if s else 0.0

    easy = g("Easy_to_Watch")
    interesting = g("Interesting_to_Watch_Engagement")
    depth = g("Psychological_Depth")
    craft = g("Narrative_Craft")
    discuss = g("Discussability_Podcast_Potential")

    # Map ratings ~4.5–8.5 → 0–10
    rating_comp = min(10.0, max(0.0, (float(avg_rating) - 4.5) * 1.85))

    # Engagement block: how “alive” the film feels for a modern viewer
    engage = (
        0.30 * interesting
        + 0.22 * easy
        + 0.28 * pace_sr.score
        + 0.20 * rating_comp
    )

    # Content block: must still have something to talk about / feel
    content = min(10.0, 0.55 * depth + 0.30 * craft + 0.15 * discuss)

    # Balance reward: high only when BOTH engagement and content show up
    # (avoids pure arthouse slog and pure empty blockbuster)
    balance_bonus = min(engage, max(content, 2.5)) * 0.35
    score = 0.45 * engage + 0.25 * content + balance_bonus

    # Drag / long-empty-scene proxies
    score -= 0.50 * drag_sr.score
    negatives: list[dict[str, Any]] = []
    if runtime and runtime >= 170:
        score -= 1.8
        negatives.append({"rule": "runtime_ge_170", "runtime_min": runtime})
    elif runtime and runtime >= 150:
        score -= 1.1
        negatives.append({"rule": "runtime_ge_150", "runtime_min": runtime})
    elif runtime and runtime >= 135:
        score -= 0.5
        negatives.append({"rule": "runtime_ge_135", "runtime_min": runtime})

    # Dense + hard + no pace language → often “long scenes without interesting moments”
    if depth >= 7.5 and easy <= 4.0 and pace_sr.score < 2.5:
        score -= 1.3
        negatives.append({"rule": "dense_hard_low_pace"})

    # Pure spectacle without substance cannot top the deliverability ladder
    if spectacle and content < 3.5:
        if score > 7.0:
            score = 7.0
            negatives.append({"rule": "spectacle_without_substance_cap", "cap": 7.0})

    # Very short films slightly easier to “deliver” for modern attention
    if runtime and runtime <= 95 and score > 0:
        score = min(10.0, score + 0.35)

    score = max(0.0, min(10.0, score))
    conf = "High" if (pace_sr.evidence or drag_sr.evidence) and score >= 6 else (
        "Medium" if score > 0 else "Low"
    )
    # Merge evidence (pace positive first, then drag as context)
    evidence = list(pace_sr.evidence[:10]) + list(drag_sr.evidence[:4])

    return ScoreResult(
        name="Modern_Viewer_Deliverability",
        score=round(score, 2),
        confidence=conf,
        evidence=evidence,
        negatives=negatives,
        raw_hits=pace_sr.raw_hits - 0.5 * drag_sr.raw_hits,
    )


def _score_awards_prestige(profile: dict, bags: list[dict], dicts: dict) -> ScoreResult:
    """
    Hybrid awards score:
      - phrase hits on awards bags + plot mentions (dictionary Awards_Prestige)
      - structured parse of awards_text (Oscar / festival ladders, RU awards)
    """
    phrases = _merge_phrases((dicts.get("scores") or {}).get("Awards_Prestige") or {})
    phrase_sr = score_dictionary(
        "Awards_Prestige",
        bags,
        phrases,
        scale=2.8,
        allow_t1_alone=True,
        de_lexicon=collect_de_lexicon(dicts),
        allow_awards_on_de=True,
        de_corroborator=False,
    )

    texts: list[str] = []
    if profile.get("awards_text"):
        texts.append(str(profile["awards_text"]))
    for b in bags:
        if str(b.get("name") or "").startswith("awards") and b.get("text"):
            texts.append(str(b["text"]))
    blob = " | ".join(texts).lower()

    structured = 0.0
    if blob.strip():
        # Top tier
        if any(
            x in blob
            for x in (
                "oscar",
                "academy award",
                "оскар",
                "palme",
                "пальмов",
                "golden lion",
                "золотой лев",
                "golden bear",
                "золотой медведь",
            )
        ):
            if "won" in blob or "win" in blob or "winner" in blob or "лауреат" in blob or "получил" in blob:
                structured = 9.0
            elif "nominat" in blob or "номинац" in blob:
                structured = 7.5
            else:
                structured = 7.0
        elif any(x in blob for x in ("bafta", "бафт", "golden globe", "золотой глобус", "cesar", "сезар", "emmy", "эмми")):
            structured = 6.5 if ("nominat" in blob or "номинац" in blob) else 7.5
        elif any(x in blob for x in ("cannes", "канн", "venice", "венеци", "berlin", "берлин", "ника", "nika")):
            structured = 6.0
        elif "nominat" in blob or "номинац" in blob or "wins" in blob or "премия" in blob or "наград" in blob:
            # count-ish signals
            m = re.search(r"(\d+)\s*win", blob)
            n = re.search(r"(\d+)\s*nomin", blob)
            wins = int(m.group(1)) if m else (3 if "win" in blob else 0)
            noms = int(n.group(1)) if n else (2 if "nomin" in blob else 0)
            structured = min(8.0, 3.0 + wins * 0.35 + noms * 0.12)
        else:
            structured = 2.5

    # Blend: max of structured ladder and phrase curve, with a floor from either signal
    score = max(phrase_sr.score, structured)
    if phrase_sr.score > 0 and structured > 0:
        score = min(10.0, 0.55 * structured + 0.45 * phrase_sr.score)
    conf = "High" if score >= 7 else ("Medium" if score >= 3.5 else ("Low" if score > 0 else "Low"))
    return ScoreResult(
        name="Awards_Prestige",
        score=round(score, 2),
        confidence=conf,
        evidence=phrase_sr.evidence,
        negatives=[],
        raw_hits=max(phrase_sr.raw_hits, structured),
    )


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
    de_lexicon = collect_de_lexicon(dicts)
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

    # multi-scores (dictionary-driven); hybrids handled separately below
    for sname, sdef in score_defs.items():
        if sname in (
            "machine_nature",
            "spiritual_nature",
            "Awards_Prestige",
            "modern_deliverability_pace",
            "modern_deliverability_drag",
        ):
            continue
        phrases = _merge_phrases(sdef) if isinstance(sdef, dict) else ((sdef or {}).get("phrases") or sdef)
        # psych family gets spectacle/doc caps
        is_psych = sname in {
            "Psychological_Depth",
            "Trauma_Clinical_Relevance",
            "Identity_Transformation",
            "Madness_Altered_States",
            "Family_Systems_Complexity",
            "Existential_Weight",
            "Collective_Historical_Psychotype",
            "Symbolism_Ambiguity",
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
            de_lexicon=de_lexicon,
            allow_awards_on_de=False,
            de_corroborator=True,
        )

    scores["Awards_Prestige"] = _score_awards_prestige(profile, bags, dicts)

    # Human nature spectrum
    scores["Human_Nature_Spectrum"] = spectrum_score(
        bags,
        _merge_phrases(score_defs.get("machine_nature") or {}),
        _merge_phrases(score_defs.get("spiritual_nature") or {}),
        de_lexicon=de_lexicon,
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

    def g(n: str) -> float:
        s = scores.get(n)
        return s.score if s else 0.0

    # Modern viewer deliverability: paced, non-boring, interesting for today's audience,
    # while still requiring some content substance (not empty spectacle).
    scores["Modern_Viewer_Deliverability"] = _score_modern_viewer_deliverability(
        profile,
        bags,
        dicts,
        scores,
        avg_rating=avg_r,
        spectacle=spectacle,
        runtime=runtime,
    )

    # Pure psych / discussability worth of an episode topic
    podcast_priority = (
        0.30 * g("Psychological_Depth")
        + 0.20 * g("Discussability_Podcast_Potential")
        + 0.15 * g("Symbolism_Ambiguity")
        + 0.15 * g("Narrative_Craft")
        + 0.12 * g("Existential_Weight")
        + 0.08 * g("Spiritual_Religious_Mystical_Depth")
    )
    scores["Podcast_Priority"] = ScoreResult(
        name="Podcast_Priority",
        score=min(10.0, podcast_priority),
        confidence="Medium",
        evidence=[],
        raw_hits=podcast_priority,
    )

    # Schedule / overall selection rank: content worth + modern deliverability + prestige
    # Deliberately NOT equal to Podcast_Priority.
    overall_priority = (
        0.32 * g("Podcast_Priority")
        + 0.28 * g("Modern_Viewer_Deliverability")
        + 0.15 * g("Awards_Prestige")
        + 0.15 * g("Interesting_to_Watch_Engagement")
        + 0.10 * g("Discussability_Podcast_Potential")
    )
    scores["Overall_Priority_for_Podcast"] = ScoreResult(
        name="Overall_Priority_for_Podcast",
        score=min(10.0, overall_priority),
        confidence="Medium",
        evidence=[],
        raw_hits=overall_priority,
    )

    # Clusters
    cluster_scores: dict[str, ScoreResult] = {}
    for cid, cdef in cluster_defs.items():
        name = cdef.get("name") or cid
        cluster_scores[name] = score_dictionary(
            name,
            bags,
            _merge_phrases(cdef),
            scale=2.0,
            cap=psych_cap,
            cap_rule=cap_rule,
            de_lexicon=de_lexicon,
            allow_awards_on_de=False,
            de_corroborator=True,
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

    # Theme-available lift on schedule priority (more episode angles)
    if primary and not str(primary).startswith("Underspecified"):
        op = scores.get("Overall_Priority_for_Podcast")
        if op:
            op.score = min(10.0, op.score + 0.35)
            op.raw_hits = op.score

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

    overviews = profile.get("overviews") or {}
    plot_en = plots.get("en") or pick("plot_en") or overviews.get("en") or pick("overview_en")
    plot_ru = plots.get("ru") or pick("plot_ru") or overviews.get("ru") or pick("overview_ru")
    overview_en = overviews.get("en") or pick("overview_en") or plot_en
    overview_ru = overviews.get("ru") or pick("overview_ru") or plot_ru
    awards_text = profile.get("awards_text") or pick("awards_text")
    # Also pull awards from bags if top-level missing
    if not awards_text:
        award_bits = [
            str(b.get("text"))
            for b in bags
            if str(b.get("name") or "").startswith("awards") and b.get("text")
        ]
        if award_bits:
            awards_text = " | ".join(award_bits)

    keywords_txt = (
        "; ".join(profile.get("keywords") or [])
        if isinstance(profile.get("keywords"), list)
        else pick("keywords")
    )

    # Full source-text inventory for human verification of scores (kept on every row)
    bag_lines: list[str] = []
    plot_en_parts: list[str] = []
    plot_ru_parts: list[str] = []
    plot_de_parts: list[str] = []
    for b in bags:
        name = b.get("name") or "?"
        src = b.get("source") or "?"
        lang = b.get("language") or "?"
        text = (b.get("text") or "").strip()
        if not text:
            continue
        bag_lines.append(f"{name}@{src}/{lang} ({len(text)}c)")
        if name == "plot_en" and text not in plot_en_parts:
            plot_en_parts.append(text)
        if name == "plot_ru" and text not in plot_ru_parts:
            plot_ru_parts.append(text)
        if name == "plot_de" and text not in plot_de_parts:
            plot_de_parts.append(text)

    # Prefer concatenated multi-source plots when available (TMDB+KP+Wiki)
    if plot_en_parts:
        plot_en = "\n---\n".join(plot_en_parts)
    if plot_ru_parts:
        plot_ru = "\n---\n".join(plot_ru_parts)
    plot_de = "\n---\n".join(plot_de_parts) if plot_de_parts else (
        (plots.get("de") if isinstance(plots, dict) else None) or profile.get("plot_de")
    )

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
        # --- SOURCE TEXT (reference — do not drop; used to verify judgments) ---
        "plot_en": plot_en,
        "plot_ru": plot_ru,
        "plot_de": plot_de,
        "overview_en": overview_en,
        "overview_ru": overview_ru,
        "overview_de": overviews.get("de") or pick("overview_de") or plot_de,
        "awards_text": awards_text,
        "keywords": keywords_txt,
        "genres_en": "; ".join(genres.get("en") or []) if isinstance(genres.get("en"), list) else pick("genres_en"),
        "genres_ru": "; ".join(genres.get("ru") or []) if isinstance(genres.get("ru"), list) else pick("genres_ru"),
        "bag_inventory": "; ".join(bag_lines),
        "bags_n": len(bags),
        "has_plot_en": bool(plot_en),
        "has_plot_ru": bool(plot_ru),
        "has_plot_de": bool(plot_de),
        "has_awards_text": bool(awards_text),
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
        "link_wikipedia_de": links.get("wikipedia_de") or pick("link_wikipedia_de"),
        "primary_theme": primary,
        "secondary_theme": secondary,
        "theme_confidence": theme_confidence,
    }
    for n, s in scores.items():
        flat[f"score_{n}"] = round(s.score, 2)
        flat[f"conf_{n}"] = s.confidence
        # Compact phrase evidence for that score (verify judgments without leaving the row)
        if s.evidence:
            flat[f"evidence_{n}"] = _format_evidence_ref(s.evidence, limit=8)
        # Human-readable argumentation for this vector
        flat[f"argumentation_{n}"] = _argumentation_for_score(
            n, s, kind="score", primary=primary, secondary=secondary
        )
    for n, s in cluster_scores.items():
        flat[f"cluster_score_{n}"] = round(s.score, 2)
        if s.evidence:
            flat[f"cluster_evidence_{n}"] = _format_evidence_ref(s.evidence, limit=5)
        flat[f"argumentation_cluster_{n}"] = _argumentation_for_score(
            n, s, kind="cluster", primary=primary, secondary=secondary
        )

    # Theme classification argumentation
    flat["argumentation_primary_theme"] = _argumentation_for_theme(
        primary, secondary, theme_confidence, cluster_scores, scores
    )

    # Roll-up of strongest evidence across key judgment scores
    flat["evidence_top"] = _format_top_evidence(scores, cluster_scores, limit=20)
    flat["argumentation_summary"] = _argumentation_summary(scores, cluster_scores, primary, secondary)
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
        # Keep raw source texts also at result root for non-flat consumers
        "source_text": {
            "plot_en": plot_en,
            "plot_ru": plot_ru,
            "plot_de": plot_de,
            "overview_en": overview_en,
            "overview_ru": overview_ru,
            "overview_de": overviews.get("de") or plot_de,
            "has_plot_de": bool(plot_de),
            "awards_text": awards_text,
            "keywords": keywords_txt,
            "bag_inventory": flat.get("bag_inventory"),
        },
    }


def _format_evidence_ref(evidence: list, *, limit: int = 8) -> str:
    """'phrase (T2, bag@source/lang ×count)' joined for Excel verification."""
    parts: list[str] = []
    for e in evidence[:limit]:
        parts.append(
            f"{e.phrase} (T{e.tier}, {e.bag}@{e.source}/{e.language}"
            f"{' ×' + str(e.count) if e.count > 1 else ''})"
        )
    return " | ".join(parts)


def _format_top_evidence(scores: dict, clusters: dict, *, limit: int = 20) -> str:
    """Strongest hits across scores/clusters for a single verification cell."""
    items: list[tuple[float, str]] = []
    for name, s in list(scores.items()) + list(clusters.items()):
        for e in s.evidence[:6]:
            items.append(
                (
                    e.weight,
                    f"[{name}] {e.phrase} (T{e.tier}, {e.bag}@{e.source}/{e.language})",
                )
            )
    items.sort(key=lambda x: -x[0])
    seen: set[str] = set()
    out: list[str] = []
    for _, label in items:
        if label in seen:
            continue
        seen.add(label)
        out.append(label)
        if len(out) >= limit:
            break
    return " || ".join(out)


# Short readable labels for composite / hybrid vectors
_VECTOR_METHOD = {
    "Podcast_Priority": (
        "composite: 30% Psychological_Depth + 20% Discussability + 15% Symbolism_Ambiguity "
        "+ 15% Narrative_Craft + 12% Existential_Weight + 8% Spiritual_Religious_Mystical_Depth"
    ),
    "Overall_Priority_for_Podcast": (
        "schedule rank (≠ Podcast_Priority): 32% Podcast_Priority + 28% Modern_Viewer_Deliverability "
        "+ 15% Awards_Prestige + 15% Interesting_to_Watch + 10% Discussability "
        "(+0.35 if primary theme is specified)"
    ),
    "Modern_Viewer_Deliverability": (
        "hybrid for today's viewer: engagement (Interesting + Easy + pace phrases + ratings) "
        "balanced with content floor (Depth + Narrative + Discuss); "
        "penalties for drag/slow/boring language, long runtime, dense-hard-low-pace; "
        "spectacle without substance capped — content and deliverability must come together"
    ),
    "Awards_Prestige": (
        "hybrid: structured awards_text ladder (Oscar/Cannes/BAFTA/Оскар/Ника/…) "
        "blended with phrase hits on awards bags"
    ),
    "Easy_to_Watch": (
        "rule hybrid: base 6.5; +spectacle/animation; −long runtime / high psych density / dense craft"
    ),
    "Interesting_to_Watch_Engagement": (
        "rule hybrid: average ratings (IMDb/KP/TMDB) scaled + 0.35×Discussability"
    ),
    "Human_Nature_Spectrum": (
        "spectrum 0=biological/machine … 10=spiritual; from machine_nature vs spiritual_nature phrases"
    ),
}


def _level_word(score: float) -> str:
    if score >= 8.0:
        return "very high"
    if score >= 6.0:
        return "high"
    if score >= 4.0:
        return "moderate"
    if score >= 2.0:
        return "low–moderate"
    if score > 0:
        return "low"
    return "none / not supported"


def _argumentation_for_score(
    name: str,
    result: ScoreResult,
    *,
    kind: str = "score",
    primary: Optional[str] = None,
    secondary: Optional[str] = None,
) -> str:
    """
    Human-readable argumentation for one vector — why this classification/score.
    Written for Excel review (not just raw phrase dumps).
    """
    score = float(result.score or 0)
    conf = result.confidence or "Low"
    level = _level_word(score)
    parts: list[str] = []

    # Headline
    label = name.replace("_", " ")
    if kind == "cluster":
        parts.append(
            f"Cluster «{label}» scored {score:.1f}/10 ({level}), confidence={conf}."
        )
        if primary and name == primary:
            parts.append("Selected as PRIMARY theme (highest strong cluster with enough psych density).")
        elif secondary and name == secondary:
            parts.append("Selected as SECONDARY theme (meets secondary threshold vs primary).")
        elif str(primary or "").startswith("Underspecified") and score > 0:
            parts.append(
                "Not promoted to primary: overall psych density too low or evidence too thin "
                "for a confident taxonomy assignment."
            )
    else:
        parts.append(f"Vector «{label}» = {score:.1f}/10 ({level}), confidence={conf}.")

    # Method
    if name in _VECTOR_METHOD:
        parts.append(f"Method: {_VECTOR_METHOD[name]}.")
    elif kind == "cluster":
        parts.append(
            "Method: weighted phrase tiers T3/T2/T1 over evidence bags "
            "(plots EN/RU/DE; German Wikipedia is corroboration only); "
            "T1-only hits are discounted."
        )
    else:
        parts.append(
            "Method: dictionary phrase matching (T3 strong multi-word, T2 medium, T1 weak) "
            "across EN+RU+DE evidence bags (plot_en, plot_ru/Kinopoisk, plot_de/Wikipedia-DE, keywords, genres, awards)."
        )

    # Evidence story
    ev = list(result.evidence or [])
    if not ev and score <= 0:
        parts.append(
            "Argument: no matching dictionary phrases found in available source texts "
            "for this vector → score remains 0 (insufficient textual support)."
        )
    elif not ev and score > 0:
        parts.append(
            "Argument: score comes from a rule/composite formula rather than direct phrase hits "
            f"(raw_hits={result.raw_hits:.2f})."
        )
    else:
        # language mix
        langs = sorted({(e.language or "?") for e in ev})
        bags = sorted({(e.bag or "?") for e in ev})
        sources = sorted({(e.source or "?") for e in ev})
        t3 = [e for e in ev if e.tier >= 3]
        t2 = [e for e in ev if e.tier == 2]
        t1 = [e for e in ev if e.tier == 1]
        parts.append(
            f"Evidence: {len(ev)} phrase hit(s) "
            f"(T3={len(t3)}, T2={len(t2)}, T1={len(t1)}); "
            f"languages={','.join(langs)}; bags={','.join(bags[:6])}; "
            f"sources={','.join(sources[:6])}."
        )
        # top phrases with where found
        top = sorted(ev, key=lambda e: -e.weight)[:5]
        cite = "; ".join(
            f"«{e.phrase}» in {e.bag}@{e.source}/{e.language} (T{e.tier})"
            for e in top
        )
        parts.append(f"Key citations: {cite}.")
        if any((e.language or "") == "ru" for e in ev):
            parts.append("Russian source text (e.g. Kinopoisk/TMDB-RU/Wikipedia-RU) contributed to this judgment.")
        de_psych = [e for e in ev if is_real_de_psych_hit(e)]
        de_psych_strong = [e for e in de_psych if e.tier >= 2]
        # Trilingual needs a real German stem (any tier) or a T2+ DE-lexicon hit.
        de_for_trilingual = de_psych_strong or de_psych
        if de_for_trilingual:
            parts.append(
                "German Wikipedia contributed a real DE-lexicon stem "
                f"(«{de_for_trilingual[0].phrase}») — corroboration, not a solo ranking driver."
            )
        langs_hit = {(e.language or "") for e in ev}
        if {"en", "ru"} <= langs_hit and de_for_trilingual:
            parts.append("EN, RU and a German-lexicon DE hit → trilingual support.")
        elif {"en", "ru"} <= langs_hit:
            parts.append("EN and RU bags both fired → cross-language support.")
        elif de_for_trilingual and ({"en", "de"} <= langs_hit or {"ru", "de"} <= langs_hit):
            parts.append("EN/RU plus a German-lexicon DE hit → bilingual corroboration.")

    # Caps / negatives
    if result.negatives:
        for neg in result.negatives:
            rule = neg.get("rule") or "cap"
            cap = neg.get("cap")
            parts.append(
                f"Constraint applied: {rule}"
                + (f" capped score at {cap}." if cap is not None else ".")
            )

    # Level interpretation
    if score >= 7:
        parts.append("Interpretation: strong textual support — safe to treat as a major signal for this film.")
    elif score >= 4:
        parts.append("Interpretation: meaningful but not dominant signal; use as supporting angle.")
    elif score > 0:
        parts.append("Interpretation: weak/sparse signal; treat cautiously (may be genre noise).")
    else:
        parts.append("Interpretation: vector not evidenced in current source bags.")

    return " ".join(parts)


def _argumentation_for_theme(
    primary: Optional[str],
    secondary: Optional[str],
    theme_confidence: str,
    cluster_scores: dict,
    scores: dict,
) -> str:
    psych = scores.get("Psychological_Depth")
    trauma = scores.get("Trauma_Clinical_Relevance")
    madness = scores.get("Madness_Altered_States")
    pd = psych.score if psych else 0.0
    parts = [
        f"Primary theme = «{primary or '—'}» (confidence={theme_confidence}).",
        f"Secondary theme = «{secondary or 'none'}».",
        "Rule: primary requires psych density gate "
        f"(Psychological_Depth≥2.5 or Trauma≥4 or Madness≥4); "
        f"here Depth={pd:.1f}, Trauma={(trauma.score if trauma else 0):.1f}, "
        f"Madness={(madness.score if madness else 0):.1f}.",
    ]
    if str(primary or "").startswith("Underspecified"):
        parts.append(
            "Why Underspecified: either psych gate failed or no cluster reached primary_min "
            "with ≥2 evidence hits / a T3 phrase — spectacle/low-density titles stay unspecified."
        )
    else:
        top = sorted(
            ((n, s.score, s) for n, s in cluster_scores.items()),
            key=lambda x: -x[1],
        )[:3]
        ranking = ", ".join(f"{n}={sc:.1f}" for n, sc, _ in top)
        parts.append(f"Top cluster ranking: {ranking}.")
        if primary in cluster_scores:
            parts.append(
                f"Primary argumentation: { _argumentation_for_score(primary, cluster_scores[primary], kind='cluster', primary=primary, secondary=secondary) }"
            )
    return " ".join(parts)


def _argumentation_summary(
    scores: dict,
    clusters: dict,
    primary: Optional[str],
    secondary: Optional[str],
) -> str:
    """One-cell roll-up of main classification decisions."""
    def g(n: str) -> float:
        s = scores.get(n)
        return float(s.score) if s else 0.0

    parts = [
        f"Theme: {primary or '—'} / {secondary or '—'}.",
        f"Podcast_Priority={g('Podcast_Priority'):.1f} (psych worth) vs "
        f"Overall_Priority={g('Overall_Priority_for_Podcast'):.1f} "
        f"(schedule: psych+deliverability+awards).",
        f"Deliverability={g('Modern_Viewer_Deliverability'):.1f} "
        f"(modern pace/interest + content floor).",
        f"Depth={g('Psychological_Depth'):.1f}, Symbol={g('Symbolism_Ambiguity'):.1f}, "
        f"Discuss={g('Discussability_Podcast_Potential'):.1f}, Awards={g('Awards_Prestige'):.1f}.",
    ]
    # top 3 score argument headlines
    ranked = sorted(
        ((n, s) for n, s in scores.items() if n not in {
            "Podcast_Priority", "Overall_Priority_for_Podcast", "Easy_to_Watch",
            "Interesting_to_Watch_Engagement", "Human_Nature_Spectrum",
        }),
        key=lambda x: -x[1].score,
    )[:3]
    if ranked:
        bits = []
        for n, s in ranked:
            if s.score <= 0:
                continue
            if s.evidence:
                top = max(s.evidence, key=lambda e: e.weight)
                bits.append(f"{n}↑ because «{top.phrase}» ({top.bag}@{top.source})")
            else:
                bits.append(f"{n}={s.score:.1f} (formula/rule)")
        if bits:
            parts.append("Drivers: " + "; ".join(bits) + ".")
    return " ".join(parts)


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


def score_profiles(
    profiles: list[dict],
    dicts: Optional[dict] = None,
    *,
    live_export: bool = False,
    output_dir: str | Path = "output",
    resume: bool = True,
    excel_every: int = 25,
    show_progress: bool = True,
) -> list[dict]:
    """
    Score profiles with optional live export (text/csv every film, Excel every N).

    When live_export=True:
      - appends each result to output/score_checkpoint.jsonl (resume-safe)
      - appends to output/score_live.txt and output/score_live.csv after each film
      - rewrites output/score_live.xlsx every excel_every films + at the end
    """
    import json
    import logging

    from tqdm import tqdm

    from psychofilm_analyzer.scoring.export_v3 import (
        append_score_live_csv,
        append_score_live_text,
        bootstrap_score_live_from_checkpoint,
        score_resume_key,
        write_score_live_excel,
    )

    logger = logging.getLogger(__name__)
    dicts = dicts or load_dictionaries_v3()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not live_export:
        iterator = profiles
        if show_progress and len(profiles) > 20:
            iterator = tqdm(profiles, desc="Score-v3", unit="film")
        return [score_profile(p, dicts) for p in iterator]

    ckpt = output_dir / "score_checkpoint.jsonl"
    live_txt = output_dir / "score_live.txt"
    live_csv = output_dir / "score_live.csv"
    live_xlsx = output_dir / "score_live.xlsx"
    progress_path = output_dir / "score_progress.json"

    done_keys: set[str] = set()
    results: list[dict] = []

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
                    row.pop("_resume_key", None)
                    results.append(row)
            logger.info("Score-v3 resume: %s already scored", len(done_keys))
            if done_keys:
                bootstrap_score_live_from_checkpoint(
                    ckpt,
                    text_path=live_txt,
                    csv_path=live_csv,
                    excel_path=live_xlsx,
                    rebuild_excel=True,
                )
        except OSError as exc:
            logger.warning("Could not read score checkpoint: %s", exc)
            done_keys = set()
            results = []
    else:
        from datetime import datetime, timezone

        live_txt.write_text(
            "PsychoFilm score-v3 live report\n"
            f"started: {now_str()}\n\n",
            encoding="utf-8",
        )
        if live_csv.exists():
            live_csv.unlink()

    # Map resume keys for already-scored so we keep catalog order of input profiles
    results_by_key = {}
    if resume and ckpt.exists():
        with ckpt.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                k = row.get("_resume_key")
                if k:
                    results_by_key[str(k)] = {kk: vv for kk, vv in row.items() if kk != "_resume_key"}

    ordered_results: list[dict] = []
    processed_new = 0
    iterator = profiles
    if show_progress:
        iterator = tqdm(profiles, desc="Score-v3", unit="film")

    for profile in iterator:
        key = score_resume_key(profile)
        if key in done_keys and key in results_by_key:
            ordered_results.append(results_by_key[key])
            continue

        result = score_profile(profile, dicts)
        ordered_results.append(result)
        done_keys.add(key)
        processed_new += 1
        total_done = len(done_keys)

        try:
            payload = dict(result)
            payload["_resume_key"] = key
            with ckpt.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
            append_score_live_text(result, live_txt, index=total_done)
            append_score_live_csv(result, live_csv, index=total_done)
            if processed_new == 1 or (excel_every and total_done % excel_every == 0):
                # Live excel from ordered results so far + remaining done from checkpoint
                write_score_live_excel(ordered_results, live_xlsx, include_evidence=False)
                logger.info("Live score Excel updated (%s films) → %s", total_done, live_xlsx)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Live score export failed: %s", exc)

        if processed_new % 50 == 0:
            progress_path.write_text(
                json.dumps(
                    {
                        "total_input": len(profiles),
                        "done": len(done_keys),
                        "new_this_run": processed_new,
                        "remaining": max(0, len(profiles) - len(done_keys)),
                        "pct": round(100.0 * len(done_keys) / max(1, len(profiles)), 2),
                        "last_title": result.get("title_en"),
                        "finished": False,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

    if processed_new:
        try:
            write_score_live_excel(ordered_results, live_xlsx, include_evidence=False)
            logger.info("Final live score Excel (%s films) → %s", len(ordered_results), live_xlsx)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Final live score Excel failed: %s", exc)

    progress_path.write_text(
        json.dumps(
            {
                "total_input": len(profiles),
                "done": len(ordered_results),
                "new_this_run": processed_new,
                "remaining": 0,
                "pct": 100.0 if ordered_results else 0.0,
                "finished": True,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    logger.info(
        "Score-v3 finished: %s new, %s total | live: %s | %s | %s",
        processed_new,
        len(ordered_results),
        live_txt,
        live_csv,
        live_xlsx,
    )
    return ordered_results

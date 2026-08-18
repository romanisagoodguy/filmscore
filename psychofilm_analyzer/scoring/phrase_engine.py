"""Weighted phrase matching over evidence bags (v3 scoring core)."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional


@dataclass
class PhraseHit:
    phrase: str
    bag: str
    source: str
    language: str
    tier: int
    weight: float
    count: int
    lexicon: str = ""  # "de" when the phrase is from the German pack

    def to_dict(self) -> dict[str, Any]:
        return {
            "phrase": self.phrase,
            "bag": self.bag,
            "source": self.source,
            "language": self.language,
            "tier": self.tier,
            "weight": round(self.weight, 3),
            "count": self.count,
            "lexicon": self.lexicon,
        }


@dataclass
class ScoreResult:
    name: str
    score: float
    confidence: str
    evidence: list[PhraseHit] = field(default_factory=list)
    negatives: list[dict[str, Any]] = field(default_factory=list)
    raw_hits: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 2),
            "confidence": self.confidence,
            "raw_hits": round(self.raw_hits, 3),
            "evidence": [e.to_dict() for e in self.evidence[:25]],
            "negatives": self.negatives,
        }


TIER_WEIGHT = {1: 1.0, 2: 2.0, 3: 3.0}

_CYR_RE = re.compile(r"[а-яё]", re.I)
_DE_CHAR_RE = re.compile(r"[äöüß]")


def _phrase_pattern(phrase: str) -> tuple[str, bool]:
    """Return (regex, is_cyrillic_stem).

    Cyrillic stems stay substring (сем/миф). Latin always uses word
    boundaries so 'war'/'son' cannot fire inside German words.
    """
    has_cyr = bool(_CYR_RE.search(phrase))
    if has_cyr:
        return re.escape(phrase), True
    return rf"(?<!\w){re.escape(phrase)}(?!\w)", False


# Award/festival tokens that may appear on a DE wiki lead but are not psych evidence.
AWARD_TOKENS_ON_DE = {
    "oscar",
    "oscars",
    "academy award",
    "academy awards",
    "bafta",
    "emmy",
    "cannes",
    "berlinale",
    "venice",
    "cesar",
    "golden globe",
    "golden bear",
    "goldener bär",
    "goldener baer",
    "goldener löwe",
    "goldener loewe",
    "goldene palme",
    "palme d'or",
}

# Genre/loanwords that fire on encyclopedia leads and must not drive psych scores.
GENRE_LOANWORDS_ON_DE = {
    "science",
    "fantasy",
    "mystery",
    "thriller",
    "cinematic",
    "revolution",
    "animation",
    "comedy",
    "drama",
    "action",
    "horror",
    "adventure",
    "romance",
    "documentary",
    "western",
    "musical",
}

# High-frequency DE-wiki lead words. Real German, but not psych evidence.
DE_ENCYCLOPEDIA_GENERIC = {
    "vergangenheit",
    "geschichte",
    "regisseur",
    "drehbuch",
    "drehbuchautor",
    "darsteller",
    "hauptdarsteller",
    "premiere",
    "aufführung",
    "auffuehrung",
    "kinostart",
    "filmkritik",
    "filmpreis",
    "auszeichnung",
}


def de_match_denylist() -> set[str]:
    return set(GENRE_LOANWORDS_ON_DE) | set(DE_ENCYCLOPEDIA_GENERIC)


def _is_de_bag(bag: dict[str, Any]) -> bool:
    lang = str(bag.get("language") or "").lower()
    name = str(bag.get("name") or "").lower()
    return lang == "de" or name == "plot_de"


def _skip_phrase_on_bag(
    phrase: str,
    *,
    bag: dict[str, Any],
    is_cyr: bool,
    de_lexicon: set[str],
    allow_awards_on_de: bool,
) -> bool:
    """DE wiki leads only match the German lexicon (or awards, if allowed)."""
    if not _is_de_bag(bag):
        return False
    if is_cyr:
        return True
    if phrase in GENRE_LOANWORDS_ON_DE or phrase in DE_ENCYCLOPEDIA_GENERIC:
        return True
    if phrase in AWARD_TOKENS_ON_DE:
        return not allow_awards_on_de
    if phrase in de_lexicon or _DE_CHAR_RE.search(phrase):
        return False
    return True


def is_real_de_psych_hit(hit: PhraseHit) -> bool:
    """True when a DE-bag hit is a German stem / DE-lexicon phrase, not a loanword."""
    if not _is_de_bag({"language": hit.language, "name": hit.bag}):
        return False
    phrase = (hit.phrase or "").lower()
    if phrase in GENRE_LOANWORDS_ON_DE or phrase in DE_ENCYCLOPEDIA_GENERIC:
        return False
    if phrase in AWARD_TOKENS_ON_DE:
        return False
    if getattr(hit, "lexicon", "") == "de":
        return True
    if _DE_CHAR_RE.search(hit.phrase or ""):
        return True
    return False


def _normalize_tiers(phrases: Any) -> list[tuple[str, int]]:
    """Accept list[str] or {t1:[], t2:[], t3:[]}."""
    out: list[tuple[str, int]] = []
    if not phrases:
        return out
    if isinstance(phrases, dict):
        for key, tier in (("t3", 3), ("t2", 2), ("t1", 1), ("T3", 3), ("T2", 2), ("T1", 1)):
            for p in phrases.get(key) or []:
                p = str(p).strip()
                if len(p) >= 3:
                    out.append((p.lower(), tier))
        return out
    for p in phrases:
        p = str(p).strip()
        if len(p) < 3:
            continue
        # multi-word or long stem => T2 default; very long multi-word => T3
        words = p.split()
        if len(words) >= 2 and len(p) >= 12:
            tier = 3
        elif len(words) >= 2 or len(p) >= 8:
            tier = 2
        else:
            tier = 1
        out.append((p.lower(), tier))
    return out


def match_phrases(
    bags: Iterable[dict[str, Any]],
    phrases: Any,
    *,
    max_count_per_bag: int = 2,
    allow_t1_alone: bool = False,
    de_lexicon: Optional[set[str]] = None,
    allow_awards_on_de: bool = False,
    de_corroborator: bool = False,
) -> tuple[float, list[PhraseHit]]:
    """
    bags: list of {name, text, source, language, weight}
    Returns (raw_hit_score, evidence hits).
    """
    phrase_list = _normalize_tiers(phrases)
    if not phrase_list:
        return 0.0, []

    de_lex = {p.lower() for p in (de_lexicon or set())}
    bag_list = list(bags)
    hits: list[PhraseHit] = []
    raw = 0.0
    t2_t3_present = False

    for phrase, tier in phrase_list:
        # Allow 3-char stems (important for Russian roots: сем, миф, бог, …)
        # Skip only very short noise (1–2 chars).
        if len(phrase) < 3:
            continue
        pat, is_cyr = _phrase_pattern(phrase)
        is_de_lex = phrase in de_lex or bool(_DE_CHAR_RE.search(phrase))
        for bag in bag_list:
            if _skip_phrase_on_bag(
                phrase,
                bag=bag,
                is_cyr=is_cyr,
                de_lexicon=de_lex,
                allow_awards_on_de=allow_awards_on_de,
            ):
                continue
            text = (bag.get("text") or "").lower()
            if not text:
                continue
            found = len(re.findall(pat, text, flags=re.IGNORECASE))
            if not found:
                continue
            count = min(found, max_count_per_bag)
            bag_w = float(bag.get("weight") or 1.0)
            contrib = TIER_WEIGHT.get(tier, 1.0) * bag_w * count
            if tier >= 2:
                t2_t3_present = True
            hits.append(
                PhraseHit(
                    phrase=phrase,
                    bag=str(bag.get("name") or ""),
                    source=str(bag.get("source") or ""),
                    language=str(bag.get("language") or ""),
                    tier=tier,
                    weight=contrib,
                    count=count,
                    lexicon="de" if is_de_lex else "",
                )
            )
            raw += contrib

    # T1-only evidence is weak: discount unless allow_t1_alone or T2/T3 also hit
    if hits and not t2_t3_present and not allow_t1_alone:
        raw *= 0.35

    # DE wiki is a corroborator: it cannot set a psych/cluster score by itself.
    if de_corroborator and hits:
        de_hits = [h for h in hits if _is_de_bag({"language": h.language, "name": h.bag})]
        other_t2 = [h for h in hits if h not in de_hits and h.tier >= 2]
        if de_hits and not other_t2:
            raw -= sum(h.weight for h in de_hits)
            raw = max(0.0, raw)

    # dedupe evidence by phrase+bag (keep max weight)
    best: dict[tuple[str, str], PhraseHit] = {}
    for h in hits:
        key = (h.phrase, h.bag)
        if key not in best or h.weight > best[key].weight:
            best[key] = h
    evidence = sorted(best.values(), key=lambda x: -x.weight)
    return raw, evidence


def hits_to_score(raw: float, *, scale: float = 2.2, ceiling: float = 10.0) -> float:
    """Map raw weighted hits to 0–10 with diminishing returns."""
    if raw <= 0:
        return 0.0
    # log1p curve: 3 raw ~ 4.5, 8 raw ~ 7, 15+ ~ 9+
    score = ceiling * (1.0 - math.exp(-raw / scale))
    return max(0.0, min(ceiling, score))


def confidence_from_evidence(evidence: list[PhraseHit], raw: float) -> str:
    if raw <= 0 or not evidence:
        return "Low"
    n = len(evidence)
    has_t3 = any(e.tier >= 3 for e in evidence)
    bags = {e.bag for e in evidence}
    if (has_t3 and n >= 2) or (n >= 4 and len(bags) >= 2) or raw >= 8:
        return "High"
    if n >= 2 or raw >= 3:
        return "Medium"
    return "Low"


def score_dictionary(
    name: str,
    bags: Iterable[dict[str, Any]],
    phrases: Any,
    *,
    scale: float = 2.2,
    allow_t1_alone: bool = False,
    cap: Optional[float] = None,
    cap_rule: Optional[str] = None,
    de_lexicon: Optional[set[str]] = None,
    allow_awards_on_de: bool = False,
    de_corroborator: bool = True,
) -> ScoreResult:
    raw, evidence = match_phrases(
        bags,
        phrases,
        allow_t1_alone=allow_t1_alone,
        de_lexicon=de_lexicon,
        allow_awards_on_de=allow_awards_on_de,
        de_corroborator=de_corroborator,
    )
    score = hits_to_score(raw, scale=scale)
    negatives: list[dict[str, Any]] = []
    if de_corroborator:
        de_hits = [e for e in evidence if _is_de_bag({"language": e.language, "name": e.bag})]
        other_t2 = [e for e in evidence if e not in de_hits and e.tier >= 2]
        if de_hits and not other_t2:
            negatives.append({"rule": "de_wiki_corroborator_only"})
    if cap is not None and score > cap:
        score = cap
        negatives.append({"rule": cap_rule or "cap", "cap": cap})
    conf = confidence_from_evidence(evidence, raw)
    if score < 1.5:
        conf = "Low"
    return ScoreResult(name=name, score=score, confidence=conf, evidence=evidence, negatives=negatives, raw_hits=raw)


def spectrum_score(
    bags: Iterable[dict[str, Any]],
    machine_phrases: Any,
    spiritual_phrases: Any,
    *,
    de_lexicon: Optional[set[str]] = None,
) -> ScoreResult:
    """0 = biological machine, 10 = spiritual being; 5 = neutral/ambiguous."""
    raw_m, ev_m = match_phrases(
        bags,
        machine_phrases,
        allow_t1_alone=True,
        de_lexicon=de_lexicon,
        de_corroborator=True,
    )
    raw_s, ev_s = match_phrases(
        bags,
        spiritual_phrases,
        allow_t1_alone=True,
        de_lexicon=de_lexicon,
        de_corroborator=True,
    )
    if raw_m <= 0 and raw_s <= 0:
        return ScoreResult(
            name="Human_Nature_Spectrum",
            score=5.0,
            confidence="Low",
            evidence=[],
            negatives=[{"rule": "default_midpoint", "value": 5.0}],
            raw_hits=0.0,
        )
    # map difference to 0–10 around 5
    total = raw_m + raw_s + 1e-6
    spiritual_ratio = raw_s / total
    score = spiritual_ratio * 10.0
    evidence = sorted(ev_s + ev_m, key=lambda x: -x.weight)[:20]
    conf = confidence_from_evidence(evidence, raw_m + raw_s)
    return ScoreResult(
        name="Human_Nature_Spectrum",
        score=max(0.0, min(10.0, score)),
        confidence=conf,
        evidence=evidence,
        raw_hits=raw_m + raw_s,
    )


def list_matched_labels(
    bags: Iterable[dict[str, Any]],
    label_map: dict[str, Any],
    *,
    min_raw: float = 1.5,
) -> list[str]:
    """label_map: {label: phrases} -> labels that match above threshold."""
    found: list[str] = []
    for label, phrases in (label_map or {}).items():
        raw, _ = match_phrases(bags, phrases, allow_t1_alone=True)
        if raw >= min_raw:
            found.append(label)
    return found

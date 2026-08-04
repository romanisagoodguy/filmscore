"""Text helpers."""

from __future__ import annotations

import re
from typing import Optional


_SEASON_PATTERNS = [
    re.compile(r"\b[Ss](?:eason)?\s*(\d{1,2})\b"),
    re.compile(r"\bсезон\s*(\d{1,2})\b", re.IGNORECASE),
    re.compile(r"\bс\.?\s*(\d{1,2})\b", re.IGNORECASE),
    re.compile(r"\bS(\d{1,2})\b"),
]


def normalize_title(title: str) -> str:
    if not title:
        return ""
    t = str(title).strip()
    t = re.sub(r"\s+", " ", t)
    # strip trailing bracket notes like [Something]
    t = re.sub(r"\s*\[[^\]]*\]\s*$", "", t).strip()
    return t


def parse_season_from_title(title: str) -> tuple[str, Optional[int]]:
    """Return (base_title, season_number or None)."""
    if not title:
        return "", None
    season: Optional[int] = None
    cleaned = title
    for pat in _SEASON_PATTERNS:
        m = pat.search(title)
        if m:
            season = int(m.group(1))
            cleaned = pat.sub(" ", title)
            break
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–—.")
    return cleaned or title, season


def safe_float(value, default: Optional[float] = None) -> Optional[float]:
    if value is None:
        return default
    try:
        if isinstance(value, str):
            value = value.replace(",", ".").strip()
            if not value or value.lower() in {"nan", "n/a", "none", "-"}:
                return default
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value, default: Optional[int] = None) -> Optional[int]:
    if value is None:
        return default
    try:
        if isinstance(value, str):
            value = value.strip()
            m = re.search(r"(\d{4})", value)
            if m and len(value) == 4:
                return int(m.group(1))
            # year-like or plain int
            value = re.sub(r"[^\d-]", "", value)
            if not value:
                return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def slugify_key(text: str) -> str:
    text = (text or "").lower().strip()
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    text = re.sub(r"[-\s]+", "_", text)
    return text[:120]


def word_count(text: str) -> int:
    if not text:
        return 0
    return len(re.findall(r"\b\w+\b", text, flags=re.UNICODE))


def truncate_words(text: str, max_words: int) -> str:
    words = re.findall(r"\S+", text or "")
    if len(words) <= max_words:
        return text or ""
    return " ".join(words[:max_words]) + "…"


def combine_text(*parts: Optional[str]) -> str:
    return "\n".join(p.strip() for p in parts if p and str(p).strip())

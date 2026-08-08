"""Configuration and dictionary loading."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "default.yaml"
DEFAULT_DICTS = ROOT / "config" / "dictionaries.yaml"


def deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_yaml(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data


def load_config(config_path: str | Path | None = None) -> dict[str, Any]:
    load_dotenv(ROOT / ".env")
    cfg = load_yaml(DEFAULT_CONFIG)
    if config_path:
        cfg = deep_merge(cfg, load_yaml(config_path))

    # env overrides
    http = cfg.setdefault("http", {})
    if os.getenv("REQUEST_DELAY_SEC"):
        http["delay_sec"] = float(os.getenv("REQUEST_DELAY_SEC", "0.6"))
    if os.getenv("MAX_RETRIES"):
        http["max_retries"] = int(os.getenv("MAX_RETRIES", "3"))

    cfg["api_keys"] = {
        "tmdb": os.getenv("TMDB_API_KEY", "").strip(),
        "omdb": os.getenv("OMDB_API_KEY", "").strip(),
        "kinopoisk": os.getenv("KINOPOISK_API_KEY", "").strip(),
        # Wikimedia OAuth (meta.wikimedia.org) — used for Wikipedia REST/API
        "wikipedia_email": os.getenv(
            "WIKIMEDIA_CONTACT_EMAIL",
            os.getenv("WIKIPEDIA_CONTACT_EMAIL", "romangermanyberlin@gmail.com"),
        ).strip(),
        "wikipedia_client_id": os.getenv(
            "WIKIMEDIA_CLIENT_ID", os.getenv("WIKIPEDIA_CLIENT_ID", "")
        ).strip(),
        "wikipedia_client_secret": os.getenv(
            "WIKIMEDIA_CLIENT_SECRET", os.getenv("WIKIPEDIA_CLIENT_SECRET", "")
        ).strip(),
        "wikipedia_access_token": os.getenv(
            "WIKIMEDIA_ACCESS_TOKEN", os.getenv("WIKIPEDIA_ACCESS_TOKEN", "")
        ).strip(),
    }
    # If OAuth token present, raise default wiki host RPM (highvolume ≈ 5000/hour)
    if cfg["api_keys"].get("wikipedia_access_token"):
        http = cfg.setdefault("http", {})
        rpm = http.setdefault("host_rate_limits_per_min", {})
        # polite default under 5000/h (~83/min): 40/min unless user already set
        if "wikipedia.org" not in rpm or int(rpm.get("wikipedia.org") or 0) <= 3:
            rpm["wikipedia.org"] = int(os.getenv("WIKIPEDIA_RPM", "40"))
        # A2 wiki delay: ~1.5s between calls if still at slow 15s
        a2 = cfg.setdefault("gather_v2", {})
        delays = a2.setdefault("site_delays_sec", {})
        if float(delays.get("wikipedia") or 0) >= 10:
            delays["wikipedia"] = float(os.getenv("WIKIPEDIA_DELAY_SEC", "1.5"))
        wadapt = a2.setdefault("wikipedia_adaptive_rpm", {})
        wadapt.setdefault("min_rpm", 5.0)
        wadapt.setdefault("max_rpm", 200.0)
        wadapt.setdefault("step_rpm", 10.0)
        if float(wadapt.get("initial_rpm") or 0) < 5:
            d0 = float(delays.get("wikipedia", 1.5))
            wadapt["initial_rpm"] = max(5.0, min(40.0, 60.0 / max(d0, 0.3)))
    return cfg


def load_dictionaries(path: str | Path | None = None) -> dict[str, Any]:
    return load_yaml(path or DEFAULT_DICTS)

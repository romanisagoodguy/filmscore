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
    }
    return cfg


def load_dictionaries(path: str | Path | None = None) -> dict[str, Any]:
    return load_yaml(path or DEFAULT_DICTS)

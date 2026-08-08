#!/usr/bin/env python3
"""Deferred Wikipedia enrich pass over gather_checkpoint.jsonl.

Main gather runs with sources.wikipedia: false for speed/reliability.
This script fills missing Wikipedia sources at a polite RPM.

Examples:
  python scripts/wiki_enrich_pass.py --limit 50
  python scripts/wiki_enrich_pass.py --in output/gather_checkpoint.jsonl --out output/gather_checkpoint.jsonl
  python scripts/wiki_enrich_pass.py --only-missing --limit 200
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _profile_needs_wiki(row: dict) -> bool:
    sources = row.get("sources") or {}
    wiki = sources.get("wikipedia") if isinstance(sources, dict) else None
    if isinstance(wiki, dict) and wiki.get("found"):
        return False
    cov = row.get("coverage") or {}
    if cov.get("wikipedia") is True:
        return False
    # also skip if already has solid EN plot bag from wiki
    bags = row.get("evidence_bags") or []
    for b in bags:
        if isinstance(b, dict) and b.get("source") == "wikipedia" and (b.get("text") or "").strip():
            return False
    return True


def _input_from_profile(row: dict):
    from psychofilm_analyzer.models import InputTitle, MediaType

    imported = row.get("imported") or {}
    titles = row.get("titles") or {}
    ids = row.get("ids") or {}
    mt_raw = (row.get("media_type") or "film") or "film"
    try:
        media_type = MediaType(str(mt_raw).lower())
    except ValueError:
        media_type = MediaType.FILM

    title = (
        titles.get("en")
        or imported.get("title")
        or titles.get("ru")
        or titles.get("original")
        or "Unknown"
    )
    return InputTitle(
        title=str(title),
        year=row.get("year") or imported.get("year"),
        english_title=titles.get("en") or None,
        russian_title=titles.get("ru") or None,
        media_type=media_type,
        season=row.get("season"),
        source_file=imported.get("file"),
        source_sheet=imported.get("sheet"),
        source_row=imported.get("row"),
        import_title=imported.get("title"),
        import_year=imported.get("year"),
        imdb_id_hint=ids.get("imdb_id"),
        tmdb_id_hint=ids.get("tmdb_id"),
        kinopoisk_id_hint=ids.get("kinopoisk_id"),
        input_id=ids.get("input_id"),
    )


def _merge_wiki(row: dict, payload) -> dict:
    """Merge Wikipedia SourcePayload into a gather profile dict."""
    from psychofilm_analyzer.enrichment.profile import EvidenceBag

    sources = dict(row.get("sources") or {})
    sources["wikipedia"] = payload.to_dict()
    row["sources"] = sources

    cov = dict(row.get("coverage") or {})
    had_wiki = bool(cov.get("wikipedia"))
    cov["wikipedia"] = bool(payload.found)
    if payload.found and not had_wiki:
        cov["sources_found"] = int(cov.get("sources_found") or 0) + 1
    if payload.found:
        if payload.overview_en or payload.overview:
            cov["has_plot_en"] = True
        if payload.overview_ru:
            cov["has_plot_ru"] = True
    row["coverage"] = cov

    links = dict(row.get("links") or {})
    extra = payload.extra or {}
    by_lang = extra.get("links_by_lang") or {}
    if by_lang.get("en"):
        links["wikipedia_en"] = by_lang["en"]
    if by_lang.get("ru"):
        links["wikipedia_ru"] = by_lang["ru"]
    if by_lang.get("de"):
        links["wikipedia_de"] = by_lang["de"]
    if payload.url and not links.get("wikipedia_en"):
        links["wikipedia_en"] = payload.url
    row["links"] = links

    plots = dict(row.get("plots") or {})
    overs = dict(row.get("overviews") or {})
    if payload.plot_en and not plots.get("en"):
        plots["en"] = payload.plot_en
    if payload.plot_ru and not plots.get("ru"):
        plots["ru"] = payload.plot_ru
    if payload.overview_en and not overs.get("en"):
        overs["en"] = payload.overview_en
    if payload.overview_ru and not overs.get("ru"):
        overs["ru"] = payload.overview_ru
    if payload.overview and not overs.get("en") and (payload.language or "en") == "en":
        overs["en"] = payload.overview
    row["plots"] = plots
    row["overviews"] = overs

    bags = list(row.get("evidence_bags") or [])
    # drop prior wikipedia bags then re-add
    bags = [b for b in bags if not (isinstance(b, dict) and b.get("source") == "wikipedia")]
    for name, text, lang, w in (
        ("plot_en", payload.overview_en or (payload.overview if payload.language == "en" else None), "en", 0.9),
        ("plot_ru", payload.overview_ru, "ru", 0.9),
    ):
        if text and str(text).strip() and len(str(text).strip()) >= 8:
            bags.append(
                EvidenceBag(
                    name=name, text=str(text).strip()[:8000], source="wikipedia", language=lang, weight=w
                ).to_dict()
            )
    if payload.awards_text:
        bags.append(
            EvidenceBag(
                name="awards_en",
                text=str(payload.awards_text)[:4000],
                source="wikipedia",
                language="en",
                weight=0.5,
            ).to_dict()
        )
    row["evidence_bags"] = bags

    # light bag_summary refresh
    summary = dict(row.get("bag_summary") or {})
    summary["wikipedia"] = bool(payload.found)
    row["bag_summary"] = summary
    row["wiki_pass_at"] = datetime.now(timezone.utc).isoformat()
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description="Deferred Wikipedia enrich pass")
    ap.add_argument(
        "--in",
        dest="inp",
        default="output/gather_checkpoint.jsonl",
        help="Input gather checkpoint JSONL",
    )
    ap.add_argument(
        "--out",
        default=None,
        help="Output JSONL (default: overwrite --in after .bak)",
    )
    ap.add_argument("--limit", type=int, default=None, help="Max films to attempt this run")
    ap.add_argument(
        "--only-missing",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Skip profiles that already have Wikipedia (default: true)",
    )
    ap.add_argument("--no-cache", action="store_true", help="Disable disk cache")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("wiki_pass")

    inp = Path(args.inp)
    out = Path(args.out) if args.out else inp
    if not inp.exists():
        print(f"ERROR: missing {inp}", file=sys.stderr)
        return 2

    from psychofilm_analyzer.config import load_config
    from psychofilm_analyzer.data_sources.wikipedia import WikipediaSource
    from psychofilm_analyzer.utils.cache import CacheStore
    from psychofilm_analyzer.utils.http import HttpClient

    cfg = load_config()
    # Force wikipedia settings even if sources.wikipedia is false for gather
    wiki_cfg = cfg.setdefault("wikipedia", {})
    wiki_cfg.setdefault("langs", ["en"])
    wiki_cfg.setdefault("max_candidates", 1)
    wiki_cfg.setdefault("search_fallback", False)
    http_cfg = cfg.get("http") or {}
    # Ensure low Wiki RPM for this pass
    rpm = dict(http_cfg.get("host_rate_limits_per_min") or {})
    rpm["wikipedia.org"] = int(rpm.get("wikipedia.org") or 3)
    if args.no_cache:
        cfg.setdefault("cache", {})["enabled"] = False

    cache = CacheStore(
        cache_dir=(cfg.get("cache") or {}).get("dir", "cache"),
        ttl_days=int((cfg.get("cache") or {}).get("ttl_days", 30)),
        enabled=bool((cfg.get("cache") or {}).get("enabled", True)),
    )
    http = HttpClient(
        delay_sec=float(http_cfg.get("delay_sec", 0.6)),
        timeout_sec=float(http_cfg.get("timeout_sec", 25)),
        max_retries=int(http_cfg.get("max_retries", 2)),
        user_agent=http_cfg.get("user_agent", "PsychoFilmAnalyzer/1.0"),
        host_rate_limits_per_min=rpm,
    )
    wiki = WikipediaSource(http, cache, cfg)

    lines = [ln for ln in inp.read_text(encoding="utf-8").splitlines() if ln.strip()]
    rows = []
    for ln in lines:
        try:
            rows.append(json.loads(ln))
        except json.JSONDecodeError:
            continue

    todo_idx = []
    for i, row in enumerate(rows):
        if args.only_missing and not _profile_needs_wiki(row):
            continue
        todo_idx.append(i)
    if args.limit is not None:
        todo_idx = todo_idx[: args.limit]

    log.info(
        "Wiki pass: %s profiles, %s need attempt (limit=%s)",
        len(rows),
        len(todo_idx),
        args.limit,
    )
    found_n = 0
    miss_n = 0
    for n, i in enumerate(todo_idx, 1):
        row = rows[i]
        item = _input_from_profile(row)
        try:
            payload = wiki.fetch(item)
        except Exception as exc:  # noqa: BLE001
            log.warning("Wiki fetch failed for %s: %s", item.title, exc)
            from psychofilm_analyzer.models import SourcePayload

            payload = SourcePayload(source="wikipedia", found=False, error=str(exc))
        rows[i] = _merge_wiki(row, payload)
        if payload.found:
            found_n += 1
            log.info("[%s/%s] FOUND %s (%s)", n, len(todo_idx), item.title, item.year)
        else:
            miss_n += 1
            log.info(
                "[%s/%s] MISS %s (%s) — %s",
                n,
                len(todo_idx),
                item.title,
                item.year,
                payload.error or "not found",
            )

    # Atomic write
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.resolve() == inp.resolve() and inp.exists():
        bak = inp.with_suffix(inp.suffix + f".bak_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        shutil.copy2(inp, bak)
        log.info("Backup → %s", bak)

    import os

    fd, tmp_name = tempfile.mkstemp(prefix="wiki_pass_", suffix=".jsonl", dir=str(out.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        Path(tmp_name).replace(out)
    except Exception:
        try:
            os.close(fd)
        except Exception:
            pass
        Path(tmp_name).unlink(missing_ok=True)
        raise

    print(
        f"Done. attempted={len(todo_idx)} found={found_n} miss={miss_n} "
        f"total_profiles={len(rows)} → {out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

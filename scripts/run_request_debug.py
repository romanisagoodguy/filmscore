#!/usr/bin/env python3
"""Run gather on N films with full HTTP reproduction debug log.

Writes output/request_debug_*.txt containing:
  - film specification + Excel sheet/row + live Excel row
  - every HTTP command as copy-paste python/PowerShell
  - status_code / error / duration / cumulative times
  - per-site statistics

Examples:
  python scripts/run_request_debug.py --limit 3 --no-cache
  python scripts/run_request_debug.py --limit 5 --resume
  python scripts/run_request_debug.py --title Busting --year 1974 --no-cache
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# Project root on sys.path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(description="Gather N films with full request debug log")
    ap.add_argument(
        "--input",
        default=None,
        help="Input Excel/CSV (default: from config / first list file)",
    )
    ap.add_argument("--limit", type=int, default=3, help="How many NEW films to gather")
    ap.add_argument(
        "--resume",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Skip titles already in gather_checkpoint.jsonl (default: true)",
    )
    ap.add_argument(
        "--no-cache",
        action="store_true",
        help="Disable disk cache so every source hits the network",
    )
    ap.add_argument(
        "--no-live-export",
        action="store_true",
        help="Do not update gather_live.* / checkpoint (pure network debug)",
    )
    ap.add_argument(
        "--checkpoint",
        default=None,
        help="Gather checkpoint path (default from config)",
    )
    ap.add_argument(
        "--out",
        default=None,
        help="Debug log path (default: output/request_debug_<timestamp>.txt)",
    )
    ap.add_argument("--title", default=None, help="Only this title (substring, case-insensitive)")
    ap.add_argument("--year", type=int, default=None, help="Filter by year")
    ap.add_argument(
        "--include-secrets",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Write full API keys into reproducible commands (default: true)",
    )
    ap.add_argument(
        "--sources",
        default=None,
        help="Comma list to enable only these sources (e.g. wikipedia,kinopoisk)",
    )
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    from psychofilm_analyzer.config import load_config
    from psychofilm_analyzer.io.input_loader import load_titles
    from psychofilm_analyzer.pipeline import Pipeline

    cfg = load_config()
    if args.no_cache:
        cfg.setdefault("cache", {})["enabled"] = False

    if args.sources:
        wanted = {s.strip().lower() for s in args.sources.split(",") if s.strip()}
        src = cfg.setdefault("sources", {})
        for name in list(src.keys()):
            src[name] = name.lower() in wanted

    # Resolve input path
    input_path = args.input
    if not input_path:
        # Prefer common catalog names under project root
        candidates = [
            ROOT / "Список-фильмов v1.xlsx",
            ROOT / "input" / "Список-фильмов v1.xlsx",
            ROOT / "data" / "Список-фильмов v1.xlsx",
        ]
        for c in candidates:
            if c.exists():
                input_path = str(c)
                break
        if not input_path:
            print("ERROR: pass --input path to catalog Excel", file=sys.stderr)
            return 2

    items = load_titles(input_path)
    if args.title:
        t = args.title.lower()
        items = [
            it
            for it in items
            if t in (it.title or "").lower()
            or t in (it.english_title or "").lower()
            or t in (it.import_title or "").lower()
            or t in (it.russian_title or "").lower()
        ]
    if args.year is not None:
        items = [it for it in items if it.year == args.year]

    if not items:
        print("ERROR: no titles matched filters", file=sys.stderr)
        return 2

    # If resume: only take first N not already done (pipeline also skips)
    # Pre-filter so we don't walk 13k items for limit=3 when resume is on.
    if args.resume and args.limit:
        from psychofilm_analyzer.pipeline import Pipeline as _P

        pipe_cfg = cfg.get("pipeline") or {}
        ckpt = Path(
            args.checkpoint
            or pipe_cfg.get("gather_checkpoint", "output/gather_checkpoint.jsonl")
        )
        done: set[str] = set()
        if ckpt.exists():
            import json

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
                        done.add(str(k))
        pending = []
        for it in items:
            key = _P.resume_key(it)
            if key not in done:
                pending.append(it)
            if len(pending) >= args.limit:
                break
        items = pending
    elif args.limit:
        items = items[: args.limit]

    if not items:
        print("ERROR: nothing left to gather (checkpoint may already cover filters)", file=sys.stderr)
        return 2

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = Path(args.out or ROOT / "output" / f"request_debug_{ts}.txt")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Input:     {input_path}")
    print(f"Films:     {len(items)}")
    for i, it in enumerate(items, 1):
        print(
            f"  {i}. {it.title!r} ({it.year}) "
            f"sheet={it.source_sheet!r} excel_row={it.source_row}"
        )
    print(f"Debug log: {out_path}")
    print(f"Cache:     {'OFF' if args.no_cache else 'ON'}")
    print(f"Secrets:   {'FULL in commands' if args.include_secrets else 'redacted'}")

    pipe = Pipeline(config=cfg)
    dbg = pipe.attach_request_debug(
        out_path,
        include_secrets=args.include_secrets,
        write_tables=True,
        excel_every_films=1 if args.limit and args.limit <= 5 else 25,
        meta={
            "script": "scripts/run_request_debug.py",
            "input": str(input_path),
            "limit": args.limit,
            "resume": args.resume,
            "cache_enabled": not args.no_cache,
            "film_count": len(items),
            "titles": "; ".join(f"{it.title} ({it.year})" for it in items),
        },
    )

    live = not args.no_live_export
    profiles = pipe.gather(
        items,
        show_progress=True,
        resume=args.resume,
        checkpoint_path=args.checkpoint,
        live_export=live,
        progress_every=1,
    )

    print(f"\nDone. profiles={len(profiles)}")
    print("Request debug outputs:")
    for k, p in dbg.output_paths().items():
        print(f"  {k}: {Path(p).resolve()}")
    print("Text log + films/requests/sites tables (CSV + Excel).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

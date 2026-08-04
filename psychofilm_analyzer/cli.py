#!/usr/bin/env python3
"""CLI for PsychoFilm Analyzer."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from psychofilm_analyzer import __version__
from psychofilm_analyzer.config import ROOT, load_config, load_dictionaries
from psychofilm_analyzer.enrichment.export import write_profiles
from psychofilm_analyzer.io.input_loader import load_titles
from psychofilm_analyzer.io.output_writer import write_outputs
from psychofilm_analyzer.models import InputTitle, MediaType
from psychofilm_analyzer.pipeline import Pipeline, configure_logging
from psychofilm_analyzer.scoring.export_v3 import write_v3_results
from psychofilm_analyzer.scoring.v3_engine import load_dictionaries_v3, score_profiles
from psychofilm_analyzer.utils.text import safe_int


def _apply_source_flags(config: dict, args: argparse.Namespace) -> None:
    if args.output_dir:
        config.setdefault("output", {})["dir"] = args.output_dir
    if getattr(args, "no_letterboxd", False):
        config.setdefault("sources", {})["letterboxd"] = False
    if getattr(args, "no_wikipedia", False):
        config.setdefault("sources", {})["wikipedia"] = False
    if getattr(args, "offline_hints", False):
        for k in ("tmdb", "omdb", "kinopoisk", "wikipedia", "letterboxd"):
            config.setdefault("sources", {})[k] = False


def _load_eval_set(path: Path | None = None) -> list[InputTitle]:
    path = path or (ROOT / "config" / "eval_set_20.yaml")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    items: list[InputTitle] = []
    for i, row in enumerate(data.get("films") or [], start=1):
        title = row.get("title") or row.get("english_title")
        if not title:
            continue
        year = safe_int(row.get("year"))
        items.append(
            InputTitle(
                title=str(title),
                year=year,
                english_title=row.get("english_title") or str(title),
                russian_title=row.get("russian_title"),
                media_type=MediaType.FILM,
                source_file="eval_set_20.yaml",
                source_sheet="eval",
                source_row=i,
                import_title=str(title),
                import_year=year,
                notes=row.get("tag"),
            )
        )
    return items


def _collect_items(args: argparse.Namespace) -> list[InputTitle]:
    items: list[InputTitle] = []
    if getattr(args, "eval_set", False):
        items.extend(_load_eval_set())
        if args.limit:
            items = items[: args.limit]
        return items
    if args.input:
        items.extend(load_titles(args.input, limit=args.limit))
    if args.title:
        extra = load_titles(titles=args.title)
        if args.input is None and args.limit:
            extra = extra[: args.limit]
        items.extend(extra)
    return items


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="psychofilm",
        description="PsychoFilm Analyzer — multi-source film enrichment & scoring",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command")

    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("-i", "--input", help="Excel/CSV path with film list")
        sp.add_argument("-t", "--title", action="append", default=[], help="Single title (repeatable)")
        sp.add_argument("-o", "--output-dir", default=None, help="Output directory (default: output/)")
        sp.add_argument("-c", "--config", default=None, help="Custom YAML config path")
        sp.add_argument("-n", "--limit", type=int, default=None, help="Process only first N titles")
        sp.add_argument("--eval-set", action="store_true", help="Use config/eval_set_20.yaml diversity set")
        sp.add_argument("--no-letterboxd", action="store_true", help="Disable Letterboxd scraping")
        sp.add_argument("--no-wikipedia", action="store_true", help="Disable Wikipedia")
        sp.add_argument("--offline-hints", action="store_true", help="Disable remote sources")
        sp.add_argument("-v", "--verbose", action="store_true")

    # gather (Phase A)
    g = sub.add_parser("gather", help="Phase A: multi-source enrichment only (no psych scores)")
    add_common(g)

    # score (legacy v1 engine)
    s = sub.add_parser("score", help="Enrich + score (legacy v1 single Psycho_Score engine)")
    add_common(s)
    s.add_argument("--no-resume", action="store_true", help="Ignore previous pipeline state")
    s.add_argument("--min-score-report", type=float, default=None)

    # score-v3
    v3 = sub.add_parser("score-v3", help="v3 multi-score + fields A–J from profiles or live gather")
    add_common(v3)
    v3.add_argument(
        "--from-profiles",
        default=None,
        help="Path to profile JSON from gather (full profiles file or profile_YYYY.json)",
    )

    # default args when no subcommand (backward compatible = score)
    add_common(p)
    p.add_argument("--no-resume", action="store_true")
    p.add_argument("--min-score-report", type=float, default=None)
    p.add_argument(
        "--gather",
        action="store_true",
        help="Shorthand for gather mode without subcommand",
    )
    return p


def cmd_gather(args: argparse.Namespace) -> int:
    configure_logging(args.verbose)
    config = load_config(args.config)
    _apply_source_flags(config, args)
    pipe = Pipeline(config, load_dictionaries())

    items = _collect_items(args)
    if not items:
        print("Provide --input, --title, and/or --eval-set.", file=sys.stderr)
        return 2

    print(f"PsychoFilm Analyzer v{__version__} — GATHER ONLY")
    print(f"Titles: {len(items)} (no psych scores / clusters)")
    keys = config.get("api_keys") or {}
    print(
        "API keys: "
        f"TMDB={'yes' if keys.get('tmdb') else 'no'}, "
        f"OMDb={'yes' if keys.get('omdb') else 'no'}, "
        f"Kinopoisk={'yes' if keys.get('kinopoisk') else 'no'}"
    )

    profiles = pipe.gather(items)
    out_dir = (config.get("output") or {}).get("dir", "output")
    written = write_profiles(profiles, output_dir=out_dir, prefix="profile")

    print("\nCoverage summary:")
    for p in profiles:
        cov = p.coverage
        print(
            f"  {p.title_en or p.input.display_title():40s} "
            f"src={cov.get('sources_found')} "
            f"plot_en={cov.get('has_plot_en')} plot_ru={cov.get('has_plot_ru')} "
            f"kw={cov.get('keywords_n')} bags={cov.get('bags_n')} "
            f"type={p.content_type} spectacle={p.type_flags.get('is_spectacle')}"
        )
    print("\nOutputs:")
    for k, path in written.items():
        print(f"  {k}: {path}")
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    configure_logging(args.verbose)
    config = load_config(args.config)
    _apply_source_flags(config, args)
    if getattr(args, "min_score_report", None) is not None:
        config.setdefault("output", {})["markdown_min_score"] = args.min_score_report

    pipe = Pipeline(config, load_dictionaries())
    items = _collect_items(args)
    if not items:
        print("Provide --input, --title, and/or --eval-set.", file=sys.stderr)
        return 2

    print(f"PsychoFilm Analyzer v{__version__} — SCORE (v1 engine)")
    print(f"Titles to process: {len(items)}")
    keys = config.get("api_keys") or {}
    print(
        "API keys: "
        f"TMDB={'yes' if keys.get('tmdb') else 'no'}, "
        f"OMDb={'yes' if keys.get('omdb') else 'no'}, "
        f"Kinopoisk={'yes' if keys.get('kinopoisk') else 'no'}"
    )

    results = pipe.run(items, resume=not args.no_resume)
    out_cfg = config.get("output") or {}
    written = write_outputs(
        results,
        output_dir=out_cfg.get("dir", "output"),
        excel=bool(out_cfg.get("excel", True)),
        json_out=bool(out_cfg.get("json", True)),
        markdown_top_n=int(out_cfg.get("markdown_top_n", 25)),
        markdown_min_score=float(out_cfg.get("markdown_min_score", 7.0)),
    )

    ranked = sorted(results, key=lambda r: r.psycho_score, reverse=True)
    print("\nTop 10 by Psycho_Score:")
    for i, r in enumerate(ranked[:10], 1):
        print(
            f"  {i:2d}. {r.psycho_score:4.1f}  {r.input.display_title()}"
            f"  [{r.primary_cluster or '—'}]  ({r.confidence.value})"
        )
    print("\nOutputs:")
    for k, path in written.items():
        print(f"  {k}: {path}")
    return 0


def cmd_score_v3(args: argparse.Namespace) -> int:
    import json

    configure_logging(args.verbose)
    config = load_config(args.config)
    _apply_source_flags(config, args)
    dicts = load_dictionaries_v3()
    out_dir = (config.get("output") or {}).get("dir", "output")

    profiles: list[dict] = []
    if args.from_profiles:
        path = Path(args.from_profiles)
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "profiles" in data:
            profiles = data["profiles"]
        elif isinstance(data, list):
            # flat list cannot rebuild full bags well — prefer full JSON
            profiles = data
        else:
            print("Unrecognized profile JSON format.", file=sys.stderr)
            return 2
    else:
        # live gather then score
        pipe = Pipeline(config, load_dictionaries())
        items = _collect_items(args)
        if not items:
            print("Provide --from-profiles, --eval-set, --input, or --title.", file=sys.stderr)
            return 2
        print(f"Gathering {len(items)} titles then scoring v3...")
        gathered = pipe.gather(items)
        profiles = [p.to_json_dict() for p in gathered]

    if args.limit:
        profiles = profiles[: args.limit]

    print(f"PsychoFilm Analyzer v{__version__} — SCORE v3")
    print(f"Profiles: {len(profiles)}")
    results = score_profiles(profiles, dicts)
    written = write_v3_results(results, output_dir=out_dir)

    ranked = sorted(
        results,
        key=lambda r: float((r.get("scores") or {}).get("Podcast_Priority", {}).get("score") or 0),
        reverse=True,
    )
    print("\nTop by Podcast_Priority:")
    for i, r in enumerate(ranked[:12], 1):
        pp = (r.get("scores") or {}).get("Podcast_Priority", {}).get("score")
        print(
            f"  {i:2d}. {pp:4.1f}  {r.get('title_en')}"
            f"  [{r.get('primary_theme')}] / {r.get('secondary_theme')}"
        )
    print("\nOutputs:")
    for k, path in written.items():
        print(f"  {k}: {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Subcommands
    if args.command == "gather" or getattr(args, "gather", False):
        return cmd_gather(args)
    if args.command == "score":
        return cmd_score(args)
    if args.command == "score-v3":
        return cmd_score_v3(args)

    # Backward compatible: bare flags => score mode
    if not args.input and not args.title and not getattr(args, "eval_set", False):
        parser.print_help()
        print("\nExamples:", file=sys.stderr)
        print("  python run.py gather --eval-set --no-letterboxd", file=sys.stderr)
        print("  python run.py score-v3 --from-profiles output/profile_YYYY.json", file=sys.stderr)
        print('  python run.py score -i "list.xlsx" -n 10', file=sys.stderr)
        return 2
    return cmd_score(args)


if __name__ == "__main__":
    raise SystemExit(main())

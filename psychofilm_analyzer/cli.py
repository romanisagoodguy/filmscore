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
from psychofilm_analyzer.utils.localtime import now_str, stamp_local


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
    g.add_argument(
        "--no-resume",
        action="store_true",
        help="Ignore gather_checkpoint.jsonl and re-fetch all titles",
    )
    g.add_argument(
        "--request-debug",
        action="store_true",
        help=(
            "Write full HTTP reproduction debug for every film: text log + "
            "CSV tables + Excel (requests/films/sites/summary). "
            "Contains API keys — treat as secret."
        ),
    )
    g.add_argument(
        "--request-debug-path",
        default=None,
        help=(
            "Path for request debug text log (default: "
            "output/request_debug_<timestamp>.txt). Tables use same stem."
        ),
    )
    g.add_argument(
        "--request-debug-no-secrets",
        action="store_true",
        help="Redact API keys in debug commands/tables (harder to reproduce)",
    )
    g.add_argument(
        "--request-debug-excel-every",
        type=int,
        default=25,
        help="Rewrite request-debug Excel every N films (default 25; CSVs update live)",
    )
    g.add_argument(
        "--approach",
        type=int,
        choices=[1, 2],
        default=1,
        help=(
            "Gather approach: 1 = sequential film-by-film (default, scoring-compatible); "
            "2 = Request Plan + independent per-site pipelines"
        ),
    )
    g.add_argument(
        "--plan-only",
        action="store_true",
        help="Approach 2 only: build Request Plan Excel/JSONL and exit (no HTTP)",
    )
    g.add_argument(
        "--no-inherit-a1",
        action="store_true",
        help="Approach 2 only: do not skip films already in Approach 1 gather_checkpoint.jsonl",
    )

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
        help="Path to profile JSON/JSONL from gather (profile_*.json or gather_checkpoint.jsonl)",
    )
    v3.add_argument(
        "--no-resume",
        action="store_true",
        help="Ignore score_checkpoint.jsonl and re-score all profiles",
    )
    v3.add_argument(
        "--no-live-export",
        action="store_true",
        help="Disable per-film text/csv/excel live export (final export only)",
    )
    v3.add_argument(
        "--excel-every",
        type=int,
        default=25,
        help="Refresh score_live.xlsx every N scored films (default 25)",
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

    items = _collect_items(args)
    if not items:
        print("Provide --input, --title, and/or --eval-set.", file=sys.stderr)
        return 2

    approach = int(getattr(args, "approach", None) or (config.get("gather") or {}).get("approach", 1) or 1)
    if approach == 2:
        return _cmd_gather_approach2(args, config, items)

    # ----- Approach 1 (sequential) — original path, unchanged internals -----
    pipe = Pipeline(config, load_dictionaries())

    from psychofilm_analyzer.utils.run_plan import (
        build_gather_plan,
        print_run_plan,
        write_run_plan,
    )

    out_dir = (config.get("output") or {}).get("dir", "output")
    resume = not getattr(args, "no_resume", False)
    src = config.get("sources") or {}
    keys = config.get("api_keys") or {}
    source_file = args.input if getattr(args, "input", None) else (
        "config/eval_set_20.yaml" if getattr(args, "eval_set", False) else None
    )
    sheet = items[0].source_sheet if items else None

    plan = build_gather_plan(
        source_file=source_file,
        source_sheet=sheet,
        titles_count=len(items),
        output_dir=out_dir,
        sources_enabled={
            "tmdb": bool(src.get("tmdb", True) and keys.get("tmdb")),
            "omdb": bool(src.get("omdb", True) and keys.get("omdb")),
            "kinopoisk": bool(src.get("kinopoisk", True) and keys.get("kinopoisk")),
            "wikipedia": bool(src.get("wikipedia", True)),
            "letterboxd": bool(src.get("letterboxd", True)),
        },
        resume=resume,
    )
    print(f"PsychoFilm Analyzer v{__version__} — GATHER ONLY (Approach 1 sequential)")
    print_run_plan(plan)
    write_run_plan(plan, Path(out_dir) / "pipeline_run_plan.json")
    print(f"  Run plan saved → {out_dir}/pipeline_run_plan.json")

    request_debug = bool(getattr(args, "request_debug", False)) or bool(
        (config.get("pipeline") or {}).get("request_debug", False)
    )
    dbg_paths: dict = {}
    if request_debug:
        from datetime import datetime, timezone

        ts = stamp_local()
        dbg_path = getattr(args, "request_debug_path", None) or (
            (config.get("pipeline") or {}).get("request_debug_path")
            or f"{out_dir}/request_debug_{ts}.txt"
        )
        include_secrets = not bool(getattr(args, "request_debug_no_secrets", False))
        excel_every = int(
            getattr(args, "request_debug_excel_every", None)
            or (config.get("pipeline") or {}).get("request_debug_excel_every", 25)
        )
        dbg = pipe.attach_request_debug(
            dbg_path,
            include_secrets=include_secrets,
            write_tables=True,
            excel_every_films=excel_every,
            meta={
                "mode": "gather",
                "approach": 1,
                "input": source_file or "",
                "titles_total": len(items),
                "resume": resume,
                "cli": "psychofilm gather --approach 1 --request-debug",
            },
        )
        dbg_paths = dbg.output_paths()
        print("\nREQUEST DEBUG ENABLED (all films in this run)")
        print("  WARNING: files may contain full API keys — do not commit/share.")
        for k, pth in dbg_paths.items():
            print(f"  {k}: {pth}")

    session_profiles = pipe.gather(items, resume=resume)

    # Prefer full checkpoint (includes prior resume rows) for export
    from psychofilm_analyzer.enrichment.export import write_profile_dicts

    ckpt_dicts = pipe.load_gather_checkpoint()
    if not ckpt_dicts and session_profiles:
        all_dicts = [p.to_json_dict() for p in session_profiles]
    else:
        # Preserve catalog order from input items
        by_key: dict[str, dict] = {}
        # Re-read with keys from file
        pipe_cfg = config.get("pipeline") or {}
        ckpt_path = Path(pipe_cfg.get("gather_checkpoint", "output/gather_checkpoint.jsonl"))
        if ckpt_path.exists():
            import json as _json

            with ckpt_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = _json.loads(line)
                    except _json.JSONDecodeError:
                        continue
                    k = row.pop("_resume_key", None)
                    if k:
                        by_key[str(k)] = row
        all_dicts = []
        for it in items:
            k = pipe.resume_key(it)
            if k in by_key:
                all_dicts.append(by_key[k])
        # Append any orphan checkpoint rows not in current input
        seen = {pipe.resume_key(it) for it in items}
        for k, row in by_key.items():
            if k not in seen:
                all_dicts.append(row)

    # Excel for full catalog is heavy; still write it (capped evidence sheet)
    write_excel = len(all_dicts) <= 20000
    written = write_profile_dicts(
        all_dicts, output_dir=out_dir, prefix="profile", write_excel=write_excel
    )

    # Coverage summary: all rows if small, else aggregate stats
    print(f"\nGathered profiles: {len(all_dicts)}")
    if len(all_dicts) <= 40:
        for p in session_profiles:
            cov = p.coverage
            print(
                f"  {p.title_en or p.input.display_title():40s} "
                f"src={cov.get('sources_found')} "
                f"plot_en={cov.get('has_plot_en')} plot_ru={cov.get('has_plot_ru')} "
                f"kw={cov.get('keywords_n')} bags={cov.get('bags_n')} "
                f"type={p.content_type} spectacle={p.type_flags.get('is_spectacle')}"
            )
    else:
        found = sum(1 for d in all_dicts if (d.get("coverage") or {}).get("sources_found"))
        plot_en = sum(1 for d in all_dicts if (d.get("coverage") or {}).get("has_plot_en"))
        plot_ru = sum(1 for d in all_dicts if (d.get("coverage") or {}).get("has_plot_ru"))
        errs = sum(1 for d in all_dicts if d.get("error"))
        print(
            f"  with sources: {found} | plot_en: {plot_en} | plot_ru: {plot_ru} | errors: {errs}"
        )
        print(f"  session new EnrichmentProfiles: {len(session_profiles)}")
    print("\nOutputs:")
    for k, path in written.items():
        print(f"  {k}: {path}")
    if dbg_paths:
        print("\nRequest debug outputs:")
        for k, path in dbg_paths.items():
            print(f"  {k}: {path}")
    return 0


def _cmd_gather_approach2(
    args: argparse.Namespace,
    config: dict,
    items: list,
) -> int:
    """Approach 2 entry — separate from sequential Pipeline.gather."""
    from psychofilm_analyzer.gather_v2.runner import run_gather_v2
    from psychofilm_analyzer.io.input_loader import load_titles

    out_dir = (config.get("output") or {}).get("dir", "output")
    resume = not getattr(args, "no_resume", False)
    request_debug = bool(getattr(args, "request_debug", False))
    plan_only = bool(getattr(args, "plan_only", False))

    # --no-inherit-a1 forces False; otherwise config gather_v2.inherit_approach1 (default True)
    if getattr(args, "no_inherit_a1", False):
        inherit_a1 = False
    else:
        inherit_a1 = bool((config.get("gather_v2") or {}).get("inherit_approach1", True))

    # With inherit + -n N: load FULL catalog, process N pending films (not first N of file)
    pending_limit = None
    if inherit_a1 and getattr(args, "limit", None):
        pending_limit = int(args.limit)
        if getattr(args, "input", None):
            items = load_titles(args.input)  # full list for A1 inherit keys
            print(f"  inherit+limit: loaded full catalog {len(items)} titles; "
                  f"will process up to {pending_limit} pending")

    print(f"PsychoFilm Analyzer v{__version__} — GATHER Approach 2")
    print("  Request Plan + independent per-site pipelines")
    print(f"  catalog_items: {len(items)}")
    print(f"  resume: {resume}")
    print(f"  plan_only: {plan_only}")
    print(f"  inherit_a1: {inherit_a1}")
    print(f"  pending_limit: {pending_limit}")
    print(f"  output: {out_dir}/gather_v2/")
    print("  ranking/scoring: use Approach 1 profiles for now")

    summary = run_gather_v2(
        items,
        config,
        resume=resume,
        plan_only=plan_only,
        request_debug=request_debug,
        inherit_approach1=inherit_a1,
        pending_limit=pending_limit,
    )
    print("\nApproach 2 complete.")
    for k, v in summary.items():
        if k == "exports" and isinstance(v, dict):
            for ek, ev in v.items():
                print(f"  export.{ek}: {ev}")
        else:
            print(f"  {k}: {v}")
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
        # Support JSONL checkpoint or full JSON
        if path.suffix.lower() == ".jsonl":
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                row.pop("_resume_key", None)
                profiles.append(row)
        else:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "profiles" in data:
                profiles = data["profiles"]
            elif isinstance(data, list):
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

    live = not getattr(args, "no_live_export", False)
    resume = not getattr(args, "no_resume", False)
    excel_every = int(getattr(args, "excel_every", None) or 25)

    from psychofilm_analyzer.utils.run_plan import (
        build_score_v3_plan,
        print_run_plan,
        write_run_plan,
    )

    score_input = args.from_profiles or "(live gather from --input / --eval-set / --title)"
    plan = build_score_v3_plan(
        score_input=score_input,
        profiles_count=len(profiles),
        output_dir=out_dir,
        resume=resume,
        excel_every=excel_every,
        live_export=live,
    )
    # Document where the film list originally came from (if present on profiles)
    sample = profiles[0] if profiles else {}
    imported = sample.get("imported") or {}
    plan["film_list_provenance"] = {
        "imported_file": imported.get("file") or sample.get("imported_file"),
        "imported_sheet": imported.get("sheet") or sample.get("imported_sheet"),
        "note": "Original Excel list is provenance only; scoring reads gather profiles.",
    }

    print(f"PsychoFilm Analyzer v{__version__} — SCORE v3")
    print_run_plan(plan)
    if plan.get("film_list_provenance", {}).get("imported_file"):
        print("  FILM LIST PROVENANCE (from gather profiles, not re-read for scoring)")
        print(f"    original list: {plan['film_list_provenance'].get('imported_file')}")
        print(f"    sheet:         {plan['film_list_provenance'].get('imported_sheet')}")
        print("")
    write_run_plan(plan, Path(out_dir) / "pipeline_run_plan.json")
    print(f"  Run plan saved → {out_dir}/pipeline_run_plan.json")

    results = score_profiles(
        profiles,
        dicts,
        live_export=live,
        output_dir=out_dir,
        resume=resume,
        excel_every=excel_every,
        show_progress=True,
    )
    # Final stamped export (ranked workbook + JSON + markdown top)
    written = write_v3_results(results, output_dir=out_dir)
    if live:
        written["live_txt"] = Path(out_dir) / "score_live.txt"
        written["live_csv"] = Path(out_dir) / "score_live.csv"
        written["live_xlsx"] = Path(out_dir) / "score_live.xlsx"
        written["score_checkpoint"] = Path(out_dir) / "score_checkpoint.jsonl"

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

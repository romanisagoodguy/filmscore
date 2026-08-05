"""Print / persist a clear confirmation of pipeline I/O for each run."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def _exists_note(path: Path | str | None) -> str:
    if not path:
        return "(not set)"
    p = Path(path)
    if p.exists():
        try:
            size = p.stat().st_size
            if size >= 1_000_000:
                sz = f"{size / 1_000_000:.1f} MB"
            elif size >= 1000:
                sz = f"{size / 1000:.1f} KB"
            else:
                sz = f"{size} B"
            return f"exists ({sz})"
        except OSError:
            return "exists"
    return "will be created"


def build_gather_plan(
    *,
    source_file: Optional[str | Path],
    source_sheet: Optional[str],
    titles_count: int,
    output_dir: str | Path = "output",
    sources_enabled: Optional[dict[str, bool]] = None,
    resume: bool = True,
) -> dict[str, Any]:
    out = Path(output_dir)
    plan = {
        "mode": "gather",
        "confirmed_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "film_list": str(source_file) if source_file else "(eval-set / --title CLI)",
            "sheet": source_sheet,
            "titles_to_process": titles_count,
            "status": _exists_note(source_file) if source_file else "n/a",
        },
        "sources_enabled": sources_enabled or {},
        "resume": resume,
        "exports": {
            "checkpoint_jsonl": str(out / "gather_checkpoint.jsonl"),
            "live_text": str(out / "gather_live.txt"),
            "live_csv": str(out / "gather_live.csv"),
            "live_excel": str(out / "gather_live.xlsx"),
            "progress": str(out / "gather_progress.json"),
            "resume_marker": str(out / "gather_resume_point.json"),
            "final_profile_json": str(out / "profile_*.json"),
            "final_profile_excel": str(out / "profile_*.xlsx"),
        },
        "notes": [
            "After each film: append to gather_checkpoint.jsonl, gather_live.txt, gather_live.csv",
            "Every N films: rewrite gather_live.xlsx",
            "At end: stamped profile_YYYYMMDD_HHMMSS.json/xlsx",
            "Next step (scoring): use gather_checkpoint.jsonl or profile_partial_latest.json / profile_*.json",
        ],
    }
    return plan


def build_score_v3_plan(
    *,
    score_input: str | Path,
    profiles_count: int,
    output_dir: str | Path = "output",
    resume: bool = True,
    excel_every: int = 25,
    live_export: bool = True,
) -> dict[str, Any]:
    out = Path(output_dir)
    inp = Path(score_input)
    plan = {
        "mode": "score-v3",
        "confirmed_at": datetime.now(timezone.utc).isoformat(),
        "input": {
            "path": str(inp),
            "kind": "jsonl_checkpoint" if inp.suffix.lower() == ".jsonl" else "profile_json",
            "profiles_count": profiles_count,
            "status": _exists_note(inp),
            "description": (
                "Gather evidence profiles (plots, keywords, bags, IDs). "
                "This is the ONLY input to score-v3 — not the original film list Excel."
            ),
        },
        "resume": resume,
        "live_export": live_export,
        "excel_every": excel_every,
        "exports": {
            "checkpoint_jsonl": str(out / "score_checkpoint.jsonl"),
            "live_text": str(out / "score_live.txt"),
            "live_csv": str(out / "score_live.csv"),
            "live_excel": str(out / "score_live.xlsx"),
            "progress": str(out / "score_progress.json"),
            "final_json": str(out / "psychofilm_v3_*.json"),
            "final_excel": str(out / "psychofilm_v3_*.xlsx"),
            "final_top_md": str(out / "psychofilm_v3_*_top.md"),
            "latest_flat": str(out / "psychofilm_v3_latest.json"),
        },
        "score_columns_note": (
            "score_Podcast_Priority = pure psych/discuss episode worth; "
            "score_Overall_Priority_for_Podcast (and Overall_Priority_for_Podcast) = schedule rank "
            "(psych + awards + engagement) — they are NOT the same formula. "
            "score_Symbolism_Ambiguity and score_Discussability_Podcast_Potential are separate; "
            "score_Awards_Prestige uses awards_text + awards bags (EN+RU)."
        ),
        "notes": [
            "After each film: append score_checkpoint.jsonl, score_live.txt, score_live.csv",
            f"Every {excel_every} films: rewrite score_live.xlsx",
            "At end: stamped psychofilm_v3_YYYYMMDD_HHMMSS.xlsx/json + top.md",
        ],
    }
    return plan


def print_run_plan(plan: dict[str, Any]) -> None:
    """Human-readable confirmation banner."""
    mode = plan.get("mode", "?")
    print("")
    print("=" * 72)
    print(f"  PsychoFilm RUN PLAN — {mode.upper()}")
    print("=" * 72)

    if mode == "gather":
        src = plan.get("source") or {}
        print("  SOURCE (film list)")
        print(f"    file:   {src.get('film_list')}")
        if src.get("sheet"):
            print(f"    sheet:  {src.get('sheet')}")
        print(f"    titles: {src.get('titles_to_process')}  [{src.get('status')}]")
        print("  DATA SOURCES")
        for k, v in (plan.get("sources_enabled") or {}).items():
            print(f"    {k}: {'ON' if v else 'OFF'}")
        print("  GATHER EXPORTS (after each film → live; end → stamped)")
        for k, v in (plan.get("exports") or {}).items():
            print(f"    {k:22s} → {v}  [{_exists_note(v) if '*' not in str(v) else 'pattern'}]")
        print("  SCORING INPUT (for later score-v3)")
        print("    preferred: output/gather_checkpoint.jsonl")
        print("    alternate: output/profile_*.json or profile_partial_latest.json")

    elif mode == "score-v3":
        inp = plan.get("input") or {}
        print("  SCORE INPUT (from gather — not the original film list)")
        print(f"    path:     {inp.get('path')}")
        print(f"    kind:     {inp.get('kind')}")
        print(f"    profiles: {inp.get('profiles_count')}  [{inp.get('status')}]")
        print(f"    note:     {inp.get('description')}")
        prov = plan.get("film_list_provenance") or {}
        if prov.get("imported_file"):
            print("  FILM LIST PROVENANCE (embedded in profiles)")
            print(f"    original list: {prov.get('imported_file')}")
            if prov.get("imported_sheet"):
                print(f"    sheet:         {prov.get('imported_sheet')}")
        print("  SCORE EXPORTS (after each film → live; end → stamped)")
        for k, v in (plan.get("exports") or {}).items():
            print(f"    {k:22s} → {v}  [{_exists_note(v) if '*' not in str(v) else 'pattern'}]")
        if plan.get("score_columns_note"):
            print("  SCORE COLUMN NAMES")
            print(f"    {plan['score_columns_note']}")

    print("  OPTIONS")
    print(f"    resume: {plan.get('resume', True)}")
    if "live_export" in plan:
        print(f"    live_export: {plan.get('live_export')}")
        print(f"    excel_every: {plan.get('excel_every')}")
    for note in plan.get("notes") or []:
        print(f"  · {note}")
    print("=" * 72)
    print("")


def write_run_plan(plan: dict[str, Any], path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return path

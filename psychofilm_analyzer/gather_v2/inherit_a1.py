"""Inherit Approach 1 gather results into Approach 2 (skip already-done films)."""

from __future__ import annotations

import csv
import json
import logging
from pathlib import Path
from typing import Any, Optional

from psychofilm_analyzer.utils.localtime import now_str
from psychofilm_analyzer.models import InputTitle
from psychofilm_analyzer.pipeline import Pipeline

logger = logging.getLogger(__name__)


def load_approach1_checkpoint(
    checkpoint_path: str | Path,
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    """
    Load Approach 1 gather_checkpoint.jsonl.

    Returns:
      by_key: resume_key → profile dict (without _resume_key)
      keys: set of resume keys
    """
    path = Path(checkpoint_path)
    by_key: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return by_key, set()
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = row.pop("_resume_key", None)
            if not key:
                imp = row.get("imported") or {}
                key = "|".join(
                    [
                        str(imp.get("file") or ""),
                        str(imp.get("sheet") or ""),
                        str(imp.get("row") or ""),
                        str(imp.get("title") or "").strip().lower(),
                        str(imp.get("year") or row.get("year") or ""),
                    ]
                )
            by_key[str(key)] = row
    return by_key, set(by_key.keys())


def split_items_for_approach2(
    items: list[InputTitle],
    *,
    approach1_checkpoint: str | Path,
    inherit: bool = True,
) -> tuple[list[InputTitle], list[InputTitle], dict[str, dict[str, Any]]]:
    """
    Split catalog into (already_done_a1, pending_for_a2, a1_profiles_by_key).

    If inherit=False, all items go to pending and a1_profiles is empty.
    """
    if not inherit:
        return [], list(items), {}

    a1_profiles, done_keys = load_approach1_checkpoint(approach1_checkpoint)
    done_items: list[InputTitle] = []
    pending: list[InputTitle] = []
    for it in items:
        key = Pipeline.resume_key(it)
        if key in done_keys:
            done_items.append(it)
        else:
            pending.append(it)
    logger.info(
        "Approach 1 inherit: %s done, %s pending for Approach 2 (checkpoint=%s)",
        len(done_items),
        len(pending),
        approach1_checkpoint,
    )
    return done_items, pending, a1_profiles


def merge_profiles_catalog_order(
    items: list[InputTitle],
    *,
    a1_profiles_by_key: dict[str, dict[str, Any]],
    a2_profiles: list[dict[str, Any]],
    a2_items: list[InputTitle],
) -> list[dict[str, Any]]:
    """
    Merge A1 + A2 profiles in full catalog order.

    a2_profiles is aligned with a2_items (same length/order).
    """
    a2_by_key: dict[str, dict[str, Any]] = {}
    for it, prof in zip(a2_items, a2_profiles):
        a2_by_key[Pipeline.resume_key(it)] = prof

    out: list[dict[str, Any]] = []
    for it in items:
        key = Pipeline.resume_key(it)
        if key in a2_by_key:
            d = dict(a2_by_key[key])
            d["gather_approach"] = 2
            d["inherited_from_approach1"] = False
            out.append(d)
        elif key in a1_profiles_by_key:
            d = dict(a1_profiles_by_key[key])
            d["gather_approach"] = 1
            d["inherited_from_approach1"] = True
            out.append(d)
        else:
            out.append(
                {
                    "imported": {
                        "file": it.source_file,
                        "sheet": it.source_sheet,
                        "row": it.source_row,
                        "title": it.import_title or it.title,
                        "year": it.year,
                    },
                    "titles": {"en": it.english_title, "ru": it.russian_title},
                    "year": it.year,
                    "error": "missing from A1 and A2",
                    "gather_approach": None,
                    "inherited_from_approach1": False,
                }
            )
    return out


def _profile_title(p: dict[str, Any]) -> str:
    titles = p.get("titles") or {}
    imp = p.get("imported") or {}
    return (
        titles.get("en")
        or titles.get("ru")
        or imp.get("title")
        or p.get("title_en")
        or ""
    )


def materialize_inherited_a1(
    *,
    plan_dir: str | Path,
    reports_dir: str | Path,
    items: list[InputTitle],
    done_a1_items: list[InputTitle],
    pending_items: list[InputTitle],
    a1_profiles_by_key: dict[str, dict[str, Any]],
    a1_checkpoint_path: str | Path,
    a2_profiles: Optional[list[dict[str, Any]]] = None,
    a2_items: Optional[list[InputTitle]] = None,
) -> dict[str, Path]:
    """
    Write visible Approach-1 inheritance artifacts so the user can open them.

    Always produces:
      - inherited_from_approach1.jsonl  (full A1 profile payloads)
      - inherited_inventory.csv         (one row per A1 film)
      - pending_for_approach2.csv       (films still to fetch)
      - reports/INHERITED_FROM_APPROACH1.txt
      - gather_v2_checkpoint.jsonl      (merged catalog: A1 + A2 if any / placeholders)
    """
    plan_dir = Path(plan_dir)
    reports_dir = Path(reports_dir)
    plan_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)

    a2_profiles = a2_profiles or []
    a2_items = a2_items or []

    # --- full A1 profile dump ---
    inherited_jsonl = plan_dir / "inherited_from_approach1.jsonl"
    inherited_rows: list[dict[str, Any]] = []
    with inherited_jsonl.open("w", encoding="utf-8") as fh:
        for it in done_a1_items:
            key = Pipeline.resume_key(it)
            p = dict(a1_profiles_by_key.get(key) or {})
            p["gather_approach"] = 1
            p["inherited_from_approach1"] = True
            p["_resume_key"] = key
            inherited_rows.append(p)
            fh.write(json.dumps(p, ensure_ascii=False) + "\n")

    # --- inventory CSV ---
    inv_csv = plan_dir / "inherited_inventory.csv"
    inv_fields = [
        "resume_key",
        "excel_sheet",
        "excel_row",
        "import_title",
        "year",
        "title_en",
        "title_ru",
        "imdb_id",
        "tmdb_id",
        "kinopoisk_id",
        "cov_sources_found",
        "has_plot_en",
        "has_plot_ru",
        "src_tmdb",
        "src_omdb",
        "src_kinopoisk",
        "src_wikipedia",
        "src_letterboxd",
        "gather_approach",
        "inherited_from_approach1",
    ]
    with inv_csv.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=inv_fields, extrasaction="ignore")
        w.writeheader()
        for it in done_a1_items:
            key = Pipeline.resume_key(it)
            p = a1_profiles_by_key.get(key) or {}
            imp = p.get("imported") or {}
            titles = p.get("titles") or {}
            ids = p.get("ids") or {}
            cov = p.get("coverage") or {}
            sources = p.get("sources") or {}

            def _found(name: str) -> str:
                s = sources.get(name) if isinstance(sources, dict) else None
                if isinstance(s, dict):
                    return "FOUND" if s.get("found") else "MISS"
                if cov.get(name) is True:
                    return "FOUND"
                if cov.get(name) is False:
                    return "MISS"
                return ""

            w.writerow(
                {
                    "resume_key": key,
                    "excel_sheet": imp.get("sheet") or it.source_sheet,
                    "excel_row": imp.get("row") if imp.get("row") is not None else it.source_row,
                    "import_title": imp.get("title") or it.import_title or it.title,
                    "year": p.get("year") or it.year,
                    "title_en": titles.get("en"),
                    "title_ru": titles.get("ru"),
                    "imdb_id": ids.get("imdb_id"),
                    "tmdb_id": ids.get("tmdb_id"),
                    "kinopoisk_id": ids.get("kinopoisk_id"),
                    "cov_sources_found": cov.get("sources_found"),
                    "has_plot_en": cov.get("has_plot_en"),
                    "has_plot_ru": cov.get("has_plot_ru"),
                    "src_tmdb": _found("tmdb"),
                    "src_omdb": _found("omdb"),
                    "src_kinopoisk": _found("kinopoisk"),
                    "src_wikipedia": _found("wikipedia"),
                    "src_letterboxd": _found("letterboxd"),
                    "gather_approach": 1,
                    "inherited_from_approach1": True,
                }
            )

    # --- pending inventory ---
    pend_csv = plan_dir / "pending_for_approach2.csv"
    with pend_csv.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(
            fh,
            fieldnames=[
                "excel_sheet",
                "excel_row",
                "title",
                "english_title",
                "year",
                "resume_key",
            ],
        )
        w.writeheader()
        for it in pending_items:
            w.writerow(
                {
                    "excel_sheet": it.source_sheet,
                    "excel_row": it.source_row,
                    "title": it.title,
                    "english_title": it.english_title,
                    "year": it.year,
                    "resume_key": Pipeline.resume_key(it),
                }
            )

    # --- Excel inventory (if pandas available) ---
    inv_xlsx = plan_dir / "inherited_inventory.xlsx"
    try:
        import pandas as pd

        df_inv = pd.read_csv(inv_csv, encoding="utf-8-sig")
        df_pend = pd.read_csv(pend_csv, encoding="utf-8-sig")
        with pd.ExcelWriter(inv_xlsx, engine="openpyxl") as writer:
            df_inv.to_excel(writer, sheet_name="inherited_a1", index=False)
            df_pend.to_excel(writer, sheet_name="pending_a2", index=False)
            summary = pd.DataFrame(
                [
                    {"metric": "a1_checkpoint", "value": str(a1_checkpoint_path)},
                    {"metric": "catalog_total", "value": len(items)},
                    {"metric": "inherited_a1", "value": len(done_a1_items)},
                    {"metric": "pending_a2", "value": len(pending_items)},
                    {
                        "metric": "catalog_pct_done_a1",
                        "value": round(100.0 * len(done_a1_items) / len(items), 2)
                        if items
                        else 0,
                    },
                    {"metric": "written_at", "value": now_str()},
                ]
            )
            summary.to_excel(writer, sheet_name="summary", index=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not write inherited_inventory.xlsx: %s", exc)
        inv_xlsx = Path("")

    # --- merged checkpoint (always visible) ---
    merged = merge_profiles_catalog_order(
        items,
        a1_profiles_by_key=a1_profiles_by_key,
        a2_profiles=a2_profiles,
        a2_items=a2_items,
    )
    # For pending not yet in A2, mark clearly
    a2_keys = {Pipeline.resume_key(it) for it in a2_items}
    for i, it in enumerate(items):
        key = Pipeline.resume_key(it)
        if key not in a1_profiles_by_key and key not in a2_keys:
            merged[i]["status_note"] = "pending_approach2"
            merged[i]["gather_approach"] = None
        elif key in a1_profiles_by_key and key not in a2_keys:
            merged[i]["status_note"] = "inherited_approach1"
        elif key in a2_keys:
            merged[i]["status_note"] = "fetched_approach2"

    merged_path = plan_dir / "gather_v2_checkpoint.jsonl"
    with merged_path.open("w", encoding="utf-8") as fh:
        for p in merged:
            fh.write(json.dumps(p, ensure_ascii=False) + "\n")

    # --- human text report ---
    txt = reports_dir / "INHERITED_FROM_APPROACH1.txt"
    samples = []
    for it in done_a1_items[:25]:
        key = Pipeline.resume_key(it)
        p = a1_profiles_by_key.get(key) or {}
        samples.append(
            f"  row={it.source_row}  {_profile_title(p)!r} ({p.get('year') or it.year})  "
            f"sources={(p.get('coverage') or {}).get('sources_found')}"
        )
    lines = [
        "INHERITED FROM APPROACH 1",
        f"written: {now_str()}",
        f"source_checkpoint: {a1_checkpoint_path}",
        "",
        "COUNTS",
        f"  catalog_total:     {len(items)}",
        f"  inherited_a1:      {len(done_a1_items)}",
        f"  pending_approach2: {len(pending_items)}",
        f"  a2_fetched_so_far: {len(a2_profiles)}",
        f"  merged_rows:       {len(merged)}",
        "",
        "FILES (open these)",
        f"  full A1 profiles:  {inherited_jsonl}",
        f"  inventory CSV:     {inv_csv}",
        f"  inventory Excel:   {inv_xlsx or '(csv only)'}",
        f"  pending A2 CSV:    {pend_csv}",
        f"  merged catalog:    {merged_path}",
        "",
        "SAMPLE INHERITED TITLES (first 25)",
    ]
    lines += samples or ["  (none)"]
    lines += [
        "",
        "NOTE",
        "  Approach 2 does NOT re-fetch these films.",
        "  They are copied from Approach 1 gather_checkpoint.jsonl.",
        "  After A2 pipelines finish, merged checkpoint includes both A1 + A2.",
        "",
    ]
    txt.write_text("\n".join(lines), encoding="utf-8")

    # Append inheritance section into UNIFIED_REPORT if present
    unified = reports_dir / "UNIFIED_REPORT.txt"
    section = "\n".join(
        [
            "",
            "=" * 72,
            "INHERITED APPROACH 1 DATA (visible files)",
            "=" * 72,
            f"  count: {len(done_a1_items)} / {len(items)} films",
            f"  detail report: {txt}",
            f"  profiles jsonl: {inherited_jsonl}",
            f"  inventory xlsx: {inv_xlsx or inv_csv}",
            f"  pending a2 csv: {pend_csv}",
            f"  merged catalog: {merged_path}",
            "",
        ]
    )
    if unified.exists():
        unified.write_text(unified.read_text(encoding="utf-8") + section, encoding="utf-8")
    else:
        unified.write_text(
            "PsychoFilm UNIFIED REPORT\n" + section, encoding="utf-8"
        )

    logger.info(
        "Materialized A1 inheritance: %s profiles → %s",
        len(done_a1_items),
        inherited_jsonl,
    )
    return {
        "inherited_jsonl": inherited_jsonl,
        "inherited_inventory_csv": inv_csv,
        "inherited_inventory_xlsx": inv_xlsx if inv_xlsx else inv_csv,
        "pending_csv": pend_csv,
        "merged_checkpoint": merged_path,
        "inherited_report_txt": txt,
        "unified_report": unified,
    }

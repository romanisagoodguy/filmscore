"""Entry point for Approach 2 gather (Request Plan + independent pipelines)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from psychofilm_analyzer.enrichment.export import write_profile_dicts
from psychofilm_analyzer.gather_v2.assemble import assemble_profiles
from psychofilm_analyzer.gather_v2.executor import Approach2Executor
from psychofilm_analyzer.gather_v2.inherit_a1 import (
    materialize_inherited_a1,
    merge_profiles_catalog_order,
    split_items_for_approach2,
)
from psychofilm_analyzer.gather_v2.models import (
    STATUS_DEFERRED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RETRY,
    STATUS_RUNNING,
)
from psychofilm_analyzer.gather_v2.plan_store import PlanStore
from psychofilm_analyzer.gather_v2.planner import build_plan_for_items
from psychofilm_analyzer.models import InputTitle
from psychofilm_analyzer.pipeline import Pipeline

logger = logging.getLogger(__name__)


def _default_site_delays(config: dict[str, Any]) -> dict[str, float]:
    a2 = config.get("gather_v2") or {}
    delays = dict(a2.get("site_delays_sec") or {})
    defaults = {
        "tmdb": 0.25,
        "omdb": 0.30,
        "kinopoisk": 0.40,
        "letterboxd": 0.50,
        "wikipedia": 2.0,
    }
    for k, v in defaults.items():
        delays.setdefault(k, v)
    return {str(k): float(v) for k, v in delays.items()}


def run_gather_v2(
    items: list[InputTitle],
    config: dict[str, Any],
    *,
    resume: bool = True,
    plan_only: bool = False,
    request_debug: bool = False,
    inherit_approach1: bool = True,
    pending_limit: Optional[int] = None,
) -> dict[str, Any]:
    """
    Run Approach 2 gather.

    Inherits Approach 1 checkpoint films (skip re-fetch) and only plans/executes
    the remaining catalog titles. Writes unified + per-pipeline reports.

    pending_limit: if set, only process this many pending films this run
    (useful with inherit A1: e.g. 20 next remaining films).
    """
    out_dir = Path((config.get("output") or {}).get("dir", "output"))
    a2 = config.get("gather_v2") or {}
    pipe_cfg = config.get("pipeline") or {}
    plan_dir = Path(a2.get("plan_dir") or (out_dir / "gather_v2"))
    plan_dir.mkdir(parents=True, exist_ok=True)
    reports_dir = Path(a2.get("reports_dir") or (plan_dir / "reports"))
    reports_dir.mkdir(parents=True, exist_ok=True)

    a1_ckpt = Path(
        a2.get("approach1_checkpoint")
        or pipe_cfg.get("gather_checkpoint")
        or (out_dir / "gather_checkpoint.jsonl")
    )
    # Explicit function argument wins (CLI --no-inherit-a1 must override config)
    inherit = bool(inherit_approach1)

    done_a1, pending_items, a1_profiles = split_items_for_approach2(
        items,
        approach1_checkpoint=a1_ckpt,
        inherit=inherit,
    )
    catalog_total = len(items)
    a1_done = len(done_a1)
    pending_total_available = len(pending_items)
    if pending_limit is not None and pending_limit >= 0:
        pending_items = pending_items[: int(pending_limit)]
    a2_film_total = len(pending_items)

    print("Approach 2: inherit Approach 1 results")
    print(f"  approach1_checkpoint: {a1_ckpt}  exists={a1_ckpt.exists()}")
    print(f"  catalog_total:        {catalog_total}")
    print(f"  approach1_done:       {a1_done}  (skip re-fetch)")
    print(f"  approach2_pending:    {pending_total_available} available")
    if pending_limit is not None:
        print(f"  this_run_limit:       {pending_limit} → processing {a2_film_total} films")
    else:
        print(f"  approach2_this_run:   {a2_film_total}")

    # Always materialize A1 data into visible files (even before / without A2 HTTP)
    inherit_paths = materialize_inherited_a1(
        plan_dir=plan_dir,
        reports_dir=reports_dir,
        items=items,
        done_a1_items=done_a1,
        pending_items=pending_items,
        a1_profiles_by_key=a1_profiles,
        a1_checkpoint_path=a1_ckpt,
        a2_profiles=[],
        a2_items=[],
    )
    print("  INHERITED DATA WRITTEN:")
    for k, p in inherit_paths.items():
        print(f"    {k}: {p}")

    if a2_film_total == 0 and not (resume and (plan_dir / "request_plan.jsonl").exists()):
        print("  Nothing left for Approach 2 — all films already in Approach 1 checkpoint.")
        _write_idle_report(
            reports_dir,
            catalog_total=catalog_total,
            a1_done=a1_done,
            a1_ckpt=a1_ckpt,
        )
        # re-materialize so idle report still has inheritance section after overwrite
        materialize_inherited_a1(
            plan_dir=plan_dir,
            reports_dir=reports_dir,
            items=items,
            done_a1_items=done_a1,
            pending_items=pending_items,
            a1_profiles_by_key=a1_profiles,
            a1_checkpoint_path=a1_ckpt,
        )
        merged = merge_profiles_catalog_order(
            items,
            a1_profiles_by_key=a1_profiles,
            a2_profiles=[],
            a2_items=[],
        )
        ckpt = Path(a2.get("checkpoint_jsonl") or (plan_dir / "gather_v2_checkpoint.jsonl"))
        written = write_profile_dicts(
            merged, output_dir=str(out_dir), prefix="profile_a2", write_excel=len(merged) <= 5000
        )
        return {
            "approach": 2,
            "inherited_a1": a1_done,
            "pending_a2": 0,
            "profiles": len(merged),
            "checkpoint_jsonl": str(ckpt),
            "inherited_jsonl": str(inherit_paths.get("inherited_jsonl", "")),
            "inherited_inventory": str(inherit_paths.get("inherited_inventory_xlsx", "")),
            "exports": {k: str(v) for k, v in written.items()},
            "reports_dir": str(reports_dir),
            "unified_report": str(reports_dir / "UNIFIED_REPORT.txt"),
            "inherited_report": str(reports_dir / "INHERITED_FROM_APPROACH1.txt"),
            "note": "all films already covered by Approach 1",
        }

    store = PlanStore(plan_dir)
    existing = store.load() if resume else 0
    work_items = pending_items

    # This run only plans/executes pending_items (optionally limited).
    # film_index is 1..len(pending_items) for this batch.
    pending_keys = {Pipeline.resume_key(it) for it in pending_items}
    work_items = pending_items

    # Rebuild plan if size does not match this batch
    if existing and resume:
        plan_films = len({int(r.film_index) for r in store.all()})
        wiki_n = sum(1 for r in store.all() if r.site == "wikipedia")
        expect_films = max(len(work_items), 1)
        if plan_films != len(work_items) and len(work_items) > 0:
            print(
                f"Approach 2: plan films={plan_films} != this_run={len(work_items)} — rebuilding"
            )
            existing = 0
        elif len(work_items) > 0 and wiki_n == 0:
            print("Approach 2: no Wikipedia in plan — rebuilding with EN/RU/DE")
            existing = 0

    if existing and resume:
        logger.info("Approach 2 resume: loaded %s requests from plan", existing)
        for req in store.all():
            if req.status == STATUS_RUNNING:
                store.update_fields(
                    req.request_id,
                    status=STATUS_DEFERRED,
                    deferred="yes",
                    deferred_reason="interrupted",
                    error="interrupted running — DEFERRED_TO_END",
                )
            elif req.status == STATUS_FAILED and int(req.attempts or 0) < int(
                req.max_attempts or 2
            ):
                store.update_fields(
                    req.request_id,
                    status=STATUS_DEFERRED,
                    deferred="yes",
                    deferred_reason="resume_failed",
                    error=(req.error or "failed") + " — DEFERRED_TO_END on resume",
                )
            elif req.status == STATUS_RETRY:
                store.update_fields(
                    req.request_id,
                    status=STATUS_DEFERRED,
                    deferred="yes",
                    deferred_reason=req.deferred_reason or "retry",
                )
        print(f"Approach 2: resumed plan with {existing} requests → {plan_dir}")
        n_def = sum(1 for r in store.all() if r.status == STATUS_DEFERRED or r.deferred == "yes")
        print(f"  deferred-to-end requests: {n_def}")
    else:
        src = config.get("sources") or {}
        keys = config.get("api_keys") or {}
        a2cfg = config.get("gather_v2") or {}
        include_wiki = bool(a2cfg.get("include_wikipedia", True))
        sources_enabled = {
            "tmdb": bool(src.get("tmdb", True) and keys.get("tmdb")),
            "omdb": bool(src.get("omdb", True) and keys.get("omdb")),
            "kinopoisk": bool(src.get("kinopoisk", True) and keys.get("kinopoisk")),
            "letterboxd": bool(src.get("letterboxd", True)),
            "wikipedia": include_wiki,
        }
        wiki_langs = list(a2cfg.get("wikipedia_langs") or ["en", "ru", "de"])
        print("Approach 2: building Request Plan for THIS RUN batch")
        print(f"  films this run: {len(work_items)}")
        print(f"  sources: TMDB/OMDb/KP/LB + wikipedia {wiki_langs}")
        # Full sources + wiki for every film in this batch
        requests = build_plan_for_items(
            work_items,
            config=config,
            sources_enabled=sources_enabled,
            film_indices=list(range(1, len(work_items) + 1)),
            full_sources_for_keys=None,  # all films in batch get full sources
            wikipedia_langs=wiki_langs,
        )
        store.replace_all(requests)
        store.write_excel_and_csv()
        print(f"  requests: {len(requests)}")
        print(f"  plan excel: {store.excel_path}")
        print(f"  plan jsonl: {store.jsonl_path}")
        by_site: dict[str, int] = {}
        by_wiki_ep: dict[str, int] = {}
        for r in requests:
            by_site[r.site] = by_site.get(r.site, 0) + 1
            if r.site == "wikipedia":
                by_wiki_ep[r.endpoint_type] = by_wiki_ep.get(r.endpoint_type, 0) + 1
        for s, n in sorted(by_site.items()):
            print(f"    {s}: {n}")
        if by_wiki_ep:
            print("  wikipedia endpoints:")
            for ep, n in sorted(by_wiki_ep.items()):
                print(f"    {ep}: {n}")

    if plan_only:
        store.write_excel_and_csv()
        _write_plan_only_report(
            reports_dir,
            store=store,
            catalog_total=catalog_total,
            a1_done=a1_done,
            a2_pending=len(work_items),
        )
        # Keep inheritance files after plan-only report rewrite
        inherit_paths = materialize_inherited_a1(
            plan_dir=plan_dir,
            reports_dir=reports_dir,
            items=items,
            done_a1_items=done_a1,
            pending_items=pending_items,
            a1_profiles_by_key=a1_profiles,
            a1_checkpoint_path=a1_ckpt,
        )
        print("  Inherited A1 still available at:")
        print(f"    {inherit_paths.get('inherited_jsonl')}")
        print(f"    {inherit_paths.get('inherited_inventory_xlsx')}")
        print(f"    {inherit_paths.get('inherited_report_txt')}")
        return {
            "plan_dir": str(plan_dir),
            "plan_excel": str(store.excel_path),
            "plan_jsonl": str(store.jsonl_path),
            "plan_only": True,
            "request_count": len(store.all()),
            "inherited_a1": a1_done,
            "pending_a2": len(work_items),
            "inherited_jsonl": str(inherit_paths.get("inherited_jsonl", "")),
            "inherited_inventory": str(inherit_paths.get("inherited_inventory_xlsx", "")),
            "merged_checkpoint": str(inherit_paths.get("merged_checkpoint", "")),
            "reports_dir": str(reports_dir),
            "unified_report": str(reports_dir / "UNIFIED_REPORT.txt"),
            "inherited_report": str(reports_dir / "INHERITED_FROM_APPROACH1.txt"),
        }

    delays = _default_site_delays(config)
    timeout = float((config.get("http") or {}).get("timeout_sec", 20))
    progress_path = Path(a2.get("progress_txt") or (plan_dir / "pipeline_progress.txt"))
    excel_every = int(a2.get("excel_every", 40))
    progress_interval = float(a2.get("progress_interval_sec", 3.0))

    print("\nApproach 2: starting independent pipelines")
    for s, d in sorted(delays.items()):
        if any(r.site == s for r in store.all()):
            print(f"  pipeline {s}: delay={d}s")
    print(f"  unified report → {reports_dir / 'UNIFIED_REPORT.txt'}")
    print(f"  per-pipeline   → {reports_dir / 'pipeline_*.txt'}")
    print(f"  progress alias → {progress_path}")

    dbg_path = None
    if request_debug:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        dbg_path = plan_dir / f"request_debug_a2_{ts}.txt"
        dbg_path.write_text(
            "Approach 2 request debug (plan-level)\n"
            f"started: {datetime.now(timezone.utc).isoformat()}\n"
            f"plan: {store.jsonl_path}\n"
            f"inherited_a1: {a1_done}\n"
            f"pending_a2: {len(work_items)}\n"
            "Per-request status: request_plan.xlsx / .jsonl\n"
            "Reports: reports/UNIFIED_REPORT.txt + reports/pipeline_*.txt\n",
            encoding="utf-8",
        )
        print(f"  request-debug → {dbg_path}")

    a2cfg = config.get("gather_v2") or {}
    wiki_adapt = dict(a2cfg.get("wikipedia_adaptive_rpm") or {})
    # Stepwise climb to max_rpm; on 429 roll back to last stable RPM
    wiki_adapt.setdefault("cool_base_sec", 30.0)
    wiki_adapt.setdefault("cool_step_sec", 15.0)
    wiki_adapt.setdefault("decrease_pct", 0.20)
    wiki_adapt.setdefault("increase_pct", 0.0)
    wiki_adapt.setdefault("step_rpm", 10.0)
    wiki_adapt.setdefault("success_batch", 8)
    wiki_adapt.setdefault("min_rpm", 5.0)
    wiki_adapt.setdefault("max_rpm", 200.0)
    wiki_adapt.setdefault("rate_limit_max_attempts", 50)
    # initial RPM from configured delay (e.g. 1.5s → 40 RPM) unless overridden
    if "initial_rpm" not in wiki_adapt:
        d0 = float(delays.get("wikipedia", 1.5))
        wiki_adapt["initial_rpm"] = max(5.0, min(40.0, 60.0 / max(d0, 0.3)))

    executor = Approach2Executor(
        store,
        site_delays=delays,
        timeout_sec=timeout,
        progress_path=progress_path,
        progress_interval_sec=progress_interval,
        excel_every=excel_every,
        progress_kwargs={
            "reports_dir": reports_dir,
            "catalog_total": catalog_total,
            "approach1_done": a1_done,
            "approach2_film_total": len(work_items) or a2_film_total,
            "inherit_from_a1": inherit,
        },
        adaptive_sites={"wikipedia": wiki_adapt},
    )
    print(
        "  wikipedia adaptive RPM: "
        f"start={wiki_adapt['initial_rpm']:.2f}/min  "
        f"step=+{wiki_adapt['step_rpm']:.0f}  max={wiki_adapt['max_rpm']:.0f}  "
        f"every {wiki_adapt['success_batch']} OK; "
        f"on 429: cool {wiki_adapt['cool_base_sec']:.0f}s (+"
        f"{wiki_adapt['cool_step_sec']:.0f}s), ROLL BACK to STABLE_RPM"
    )
    print(f"  adaptive log → {reports_dir / 'adaptive_rpm_wikipedia.txt'}")
    print(f"  live RPM     → {reports_dir / 'CURRENT_RPM_wikipedia.txt'}")

    # --- Periodic profile_a2 Excel snapshots (default every 20 min) ---
    from psychofilm_analyzer.gather_v2.profile_snapshot import ProfileSnapshotWriter

    batch_indices = list(range(1, len(work_items) + 1))
    snap_interval = float(a2cfg.get("profile_snapshot_interval_sec", 1200))

    def _assemble_merged_now() -> list[dict]:
        """Current A2 batch profiles merged with A1 (full catalog table)."""
        a2p = (
            assemble_profiles(work_items, store, film_indices=batch_indices)
            if work_items
            else []
        )
        return merge_profiles_catalog_order(
            items,
            a1_profiles_by_key=a1_profiles,
            a2_profiles=a2p,
            a2_items=work_items,
        )

    snapshotter = ProfileSnapshotWriter(
        assemble_fn=_assemble_merged_now,
        output_dir=out_dir,
        interval_sec=snap_interval,
        prefix="profile_a2",
        write_excel=True,
        log_path=plan_dir / "profile_snapshot.log",
    )
    print(
        f"  profile snapshots every {snap_interval:.0f}s "
        f"({snap_interval / 60.0:.1f} min) → {out_dir}/profile_a2_*.xlsx "
        f"+ profile_a2_live.xlsx"
    )
    snapshotter.start(write_immediately=True)
    try:
        executor.run()
    finally:
        snapshotter.stop(final_write=True)

    print("\nApproach 2: assembling profiles for this batch...")
    a2_profiles = (
        assemble_profiles(work_items, store, film_indices=batch_indices)
        if work_items
        else []
    )
    print("Approach 2: merging with full Approach 1 catalog profiles...")
    inherit_paths = materialize_inherited_a1(
        plan_dir=plan_dir,
        reports_dir=reports_dir,
        items=items,
        done_a1_items=done_a1,
        pending_items=pending_items,
        a1_profiles_by_key=a1_profiles,
        a1_checkpoint_path=a1_ckpt,
        a2_profiles=a2_profiles,
        a2_items=work_items,
    )
    merged = merge_profiles_catalog_order(
        items,
        a1_profiles_by_key=a1_profiles,
        a2_profiles=a2_profiles,
        a2_items=work_items,
    )

    ckpt = Path(a2.get("checkpoint_jsonl") or (plan_dir / "gather_v2_checkpoint.jsonl"))
    # materialize already wrote merged checkpoint; ensure sync
    with ckpt.open("w", encoding="utf-8") as fh:
        for p in merged:
            fh.write(json.dumps(p, ensure_ascii=False) + "\n")

    a2_only_path = plan_dir / "gather_v2_new_profiles.jsonl"
    with a2_only_path.open("w", encoding="utf-8") as fh:
        for p in a2_profiles:
            fh.write(json.dumps(p, ensure_ascii=False) + "\n")

    # Excel for full catalog is heavy; always write JSON, Excel if reasonable
    written = write_profile_dicts(
        merged,
        output_dir=str(out_dir),
        prefix="profile_a2",
        write_excel=len(merged) <= 15000,
    )
    store.write_excel_and_csv()

    counts = store.counts_by_status()
    summary = {
        "approach": 2,
        "inherited_a1": a1_done,
        "pending_a2": len(work_items),
        "a2_new_profiles": len(a2_profiles),
        "merged_profiles": len(merged),
        "plan_dir": str(plan_dir),
        "plan_excel": str(store.excel_path),
        "plan_jsonl": str(store.jsonl_path),
        "progress_txt": str(progress_path),
        "reports_dir": str(reports_dir),
        "unified_report": str(reports_dir / "UNIFIED_REPORT.txt"),
        "inherited_report": str(reports_dir / "INHERITED_FROM_APPROACH1.txt"),
        "inherited_jsonl": str(inherit_paths.get("inherited_jsonl", "")),
        "inherited_inventory": str(inherit_paths.get("inherited_inventory_xlsx", "")),
        "checkpoint_jsonl": str(ckpt),
        "a2_only_jsonl": str(a2_only_path),
        "request_status": counts,
        "exports": {k: str(v) for k, v in written.items()},
        "request_debug": str(dbg_path) if dbg_path else None,
        "approach1_checkpoint": str(a1_ckpt),
    }
    (plan_dir / "run_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"  merged profiles: {len(merged)} (A1={a1_done} + A2_new={len(a2_profiles)})")
    print(f"  inherited jsonl: {inherit_paths.get('inherited_jsonl')}")
    print(f"  inherited xlsx:  {inherit_paths.get('inherited_inventory_xlsx')}")
    print(f"  checkpoint: {ckpt}")
    print(f"  unified report: {reports_dir / 'UNIFIED_REPORT.txt'}")
    print(f"  status: {counts}")
    for k, v in written.items():
        print(f"  {k}: {v}")
    return summary


def _write_idle_report(
    reports_dir: Path,
    *,
    catalog_total: int,
    a1_done: int,
    a1_ckpt: Path,
) -> None:
    reports_dir.mkdir(parents=True, exist_ok=True)
    pct = 100.0 if catalog_total and a1_done >= catalog_total else (
        round(100.0 * a1_done / catalog_total, 2) if catalog_total else 0.0
    )
    text = "\n".join(
        [
            "PsychoFilm UNIFIED REPORT — Approach 2",
            f"updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
            "",
            "ENTIRE GATHERING PROCESS",
            f"  approach1_checkpoint: {a1_ckpt}",
            f"  catalog_total: {catalog_total}",
            f"  approach1_done: {a1_done}",
            f"  approach2_pending: 0",
            f"  CATALOG PROGRESS: {pct}%  (all remaining covered by Approach 1)",
            "",
            "No Approach 2 pipelines started — nothing left to fetch.",
            "",
        ]
    )
    (reports_dir / "UNIFIED_REPORT.txt").write_text(text, encoding="utf-8")


def _write_plan_only_report(
    reports_dir: Path,
    *,
    store: PlanStore,
    catalog_total: int,
    a1_done: int,
    a2_pending: int,
) -> None:
    reports_dir.mkdir(parents=True, exist_ok=True)
    by_site: dict[str, int] = {}
    for r in store.all():
        by_site[r.site] = by_site.get(r.site, 0) + 1
    lines = [
        "PsychoFilm UNIFIED REPORT — Approach 2 (PLAN ONLY)",
        f"updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        "",
        "ENTIRE GATHERING PROCESS",
        f"  catalog_total: {catalog_total}",
        f"  approach1_done: {a1_done}",
        f"  approach2_pending_films: {a2_pending}",
        f"  catalog_pct_if_a1_only: "
        f"{round(100.0 * a1_done / catalog_total, 2) if catalog_total else 0}%",
        "",
        f"REQUEST PLAN: {len(store.all())} requests",
    ]
    for s, n in sorted(by_site.items()):
        lines.append(f"  {s}: {n}")
        (reports_dir / f"pipeline_{s}.txt").write_text(
            f"PIPELINE {s.upper()} — plan only\n"
            f"planned_requests: {n}\n"
            f"status: all pending (not executed)\n",
            encoding="utf-8",
        )
    lines += ["", f"plan_excel: {store.excel_path}", ""]
    (reports_dir / "UNIFIED_REPORT.txt").write_text("\n".join(lines), encoding="utf-8")

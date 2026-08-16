"""Assign fixed per-worker request queues once, before any worker starts.

Sort key (deterministic, resume-stable):
  (film_index, plan_order_index, request_id)

Partition:
  worker_i = film_index % n_workers

All requests for the same film go to the same worker so depends_on /
letterboxd chains stay co-located. No mid-run re-sort or free claim.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from psychofilm_analyzer.gather_v2.models import (
    OPEN_WORK,
    STATUS_DEFERRED,
    STATUS_PENDING,
    STATUS_RETRY,
    PlanRequest,
)
from psychofilm_analyzer.gather_v2.plan_store import PlanStore
from psychofilm_analyzer.utils.localtime import now_str

logger = logging.getLogger(__name__)


def _open_statuses() -> set[str]:
    return {STATUS_PENDING, STATUS_DEFERRED, STATUS_RETRY} | set(OPEN_WORK) - {
        # running should already be reset to deferred on resume; exclude if still present
    }


def assign_site_queues(
    store: PlanStore,
    site: str,
    n_workers: int,
    *,
    endpoint_suffix: Optional[str] = None,
    write_dir: Optional[Path] = None,
) -> list[list[str]]:
    """
    Sort open work for ``site`` once and split into ``n_workers`` fixed queues.

    Returns list of length n_workers; each element is an ordered list of
    request_ids that worker will process (and only those).
    """
    n_workers = max(1, int(n_workers))
    open_st = {STATUS_PENDING, STATUS_DEFERRED, STATUS_RETRY}

    rows: list[tuple[int, int, str, PlanRequest]] = []
    for req in store.by_site(site):
        if req.status not in open_st:
            continue
        if endpoint_suffix:
            ep = (req.endpoint_type or "").lower()
            suf = endpoint_suffix.lower()
            rid = (req.request_id or "").lower()
            if not (
                ep.endswith(suf)
                or ep == f"summary{suf}"
                or ep.endswith(suf.lstrip("_"))
                or rid.endswith(suf)
            ):
                continue
        oi = store.order_index(req.request_id)
        rows.append((int(req.film_index or 0), oi, req.request_id, req))

    # ONE global sort for this site (or lang slice)
    rows.sort(key=lambda t: (t[0], t[1], t[2]))

    queues: list[list[str]] = [[] for _ in range(n_workers)]
    for film_index, _oi, rid, _req in rows:
        w = film_index % n_workers
        queues[w].append(rid)

    if write_dir is not None:
        write_dir = Path(write_dir)
        write_dir.mkdir(parents=True, exist_ok=True)
        for i, q in enumerate(queues):
            path = write_dir / f"{site}_w{i + 1}.ids.txt"
            if endpoint_suffix:
                path = write_dir / f"{site}{endpoint_suffix}_w{i + 1}.ids.txt"
            with path.open("w", encoding="utf-8") as fh:
                fh.write(
                    f"# fixed worker queue  site={site}  worker={i + 1}/{n_workers}\n"
                    f"# sorted once by (film_index, plan_order, request_id)\n"
                    f"# partition: film_index % {n_workers}\n"
                    f"# count={len(q)}  written={now_str()}\n"
                )
                for rid in q:
                    fh.write(rid + "\n")

    sizes = [len(q) for q in queues]
    logger.info(
        "[%s] assigned fixed queues n_workers=%s open=%s sizes=%s (sorted once)",
        site + (endpoint_suffix or ""),
        n_workers,
        sum(sizes),
        sizes,
    )
    return queues

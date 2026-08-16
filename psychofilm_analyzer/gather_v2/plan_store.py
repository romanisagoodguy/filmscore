"""Request plan store: immutable base + append-only event log.

Durability model
----------------
* ``request_plan.jsonl``  — base plan (identity + static fields). Written
  **once** when the plan is built/rebuilt. Never rewritten for status updates.
* ``request_events.jsonl`` — append-only status/result events. One JSON line
  per change. On load, base is replayed with events (last write wins per id).

Reconciliation for Excel / audits is an explicit rare operation
(``reconcile_to_snapshot``), never part of the worker hot path.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from psychofilm_analyzer.gather_v2.models import PLAN_COLUMNS, PlanRequest
from psychofilm_analyzer.utils.localtime import now_str

logger = logging.getLogger(__name__)

# Fields that may change after plan creation (everything else is base-only).
EVENT_FIELDS: tuple[str, ...] = (
    "status",
    "attempts",
    "http_status",
    "duration_ms",
    "error",
    "reproducible_command",
    "deferred",
    "deferred_reason",
    "started_at",
    "finished_at",
    "result_path",
    "result_preview",
    "url",
    "params_json",
    "headers_json",
)


def _event_ts() -> str:
    return now_str(with_ms=True)


class PlanStore:
    """Thread-safe request plan store (append-only status durability)."""

    def __init__(
        self,
        plan_dir: str | Path,
        *,
        # Kept for API compat; full rewrites are no longer used on the hot path.
        flush_every_updates: int = 10_000_000,
        flush_every_sec: float = 86_400.0,
    ):
        self.plan_dir = Path(plan_dir)
        self.plan_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = self.plan_dir / "request_plan.jsonl"
        self.events_path = self.plan_dir / "request_events.jsonl"
        self.excel_path = self.plan_dir / "request_plan.xlsx"
        self.csv_path = self.plan_dir / "request_plan.csv"
        self.snapshot_path = self.plan_dir / "request_plan_snapshot.jsonl"
        self.responses_dir = self.plan_dir / "responses"
        self.responses_dir.mkdir(parents=True, exist_ok=True)
        self.worker_queues_dir = self.plan_dir / "worker_queues"
        self._lock = threading.RLock()
        self._by_id: dict[str, PlanRequest] = {}
        self._order: list[str] = []
        # site -> status -> count  (O(1) progress)
        self._site_status: dict[str, dict[str, int]] = {}
        # site -> request ids in plan order
        self._ids_by_site: dict[str, list[str]] = {}
        # plan_order index for stable sort
        self._order_index: dict[str, int] = {}
        self._event_io_lock = threading.Lock()
        self._events_fh: Optional[Any] = None
        # legacy compat attrs (unused for hot path)
        self._dirty = False
        self._updates_since_flush = 0
        self._flush_every_updates = int(flush_every_updates)
        self._flush_every_sec = float(flush_every_sec)
        self._last_flush_mono = 0.0

    # ------------------------------------------------------------------ index
    def _rebuild_status_index_unlocked(self) -> None:
        self._site_status = {}
        self._ids_by_site = {}
        self._order_index = {}
        for i, rid in enumerate(self._order):
            req = self._by_id.get(rid)
            if not req:
                continue
            self._order_index[rid] = i
            bucket = self._site_status.setdefault(req.site, {})
            bucket[req.status] = bucket.get(req.status, 0) + 1
            self._ids_by_site.setdefault(req.site, []).append(rid)

    def _bump_status_unlocked(
        self, site: str, old_status: Optional[str], new_status: str
    ) -> None:
        if old_status == new_status:
            return
        bucket = self._site_status.setdefault(site, {})
        if old_status:
            n = bucket.get(old_status, 0) - 1
            if n <= 0:
                bucket.pop(old_status, None)
            else:
                bucket[old_status] = n
        bucket[new_status] = bucket.get(new_status, 0) + 1

    def order_index(self, request_id: str) -> int:
        with self._lock:
            return int(self._order_index.get(request_id, 10**12))

    # ------------------------------------------------------------------ load
    def load(self) -> int:
        """Load base plan then replay append-only events (last write wins)."""
        with self._lock:
            self._by_id.clear()
            self._order.clear()
            if not self.jsonl_path.exists():
                self._site_status = {}
                self._ids_by_site = {}
                self._order_index = {}
                return 0
            with self.jsonl_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    req = PlanRequest.from_row(row)
                    self._by_id[req.request_id] = req
                    self._order.append(req.request_id)
            n_base = len(self._order)
            n_events = self._replay_events_unlocked()
            self._rebuild_status_index_unlocked()
            self._dirty = False
            self._updates_since_flush = 0
            self._last_flush_mono = time.monotonic()
            logger.info(
                "PlanStore load: base=%s events_applied≈%s open_sites=%s",
                n_base,
                n_events,
                len(self._site_status),
            )
            return n_base

    def _replay_events_unlocked(self) -> int:
        if not self.events_path.exists():
            return 0
        applied = 0
        with self.events_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rid = ev.get("request_id")
                if not rid or rid not in self._by_id:
                    continue
                req = self._by_id[rid]
                for k in EVENT_FIELDS:
                    if k in ev and hasattr(req, k):
                        setattr(req, k, ev[k])
                applied += 1
        return applied

    def replace_all(self, requests: Iterable[PlanRequest]) -> None:
        """Write a new base plan (plan generation only). Clears event log."""
        with self._lock:
            self._by_id = {}
            self._order = []
            for req in requests:
                self._by_id[req.request_id] = req
                self._order.append(req.request_id)
            self._rebuild_status_index_unlocked()
        # Write base once
        rows = [self._by_id[i].to_row() for i in self._order if i in self._by_id]
        tmp = self.jsonl_path.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        tmp.replace(self.jsonl_path)
        # Fresh event log for this plan generation
        if self.events_path.exists():
            bak = self.events_path.with_suffix(
                f".jsonl.bak_{int(time.time())}"
            )
            try:
                self.events_path.replace(bak)
            except OSError:
                self.events_path.write_text("", encoding="utf-8")
        else:
            self.events_path.write_text("", encoding="utf-8")
        logger.info(
            "PlanStore replace_all: wrote base plan rows=%s → %s (events reset)",
            len(rows),
            self.jsonl_path,
        )

    # ------------------------------------------------------------------ read
    def get(self, request_id: str) -> Optional[PlanRequest]:
        with self._lock:
            return self._by_id.get(request_id)

    def all(self) -> list[PlanRequest]:
        with self._lock:
            return [self._by_id[i] for i in self._order if i in self._by_id]

    def by_site(self, site: str) -> list[PlanRequest]:
        """O(site size) via per-site id index."""
        with self._lock:
            ids = self._ids_by_site.get(site) or []
            out: list[PlanRequest] = []
            for rid in ids:
                req = self._by_id.get(rid)
                if req is not None:
                    out.append(req)
            return out

    def sites(self) -> list[str]:
        with self._lock:
            if not self._site_status and self._order:
                self._rebuild_status_index_unlocked()
            if self._site_status:
                return list(self._site_status.keys())
            if self._ids_by_site:
                return list(self._ids_by_site.keys())
            return list(
                dict.fromkeys(
                    self._by_id[i].site for i in self._order if i in self._by_id
                )
            )

    def counts_by_status(self, site: Optional[str] = None) -> dict[str, int]:
        with self._lock:
            if not self._site_status and self._order:
                self._rebuild_status_index_unlocked()
            if site:
                return dict(self._site_status.get(site) or {})
            out: dict[str, int] = {}
            for bucket in self._site_status.values():
                for st, n in bucket.items():
                    out[st] = out.get(st, 0) + n
            return out

    def counts_by_site_and_status(self) -> dict[str, dict[str, int]]:
        with self._lock:
            if not self._site_status and self._order:
                self._rebuild_status_index_unlocked()
            return {s: dict(b) for s, b in self._site_status.items()}

    # ------------------------------------------------------------------ write (append events)
    def _append_event_unlocked(
        self,
        request_id: str,
        fields: dict[str, Any],
        *,
        worker_id: int = 0,
        site: str = "",
    ) -> None:
        """Build event payload from fields (caller holds data lock briefly)."""
        ev: dict[str, Any] = {
            "v": 1,
            "ts": _event_ts(),
            "request_id": request_id,
        }
        if worker_id:
            ev["worker_id"] = int(worker_id)
        if site:
            ev["site"] = site
        for k in EVENT_FIELDS:
            if k in fields:
                ev[k] = fields[k]
        line = json.dumps(ev, ensure_ascii=False) + "\n"
        # Disk append outside data-path critical work: caller releases lock first ideally
        with self._event_io_lock:
            with self.events_path.open("a", encoding="utf-8") as fh:
                fh.write(line)

    def update(self, req: PlanRequest) -> None:
        """Replace full request object in memory and append event for mutables."""
        with self._lock:
            old = self._by_id.get(req.request_id)
            old_st = old.status if old else None
            old_site = old.site if old else None
            self._by_id[req.request_id] = req
            if req.request_id not in self._order:
                self._order.append(req.request_id)
                self._order_index[req.request_id] = len(self._order) - 1
                self._ids_by_site.setdefault(req.site, []).append(req.request_id)
            if old is None:
                self._bump_status_unlocked(req.site, None, req.status)
            elif old_site != req.site:
                if old_st and old_site:
                    b = self._site_status.setdefault(old_site, {})
                    n = b.get(old_st, 0) - 1
                    if n <= 0:
                        b.pop(old_st, None)
                    else:
                        b[old_st] = n
                    old_ids = self._ids_by_site.get(old_site) or []
                    self._ids_by_site[old_site] = [
                        i for i in old_ids if i != req.request_id
                    ]
                self._bump_status_unlocked(req.site, None, req.status)
                self._ids_by_site.setdefault(req.site, []).append(req.request_id)
            else:
                self._bump_status_unlocked(req.site, old_st, req.status)
            fields = {k: getattr(req, k) for k in EVENT_FIELDS if hasattr(req, k)}
            site = req.site
            rid = req.request_id
        self._append_event_unlocked(rid, fields, site=site)

    def update_fields(
        self,
        request_id: str,
        *,
        worker_id: int = 0,
        **fields: Any,
    ) -> Optional[PlanRequest]:
        """Update mutable fields in memory and append one event line."""
        with self._lock:
            req = self._by_id.get(request_id)
            if not req:
                return None
            old_st = req.status
            applied: dict[str, Any] = {}
            for k, v in fields.items():
                if hasattr(req, k):
                    setattr(req, k, v)
                    if k in EVENT_FIELDS:
                        applied[k] = v
            if "status" in fields and fields["status"] != old_st:
                self._bump_status_unlocked(req.site, old_st, req.status)
            site = req.site
        if applied:
            self._append_event_unlocked(
                request_id, applied, worker_id=worker_id, site=site
            )
        return req

    def claim_if(
        self,
        request_id: str,
        *,
        from_statuses: set[str],
        to_status: str,
        worker_id: int = 0,
        **fields: Any,
    ) -> Optional[PlanRequest]:
        """Compare-and-set status (kept for compatibility; prefer fixed queues)."""
        with self._lock:
            req = self._by_id.get(request_id)
            if not req or req.status not in from_statuses:
                return None
            old_st = req.status
            req.status = to_status
            applied: dict[str, Any] = {"status": to_status}
            for k, v in fields.items():
                if hasattr(req, k):
                    setattr(req, k, v)
                    if k in EVENT_FIELDS:
                        applied[k] = v
            self._bump_status_unlocked(req.site, old_st, to_status)
            site = req.site
        self._append_event_unlocked(
            request_id, applied, worker_id=worker_id, site=site
        )
        return req

    # ------------------------------------------------------------------ responses
    def save_response(self, request_id: str, data: Any) -> Path:
        path = self.responses_dir / f"{request_id}.json"
        path.write_text(
            json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8"
        )
        return path

    def load_response(self, request_id: str) -> Any:
        path = self.responses_dir / f"{request_id}.json"
        if not path.exists():
            req = self.get(request_id)
            if req and req.result_path:
                path = Path(req.result_path)
            if not path.exists():
                return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    # ------------------------------------------------------------------ rare export
    def reconcile_to_snapshot(self) -> Path:
        """Write reconciled full rows to snapshot JSONL (rare; not hot path)."""
        with self._lock:
            rows = [self._by_id[i].to_row() for i in self._order if i in self._by_id]
        tmp = self.snapshot_path.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        tmp.replace(self.snapshot_path)
        logger.info(
            "PlanStore reconcile_to_snapshot: rows=%s → %s",
            len(rows),
            self.snapshot_path,
        )
        return self.snapshot_path

    def write_excel_and_csv(self) -> None:
        """Rare export from in-memory reconciled state (never rewrites base plan)."""
        with self._lock:
            rows = [self._by_id[i].to_row() for i in self._order if i in self._by_id]
        try:
            import pandas as pd

            df = pd.DataFrame(rows, columns=PLAN_COLUMNS)
            df.to_csv(self.csv_path, index=False, encoding="utf-8-sig")
            for col in ("params_json", "headers_json", "result_preview", "error", "url"):
                if col in df.columns:
                    df[col] = df[col].astype(str).str.slice(0, 32000)
            df.to_excel(self.excel_path, index=False, sheet_name="request_plan")
        except Exception:
            import csv

            with self.csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=PLAN_COLUMNS, extrasaction="ignore")
                w.writeheader()
                for r in rows:
                    w.writerow(r)

    def flush(self, *, force: bool = False) -> None:
        """No-op for base plan. Status durability is append-only events.

        Kept so callers that still invoke flush() do not rewrite 220MB.
        Use ``reconcile_to_snapshot()`` if a full materialization is needed.
        """
        if force:
            logger.debug(
                "PlanStore.flush(force=True) ignored for base plan "
                "(append-only events; use reconcile_to_snapshot for export)"
            )
        return

    def _maybe_flush(self) -> None:
        """No-op — hot path never rewrites the base plan."""
        return

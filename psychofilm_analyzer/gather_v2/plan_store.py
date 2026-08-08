"""Persist Request Plan as JSONL (source of truth) + Excel snapshot."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Iterable, Optional

from psychofilm_analyzer.gather_v2.models import PLAN_COLUMNS, PlanRequest


class PlanStore:
    """Thread-safe request plan store."""

    def __init__(self, plan_dir: str | Path):
        self.plan_dir = Path(plan_dir)
        self.plan_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = self.plan_dir / "request_plan.jsonl"
        self.excel_path = self.plan_dir / "request_plan.xlsx"
        self.csv_path = self.plan_dir / "request_plan.csv"
        self.responses_dir = self.plan_dir / "responses"
        self.responses_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._by_id: dict[str, PlanRequest] = {}
        self._order: list[str] = []

    def load(self) -> int:
        with self._lock:
            self._by_id.clear()
            self._order.clear()
            if not self.jsonl_path.exists():
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
            return len(self._order)

    def replace_all(self, requests: Iterable[PlanRequest]) -> None:
        with self._lock:
            self._by_id = {}
            self._order = []
            for req in requests:
                self._by_id[req.request_id] = req
                self._order.append(req.request_id)
            self._flush_jsonl_unlocked()

    def get(self, request_id: str) -> Optional[PlanRequest]:
        with self._lock:
            return self._by_id.get(request_id)

    def all(self) -> list[PlanRequest]:
        with self._lock:
            return [self._by_id[i] for i in self._order if i in self._by_id]

    def by_site(self, site: str) -> list[PlanRequest]:
        with self._lock:
            return [
                self._by_id[i]
                for i in self._order
                if i in self._by_id and self._by_id[i].site == site
            ]

    def update(self, req: PlanRequest) -> None:
        with self._lock:
            self._by_id[req.request_id] = req
            if req.request_id not in self._order:
                self._order.append(req.request_id)
            self._flush_jsonl_unlocked()

    def update_fields(self, request_id: str, **fields: Any) -> Optional[PlanRequest]:
        with self._lock:
            req = self._by_id.get(request_id)
            if not req:
                return None
            for k, v in fields.items():
                if hasattr(req, k):
                    setattr(req, k, v)
            self._flush_jsonl_unlocked()
            return req

    def counts_by_status(self, site: Optional[str] = None) -> dict[str, int]:
        with self._lock:
            out: dict[str, int] = {}
            for rid in self._order:
                req = self._by_id.get(rid)
                if not req:
                    continue
                if site and req.site != site:
                    continue
                out[req.status] = out.get(req.status, 0) + 1
            return out

    def sites(self) -> list[str]:
        with self._lock:
            seen = []
            for rid in self._order:
                s = self._by_id[rid].site
                if s not in seen:
                    seen.append(s)
            return seen

    def save_response(self, request_id: str, data: Any) -> Path:
        path = self.responses_dir / f"{request_id}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")
        return path

    def load_response(self, request_id: str) -> Any:
        path = self.responses_dir / f"{request_id}.json"
        if not path.exists():
            # try from plan result_path
            req = self.get(request_id)
            if req and req.result_path:
                path = Path(req.result_path)
            if not path.exists():
                return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def write_excel_and_csv(self) -> None:
        with self._lock:
            rows = [self._by_id[i].to_row() for i in self._order if i in self._by_id]
        try:
            import pandas as pd

            df = pd.DataFrame(rows, columns=PLAN_COLUMNS)
            df.to_csv(self.csv_path, index=False, encoding="utf-8-sig")
            # Truncate long text for Excel
            for col in ("params_json", "headers_json", "result_preview", "error", "url"):
                if col in df.columns:
                    df[col] = df[col].astype(str).str.slice(0, 32000)
            df.to_excel(self.excel_path, index=False, sheet_name="request_plan")
        except Exception:
            # CSV-only fallback
            import csv

            with self.csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=PLAN_COLUMNS, extrasaction="ignore")
                w.writeheader()
                for r in rows:
                    w.writerow(r)

    def _flush_jsonl_unlocked(self) -> None:
        tmp = self.jsonl_path.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for rid in self._order:
                req = self._by_id.get(rid)
                if not req:
                    continue
                fh.write(json.dumps(req.to_row(), ensure_ascii=False) + "\n")
        tmp.replace(self.jsonl_path)

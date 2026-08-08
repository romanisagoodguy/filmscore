"""Request Plan models for Approach 2 gather."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_RETRY = "retry"
# Failed once: marked and held until the main queue is empty, then processed last
STATUS_DEFERRED = "deferred"

TERMINAL_OK = {STATUS_SUCCESS}
TERMINAL_BAD = {STATUS_FAILED, STATUS_SKIPPED}
BLOCKING_DONE = TERMINAL_OK | TERMINAL_BAD
# Work that is not finished yet (including end-of-queue retries)
OPEN_WORK = {STATUS_PENDING, STATUS_RUNNING, STATUS_RETRY, STATUS_DEFERRED}

PLAN_COLUMNS = [
    "request_id",
    "film_index",
    "excel_row",
    "excel_sheet",
    "film_title",
    "year",
    "english_title",
    "site",
    "endpoint_type",
    "method",
    "url",
    "params_json",
    "headers_json",
    "depends_on",
    "status",
    "attempts",
    "max_attempts",
    "http_status",
    "duration_ms",
    "error",
    "reproducible_command",  # full python -c one-liner to replay this request
    "deferred",  # "yes" if marked for end-of-queue retry
    "deferred_reason",  # short reason (429, 404, 5xx, exception)
    "started_at",
    "finished_at",
    "result_path",
    "result_preview",
    "resolve_hint",  # how to fill URL from parent (e.g. tmdb_id_from_search)
]


@dataclass
class PlanRequest:
    request_id: str
    film_index: int
    excel_row: Optional[int] = None
    excel_sheet: Optional[str] = None
    film_title: str = ""
    year: Optional[int] = None
    english_title: Optional[str] = None
    site: str = ""
    endpoint_type: str = ""
    method: str = "GET"
    url: str = ""
    params_json: str = "{}"
    headers_json: str = "{}"
    depends_on: str = ""  # comma-separated request_ids
    status: str = STATUS_PENDING
    attempts: int = 0
    max_attempts: int = 2
    http_status: Optional[int] = None
    duration_ms: Optional[float] = None
    error: str = ""
    reproducible_command: str = ""
    deferred: str = ""  # "yes" when moved to end-of-queue
    deferred_reason: str = ""
    started_at: str = ""
    finished_at: str = ""
    result_path: str = ""
    result_preview: str = ""
    resolve_hint: str = ""

    def dep_ids(self) -> list[str]:
        if not self.depends_on or not str(self.depends_on).strip():
            return []
        return [x.strip() for x in str(self.depends_on).split(",") if x.strip()]

    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        return {k: d.get(k, "") for k in PLAN_COLUMNS}

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "PlanRequest":
        kwargs: dict[str, Any] = {}
        for k in PLAN_COLUMNS:
            if k not in row:
                continue
            v = row[k]
            if k in {"film_index", "excel_row", "attempts", "max_attempts", "http_status"}:
                if v is None or v == "":
                    kwargs[k] = None if k in {"excel_row", "http_status"} else 0
                else:
                    try:
                        kwargs[k] = int(float(v))
                    except (TypeError, ValueError):
                        kwargs[k] = 0 if k != "http_status" else None
            elif k == "duration_ms":
                if v is None or v == "":
                    kwargs[k] = None
                else:
                    try:
                        kwargs[k] = float(v)
                    except (TypeError, ValueError):
                        kwargs[k] = None
            elif k == "year":
                if v is None or v == "":
                    kwargs[k] = None
                else:
                    try:
                        kwargs[k] = int(float(v))
                    except (TypeError, ValueError):
                        kwargs[k] = None
            else:
                kwargs[k] = "" if v is None else str(v)
        if "request_id" not in kwargs:
            raise ValueError("missing request_id")
        return cls(**kwargs)  # type: ignore[arg-type]


@dataclass
class PipelineStats:
    site: str
    pending: int = 0
    running: int = 0
    success: int = 0
    failed: int = 0
    skipped: int = 0
    retry: int = 0
    completed: int = 0
    total: int = 0
    rpm: float = 0.0
    eta_sec: Optional[float] = None
    last_activity: str = ""
    last_request_id: str = ""
    delay_sec: float = 0.0

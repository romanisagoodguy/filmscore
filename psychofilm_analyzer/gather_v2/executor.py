"""Independent per-site pipeline executor for Approach 2."""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional

import requests

from psychofilm_analyzer.utils.localtime import now_str, stamp_local
from psychofilm_analyzer.gather_v2.adaptive_rpm import AdaptiveRpmController
from psychofilm_analyzer.gather_v2.errors import (
    is_recoverable,
    normalize_reason,
)
from psychofilm_analyzer.gather_v2.models import (
    OPEN_WORK,
    STATUS_DEFERRED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RETRY,
    STATUS_RUNNING,
    STATUS_SKIPPED,
    STATUS_SUCCESS,
    PlanRequest,
)
from psychofilm_analyzer.gather_v2.plan_store import PlanStore
from psychofilm_analyzer.gather_v2.progress import ProgressReporter
from psychofilm_analyzer.gather_v2.resolver import deps_ready, resolve_request

logger = logging.getLogger(__name__)


def _now() -> str:
    return now_str(with_ms=True)


def _py_lit(obj: Any) -> str:
    """Python literal safe inside: python -c \"...\" on PowerShell."""
    if obj is None:
        return "None"
    if isinstance(obj, bool):
        return "True" if obj else "False"
    if isinstance(obj, (int, float)) and not isinstance(obj, bool):
        return repr(obj)
    if isinstance(obj, str):
        if "'" not in obj:
            return "'" + obj + "'"
        if '"' not in obj:
            return '"' + obj + '"'
        return repr(obj)
    if isinstance(obj, dict):
        inner = ", ".join(f"{_py_lit(k)}: {_py_lit(v)}" for k, v in obj.items())
        return "{" + inner + "}"
    if isinstance(obj, (list, tuple)):
        return "[" + ", ".join(_py_lit(v) for v in obj) + "]"
    return repr(obj)


def build_reproducible_command(
    *,
    method: str = "GET",
    url: str,
    params: Optional[dict[str, Any]] = None,
    headers: Optional[dict[str, str]] = None,
    timeout: float = 20.0,
) -> str:
    """Full copy-paste command that replays the exact HTTP call."""
    h = dict(headers or {})
    p = dict(params or {})
    # Prefer single-quoted Python strings so PowerShell outer "..." works
    if method.upper() != "GET":
        return (
            f'python -c "import requests; r=requests.request({_py_lit(method.upper())}, '
            f'{_py_lit(url)}, headers={_py_lit(h)}'
            f'{(", params=" + _py_lit(p)) if p else ""}, timeout={timeout}); '
            f'print(r.status_code); print(r.text[:500])"'
        )
    return (
        f'python -c "import requests; r=requests.get({_py_lit(url)}, '
        f'headers={_py_lit(h)}'
        f'{(", params=" + _py_lit(p)) if p else ""}, timeout={timeout}); '
        f'print(r.status_code); print(r.text[:500])"'
    )


class SitePipeline(threading.Thread):
    def __init__(
        self,
        site: str,
        store: PlanStore,
        *,
        delay_sec: float = 0.35,
        timeout_sec: float = 20.0,
        max_attempts: int = 2,
        progress: Optional[ProgressReporter] = None,
        stop_event: Optional[threading.Event] = None,
        excel_every: int = 50,
        adaptive: Optional[AdaptiveRpmController] = None,
        # For 429: never permanent-fail until many attempts (request is paused/retried)
        rate_limit_max_attempts: int = 50,
        error_commands_path: Optional[str | Path] = None,
        worker_id: int = 0,
        # Approach 3: only claim rows matching this endpoint suffix filter, e.g. "_en"
        endpoint_suffix: Optional[str] = None,
        # Approach 3: never end-queue terminal errors
        recoverable_only_retry: bool = True,
        # Shared cool-down map for multi-worker wikipedia (request_id -> mono)
        shared_not_before: Optional[dict[str, float]] = None,
        shared_not_before_lock: Optional[threading.Lock] = None,
    ):
        wid = f"-w{worker_id}" if worker_id else ""
        suf = endpoint_suffix or ""
        super().__init__(name=f"pipeline-{site}{suf}{wid}", daemon=True)
        self.site = site
        self.store = store
        self.delay_sec = delay_sec
        self.timeout_sec = timeout_sec
        self.max_attempts = max_attempts
        self.rate_limit_max_attempts = rate_limit_max_attempts
        self.progress = progress
        self.stop_event = stop_event or threading.Event()
        self.excel_every = excel_every
        self.adaptive = adaptive
        self.error_commands_path = Path(error_commands_path) if error_commands_path else None
        self.worker_id = int(worker_id or 0)
        self.endpoint_suffix = (endpoint_suffix or "").lower() or None
        self.recoverable_only_retry = bool(recoverable_only_retry)
        self.session = requests.Session()
        self._done = 0
        self._last_req = 0.0
        self._cmd_log_lock = threading.Lock()
        # request_id -> monotonic time when it may be claimed again (after 429/park)
        self._not_before_lock = shared_not_before_lock or threading.Lock()
        self._not_before: dict[str, float] = (
            shared_not_before if shared_not_before is not None else {}
        )
        # round-robin so one bad request does not monopolize the queue
        self._claim_offset = int(worker_id or 0)

    def run(self) -> None:
        if self.adaptive:
            logger.info(
                "Pipeline %s started ADAPTIVE rpm=%.3f delay=%.2fs "
                "(bad requests are parked; queue continues)",
                self.site,
                self.adaptive.current_rpm(),
                self.adaptive.current_delay_sec(),
            )
        else:
            logger.info(
                "Pipeline %s started (delay=%.2fs; bad requests do not block others)",
                self.site,
                self.delay_sec,
            )
        idle_rounds = 0
        end_queue_announced = False
        while not self.stop_event.is_set():
            req = self._claim_next()
            if req is None:
                idle_rounds += 1
                counts = self.store.counts_by_status(self.site)
                left = sum(counts.get(s, 0) for s in OPEN_WORK)
                if left == 0:
                    logger.info("Pipeline %s finished (no work left)", self.site)
                    break
                # Main queue empty but deferred/retry remain?
                main_left = counts.get(STATUS_PENDING, 0) + counts.get(STATUS_RUNNING, 0)
                end_left = counts.get(STATUS_DEFERRED, 0) + counts.get(STATUS_RETRY, 0)
                if main_left == 0 and end_left > 0 and not end_queue_announced:
                    logger.info(
                        "[%s] MAIN queue empty — processing %s deferred/failing requests at END",
                        self.site,
                        end_left,
                    )
                    end_queue_announced = True
                wait_for = self._seconds_until_next_claimable()
                if wait_for is not None and wait_for > 0:
                    logger.info(
                        "[%s] end-queue cooling — wait %.1fs then retry deferred",
                        self.site,
                        wait_for,
                    )
                    self.stop_event.wait(min(wait_for + 0.05, 30.0))
                else:
                    self.stop_event.wait(0.25 if idle_rounds < 20 else 1.0)
                continue
            idle_rounds = 0
            self._throttle()
            self._execute(req)
            self._done += 1
            if self.progress:
                self.progress.note_completion(
                    self.site, request_id=getattr(req, "request_id", "") or ""
                )
                if self.adaptive and hasattr(self.progress, "set_adaptive_snapshot"):
                    try:
                        self.progress.set_adaptive_snapshot(self.site, self.adaptive.snapshot())
                    except Exception:
                        pass
            if self.excel_every and self._done % self.excel_every == 0:
                try:
                    self.store.write_excel_and_csv()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("plan excel write failed: %s", exc)
        logger.info("Pipeline %s stopped", self.site)

    def _throttle(self) -> None:
        if self.adaptive:
            self.adaptive.wait_turn(self.stop_event)
            return
        elapsed = time.monotonic() - self._last_req
        if elapsed < self.delay_sec:
            time.sleep(self.delay_sec - elapsed)

    def _seconds_until_next_claimable(self) -> Optional[float]:
        """If end-queue items are parked, return seconds until the soonest is ready."""
        now = time.monotonic()
        waits: list[float] = []
        with self._not_before_lock:
            nb_map = dict(self._not_before)
        for req in self.store.by_site(self.site):
            if not self._matches_endpoint_filter(req):
                continue
            if req.status not in (STATUS_RETRY, STATUS_DEFERRED):
                continue
            if self.recoverable_only_retry and not is_recoverable(
                req.deferred_reason or "",
                http_status=req.http_status,
                error=req.error or "",
            ):
                continue
            nb = nb_map.get(req.request_id, 0.0)
            if nb > now:
                waits.append(nb - now)
        if not waits:
            return None
        return min(waits)

    def _main_queue_empty(self) -> bool:
        """True when no fresh PENDING work remains for this worker filter."""
        for req in self.store.by_site(self.site):
            if not self._matches_endpoint_filter(req):
                continue
            if req.status == STATUS_PENDING:
                return False
            if req.status == STATUS_RUNNING:
                return False
        return True

    def _ordered_candidates(self) -> list[PlanRequest]:
        """
        Two phases:
          1) MAIN: only PENDING (never-failed fresh work)
          2) END: only when main empty — DEFERRED / RETRY that are recoverable
             and past cool-down. Terminal errors are never re-queued.
        """
        now = time.monotonic()
        all_site = [
            r for r in self.store.by_site(self.site) if self._matches_endpoint_filter(r)
        ]
        if not all_site:
            return []
        n = len(all_site)
        start = self._claim_offset % n
        rotated = all_site[start:] + all_site[:start]
        self._claim_offset = (self._claim_offset + 1) % max(n, 1)

        with self._not_before_lock:
            nb_map = dict(self._not_before)

        pending: list[PlanRequest] = []
        end_ready: list[PlanRequest] = []
        for req in rotated:
            if req.status == STATUS_PENDING:
                pending.append(req)
            elif req.status in (STATUS_DEFERRED, STATUS_RETRY):
                # Approach 3: only recoverable reasons re-enter the queue
                if self.recoverable_only_retry and not is_recoverable(
                    req.deferred_reason or "",
                    http_status=req.http_status,
                    error=req.error or "",
                ):
                    # Convert false deferred → terminal skipped once
                    self.store.update_fields(
                        req.request_id,
                        status=STATUS_SKIPPED,
                        deferred="",
                        deferred_reason=normalize_reason(
                            req.deferred_reason or "",
                            http_status=req.http_status,
                            error=req.error or "",
                        ),
                        error=(req.error or "") + " | converted to TERMINAL (non-recoverable)",
                        finished_at=_now(),
                    )
                    continue
                nb = nb_map.get(req.request_id, 0.0)
                if now >= nb:
                    end_ready.append(req)

        # Phase 1: main queue only
        if pending:
            pending.sort(key=lambda r: (r.film_index, r.request_id))
            return pending
        # Phase 2: recoverable end-queue only
        if end_ready:
            end_ready.sort(key=lambda r: (int(r.attempts or 0), r.request_id))
            return end_ready
        return []

    def _park_until(self, request_id: str, park_sec: float) -> None:
        if park_sec <= 0:
            return
        with self._not_before_lock:
            self._not_before[request_id] = time.monotonic() + park_sec

    def _is_parked(self, request_id: str) -> bool:
        with self._not_before_lock:
            nb = self._not_before.get(request_id, 0.0)
        return time.monotonic() < nb

    def _seconds_until_unpark(self, request_id: str) -> float:
        with self._not_before_lock:
            nb = self._not_before.get(request_id, 0.0)
        return max(0.0, nb - time.monotonic())

    def _mark_deferred(
        self,
        req: PlanRequest,
        *,
        reason: str,
        error: str,
        http_status: Any = None,
        duration_ms: float = 0.0,
        repro_cmd: str = "",
        preview: str = "",
        park_sec: float = 0.0,
        permanent: bool = False,
    ) -> None:
        """
        End-queue only for recoverable errors (Approach 3).
        Terminal errors (404, not_found, empty search, 4xx) → skipped/failed, never retry.
        """
        attempts = int(req.attempts or 1)
        max_a = int(req.max_attempts or self.max_attempts)
        norm = normalize_reason(reason, http_status=http_status, error=error)
        recoverable = is_recoverable(norm, http_status=http_status, error=error)

        if self.adaptive and norm in {"rate_limit", "429", "5xx", "exception", "timeout"}:
            max_a = max(self.rate_limit_max_attempts, max_a)

        # Terminal or permanent → never end-queue
        if permanent or not recoverable:
            st = STATUS_SKIPPED if norm in {
                "not_found",
                "omdb_not_found",
                "kp_search_empty",
                "http_400",
                "http_404",
            } else STATUS_FAILED
            note = f"TERMINAL ({norm}): {error} — will NOT retry at end-queue"
            mark = ""
            self.store.update_fields(
                req.request_id,
                status=st,
                http_status=http_status if http_status is not None else req.http_status,
                duration_ms=round(duration_ms, 1),
                error=note if not repro_cmd else f"{note} | REPRO: {repro_cmd}",
                reproducible_command=repro_cmd or getattr(req, "reproducible_command", "") or "",
                deferred="",
                deferred_reason=norm,
                finished_at=_now(),
                result_preview=(preview or "")[:200],
            )
            logger.info(
                "[%s] TERMINAL %s reason=%s status=%s (no end-queue)",
                self.site,
                req.request_id,
                norm,
                st,
            )
            return

        if attempts >= max_a:
            st = STATUS_FAILED
            mark = "yes"
            note = f"RECOVERABLE_EXHAUSTED after {attempts} attempts ({norm}): {error}"
        elif self.recoverable_only_retry:
            st = STATUS_DEFERRED
            mark = "yes"
            note = (
                f"DEFERRED_TO_END ({norm}): {error} — recoverable; "
                f"retry only after main queue"
            )
        else:
            st = STATUS_DEFERRED
            mark = "yes"
            note = f"DEFERRED_TO_END ({norm}): {error} — will retry after main queue"

        if park_sec > 0 and st == STATUS_DEFERRED:
            self._park_until(req.request_id, park_sec)
            note += f" | parked {park_sec:.0f}s before end-queue retry"

        if repro_cmd and "REPRO:" not in note:
            note = f"{note} | REPRO: {repro_cmd}"

        self.store.update_fields(
            req.request_id,
            status=st,
            http_status=http_status if http_status is not None else req.http_status,
            duration_ms=round(duration_ms, 1),
            error=note,
            reproducible_command=repro_cmd or getattr(req, "reproducible_command", "") or "",
            deferred=mark,
            deferred_reason=norm,
            finished_at=_now(),
            result_preview=(preview or "")[:200],
        )
        logger.info(
            "[%s] marked %s deferred=%s reason=%s status=%s",
            self.site,
            req.request_id,
            mark,
            norm,
            st,
        )

    def _matches_endpoint_filter(self, req: PlanRequest) -> bool:
        if not self.endpoint_suffix:
            return True
        ep = (req.endpoint_type or "").lower()
        suf = self.endpoint_suffix
        if ep.endswith(suf) or ep == f"summary{suf}" or ep.endswith(suf.lstrip("_")):
            return True
        # also match _en in request_id
        if (req.request_id or "").lower().endswith(suf):
            return True
        return False

    def _claim_next(self) -> Optional[PlanRequest]:
        for req in self._ordered_candidates():
            if not self._matches_endpoint_filter(req):
                continue
            if not deps_ready(self.store, req):
                continue
            ok, err = resolve_request(self.store, req)
            if not ok:
                if "not finished" in err or "not finished" in (err or ""):
                    continue
                if "dependency" in err and "failed" in err:
                    self.store.update_fields(
                        req.request_id,
                        status=STATUS_SKIPPED,
                        error=err,
                        deferred_reason="dependency_failed",
                        finished_at=_now(),
                    )
                    continue
                if "earlier letterboxd" in err:
                    self.store.update_fields(
                        req.request_id,
                        status=STATUS_SKIPPED,
                        error=err,
                        deferred_reason="not_found",
                        finished_at=_now(),
                    )
                    continue
                if "empty" in err or "no " in err:
                    self.store.update_fields(
                        req.request_id,
                        status=STATUS_SKIPPED,
                        error=err,
                        finished_at=_now(),
                    )
                    continue
                self.store.update_fields(
                    req.request_id,
                    status=STATUS_SKIPPED,
                    error=err,
                    finished_at=_now(),
                )
                continue
            # Atomic claim — required for parallel wikipedia workers
            claimed = self.store.claim_if(
                req.request_id,
                from_statuses={STATUS_PENDING, STATUS_DEFERRED, STATUS_RETRY},
                to_status=STATUS_RUNNING,
                started_at=_now(),
                url=req.url,
                params_json=req.params_json,
                attempts=int(req.attempts or 0) + 1,
            )
            if claimed is None:
                continue  # another worker won the race
            return claimed
        return None

    def _execute(self, req: PlanRequest) -> None:
        # Wikipedia: multi-step search + 1:1 command/error attribution
        if req.site == "wikipedia":
            self._execute_wikipedia(req)
            return

        try:
            params = json.loads(req.params_json or "{}")
        except json.JSONDecodeError:
            params = {}
        try:
            headers = json.loads(req.headers_json or "{}")
        except json.JSONDecodeError:
            headers = {}
        params = {k: v for k, v in params.items() if v is not None and v != ""}
        url = req.url
        repro = build_reproducible_command(
            method=req.method or "GET",
            url=url or "",
            params=params,
            headers=headers,
            timeout=self.timeout_sec,
        )
        if not url:
            self.store.update_fields(
                req.request_id,
                status=STATUS_FAILED,
                error="empty url",
                reproducible_command=repro,
                finished_at=_now(),
            )
            self._last_req = time.monotonic()
            return

        if self.adaptive:
            self.adaptive.mark_request_started()
        t0 = time.monotonic()
        try:
            resp = self.session.request(
                req.method or "GET",
                url,
                params=params or None,
                headers=headers or None,
                timeout=self.timeout_sec,
            )
            ms = (time.monotonic() - t0) * 1000.0
            self._last_req = time.monotonic()
            body_text = ""
            try:
                body_text = resp.text or ""
            except Exception:
                body_text = ""
            preview = body_text[:400]
            data: Any
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "json" in ctype or body_text[:1] in ("{", "["):
                try:
                    data = resp.json()
                except Exception:
                    data = {"_raw": body_text[:5000]}
            else:
                data = {"_html": body_text[:20000], "_content_type": ctype}

            if resp.status_code == 429:
                self._handle_429(req, ms=ms, preview=preview, repro_cmd=repro)
                return

            if resp.status_code >= 500:
                err5 = f"server {resp.status_code}"
                self._mark_deferred(
                    req,
                    reason="5xx",
                    error=err5,
                    http_status=resp.status_code,
                    duration_ms=ms,
                    repro_cmd=repro,
                    preview=preview,
                    park_sec=5.0,
                )
                self._append_error_command(
                    req, status_code=resp.status_code, err=err5, repro_cmd=repro
                )
                if self.adaptive:
                    self.adaptive.on_other_result(
                        ok=False, request_id=req.request_id, http_status=resp.status_code
                    )
                return

            # Soft / business outcomes (HTTP may still be 200)
            omdb_not_found = (
                req.site == "omdb"
                and isinstance(data, dict)
                and data.get("Response") == "False"
            )
            lb_not_found = False
            lb_cf = False
            if req.site == "letterboxd" and isinstance(data, dict):
                html = data.get("_html") or ""
                low = html.lower()
                if "sorry, we can’t find" in low or "sorry, we can't find" in low:
                    lb_not_found = True
                if "just a moment" in low:
                    lb_cf = True

            ok = 200 <= resp.status_code < 400 and not omdb_not_found and not lb_not_found and not lb_cf
            path = self.store.save_response(req.request_id, data)

            if ok:
                with self._not_before_lock:
                    self._not_before.pop(req.request_id, None)
                self.store.update_fields(
                    req.request_id,
                    status=STATUS_SUCCESS,
                    http_status=resp.status_code,
                    duration_ms=round(ms, 1),
                    error="",
                    # Keep command so pipeline reports can retest successes too
                    reproducible_command=repro or "",
                    deferred="",
                    deferred_reason="",
                    finished_at=_now(),
                    result_path=str(path),
                    result_preview=preview[:300],
                )
                if self.adaptive:
                    self.adaptive.on_success(request_id=req.request_id)
                    if self.progress and hasattr(self.progress, "set_adaptive_snapshot"):
                        self.progress.set_adaptive_snapshot(
                            self.site, self.adaptive.snapshot()
                        )
                return

            # Terminal soft-fails: do NOT mislabel as "http_200" or defer forever
            if omdb_not_found:
                err_txt = f"omdb_not_found: {(data or {}).get('Error') or 'Movie not found!'}"
                self.store.update_fields(
                    req.request_id,
                    status=STATUS_SKIPPED,
                    http_status=resp.status_code,
                    duration_ms=round(ms, 1),
                    error=err_txt,
                    reproducible_command=repro,
                    deferred="",
                    deferred_reason="omdb_not_found",
                    finished_at=_now(),
                    result_path=str(path),
                    result_preview=preview[:300],
                )
                self._append_error_command(
                    req,
                    status_code=resp.status_code,
                    err=err_txt,
                    repro_cmd=repro,
                    classification="omdb_not_found",
                    captured_at=_now(),
                )
                return

            if lb_not_found or resp.status_code == 404:
                err_txt = f"not_found: http={resp.status_code}"
                self.store.update_fields(
                    req.request_id,
                    status=STATUS_SKIPPED,
                    http_status=resp.status_code,
                    duration_ms=round(ms, 1),
                    error=err_txt,
                    reproducible_command=repro,
                    deferred="",
                    deferred_reason="not_found",
                    finished_at=_now(),
                    result_path=str(path),
                    result_preview=preview[:300],
                )
                self._append_error_command(
                    req,
                    status_code=resp.status_code,
                    err=err_txt,
                    repro_cmd=repro,
                    classification="not_found",
                    captured_at=_now(),
                )
                return

            if lb_cf:
                err_txt = "letterboxd_cloudflare_challenge"
                self._mark_deferred(
                    req,
                    reason="cloudflare",
                    error=err_txt,
                    http_status=resp.status_code,
                    duration_ms=ms,
                    repro_cmd=repro,
                    preview=preview,
                    park_sec=30.0,
                )
                self.store.update_fields(req.request_id, result_path=str(path))
                self._append_error_command(
                    req,
                    status_code=resp.status_code,
                    err=err_txt,
                    repro_cmd=repro,
                    classification="cloudflare",
                    captured_at=_now(),
                )
                return

            # Other non-OK: defer with true classification (not fake http_200)
            reason = f"http_{resp.status_code}"
            err_txt = f"http {resp.status_code}"
            self._mark_deferred(
                req,
                reason=reason,
                error=err_txt,
                http_status=resp.status_code,
                duration_ms=ms,
                repro_cmd=repro,
                preview=preview,
                park_sec=0.0,
            )
            self.store.update_fields(req.request_id, result_path=str(path))
            self._append_error_command(
                req,
                status_code=resp.status_code,
                err=err_txt,
                repro_cmd=repro,
                classification=reason,
                captured_at=_now(),
            )
            if self.adaptive:
                self.adaptive.on_other_result(
                    ok=False, request_id=req.request_id, http_status=resp.status_code
                )
        except Exception as exc:  # noqa: BLE001
            ms = (time.monotonic() - t0) * 1000.0
            self._last_req = time.monotonic()
            err_x = f"{type(exc).__name__}: {exc}"
            self._mark_deferred(
                req,
                reason="exception",
                error=err_x,
                duration_ms=ms,
                repro_cmd=repro,
                park_sec=5.0,
            )
            self._append_error_command(
                req,
                status_code="EXCEPTION",
                err=err_x,
                repro_cmd=repro,
                classification="network_error",
                captured_at=_now(),
            )
            if self.adaptive:
                self.adaptive.on_other_result(ok=False, request_id=req.request_id)

    def _execute_wikipedia(self, req: PlanRequest) -> None:
        """
        Multi-step Wikipedia resolve with 1:1 command↔error attribution.

        Procedure: bare title → (film)/(year film) → MediaWiki search → summary on hits.
        Every HTTP call is logged in the response JSON under attempts[].
        Final plan.reproducible_command is ALWAYS the command of the attributed attempt
        (the success call, or the exact call that produced the stopping error).
        """
        from psychofilm_analyzer.gather_v2.wiki_resolve import (
            CLASS_NOT_FOUND,
            CLASS_RATE_LIMIT,
            CLASS_SERVER_ERROR,
            CLASS_NETWORK,
            resolve_wikipedia,
        )

        try:
            headers = json.loads(req.headers_json or "{}") or {}
        except json.JSONDecodeError:
            headers = {}
        # Always layer current OAuth + contact UA (token may be rotated in .env)
        try:
            from psychofilm_analyzer.utils.wikipedia_auth import wikipedia_headers

            auth_h = wikipedia_headers()
            headers = {**auth_h, **headers, **{k: v for k, v in auth_h.items() if k in ("Authorization", "User-Agent")}}
        except Exception:
            if not headers:
                headers = {
                    "User-Agent": (
                        "PsychoFilmAnalyzer/1.0 "
                        "(contact: romangermanyberlin@gmail.com; educational/research)"
                    ),
                    "Accept": "application/json, text/html, */*",
                }

        ep = (req.endpoint_type or "summary_en").lower()
        lang = "en"
        if ep.endswith("_ru") or ep == "ru":
            lang = "ru"
        elif ep.endswith("_de") or ep in {"de", "ge"}:
            lang = "de"

        # Heuristic TV: endpoint or title patterns (planner may pass more later)
        is_tv = False
        title_l = (req.film_title or "").lower()
        if "сериал" in title_l or " season" in title_l or "s0" in (req.english_title or "").lower():
            is_tv = True

        # Adaptive RPM is applied ONCE per plan row in run() via _throttle().
        # Inside multi-step resolve we only use a micro-gap between HTTP calls
        # (see inter_http_gap_sec) so a 4-step not_found is not charged 4× DELAY.
        if self.adaptive:
            self.adaptive.mark_request_started()

        t0 = time.monotonic()
        result = resolve_wikipedia(
            session=self.session,
            lang=lang,
            english_title=req.english_title or "",
            film_title=req.film_title or "",
            year=req.year,
            is_tv=is_tv,
            headers=headers,
            timeout=min(float(self.timeout_sec), 15.0),
            throttle=None,  # plan-row spacing already done; no full delay per GET
            inter_http_gap_sec=0.08,
            max_direct_titles=2,  # bare + (film); then search (skip year form bulk)
            max_search_hits=3,
            # 429 is transient: cool + re-fetch SAME url until durable 404/200
            max_429_retries=6,
            cool_base_sec=45.0,
            cool_step_sec=15.0,
            stop_event=self.stop_event,
        )
        ms = (time.monotonic() - t0) * 1000.0
        self._last_req = time.monotonic()

        payload = result.to_dict()
        path = self.store.save_response(req.request_id, payload)

        # Primary attribution — ONLY the attempt that owns the outcome
        repro = result.attributed_command or ""
        http_st = result.attributed_http_status
        captured = result.attributed_captured_at
        cls = result.classification
        preview = ""
        if result.attempts:
            preview = (result.attempts[-1].body_preview or "")[:300]
        if result.ok and result.summary:
            preview = str(result.summary.get("extract") or result.summary.get("title") or "")[:300]

        # Always store the attributed command on the plan row (success AND failure)
        self.store.update_fields(
            req.request_id,
            url=result.attributed_url or req.url,
            reproducible_command=repro,
            result_path=str(path),
            result_preview=preview,
            http_status=http_st,
            duration_ms=round(ms, 1),
            finished_at=_now(),
        )
        # Full detail lives in responses/<id>.json; human report is WIKI_REPORT.txt
        # (regenerated by ProgressReporter with ALL finished requests + commands).

        if result.ok:
            self._not_before.pop(req.request_id, None)
            self.store.update_fields(
                req.request_id,
                status=STATUS_SUCCESS,
                error="",
                deferred="",
                deferred_reason="",
                reproducible_command=repro,  # keep winning command for independent check
            )
            if self.adaptive:
                self.adaptive.on_success(request_id=req.request_id)
                if self.progress and hasattr(self.progress, "set_adaptive_snapshot"):
                    self.progress.set_adaptive_snapshot(
                        self.site, self.adaptive.snapshot()
                    )
            logger.info(
                "[wikipedia] OK %s via step=%s title=%r http=%s CURRENT_RPM=%.2f",
                req.request_id,
                result.attributed_step,
                result.final_title,
                http_st,
                self.adaptive.current_rpm() if self.adaptive else 0.0,
            )
            return

        # Failure path — classify correctly (never call not_found a 429)
        err_line = (
            f"CLASS={cls} CAPTURED_HTTP={http_st} CAPTURED_AT={captured} "
            f"STEP={result.attributed_step} TITLE={result.final_title!r} "
            f"MSG={result.message}"
        )

        if cls == CLASS_RATE_LIMIT:
            # Adaptive cool + defer; repro is THE 429 command only
            if self.adaptive:
                event = self.adaptive.on_429(request_id=req.request_id, repro_cmd=repro)
                if self.progress and hasattr(self.progress, "set_adaptive_snapshot"):
                    self.progress.set_adaptive_snapshot(
                        self.site, self.adaptive.snapshot()
                    )
                cool_sec = float(event.get("cool_pause_sec") or 60.0)
            else:
                cool_sec = 60.0
            self._park_until(req.request_id, cool_sec)
            self.store.update_fields(
                req.request_id,
                status=STATUS_DEFERRED,
                error=(
                    f"rate_limit (429) DEFERRED; GLOBAL pause {cool_sec:.0f}s; "
                    f"{err_line} | REPRO: {repro}"
                ),
                deferred="yes",
                deferred_reason="rate_limit",
            )
            self._append_error_command(
                req,
                status_code=http_st or 429,
                err=err_line,
                repro_cmd=repro,
                classification="rate_limit",
                captured_at=captured,
                page_title=result.final_title,
                attempts_payload=payload,
            )
            if self.adaptive:
                self.adaptive.apply_cool_pause(cool_sec, self.stop_event)
            else:
                self.stop_event.wait(cool_sec) if self.stop_event else time.sleep(cool_sec)
            return

        if cls in {CLASS_NETWORK, CLASS_SERVER_ERROR}:
            self._mark_deferred(
                req,
                reason=cls,
                error=err_line,
                http_status=http_st,
                duration_ms=ms,
                repro_cmd=repro,
                preview=preview,
                park_sec=10.0 if cls == CLASS_NETWORK else 5.0,
            )
            self._append_error_command(
                req,
                status_code=http_st or "ERR",
                err=err_line,
                repro_cmd=repro,
                classification=cls,
                captured_at=captured,
                page_title=result.final_title,
                attempts_payload=payload,
            )
            if self.adaptive:
                self.adaptive.on_other_result(ok=False, request_id=req.request_id)
            return

        # not_found / disambiguation / empty after full search → terminal skip
        # (retrying the same bad titles will not help; search already ran)
        self.store.update_fields(
            req.request_id,
            status=STATUS_SKIPPED,
            error=err_line,
            deferred="",
            deferred_reason=cls or CLASS_NOT_FOUND,
        )
        self._append_error_command(
            req,
            status_code=http_st or 404,
            err=err_line,
            repro_cmd=repro,
            classification=cls or CLASS_NOT_FOUND,
            captured_at=captured,
            page_title=result.final_title,
            attempts_payload=payload,
        )
        if self.adaptive:
            self.adaptive.on_other_result(ok=False, request_id=req.request_id)
        logger.info(
            "[wikipedia] %s %s steps=%s",
            cls,
            req.request_id,
            len(result.attempts),
        )
    def _append_error_command(
        self,
        req: PlanRequest,
        *,
        status_code: Any,
        err: str,
        repro_cmd: str,
        classification: str = "",
        captured_at: str = "",
        page_title: str = "",
        attempts_payload: Optional[dict[str, Any]] = None,
    ) -> None:
        """
        Append a human-visible block with the FULL executable command that owns this error.

        BINDING RULE: CAPTURED_HTTP / classification are bound ONLY to THIS command
        at CAPTURED_AT. Retest later may return a different status (esp. rate_limit).
        """
        if not repro_cmd:
            return
        # Always keep on plan row
        try:
            self.store.update_fields(req.request_id, reproducible_command=repro_cmd)
        except Exception:
            pass
        # Wikipedia: all human text is in WIKI_REPORT.txt (regenerated live) — skip extra dumps
        if self.site == "wikipedia":
            return
        paths: list[Path] = []
        if self.error_commands_path:
            paths.append(self.error_commands_path)
        if self.progress is not None and getattr(self.progress, "reports_dir", None):
            paths.append(
                Path(self.progress.reports_dir) / f"{self.site}_ERROR_COMMANDS.txt"
            )
        n_attempts = 0
        if attempts_payload and isinstance(attempts_payload.get("attempts"), list):
            n_attempts = len(attempts_payload["attempts"])
        block = (
            f"\n{'='*72}\n"
            f"request_id: {req.request_id}\n"
            f"film: {req.film_title!r} ({req.year}) excel_row={req.excel_row}\n"
            f"endpoint: {req.endpoint_type}\n"
            f"CLASSIFICATION: {classification or req.deferred_reason or 'unknown'}\n"
            f"CAPTURED_HTTP: {status_code}\n"
            f"CAPTURED_AT: {captured_at or _now()}\n"
            f"PAGE_TITLE: {page_title or '-'}\n"
            f"URL: {req.url}\n"
            f"NOTE: {err}\n"
            f"ATTEMPTS_IN_CHAIN: {n_attempts} (full chain in responses/{req.request_id}.json)\n"
            f"BINDING: CAPTURED_HTTP is bound ONLY to the command below at CAPTURED_AT.\n"
            f"  Retest later can differ (rate limits cool; DNS fluctuates).\n"
            f"THIS_COMMAND_ONLY (paste into PowerShell):\n"
            f"{repro_cmd}\n"
        )
        with self._cmd_log_lock:
            for p in paths:
                if p.name == "pipeline_wikipedia.txt":
                    # Do not append to rolling report (would corrupt structure); only dedicated files
                    continue
                try:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    if not p.exists():
                        p.write_text(
                            f"{self.site.upper()} ERROR COMMANDS LOG\n"
                            f"Each block is one failed/retried request with a full executable line.\n"
                            f"{'='*72}\n",
                            encoding="utf-8",
                        )
                    with p.open("a", encoding="utf-8") as fh:
                        fh.write(block)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("error command log write failed %s: %s", p, exc)

    def _handle_429(
        self, req: PlanRequest, *, ms: float, preview: str, repro_cmd: str = ""
    ) -> None:
        """
        On 429:
          - DEFER THIS request to END queue until cool-down expires
          - lower RPM / grow cool-down (adaptive)
          - GLOBAL pause of the whole site pipeline for cool_sec (do NOT keep firing)
        Full reproducible command is stored on the plan row and in adaptive log.
        """
        attempts = int(req.attempts or 1)
        if self.adaptive:
            max_a = max(self.rate_limit_max_attempts, int(req.max_attempts or self.max_attempts))
        else:
            max_a = int(req.max_attempts or self.max_attempts)
        st = STATUS_RETRY if attempts < max_a else STATUS_FAILED

        cool_sec = 60.0 if self.site == "wikipedia" else 10.0
        if self.adaptive:
            event = self.adaptive.on_429(request_id=req.request_id, repro_cmd=repro_cmd)
            if self.progress and hasattr(self.progress, "set_adaptive_snapshot"):
                self.progress.set_adaptive_snapshot(self.site, self.adaptive.snapshot())
            cool_sec = float(event.get("cool_pause_sec") or cool_sec)
            logger.warning(
                "[%s] 429: DEFER %s + GLOBAL pause %.0fs | "
                "CURRENT_RPM→%.2f STABLE_RPM=%.2f cool_next=%.0fs",
                self.site,
                req.request_id,
                cool_sec,
                float(event.get("new_rpm") or event.get("rpm") or 0),
                float(event.get("stable_rpm") or 0),
                float(event.get("next_cool_pause_sec") or 0),
            )
            err = (
                f"rate limited (429); DEFERRED_TO_END; GLOBAL pause {cool_sec:.0f}s; "
                f"CURRENT_RPM={event.get('new_rpm', event.get('rpm'))} "
                f"STABLE_RPM={event.get('stable_rpm')} | REPRO: {repro_cmd}"
            )
        else:
            err = f"rate limited; parked; REPRO: {repro_cmd}"
            logger.warning(
                "[%s] 429 on %s — GLOBAL pause %.0fs then continue",
                self.site,
                req.request_id,
                cool_sec,
            )

        # Mark deferred to END queue + park this id until cool_sec
        if st == STATUS_FAILED:
            self._mark_deferred(
                req,
                reason="429",
                error="rate limited (429) max attempts",
                http_status=429,
                duration_ms=ms,
                repro_cmd=repro_cmd,
                preview=preview,
                park_sec=cool_sec,
                permanent=True,
            )
        else:
            self._park_until(req.request_id, cool_sec)
            self.store.update_fields(
                req.request_id,
                status=STATUS_DEFERRED,
                http_status=429,
                duration_ms=round(ms, 1),
                error=err,
                reproducible_command=repro_cmd,
                deferred="yes",
                deferred_reason="429",
                finished_at=_now(),
                result_preview=preview[:200],
            )
            logger.info(
                "[%s] DEFERRED_TO_END %s (429) park=%.0fs — GLOBAL pause before any next call",
                self.site,
                req.request_id,
                cool_sec,
            )
        self._append_error_command(
            req,
            status_code=429,
            err=err,
            repro_cmd=repro_cmd,
            classification="rate_limit",
            captured_at=_now(),
        )

        # GLOBAL pause: entire site pipeline waits cool_sec (no more traffic while hot).
        if self.adaptive:
            self.adaptive.apply_cool_pause(cool_sec, self.stop_event)
        else:
            if self.stop_event:
                self.stop_event.wait(cool_sec)
            else:
                time.sleep(cool_sec)


def reset_session_command_logs(
    reports_dir: str | Path,
    *,
    sites: Optional[list[str]] = None,
    archive: bool = True,
) -> list[Path]:
    """
    Start a clean session for command/error dump files.

    Files like WIKI_ALL_REQUESTS.txt and WIKI_ERROR_COMMANDS.txt used to append
    forever across runs — old records mixed with the current session. On each
    Approach 2 start we truncate them (optionally archive previous content once).
    """
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    # Wiki human text = WIKI_REPORT.txt only (regenerated by ProgressReporter).
    # Other sites: one ERROR_COMMANDS file each. Legacy wiki dumps archived/removed.
    names = [
        "WIKI_COMMANDS.txt",
        "WIKI_ALL_REQUESTS.txt",
        "WIKI_ERROR_COMMANDS.txt",
        "WIKI_ATTEMPT_CHAINS.txt",
        "WIKI_RECHECK_LOG.txt",
        "wikipedia_ERROR_COMMANDS.txt",
    ]
    for site in sites or []:
        if site != "wikipedia":
            names.append(f"{site}_ERROR_COMMANDS.txt")
    for site in ("tmdb", "omdb", "kinopoisk", "letterboxd"):
        names.append(f"{site}_ERROR_COMMANDS.txt")

    stamp = stamp_local()
    archive_dir = reports_dir / "archive"
    touched: list[Path] = []
    seen: set[str] = set()
    header = (
        f"SESSION LOG (this run only)\n"
        f"started: {_now()}\n"
        f"Note: previous content was cleared at session start"
        + (f" (archive under reports/archive/ if non-empty).\n" if archive else ".\n")
        + f"{'=' * 72}\n"
    )
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        path = reports_dir / name
        try:
            if path.exists() and path.stat().st_size > 0 and archive:
                archive_dir.mkdir(parents=True, exist_ok=True)
                dest = archive_dir / f"{path.stem}_{stamp}{path.suffix}"
                # only archive if not already tiny header-only
                text = path.read_text(encoding="utf-8", errors="replace")
                if len(text) > 200 and "SESSION LOG (this run only)" not in text[:80]:
                    dest.write_text(text, encoding="utf-8")
            legacy_wiki = {
                "WIKI_COMMANDS.txt",
                "WIKI_ALL_REQUESTS.txt",
                "WIKI_ERROR_COMMANDS.txt",
                "WIKI_ATTEMPT_CHAINS.txt",
                "WIKI_RECHECK_LOG.txt",
                "wikipedia_ERROR_COMMANDS.txt",
            }
            if name in legacy_wiki:
                if path.exists() and path.stat().st_size > 0 and archive:
                    try:
                        path.unlink(missing_ok=True)  # type: ignore[call-arg]
                    except TypeError:
                        if path.exists():
                            path.unlink()
                    except Exception:
                        path.write_text(
                            "DEPRECATED — use WIKI_REPORT.txt only.\n",
                            encoding="utf-8",
                        )
                continue
            path.write_text(header, encoding="utf-8")
            touched.append(path)
        except Exception as exc:  # noqa: BLE001
            logger.debug("reset session log %s failed: %s", path, exc)
    # Seed the single all-in-one wiki report
    try:
        from psychofilm_analyzer.gather_v2.wiki_report import (
            WIKI_REPORT_NAME,
            write_wikipedia_pipeline_report,
        )

        # store may be unavailable here — only create header if missing
        wiki_path = reports_dir / WIKI_REPORT_NAME
        if not wiki_path.exists():
            wiki_path.write_text(
                "WIKI_REPORT.txt — single all-in-one Wikipedia report\n"
                f"started: {_now()}\n"
                "Regenerated live during Approach 2 with dashboard + FULL_COMMAND_LINEs.\n"
                f"{'=' * 72}\n",
                encoding="utf-8",
            )
        touched.append(wiki_path)
    except Exception as exc:  # noqa: BLE001
        logger.debug("seed WIKI_REPORT failed: %s", exc)
    logger.info(
        "Session logs reset (%s files); wikipedia → WIKI_REPORT.txt only",
        len(touched),
    )
    return touched


class Approach2Executor:
    def __init__(
        self,
        store: PlanStore,
        *,
        site_delays: dict[str, float],
        timeout_sec: float = 20.0,
        progress_interval_sec: float = 3.0,
        excel_every: int = 50,
        progress_kwargs: Optional[dict] = None,
        adaptive_sites: Optional[dict[str, dict[str, Any]]] = None,
        # Approach 3 options
        approach: int = 2,
        wikipedia_lang_workers: bool = False,
        recoverable_only_retry: bool = True,
        smart_progress: bool = False,
    ):
        self.store = store
        self.site_delays = site_delays
        self.timeout_sec = timeout_sec
        self.excel_every = excel_every
        self.adaptive_sites = adaptive_sites or {}
        self.approach = int(approach or 2)
        self.wikipedia_lang_workers = bool(wikipedia_lang_workers) or self.approach >= 3
        self.recoverable_only_retry = bool(recoverable_only_retry)
        self.stop_event = threading.Event()
        pk = dict(progress_kwargs or {})
        pk.setdefault("smart_progress", smart_progress or self.approach >= 3)
        pk.setdefault("approach", self.approach)
        self.progress = ProgressReporter(
            store,
            site_delays=site_delays,
            interval_sec=progress_interval_sec,
            **pk,
        )
        self.pipelines: list[SitePipeline] = []
        self.adaptive_controllers: dict[str, AdaptiveRpmController] = {}
        # Shared cool map for all wiki language workers
        self._wiki_not_before: dict[str, float] = {}
        self._wiki_not_before_lock = threading.Lock()

    def run(self) -> None:
        sites = self.store.sites()
        self.progress.start()
        reports_dir = Path(
            (self.progress.reports_dir if hasattr(self.progress, "reports_dir") else None)
            or (self.store.plan_dir / "reports")
        )
        reports_dir.mkdir(parents=True, exist_ok=True)
        reset_session_command_logs(reports_dir, sites=sites)

        for site in sites:
            delay = float(self.site_delays.get(site, 0.4))
            adaptive = None
            cfg = self.adaptive_sites.get(site)
            if site == "wikipedia" and cfg is not False:
                acfg = dict(cfg or {})
                initial_rpm = float(
                    acfg.get("initial_rpm")
                    or AdaptiveRpmController.delay_to_rpm(delay)
                )
                # Approach 3: shared max_rpm default 80 for multi-worker safety
                max_rpm = float(acfg.get("max_rpm", 200.0))
                if self.wikipedia_lang_workers and max_rpm > 100:
                    max_rpm = float(acfg.get("max_rpm_parallel", 80.0))
                adaptive = AdaptiveRpmController(
                    site,
                    initial_rpm=min(initial_rpm, max_rpm),
                    min_rpm=float(acfg.get("min_rpm", 5.0)),
                    max_rpm=max_rpm,
                    step_rpm=float(acfg.get("step_rpm", 10.0)),
                    cool_base_sec=float(acfg.get("cool_base_sec", 30.0)),
                    cool_step_sec=float(acfg.get("cool_step_sec", 15.0)),
                    decrease_pct=float(acfg.get("decrease_pct", 0.20)),
                    increase_pct=float(acfg.get("increase_pct", 0.0)),
                    success_batch=int(acfg.get("success_batch", 8)),
                    log_path=reports_dir / f"adaptive_rpm_{site}.txt",
                    live_rpm_path=reports_dir / f"CURRENT_RPM_{site}.txt",
                )
                self.adaptive_controllers[site] = adaptive
                if hasattr(self.progress, "set_adaptive_snapshot"):
                    self.progress.set_adaptive_snapshot(site, adaptive.snapshot())

            rate_max = int(
                (self.adaptive_sites.get(site) or {}).get("rate_limit_max_attempts", 50)
            )
            err_path = (
                None
                if site == "wikipedia"
                else reports_dir / f"{site}_ERROR_COMMANDS.txt"
            )

            if site == "wikipedia" and self.wikipedia_lang_workers:
                # Three language workers: EN / RU / DE — shared adaptive RPM
                for i, suf in enumerate(("_en", "_ru", "_de")):
                    p = SitePipeline(
                        site,
                        self.store,
                        delay_sec=delay,
                        timeout_sec=self.timeout_sec,
                        progress=self.progress,
                        stop_event=self.stop_event,
                        excel_every=self.excel_every,
                        adaptive=adaptive,
                        rate_limit_max_attempts=rate_max,
                        error_commands_path=err_path,
                        worker_id=i + 1,
                        endpoint_suffix=suf,
                        recoverable_only_retry=self.recoverable_only_retry,
                        shared_not_before=self._wiki_not_before,
                        shared_not_before_lock=self._wiki_not_before_lock,
                    )
                    self.pipelines.append(p)
                    p.start()
                    logger.info(
                        "Wikipedia lang worker %s started (filter=%s) shared adaptive",
                        i + 1,
                        suf,
                    )
            else:
                p = SitePipeline(
                    site,
                    self.store,
                    delay_sec=delay,
                    timeout_sec=self.timeout_sec,
                    progress=self.progress,
                    stop_event=self.stop_event,
                    excel_every=self.excel_every,
                    adaptive=adaptive,
                    rate_limit_max_attempts=rate_max,
                    error_commands_path=err_path,
                    recoverable_only_retry=self.recoverable_only_retry,
                )
                self.pipelines.append(p)
                p.start()
        try:
            for p in self.pipelines:
                p.join()
        except KeyboardInterrupt:
            self.stop_event.set()
            for p in self.pipelines:
                p.join(timeout=5)
        finally:
            for site, ctrl in self.adaptive_controllers.items():
                snap = ctrl.snapshot()
                logger.info(
                    "Adaptive RPM final [%s]: rpm=%.3f delay=%.2fs 429s=%s successes=%s",
                    site,
                    snap["rpm"],
                    snap["delay_sec"],
                    snap["total_429"],
                    snap["total_success"],
                )
                if hasattr(self.progress, "set_adaptive_snapshot"):
                    self.progress.set_adaptive_snapshot(site, snap)
            self.progress.stop()
            self.store.write_excel_and_csv()

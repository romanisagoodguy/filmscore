"""Polite HTTP client with retries, delay, logging, and per-host rate caps."""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict, deque
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from psychofilm_analyzer.utils.tmdb_network import install_tmdb_api_bypass

logger = logging.getLogger(__name__)

_SENSITIVE_PARAM = re.compile(r"^(api_key|apikey|api-key|token|password)$", re.I)
_SENSITIVE_HEADER = re.compile(r"^(authorization|x-api-key)$", re.I)

# Default safe caps (requests per rolling 60s window)
DEFAULT_HOST_RPM: dict[str, int] = {
    # Without OAuth keep low; with highvolume token config raises this (see config.py)
    "wikipedia.org": 3,
    "kinopoiskapiunofficial.tech": 30,
    "letterboxd.com": 20,
}


class RateLimitedError(Exception):
    """HTTP 429 — not retried by tenacity (callers should back off)."""


class HostRateLimiter:
    """Sliding-window per-host requests-per-minute limiter."""

    def __init__(self, limits_per_min: Optional[dict[str, int]] = None):
        self.limits = dict(DEFAULT_HOST_RPM)
        if limits_per_min:
            for k, v in limits_per_min.items():
                if v is None:
                    continue
                self.limits[str(k).lower()] = int(v)
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self.stats_waits = 0
        self.stats_wait_sec = 0.0

    def _key_for_host(self, host: str) -> Optional[str]:
        host = (host or "").lower()
        if not host:
            return None
        # exact match first
        if host in self.limits:
            return host
        # suffix match (en.wikipedia.org → wikipedia.org)
        for key in self.limits:
            if host == key or host.endswith("." + key) or host.endswith(key):
                return key
        return None

    def wait_if_needed(self, url: str) -> None:
        host = urlsplit(url).netloc
        key = self._key_for_host(host)
        if not key:
            return
        limit = int(self.limits.get(key) or 0)
        if limit <= 0:
            return
        now = time.monotonic()
        window = self._hits[key]
        while window and now - window[0] >= 60.0:
            window.popleft()
        if len(window) >= limit:
            sleep_for = 60.0 - (now - window[0]) + 0.05
            if sleep_for > 0:
                self.stats_waits += 1
                self.stats_wait_sec += sleep_for
                logger.info(
                    "Host RPM cap %s: %s/%s in last 60s — waiting %.1fs",
                    key,
                    len(window),
                    limit,
                    sleep_for,
                )
                time.sleep(sleep_for)
            now = time.monotonic()
            while window and now - window[0] >= 60.0:
                window.popleft()
        window.append(time.monotonic())

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        out = {}
        for key, limit in self.limits.items():
            window = self._hits.get(key) or deque()
            recent = [t for t in window if now - t < 60.0]
            out[key] = {
                "limit_per_min": limit,
                "used_last_60s": len(recent),
                "remaining_last_60s": max(0, limit - len(recent)),
            }
        out["_waits"] = self.stats_waits
        out["_wait_sec_total"] = round(self.stats_wait_sec, 2)
        return out


def _redact_params(params: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not params:
        return params
    out = {}
    for k, v in params.items():
        if _SENSITIVE_PARAM.match(str(k)):
            out[k] = "***"
        else:
            out[k] = v
    return out


def _redact_url(url: str) -> str:
    parts = urlsplit(url)
    if not parts.query:
        return url
    q = []
    for k, v in parse_qsl(parts.query, keep_blank_values=True):
        q.append((k, "***" if _SENSITIVE_PARAM.match(k) else v))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), parts.fragment))


class HttpClient:
    def __init__(
        self,
        delay_sec: float = 0.6,
        timeout_sec: float = 25.0,
        max_retries: int = 3,
        user_agent: str = "PsychoFilmAnalyzer/1.0",
        *,
        tmdb_dns_bypass: bool = True,
        host_rate_limits_per_min: Optional[dict[str, int]] = None,
    ):
        self.delay_sec = delay_sec
        self.timeout = timeout_sec
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "application/json, text/html, */*",
                "Accept-Language": "en-US,en;q=0.9,ru;q=0.8,de;q=0.7",
            }
        )
        # Optional per-request Wikipedia OAuth headers (Authorization Bearer …)
        self.wikipedia_headers: dict[str, str] = {}
        self._last_request = 0.0
        self.host_limiter = HostRateLimiter(host_rate_limits_per_min)
        # lightweight request counters for diagnostics
        self.request_stats: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"ok": 0, "fail": 0, "latency_ms": []}
        )
        self.debug_log: Any = None  # Optional[RequestDebugLog]
        self._attempt_counter: dict[str, int] = defaultdict(int)
        self.tmdb_bypass_ip: Optional[str] = None
        if tmdb_dns_bypass:
            try:
                self.tmdb_bypass_ip = install_tmdb_api_bypass(self.session)
            except Exception as exc:  # noqa: BLE001
                logger.warning("TMDB DNS bypass setup failed: %s", exc)

    def attach_debug_log(self, debug_log: Any) -> None:
        """Attach a RequestDebugLog to record every HTTP call for reproduction."""
        self.debug_log = debug_log

    def set_wikipedia_auth(self, headers: Optional[dict[str, str]]) -> None:
        """Attach Wikimedia OAuth/User-Agent headers for *.wikipedia.org calls."""
        self.wikipedia_headers = dict(headers or {})

    def _merge_headers(self, url: str, headers: Optional[dict[str, str]]) -> Optional[dict[str, str]]:
        host = (urlsplit(url).netloc or "").lower()
        merged: dict[str, str] = {}
        if "wikipedia.org" in host and self.wikipedia_headers:
            merged.update(self.wikipedia_headers)
        if headers:
            merged.update(headers)
        return merged or headers

    def _throttle(self, url: str) -> float:
        """Apply delays; return total throttle wait in milliseconds."""
        wait_ms = 0.0
        # 1) global polite delay between any two requests
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.delay_sec:
            sleep_for = self.delay_sec - elapsed
            time.sleep(sleep_for)
            wait_ms += sleep_for * 1000.0
        # 2) per-host RPM caps (Wiki / Kinopoisk)
        before = time.monotonic()
        self.host_limiter.wait_if_needed(url)
        wait_ms += (time.monotonic() - before) * 1000.0
        return wait_ms

    def get(
        self,
        url: str,
        *,
        params: Optional[dict[str, Any]] = None,
        headers: Optional[dict[str, str]] = None,
        as_json: bool = True,
    ) -> Any:
        return self._request("GET", url, params=params, headers=headers, as_json=as_json)

    def _emit_debug(
        self,
        *,
        method: str,
        url: str,
        params: Optional[dict[str, Any]],
        headers: Optional[dict[str, str]],
        status_code: Optional[int],
        ok: bool,
        error: Optional[str],
        duration_ms: float,
        throttle_wait_ms: float,
        attempt: int,
        response_preview: str = "",
        body_len: Optional[int] = None,
    ) -> None:
        if not self.debug_log:
            return
        try:
            self.debug_log.log_http(
                method=method,
                url=url,
                params=params,
                headers=headers,
                session_headers=dict(self.session.headers),
                status_code=status_code,
                ok=ok,
                error=error,
                duration_ms=duration_ms,
                throttle_wait_ms=throttle_wait_ms,
                attempt=attempt,
                max_attempts=self.max_retries,
                response_preview=response_preview,
                body_len=body_len,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("request debug log write failed: %s", exc)

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[dict[str, Any]] = None,
        headers: Optional[dict[str, str]] = None,
        as_json: bool = True,
        data: Any = None,
    ) -> Any:
        host = urlsplit(url).netloc or "unknown"
        call_id = f"{method}:{url}:{id(params)}"
        self._attempt_counter[call_id] = 0

        @retry(
            reraise=True,
            stop=stop_after_attempt(self.max_retries),
            wait=wait_exponential(multiplier=1, min=1, max=20),
            retry=retry_if_exception_type((requests.RequestException,)),
        )
        def _do() -> Any:
            self._attempt_counter[call_id] += 1
            attempt = self._attempt_counter[call_id]
            throttle_ms = self._throttle(url)
            logger.info("HTTP %s %s params=%s", method, _redact_url(url), _redact_params(params))
            t0 = time.monotonic()
            try:
                req_headers = self._merge_headers(url, headers)
                resp = self.session.request(
                    method,
                    url,
                    params=params,
                    headers=req_headers,
                    data=data,
                    timeout=self.timeout,
                )
            except Exception as exc:
                self.request_stats[host]["fail"] += 1
                ms = (time.monotonic() - t0) * 1000.0
                self.request_stats[host]["latency_ms"].append(ms)
                self._emit_debug(
                    method=method,
                    url=url,
                    params=params,
                    headers=headers,
                    status_code=None,
                    ok=False,
                    error=f"{type(exc).__name__}: {exc}",
                    duration_ms=ms,
                    throttle_wait_ms=throttle_ms,
                    attempt=attempt,
                )
                raise
            self._last_request = time.monotonic()
            ms = (self._last_request - t0) * 1000.0
            self.request_stats[host]["latency_ms"].append(ms)
            body_preview = ""
            try:
                body_preview = (resp.text or "")[:500]
            except Exception:  # noqa: BLE001
                body_preview = ""
            body_len = len(resp.content) if resp.content is not None else None

            if resp.status_code == 429:
                self.request_stats[host]["fail"] += 1
                self._emit_debug(
                    method=method,
                    url=url,
                    params=params,
                    headers=headers,
                    status_code=429,
                    ok=False,
                    error="RateLimitedError",
                    duration_ms=ms,
                    throttle_wait_ms=throttle_ms,
                    attempt=attempt,
                    response_preview=body_preview,
                    body_len=body_len,
                )
                # Short pause only — sources (e.g. Wikipedia) own long cooldown/circuit
                retry_after = float(resp.headers.get("Retry-After", "1") or 1)
                retry_after = min(max(retry_after, 0.3), 1.5)
                logger.warning("Rate limited on %s (no tenacity retry, wait %.1fs)", url, retry_after)
                time.sleep(retry_after)
                # Not a RequestException — tenacity will not re-hammer the endpoint
                raise RateLimitedError(f"429 rate limited: {url}")
            if resp.status_code >= 500:
                self.request_stats[host]["fail"] += 1
                self._emit_debug(
                    method=method,
                    url=url,
                    params=params,
                    headers=headers,
                    status_code=resp.status_code,
                    ok=False,
                    error=f"Server error {resp.status_code}",
                    duration_ms=ms,
                    throttle_wait_ms=throttle_ms,
                    attempt=attempt,
                    response_preview=body_preview,
                    body_len=body_len,
                )
                raise requests.RequestException(f"Server error {resp.status_code}")
            if resp.status_code == 404:
                self.request_stats[host]["ok"] += 1
                self._emit_debug(
                    method=method,
                    url=url,
                    params=params,
                    headers=headers,
                    status_code=404,
                    ok=True,
                    error=None,
                    duration_ms=ms,
                    throttle_wait_ms=throttle_ms,
                    attempt=attempt,
                    response_preview=body_preview,
                    body_len=body_len,
                )
                return None if as_json else ""
            # Other 4xx are permanent — do not retry (e.g. Kinopoisk keywords 400)
            if 400 <= resp.status_code < 500:
                self.request_stats[host]["fail"] += 1
                logger.debug("HTTP %s client error %s on %s", method, resp.status_code, url)
                self._emit_debug(
                    method=method,
                    url=url,
                    params=params,
                    headers=headers,
                    status_code=resp.status_code,
                    ok=False,
                    error=f"HTTP {resp.status_code}",
                    duration_ms=ms,
                    throttle_wait_ms=throttle_ms,
                    attempt=attempt,
                    response_preview=body_preview,
                    body_len=body_len,
                )
                return None if as_json else ""
            try:
                resp.raise_for_status()
            except Exception as exc:
                self.request_stats[host]["fail"] += 1
                self._emit_debug(
                    method=method,
                    url=url,
                    params=params,
                    headers=headers,
                    status_code=resp.status_code,
                    ok=False,
                    error=str(exc),
                    duration_ms=ms,
                    throttle_wait_ms=throttle_ms,
                    attempt=attempt,
                    response_preview=body_preview,
                    body_len=body_len,
                )
                raise
            self.request_stats[host]["ok"] += 1
            self._emit_debug(
                method=method,
                url=url,
                params=params,
                headers=headers,
                status_code=resp.status_code,
                ok=True,
                error=None,
                duration_ms=ms,
                throttle_wait_ms=throttle_ms,
                attempt=attempt,
                response_preview=body_preview,
                body_len=body_len,
            )
            if as_json:
                try:
                    return resp.json()
                except ValueError:
                    return None
            return resp.text

        try:
            return _do()
        except RateLimitedError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("HTTP failed %s %s: %s", method, url, exc)
            raise

    def stats_snapshot(self) -> dict[str, Any]:
        """Aggregate request stats for diagnostics."""
        hosts = {}
        for host, st in self.request_stats.items():
            lats = list(st.get("latency_ms") or [])
            hosts[host] = {
                "ok": st.get("ok", 0),
                "fail": st.get("fail", 0),
                "n": len(lats),
                "latency_ms_avg": round(sum(lats) / len(lats), 1) if lats else None,
                "latency_ms_max": round(max(lats), 1) if lats else None,
            }
        return {
            "hosts": hosts,
            "host_rpm": self.host_limiter.snapshot(),
            "global_delay_sec": self.delay_sec,
            "timeout_sec": self.timeout,
            "max_retries": self.max_retries,
        }

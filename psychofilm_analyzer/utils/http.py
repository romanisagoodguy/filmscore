"""Polite HTTP client with retries, delay, and logging."""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from psychofilm_analyzer.utils.tmdb_network import install_tmdb_api_bypass

logger = logging.getLogger(__name__)

_SENSITIVE_PARAM = re.compile(r"^(api_key|apikey|api-key|token|password)$", re.I)


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
        self._last_request = 0.0
        self.tmdb_bypass_ip: Optional[str] = None
        if tmdb_dns_bypass:
            try:
                self.tmdb_bypass_ip = install_tmdb_api_bypass(self.session)
            except Exception as exc:  # noqa: BLE001
                logger.warning("TMDB DNS bypass setup failed: %s", exc)

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.delay_sec:
            time.sleep(self.delay_sec - elapsed)

    def get(
        self,
        url: str,
        *,
        params: Optional[dict[str, Any]] = None,
        headers: Optional[dict[str, str]] = None,
        as_json: bool = True,
    ) -> Any:
        return self._request("GET", url, params=params, headers=headers, as_json=as_json)

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
        @retry(
            reraise=True,
            stop=stop_after_attempt(self.max_retries),
            wait=wait_exponential(multiplier=1, min=1, max=20),
            retry=retry_if_exception_type((requests.RequestException,)),
        )
        def _do() -> Any:
            self._throttle()
            logger.info("HTTP %s %s params=%s", method, _redact_url(url), _redact_params(params))
            resp = self.session.request(
                method,
                url,
                params=params,
                headers=headers,
                data=data,
                timeout=self.timeout,
            )
            self._last_request = time.monotonic()
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", "5"))
                # Cap wait so one hostile endpoint cannot stall the whole batch
                retry_after = min(retry_after, 15.0)
                logger.warning("Rate limited on %s, sleeping %ss", url, retry_after)
                time.sleep(retry_after)
                raise requests.RequestException("429 rate limited")
            if resp.status_code >= 500:
                raise requests.RequestException(f"Server error {resp.status_code}")
            if resp.status_code == 404:
                return None if as_json else ""
            resp.raise_for_status()
            if as_json:
                try:
                    return resp.json()
                except ValueError:
                    return None
            return resp.text

        try:
            return _do()
        except Exception as exc:  # noqa: BLE001
            logger.warning("HTTP failed %s %s: %s", method, url, exc)
            raise

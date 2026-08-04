"""Workarounds when api.themoviedb.org is DNS-poisoned to localhost.

Common in restricted networks: www.themoviedb.org works via hosts file,
but api.themoviedb.org still resolves to 127.0.0.1. We detect that and
route API calls to a real edge IP with correct TLS SNI / Host header.
"""

from __future__ import annotations

import logging
import socket
import time
from typing import Optional
from urllib.parse import urlparse, urlunparse

import requests
from requests.adapters import HTTPAdapter

logger = logging.getLogger(__name__)

TMDB_API_HOST = "api.themoviedb.org"
_DOH_CACHE: dict[str, tuple[float, list[str]]] = {}
_DOH_TTL = 600.0  # seconds


def _is_loopback(ip: str) -> bool:
    return ip in {"127.0.0.1", "::1", "0.0.0.0"} or ip.startswith("127.")


def dns_is_poisoned(hostname: str = TMDB_API_HOST) -> bool:
    try:
        ip = socket.gethostbyname(hostname)
    except OSError:
        return True
    return _is_loopback(ip)


def resolve_via_doh(hostname: str = TMDB_API_HOST) -> list[str]:
    now = time.time()
    cached = _DOH_CACHE.get(hostname)
    if cached and now - cached[0] < _DOH_TTL:
        return cached[1]

    ips: list[str] = []
    endpoints = [
        (
            "https://dns.google/resolve",
            {"name": hostname, "type": "A"},
            {},
        ),
        (
            "https://1.1.1.1/dns-query",
            {"name": hostname, "type": "A"},
            {"accept": "application/dns-json"},
        ),
    ]
    for url, params, headers in endpoints:
        try:
            r = requests.get(url, params=params, headers=headers, timeout=10)
            r.raise_for_status()
            for ans in r.json().get("Answer") or []:
                if ans.get("type") == 1 and ans.get("data") and not _is_loopback(ans["data"]):
                    if ans["data"] not in ips:
                        ips.append(ans["data"])
            if ips:
                break
        except Exception as exc:  # noqa: BLE001
            logger.debug("DoH resolve failed via %s: %s", url, exc)

    if ips:
        _DOH_CACHE[hostname] = (now, ips)
        logger.info("DoH resolved %s -> %s", hostname, ips)
    else:
        logger.warning("DoH could not resolve %s", hostname)
    return ips


class ForcedIPAdapter(HTTPAdapter):
    """Send requests for a hostname to a fixed IP while keeping TLS SNI."""

    def __init__(self, hostname: str, ip: str, **kwargs):
        self.hostname = hostname
        self.ip = ip
        super().__init__(**kwargs)

    def init_poolmanager(self, *args, **kwargs):
        kwargs["server_hostname"] = self.hostname
        kwargs["assert_hostname"] = self.hostname
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        kwargs["server_hostname"] = self.hostname
        kwargs["assert_hostname"] = self.hostname
        return super().proxy_manager_for(*args, **kwargs)

    def send(self, request, **kwargs):
        parsed = urlparse(request.url)
        if parsed.hostname == self.hostname:
            netloc = f"{self.ip}:{parsed.port}" if parsed.port else self.ip
            request.url = urlunparse(
                (parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment)
            )
            # Host header for virtual hosting; SNI set via poolmanager
            request.headers["Host"] = self.hostname
        return super().send(request, **kwargs)


def install_tmdb_api_bypass(session: requests.Session) -> Optional[str]:
    """
    If api.themoviedb.org is poisoned, mount a DoH-based IP adapter.

    Returns the chosen IP, or None if bypass was not needed / not possible.
    """
    if not dns_is_poisoned(TMDB_API_HOST):
        logger.info("TMDB DNS looks healthy; no bypass needed")
        return None

    ips = resolve_via_doh(TMDB_API_HOST)
    if not ips:
        logger.error(
            "TMDB API host resolves to localhost and DoH found no IPs. "
            "Add a working IP for api.themoviedb.org to your hosts file."
        )
        return None

    # Pick first IP that answers TLS quickly
    chosen = None
    for ip in ips:
        try:
            with socket.create_connection((ip, 443), timeout=5):
                chosen = ip
                break
        except OSError:
            continue
    if not chosen:
        chosen = ips[0]

    adapter = ForcedIPAdapter(TMDB_API_HOST, chosen)
    # Mount for both the domain form and the IP form after rewrite
    session.mount(f"https://{TMDB_API_HOST}/", adapter)
    session.mount(f"https://{chosen}/", adapter)
    logger.warning(
        "TMDB DNS poisoned to localhost — routing api.themoviedb.org via DoH IP %s "
        "(same idea as hosts-file fix for the website)",
        chosen,
    )
    return chosen

"""Test TMDB API via forced IP + correct TLS SNI."""

from __future__ import annotations

import os
import socket
import ssl

import requests
import urllib3
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.connection import create_connection

urllib3.disable_warnings()
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
KEY = os.getenv("TMDB_API_KEY", "")


def forced_ip_session(hostname: str, ip: str) -> requests.Session:
    class ForcedIPAdapter(HTTPAdapter):
        def init_poolmanager(self, *args, **kwargs):
            # monkeypatch connection for this host
            return super().init_poolmanager(*args, **kwargs)

        def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
            conn = super().get_connection_with_tls_context(request, verify, proxies, cert)
            return conn

    # Simpler approach: custom HTTPSConnection
    class HostHeaderSSLAdapter(HTTPAdapter):
        def __init__(self, server_hostname: str, ip: str, **kwargs):
            self._server_hostname = server_hostname
            self._ip = ip
            super().__init__(**kwargs)

        def init_poolmanager(self, *args, **kwargs):
            kwargs["assert_hostname"] = self._server_hostname
            kwargs["server_hostname"] = self._server_hostname
            return super().init_poolmanager(*args, **kwargs)

        def proxy_manager_for(self, *args, **kwargs):
            kwargs["assert_hostname"] = self._server_hostname
            kwargs["server_hostname"] = self._server_hostname
            return super().proxy_manager_for(*args, **kwargs)

        def send(self, request, **kwargs):
            # rewrite URL host to IP while keeping Host/SNI as domain
            from urllib.parse import urlparse, urlunparse

            p = urlparse(request.url)
            if p.hostname == self._server_hostname:
                netloc = f"{self._ip}:{p.port}" if p.port else self._ip
                request.url = urlunparse(
                    (p.scheme, netloc, p.path, p.params, p.query, p.fragment)
                )
                request.headers["Host"] = self._server_hostname
            return super().send(request, **kwargs)

    s = requests.Session()
    s.mount("https://", HostHeaderSSLAdapter(hostname, ip))
    return s


def main() -> None:
    # Real IPs from DoH
    r = requests.get(
        "https://dns.google/resolve",
        params={"name": "api.themoviedb.org", "type": "A"},
        timeout=10,
    )
    answers = r.json().get("Answer") or []
    ips = [a["data"] for a in answers if a.get("type") == 1]
    print("DoH A records:", ips)

    for ip in ips[:4]:
        # raw socket TLS test
        try:
            ctx = ssl.create_default_context()
            with socket.create_connection((ip, 443), timeout=10) as sock:
                with ctx.wrap_socket(sock, server_hostname="api.themoviedb.org") as ssock:
                    print(f"TLS to {ip} OK cipher={ssock.cipher()}")
        except Exception as exc:  # noqa: BLE001
            print(f"TLS to {ip} FAIL {exc}")
            continue

        try:
            sess = forced_ip_session("api.themoviedb.org", ip)
            resp = sess.get(
                "https://api.themoviedb.org/3/configuration",
                params={"api_key": KEY},
                timeout=15,
            )
            print(f"API via {ip}: {resp.status_code} {resp.text[:100]}")
        except Exception as exc:  # noqa: BLE001
            print(f"API via {ip}: ERR {exc}")


if __name__ == "__main__":
    main()

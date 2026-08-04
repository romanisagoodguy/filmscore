"""Probe whether TMDB API works via hosts-file IPs / alternate DNS."""

from __future__ import annotations

import os
import sys

import requests
import urllib3
from dotenv import load_dotenv

urllib3.disable_warnings()
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
KEY = os.getenv("TMDB_API_KEY", "")
IPS = ["3.168.73.40", "3.168.73.14", "3.168.73.5", "3.168.73.124"]


def main() -> int:
    print("=== Public DoH for api.themoviedb.org ===")
    for name, url, params in [
        ("cloudflare", "https://1.1.1.1/dns-query", {"name": "api.themoviedb.org", "type": "A"}),
        ("google", "https://dns.google/resolve", {"name": "api.themoviedb.org", "type": "A"}),
    ]:
        try:
            headers = {"accept": "application/dns-json"} if "1.1.1.1" in url else {}
            r = requests.get(url, params=params, headers=headers, timeout=10)
            print(name, r.status_code, r.text[:250])
        except Exception as exc:  # noqa: BLE001
            print(name, "ERR", exc)

    print("\n=== Direct IP + Host header probes ===")
    for ip in IPS:
        for host in ("api.themoviedb.org", "www.themoviedb.org"):
            try:
                r = requests.get(
                    f"https://{ip}/3/configuration",
                    params={"api_key": KEY},
                    headers={"Host": host},
                    timeout=12,
                    verify=False,
                )
                snippet = (r.text or "").replace("\n", " ")[:90]
                print(f"IP {ip} Host={host} -> {r.status_code} {snippet}")
            except Exception as exc:  # noqa: BLE001
                print(f"IP {ip} Host={host} -> ERR {exc}")

    print("\n=== Normal DNS path ===")
    try:
        r = requests.get(
            "https://api.themoviedb.org/3/configuration",
            params={"api_key": KEY},
            timeout=12,
        )
        print("api.themoviedb.org", r.status_code, r.text[:90])
    except Exception as exc:  # noqa: BLE001
        print("api.themoviedb.org ERR", exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())

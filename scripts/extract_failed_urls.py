#!/usr/bin/env python3
"""Extract failed Wikipedia RU and Kinopoisk URLs from gather_full_run.log."""

from __future__ import annotations

import re
from pathlib import Path


def uniq(seq: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in seq:
        x = x.strip().rstrip(").,\"'")
        x = x.split("(Caused")[0].strip()
        x = x.split(" params=")[0].strip()
        if x and x not in seen and len(x) > 25:
            seen.add(x)
            out.append(x)
    return out


def main() -> None:
    path = Path("output/gather_full_run.log")
    raw = path.read_bytes()
    if raw[:2] == b"\xff\xfe" or (len(raw) > 2 and raw[1] == 0):
        text = raw.decode("utf-16")
    else:
        text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()

    wiki_ru: list[str] = []
    kp: list[str] = []

    fail_markers = (
        "failed",
        "Failed",
        "WARNING",
        "Rate limited",
        "Max retries",
        "getaddrinfo",
        "NameResolution",
        "fetch failed",
        "HTTP failed",
    )

    for i, line in enumerate(lines):
        is_fail = any(m in line for m in fail_markers)

        if "ru.wikipedia.org" in line:
            m = re.search(r"https://ru\.wikipedia\.org\S+", line)
            if m:
                url = m.group(0)
                if is_fail:
                    wiki_ru.append(url)
            m3 = re.search(
                r"host='ru\.wikipedia\.org'.*?url:\s*([^\s(]+)", line
            )
            if m3 and is_fail:
                path_u = m3.group(1)
                if path_u.startswith("/"):
                    wiki_ru.append("https://ru.wikipedia.org" + path_u)
            # failure line without URL: look back for last GET
            if is_fail and not m:
                for j in range(i, max(-1, i - 25), -1):
                    if "HTTP GET https://ru.wikipedia.org" in lines[j]:
                        m2 = re.search(
                            r"https://ru\.wikipedia\.org\S+", lines[j]
                        )
                        if m2:
                            wiki_ru.append(m2.group(0))
                        break

        if "kinopoiskapiunofficial.tech" in line:
            m = re.search(
                r"https://kinopoiskapiunofficial\.tech\S+", line
            )
            if m and is_fail:
                kp.append(m.group(0))
            m3 = re.search(
                r"host='kinopoiskapiunofficial\.tech'.*?url:\s*([^\s(]+)",
                line,
            )
            if m3 and is_fail:
                path_u = m3.group(1)
                if path_u.startswith("/"):
                    kp.append(
                        "https://kinopoiskapiunofficial.tech" + path_u
                    )
            if is_fail and not m:
                for j in range(i, max(-1, i - 30), -1):
                    if (
                        "HTTP GET https://kinopoiskapiunofficial.tech"
                        in lines[j]
                    ):
                        m2 = re.search(
                            r"https://kinopoiskapiunofficial\.tech\S+",
                            lines[j],
                        )
                        if m2:
                            kp.append(m2.group(0))
                        # reconstruct search with params from log
                        if "params=" in lines[j]:
                            pm = re.search(
                                r"params=(\{[^}]+\})", lines[j]
                            )
                            if pm:
                                # keep base for command building
                                pass
                        break

    wiki_ru = uniq(wiki_ru)
    kp = uniq(kp)

    print("WIKI_RU", len(wiki_ru))
    for u in wiki_ru[:20]:
        print("W", u)
    print("KP", len(kp))
    for u in kp[:20]:
        print("K", u)


if __name__ == "__main__":
    main()

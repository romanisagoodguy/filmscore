#!/usr/bin/env python3
"""Build 10 failing Wiki + 10 failing Kinopoisk commands from gather log."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote


def load_log() -> list[str]:
    raw = Path("output/gather_full_run.log").read_bytes()
    if raw[:2] == b"\xff\xfe" or (len(raw) > 2 and raw[1] == 0):
        text = raw.decode("utf-16")
    else:
        text = raw.decode("utf-8", errors="replace")
    return text.splitlines()


def uniq(items: list) -> list:
    seen = set()
    out = []
    for it in items:
        key = str(it)
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def main() -> None:
    lines = load_log()
    fail_tokens = (
        "failed",
        "Failed",
        "Max retries",
        "getaddrinfo",
        "NameResolution",
        "HTTP failed",
        "fetch failed",
        "Rate limited",
        "cooldown",
    )

    # --- Kinopoisk GETs that fail ---
    kp: list[dict] = []
    for i, line in enumerate(lines):
        if "HTTP GET https://kinopoiskapiunofficial.tech" not in line:
            continue
        window = "\n".join(lines[i : i + 12])
        if not any(t in window for t in fail_tokens):
            continue
        m = re.search(r"https://kinopoiskapiunofficial\.tech[^\s]+", line)
        url = m.group(0) if m else None
        params = None
        pm = re.search(r"params=(\{.*\})", line)
        if pm:
            params = pm.group(1)
        title = None
        for j in range(i, min(i + 15, len(lines))):
            if "fetch failed for" in lines[j]:
                title = lines[j].split("fetch failed for", 1)[-1].strip().rstrip(":")
                # next line may continue garbled title
                break
        err = None
        for j in range(i, min(i + 12, len(lines))):
            if any(t in lines[j] for t in fail_tokens):
                err = lines[j].strip()[:200]
                if "url:" in lines[j] and i + 1 < len(lines):
                    # path may be next line
                    pass
                break
        # full path from Max retries block
        for j in range(i, min(i + 12, len(lines))):
            if "url:" in lines[j] and "kinopoisk" in "\n".join(lines[max(0, j - 2) : j + 2]):
                um = re.search(r"url:\s*(\S+)", lines[j])
                if um and um.group(1).startswith("/"):
                    url = "https://kinopoiskapiunofficial.tech" + um.group(1)
                elif j + 1 < len(lines) and lines[j + 1].strip().startswith("/"):
                    url = "https://kinopoiskapiunofficial.tech" + lines[j + 1].strip().split()[0].rstrip(")")
        kp.append({"url": url, "params": params, "title": title, "err": err, "line": i})

    # --- Wikipedia GETs that fail (en + ru) ---
    wiki: list[dict] = []
    for i, line in enumerate(lines):
        if "HTTP GET https://" not in line or "wikipedia.org" not in line:
            continue
        window = "\n".join(lines[i : i + 12])
        if not any(t in window for t in fail_tokens):
            continue
        m = re.search(r"https://(?:en|ru)\.wikipedia\.org[^\s]+", line)
        url = m.group(0) if m else None
        lang = "ru" if url and "ru.wikipedia" in url else "en"
        params = None
        pm = re.search(r"params=(\{.*\})", line)
        if pm:
            params = pm.group(1)
        err = None
        for j in range(i, min(i + 12, len(lines))):
            if any(t in lines[j] for t in fail_tokens):
                err = lines[j].strip()[:200]
                break
        wiki.append({"url": url, "params": params, "lang": lang, "err": err, "line": i})

    # Also pull Max-retries path-only blocks for KP
    for i, line in enumerate(lines):
        if "host='kinopoiskapiunofficial.tech'" in line and "url:" in line:
            path = None
            um = re.search(r"url:\s*(\S+)", line)
            if um and um.group(1).startswith("/"):
                path = um.group(1)
            elif i + 1 < len(lines) and lines[i + 1].strip().startswith("/"):
                path = lines[i + 1].strip().split()[0].rstrip(")")
            if path:
                kp.append(
                    {
                        "url": "https://kinopoiskapiunofficial.tech" + path,
                        "params": None,
                        "title": None,
                        "err": line.strip()[:200],
                        "line": i,
                    }
                )

    # dedupe by url+params
    def dedupe(items: list[dict], keyfn) -> list[dict]:
        seen = set()
        out = []
        for it in items:
            k = keyfn(it)
            if not k or k in seen:
                continue
            seen.add(k)
            out.append(it)
        return out

    kp = dedupe(kp, lambda x: (x.get("url") or "") + "|" + (x.get("params") or ""))
    wiki = dedupe(wiki, lambda x: (x.get("url") or "") + "|" + (x.get("params") or ""))
    wiki_ru = [w for w in wiki if w.get("lang") == "ru"]
    wiki_en = [w for w in wiki if w.get("lang") == "en"]

    print("STATS")
    print("  kp_fail_urls", len(kp))
    print("  wiki_fail_en", len(wiki_en))
    print("  wiki_fail_ru", len(wiki_ru))
    print("  wiki_fail_total", len(wiki))

    key = "72862a1e-ecac-4abf-8677-85d2831ae9d6"

    print("\n=== 10 KINOPOISK COMMANDS (from failing log requests) ===\n")
    for n, item in enumerate(kp[:10], 1):
        url = item.get("url") or "https://kinopoiskapiunofficial.tech/api/v2.1/films/search-by-keyword"
        params = item.get("params")
        # Build python one-liner
        if params and "keyword" in params:
            km = re.search(r"'keyword':\s*'([^']*)'", params)
            page_m = re.search(r"'page':\s*(\d+)", params)
            kw = km.group(1) if km else "test"
            page = page_m.group(1) if page_m else "1"
            # escape for command
            kw_esc = kw.replace("\\", "\\\\").replace('"', '\\"')
            cmd = (
                f'python -c "import requests; r=requests.get('
                f"'https://kinopoiskapiunofficial.tech/api/v2.1/films/search-by-keyword', "
                f"params={{'keyword':'{kw_esc}','page':{page}}}, "
                f"headers={{'X-API-KEY':'{key}','Content-Type':'application/json'}}, "
                f'timeout=20); print(r.status_code); print(r.text[:300])"'
            )
            curl = (
                f'curl.exe -i -H "X-API-KEY: {key}" -H "Content-Type: application/json" '
                f'"https://kinopoiskapiunofficial.tech/api/v2.1/films/search-by-keyword?'
                f'keyword={quote(kw)}&page={page}"'
            )
        else:
            # bare URL
            full = url
            if not full.startswith("http"):
                full = "https://kinopoiskapiunofficial.tech" + full
            cmd = (
                f'python -c "import requests; r=requests.get('
                f"'{full}', "
                f"headers={{'X-API-KEY':'{key}','Content-Type':'application/json'}}, "
                f'timeout=20); print(r.status_code); print(r.text[:300])"'
            )
            curl = f'curl.exe -i -H "X-API-KEY: {key}" -H "Content-Type: application/json" "{full}"'
        print(f"--- KP #{n} ---")
        if item.get("title"):
            print(f"film context: {item['title'][:80]}")
        print(f"error sample: {(item.get('err') or '')[:160]}")
        print(f"URL: {url}")
        print(f"PYTHON:\n{cmd}")
        print(f"CURL:\n{curl}")
        print()

    print("\n=== WIKIPEDIA RU ===")
    if not wiki_ru:
        print(
            "NONE in gather_full_run.log — this run used EN-only Wikipedia "
            "(config wikipedia.langs: [en]). No https://ru.wikipedia.org requests were made."
        )
        print("\n=== 10 WIKIPEDIA EN COMMANDS that failed in THIS log (substitute for RU) ===\n")
        for n, item in enumerate(wiki_en[:10], 1):
            url = item.get("url") or ""
            params = item.get("params")
            print(f"--- WIKI EN #{n} ---")
            print(f"error sample: {(item.get('err') or '')[:160]}")
            if params and "action" in params:
                # MediaWiki api.php
                # reconstruct query from params dict-like string
                action = re.search(r"'action':\s*'([^']*)'", params)
                titles = re.search(r"'titles':\s*'([^']*)'", params)
                srsearch = re.search(r"'srsearch':\s*'([^']*)'", params)
                q = []
                if action:
                    q.append(f"action={action.group(1)}")
                if titles:
                    q.append(f"titles={quote(titles.group(1))}")
                if srsearch:
                    q.append(f"list=search&srsearch={quote(srsearch.group(1))}")
                q.append("format=json")
                full = "https://en.wikipedia.org/w/api.php?" + "&".join(q)
                # Also show RU equivalent URL pattern for user
                full_ru = full.replace("en.wikipedia.org", "ru.wikipedia.org")
            else:
                full = url
                full_ru = url.replace("en.wikipedia.org", "ru.wikipedia.org") if url else ""
            print(f"URL_EN: {full}")
            print(f"URL_RU_equivalent: {full_ru}")
            print(
                f'PYTHON_EN:\npython -c "import requests; r=requests.get(\'{full}\', timeout=20); '
                f'print(r.status_code); print(r.text[:300])"'
            )
            if full_ru:
                print(
                    f'PYTHON_RU:\npython -c "import requests; r=requests.get(\'{full_ru}\', timeout=20); '
                    f'print(r.status_code); print(r.text[:300])"'
                )
            print()
    else:
        for n, item in enumerate(wiki_ru[:10], 1):
            url = item.get("url") or ""
            print(f"--- WIKI RU #{n} ---")
            print(f"error: {(item.get('err') or '')[:160]}")
            print(f"URL: {url}")
            print(
                f'PYTHON:\npython -c "import requests; r=requests.get(\'{url}\', timeout=20); '
                f'print(r.status_code); print(r.text[:300])"'
            )
            print()


if __name__ == "__main__":
    main()

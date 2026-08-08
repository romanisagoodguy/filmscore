"""Probe Wikipedia REST titles: wrong (film) suffix vs bare titles."""
from __future__ import annotations

import requests
from urllib.parse import quote

UA = {
    "User-Agent": "PsychoFilmAnalyzer/1.0 (+research; educational; contact: local)",
    "Accept": "application/json",
}

TITLES = [
    # forms used by gather plan (EN always appends " (film)")
    "Barbie and Her Sisters in the Great Puppy Adventure (film)",
    "Barbie in The Pink Shoes (film)",
    "Shaun the Sheep Movie (film)",
    "Balto III: Wings of Change (film)",
    "Balto: Wolf Quest (film)",
    "Barbie in Rock N Royals (film)",
    "Barbie Video Game Hero (film)",
    # likely correct pages
    "Barbie and Her Sisters in the Great Puppy Adventure",
    "Barbie in the Pink Shoes",
    "Shaun the Sheep Movie",
    "Balto III: Wings of Change",
    "Balto: Wolf Quest",
    "Balto (film)",
    "Barbie in Rock 'N Royals",
    "Barbie: Video Game Hero",
]


def main() -> None:
    for t in TITLES:
        url = "https://en.wikipedia.org/api/rest_v1/page/summary/" + quote(t, safe="")
        try:
            r = requests.get(url, headers=UA, timeout=15)
            body = r.text[:100].replace("\n", " ")
            print(f"{r.status_code:3d} | {t[:58]:58s} | {body}")
        except Exception as exc:  # noqa: BLE001
            print(f"EXC | {t[:58]:58s} | {exc}")


if __name__ == "__main__":
    main()

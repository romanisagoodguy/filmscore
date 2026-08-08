"""Verify multi-step wiki resolve + 1:1 attribution on known films."""
from __future__ import annotations

import json
import sys

import requests

from psychofilm_analyzer.gather_v2.wiki_resolve import resolve_wikipedia

CASES = [
    {"english_title": "Balto", "year": 1995},
    {"english_title": "Balto: Wolf Quest", "year": 2002},
    {"english_title": "Shaun the Sheep Movie", "year": 2015},
    {"english_title": "Barbie in The Pink Shoes", "year": 2013},
    {
        "english_title": "Barbie and Her Sisters in the Great Puppy Adventure",
        "year": 2015,
    },
]


def main() -> int:
    session = requests.Session()
    headers = {
        "User-Agent": "PsychoFilmAnalyzer/1.0 (+research; educational; contact: local)",
        "Accept": "application/json",
    }
    delays = {"n": 0}

    def throttle() -> None:
        import time

        time.sleep(1.2)  # polite
        delays["n"] += 1

    ok_n = 0
    for case in CASES:
        print("=" * 72)
        print("CASE", case)
        res = resolve_wikipedia(
            session=session,
            lang="en",
            english_title=case["english_title"],
            film_title=case["english_title"],
            year=case.get("year"),
            headers=headers,
            timeout=20.0,
            throttle=throttle,
        )
        print("  ok=", res.ok, "class=", res.classification, "title=", res.final_title)
        print(
            "  ATTR step=",
            res.attributed_step,
            "http=",
            res.attributed_http_status,
            "at=",
            res.attributed_captured_at,
        )
        print("  ATTR cmd starts:", (res.attributed_command or "")[:120], "...")
        print("  attempts:")
        for a in res.attempts:
            print(
                f"    step={a.step} kind={a.kind:20s} http={a.http_status} "
                f"class={a.classification:14s} title={a.page_title!r}"
            )
            # Binding check: attributed command must equal some attempt's command
        cmds = {a.reproducible_command for a in res.attempts}
        if res.attributed_command not in cmds:
            print("  BINDING FAIL: attributed command not in attempt set")
            return 2
        # For the attributed attempt, http must match
        att = next(a for a in res.attempts if a.step == res.attributed_step)
        if att.http_status != res.attributed_http_status:
            print("  BINDING FAIL: http mismatch")
            return 2
        if att.reproducible_command != res.attributed_command:
            print("  BINDING FAIL: command mismatch")
            return 2
        print("  BINDING OK for step", res.attributed_step)
        if res.ok:
            ok_n += 1
        # small dump of attribution JSON
        print("  attribution:", json.dumps(res.to_dict()["attribution"], ensure_ascii=False)[:200])

    print("=" * 72)
    print(f"resolved_ok={ok_n}/{len(CASES)}")
    return 0 if ok_n >= 2 else 1


if __name__ == "__main__":
    sys.exit(main())

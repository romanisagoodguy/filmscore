"""Resolve dependent request URLs/params from parent responses."""

from __future__ import annotations

import json
from typing import Any, Optional

from psychofilm_analyzer.gather_v2.models import PlanRequest, STATUS_FAILED, STATUS_SKIPPED, STATUS_SUCCESS
from psychofilm_analyzer.gather_v2.plan_store import PlanStore
from psychofilm_analyzer.utils.text import safe_int


def _pick_tmdb(results: list[dict], year: Optional[int]) -> Optional[dict]:
    if not results:
        return None
    if not year:
        return results[0]
    scored = []
    for r in results:
        date = r.get("release_date") or r.get("first_air_date") or ""
        y = safe_int(date[:4]) if date else None
        dist = abs(y - year) if y else 99
        scored.append((dist, -(r.get("popularity") or 0), r))
    scored.sort(key=lambda x: (x[0], x[1]))
    return scored[0][2]


def _pick_kp(films: list[dict], year: Optional[int]) -> Optional[dict]:
    if not films:
        return None
    if not year:
        return films[0]
    best = films[0]
    best_dist = 999
    for f in films:
        y = safe_int(f.get("year") or (str(f.get("year") or "").split("-")[0]))
        if y is None:
            continue
        dist = abs(y - year)
        if dist < best_dist:
            best_dist = dist
            best = f
    return best


def resolve_request(store: PlanStore, req: PlanRequest) -> tuple[bool, str]:
    """
    Fill url/params for a dependent request.
    Returns (ok_to_run, error_message).
    """
    hint = (req.resolve_hint or "").strip()
    deps = req.dep_ids()

    # Letterboxd chain: run slug_N only if earlier slug finished and failed
    if hint == "letterboxd_slug_chain" or (
        req.site == "letterboxd" and req.endpoint_type.startswith("slug_")
    ):
        try:
            n = int(req.endpoint_type.split("_")[1])
        except (IndexError, ValueError):
            n = 0
        if n > 0:
            for i in range(n):
                earlier = store.get(f"f{req.film_index:05d}_letterboxd_slug_{i}")
                if not earlier:
                    return False, f"missing earlier slug {i}"
                if earlier.status not in (STATUS_SUCCESS, STATUS_FAILED, STATUS_SKIPPED):
                    return False, f"dependency letterboxd slug_{i} not finished"
                if earlier.status == STATUS_SUCCESS:
                    return False, "earlier letterboxd slug succeeded"
            return True, ""

    if not deps and not hint:
        return True, ""

    # Check deps
    for d in deps:
        parent = store.get(d)
        if not parent:
            return False, f"missing dependency {d}"
        if parent.status not in (STATUS_SUCCESS, STATUS_FAILED, STATUS_SKIPPED):
            return False, f"dependency {d} not finished"
        if parent.status != STATUS_SUCCESS and hint not in ("",):
            # For omdb_imdb_from_tmdb, if TMDB failed we skip by_id
            if hint == "omdb_imdb_from_tmdb":
                return False, f"dependency {d} failed — skip omdb by_id"
            if hint.startswith("tmdb_") or hint.startswith("kp_"):
                return False, f"dependency {d} not success"

    if not hint:
        return True, ""

    parent_id = deps[0] if deps else ""
    parent_data = store.load_response(parent_id) if parent_id else None

    if hint == "tmdb_details":
        if not isinstance(parent_data, dict):
            return False, "no tmdb search payload"
        results = parent_data.get("results") or []
        pick = _pick_tmdb(results, req.year)
        if not pick:
            return False, "tmdb search empty"
        tmdb_id = pick.get("id")
        media = "tv" if pick.get("media_type") == "tv" else "movie"
        # search endpoint already uses /search/movie or /search/tv
        # infer media from parent URL if needed
        parent_req = store.get(parent_id)
        if parent_req and "/search/tv" in (parent_req.url or ""):
            media = "tv"
        elif parent_req and "/search/movie" in (parent_req.url or ""):
            media = "movie"
        req.url = f"https://api.themoviedb.org/3/{media}/{tmdb_id}"
        return True, ""

    if hint == "omdb_imdb_from_tmdb":
        if not isinstance(parent_data, dict):
            return False, "no tmdb details for imdb"
        ext = parent_data.get("external_ids") or {}
        imdb = ext.get("imdb_id") or parent_data.get("imdb_id")
        if not imdb:
            return False, "no imdb_id on tmdb details"
        try:
            params = json.loads(req.params_json or "{}")
        except json.JSONDecodeError:
            params = {}
        params["i"] = imdb
        req.params_json = json.dumps(params, ensure_ascii=False)
        req.url = req.url or "https://www.omdbapi.com/"
        return True, ""

    if hint in {"kp_details", "kp_staff", "pick_kp_search"}:
        pass

    if hint == "kp_details" or hint == "kp_staff":
        if not isinstance(parent_data, dict):
            return False, "no kp search payload"
        films = parent_data.get("films") or []
        pick = _pick_kp(films, req.year)
        if not pick:
            return False, "kp search empty"
        kp_id = pick.get("filmId") or pick.get("kinopoiskId") or pick.get("id")
        if not kp_id:
            return False, "no kp id"
        if hint == "kp_details":
            req.url = f"https://kinopoiskapiunofficial.tech/api/v2.2/films/{kp_id}"
        else:
            req.url = "https://kinopoiskapiunofficial.tech/api/v1/staff"
            req.params_json = json.dumps({"filmId": kp_id}, ensure_ascii=False)
        return True, ""

    return True, ""


def deps_ready(store: PlanStore, req: PlanRequest) -> bool:
    for d in req.dep_ids():
        parent = store.get(d)
        if not parent:
            return False
        if parent.status not in (STATUS_SUCCESS, STATUS_FAILED, STATUS_SKIPPED):
            return False
    # letterboxd chain soft skip handled in resolve
    return True

"""
Wikipedia multi-step resolve for Approach 2.

For each film/lang:
  1) Try direct REST summary on candidate titles (bare first, then disambiguators)
  2) MediaWiki search API
  3) REST summary on search hits

Every HTTP call is recorded as an ATTEMPT with:
  - exact url / params / headers
  - exact reproducible command
  - captured_at timestamp
  - http_status + body_preview
  - classification (bound 1:1 to that command)

Error handling policy:
  rate_limit (429)  → stop, defer whole request; repro = THE 429 attempt command
  not_found (404)   → try next candidate/search; never label as rate_limit
  network/5xx       → stop, defer; repro = that attempt
  success (200+extract) → success; repro = the successful attempt command
  exhausted         → skipped/failed not_found; repro = last attempt + full chain in result JSON
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import quote

from psychofilm_analyzer.utils.localtime import now_str


def _now() -> str:
    return now_str(with_ms=True)


# Classifications (stable codes used in reports / deferred_reason)
CLASS_SUCCESS = "success"
CLASS_NOT_FOUND = "not_found"
CLASS_DISAMBIGUATION = "disambiguation"
CLASS_RATE_LIMIT = "rate_limit"
CLASS_SERVER_ERROR = "server_error"
CLASS_CLIENT_ERROR = "client_error"
CLASS_NETWORK = "network_error"
CLASS_EMPTY_BODY = "empty_or_unusable"
CLASS_UNKNOWN = "unknown"


@dataclass
class WikiAttempt:
    step: int
    kind: str  # summary_direct | search | summary_from_search
    page_title: str
    lang: str
    url: str
    method: str = "GET"
    params: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    reproducible_command: str = ""
    captured_at: str = ""
    http_status: Optional[int] = None
    body_preview: str = ""
    classification: str = CLASS_UNKNOWN
    note: str = ""
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WikiResolveResult:
    ok: bool
    classification: str
    lang: str
    final_title: str = ""
    summary: Optional[dict[str, Any]] = None
    attempts: list[WikiAttempt] = field(default_factory=list)
    # The single command that owns the final outcome (success OR the error that stopped us)
    attributed_command: str = ""
    attributed_url: str = ""
    attributed_http_status: Optional[int] = None
    attributed_captured_at: str = ""
    attributed_step: int = 0
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "classification": self.classification,
            "lang": self.lang,
            "final_title": self.final_title,
            "summary": self.summary,
            "attribution": {
                "step": self.attributed_step,
                "http_status": self.attributed_http_status,
                "captured_at": self.attributed_captured_at,
                "url": self.attributed_url,
                "reproducible_command": self.attributed_command,
                "classification": self.classification,
                "note": self.message,
            },
            "attempts": [a.to_dict() for a in self.attempts],
            "procedure": [
                "1. Direct REST summary on candidate titles (bare → disambiguators)",
                "2. MediaWiki search API (srsearch film/year)",
                "3. REST summary on search hit titles",
                "4. Stop on rate_limit/network/5xx; continue on not_found only",
            ],
        }


def classify_http(
    status: Optional[int],
    body_text: str,
    data: Any,
    *,
    kind: str,
) -> tuple[str, str]:
    """Return (classification, note) for this exact response."""
    if status == 429:
        return CLASS_RATE_LIMIT, "HTTP 429 rate limit (transient; retest later may differ)"
    if status is not None and status >= 500:
        return CLASS_SERVER_ERROR, f"HTTP {status} server error"
    if status == 404:
        # REST often returns {"status":404,"type":"Internal error"} for missing titles
        return CLASS_NOT_FOUND, "HTTP 404 — page title does not exist on this wiki"
    if status is not None and status >= 400:
        return CLASS_CLIENT_ERROR, f"HTTP {status} client error"

    if status is None:
        return CLASS_NETWORK, "no HTTP status (network/exception)"

    # 2xx
    if kind == "search":
        if not isinstance(data, dict):
            return CLASS_EMPTY_BODY, "search response not JSON"
        hits = ((data.get("query") or {}).get("search")) or []
        if not hits:
            return CLASS_NOT_FOUND, "search returned zero hits"
        return CLASS_SUCCESS, f"search returned {len(hits)} hit(s)"

    # summary
    if not isinstance(data, dict):
        return CLASS_EMPTY_BODY, "summary response not JSON object"
    typ = str(data.get("type") or "")
    if "not_found" in typ or data.get("status") == 404:
        return CLASS_NOT_FOUND, "REST summary: not_found / status 404"
    if typ == "disambiguation" or data.get("type") == "disambiguation":
        return CLASS_DISAMBIGUATION, "disambiguation page (not a film article)"
    extract = (data.get("extract") or "").strip()
    if not extract and not data.get("title"):
        return CLASS_EMPTY_BODY, "200 but empty/unusable summary"
    if not extract:
        return CLASS_EMPTY_BODY, "200 but no extract text"
    return CLASS_SUCCESS, f"summary ok title={data.get('title')!r}"


def candidate_titles(
    *,
    english_title: str = "",
    film_title: str = "",
    year: Optional[int] = None,
    lang: str = "en",
    is_tv: bool = False,
) -> list[str]:
    """
    Ordered title forms to try for REST summary.
    Bare title FIRST (many pages have no (film) suffix), then disambiguators.
    """
    base = (english_title or film_title or "").strip()
    if not base:
        return []
    out: list[str] = []

    def add(t: str) -> None:
        t = (t or "").strip()
        if t and t.lower() not in {x.lower() for x in out}:
            out.append(t)

    # 1) bare
    add(base)
    # 2) common EN disambiguators
    if is_tv:
        if lang == "en":
            add(f"{base} (TV series)")
            if year:
                add(f"{base} ({year} TV series)")
        elif lang == "ru":
            add(f"{base} (сериал)")
        # de: bare often enough
    else:
        if lang == "en":
            add(f"{base} (film)")
            if year:
                add(f"{base} ({year} film)")
        elif lang == "ru":
            add(f"{base} (фильм)")
            if year:
                add(f"{base} (фильм, {year})")
        # de bare first already

    # 3) if film_title differs from english, try it bare (RU catalog names rarely work on EN)
    if film_title and film_title.strip().lower() != base.lower() and lang != "en":
        add(film_title.strip())

    return out


def search_query(
    *,
    english_title: str = "",
    film_title: str = "",
    year: Optional[int] = None,
    is_tv: bool = False,
) -> str:
    q = (english_title or film_title or "").strip()
    if year:
        q = f"{q} {year}"
    if is_tv:
        q = f"{q} television series"
    else:
        q = f"{q} film"
    return q.strip()


def _summary_url(lang: str, title: str) -> str:
    return f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{quote(title, safe='')}"


def _search_url(lang: str) -> str:
    return f"https://{lang}.wikipedia.org/w/api.php"


def resolve_wikipedia(
    *,
    session: Any,
    lang: str,
    english_title: str = "",
    film_title: str = "",
    year: Optional[int] = None,
    is_tv: bool = False,
    headers: Optional[dict[str, str]] = None,
    timeout: float = 15.0,
    throttle: Optional[Callable[[], None]] = None,
    # Prefer plan-row spacing outside resolve; inside use micro_gap only.
    inter_http_gap_sec: float = 0.08,
    max_direct_titles: int = 2,
    max_search_hits: int = 3,
    # 429 is TRANSIENT. Retry same URL until durable status (404/200/5xx) so
    # reported CAPTURED_HTTP matches independent retest after cool-down.
    max_429_retries: int = 6,
    cool_base_sec: float = 45.0,
    cool_step_sec: float = 15.0,
    stop_event: Any = None,
) -> WikiResolveResult:
    """
    Run the full search/resolve procedure.

    CRITICAL: HTTP 429 is NEVER the final page truth. On 429 we log the transient
    hit, cool down, and re-request THE SAME URL until we get a durable code
    (200 / 404 / 5xx / network). Outcome attribution uses only the durable attempt,
    so pasting THIS_COMMAND_ONLY reproduces the same class of result (404 not_found,
    not a stale 429).
    """
    import time as _time

    lang = (lang or "en").strip().lower()
    if lang in {"ge", "german", "deu"}:
        lang = "de"
    if headers:
        hdrs = dict(headers)
    else:
        try:
            from psychofilm_analyzer.utils.wikipedia_auth import wikipedia_headers

            hdrs = wikipedia_headers()
        except Exception:
            hdrs = {
                "User-Agent": (
                    "PsychoFilmAnalyzer/1.0 "
                    "(contact: romangermanyberlin@gmail.com; educational/research)"
                ),
                "Accept": "application/json, text/html, */*",
            }
    attempts: list[WikiAttempt] = []
    step = 0
    cool_next = float(cool_base_sec)
    _http_n = 0

    def do_get(url: str, params: Optional[dict] = None) -> tuple[Optional[int], str, Any, float, Optional[str]]:
        """Returns status, body_text, data, ms, exception_name.

        Adaptive RPM should run *once per plan row* (caller). Between title/search
        attempts we only apply a small inter_http_gap so multi-step resolves are
        not charged full DELAY_SEC per GET.
        """
        nonlocal _http_n
        if throttle:
            throttle()
        elif _http_n > 0 and inter_http_gap_sec > 0:
            if stop_event is not None and hasattr(stop_event, "wait"):
                stop_event.wait(inter_http_gap_sec)
            else:
                _time.sleep(inter_http_gap_sec)
        _http_n += 1
        mono0 = _time.monotonic()
        try:
            resp = session.get(url, params=params or None, headers=hdrs, timeout=timeout)
            ms = (_time.monotonic() - mono0) * 1000.0
            body = ""
            try:
                body = resp.text or ""
            except Exception:
                body = ""
            data: Any
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "json" in ctype or (body[:1] in ("{", "[")):
                try:
                    data = resp.json()
                except Exception:
                    data = {"_raw": body[:5000]}
            else:
                data = {"_html": body[:8000], "_content_type": ctype}
            return resp.status_code, body, data, ms, None
        except Exception as exc:  # noqa: BLE001
            ms = (_time.monotonic() - mono0) * 1000.0
            return None, str(exc)[:500], None, ms, f"{type(exc).__name__}: {exc}"

    def record(
        *,
        kind: str,
        page_title: str,
        url: str,
        params: Optional[dict],
        status: Optional[int],
        body: str,
        data: Any,
        ms: float,
        exc: Optional[str],
        note_extra: str = "",
    ) -> WikiAttempt:
        nonlocal step
        step += 1
        from psychofilm_analyzer.gather_v2.executor import build_reproducible_command

        cmd = build_reproducible_command(
            method="GET",
            url=url,
            params=params or {},
            headers=hdrs,
            timeout=timeout,
        )
        if exc:
            cls, note = CLASS_NETWORK, exc
        else:
            cls, note = classify_http(status, body, data, kind=kind)
        if note_extra:
            note = f"{note} | {note_extra}"
        att = WikiAttempt(
            step=step,
            kind=kind,
            page_title=page_title,
            lang=lang,
            url=url,
            method="GET",
            params=dict(params or {}),
            headers=dict(hdrs),
            reproducible_command=cmd,
            captured_at=_now(),
            http_status=status,
            body_preview=(body or "")[:400],
            classification=cls,
            note=note,
            duration_ms=round(ms, 1),
        )
        attempts.append(att)
        return att

    def cool_sleep(seconds: float) -> None:
        if seconds <= 0:
            return
        if stop_event is not None and hasattr(stop_event, "wait"):
            stop_event.wait(seconds)
        else:
            _time.sleep(seconds)

    def request_durable(
        *,
        kind: str,
        page_title: str,
        url: str,
        params: Optional[dict] = None,
    ) -> tuple[WikiAttempt, Any]:
        """
        GET until status is durable (not 429).

        - Every 429 is logged as a TRANSIENT attempt (for audit).
        - Same URL is retried after cool-down.
        - Final returned attempt is durable: 200/404/5xx/network.
        - Only if max_429_retries exhausted do we return rate_limit as final.
        """
        nonlocal cool_next
        last_data: Any = None
        durable: Optional[WikiAttempt] = None
        for i in range(max(1, int(max_429_retries)) + 1):
            status, body, data, ms, exc = do_get(url, params)
            last_data = data
            extra = ""
            if status == 429:
                extra = (
                    f"TRANSIENT rate_limit (retry {i + 1}/{max_429_retries}); "
                    f"cooling {cool_next:.0f}s then SAME url will be re-fetched "
                    f"so final status is durable (404/200), not 429"
                )
            att = record(
                kind=kind if status != 429 else f"{kind}_429_transient",
                page_title=page_title,
                url=url,
                params=params,
                status=status,
                body=body,
                data=data,
                ms=ms,
                exc=exc,
                note_extra=extra,
            )
            if att.classification != CLASS_RATE_LIMIT:
                durable = att
                break
            # 429: cool then retry same URL
            if i >= int(max_429_retries):
                durable = att
                durable.note = (
                    f"rate_limit exhausted after {max_429_retries} cool-retries on same URL; "
                    f"page truth still unknown — retest later may show 404/200"
                )
                break
            cool_sleep(cool_next)
            cool_next = cool_next + float(cool_step_sec)
        assert durable is not None
        return durable, last_data

    def finish_from_attempt(
        att: WikiAttempt,
        *,
        ok: bool,
        summary: Optional[dict] = None,
        final_title: str = "",
        message: str = "",
    ) -> WikiResolveResult:
        # Prefer durable classification (never attribute outcome to a transient 429
        # if a later durable attempt exists for reporting — caller passes durable att)
        return WikiResolveResult(
            ok=ok,
            classification=CLASS_SUCCESS if ok else att.classification,
            lang=lang,
            final_title=final_title or att.page_title,
            summary=summary,
            attempts=attempts,
            attributed_command=att.reproducible_command,
            attributed_url=att.url
            + (
                ("?" + "&".join(f"{k}={v}" for k, v in att.params.items()))
                if att.params
                else ""
            ),
            attributed_http_status=att.http_status,
            attributed_captured_at=att.captured_at,
            attributed_step=att.step,
            message=message or att.note,
        )

    # --- Phase 1: direct summary candidates (bulk: bare + (film) only) ---
    titles = candidate_titles(
        english_title=english_title,
        film_title=film_title,
        year=year,
        lang=lang,
        is_tv=is_tv,
    )
    # Cap direct tries so we reach MediaWiki search faster on hard titles
    if max_direct_titles > 0:
        titles = titles[: max(1, int(max_direct_titles))]
    for title in titles:
        url = _summary_url(lang, title)
        att, data = request_durable(
            kind="summary_direct", page_title=title, url=url, params=None
        )
        if att.classification == CLASS_RATE_LIMIT:
            # only if cool-retries exhausted
            return finish_from_attempt(
                att,
                ok=False,
                message=f"rate_limit exhausted on title={title!r} (truth unknown)",
            )
        if att.classification in {CLASS_NETWORK, CLASS_SERVER_ERROR}:
            return finish_from_attempt(
                att,
                ok=False,
                message=f"{att.classification} on direct summary title={title!r}",
            )
        if att.classification == CLASS_SUCCESS and isinstance(data, dict):
            return finish_from_attempt(
                att,
                ok=True,
                summary=data,
                final_title=str(data.get("title") or title),
                message=f"resolved via direct summary: {title!r}",
            )
        # durable not_found / disambiguation / empty → next candidate

    # --- Phase 2: MediaWiki search ---
    q = search_query(
        english_title=english_title,
        film_title=film_title,
        year=year,
        is_tv=is_tv,
    )
    search_params = {
        "action": "query",
        "list": "search",
        "srsearch": q,
        "srlimit": max_search_hits,
        "format": "json",
        "utf8": 1,
    }
    s_url = _search_url(lang)
    satt, data = request_durable(
        kind="search", page_title=q, url=s_url, params=search_params
    )
    if satt.classification == CLASS_RATE_LIMIT:
        return finish_from_attempt(
            satt, ok=False, message=f"rate_limit exhausted on search q={q!r}"
        )
    if satt.classification in {CLASS_NETWORK, CLASS_SERVER_ERROR}:
        return finish_from_attempt(
            satt, ok=False, message=f"{satt.classification} on search q={q!r}"
        )

    hits: list[str] = []
    if satt.classification == CLASS_SUCCESS and isinstance(data, dict):
        for h in ((data.get("query") or {}).get("search")) or []:
            t = (h.get("title") or "").strip()
            if t:
                hits.append(t)

    if not hits:
        last = attempts[-1]
        # Prefer last durable not_found for attribution (user can retest → 404)
        for a in reversed(attempts):
            if a.classification == CLASS_NOT_FOUND and a.http_status == 404:
                last = a
                break
        return finish_from_attempt(
            last,
            ok=False,
            message=f"not_found after candidates + search q={q!r}",
        )

    # --- Phase 3: summary for each search hit ---
    for hit in hits:
        if hit.lower() in {t.lower() for t in titles}:
            continue
        url = _summary_url(lang, hit)
        att, data = request_durable(
            kind="summary_from_search", page_title=hit, url=url, params=None
        )
        if att.classification == CLASS_RATE_LIMIT:
            return finish_from_attempt(
                att,
                ok=False,
                message=f"rate_limit exhausted on search-hit title={hit!r}",
            )
        if att.classification in {CLASS_NETWORK, CLASS_SERVER_ERROR}:
            return finish_from_attempt(
                att,
                ok=False,
                message=f"{att.classification} on search-hit summary title={hit!r}",
            )
        if att.classification == CLASS_SUCCESS and isinstance(data, dict):
            return finish_from_attempt(
                att,
                ok=True,
                summary=data,
                final_title=str(data.get("title") or hit),
                message=f"resolved via search hit: {hit!r} (q={q!r})",
            )

    # Prefer a durable 404 attempt as attributed outcome (retestable)
    last = attempts[-1]
    for a in reversed(attempts):
        if a.classification == CLASS_NOT_FOUND:
            last = a
            break
    if last.classification not in {
        CLASS_RATE_LIMIT,
        CLASS_NETWORK,
        CLASS_SERVER_ERROR,
        CLASS_NOT_FOUND,
    }:
        last.classification = CLASS_NOT_FOUND
        last.note = f"search hits tried but no usable film summary; q={q!r}"
    return finish_from_attempt(
        last,
        ok=False,
        message=f"not_found after search hits; q={q!r}; attempts={len(attempts)}",
    )


def format_attempt_block(att: WikiAttempt, *, request_id: str = "") -> str:
    """Human-readable attributed error block for logs/reports."""
    return (
        f"--- ATTEMPT step={att.step} kind={att.kind} ---\n"
        f"request_id: {request_id}\n"
        f"CAPTURED_AT: {att.captured_at}\n"
        f"CAPTURED_HTTP: {att.http_status}\n"
        f"CLASSIFICATION: {att.classification}\n"
        f"NOTE: {att.note}\n"
        f"PAGE_TITLE: {att.page_title}\n"
        f"LANG: {att.lang}\n"
        f"URL: {att.url}\n"
        f"PARAMS: {att.params}\n"
        f"BODY_PREVIEW: {(att.body_preview or '')[:200]!r}\n"
        f"THIS_COMMAND_ONLY (paste into PowerShell — result may differ if rate-limit cooled):\n"
        f"{att.reproducible_command}\n"
        f"BINDING: CAPTURED_HTTP={att.http_status} is bound ONLY to the command above "
        f"at CAPTURED_AT={att.captured_at}. Retest later can return a different code.\n"
    )

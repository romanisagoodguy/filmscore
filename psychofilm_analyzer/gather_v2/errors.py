"""Error taxonomy for Approach 3: recoverable (retry) vs terminal (never end-queue).

Rules of thumb:
  - Terminal: content does not exist / cannot be found / soft business miss.
  - Recoverable: transport, rate limit, 5xx, timeouts — worth retrying later.
"""

from __future__ import annotations

from typing import Any, Optional

# Reasons that may succeed on a later retry (end-queue allowed)
RECOVERABLE_REASONS = frozenset(
    {
        "429",
        "rate_limit",
        "5xx",
        "server_error",
        "exception",
        "network_error",
        "timeout",
        "ssl_error",
        "connection_error",
        "proxy_error",
        "cloudflare",  # may clear later
        "http_500",
        "http_502",
        "http_503",
        "http_504",
        "interrupted",
    }
)

# Reasons that must NOT be re-requested at end of queue
TERMINAL_REASONS = frozenset(
    {
        "not_found",
        "omdb_not_found",
        "kp search empty",
        "kp_search_empty",
        "empty",
        "404",
        "http_404",
        "http_400",  # bad request to API — retry usually useless
        "http_401",
        "http_403",  # auth/forbidden without fix
        "client_error",
        "disambiguation",  # treated as soft miss after resolve
        "dependency_failed",
        "skipped",
        "permanent",
    }
)


def normalize_reason(reason: str = "", *, http_status: Any = None, error: str = "") -> str:
    r = (reason or "").strip().lower()
    err = (error or "").lower()
    try:
        code = int(http_status) if http_status is not None else None
    except (TypeError, ValueError):
        code = None

    if code == 429 or "429" in r or "rate_limit" in r or "rate limit" in err:
        return "rate_limit"
    if code is not None and 500 <= code <= 599:
        return "5xx"
    if code == 404 or r in {"404", "http_404", "not_found"} or "not_found" in r:
        return "not_found"
    if code == 400 or r in {"400", "http_400"}:
        return "http_400"
    if code in {401, 403}:
        return f"http_{code}"
    if "timeout" in r or "timeout" in err or "timed out" in err:
        return "timeout"
    if "ssl" in r or "ssl" in err or "certificate" in err:
        return "ssl_error"
    if "connection" in r or "connection" in err or "remotedisconnected" in err:
        return "connection_error"
    if "proxy" in r or "proxy" in err:
        return "proxy_error"
    if "cloudflare" in r or "cloudflare" in err or "just a moment" in err:
        return "cloudflare"
    if "omdb_not_found" in r or "movie not found" in err:
        return "omdb_not_found"
    if "search empty" in r or "search empty" in err or "kp search empty" in err:
        return "kp_search_empty"
    if "exception" in r or "error" in r:
        # generic exception — recoverable unless clearly terminal in message
        if any(x in err for x in ("not found", "404", "no results")):
            return "not_found"
        return "exception"
    if r:
        return r
    if code is not None:
        return f"http_{code}"
    return "unknown"


def is_recoverable(
    reason: str = "",
    *,
    http_status: Any = None,
    error: str = "",
) -> bool:
    """True → may go to end-queue / retry. False → terminal skip/fail, never end-queue."""
    norm = normalize_reason(reason, http_status=http_status, error=error)
    if norm in TERMINAL_REASONS:
        return False
    if norm in RECOVERABLE_REASONS:
        return True
    # Unknown HTTP 4xx (except listed) → terminal; unknown else → recoverable if looks transporty
    try:
        code = int(http_status) if http_status is not None else None
    except (TypeError, ValueError):
        code = None
    if code is not None and 400 <= code < 500 and code != 429:
        return False
    if code is not None and code >= 500:
        return True
    # default: if we got an exception-like error, retry; else terminal
    err = (error or "").lower()
    if any(
        x in err
        for x in (
            "timeout",
            "connection",
            "ssl",
            "proxy",
            "temporarily",
            "reset",
            "refused",
            "unavailable",
        )
    ):
        return True
    if norm == "unknown":
        return bool(err)  # bare unknown with message → try once more class
    return False


def is_terminal_outcome(
    reason: str = "",
    *,
    http_status: Any = None,
    error: str = "",
) -> bool:
    return not is_recoverable(reason, http_status=http_status, error=error)


def classify_for_progress(status: str, deferred_reason: str = "", error: str = "", http_status: Any = None) -> str:
    """
    Bucket for progress accounting:
      success | terminal | recoverable_open | other_open
    """
    st = (status or "").lower()
    if st == "success":
        return "success"
    if st in {"failed", "skipped"}:
        return "terminal"
    if st in {"deferred", "retry"}:
        if is_recoverable(deferred_reason, http_status=http_status, error=error):
            return "recoverable_open"
        return "terminal"  # misclassified deferred → treat as done for %
    if st in {"pending", "running"}:
        return "other_open"
    return "other_open"

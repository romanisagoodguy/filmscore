"""Local system timestamps for human-readable reports and logs.

All user-facing text times should use these helpers (not UTC).
"""

from __future__ import annotations

from datetime import datetime


def now_local(*, with_ms: bool = False) -> datetime:
    """Current time in the machine's local timezone."""
    return datetime.now().astimezone()


def format_local(dt: datetime | None = None, *, with_ms: bool = False) -> str:
    """
    Human-readable local time, e.g.:
      2026-08-09 04:45:10 +05:00
      2026-08-09 04:45:10.573 +05:00
    """
    if dt is None:
        dt = now_local()
    elif dt.tzinfo is None:
        # treat naive as local wall clock
        dt = dt.astimezone()
    else:
        dt = dt.astimezone()  # convert to local
    if with_ms:
        body = dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    else:
        body = dt.strftime("%Y-%m-%d %H:%M:%S")
    off = dt.strftime("%z")  # +0500
    if off and len(off) >= 5:
        off = f"{off[:3]}:{off[3:5]}"
        return f"{body} {off}"
    return body


def stamp_local() -> str:
    """Filesystem-safe local stamp: 20260809_044510"""
    return now_local().strftime("%Y%m%d_%H%M%S")


# Short alias used by many modules (historical name _utc → now local)
def now_str(*, with_ms: bool = False) -> str:
    return format_local(with_ms=with_ms)

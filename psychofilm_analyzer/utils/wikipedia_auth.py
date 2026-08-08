"""
Wikimedia OAuth credentials and request headers for Wikipedia access.

Credentials are loaded from environment / config.api_keys (never hardcode secrets).
With a personal access token (scopes basic + highvolume), REST summary and
MediaWiki API accept:

  Authorization: Bearer <access_token>
  User-Agent: PsychoFilmAnalyzer/1.0 (contact: you@example.com; research)

Token claim ratelimit.requests_per_unit=5000 / HOUR when highvolume is granted.
"""

from __future__ import annotations

from typing import Any, Optional


DEFAULT_CONTACT_EMAIL = "romangermanyberlin@gmail.com"


def wikipedia_credentials(config: Optional[dict[str, Any]] = None) -> dict[str, str]:
    """Return non-empty credential fields from config.api_keys / nested wikipedia."""
    cfg = config or {}
    keys = dict(cfg.get("api_keys") or {})
    # allow nested override under api_keys.wikipedia or top-level wikipedia.oauth
    nested = keys.get("wikipedia") if isinstance(keys.get("wikipedia"), dict) else {}
    wiki_cfg = dict(cfg.get("wikipedia") or {})
    oauth = dict(wiki_cfg.get("oauth") or {})

    def pick(*names: str) -> str:
        for n in names:
            for src in (keys, nested, oauth):
                if not isinstance(src, dict):
                    continue
                v = src.get(n)
                if v is not None and str(v).strip():
                    return str(v).strip()
        return ""

    return {
        "email": pick("wikipedia_email", "email", "contact_email") or DEFAULT_CONTACT_EMAIL,
        "client_id": pick("wikipedia_client_id", "client_id", "client_key"),
        "client_secret": pick("wikipedia_client_secret", "client_secret"),
        "access_token": pick("wikipedia_access_token", "access_token", "token"),
    }


def wikipedia_user_agent(config: Optional[dict[str, Any]] = None) -> str:
    creds = wikipedia_credentials(config)
    email = creds.get("email") or DEFAULT_CONTACT_EMAIL
    base = ((config or {}).get("http") or {}).get("user_agent") or "PsychoFilmAnalyzer/1.0"
    # Wikimedia policy: identify app + contact
    if email and email not in base:
        return f"{base} (contact: {email}; educational/research)"
    return base


def wikipedia_headers(
    config: Optional[dict[str, Any]] = None,
    *,
    extra: Optional[dict[str, str]] = None,
    include_auth: bool = True,
) -> dict[str, str]:
    """
    Headers for en/ru/de.wikipedia.org REST + action API.

    When access_token is set, adds Authorization: Bearer …
    """
    creds = wikipedia_credentials(config)
    headers: dict[str, str] = {
        "User-Agent": wikipedia_user_agent(config),
        "Accept": "application/json, text/html, */*",
        "Accept-Language": "en-US,en;q=0.9,ru;q=0.8,de;q=0.7",
    }
    if include_auth and creds.get("access_token"):
        headers["Authorization"] = f"Bearer {creds['access_token']}"
    if extra:
        headers.update({k: v for k, v in extra.items() if v is not None})
    return headers


def wikipedia_auth_configured(config: Optional[dict[str, Any]] = None) -> bool:
    return bool(wikipedia_credentials(config).get("access_token"))


def wikipedia_auth_status(config: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Safe status for logs (no secrets)."""
    c = wikipedia_credentials(config)
    tok = c.get("access_token") or ""
    return {
        "email": c.get("email") or "",
        "client_id_set": bool(c.get("client_id")),
        "client_secret_set": bool(c.get("client_secret")),
        "access_token_set": bool(tok),
        "access_token_prefix": (tok[:12] + "…") if len(tok) > 12 else ("set" if tok else ""),
        "user_agent": wikipedia_user_agent(config),
    }

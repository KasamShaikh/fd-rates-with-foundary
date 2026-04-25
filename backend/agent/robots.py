"""robots.txt compliance helper.

Provides a thread-safe cached `is_allowed(url)` check that downloads and
parses each origin's `/robots.txt` once per process and caches the parser.

Behaviour:
- If `ROBOTS_RESPECT` env var is set to a falsy value ("0", "false", "no",
  "off"), all checks return allowed=True (opt-out for testing/private use).
- If the robots.txt cannot be fetched (network error, 4xx, 5xx, timeout),
  we **default-allow** — same behaviour as `urllib.robotparser` when no
  rules are loaded. We log a warning so the operator can see it.
- The user-agent string used for matching is `ROBOTS_USER_AGENT` env var
  (default `FDRateAggregator`). Most banks' robots.txt uses `*` rules,
  which always match.
"""

from __future__ import annotations

import logging
import os
import threading
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests

logger = logging.getLogger(__name__)

_USER_AGENT = os.environ.get("ROBOTS_USER_AGENT", "FDRateAggregator")
_TIMEOUT_SECONDS = 8
_cache: dict[str, RobotFileParser | None] = {}
_lock = threading.Lock()


def _respect_enabled() -> bool:
    val = os.environ.get("ROBOTS_RESPECT", "true").strip().lower()
    return val not in ("0", "false", "no", "off", "")


def _origin(url: str) -> str | None:
    p = urlparse(url)
    if not p.scheme or not p.netloc:
        return None
    return f"{p.scheme}://{p.netloc}"


def _load_parser(origin: str) -> RobotFileParser | None:
    """Fetch and parse robots.txt for an origin. Returns None on failure
    (caller should treat None as allow-all per RFC 9309)."""
    robots_url = origin.rstrip("/") + "/robots.txt"
    try:
        resp = requests.get(
            robots_url,
            timeout=_TIMEOUT_SECONDS,
            headers={"User-Agent": _USER_AGENT},
        )
    except Exception as e:
        logger.warning(
            "robots.txt fetch failed for %s: %s — defaulting to allow", origin, e
        )
        return None

    # 4xx => no robots.txt published; treat as allow-all (RFC 9309 §2.3.1.3)
    if 400 <= resp.status_code < 500:
        logger.info("robots.txt %d for %s — allow-all", resp.status_code, origin)
        rp = RobotFileParser()
        rp.parse([])  # empty rules = allow-all
        return rp

    if resp.status_code >= 500:
        logger.warning(
            "robots.txt %d for %s — defaulting to allow", resp.status_code, origin
        )
        return None

    rp = RobotFileParser()
    try:
        rp.parse(resp.text.splitlines())
    except Exception as e:
        logger.warning(
            "robots.txt parse error for %s: %s — defaulting to allow", origin, e
        )
        return None
    return rp


def is_allowed(url: str) -> tuple[bool, str]:
    """Check whether `url` is permitted by the origin's robots.txt.

    Returns (allowed, reason). `reason` is a short human-readable string for
    logging when disallowed.
    """
    if not _respect_enabled():
        return True, "robots check disabled"

    origin = _origin(url)
    if origin is None:
        return True, "no origin"

    with _lock:
        if origin in _cache:
            rp = _cache[origin]
        else:
            rp = _load_parser(origin)
            _cache[origin] = rp

    if rp is None:
        # network/server error => default-allow
        return True, "robots.txt unavailable"

    try:
        allowed = rp.can_fetch(_USER_AGENT, url)
    except Exception as e:
        logger.warning("can_fetch error for %s: %s — defaulting to allow", url, e)
        return True, "robots check error"

    if allowed:
        return True, "allowed by robots.txt"
    return False, f"disallowed by robots.txt for UA '{_USER_AGENT}'"

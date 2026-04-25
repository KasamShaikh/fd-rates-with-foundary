"""HTTP-level change detection (L1) for FD rate pages.

Strategy
--------
Before invoking the (expensive) Foundry agent + Document Intelligence pipeline
for a bank URL, we send a *conditional* HTTP GET that asks the bank's webserver:
"has this page changed since I last saw it?". Banks publish FD rate revisions
roughly quarterly, so on most runs the answer is **no**, and we can skip the
LLM/DI work entirely and reuse the previously-extracted result.

We use two signals, in order of preference:

1. **HTTP caching headers** (`ETag` and `Last-Modified`) sent back with
   `If-None-Match` / `If-Modified-Since`. A `304 Not Modified` response is the
   cheapest and most reliable "unchanged" signal — it costs ~1 KB of headers
   and 0 tokens / 0 DI pages.
2. **Body sha256 fingerprint**. Some sites disable caching headers but still
   serve byte-identical HTML between runs. We hash the response body and
   compare against the previously-stored hash.

If either signal says "unchanged" *and* we have a cached result from a previous
successful run, we short-circuit and return the cached payload tagged with
`unchanged: True`. Otherwise we fall through to the full scrape path.

Storage
-------
All state lives under `STATE_DIR` (default `backend/_local_results/state/`):

    state/
      url_state.json          — { url_id: {etag, last_modified, sha256,
                                            content_length, last_checked_at,
                                            last_changed_at} }
      per_url/<url_id>.json   — last successful agent result for that URL
                                 (used to reuse `categories` when unchanged)

Fail-open behaviour
-------------------
Any exception during the conditional GET (network error, timeout, SSL hiccup)
triggers a full scrape — we never silently return stale data because of a
transport failure.

Configuration
-------------
- `STATE_DIR`     : override the state directory (default as above).
- `FORCE_REFRESH` : when truthy ("1", "true", "yes", "on") all cache checks
                    return *changed* — guarantees a full scrape this run.
- `HTTP_CACHE_TIMEOUT_SECONDS` : conditional GET timeout (default 15s).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level config + locks
# ---------------------------------------------------------------------------
# A single global lock protects state-file reads/writes so parallel workers
# can't trample each other's updates between load() and save().
_state_lock = threading.Lock()

_TIMEOUT_SECONDS = int(os.environ.get("HTTP_CACHE_TIMEOUT_SECONDS", "15") or "15")

# Browser-style headers — some banks block "python-requests/*" UAs outright.
# Matches the UA used by fetch_webpage_handler so the response is comparable.
_HEADERS_BASE = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
}


def _force_refresh_enabled() -> bool:
    val = os.environ.get("FORCE_REFRESH", "").strip().lower()
    return val in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# HTML normalization for the body fingerprint
# ---------------------------------------------------------------------------
# Many bank pages (SBI is the canonical offender) inject per-request dynamic
# content into otherwise-static HTML: CSRF tokens, session ids, build hashes,
# ad-slot ids, render timestamps, etc. A naive sha256 of the raw bytes will
# therefore differ on every fetch even when the underlying rate tables are
# unchanged.
#
# We compute *two* hashes per response:
#   - sha256_raw  : sha256 of the raw bytes (cheap; matches well-behaved sites).
#   - sha256_norm : sha256 after stripping:
#         * <script>...</script> blocks
#         * <style>...</style> blocks
#         * HTML comments <!-- ... -->
#         * any attribute whose name or value looks token-like
#           (csrf, nonce, session, requestid, timestamp, build, hash)
#         * runs of whitespace
# Either match indicates an unchanged page.
#
# Normalization is intentionally regex-based (no BeautifulSoup dep) and
# conservative: we'd rather report "changed" on a borderline case and pay
# the agent cost than report "unchanged" on a real update.
_RE_SCRIPT = re.compile(rb"<script\b[^>]*>.*?</script\s*>", re.IGNORECASE | re.DOTALL)
_RE_STYLE = re.compile(rb"<style\b[^>]*>.*?</style\s*>", re.IGNORECASE | re.DOTALL)
_RE_COMMENT = re.compile(rb"<!--.*?-->", re.DOTALL)
# Attribute pairs whose *name* contains a dynamic-looking token. Captures the
# entire `name="value"` (or single-quoted, or unquoted) and removes it.
_RE_DYNAMIC_ATTR = re.compile(
    rb"\s+[\w:-]*(csrf|nonce|session|sessionid|sid|requestid|reqid|"
    rb"timestamp|build|buildid|hash|token|viewstate|eventvalidation)[\w:-]*"
    rb"\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)",
    re.IGNORECASE,
)
# Cache-buster query params on asset URLs: ?t=1776998340000, &v=12345, &_=...,
# &amp;t=... (HTML-encoded), etc. Liferay (SBI), WordPress, Drupal all do this.
_RE_CACHE_BUSTER = re.compile(
    rb"([?&]|&amp;)((?:t|v|ts|_|cb|nocache|version|ver|rev)=)\d{6,}",
    re.IGNORECASE,
)
# Hidden inputs whose value is a long digit run (form timestamps like
# Liferay's `formDate` field — changes on every render).
_RE_HIDDEN_TS = re.compile(
    rb"<input\b[^>]*\btype\s*=\s*[\"']hidden[\"'][^>]*\bvalue\s*=\s*[\"']\d{10,}[\"'][^>]*/?>",
    re.IGNORECASE,
)
# Short hex IDs auto-generated by templating engines (e.g. Liferay theme links
# like id="fc32e041"). Regular semantic ids are usually longer or non-hex.
_RE_HEX_ID = re.compile(rb"\bid\s*=\s*[\"'][0-9a-f]{6,10}[\"']", re.IGNORECASE)
_RE_WS = re.compile(rb"\s+")


def _normalize_html(body: bytes) -> bytes:
    """Strip per-request dynamic noise so semantically-equal pages hash equal."""
    if not body:
        return b""
    out = _RE_SCRIPT.sub(b"", body)
    out = _RE_STYLE.sub(b"", out)
    out = _RE_COMMENT.sub(b"", out)
    out = _RE_DYNAMIC_ATTR.sub(b"", out)
    out = _RE_CACHE_BUSTER.sub(rb"\1\2X", out)
    out = _RE_HIDDEN_TS.sub(b"", out)
    out = _RE_HEX_ID.sub(b"", out)
    out = _RE_WS.sub(b" ", out)
    return out.strip()


def _state_dir() -> Path:
    """Resolve and lazily create the on-disk state directory."""
    default = Path(__file__).resolve().parent.parent / "_local_results" / "state"
    p = Path(os.environ.get("STATE_DIR", str(default)))
    (p / "per_url").mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# State file I/O
# ---------------------------------------------------------------------------
def load_state() -> dict[str, dict]:
    """Load the per-URL fingerprint state. Returns {} on first run / corruption."""
    path = _state_dir() / "url_state.json"
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        # Corrupt state file shouldn't block a run — just start fresh.
        logger.warning("url_state.json unreadable (%s) — starting fresh", e)
        return {}


def save_state(state: dict[str, dict]) -> None:
    """Persist the per-URL fingerprint state atomically (write-temp-then-rename)."""
    path = _state_dir() / "url_state.json"
    tmp = path.with_suffix(".json.tmp")
    with _state_lock:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)


def get_cached_result(url_id: str) -> Optional[dict]:
    """Return the last successful agent result for `url_id`, or None."""
    if not url_id:
        return None
    path = _state_dir() / "per_url" / f"{url_id}.json"
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("Cached result for %s unreadable: %s", url_id, e)
        return None


def save_cached_result(url_id: str, result: dict) -> None:
    """Persist a successful agent result so it can be reused next run."""
    if not url_id or not isinstance(result, dict):
        return
    path = _state_dir() / "per_url" / f"{url_id}.json"
    tmp = path.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        logger.warning("Failed to write cached result for %s: %s", url_id, e)


# ---------------------------------------------------------------------------
# Public: change-detection probe
# ---------------------------------------------------------------------------
def check_unchanged(
    url_id: str,
    url: str,
    state: dict[str, dict],
) -> tuple[bool, dict[str, Any]]:
    """Determine whether `url` is byte-/header-identical to the last successful fetch.

    Parameters
    ----------
    url_id : the urls.json id field for this entry.
    url    : the absolute URL to probe.
    state  : the loaded url_state dict (mutated in-place with new fingerprint
             on a successful probe; caller should `save_state` after the run).

    Returns
    -------
    (unchanged, fingerprint) where:
      - unchanged is True only if (a) we have a previous fingerprint AND
        (b) the server returned 304 OR the body sha256 matches stored.
      - fingerprint is the dict that should be merged back into state for
        this run. Fields:
          {
            "etag": str | None,
            "last_modified": str | None,
            "sha256": str | None,
            "content_length": int | None,
            "status_code": int,
            "last_checked_at": iso8601,
            "last_changed_at": iso8601,   # carried forward when unchanged
            "probe_error": str | None,    # set on transport failures
          }

    On any error this returns (False, {...partial info}) — never (True, _).
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    prior = state.get(url_id) or {}

    # Manual override: caller wants a guaranteed full scrape this run.
    if _force_refresh_enabled():
        logger.info("FORCE_REFRESH set — skipping cache check for %s", url)
        return False, {
            **prior,
            "last_checked_at": now_iso,
            "probe_error": "force_refresh",
        }

    # Build conditional headers from whatever the server gave us last time.
    # On the very first run `prior` is empty, so we send an unconditional GET
    # — the response still gives us the etag / last-modified / body hash we
    # need to *seed* the cache so the next run can short-circuit.
    has_prior_fingerprint = bool(
        prior.get("etag") or prior.get("last_modified") or prior.get("sha256")
    )
    headers = dict(_HEADERS_BASE)
    if prior.get("etag"):
        headers["If-None-Match"] = prior["etag"]
    if prior.get("last_modified"):
        headers["If-Modified-Since"] = prior["last_modified"]

    try:
        resp = requests.get(url, headers=headers, timeout=_TIMEOUT_SECONDS)
    except Exception as e:
        # Transport failure — fail-open: pretend nothing is cached so the
        # caller proceeds with the full scrape path.
        logger.warning("Conditional GET failed for %s: %s — falling through", url, e)
        return False, {
            **prior,
            "last_checked_at": now_iso,
            "probe_error": str(e)[:200],
        }

    # 304: server confirms our fingerprint is still current. Cheapest happy path.
    if resp.status_code == 304:
        logger.info("304 Not Modified for %s — reusing cached result", url)
        return True, {
            **prior,
            "status_code": 304,
            "last_checked_at": now_iso,
        }

    # Anything other than a successful body — fall through to full scrape and
    # let the agent layer surface the underlying error.
    if not (200 <= resp.status_code < 300):
        return False, {
            **prior,
            "status_code": resp.status_code,
            "last_checked_at": now_iso,
            "probe_error": f"HTTP {resp.status_code}",
        }

    # 200 OK: compare body fingerprint against stored hash.
    body = resp.content or b""
    new_sha_raw = hashlib.sha256(body).hexdigest()
    new_sha_norm = hashlib.sha256(_normalize_html(body)).hexdigest()
    new_etag = resp.headers.get("ETag")
    new_lm = resp.headers.get("Last-Modified")
    new_len = len(body)

    fingerprint: dict[str, Any] = {
        "etag": new_etag,
        "last_modified": new_lm,
        # `sha256` is the *normalized* hash — the one used for change detection.
        # `sha256_raw` is kept for debugging / forensics.
        "sha256": new_sha_norm,
        "sha256_raw": new_sha_raw,
        "content_length": new_len,
        "status_code": resp.status_code,
        "last_checked_at": now_iso,
        # last_changed_at is updated only when we actually see a change.
        "last_changed_at": prior.get("last_changed_at"),
    }

    # Match against either the normalized hash (preferred) or the raw hash
    # (kept for back-compat with state files written before normalization).
    prior_norm = prior.get("sha256")
    prior_raw = prior.get("sha256_raw")
    if (prior_norm and prior_norm == new_sha_norm) or (
        prior_raw and prior_raw == new_sha_raw
    ):
        # Semantically-identical body even though server didn't honour our
        # conditional request — treat as unchanged.
        logger.info("Body hash unchanged for %s — reusing cached result", url)
        return True, fingerprint

    # Genuine change.
    fingerprint["last_changed_at"] = now_iso
    return False, fingerprint

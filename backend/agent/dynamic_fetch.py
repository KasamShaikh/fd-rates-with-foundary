"""Headless-browser fallback for JavaScript-rendered pages.

Some bank rate pages (ICICI, PNB, Kotak FD, etc.) load their rate tables via
client-side JavaScript widgets. A plain `requests.get` returns only the
shell HTML and never sees the actual numbers. This module uses Playwright
(Chromium) to render the page fully and return the post-JS DOM as HTML.

Optimisations:
    * Per-thread browser reuse — Chromium cold-start is 5-10 s on B1, so we
      launch once per worker thread and reuse it across URLs.
    * Heavy resources (images, fonts, media, third-party trackers) are
      blocked via a route handler. Rate tables are HTML; we don't need the
      hero carousel or marketing pixels.
    * Tight timeouts — bank pages render their tables long before
      networkidle because of the long tail of analytics/marketing scripts.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Per-thread state: each worker thread keeps its own Playwright instance,
# browser, and context for reuse across URLs handled by that thread.
_thread_local = threading.local()

# Resource types dropped entirely — saves bandwidth and avoids waiting on
# slow ad/analytics endpoints that prevent networkidle from firing.
_BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}

# Common third-party hosts that delay networkidle without contributing to
# rate-table content.
_BLOCKED_HOST_FRAGMENTS = (
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "facebook.net",
    "facebook.com/tr",
    "hotjar.com",
    "clarity.ms",
    "linkedin.com/li",
    "newrelic.com",
    "nr-data.net",
    "adservice.google",
    "adsystem.com",
    "criteo",
    "branch.io",
)


# JavaScript snippet executed inside the page that returns a list of
# CSS-locator-friendly indices of clickable tab/accordion controls. We then
# click each one from Python via Playwright's `page.click()` so React/Vue see
# real trusted events (synthetic JS-dispatched events are often ignored).
_FIND_TABS_JS = r"""
() => {
  const selectors = [
    '[role="tab"]',
    'button[data-toggle="tab"]',
    'button[data-bs-toggle="tab"]',
    'a[data-toggle="tab"]',
    'a[data-bs-toggle="tab"]',
    'button[data-toggle="collapse"]',
    'button[data-bs-toggle="collapse"]',
    '.nav-tabs a', '.nav-tabs button',
    '.nav-pills a', '.nav-pills button',
    '.tab-link', '.tab-button',
    '.accordion-button', '.accordion-header button',
    'summary'
  ];
  const seen = new Set();
  const out = [];
  const tag = (el, label) => {
    if (seen.has(el)) return;
    seen.add(el);
    const t = '__fd_tab_' + out.length;
    el.setAttribute('data-fd-tab', t);
    out.push({ tag: t, label: (label || '').slice(0, 60) });
  };
  for (const sel of selectors) {
    try {
      document.querySelectorAll(sel).forEach((el) => {
        const txt = (el.innerText || el.textContent || '').trim();
        tag(el, txt);
      });
    } catch (e) {}
  }
  // ICICI / HDFC / Kotak / Axis often use plain <button> with no role or
  // data-toggle attribute — the slab selector is a custom React component.
  // Also catch any clickable element whose visible text matches FD slab
  // keywords (amount slabs, customer categories, "domestic"/"NRE"/"NRO",
  // "Senior Citizen", etc.).
  const KEYWORD_RE = new RegExp(
    [
      '\\b\\d+\\s*-\\s*<?\\s*\\d',  // "3 - < 5", "5 - < 5.10"
      '\\bless\\s+than\\b',
      '\\bmore\\s+than\\b',
      '\\bcr\\.?\\b',
      '\\bcrore\\b',
      '\\blakh\\b',
      '\\bsenior\\s+citizen\\b',
      '\\bsuper\\s+senior\\b',
      '\\bgeneral\\s+(public|citizen)\\b',
      '\\bdomestic\\b',
      '\\bnre\\b',
      '\\bnro\\b',
      '\\bfcnr\\b',
      '\\brfc\\b',
      '\\bretail\\b',
      '\\bbulk\\b',
      '\\bcallable\\b',
      '\\bnon-?callable\\b',
      '\\btax\\s+saver\\b',
      '\\bgreen\\s+deposit\\b',
    ].join('|'),
    'i'
  );
  const clickableSel = 'button, a, [role="button"], li[onclick], div[onclick]';
  document.querySelectorAll(clickableSel).forEach((el) => {
    if (seen.has(el)) return;
    const txt = (el.innerText || el.textContent || '').trim();
    if (txt.length === 0 || txt.length > 80) return;
    if (!KEYWORD_RE.test(txt)) return;
    // Filter out obvious nav links to other pages.
    if (el.tagName === 'A') {
      const href = el.getAttribute('href') || '';
      if (href && href !== '#' && !href.startsWith('javascript') && !href.startsWith('#')) return;
    }
    tag(el, txt);
  });
  return out;
}
"""


def _click_all_tabs_and_capture(page) -> int:
    """Discover tab/accordion controls, click each via Playwright, and append
    the post-click innerText into a hidden div on the page so it appears in
    page.content()."""
    candidates = page.evaluate(_FIND_TABS_JS) or []
    if not candidates:
        return 0
    # Cap the work — pages with sidebar nav or huge accordions can list 100+
    # buttons; we only need rate slabs/categories. 30 is plenty.
    candidates = candidates[:30]
    clicks = 0
    snapshots: list[str] = []
    # Capture the initial main text first.
    try:
        snapshots.append(
            page.evaluate(
                "() => (document.querySelector('main')||document.body).innerText || ''"
            )
        )
    except Exception:
        pass
    for c in candidates:
        sel = f"[data-fd-tab='{c['tag']}']"
        try:
            page.click(sel, timeout=1500, force=True, no_wait_after=True)
            clicks += 1
        except Exception:
            continue
        try:
            page.wait_for_timeout(350)
            snapshots.append(
                page.evaluate(
                    "() => (document.querySelector('main')||document.body).innerText || ''"
                )
            )
        except Exception:
            pass
    if snapshots:
        try:
            page.evaluate(
                """(text) => {
                    const div = document.createElement('div');
                    div.id = '__expanded_tabs__';
                    div.style.cssText = 'position:absolute;left:-9999px;top:-9999px;';
                    div.textContent = text;
                    document.body.appendChild(div);
                }""",
                "\n\n--- SECTION ---\n\n".join(snapshots),
            )
        except Exception:
            pass
    return clicks


def _should_block(req) -> bool:
    if req.resource_type in _BLOCKED_RESOURCE_TYPES:
        return True
    url = req.url.lower()
    for frag in _BLOCKED_HOST_FRAGMENTS:
        if frag in url:
            return True
    return False


def _ensure_browser():
    """Launch (or reuse) a Chromium browser+context for the current thread."""
    state = getattr(_thread_local, "state", None)
    if state is not None:
        return state

    from playwright.sync_api import sync_playwright  # type: ignore

    pw = sync_playwright().start()
    browser = pw.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ],
    )
    ctx = browser.new_context(
        user_agent=_USER_AGENT,
        viewport={"width": 1366, "height": 900},
        locale="en-IN",
        java_script_enabled=True,
    )
    ctx.route(
        "**/*",
        lambda route, request: (
            route.abort() if _should_block(request) else route.continue_()
        ),
    )
    state = {"pw": pw, "browser": browser, "ctx": ctx}
    _thread_local.state = state
    logger.info("Playwright: started browser for thread %s", threading.get_ident())
    return state


def render_page_html(url: str, timeout_ms: int = 20000) -> Optional[str]:
    """Render a page with headless Chromium and return its post-JS HTML.

    Returns None if Playwright isn't installed or rendering fails — callers
    should fall back to whatever they had before.
    """
    try:
        import playwright  # type: ignore  # noqa: F401
    except ImportError:
        logger.warning("Playwright not installed; cannot render %s dynamically", url)
        return None

    try:
        state = _ensure_browser()
        page = state["ctx"].new_page()
        try:
            page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            # Brief wait for networkidle; bank pages rarely truly idle.
            try:
                page.wait_for_load_state("networkidle", timeout=3000)
            except Exception:
                pass
            # One scroll to trigger lazy-loaded tables; no scroll-back.
            try:
                page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(600)
            except Exception:
                pass

            # Many bank rate pages (ICICI, HDFC, Kotak, Axis) hide rate slabs
            # behind tab/accordion controls. Without clicking each tab the
            # rendered HTML contains only the default tab's rates, which the
            # agent rejects as "incomplete FD rate tables". We use Playwright's
            # page.click() (which dispatches real trusted events that React /
            # Vue / jQuery widgets actually respond to) to click each candidate
            # control, capture innerText, and accumulate everything into a
            # hidden div so the final page.content() contains rates from ALL
            # slabs.
            try:
                clicks = _click_all_tabs_and_capture(page)
                logger.info("tab-expand: clicked %d controls on %s", clicks, url)
            except Exception as e:
                logger.debug("tab-expand pass skipped: %s", e)

            html = page.content()
            logger.info("Playwright rendered %s — %d chars HTML", url, len(html))
            return html
        finally:
            try:
                page.close()
            except Exception:
                pass
    except Exception as e:
        logger.warning("Playwright render failed for %s: %s", url, e)
        close_thread_browser()
        return None


def close_thread_browser() -> None:
    """Tear down the current thread's Playwright browser, if any."""
    state = getattr(_thread_local, "state", None)
    if state is None:
        return
    try:
        state["ctx"].close()
    except Exception:
        pass
    try:
        state["browser"].close()
    except Exception:
        pass
    try:
        state["pw"].stop()
    except Exception:
        pass
    _thread_local.state = None
    logger.info("Playwright: closed browser for thread %s", threading.get_ident())

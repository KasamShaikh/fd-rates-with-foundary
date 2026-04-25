"""Headless-browser fallback for JavaScript-rendered pages.

Some bank rate pages (ICICI, PNB, Kotak FD, etc.) load their rate tables via
client-side JavaScript widgets. A plain `requests.get` returns only the
shell HTML and never sees the actual numbers. This module uses Playwright
(Chromium) to render the page fully and return the post-JS DOM as HTML.

The import of `playwright` is deferred so the static-only path keeps working
even if Playwright/browsers aren't installed.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def render_page_html(url: str, timeout_ms: int = 25000) -> Optional[str]:
    """Render a page with headless Chromium and return its post-JS HTML.

    Returns None if Playwright isn't installed or rendering fails — callers
    should fall back to whatever they had before.
    """
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError:
        logger.warning("Playwright not installed; cannot render %s dynamically", url)
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(
                    user_agent=_USER_AGENT,
                    viewport={"width": 1366, "height": 900},
                    locale="en-IN",
                )
                page = ctx.new_page()
                page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                # Give async widgets a chance to populate
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass  # networkidle is best-effort
                # Scroll to trigger lazy-loaded tables
                try:
                    page.evaluate(
                        "() => window.scrollTo(0, document.body.scrollHeight)"
                    )
                    page.wait_for_timeout(1500)
                    page.evaluate("() => window.scrollTo(0, 0)")
                    page.wait_for_timeout(500)
                except Exception:
                    pass
                html = page.content()
                logger.info("Playwright rendered %s — %d chars HTML", url, len(html))
                return html
            finally:
                browser.close()
    except Exception as e:
        logger.warning("Playwright render failed for %s: %s", url, e)
        return None

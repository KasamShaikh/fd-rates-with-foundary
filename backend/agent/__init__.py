"""Agent package — FD-rate extraction logic.

Submodules:
    fd_rate_agent   — Foundry agent orchestration, parallel scrape, tool handlers.
    asset_extractors— PDF/image download + Document Intelligence extraction.
    dynamic_fetch   — Playwright-based JS-rendering fallback for static fetch.
    progress        — Thread-safe live activity buffer surfaced to the UI.
    robots          — robots.txt compliance helper (cached, thread-safe).
"""

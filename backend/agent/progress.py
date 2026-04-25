"""Thread-safe, business-friendly progress log for scrape runs.

The scrape pipeline posts short human-readable milestones here (e.g. "Starting
HDFC Bank", "Reading 2 PDF files"). The API exposes the current buffer via
`/api/scrape/progress` so the UI can poll and display a live activity feed.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

_lock = threading.Lock()
_events: list[dict] = []
_running: bool = False
_cancelled: bool = False
_run_id: int = 0


def reset() -> int:
    """Clear the buffer and start a new run. Returns the new run_id."""
    global _running, _cancelled, _run_id
    with _lock:
        _events.clear()
        _running = True
        _cancelled = False
        _run_id += 1
        return _run_id


def mark_done() -> None:
    global _running
    with _lock:
        _running = False


def cancel() -> bool:
    """Request cancellation of the in-flight run.

    Workers poll `is_cancelled()` between banks (and between agent-poll ticks)
    and bail out early. Returns True if a run was active and is now flagged
    for cancellation; False if there was nothing running to cancel.
    """
    global _cancelled
    with _lock:
        if not _running:
            return False
        _cancelled = True
        return True


def is_cancelled() -> bool:
    with _lock:
        return _cancelled


def log(message: str, level: str = "info", bank: Optional[str] = None) -> None:
    """Append a human-readable event. Safe to call from any thread."""
    ev = {
        "ts": time.strftime("%H:%M:%S"),
        "level": level,
        "message": message,
    }
    if bank:
        ev["bank"] = bank
    with _lock:
        _events.append(ev)
        # Keep the buffer bounded so long runs don't explode memory
        if len(_events) > 2000:
            del _events[: len(_events) - 2000]


def snapshot(since: int = 0) -> dict:
    """Return events from index `since` onward plus running flag + run_id."""
    with _lock:
        return {
            "run_id": _run_id,
            "running": _running,
            "cancelled": _cancelled,
            "total": len(_events),
            "events": list(_events[since:]),
        }

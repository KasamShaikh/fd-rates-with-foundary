"""Quick CLI summariser for the most recent local scrape result.

Reads `_local_results/latest.json` (saved by dev_server.py after every run)
and prints a short human-readable digest: timestamp, bank count, DI pages,
token usage, plus a one-line OK/FAIL row per bank.

Usage (from the backend/ directory):
    python _summary.py
"""

import json

# Load the latest run from the local file-based dev cache.
d = json.load(open("_local_results/latest.json", "r", encoding="utf-8"))

# --- Header ----------------------------------------------------------------
print(f"Scraped at : {d['scraped_at']}")
print(f"Banks      : {d['bank_count']}")
print(f"DI pages   : {d.get('di_pages', 0)}")
tu = d.get("token_usage", {})
print(
    f"Tokens     : prompt={tu.get('prompt_tokens')} completion={tu.get('completion_tokens')} total={tu.get('total_tokens')}"
)

# --- Per-bank rows ---------------------------------------------------------
print("--- Per-bank ---")
ok = fail = 0
for r in d["results"]:
    name = r.get("bank_name", "?")
    if r.get("error"):
        # Failed (or robots-blocked) row — show the truncated reason.
        fail += 1
        reason = (r.get("reason") or "")[:110]
        print(f"FAIL  {name:<42} {reason}")
    else:
        # Successful row — count categories and total rates extracted.
        ok += 1
        cats = r.get("categories") or []
        rates = sum(len(c.get("rates") or []) for c in cats)
        print(f"OK    {name:<42} categories={len(cats):<2} rates={rates}")

# --- Footer ----------------------------------------------------------------
print(f"\nSummary: {ok} OK / {fail} FAIL out of {len(d['results'])}")

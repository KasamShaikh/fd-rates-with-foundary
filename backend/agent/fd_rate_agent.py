"""
FD Rate Aggregator Agent — Uses Microsoft Foundry with Bing Grounding
to extract Fixed Deposit rates from Indian bank websites.
"""

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from azure.ai.agents import AgentsClient
from azure.identity import DefaultAzureCredential

from .asset_extractors import (
    extract_image,
    extract_pdf,
    get_di_page_count,
    reset_di_page_count,
)
from .http_cache import (
    check_unchanged as http_cache_check,
    get_cached_result,
    load_state,
    save_cached_result,
    save_state,
)
from .progress import log as progress_log, is_cancelled as progress_is_cancelled
from .robots import is_allowed as robots_is_allowed

logger = logging.getLogger(__name__)

SYSTEM_INSTRUCTIONS = """You are an expert financial data extractor specializing in Indian bank Fixed Deposit rates.

When given a bank URL, you MUST:
1. Call `fetch_webpage` on the provided URL to retrieve the page's visible text plus a `[DISCOVERED_ASSETS]` inventory of linked PDF documents and embedded images.
2. Extract ALL Fixed Deposit rate information from the page text.
3. If the visible HTML text does NOT contain complete FD rate tables (many Indian banks publish rates as PDF circulars or image charts), fall back to the asset inventory:
   - Call `fetch_pdf` on the most relevant PDF URL(s). Prefer links whose text or filename mentions "interest", "deposit", "rate", "FD", "term deposit", "retail", "domestic", "NRE", "NRO", or "senior".
   - Call `fetch_image` on rate-chart image URLs when rates are embedded as images.
   - You MAY call these tools multiple times (e.g., a separate PDF for General vs Senior Citizen vs NRE).
4. If the page has multiple tabs/slabs (e.g., "Less than 3 Cr", "3 Cr to 10 Cr", "Senior Citizen", etc.), extract rates from EACH tab/slab separately.
5. Return the data as a valid JSON object (no markdown formatting, no code blocks).

The JSON response MUST follow this exact schema:
{
  "bank_name": "<bank name>",
  "url": "<source URL>",
  "effective_date": "<effective date if found, else null>",
  "categories": [
    {
      "category_name": "<e.g., General Public, Senior Citizen, Staff, NRE, etc.>",
      "amount_slab": "<e.g., Less than 3 Cr, 3 Cr to 10 Cr, etc., or null>",
      "scheme_name": "<scheme name if applicable, else null>",
      "rates": [
        {
          "tenor_description": "<e.g., 7 days to 14 days>",
          "min_days": <minimum days as integer or null>,
          "max_days": <maximum days as integer or null>,
          "rate_percent": <rate as decimal number>,
          "additional_info": "<any extra notes or null>"
        }
      ]
    }
  ]
}

IMPORTANT RULES:
- Always return ONLY valid JSON. No markdown, no code blocks, no explanations.
- Return compact/minified JSON in a single line (no indentation or pretty-printing).
- Extract rates for ALL customer categories found on the page (General, Senior Citizen, Staff, Super Senior, NRE, NRO, etc.)
- Extract rates for ALL amount slabs if present.
- Convert tenor descriptions to min/max days where possible (e.g., "1 year" = 365 days).
- Rate must be a number (e.g., 6.50 not "6.50%").
- If multiple rate tables exist, include all of them under appropriate categories.
- If you cannot access the page or find rates, return: {"error": "Could not extract rates", "bank_name": "<name>", "url": "<url>", "reason": "<why>"}

DETERMINISM AND COMPLETENESS RULES (critical — read carefully):
- Be EXHAUSTIVE and STABLE: every distinct labelled section, table, or special
  scheme on the page MUST appear as its own entry in `categories`. Do NOT merge
  a special scheme into "General Public" just because it shares a column header.
- Treat any of the following as its OWN category entry (one per audience it
  applies to), with `scheme_name` set to the exact scheme label as printed:
    * Named special-tenor schemes (e.g., "SBI Amrit Vrishti", "Amrit Kalash",
      "SBI Green Rupee Term Deposit", "WeCare", "Tax Saving Term Deposit",
      "Utsav Deposit", "Shubh Aarambh", "Patrons", "Maha Yodha").
    * Any row or table with its own heading/sub-heading distinct from the main
      tenor table, even if it has only 1 rate.
- For each such scheme, set `category_name` to the actual audience the row
  applies to ("General Public", "Senior Citizen", "Super Senior Citizen", etc.)
  — never leave it generic if the page specifies an audience. If the scheme
  applies to multiple audiences, emit one category entry per audience.
- Do NOT drop, summarise, or skip any rate row, even if it appears to duplicate
  another row. Two runs of this prompt on the same page MUST produce the same
  set of categories and the same number of rates.
- Order `categories` deterministically: top-to-bottom in page order, and within
  each section General Public before Senior Citizen before Super Senior.
"""


def create_agent(agents_client: AgentsClient) -> object:
    """Create a Foundry Agent with web + PDF + image extraction tools.

    Determinism: temperature and top_p are pinned to 0 so repeat runs against
    the same page produce the same JSON (same categories, same rate count).
    Without this, gpt-4.1 sometimes drops or relabels special-scheme rows.
    """
    agent = agents_client.create_agent(
        model=os.environ.get("MODEL_DEPLOYMENT_NAME", "gpt-4.1"),
        name="fd-rate-scraper",
        instructions=SYSTEM_INSTRUCTIONS,
        temperature=0.0,
        top_p=1.0,
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "fetch_webpage",
                    "description": (
                        "Fetch visible text from a bank web page. The return value "
                        "also includes a [DISCOVERED_ASSETS] inventory listing any "
                        "PDF/image/iframe URLs found on the page, which you can then "
                        "pass to fetch_pdf or fetch_image when the page text does not "
                        "contain the full FD rate tables."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "url": {
                                "type": "string",
                                "description": "The URL to fetch (must be a bank website URL)",
                            }
                        },
                        "required": ["url"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "fetch_pdf",
                    "description": (
                        "Download a PDF document (e.g., a bank rate circular) and "
                        "extract its text and tables using Azure AI Document "
                        "Intelligence. Use this when the main page links to a PDF "
                        "that contains the FD rate schedule. The URL must share the "
                        "registered domain of the bank URL you were given."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "url": {
                                "type": "string",
                                "description": "Absolute URL of the PDF to download and extract.",
                            }
                        },
                        "required": ["url"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "fetch_image",
                    "description": (
                        "Download an image (PNG/JPG/TIFF) and OCR its text and "
                        "tables using Azure AI Document Intelligence. Use this when "
                        "the FD rate schedule is embedded as an image rather than "
                        "HTML text. The URL must share the registered domain of the "
                        "bank URL you were given."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "url": {
                                "type": "string",
                                "description": "Absolute URL of the image to download and OCR.",
                            }
                        },
                        "required": ["url"],
                    },
                },
            },
        ],
    )
    logger.info("Created agent: %s (id=%s)", agent.name, agent.id)
    return agent


def _discover_assets(soup: BeautifulSoup, base_url: str) -> dict:
    """Scan parsed HTML for linked PDFs, embedded images, and iframe/embed PDFs.

    Returns a dict with three lists of `(url, label)` tuples. URLs are resolved
    to absolute form using `base_url` as the reference. PDFs are sorted by
    relevance: any PDF embedded as the page's main document (`<object>`,
    `<embed>`, `<iframe>`) ranks first, followed by PDFs whose URL or anchor
    label mentions FD-rate keywords (rate / deposit / interest / fd / bulk /
    term / fcnr / nre / nro). Pages like Punjab & Sind Bank link to 100+
    unrelated PDFs (account-opening forms, brochures, posters) so without this
    ranking the actual rate PDF was getting truncated out of the top-20 cap
    that we send to the agent.
    """
    pdfs: list[tuple[str, str, int]] = []  # (url, label, score) — temp
    images: list[tuple[str, str]] = []
    iframes: list[tuple[str, str]] = []

    seen: set[str] = set()

    _RATE_KEYWORDS = (
        "rate",
        "deposit",
        "interest",
        " fd ",
        "/fd",
        "fixed",
        "bulk",
        "term",
        "fcnr",
        "nre",
        "nro",
        "rfc",
        "savings",
    )

    def _is_pdf_url(href: str) -> bool:
        path = href.lower().split("?", 1)[0].split("#", 1)[0]
        return path.endswith(".pdf")

    def _score_pdf(url: str, label: str) -> int:
        hay = f"{url} {label}".lower()
        score = 0
        for kw in _RATE_KEYWORDS:
            if kw.strip() and kw.strip() in hay:
                score += 10
        # Boost recent-looking filenames (banks tag PDFs with year/date).
        if any(y in hay for y in ("2026", "2025", "2024")):
            score += 1
        return score

    def _add_pdf(href: str, label: str, base_score: int = 0) -> None:
        absolute = urljoin(base_url, href.strip())
        if not absolute or absolute in seen:
            return
        seen.add(absolute)
        score = base_score + _score_pdf(absolute, label or "")
        pdfs.append((absolute, (label or "").strip()[:120], score))

    def _add_image(href: str, label: str) -> None:
        absolute = urljoin(base_url, href.strip())
        if not absolute or absolute in seen:
            return
        seen.add(absolute)
        images.append((absolute, (label or "").strip()[:120]))

    def _add_iframe(href: str, label: str) -> None:
        absolute = urljoin(base_url, href.strip())
        if not absolute or absolute in seen:
            return
        seen.add(absolute)
        iframes.append((absolute, (label or "").strip()[:120]))

    # <object>/<embed>/<iframe> — these are the page's MAIN document. Add
    # both to the iframes bucket (preserves backward compat) AND to the pdfs
    # bucket with a huge score boost so they sort to the top.
    for tag in soup.find_all(["iframe", "embed", "object"]):
        src = tag.get("src") or tag.get("data") or ""
        if not src:
            continue
        type_attr = (tag.get("type") or "").lower()
        is_pdf = _is_pdf_url(src) or "pdf" in type_attr
        if is_pdf:
            label = tag.name
            _add_iframe(src, label)
            # Re-add into pdfs with a 1000-point boost so it ranks #1.
            absolute = urljoin(base_url, src.strip())
            if absolute not in {p[0] for p in pdfs}:
                pdfs.append((absolute, label, 1000 + _score_pdf(absolute, label)))

    # <a href="*.pdf">
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if _is_pdf_url(href):
            _add_pdf(href, a.get_text(strip=True))

    # <img src="..."> (cap to 15 to keep prompt lean)
    for img in soup.find_all("img", src=True)[:15]:
        src = img["src"]
        lower = src.lower().split("?")[0]
        if any(
            lower.endswith(ext)
            for ext in (".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".webp")
        ):
            _add_image(src, img.get("alt", ""))

    # Sort PDFs by score desc, then by source order (stable sort).
    pdfs.sort(key=lambda t: -t[2])
    pdfs_out = [(u, lbl) for (u, lbl, _s) in pdfs]

    return {"pdfs": pdfs_out, "images": images, "iframes": iframes}


def _format_asset_inventory(assets: dict) -> str:
    """Render discovered assets as a compact block the agent can read."""
    if not any(assets.values()):
        return ""
    lines = ["\n\n[DISCOVERED_ASSETS]"]
    if assets["pdfs"]:
        lines.append("PDFs:")
        for url, label in assets["pdfs"][:20]:
            lines.append(f"- {url}  ::  {label}" if label else f"- {url}")
    if assets["iframes"]:
        lines.append("PDF iframes/embeds:")
        for url, label in assets["iframes"][:10]:
            lines.append(f"- {url}  ::  {label}" if label else f"- {url}")
    if assets["images"]:
        lines.append("Images:")
        for url, label in assets["images"][:15]:
            lines.append(f"- {url}  ::  {label}" if label else f"- {url}")
    return "\n".join(lines)


def _parse_html_for_rates(html: str, base_url: str) -> tuple[str, dict, int]:
    """Parse rendered/static HTML, return (clean_text, assets_dict, percent_count).

    `percent_count` is the number of '%' characters in the extracted visible
    text — used as a heuristic for whether the page actually contains rate
    data (FD rate pages have many %).
    """
    soup = BeautifulSoup(html, "html.parser")
    assets = _discover_assets(soup, base_url)

    for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    import re

    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text, assets, text.count("%")


def fetch_webpage_handler(url: str) -> str:
    """Fetch a webpage, strip HTML tags, and return clean visible text plus an
    inventory of discovered PDF/image/iframe assets for the agent to consider.

    If the static fetch yields a page with very few rate-like signals (few '%'
    characters), automatically retries via a headless Chromium browser to
    handle JavaScript-rendered rate widgets.
    """
    # Honour robots.txt before any network call.
    allowed, reason = robots_is_allowed(url)
    if not allowed:
        logger.warning("robots.txt disallows %s (%s)", url, reason)
        progress_log(
            f"⛔ Blocked by robots.txt: {url} — {reason}. Skipping this fetch.",
            level="warn",
        )
        return (
            f"BLOCKED_BY_ROBOTS_TXT: The site's robots.txt disallows fetching {url}. "
            f"No request was sent. Reason: {reason}. "
            f"Return a JSON error object indicating the site forbids automated access."
        )

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-IN,en;q=0.9",
        }
        response = requests.get(url, timeout=15, headers=headers)
        response.raise_for_status()

        text, assets, pct_count = _parse_html_for_rates(response.text, response.url)
        used_dynamic = False

        # Heuristic: real FD rate tables have dozens of '%' signs (one per
        # row × multiple columns). If the static page has only a handful, it's
        # almost certainly a JS-rendered widget showing only summary copy.
        MIN_PERCENT_SIGNS = 20
        if pct_count < MIN_PERCENT_SIGNS:
            progress_log(
                "Page looks dynamic (rate table not in HTML). Switching to a full browser to render JavaScript..."
            )
            from .dynamic_fetch import render_page_html

            rendered = render_page_html(url)
            if rendered:
                d_text, d_assets, d_pct = _parse_html_for_rates(rendered, response.url)
                # Adopt the dynamic result whenever it has more rate signals OR
                # more visible text. Tab-expanded pages add text incrementally
                # (clicking each slab tab adds one row block at a time) and
                # may not cross the old 1.5x bar even when they're strictly
                # better than the static page.
                if d_pct >= pct_count and (
                    d_pct > pct_count or len(d_text) > len(text)
                ):
                    text, assets, pct_count = d_text, d_assets, d_pct
                    used_dynamic = True

        asset_block = _format_asset_inventory(assets)
        # Reserve up to 2000 chars for the asset inventory block when present.
        text_budget = 15000 - len(asset_block) if asset_block else 15000
        if text_budget < 5000:
            text_budget = 5000
        combined = text[:text_budget] + asset_block

        logger.info(
            "Fetched %s — %d chars text, %d pdfs, %d images, %d iframes (dynamic=%s)",
            url,
            len(text),
            len(assets["pdfs"]),
            len(assets["images"]),
            len(assets["iframes"]),
            used_dynamic,
        )
        npdf = len(assets["pdfs"])
        nimg = len(assets["images"])
        niframe = len(assets["iframes"])
        extras = []
        if npdf:
            extras.append(f"{npdf} PDF document{'s' if npdf != 1 else ''}")
        if niframe:
            extras.append(f"{niframe} embedded document{'s' if niframe != 1 else ''}")
        if nimg:
            extras.append(f"{nimg} image{'s' if nimg != 1 else ''}")
        extras_text = (
            f" Found {', '.join(extras)} linked from this page." if extras else ""
        )
        rendered_note = " (rendered with browser)" if used_dynamic else ""
        progress_log(
            f"Read the web page successfully{rendered_note} ({len(text):,} characters of text).{extras_text}"
        )
        return combined
    except Exception as e:
        logger.error("fetch_webpage_handler error for %s: %s", url, e)
        progress_log(f"Could not open the page: {e}", level="warn")
        return f"Error fetching {url}: {str(e)}"


def scrape_bank_url(
    agents_client: AgentsClient,
    agent_id: str,
    url: str,
    bank_name: str,
) -> dict:
    """Send a scrape request to the agent and return parsed JSON.

    Manually handles tool calls since enable_auto_function_calls doesn't work
    with the current SDK version.
    """
    thread = agents_client.threads.create()

    user_message = (
        f"Extract all Fixed Deposit interest rates from this bank's website.\n"
        f"Bank Name: {bank_name}\n"
        f"URL: {url}\n\n"
        f"Search for the FD rates page at this URL and extract ALL rate categories, "
        f"amount slabs, and tenor-wise rates. Return ONLY valid JSON."
    )

    agents_client.messages.create(
        thread_id=thread.id,
        role="user",
        content=user_message,
    )

    # Create run (don't use create_and_process - manually handle tool calls).
    # Pin temperature/top_p at the run level too — even when the agent has
    # them set, the run-level values win, so we pass them explicitly.
    run = agents_client.runs.create(
        thread_id=thread.id,
        agent_id=agent_id,
        temperature=0.0,
        top_p=1.0,
    )

    # Poll run status and handle tool calls manually. Bail out early if the
    # user hit the Stop button so we don't keep racking up tokens.
    while run.status in ["queued", "in_progress"]:
        if progress_is_cancelled():
            try:
                agents_client.runs.cancel(thread_id=thread.id, run_id=run.id)
            except Exception:
                pass
            return {
                "error": "Cancelled",
                "bank_name": bank_name,
                "url": url,
                "reason": "Run cancelled by user before completion",
                "cancelled": True,
                "_token_usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
            }
        time.sleep(1)
        run = agents_client.runs.get(
            thread_id=thread.id,
            run_id=run.id,
        )

    # Handle tool calls in a loop — agent may call tools multiple times
    max_rounds = 8
    round_count = 0
    while run.status == "requires_action" and round_count < max_rounds:
        round_count += 1
        tool_calls = run.required_action.submit_tool_outputs.tool_calls
        tool_outputs = []

        for tool_call in tool_calls:
            name = tool_call.function.name
            try:
                args = json.loads(tool_call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            url_to_fetch = (args.get("url") or "").strip()
            logger.info("[Round %d] Tool=%s URL=%s", round_count, name, url_to_fetch)

            if name == "fetch_webpage":
                progress_log(f"Opening web page: {url_to_fetch}", bank=bank_name)
                output = fetch_webpage_handler(url_to_fetch)
            elif name == "fetch_pdf":
                progress_log(
                    f"Rates likely inside a PDF — downloading and reading it now...",
                    bank=bank_name,
                )
                output = extract_pdf(url_to_fetch, source_url=url)
                if output and not output.startswith("Error"):
                    progress_log(
                        "PDF processed — extracted text and tables.", bank=bank_name
                    )
                else:
                    progress_log(
                        f"Could not read PDF: {output[:150] if output else 'unknown error'}",
                        level="warn",
                        bank=bank_name,
                    )
            elif name == "fetch_image":
                progress_log(
                    "Rates likely shown as an image — using OCR to read it...",
                    bank=bank_name,
                )
                output = extract_image(url_to_fetch, source_url=url)
                if output and not output.startswith("Error"):
                    progress_log(
                        "Image processed — text extracted via OCR.", bank=bank_name
                    )
                else:
                    progress_log(
                        f"Could not read image: {output[:150] if output else 'unknown error'}",
                        level="warn",
                        bank=bank_name,
                    )
            else:
                output = f"Error: unknown tool '{name}'"

            tool_outputs.append(
                {
                    "tool_call_id": tool_call.id,
                    "output": output,
                }
            )

        # Submit tool outputs
        run = agents_client.runs.submit_tool_outputs(
            thread_id=thread.id,
            run_id=run.id,
            tool_outputs=tool_outputs,
        )

        # Poll until next action or completion
        while run.status in ["queued", "in_progress"]:
            if progress_is_cancelled():
                try:
                    agents_client.runs.cancel(thread_id=thread.id, run_id=run.id)
                except Exception:
                    pass
                return {
                    "error": "Cancelled",
                    "bank_name": bank_name,
                    "url": url,
                    "reason": "Run cancelled by user mid-extraction",
                    "cancelled": True,
                    "_token_usage": {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                    },
                }
            time.sleep(1)
            run = agents_client.runs.get(
                thread_id=thread.id,
                run_id=run.id,
            )

    # Capture token usage from the completed/failed run
    usage = getattr(run, "usage", None)
    token_usage = (
        {
            "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
            "total_tokens": getattr(usage, "total_tokens", 0) or 0,
        }
        if usage
        else {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    )

    if run.status == "failed":
        logger.error("Agent run failed for %s: %s", bank_name, run.last_error)
        return {
            "error": "Agent run failed",
            "bank_name": bank_name,
            "url": url,
            "reason": str(run.last_error),
            "_token_usage": token_usage,
        }

    messages = agents_client.messages.list(thread_id=thread.id)

    def _latest_assistant_text() -> str | None:
        for msg in messages:
            if msg.role == "assistant" and msg.text_messages:
                return "\n".join(
                    [
                        tm.text.value
                        for tm in msg.text_messages
                        if tm.text and tm.text.value
                    ]
                )
        return None

    raw_text = _latest_assistant_text()
    if raw_text:
        parsed = _parse_agent_response(raw_text, bank_name, url)
        parsed["_token_usage"] = token_usage
        if parsed.get("error") != "Failed to parse response":
            return parsed

        # Retry once: ask the model to reformat strictly as valid compact JSON.
        agents_client.messages.create(
            thread_id=thread.id,
            role="user",
            content=(
                "Your previous response was not valid JSON. "
                "Return the same data as STRICT valid compact JSON in one line only. "
                "Do not include markdown, comments, or trailing text."
            ),
        )

        rerun = agents_client.runs.create(thread_id=thread.id, agent_id=agent_id)
        while rerun.status in ["queued", "in_progress"]:
            time.sleep(1)
            rerun = agents_client.runs.get(thread_id=thread.id, run_id=rerun.id)

        if rerun.status == "completed":
            retry_usage = getattr(rerun, "usage", None)
            if retry_usage:
                token_usage["prompt_tokens"] += (
                    getattr(retry_usage, "prompt_tokens", 0) or 0
                )
                token_usage["completion_tokens"] += (
                    getattr(retry_usage, "completion_tokens", 0) or 0
                )
                token_usage["total_tokens"] += (
                    getattr(retry_usage, "total_tokens", 0) or 0
                )
            messages_retry = agents_client.messages.list(thread_id=thread.id)
            for msg in messages_retry:
                if msg.role == "assistant" and msg.text_messages:
                    retry_text = "\n".join(
                        [
                            tm.text.value
                            for tm in msg.text_messages
                            if tm.text and tm.text.value
                        ]
                    )
                    parsed_retry = _parse_agent_response(retry_text, bank_name, url)
                    parsed_retry["_token_usage"] = token_usage
                    if parsed_retry.get("error") != "Failed to parse response":
                        return parsed_retry

        return parsed

    return {
        "error": "No response from agent",
        "bank_name": bank_name,
        "url": url,
        "_token_usage": token_usage,
    }


def _parse_agent_response(raw_text: str, bank_name: str, url: str) -> dict:
    """Parse agent response text into JSON, handling markdown code blocks."""
    text = raw_text.strip()

    # Strip markdown code block if present
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first line (```json or ```) and last line (```)
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning("Failed to parse agent response for %s: %s", bank_name, e)
        # Fallback: try extracting the outermost JSON object from surrounding text.
        try:
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1 and end > start:
                return json.loads(text[start : end + 1])
        except Exception:
            pass
        return {
            "error": "Failed to parse response",
            "bank_name": bank_name,
            "url": url,
            "raw_response": raw_text[:4000],
        }


def scrape_all_urls(urls: list[dict]) -> list[dict]:
    """Scrape FD rates from all provided bank URLs using Foundry Agent."""
    credential = DefaultAzureCredential()
    project_endpoint = os.environ.get("PROJECT_ENDPOINT", "")
    if not project_endpoint:
        raise ValueError("PROJECT_ENDPOINT environment variable is not set")

    # Use standalone AgentsClient for agent operations (SDK v2.0.1+)
    agents_client = AgentsClient(
        endpoint=project_endpoint,
        credential=credential,
    )

    agent = create_agent(agents_client)
    results: list[dict | None] = [None] * len(urls)
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    usage_lock = threading.Lock()
    reset_di_page_count()

    # L1 change-detection: load the prior per-URL fingerprint state once at
    # the start of the run. Each worker mutates its slot via state_lock so we
    # can persist updates atomically when the run finishes.
    cache_state = load_state()
    state_lock = threading.Lock()

    total_banks = len(urls)
    # Tunable parallelism. Default 4 — a good tradeoff for Foundry/DI quotas.
    max_workers = int(os.environ.get("SCRAPE_MAX_WORKERS", "4") or "4")
    max_workers = max(1, min(max_workers, total_banks)) if total_banks > 0 else 1
    progress_log(
        f"Starting fetch for {total_banks} bank{'s' if total_banks != 1 else ''} "
        f"({max_workers} in parallel)."
    )

    def _scrape_one(idx: int, entry: dict) -> tuple[int, dict]:
        bank_name = entry["bank_name"]
        url_id = str(entry.get("id") or "")
        # Cancellation short-circuit — if the user hit Stop while this task was
        # still queued in the executor, bail out before doing any work.
        if progress_is_cancelled():
            progress_log(
                f"⏹️ {bank_name}: skipped — run cancelled by user.",
                level="warn",
                bank=bank_name,
            )
            return idx, {
                "bank_name": bank_name,
                "url": entry["url"],
                "error": "Cancelled",
                "reason": "Run cancelled by user before this bank started",
                "cancelled": True,
            }
        logger.info("Fetching: %s (%s)", bank_name, entry["url"])
        progress_log(
            f"[{idx + 1}/{total_banks}] Starting {bank_name}...", bank=bank_name
        )

        # Pre-flight: honour robots.txt before spending agent tokens.
        allowed, reason = robots_is_allowed(entry["url"])
        if not allowed:
            logger.warning(
                "Skipping %s — robots.txt disallows %s (%s)",
                bank_name,
                entry["url"],
                reason,
            )
            progress_log(
                f"⛔ {bank_name}: Skipped — the bank's robots.txt forbids automated access "
                f"to this URL. ({reason}). To override, set ROBOTS_RESPECT=false (not recommended).",
                level="warn",
                bank=bank_name,
            )
            return idx, {
                "bank_name": bank_name,
                "url": entry["url"],
                "error": "Blocked by robots.txt",
                "reason": (
                    f"The bank's robots.txt disallows automated access to {entry['url']} "
                    f"for user-agent matching '{os.environ.get('ROBOTS_USER_AGENT', 'FDRateAggregator')}'. "
                    f"No fetch was attempted. ({reason})"
                ),
                "blocked_by_robots": True,
                "di_pages": None,
            }

        # L1 change-detection: cheap conditional GET. If the page is byte- or
        # header-identical to the last successful fetch and we have a cached
        # result on disk, short-circuit without spending agent tokens.
        unchanged, fingerprint = http_cache_check(url_id, entry["url"], cache_state)
        if unchanged:
            cached = get_cached_result(url_id) if url_id else None
            if cached and not cached.get("error"):
                with state_lock:
                    cache_state[url_id] = fingerprint
                cats = cached.get("categories") or []
                rate_count = sum(len(c.get("rates") or []) for c in cats)
                progress_log(
                    f"↻ {bank_name}: unchanged since {fingerprint.get('last_changed_at') or 'previous run'} — "
                    f"reused cached result ({rate_count} rate{'s' if rate_count != 1 else ''}). 0 tokens used.",
                    level="success",
                    bank=bank_name,
                )
                # Return a copy so we don't mutate the on-disk snapshot.
                reused = dict(cached)
                reused["unchanged"] = True
                reused["last_changed_at"] = fingerprint.get("last_changed_at")
                reused["di_pages"] = 0
                return idx, reused
            # No usable cache — fall through to full scrape.
            logger.info(
                "%s: 304/hash-match but no cached result — full scrape", bank_name
            )

        result = scrape_bank_url(
            agents_client=agents_client,
            agent_id=agent.id,
            url=entry["url"],
            bank_name=bank_name,
        )
        bank_usage = result.pop("_token_usage", {})
        with usage_lock:
            for key in total_usage:
                total_usage[key] += bank_usage.get(key, 0)
        # Note: per-bank di_pages is not reliable under parallel execution
        # (the global counter advances across threads). Total is still correct.
        result["di_pages"] = None
        if result.get("cancelled"):
            progress_log(
                f"⏹️ {bank_name}: cancelled by user mid-extraction.",
                level="warn",
                bank=bank_name,
            )
        elif result.get("error"):
            progress_log(
                f"{bank_name}: could not extract rates — {result.get('reason') or result.get('error')}",
                level="warn",
                bank=bank_name,
            )
        else:
            cats = result.get("categories") or []
            rate_count = sum(len(c.get("rates") or []) for c in cats)
            progress_log(
                f"{bank_name}: extracted {rate_count} rate{'s' if rate_count != 1 else ''} across {len(cats)} categor{'ies' if len(cats) != 1 else 'y'}.",
                level="success",
                bank=bank_name,
            )
            # Persist the successful result + fingerprint so the next run can
            # short-circuit if the bank hasn't republished its rate page.
            if url_id:
                save_cached_result(url_id, result)
                with state_lock:
                    cache_state[url_id] = fingerprint
        return idx, result

    try:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = [
                ex.submit(_scrape_one, idx, entry) for idx, entry in enumerate(urls)
            ]
            for fut in as_completed(futures):
                try:
                    idx, result = fut.result()
                    results[idx] = result
                except Exception as e:
                    logger.exception("Worker failed: %s", e)
                    progress_log(f"Worker failed: {e}", level="warn")
            # Close per-thread Playwright browsers from inside each worker
            # thread before the executor shuts down.
            try:
                from .dynamic_fetch import close_thread_browser

                close_futures = [
                    ex.submit(close_thread_browser) for _ in range(max_workers)
                ]
                for cf in close_futures:
                    try:
                        cf.result(timeout=10)
                    except Exception:
                        pass
            except Exception:
                pass
    finally:
        # Clean up agent
        try:
            agents_client.delete_agent(agent.id)
            logger.info("Deleted agent: %s", agent.id)
        except Exception as e:
            logger.warning("Failed to delete agent: %s", e)

    # Replace any remaining None slots (worker exception) with error stubs
    for i, r in enumerate(results):
        if r is None:
            results[i] = {
                "bank_name": urls[i].get("bank_name"),
                "url": urls[i].get("url"),
                "error": "Worker failed",
            }

    total_di_pages = get_di_page_count()
    logger.info("Total tokens used: %s  DI pages: %d", total_usage, total_di_pages)

    # Persist any fingerprint updates collected by the workers so the next
    # run can short-circuit unchanged banks.
    try:
        save_state(cache_state)
    except Exception as e:
        logger.warning("Failed to save url_state.json: %s", e)

    success_count = sum(1 for r in results if not r.get("error"))
    unchanged_count = sum(1 for r in results if r and r.get("unchanged"))
    cancelled_count = sum(1 for r in results if r and r.get("cancelled"))
    was_cancelled = progress_is_cancelled() or cancelled_count > 0
    if was_cancelled:
        progress_log(
            f"⏹️ Run cancelled by user. Completed {success_count} of {total_banks} banks "
            f"before stop ({cancelled_count} skipped). "
            f"Total tokens used: {total_usage.get('total_tokens', 0):,}.",
            level="warn",
        )
    elif unchanged_count:
        progress_log(
            f"All done. {success_count} of {total_banks} banks succeeded "
            f"({unchanged_count} unchanged — reused from cache, 0 tokens). "
            f"Total tokens used: {total_usage.get('total_tokens', 0):,}.",
            level="success",
        )
    else:
        progress_log(
            f"All done. Successfully extracted rates from {success_count} of {total_banks} banks. "
            f"Total tokens used: {total_usage.get('total_tokens', 0):,}.",
            level="success",
        )
    return {
        "results": results,
        "token_usage": total_usage,
        "di_pages": total_di_pages,
        "unchanged_count": unchanged_count,
        "cancelled": was_cancelled,
        "cancelled_count": cancelled_count,
    }

"""
FD Rate Scraper Agent — Uses Microsoft Foundry with Bing Grounding
to extract Fixed Deposit rates from Indian bank websites.
"""

import json
import logging
import os
import time
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
from .progress import log as progress_log

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
"""


def create_agent(agents_client: AgentsClient) -> object:
    """Create a Foundry Agent with web + PDF + image extraction tools."""
    agent = agents_client.create_agent(
        model=os.environ.get("MODEL_DEPLOYMENT_NAME", "gpt-4.1"),
        name="fd-rate-scraper",
        instructions=SYSTEM_INSTRUCTIONS,
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
    to absolute form using `base_url` as the reference.
    """
    pdfs: list[tuple[str, str]] = []
    images: list[tuple[str, str]] = []
    iframes: list[tuple[str, str]] = []

    seen: set[str] = set()

    def _add(bucket: list, href: str, label: str) -> None:
        absolute = urljoin(base_url, href.strip())
        if not absolute or absolute in seen:
            return
        seen.add(absolute)
        bucket.append((absolute, (label or "").strip()[:120]))

    # <a href="*.pdf"> or href containing 'pdf' keyword
    for a in soup.find_all("a", href=True):
        href = a["href"]
        lower = href.lower().split("?")[0]
        if lower.endswith(".pdf") or "pdf" in lower:
            _add(pdfs, href, a.get_text(strip=True))

    # <img src="..."> (cap to 15 to keep prompt lean)
    for img in soup.find_all("img", src=True)[:15]:
        src = img["src"]
        lower = src.lower().split("?")[0]
        if any(
            lower.endswith(ext)
            for ext in (".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".webp")
        ):
            _add(images, src, img.get("alt", ""))

    # <iframe src="*.pdf"> / <embed src="*.pdf">
    for tag in soup.find_all(["iframe", "embed", "object"]):
        src = tag.get("src") or tag.get("data") or ""
        if src and ".pdf" in src.lower():
            _add(iframes, src, tag.name)

    return {"pdfs": pdfs, "images": images, "iframes": iframes}


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
                # Only adopt the dynamic result if it's actually richer
                if d_pct > pct_count or len(d_text) > len(text) * 1.5:
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

    # Create run (don't use create_and_process - manually handle tool calls)
    run = agents_client.runs.create(
        thread_id=thread.id,
        agent_id=agent_id,
    )

    # Poll run status and handle tool calls manually
    while run.status in ["queued", "in_progress"]:
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
    results = []
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    reset_di_page_count()
    di_pages_before = 0

    total_banks = len(urls)
    progress_log(
        f"Starting scrape for {total_banks} bank{'s' if total_banks != 1 else ''}."
    )
    try:
        for idx, entry in enumerate(urls, start=1):
            bank_name = entry["bank_name"]
            logger.info("Scraping: %s (%s)", bank_name, entry["url"])
            progress_log(
                f"[{idx}/{total_banks}] Starting {bank_name}...", bank=bank_name
            )
            di_pages_before = get_di_page_count()
            result = scrape_bank_url(
                agents_client=agents_client,
                agent_id=agent.id,
                url=entry["url"],
                bank_name=entry["bank_name"],
            )
            # Accumulate token usage and remove from per-bank result
            bank_usage = result.pop("_token_usage", {})
            for key in total_usage:
                total_usage[key] += bank_usage.get(key, 0)
            # Per-bank DI page count (for cost visibility)
            result["di_pages"] = get_di_page_count() - di_pages_before
            results.append(result)
            if result.get("error"):
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
    finally:
        # Clean up agent
        try:
            agents_client.delete_agent(agent.id)
            logger.info("Deleted agent: %s", agent.id)
        except Exception as e:
            logger.warning("Failed to delete agent: %s", e)

    total_di_pages = get_di_page_count()
    logger.info("Total tokens used: %s  DI pages: %d", total_usage, total_di_pages)
    success_count = sum(1 for r in results if not r.get("error"))
    progress_log(
        f"All done. Successfully extracted rates from {success_count} of {total_banks} banks. "
        f"Total tokens used: {total_usage.get('total_tokens', 0):,}.",
        level="success",
    )
    return {
        "results": results,
        "token_usage": total_usage,
        "di_pages": total_di_pages,
    }

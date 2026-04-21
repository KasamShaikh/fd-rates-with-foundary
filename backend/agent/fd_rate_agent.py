"""
FD Rate Scraper Agent — Uses Microsoft Foundry with Bing Grounding
to extract Fixed Deposit rates from Indian bank websites.
"""

import json
import logging
import os
import time
import requests
from bs4 import BeautifulSoup
from azure.ai.agents import AgentsClient
from azure.identity import DefaultAzureCredential

logger = logging.getLogger(__name__)

SYSTEM_INSTRUCTIONS = """You are an expert financial data extractor specializing in Indian bank Fixed Deposit rates.

When given a bank URL, you MUST:
1. Use Bing Search to find and navigate to the bank's FD interest rate page at the provided URL.
2. Extract ALL Fixed Deposit rate information from the page.
3. If the page has multiple tabs/slabs (e.g., "Less than 3 Cr", "3 Cr to 10 Cr", "Senior Citizen", etc.), extract rates from EACH tab/slab separately.
4. Return the data as a valid JSON object (no markdown formatting, no code blocks).

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
    """Create a Foundry Agent with web scraper tool (no Bing/connection required)."""
    agent = agents_client.create_agent(
        model=os.environ.get("MODEL_DEPLOYMENT_NAME", "gpt-4.1"),
        name="fd-rate-scraper",
        instructions=SYSTEM_INSTRUCTIONS,
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "fetch_webpage",
                    "description": "Fetch HTML content from a URL and return the page text",
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
            }
        ],
    )
    logger.info("Created agent: %s (id=%s)", agent.name, agent.id)
    return agent


def fetch_webpage_handler(url: str) -> str:
    """Fetch a webpage, strip HTML tags, and return clean visible text."""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-IN,en;q=0.9",
        }
        response = requests.get(url, timeout=15, headers=headers)
        response.raise_for_status()

        # Use BeautifulSoup to extract visible text only (strips JS/CSS/nav)
        soup = BeautifulSoup(response.text, "html.parser")
        # Remove script and style elements
        for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)
        # Collapse excessive whitespace
        import re

        text = re.sub(r"[ \t]{2,}", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)

        logger.info("Fetched %s — extracted %d chars of text", url, len(text))
        # Return up to 15000 chars for the agent to analyse
        return text[:15000]
    except Exception as e:
        logger.error("fetch_webpage_handler error for %s: %s", url, e)
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

    # Handle tool calls in a loop — agent may call the tool multiple times
    max_rounds = 5
    round_count = 0
    while run.status == "requires_action" and round_count < max_rounds:
        round_count += 1
        tool_calls = run.required_action.submit_tool_outputs.tool_calls
        tool_outputs = []

        for tool_call in tool_calls:
            if tool_call.function.name == "fetch_webpage":
                args = json.loads(tool_call.function.arguments)
                url_to_fetch = args.get("url", "")
                logger.info("[Round %d] Fetching URL: %s", round_count, url_to_fetch)
                output = fetch_webpage_handler(url_to_fetch)
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

    try:
        for entry in urls:
            logger.info("Scraping: %s (%s)", entry["bank_name"], entry["url"])
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
            results.append(result)
    finally:
        # Clean up agent
        try:
            agents_client.delete_agent(agent.id)
            logger.info("Deleted agent: %s", agent.id)
        except Exception as e:
            logger.warning("Failed to delete agent: %s", e)

    logger.info("Total tokens used: %s", total_usage)
    return {"results": results, "token_usage": total_usage}

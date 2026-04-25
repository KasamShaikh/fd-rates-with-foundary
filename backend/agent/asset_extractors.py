"""
PDF and image asset extractors for FD rate pages.

Uses Azure AI Document Intelligence (prebuilt-layout) to extract both plain
text and structured tables from PDFs and images linked from bank websites.
Exposed as two agent function tools: `fetch_pdf` and `fetch_image`.
"""

from __future__ import annotations

import logging
import os
from urllib.parse import urlparse

import requests
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import AnalyzeDocumentRequest
from azure.identity import DefaultAzureCredential

logger = logging.getLogger(__name__)

_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/pdf,image/*,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
}

# Size caps to protect against huge downloads / DI cost blow-ups.
_MAX_PDF_BYTES = 15 * 1024 * 1024  # 15 MB
_MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB

# Per-process counter for Document Intelligence pages processed.
# Reset by the caller (agent) between scrape runs if desired.
_di_page_count = 0


def get_di_page_count() -> int:
    """Return the number of DI pages processed since the last reset."""
    return _di_page_count


def reset_di_page_count() -> None:
    global _di_page_count
    _di_page_count = 0


def _get_di_client() -> DocumentIntelligenceClient | None:
    endpoint = os.environ.get("DOC_INTELLIGENCE_ENDPOINT", "").strip()
    if not endpoint:
        logger.warning("DOC_INTELLIGENCE_ENDPOINT is not set")
        return None
    return DocumentIntelligenceClient(
        endpoint=endpoint, credential=DefaultAzureCredential()
    )


def _same_origin(asset_url: str, source_url: str) -> bool:
    """Allow the asset only if it shares the registered domain with the bank URL.

    Matches either an exact host match or a suffix match on the parent domain
    (e.g. `rbidocs.rbi.org.in` vs `rbi.org.in`). This is a lightweight SSRF
    guard so the agent cannot be tricked into fetching arbitrary external URLs.
    """
    try:
        a = urlparse(asset_url).hostname or ""
        s = urlparse(source_url).hostname or ""
        if not a or not s:
            return False
        if a == s:
            return True
        # Compare the last 2 labels (e.g., hdfc.bank.in -> bank.in)
        a_root = ".".join(a.lower().split(".")[-2:])
        s_root = ".".join(s.lower().split(".")[-2:])
        return a_root == s_root
    except Exception:
        return False


def _download(url: str, max_bytes: int, expect_prefix: str) -> tuple[bytes, str]:
    """Download `url` and return (bytes, content_type). Raises on any failure."""
    with requests.get(url, headers=_BROWSER_HEADERS, timeout=30, stream=True) as resp:
        resp.raise_for_status()
        content_type = (
            (resp.headers.get("Content-Type") or "").lower().split(";")[0].strip()
        )

        # Accept octet-stream if URL ends with a sensible extension.
        lower_url = url.lower().split("?")[0]
        if not content_type.startswith(expect_prefix):
            if content_type == "application/octet-stream" and (
                (expect_prefix == "application/pdf" and lower_url.endswith(".pdf"))
                or (
                    expect_prefix == "image/"
                    and any(
                        lower_url.endswith(ext)
                        for ext in (
                            ".png",
                            ".jpg",
                            ".jpeg",
                            ".bmp",
                            ".tiff",
                            ".tif",
                            ".webp",
                        )
                    )
                )
            ):
                pass
            else:
                raise ValueError(
                    f"Unexpected Content-Type '{content_type}' (expected {expect_prefix}*)"
                )

        chunks: list[bytes] = []
        total = 0
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"Asset exceeds size cap ({max_bytes} bytes) at {url}")
            chunks.append(chunk)
        return b"".join(chunks), content_type


def _tables_to_markdown(tables) -> str:
    """Convert DI table objects to pipe-delimited markdown rows."""
    lines: list[str] = []
    for idx, table in enumerate(tables or []):
        lines.append(
            f"\n[TABLE {idx + 1}: {table.row_count} rows x {table.column_count} cols]"
        )
        # Build grid
        grid: list[list[str]] = [
            ["" for _ in range(table.column_count)] for _ in range(table.row_count)
        ]
        for cell in table.cells or []:
            r = getattr(cell, "row_index", 0) or 0
            c = getattr(cell, "column_index", 0) or 0
            content = (
                (getattr(cell, "content", "") or "")
                .replace("\n", " ")
                .replace("|", "/")
            )
            if 0 <= r < table.row_count and 0 <= c < table.column_count:
                grid[r][c] = content.strip()
        for row in grid:
            lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _analyze_bytes(data: bytes) -> dict:
    """Run DI prebuilt-layout on raw bytes, return {text, tables_markdown, pages}."""
    global _di_page_count
    client = _get_di_client()
    if client is None:
        raise RuntimeError("Document Intelligence client is not configured")

    poller = client.begin_analyze_document(
        "prebuilt-layout",
        AnalyzeDocumentRequest(bytes_source=data),
    )
    result = poller.result()

    text = getattr(result, "content", "") or ""
    tables_md = _tables_to_markdown(getattr(result, "tables", None))
    page_count = len(getattr(result, "pages", []) or [])
    _di_page_count += page_count

    return {"text": text, "tables_markdown": tables_md, "pages": page_count}


def _extract(url: str, source_url: str, kind: str, max_chars: int) -> str:
    """Shared extraction path for PDF and image tools."""
    if not url or not isinstance(url, str):
        return f"Error extracting asset: invalid URL"

    # SSRF guard: asset must share the bank URL's registered domain.
    if source_url and not _same_origin(url, source_url):
        logger.warning(
            "Rejected cross-origin asset fetch: asset=%s source=%s", url, source_url
        )
        return (
            f"Error extracting {url}: refusing cross-origin fetch "
            f"(must share domain with bank URL {source_url})"
        )

    try:
        if kind == "pdf":
            data, _ct = _download(url, _MAX_PDF_BYTES, "application/pdf")
        else:
            data, _ct = _download(url, _MAX_IMAGE_BYTES, "image/")
    except Exception as e:
        logger.error("Download failed for %s: %s", url, e)
        return f"Error extracting {url}: download failed — {e}"

    try:
        analyzed = _analyze_bytes(data)
    except Exception as e:
        logger.error("Document Intelligence failed for %s: %s", url, e)
        return f"Error extracting {url}: Document Intelligence failed — {e}"

    combined = (
        f"[SOURCE: {url}]\n"
        f"[PAGES: {analyzed['pages']}]\n"
        f"{analyzed['text']}\n"
        f"{analyzed['tables_markdown']}"
    )
    logger.info(
        "Extracted %s (%d bytes, %d pages) — %d chars",
        url,
        len(data),
        analyzed["pages"],
        len(combined),
    )
    return combined[:max_chars]


def extract_pdf(url: str, source_url: str = "", max_chars: int = 20000) -> str:
    """Download a PDF and extract text + tables via Document Intelligence."""
    return _extract(url, source_url, "pdf", max_chars)


def extract_image(url: str, source_url: str = "", max_chars: int = 15000) -> str:
    """Download an image and OCR text + tables via Document Intelligence."""
    return _extract(url, source_url, "image", max_chars)

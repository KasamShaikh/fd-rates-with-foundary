# FD Rate Aggregator

An AI-powered Fixed Deposit rate aggregator for Indian banks. Uses **Azure AI Foundry** (`gpt-4.1`) as an intelligent agent with a custom `fetch_webpage` tool to scrape, parse, and structure FD rate data from bank websites. JS-rendered pages fall back to a **Playwright** headless browser, and PDFs/images are extracted with **Azure AI Document Intelligence** (`prebuilt-layout`). Results are stored in **Azure Blob Storage** and exposed through a **Flask REST API** consumed by a **React** browser UI with a live progress feed.

## What's New

- **robots.txt compliance** — every outbound HTML/PDF/image fetch now consults the origin's `/robots.txt` via `urllib.robotparser` (cached per host) and skips disallowed URLs with a warning. Toggle via `ROBOTS_RESPECT=false`; user-agent matched against rules is `ROBOTS_USER_AGENT` (default `FDRateAggregator`). Default-allow on network/parse errors per RFC 9309.
- **Parallel fetching (~2.6× faster)** — bank URLs are now processed concurrently with a `ThreadPoolExecutor` (default 4 workers, override via `SCRAPE_MAX_WORKERS`). A single shared Foundry agent is reused across workers, and per-run token usage is aggregated under a `threading.Lock`. End-to-end run for 20 banks dropped from ~527 s to ~201 s in our benchmark.
- **Playwright fallback** — when a static fetch returns too few rate-like signals (e.g. JS-rendered pages), the agent re-fetches via headless Chromium.
- **Document Intelligence integration** — PDF circulars and image-based rate cards are processed with Azure DI `prebuilt-layout`; extracted text is returned to the agent.
- **Live progress log** — every scrape emits real-time events (per-URL start, tool calls, retries, completion). The UI polls `/api/scrape/progress` and shows them in an Activity panel.
- **Total scrape time** — every result payload now includes `elapsed_seconds`; the UI shows `Time: Xm Ys` next to bank/token counts.
- **Selective scraping** — choose specific banks in the URL manager and click "Scrape Selected" instead of running all.
- **Run-id locked activity** — when you press Scrape, the dashboard and activity log clear and the poller locks onto the new `run_id`, ignoring stale events from prior runs.
- **Server-side reset** — the Reset button now also calls `DELETE /api/results/latest`, removing the cached `latest.json` (local + blob) so a refresh will not repopulate the dashboard.
- **Connection-drop recovery** — if the browser fetch times out while the backend is still scraping, the UI polls until the backend reports `running:false` and then loads the result.


---

## Table of Contents

1. [Architecture](#architecture)
2. [Azure Resources](#azure-resources)
3. [Project Structure](#project-structure)
4. [File Reference](#file-reference)
5. [Prerequisites](#prerequisites)
6. [Environment Setup](#environment-setup)
7. [Running Locally](#running-locally)
8. [API Reference](#api-reference)
9. [How the Agent Works](#how-the-agent-works)
10. [Data Schema](#data-schema)
11. [UI Features](#ui-features)
12. [Blob Storage Contents](#blob-storage-contents)
13. [Production Deployment](#production-deployment)
14. [Responsible Fetching (robots.txt)](#responsible-fetching-robotstxt)
15. [Change Detection (HTTP cache)](#change-detection-http-cache)
16. [Code Commenting Convention](#code-commenting-convention)
17. [Troubleshooting](#troubleshooting)

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      Browser (React UI)                      │
│  localhost:3000  —  custom colour theme                   │
│  - Manage bank URLs          - View rate tables              │
│  - Trigger scrape            - Filter by category            │
│  - Export to Excel           - Token usage display           │
└───────────────────────┬─────────────────────────────────────┘
                        │ HTTP (REST)
                        ▼
┌─────────────────────────────────────────────────────────────┐
│               Flask Dev Server  (backend/dev_server.py)      │
│  localhost:7071  —  CORS-enabled REST API                    │
│  /api/urls   /api/scrape   /api/results   /api/export-excel  │
└───────────────────────┬─────────────────────────────────────┘
                        │ azure-ai-agents SDK
                        ▼
┌─────────────────────────────────────────────────────────────┐
│              Azure AI Foundry Agent  (gpt-4.1)               │
│  Account : web-tools   Project : prj-web-tools               │
│  Region  : South India                                       │
│                                                              │
│  Tool: fetch_webpage(url)                                    │
│    1. requests.get() + BeautifulSoup text extraction         │
│    2. If <MIN_PERCENT_SIGNS rate-like signals →              │
│         Playwright headless Chromium re-render               │
│    3. If URL ends in .pdf / image →                          │
│         Azure AI Document Intelligence (prebuilt-layout)     │
│    → returns ≤15,000 chars of visible text to the model      │
│                                                              │
│  Manual tool-call loop (up to 8 rounds per bank)             │
│  Emits progress events to a thread-safe ring buffer          │
│  Tracks prompt / completion / total token usage per run      │
└───────────────────────┬─────────────────────────────────────┘
                        │ azure-storage-blob SDK (Entra ID auth)
                        ▼
┌─────────────────────────────────────────────────────────────┐
│           Azure Blob Storage   (fd-rates container)          │
│  Account : fdratesstxf6etxfnua6lq                            │
│  fd_rates_YYYYMMDD_HHMMSS.json   ← timestamped scrape        │
│  latest.json                     ← always latest scrape      │
│  fd_rates_YYYYMMDD_HHMMSS.xlsx   ← timestamped Excel         │
│  latest.xlsx                     ← always latest Excel       │
└─────────────────────────────────────────────────────────────┘
```

---

## Azure Resources

| Resource | Name | Type | Region |
|---|---|---|---|
| AI Services account | `web-tools` | Azure AI Services (Cognitive Services) | South India |
| AI Foundry project | `prj-web-tools` | AI Foundry Project | South India |
| Model deployment | `gpt-4.1` | Azure OpenAI gpt-4.1 | South India |
| Document Intelligence | `fdrates-di-kvihlu` | Azure AI Document Intelligence (`prebuilt-layout`) | Central India |
| Resource group (AI) | `demo-web-tool` | Resource Group | — |
| Storage account | `fdratesstxf6etxfnua6lq` | Storage V2 Standard LRS | Central India |
| Blob container | `fd-rates` | Blob Container | — |
| Resource group (storage) | `rg-fd-rates` | Resource Group | Central India |

### Project endpoint

```
https://web-tools.services.ai.azure.com/api/projects/prj-web-tools
```

### Authentication

All Azure SDK calls use **`DefaultAzureCredential`**, which resolves to `AzureCliCredential` during local development. No connection strings or API keys are stored in code.

Required RBAC role on the storage account:

```
Role  : Storage Blob Data Contributor
Scope : /subscriptions/<YOUR_SUBSCRIPTION_ID>
          /resourceGroups/rg-fd-rates
          /providers/Microsoft.Storage/storageAccounts/fdratesstxf6etxfnua6lq
```

---

## Project Structure

```
fd-rates-with-foundary/
├── .env                        ← Local secrets (not committed)
├── .env.template               ← Template — copy to .env and fill in
├── .gitignore
├── setup.ps1                   ← PowerShell provisioning script
├── README.md
│
├── backend/
│   ├── dev_server.py           ← Flask REST API (local dev server)
│   ├── function_app.py         ← Azure Functions v2 (Python) — production
│   ├── host.json               ← Azure Functions host config
│   ├── local.settings.json     ← Azure Functions local environment variables
│   ├── requirements.txt        ← Python dependencies
│   ├── urls.json               ← Persisted bank URL list (20 banks)
│   ├── _local_results/         ← Local JSON/Excel result cache (dev only, gitignored)
│   └── agent/
│       ├── __init__.py
│       ├── fd_rate_agent.py    ← Core Foundry agent + tool-call loop
│       ├── dynamic_fetch.py    ← Playwright headless Chromium renderer
│       ├── asset_extractors.py ← Azure DI `prebuilt-layout` for PDFs / images
│       ├── progress.py         ← Thread-safe live progress event buffer
│       └── robots.py           ← robots.txt compliance (cached, thread-safe)
│
├── frontend/
│   ├── package.json
│   └── src/
│       ├── App.js              ← Root component, state, API calls
│       ├── App.css             ← Global styles (custom colour theme)
│       ├── index.js
│       └── components/
│           ├── UrlManager.js       ← Add / delete / select bank URLs
│           ├── ScrapeButton.js     ← Trigger scrape ("Scrape All" / "Scrape Selected")
│           ├── ExportButton.js     ← Trigger Excel export
│           ├── ProgressLog.js     ← Live activity log (run-id locked)
│           └── ResultsDashboard.js ← Rate tables + token usage + elapsed time
│
└── infra/
    ├── main.bicep              ← Full infrastructure (storage, functions, AI)
    └── project-only.bicep      ← AI Foundry project only
```

---

## File Reference

### `backend/agent/fd_rate_agent.py`

Core AI agent module.

| Function | Purpose |
|---|---|
| `create_agent(agents_client)` | Creates a Foundry agent with `gpt-4.1` and registers the `fetch_webpage` function tool schema |
| `fetch_webpage_handler(url)` | Three-tier fetch: (1) `requests.get` + BeautifulSoup; (2) if rate-like signals (`%`) below `MIN_PERCENT_SIGNS=20`, re-render with Playwright; (3) if URL is `.pdf` or image, run Document Intelligence `prebuilt-layout`. Returns ≤ 15,000 chars of visible text and emits per-URL progress events. |
| `scrape_bank_url(...)` | Creates a thread, sends a user message, manually polls `run.status`, handles `requires_action` tool call rounds (max 8), captures `run.usage` token counts |
| `_parse_agent_response(...)` | Strips markdown fences, attempts `json.loads()`, falls back to substring extraction between first `{` and last `}`, triggers a single auto-retry if both fail |
| `scrape_all_urls(urls)` | Creates a single shared `AgentsClient` + agent, dispatches each bank to a `ThreadPoolExecutor` (default 4 workers, configurable via `SCRAPE_MAX_WORKERS` env var), aggregates token usage under a thread lock, deletes the agent on completion, returns `{"results": [...], "token_usage": {...}, "elapsed_seconds": float}`. Results are kept in input order. |

**Tool call loop detail:**

```
runs.create()
  → poll while "queued" / "in_progress"
  → while status == "requires_action":
      for each tool_call in required_action.submit_tool_outputs.tool_calls:
          call fetch_webpage_handler(url)
      runs.submit_tool_outputs(tool_outputs)
      poll again
  → capture run.usage
  → read assistant messages
```

---

### `backend/agent/dynamic_fetch.py`

Playwright-based fallback renderer. Launches a headless Chromium browser, navigates to the URL with `wait_until="networkidle"`, returns the rendered HTML / extracted text. Triggered by `fetch_webpage_handler` when a static fetch yields fewer than `MIN_PERCENT_SIGNS=20` `%` characters (a heuristic for rate tables).

### `backend/agent/asset_extractors.py`

Azure AI Document Intelligence wrapper. Uses the `prebuilt-layout` model to OCR PDFs and image-based rate cards. Endpoint configured via `DOC_INTELLIGENCE_ENDPOINT` env var; auth via `DefaultAzureCredential` (requires `Cognitive Services User` role on the DI resource).

### `backend/agent/progress.py`

Thread-safe live progress event buffer. Exposes:

- `reset(total)` — clears buffer, assigns a fresh `run_id` (UUID), marks `running=True`.
- `log(message)` — appends `{ts, message}` event.
- `mark_done()` — sets `running=False`.
- `snapshot(since=N)` — returns `{run_id, running, total, events[since:]}` for incremental polling.

### `backend/agent/robots.py`

Wraps `urllib.robotparser.RobotFileParser` with a thread-safe per-origin in-memory cache. Exposes:

- `is_allowed(url) -> (bool, reason)` — returns `(True, "allowed by robots.txt")` or `(False, "disallowed by robots.txt for UA '...'")`. On network/parse error, returns `(True, "robots.txt unavailable")` per RFC 9309.

Config: `ROBOTS_RESPECT` (default `true`) and `ROBOTS_USER_AGENT` (default `FDRateAggregator`). See [Responsible Fetching](#responsible-fetching-robotstxt) for full details.

---

### `backend/dev_server.py`

Flask application serving the REST API on port **7071**. Mirrors the Azure Functions API surface so the React frontend works identically in both local dev and production.

Key implementation details:

- `CORS(app, origins=["http://localhost:3000"])` — allows cross-origin requests from the React dev server
- `_get_blob_service_client()` — creates `BlobServiceClient` using account URL from `STORAGE_ACCOUNT_NAME` env var + `DefaultAzureCredential`
- `_upload_to_blob(blob_name, data, content_type)` — uploads bytes to `BLOB_CONTAINER` with `overwrite=True`
- `scrape_all()` — accepts optional `{"urls": [...]}` body for selective scraping, wraps `scrape_all_urls()` with a `time.monotonic()` timer, unpacks `{"results": ..., "token_usage": ...}`, adds `elapsed_seconds`, uploads to blob as both timestamped and `latest.json`
- `scrape_progress()` — `GET /api/scrape/progress?since=N` returns `{run_id, running, total, events}` from the in-memory buffer
- `delete_latest()` — `DELETE /api/results/latest` removes the local `latest.json` and the blob `latest.json`
- `export_excel()` — generates `openpyxl` workbook with per-bank sheets, styled headers, alternating row colours, auto-filter; uploads as timestamped + `latest.xlsx`

---

### `backend/function_app.py`

Azure Functions (Python v2 programming model) equivalent of `dev_server.py`. Same API surface and same blob/agent logic, deployed to Azure Functions for production. Uses `func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)`.

---

### `backend/urls.json`

Persisted list of bank URLs. Managed via the `/api/urls` endpoints. Default content:

```json
[
  {
    "id": "...",
    "url": "https://www.hdfc.bank.in/interest-rates",
    "bank_name": "HDFC",
    "created_at": "..."
  },
  {
    "id": "...",
    "url": "https://www.indusind.bank.in/in/en/personal/rates.html",
    "bank_name": "IndusInd",
    "created_at": "..."
  },
  {
    "id": "...",
    "url": "https://sbi.co.in/web/interest-rates/deposit-rates/retail-domestic-term-deposits",
    "bank_name": "SBI",
    "created_at": "..."
  }
]
```

---

### `backend/host.json`

Azure Functions v2 host configuration. Extension bundle `[4.*, 5.0.0)`, route prefix `api`.

---

### `backend/local.settings.json`

Local environment variables for Azure Functions and the dev server. Set `IsEncrypted: false`. Values:

| Key | Value |
|---|---|
| `PROJECT_ENDPOINT` | `https://web-tools.services.ai.azure.com/api/projects/prj-web-tools` |
| `MODEL_DEPLOYMENT_NAME` | `gpt-4.1` |
| `BLOB_CONTAINER_NAME` | `fd-rates` |
| `STORAGE_ACCOUNT_NAME` | `fdratesstxf6etxfnua6lq` |
| `DOC_INTELLIGENCE_ENDPOINT` | `https://fdrates-di-kvihlu.cognitiveservices.azure.com/` |

---

### `frontend/src/App.js`

Root React component. State:
- `urls` — configured bank URL list
- `results` — last scrape payload including `token_usage`
- `scraping` / `exporting` — loading flags for spinner display
- `message` — status bar text

All API calls use `REACT_APP_API_BASE_URL` (defaults to `http://localhost:7071`).

---

### `frontend/src/components/ResultsDashboard.js`

Right panel rendering:
- **Token usage bar** — maroon gradient banner; shows `total`, `prompt`, `completion` tokens when `results.token_usage` is present
- **Category filter chips** — derived from all unique `category_name` values across all bank results
- **Bank sections** — collapsible per-bank cards with rate tables (`Tenor`, `Min Days`, `Max Days`, `Rate (%)`, `Info`)

---

### `frontend/src/App.css`

custom colour theme via CSS custom properties:

| Variable | Hex | Usage |
|---|---|---|
| `--primary` | `#97144D` | Buttons, table headers, active filter chips |
| `--primary-dark` | `#6B0E37` | Hover states, page header gradient start |
| `--primary-light` | `#C4547A` | Page header gradient end |
| `--accent` | `#12877F` | Export (Excel) button, accent elements |
| `--bg` | `#FDF5F8` | Page background |
| `--border` | `#e8d0db` | Card and table borders |

---

### `infra/main.bicep`

Full infrastructure-as-code template. Provisions:

| Resource | Naming pattern |
|---|---|
| Storage account | `${baseName}st${uniqueString(resourceGroup().id)}` |
| Blob container | `fd-rates` |
| App Service Plan | `${baseName}-plan-${uniqueSuffix}` |
| Azure Functions app | `${baseName}-func-${uniqueSuffix}` |
| Application Insights | `${baseName}-insights-${uniqueSuffix}` |
| Log Analytics workspace | `${baseName}-logs-${uniqueSuffix}` |
| AI Services account | `${baseName}-ai-${uniqueSuffix}` |
| AI Foundry project | `${baseName}-project` |

Parameters: `baseName` (default `fdrates`), `location` (default `centralindia`), `aiLocation` (default `centralindia`).

---

### `.env` / `.env.template`

```env
# Azure Subscription
AZURE_SUBSCRIPTION_ID=<YOUR_SUBSCRIPTION_ID>
AZURE_RESOURCE_GROUP=rg-fd-rates
AZURE_LOCATION=centralindia

# Azure AI Foundry
PROJECT_ENDPOINT=https://web-tools.services.ai.azure.com/api/projects/prj-web-tools
MODEL_DEPLOYMENT_NAME=gpt-4.1

# Azure Blob Storage
STORAGE_ACCOUNT_NAME=fdratesstxf6etxfnua6lq
BLOB_CONTAINER_NAME=fd-rates

# Frontend (React)
REACT_APP_API_BASE_URL=http://localhost:7071
```

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.11+ | Tested on 3.11 |
| Node.js 18+ | For the React frontend |
| Azure CLI | `az login --use-device-code` before running |
| Azure AI Services quota | `gpt-4.1` in South India |
| Storage Blob Data Contributor | RBAC role assigned on the storage account |

---

## Environment Setup

### 1. Clone and configure

```powershell
git clone <repo-url>
cd fd-rates-with-foundary
Copy-Item .env.template .env
# Edit .env with your values
```

### 2. Azure login

```powershell
az login --use-device-code
az account set --subscription <YOUR_SUBSCRIPTION_ID>
```

### 3. Backend — Python virtual environment

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install flask flask-cors requests beautifulsoup4
# Install the headless Chromium used by the Playwright fallback
python -m playwright install chromium
```

### 4. Frontend — Node dependencies

```powershell
cd ..\frontend
npm install
```

---

## Running Locally

### Start the backend

```powershell
cd backend
.\.venv\Scripts\Activate.ps1
$env:DOC_INTELLIGENCE_ENDPOINT = "https://fdrates-di-kvihlu.cognitiveservices.azure.com/"
python dev_server.py
# API available at http://localhost:7071
```

### Start the frontend

```powershell
cd frontend
npm start
# UI available at http://localhost:3000
```

### Kill an already-running backend and restart (PowerShell)

```powershell
$conn = Get-NetTCPConnection -LocalPort 7071 -ErrorAction SilentlyContinue | Select-Object -First 1
if ($conn) { Stop-Process -Id $conn.OwningProcess -Force -ErrorAction SilentlyContinue }
Set-Location backend
python dev_server.py
```

---

## API Reference

### `GET /api/urls`

Returns all configured bank URLs.

```json
[
  {
    "id": "e1a5f657-dedf-4396-86a0-77bea729081b",
    "url": "https://www.hdfc.bank.in/interest-rates",
    "bank_name": "HDFC",
    "created_at": "2026-04-21T04:57:36.183203+00:00"
  }
]
```

### `POST /api/urls`

Add a bank URL.

```json
{ "url": "https://...", "bank_name": "Your Bank" }
```

### `DELETE /api/urls/{id}`

Remove a bank URL by its UUID.

### `POST /api/scrape`

Trigger a full agent scrape across all configured URLs, or pass a JSON body to scrape a subset:

```json
{ "urls": ["<url-id-1>", "<url-id-2>"] }
```

Response:

```json
{
  "scraped_at": "2026-04-21T07:48:00+00:00",
  "bank_count": 20,
  "elapsed_seconds": 636.4,
  "token_usage": {
    "prompt_tokens": 105563,
    "completion_tokens": 35819,
    "total_tokens": 141382
  },
  "di_pages": 2,
  "results": [ ... ]
}
```

Also uploads `fd_rates_<timestamp>.json` and `latest.json` to blob storage.

### `GET /api/scrape/progress?since=N`

Returns the live progress buffer for the most recent (or in-flight) scrape:

```json
{
  "run_id": "a1b2c3d4-...",
  "running": true,
  "total": 20,
  "events": [
    { "ts": "2026-04-25T19:42:01Z", "message": "[1/20] HDFC — fetching https://..." },
    { "ts": "2026-04-25T19:42:08Z", "message": "[1/20] HDFC — Playwright fallback (signals=4)" }
  ]
}
```

Clients pass `since` (the index of the last event already received) for incremental polling. `run_id` changes on every new scrape; clients should clear local state when it does.

### `GET /api/results/latest`

Returns the cached result from the most recent scrape (same shape as `/api/scrape`).

### `DELETE /api/results/latest`

Clears the cached result. Removes both the local `latest.json` and the blob `latest.json`. Used by the UI's Reset button so a page refresh does not repopulate the dashboard.

```json
{
  "message": "Latest result cleared",
  "removed_local": true,
  "removed_blob": true
}
```

### `POST /api/export-excel`

Generates a formatted Excel workbook and uploads it to blob storage.

```json
{
  "message": "Excel exported successfully",
  "blob_name": "fd_rates_20260421_074832.xlsx",
  "latest_blob": "latest.xlsx",
  "bank_count": 3,
  "exported_at": "2026-04-21T07:48:32+00:00"
}
```

---

## How the Agent Works

1. **Agent creation** — a Foundry agent is created with `gpt-4.1` and one registered function tool: `fetch_webpage(url: string)`.

2. **Thread per bank** — each bank gets its own conversation thread. The user message instructs the agent to extract all FD rate information from the given URL and return it as structured JSON.

3. **Tool call loop** — up to **5 rounds** per bank:
   - The model decides to call `fetch_webpage` with a specific URL
   - `fetch_webpage_handler` fetches the page with a Chrome User-Agent, uses BeautifulSoup to remove `<script>`, `<style>`, `<nav>`, `<header>`, `<footer>` tags, collapses whitespace, and returns the first **15,000 characters** of visible text
   - Tool output is submitted; the model continues reasoning
   - If the model needs additional pages it calls `fetch_webpage` again in the next round

4. **Token tracking** — `run.usage.prompt_tokens`, `run.usage.completion_tokens`, and `run.usage.total_tokens` are captured after each run completion and aggregated across all banks and retry attempts.

5. **JSON extraction** — the assistant's final text message is cleaned and parsed:
   - Strip markdown code fences (` ```json ... ``` `)
   - Attempt `json.loads()`
   - Fallback: extract text between first `{` and last `}`
   - If still invalid: ask the model to reformat as strict compact JSON (one retry)

6. **Cleanup** — the Foundry agent is deleted via `agents_client.delete_agent()` after all banks are processed.

---

## Data Schema

Each bank result in `results`:

```json
{
  "bank_name": "HDFC",
  "url": "https://www.hdfc.bank.in/interest-rates",
  "effective_date": "2026-03-06",
  "categories": [
    {
      "category_name": "General Public",
      "amount_slab": "Less than 3 Cr",
      "scheme_name": null,
      "rates": [
        {
          "tenor_description": "7 - 14 days",
          "min_days": 7,
          "max_days": 14,
          "rate_percent": 2.75,
          "additional_info": null
        }
      ]
    }
  ]
}
```

On extraction failure:

```json
{
  "error": "Could not extract rates",
  "bank_name": "XYZ Bank",
  "url": "https://...",
  "reason": "Page content not parseable"
}
```

---

## UI Features

| Feature | Detail |
|---|---|
| Bank URL management | Sidebar form — add bank name + URL, delete individual entries, **select** banks via checkbox; persisted to `backend/urls.json` |
| Scrape All / Scrape Selected | Button label switches based on selection; calls `POST /api/scrape` (no body = all, body `{urls:[...]}` = selected). Shows spinner and disables button during execution. |
| Live Activity Log | Polls `GET /api/scrape/progress?since=N` every 2s; shows per-URL fetch events, Playwright fallbacks, DI page extracts, retry attempts, completion. Locked to the current `run_id` so stale events from the prior run never bleed into the next. |
| Results Dashboard | Per-bank collapsible sections with full rate tables (Tenor / Min Days / Max Days / Rate / Info); meta-info bar shows `Banks: N · Tokens: X · Time: Xm Ys`. |
| Category filter chips | Auto-generated from scraped data; filter across all banks simultaneously |
| Token usage bar | Maroon gradient banner: `total · prompt · completion` tokens from the last scrape |
| Reset | Clears dashboard + activity log AND calls `DELETE /api/results/latest` so a refresh does not repopulate. |
| Connection-drop recovery | If the long fetch to `/api/scrape` gets dropped by the dev proxy, the UI falls back to polling `/api/scrape/progress` until `running:false`, then loads `/api/results/latest`. |
| Write Excel | Calls `POST /api/export-excel`; shows uploaded blob name in the status bar |
| custom colour theme | Primary maroon `#97144D`, teal accent `#12877F`, blush background `#FDF5F8` |

---

## Blob Storage Contents

After a full scrape + export cycle the `fd-rates` container holds:

```
fd_rates_20260421_074454.json   ← archived scrape result
fd_rates_20260421_074832.xlsx   ← archived Excel workbook
latest.json                     ← always the most recent scrape
latest.xlsx                     ← always the most recent Excel file
```

Files are uploaded using `DefaultAzureCredential`. The `Storage Blob Data Contributor` role is required on the storage account. No SAS tokens or connection strings are used.

---

## Production Deployment

The `backend/function_app.py` file implements the same API using **Azure Functions v2 (Python)**. To deploy:

1. Provision infrastructure:
   ```powershell
   az deployment group create \
     --resource-group rg-fd-rates \
     --template-file infra/main.bicep
   ```

2. Deploy the function app:
   ```powershell
   func azure functionapp publish <function-app-name>
   ```

3. Set application settings on the Function App:
   - `PROJECT_ENDPOINT`
   - `MODEL_DEPLOYMENT_NAME`
   - `STORAGE_ACCOUNT_NAME`
   - `BLOB_CONTAINER_NAME`

4. Assign `Storage Blob Data Contributor` to the Function App's system-assigned managed identity on the storage account.

5. Update `REACT_APP_API_BASE_URL` to the Function App URL and redeploy the frontend.

---

## Responsible Fetching (`robots.txt`)

Every outbound HTTP request the agent makes — HTML page fetches, Playwright re-renders, and PDF/image downloads — first consults the origin's `/robots.txt`. Disallowed URLs are skipped **before** any network call is made and **before** any agent tokens are spent.

### How it works

`backend/agent/robots.py` wraps Python's standard `urllib.robotparser.RobotFileParser` with:

- **Per-origin in-memory cache** — `robots.txt` is fetched once per process per host, then re-used across all subsequent calls (and worker threads).
- **Thread-safe** — guarded by a `threading.Lock`, safe for the parallel ThreadPoolExecutor.
- **Fail-open by RFC 9309** — if `robots.txt` returns 4xx, treat as allow-all; if it returns 5xx / network error / parse error, default-allow with a warning.
- **8-second timeout** on the `robots.txt` fetch itself.

### Where it's enforced

1. **Pre-flight per bank** in `scrape_all_urls._scrape_one()` — short-circuits before creating a thread, so blocked banks consume **zero** agent tokens.
2. **Defense-in-depth in `fetch_webpage_handler()`** — guards `requests.get()` and the Playwright fallback.
3. **Defense-in-depth in `asset_extractors._download()`** — guards every PDF and image download triggered by the `fetch_pdf` / `fetch_image` tools.

### Configuration

| Env var | Default | Purpose |
|---|---|---|
| `ROBOTS_RESPECT` | `true` | Set to `false`/`0`/`no`/`off` to bypass entirely (not recommended for production). |
| `ROBOTS_USER_AGENT` | `FDRateAggregator` | User-agent string matched against `User-agent:` rules in the bank's `robots.txt`. Most bank sites use `*` rules, which always match. |

### What the user sees when blocked

- **Activity log (live)** — amber `⛔` warning with the URL, the reason, and the override hint.
- **Summary tab** — a distinct **"⛔ Blocked"** pill (separate from "✖ Failed") and the reason inline.
- **Rates tab → expanded bank** — amber callout: *"Blocked by robots.txt — no fetch was attempted, no tokens used."*
- **JSON result** — `{"blocked_by_robots": true, "error": "Blocked by robots.txt", "reason": "..."}`.

### Verifying compliance

```powershell
# Test against a real bank URL
python -c "from backend.agent.robots import is_allowed; print(is_allowed('https://www.hdfcbank.com/personal/resources/rates'))"
# Expected: (True, 'allowed by robots.txt')
```

---

## Change Detection (HTTP cache)

Bank FD-rate pages are republished roughly quarterly. To avoid re-running the
(expensive) Foundry agent + Document Intelligence pipeline on every fetch, an
**L1 HTTP-level short-circuit** in `backend/agent/http_cache.py` probes each
URL with a conditional `GET` *before* spending any tokens.

### How it works

For every URL, the previous run's response fingerprint is stored in
`backend/_local_results/state/url_state.json`:

```json
{
  "<url_id>": {
    "etag": "\"abc123\"",
    "last_modified": "Wed, 15 Oct 2025 09:21:00 GMT",
    "sha256": "f0c8…",
    "content_length": 84231,
    "last_checked_at": "2026-04-21T09:30:00+00:00",
    "last_changed_at": "2026-01-12T11:14:30+00:00"
  }
}
```

On the next run, before invoking the agent for a bank, the worker:

1. Sends a `GET` with `If-None-Match` and `If-Modified-Since` set from the
   stored fingerprint.
2. **`304 Not Modified`** → page is unchanged. Reuse the cached result from
   `state/per_url/<url_id>.json` and tag it `unchanged: true`. **0 tokens, 0
   DI pages.** Cheapest happy path.
3. **`200 OK` with matching body sha256** → same outcome (handles servers
   that ignore conditional headers but still serve byte-identical HTML).
4. **Anything else** (sha mismatch, non-2xx, transport error) → fall through
   to the full scrape; the new fingerprint is stored on success.

Fail-open: any exception during the probe triggers the full scrape — we never
return stale data because of a network blip.

### Configuration

| Env var | Default | Purpose |
|---|---|---|
| `STATE_DIR` | `backend/_local_results/state/` | Where the fingerprint and per-URL snapshot files live. |
| `FORCE_REFRESH` | unset | When truthy, every cache check returns *changed*, guaranteeing a full scrape. |
| `HTTP_CACHE_TIMEOUT_SECONDS` | `15` | Timeout for the conditional `GET` probe. |

The API also accepts `{"force": true}` in the `POST /api/scrape` body, and the
UI exposes a **"Force refresh (skip cache)"** checkbox in the sidebar.

### What the user sees on a cache hit

- **Activity log** — `↻ HDFC Bank: unchanged since 2026-01-12 — reused cached result (24 rates). 0 tokens used.`
- **Summary tab** — a separate **"↻ Unchanged"** pill and an *"Unchanged (cached)"* count card.
- **Run footer** — the final progress line includes `(N unchanged — reused from cache, 0 tokens)`.
- **JSON result** — `{"unchanged": true, "last_changed_at": "…"}` on the bank entry.

### Where the state lives

State files are excluded from git via `**/_local_results/` in `.gitignore`,
so they stay local to each environment. For the deployed Function App,
point `STATE_DIR` at a writable directory or a mounted volume.

---

## Code Commenting Convention

Every source file in this repo carries documentation that lets a new developer
understand it without spelunking. **Please keep this convention in any future
change** — reviewers will ask for it.

### Required for every file

1. **File header.** First lines of every `.py` / `.js` / `.bicep` / `.ps1`
   file must explain the module's *purpose* and (when relevant) its *inputs
   and outputs*. Use docstrings for Python, JSDoc/`//` blocks for JS,
   `//` for Bicep, and `<# .SYNOPSIS / .DESCRIPTION #>` for PowerShell.
2. **Function / component docstrings.** Every public function, React
   component, and Bicep resource block gets a one-paragraph description of
   what it does, its parameters, and any non-obvious side effects (network
   calls, file writes, blob uploads, RBAC implications).
3. **Section dividers.** When a file has multiple logical sections (e.g.,
   route handlers in `dev_server.py`, resource groups in `main.bicep`),
   separate them with banner comments (`# ---- ... ----` or `// === ... ===`).
4. **Inline notes for non-obvious logic.** Add a short comment immediately
   above any block whose intent isn't visible from the code itself —
   examples already in the codebase: the *"heuristic for JS-rendered pages"*
   note in `fetch_webpage_handler`, the *"default-allow per RFC 9309"* note
   in `robots.py`, the *"reset run_id on mid-poll change"* note in `App.js`.

### Style rules

- **Explain *why*, not *what*.** The code already says what it does;
  comments should explain reasoning, trade-offs, gotchas, and references.
- **Keep comments truthful.** If you change behaviour, update the comment in
  the same commit.
- **No commented-out code in PRs.** Delete it; git history is the archive.
- **Prefer one good comment over many small ones.** A clear paragraph at the
  top of a function beats line-by-line narration.

### Quick checklist before opening a PR

- [ ] Every new/modified file has a header describing its purpose.
- [ ] Every new public function has a docstring.
- [ ] Every non-obvious block has a *why* comment.
- [ ] Existing comments still match the code after your changes.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `AuthorizationFailure` on blob upload | Missing RBAC role, not logged in, or storage `publicNetworkAccess: Disabled` | `az login --use-device-code`; verify `Storage Blob Data Contributor` role; ensure `publicNetworkAccess` is `Enabled` (`az storage account update -g rg-fd-rates -n fdratesstxf6etxfnua6lq --public-network-access Enabled`) |
| Bank shows "⛔ Blocked by robots.txt" | The site's `/robots.txt` disallows your `ROBOTS_USER_AGENT` for that path | Verify by visiting `https://<bank-domain>/robots.txt` and looking for matching `Disallow:` rules. Either update the URL in `urls.json` to a permitted page, contact the bank for an exception, or (last resort, not recommended) set `ROBOTS_RESPECT=false`. |
| `json.JSONDecodeError` from agent | Model returned markdown or conversational text | Handled automatically by `_parse_agent_response` — check logs for "retry" messages |
| `requires_action` never resolves | Tool output not submitted correctly | Verify `tool_call.id` is passed correctly to `submit_tool_outputs` |
| Empty results / `error` key in result | Bank site is JS-rendered or PDF-based and fallback didn't trigger | Check the activity log for "Playwright fallback" / "DI extract" messages; lower `MIN_PERCENT_SIGNS` in `fd_rate_agent.py` if needed |
| Playwright `Executable doesn't exist` | Browser binaries not installed | Run `python -m playwright install chromium` |
| Document Intelligence 401/403 | Missing role on the DI resource | Assign `Cognitive Services User` to your identity on `fdrates-di-kvihlu` |
| Scrape "failed" but backend still running | Dev proxy dropped the long fetch (~10 min for 20 URLs) | The UI now auto-recovers via `waitForBackendIdle()` — wait for it to load the result |
| Activity log shows old events | Stale `run_id` from previous run | The poller now locks onto `run_id`; if you still see this, hard-refresh the page |
| Port 7071 already in use | Previous Flask process still running | Kill with `Stop-Process` (see [Running Locally](#running-locally)) |
| `NameError: ListSortOrder` | Old SDK import left in code | Remove the `order=ListSortOrder.DESCENDING` argument |
| CORS error in browser | Flask CORS not configured | Ensure `CORS(app, origins=["http://localhost:3000"])` is present in `dev_server.py` |

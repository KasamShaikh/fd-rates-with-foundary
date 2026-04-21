# FD Rate Scraper

An AI-powered Fixed Deposit rate aggregator for Indian banks. Uses **Azure AI Foundry** (`gpt-4.1`) as an intelligent agent with a custom `fetch_webpage` tool to scrape, parse, and structure FD rate data from bank websites. Results are stored in **Azure Blob Storage** and exposed through a **Flask REST API** consumed by a **React** browser UI.

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
14. [Troubleshooting](#troubleshooting)

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      Browser (React UI)                      │
│  localhost:3000  —  Axis Bank colour theme                   │
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
│    → requests.get() + BeautifulSoup text extraction          │
│    → returns ≤15,000 chars of visible text to the model      │
│                                                              │
│  Manual tool-call loop (up to 5 rounds per bank)             │
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
Scope : /subscriptions/a012b726-d694-4532-a833-1bc28b0185a2
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
│   ├── urls.json               ← Persisted bank URL list
│   ├── _local_results/         ← Local JSON/Excel result cache (dev only)
│   └── agent/
│       ├── __init__.py
│       └── fd_rate_agent.py    ← Core Foundry agent logic
│
├── frontend/
│   ├── package.json
│   └── src/
│       ├── App.js              ← Root component, state, API calls
│       ├── App.css             ← Global styles (Axis Bank theme)
│       ├── index.js
│       └── components/
│           ├── UrlManager.js       ← Add / delete bank URLs
│           ├── ScrapeButton.js     ← Trigger scrape
│           ├── ExportButton.js     ← Trigger Excel export
│           └── ResultsDashboard.js ← Rate tables + token usage bar
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
| `fetch_webpage_handler(url)` | Fetches a URL with a Chrome User-Agent; strips HTML via BeautifulSoup (removes `<script>`, `<style>`, `<nav>`, `<header>`, `<footer>`); returns ≤ 15,000 chars of visible text |
| `scrape_bank_url(...)` | Creates a thread, sends a user message, manually polls `run.status`, handles `requires_action` tool call rounds (max 5), captures `run.usage` token counts |
| `_parse_agent_response(...)` | Strips markdown fences, attempts `json.loads()`, falls back to substring extraction between first `{` and last `}`, triggers a single auto-retry if both fail |
| `scrape_all_urls(urls)` | Creates `AgentsClient`, iterates all banks, accumulates token usage across all runs, deletes the agent on completion, returns `{"results": [...], "token_usage": {...}}` |

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

### `backend/dev_server.py`

Flask application serving the REST API on port **7071**. Mirrors the Azure Functions API surface so the React frontend works identically in both local dev and production.

Key implementation details:

- `CORS(app, origins=["http://localhost:3000"])` — allows cross-origin requests from the React dev server
- `_get_blob_service_client()` — creates `BlobServiceClient` using account URL from `STORAGE_ACCOUNT_NAME` env var + `DefaultAzureCredential`
- `_upload_to_blob(blob_name, data, content_type)` — uploads bytes to `BLOB_CONTAINER` with `overwrite=True`
- `scrape_all()` — calls `scrape_all_urls(urls)`, unpacks `{"results": ..., "token_usage": ...}`, builds the full payload, uploads to blob as both timestamped and `latest.json`
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

Axis Bank colour theme via CSS custom properties:

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
AZURE_SUBSCRIPTION_ID=a012b726-d694-4532-a833-1bc28b0185a2
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
az account set --subscription a012b726-d694-4532-a833-1bc28b0185a2
```

### 3. Backend — Python virtual environment

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install flask flask-cors requests beautifulsoup4
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
{ "url": "https://...", "bank_name": "Axis Bank" }
```

### `DELETE /api/urls/{id}`

Remove a bank URL by its UUID.

### `POST /api/scrape`

Trigger a full agent scrape across all configured URLs. Response:

```json
{
  "scraped_at": "2026-04-21T07:48:00+00:00",
  "bank_count": 3,
  "token_usage": {
    "prompt_tokens": 16531,
    "completion_tokens": 8875,
    "total_tokens": 25406
  },
  "results": [ ... ]
}
```

Also uploads `fd_rates_<timestamp>.json` and `latest.json` to blob storage.

### `GET /api/results/latest`

Returns the cached result from the most recent scrape (same shape as above).

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
| Bank URL management | Sidebar form — add bank name + URL, delete individual entries; persisted to `backend/urls.json` |
| Scrape All Banks | Calls `POST /api/scrape`; shows spinner and disables button during execution |
| Results Dashboard | Per-bank collapsible sections with full rate tables (Tenor / Min Days / Max Days / Rate / Info) |
| Category filter chips | Auto-generated from scraped data; filter across all banks simultaneously |
| Token usage bar | Maroon gradient banner: `total · prompt · completion` tokens from the last scrape |
| Write Excel | Calls `POST /api/export-excel`; shows uploaded blob name in the status bar |
| Axis Bank theme | Primary maroon `#97144D`, teal accent `#12877F`, blush background `#FDF5F8` |

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

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `AuthorizationFailure` on blob upload | Missing RBAC role or not logged in | `az login --use-device-code`; verify `Storage Blob Data Contributor` role is assigned |
| `json.JSONDecodeError` from agent | Model returned markdown or conversational text | Handled automatically by `_parse_agent_response` — check logs for "retry" messages |
| `requires_action` never resolves | Tool output not submitted correctly | Verify `tool_call.id` is passed correctly to `submit_tool_outputs` |
| Empty results / `error` key in result | Bank website structure changed or JS-rendered | Inspect the fetched text via logging; consider adding the URL to a headless browser path |
| Port 7071 already in use | Previous Flask process still running | Kill with `Stop-Process` (see [Running Locally](#running-locally)) |
| `NameError: ListSortOrder` | Old SDK import left in code | Remove the `order=ListSortOrder.DESCENDING` argument |
| CORS error in browser | Flask CORS not configured | Ensure `CORS(app, origins=["http://localhost:3000"])` is present in `dev_server.py` |

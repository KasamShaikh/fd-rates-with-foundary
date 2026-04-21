<#
.SYNOPSIS
    Provisions Azure infrastructure for the FD Rate Scraper application.
.DESCRIPTION
    Creates a resource group in Central India and deploys all required resources
    using the Bicep template: Storage Account, Function App, AI Foundry, Bing Search.
    Also registers the Bing provider, creates the Bing Grounding connection,
    and assigns RBAC roles for the current user.
#>

param(
    [string]$ResourceGroupName = "rg-fd-rates",
    [string]$Location = "centralindia",
    [string]$AILocation = "centralindia",
    [string]$BaseName = "fdrates",
    [string]$BingConnectionName = "bing-grounding-connection"
)

$ErrorActionPreference = "Stop"

Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  FD Rate Scraper — Azure Setup" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan

# -------------------------------------------------------
# 1. Check prerequisites
# -------------------------------------------------------
Write-Host "`n[1/8] Checking prerequisites..." -ForegroundColor Yellow

if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    Write-Error "Azure CLI (az) is not installed. Please install from https://aka.ms/installazurecli"
    exit 1
}

$account = az account show 2>$null | ConvertFrom-Json
if (-not $account) {
    Write-Host "Not logged in. Running 'az login'..."
    az login
    $account = az account show | ConvertFrom-Json
}
Write-Host "  Subscription: $($account.name) ($($account.id))" -ForegroundColor Green

# -------------------------------------------------------
# 2. Register Bing provider
# -------------------------------------------------------
Write-Host "`n[2/8] Registering Microsoft.Bing provider..." -ForegroundColor Yellow
az provider register --namespace "Microsoft.Bing" --wait 2>$null
Write-Host "  Microsoft.Bing provider registered." -ForegroundColor Green

# -------------------------------------------------------
# 3. Create Resource Group
# -------------------------------------------------------
Write-Host "`n[3/8] Creating resource group '$ResourceGroupName' in '$Location'..." -ForegroundColor Yellow
az group create --name $ResourceGroupName --location $Location --output none
Write-Host "  Resource group created." -ForegroundColor Green

# -------------------------------------------------------
# 4. Deploy Bicep template
# -------------------------------------------------------
Write-Host "`n[4/8] Deploying Bicep template..." -ForegroundColor Yellow
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$bicepPath = Join-Path $scriptDir "infra\main.bicep"

$ErrorActionPreference = "Continue"
$deploymentJson = az deployment group create `
    --resource-group $ResourceGroupName `
    --template-file $bicepPath `
    --parameters baseName=$BaseName location=$Location aiLocation=$AILocation `
    --output json
$ErrorActionPreference = "Stop"

if ($LASTEXITCODE -ne 0) {
    Write-Error "Bicep deployment failed. See errors above."
    exit 1
}

$deployment = $deploymentJson | ConvertFrom-Json
$outputs = $deployment.properties.outputs
$storageAccountName = $outputs.storageAccountName.value
$functionAppName = $outputs.functionAppName.value
$functionAppUrl = $outputs.functionAppUrl.value
$aiServicesName = $outputs.aiServicesName.value
$aiProjectName = $outputs.aiProjectName.value
$projectEndpoint = $outputs.projectEndpoint.value
$bingSearchName = $outputs.bingSearchName.value
$funcPrincipalId = $outputs.functionAppPrincipalId.value

Write-Host "  Deployment complete!" -ForegroundColor Green
Write-Host "    Storage Account : $storageAccountName"
Write-Host "    Function App    : $functionAppName"
Write-Host "    AI Services     : $aiServicesName"
Write-Host "    AI Project      : $aiProjectName"
Write-Host "    Bing Search     : $bingSearchName"
Write-Host "    Project Endpoint: $projectEndpoint"

# -------------------------------------------------------
# 5. Get Bing Search key and create Foundry connection
# -------------------------------------------------------
Write-Host "`n[5/8] Creating Bing Grounding connection in AI Foundry project..." -ForegroundColor Yellow

$bingKeysJson = az rest --method post `
    --uri "https://management.azure.com/subscriptions/$($account.id)/resourceGroups/$ResourceGroupName/providers/Microsoft.Bing/accounts/$bingSearchName/listKeys?api-version=2020-06-10" `
    --output json 2>$null

if (-not $bingKeysJson) {
    $bingKeysJson = az rest --method get `
        --uri "https://management.azure.com/subscriptions/$($account.id)/resourceGroups/$ResourceGroupName/providers/Microsoft.Bing/accounts/$bingSearchName/listKeys?api-version=2020-06-10" `
        --output json
}

$bingKeys = $bingKeysJson | ConvertFrom-Json
$bingKey = $bingKeys.key1

$aiServicesResourceId = "/subscriptions/$($account.id)/resourceGroups/$ResourceGroupName/providers/Microsoft.CognitiveServices/accounts/$aiServicesName"

# Use native account connection command with the schema it expects.
$connectionFilePath = Join-Path $env:TEMP "bing-connection.json"
$connectionSpec = @{
    type = "BingLLMSearch"
    target = "https://api.bing.microsoft.com/"
    credentials = @{
        type = "ApiKey"
        key = $bingKey
    }
    metadata = @{
        Location = "global"
    }
} | ConvertTo-Json -Depth 6
$connectionSpec | Out-File -FilePath $connectionFilePath -Encoding ascii

az cognitiveservices account connection create `
    --resource-group $ResourceGroupName `
    --name $aiServicesName `
    --connection-name $BingConnectionName `
    --file $connectionFilePath `
    --output none 2>$null

if ($LASTEXITCODE -ne 0) {
    # If it already exists, rotate key via update; otherwise continue with a warning.
    az cognitiveservices account connection update `
        --resource-group $ResourceGroupName `
        --name $aiServicesName `
        --connection-name $BingConnectionName `
        --set properties.credentials.key=$bingKey `
        --output none 2>$null

    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Could not create/update Bing connection '$BingConnectionName'. Continuing setup."
    } else {
        Write-Host "  Bing Grounding connection '$BingConnectionName' updated." -ForegroundColor Green
    }
} else {
    Write-Host "  Bing Grounding connection '$BingConnectionName' created." -ForegroundColor Green
}

# -------------------------------------------------------
# 6. Assign RBAC for current user (local dev)
# -------------------------------------------------------
Write-Host "`n[6/8] Assigning RBAC roles for current user (local development)..." -ForegroundColor Yellow

$currentUser = az ad signed-in-user show --output json | ConvertFrom-Json
$userId = $currentUser.id

# Storage Blob Data Contributor
az role assignment create `
    --assignee $userId `
    --role "Storage Blob Data Contributor" `
    --scope "/subscriptions/$($account.id)/resourceGroups/$ResourceGroupName/providers/Microsoft.Storage/storageAccounts/$storageAccountName" `
    --output none 2>$null

# Cognitive Services OpenAI User
az role assignment create `
    --assignee $userId `
    --role "Cognitive Services OpenAI User" `
    --scope $aiServicesResourceId `
    --output none 2>$null

# Azure AI Developer
az role assignment create `
    --assignee $userId `
    --role "Azure AI Developer" `
    --scope $aiServicesResourceId `
    --output none 2>$null

Write-Host "  RBAC roles assigned." -ForegroundColor Green

# -------------------------------------------------------
# 7. Model deployment (using pre-existing gpt-4.1 in prj-web-tools)
# -------------------------------------------------------
Write-Host "`n[7/8] Using pre-existing gpt-4.1 model in prj-web-tools..." -ForegroundColor Yellow
# Model 'gpt-4.1' is already deployed in the web-tools AI Services account.
# Override projectEndpoint to use prj-web-tools project.
$projectEndpoint = "https://web-tools.services.ai.azure.com/api/projects/prj-web-tools"
$modelDeploymentName = "gpt-4.1"
Write-Host "  Using project endpoint: $projectEndpoint" -ForegroundColor Green
Write-Host "  Using model deployment: $modelDeploymentName" -ForegroundColor Green

# -------------------------------------------------------
# 8. Generate .env file
# -------------------------------------------------------
Write-Host "`n[8/8] Generating .env file..." -ForegroundColor Yellow

$envLines = @(
    "# Auto-generated by setup.ps1 on $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')",
    "AZURE_SUBSCRIPTION_ID=$($account.id)",
    "AZURE_RESOURCE_GROUP=$ResourceGroupName",
    "AZURE_LOCATION=$Location",
    "",
    "# Azure AI Foundry",
    "PROJECT_ENDPOINT=$projectEndpoint",
    "MODEL_DEPLOYMENT_NAME=$modelDeploymentName",
    "BING_CONNECTION_NAME=$BingConnectionName",
    "",
    "# Azure Storage (Entra ID auth)",
    "STORAGE_ACCOUNT_NAME=$storageAccountName",
    "BLOB_CONTAINER_NAME=fd-rates",
    "",
    "# Azure Functions",
    "FUNCTIONS_APP_NAME=$functionAppName",
    "",
    "# Frontend",
    "REACT_APP_API_BASE_URL=http://localhost:7071"
)

$envPath = Join-Path $scriptDir ".env"
$envLines -join "`n" | Out-File -FilePath $envPath -Encoding utf8
Write-Host "  .env file written to: $envPath" -ForegroundColor Green

# Also update local.settings.json for backend
$localSettings = @{
    IsEncrypted = $false
    Values = @{
        FUNCTIONS_WORKER_RUNTIME = "python"
        AzureWebJobsStorage = "UseDevelopmentStorage=true"
        STORAGE_ACCOUNT_NAME = $storageAccountName
        BLOB_CONTAINER_NAME = "fd-rates"
        PROJECT_ENDPOINT = $projectEndpoint
        MODEL_DEPLOYMENT_NAME = $modelDeploymentName
        BING_CONNECTION_NAME = $BingConnectionName
    }
    Host = @{
        CORS = "http://localhost:3000"
        CORSCredentials = $false
    }
}
$localSettingsPath = Join-Path $scriptDir "backend\local.settings.json"
$localSettings | ConvertTo-Json -Depth 3 | Out-File -FilePath $localSettingsPath -Encoding utf8
Write-Host "  local.settings.json updated." -ForegroundColor Green

# -------------------------------------------------------
# Summary
# -------------------------------------------------------
Write-Host "`n============================================" -ForegroundColor Cyan
Write-Host "  Setup Complete!" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Yellow
Write-Host "  1. cd backend; pip install -r requirements.txt"
Write-Host "  2. func start"
Write-Host "  3. (New terminal) cd frontend; npm install; npm start"
Write-Host ""
Write-Host "Resources:" -ForegroundColor Yellow
Write-Host "  Resource Group  : $ResourceGroupName"
Write-Host "  Function App    : $functionAppUrl"
Write-Host ("  Project Endpoint: " + $projectEndpoint)
Write-Host ""

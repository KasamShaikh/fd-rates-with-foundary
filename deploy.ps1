# =============================================================================
# deploy.ps1 — One-shot cloud deployment for the FD Rate Aggregator
#
# Provisions/updates infra via Bicep, builds & pushes the backend image to ACR,
# rolls the Container App to the new image, builds the React app, deploys it
# to Static Web Apps, and rewires CORS so the SWA URL can call the backend.
#
# Prerequisites:
#   - Azure CLI logged in to the right subscription:    az login
#   - Docker Desktop running (for backend image build)
#   - Node.js 18+ installed (for frontend build)
#   - SWA CLI installed:                                npm i -g @azure/static-web-apps-cli
#
# Usage:
#   pwsh ./deploy.ps1 -ResourceGroup rg-fd-rates -Location centralindia
# =============================================================================

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $ResourceGroup,
    [string] $Location = 'centralindia',
    [string] $BaseName = 'fdrates',
    [string] $ImageTag = (Get-Date -Format 'yyyyMMdd-HHmmss')
)

$ErrorActionPreference = 'Stop'
$repoRoot = $PSScriptRoot

function Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }

# -----------------------------------------------------------------------------
Step "1/7 Ensure resource group exists"
az group create --name $ResourceGroup --location $Location | Out-Null

# -----------------------------------------------------------------------------
Step "2/7 First-pass Bicep deploy (creates ACR + ACA on bootstrap image)"
$firstPass = az deployment group create `
    --resource-group $ResourceGroup `
    --template-file (Join-Path $repoRoot 'infra/main.bicep') `
    --parameters baseName=$BaseName location=$Location useAcrImage=$false `
    --query 'properties.outputs' -o json | ConvertFrom-Json

$acrName       = $firstPass.containerRegistryName.value
$acrLogin      = $firstPass.containerRegistryLoginServer.value
$containerApp  = $firstPass.containerAppName.value
$swaName       = $firstPass.staticWebAppName.value

Write-Host "ACR:           $acrLogin"
Write-Host "Container App: $containerApp"
Write-Host "Static Web:    $swaName"

# -----------------------------------------------------------------------------
Step "3/7 Build & push backend image to ACR"
# `az acr build` uses ACR Tasks — no local Docker daemon needed and runs
# inside Azure (faster + works on any platform/architecture).
az acr build `
    --registry $acrName `
    --image "fdrates-backend:$ImageTag" `
    --image "fdrates-backend:latest" `
    --file (Join-Path $repoRoot 'backend/Dockerfile') `
    $repoRoot | Out-Null

# -----------------------------------------------------------------------------
Step "4/7 Re-deploy Bicep, this time pointing the Container App at the ACR image"
$secondPass = az deployment group create `
    --resource-group $ResourceGroup `
    --template-file (Join-Path $repoRoot 'infra/main.bicep') `
    --parameters baseName=$BaseName location=$Location `
                 useAcrImage=$true backendImageTag=$ImageTag `
    --query 'properties.outputs' -o json | ConvertFrom-Json

$apiFqdn       = $secondPass.containerAppFqdn.value
$swaHostname   = $secondPass.staticWebAppDefaultHostname.value
$apiUrl        = "https://$apiFqdn"
$swaUrl        = "https://$swaHostname"

Write-Host "Backend API:   $apiUrl"
Write-Host "Frontend URL:  $swaUrl"

# -----------------------------------------------------------------------------
Step "5/7 Re-deploy Bicep with CORS pointing at the SWA URL"
az deployment group create `
    --resource-group $ResourceGroup `
    --template-file (Join-Path $repoRoot 'infra/main.bicep') `
    --parameters baseName=$BaseName location=$Location `
                 useAcrImage=$true backendImageTag=$ImageTag `
                 allowedOrigin=$swaUrl `
    --query 'properties.outputs.containerAppFqdn.value' -o tsv | Out-Null

# -----------------------------------------------------------------------------
Step "6/7 Build the React frontend with the API URL baked in"
Push-Location (Join-Path $repoRoot 'frontend')
try {
    if (-not (Test-Path 'node_modules')) { npm ci }
    $env:REACT_APP_API_BASE_URL = $apiUrl
    npm run build
}
finally { Pop-Location }

# -----------------------------------------------------------------------------
Step "7/7 Deploy the build to Static Web Apps"
$swaToken = az staticwebapp secrets list `
    --name $swaName --resource-group $ResourceGroup `
    --query 'properties.apiKey' -o tsv

swa deploy (Join-Path $repoRoot 'frontend/build') `
    --deployment-token $swaToken `
    --env production

Write-Host "`nDeployment complete." -ForegroundColor Green
Write-Host "  Frontend: $swaUrl"
Write-Host "  Backend:  $apiUrl"

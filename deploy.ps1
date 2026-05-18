# =============================================================================
# deploy.ps1 — One-shot cloud deployment for the FD Rate Aggregator
#
# Uses existing Azure resources, builds & pushes the backend image to ACR,
# updates the existing App Service container image, builds the React app,
# deploys it to Static Web Apps, and rewires CORS so the SWA URL can call the
# backend.
#
# Prerequisites:
#   - Azure CLI logged in to the right subscription:    az login
#   - Node.js 18+ installed (for frontend build)
#   - SWA CLI installed:                                npm i -g @azure/static-web-apps-cli
#
# Usage:
#   pwsh ./deploy.ps1 -ResourceGroup rg-fd-rates-aca -Location centralindia
#
# Set -UseExistingResourcesOnly:$false to run the original Bicep provisioning
# flow instead.
# =============================================================================

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $ResourceGroup,
    [string] $Location = 'centralindia',
    [string] $BaseName = 'fdrates',
    [string] $ImageTag = (Get-Date -Format 'yyyyMMdd-HHmmss'),
    [string] $LocalImageTarDirectory = '',
    [bool] $UseExistingResourcesOnly = $true
)

$ErrorActionPreference = 'Stop'
$repoRoot = $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($LocalImageTarDirectory)) {
    $LocalImageTarDirectory = Join-Path $repoRoot 'artifacts'
}

function Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }

function Resolve-ResourceName([string] $resourceType, [string] $namePrefix) {
    $resolved = az resource list `
        --resource-group $ResourceGroup `
        --resource-type $resourceType `
        --query "[?starts_with(name, '$namePrefix')].name | [0]" `
        -o tsv

    if ([string]::IsNullOrWhiteSpace($resolved)) {
        throw "Could not find an existing resource of type '$resourceType' with prefix '$namePrefix' in resource group '$ResourceGroup'."
    }

    return $resolved.Trim()
}

function Assert-NotEmpty([string] $value, [string] $description) {
    if ([string]::IsNullOrWhiteSpace($value)) {
        throw "Could not resolve $description."
    }

    return $value.Trim()
}

if ($UseExistingResourcesOnly) {
    # -----------------------------------------------------------------------------
    # [SPA-SERVE-FROM-BACKEND] This existing-resources path now bakes the React
    # build INTO the backend container image and serves it from Flask, so the
    # Static Web App is no longer required. To revert: restore the previous
    # 5-step flow (build frontend -> swa deploy) from git history.
    # -----------------------------------------------------------------------------
    Step "1/4 Resolve existing resources"
    az group show --name $ResourceGroup | Out-Null

    $acrName = Resolve-ResourceName 'Microsoft.ContainerRegistry/registries' "${BaseName}acr"
    $webAppName = Resolve-ResourceName 'Microsoft.Web/sites' "${BaseName}-web-app-"

    $acrLogin = Assert-NotEmpty (az acr show --name $acrName --resource-group $ResourceGroup --query loginServer -o tsv) 'ACR login server'
    $apiFqdn = Assert-NotEmpty (az webapp show --name $webAppName --resource-group $ResourceGroup --query defaultHostName -o tsv) 'Web App hostname'

    $apiUrl = "https://$apiFqdn"

    Write-Host "ACR:           $acrLogin"
    Write-Host "Web App:       $webAppName"
    Write-Host "App URL:       $apiUrl"

    # -----------------------------------------------------------------------------
    # [SPA-SERVE-FROM-BACKEND] Build the React app with an empty API base URL
    # so it issues same-origin /api/... calls when served by Flask.
    Step "2/4 Build the React frontend (same-origin, no API base URL)"
    Push-Location (Join-Path $repoRoot 'frontend')
    try {
        if (-not (Test-Path 'node_modules')) { npm ci }
        $env:REACT_APP_API_BASE_URL = ''
        npm run build
    }
    finally { Pop-Location }

    # -----------------------------------------------------------------------------
    # [SPA-SERVE-FROM-BACKEND] Build the combined image in ACR (frontend/build
    # is now part of the build context). Uses the unique $ImageTag so each
    # deployment is identifiable.
    Step "3/4 Build & push combined backend+frontend image to ACR"
    az acr build `
        --registry $acrName `
        --image "fdrates-backend:$ImageTag" `
        --image "fdrates-backend:latest" `
        --no-logs `
        --file (Join-Path $repoRoot 'backend/Dockerfile') `
        $repoRoot | Out-Null

    # -----------------------------------------------------------------------------
    Step "4/4 Point the App Service at the new image and restart"
    $backendImageRef = "$acrLogin/fdrates-backend:$ImageTag"
    az webapp config container set `
        --name $webAppName `
        --resource-group $ResourceGroup `
        --container-image-name $backendImageRef | Out-Null

    az webapp config set `
        --name $webAppName `
        --resource-group $ResourceGroup `
        --acr-use-identity true `
        --acr-identity '[system]' `
        --always-on true `
        --http20-enabled true `
        --ftps-state Disabled `
        --min-tls-version 1.2 | Out-Null

    # [SPA-SERVE-FROM-BACKEND] Same-origin = no browser CORS needed; allow only
    # the App Service URL itself.
    az webapp cors add `
        --name $webAppName `
        --resource-group $ResourceGroup `
        --allowed-origins $apiUrl | Out-Null

    az webapp restart --name $webAppName --resource-group $ResourceGroup | Out-Null

    Write-Host "`nDeployment complete." -ForegroundColor Green
    Write-Host "  App (UI + API): $apiUrl"
    return
}

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
$localImageTag = "$acrLogin/fdrates-backend:$ImageTag"
$localLatestTag = "$acrLogin/fdrates-backend:latest"
$localImageTarPath = Join-Path $LocalImageTarDirectory "fdrates-backend-$ImageTag.tar"

New-Item -ItemType Directory -Force -Path $LocalImageTarDirectory | Out-Null

docker build `
    --platform linux/amd64 `
    --tag $localImageTag `
    --tag $localLatestTag `
    --file (Join-Path $repoRoot 'backend/Dockerfile') `
    $repoRoot

docker save `
    --output $localImageTarPath `
    $localImageTag `
    $localLatestTag

Write-Host "Local image tar: $localImageTarPath"

# `az acr build` still uses ACR Tasks for the cloud build and push. The local
# build above exists only to produce a portable tar artifact.
az acr build `
    --registry $acrName `
    --image "fdrates-backend:$ImageTag" `
    --image "fdrates-backend:latest" `
    --no-logs `
    --no-wait `
    --file (Join-Path $repoRoot 'backend/Dockerfile') `
    $repoRoot | Out-Null

# -----------------------------------------------------------------------------
Step "4/7 Re-deploy Bicep, this time pointing the App Service at the ACR image"
$secondPass = az deployment group create `
    --resource-group $ResourceGroup `
    --template-file (Join-Path $repoRoot 'infra/main.bicep') `
    --parameters baseName=$BaseName location=$Location `
                 useAcrImage=$true backendImageTag=$ImageTag `
    --query 'properties.outputs' -o json | ConvertFrom-Json

$apiFqdn       = $secondPass.webAppDefaultHostname.value
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

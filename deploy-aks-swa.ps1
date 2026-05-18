[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $ResourceGroup,
    [string] $Location = 'centralindia',
    [string] $SwaLocation = 'eastasia',
    [string] $BaseName = 'fdratesaks',
    [string] $ImageTag = 'latest',
    [string] $FrontendImageTag = '',
    [string] $Namespace = 'fd-rates-aks',
    [string] $ServiceAccountName = 'fd-rates-sa',
    [switch] $EnableWorkloadIdentity,
    [switch] $SkipWhatIf
)

$ErrorActionPreference = 'Stop'
$repoRoot = $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($FrontendImageTag)) {
    $FrontendImageTag = $ImageTag
}

function Step($msg) {
    Write-Host "`n=== $msg ===" -ForegroundColor Cyan
}

function Assert-NotEmpty([string] $value, [string] $description) {
    if ([string]::IsNullOrWhiteSpace($value)) {
        throw "Could not resolve $description."
    }

    return $value.Trim()
}

function Resolve-ResourceName([string] $resourceType, [string] $namePrefix) {
    $resolved = az resource list `
        --resource-group $ResourceGroup `
        --resource-type $resourceType `
        --query "[?starts_with(name, '$namePrefix')].name | [0]" `
        -o tsv

    if ([string]::IsNullOrWhiteSpace($resolved)) {
        throw "Could not find resource type '$resourceType' with prefix '$namePrefix' in '$ResourceGroup'."
    }

    return $resolved.Trim()
}

function Render-Template([string] $templatePath, [string] $outputPath, [hashtable] $replacements) {
    $content = Get-Content -Raw -Path $templatePath
    foreach ($key in $replacements.Keys) {
        $content = $content.Replace($key, $replacements[$key])
    }

    Set-Content -Path $outputPath -Value $content -Encoding utf8
}

$workloadIdentityEnabled = $EnableWorkloadIdentity.IsPresent

Step '1/8 Resolve existing shared resources (no changes to existing app setups)'
az group show --name $ResourceGroup | Out-Null

$acrName = Resolve-ResourceName 'Microsoft.ContainerRegistry/registries' 'fdratesacr'
$storageName = Resolve-ResourceName 'Microsoft.Storage/storageAccounts' 'fdratesst'
$aiServicesName = Resolve-ResourceName 'Microsoft.CognitiveServices/accounts' 'fdrates-ai-'
$docIntelligenceName = Resolve-ResourceName 'Microsoft.CognitiveServices/accounts' 'fdrates-di-'

Write-Host "ACR:               $acrName"
Write-Host "Storage:           $storageName"
Write-Host "AI Services:       $aiServicesName"
Write-Host "Doc Intelligence:  $docIntelligenceName"

Step '2/8 Validate that the target backend image tag exists in ACR'
$tagExists = az acr repository show-tags --name $acrName --repository fdrates-backend --query "contains(@, '$ImageTag')" -o tsv
if ($tagExists -ne 'true') {
    throw "Image tag 'fdrates-backend:${ImageTag}' not found in ACR '$acrName'."
}

Step '3/8 Build and push frontend image to ACR (React + Nginx)'
az acr build `
    --registry $acrName `
    --image "fdrates-frontend:$FrontendImageTag" `
    --image "fdrates-frontend:latest" `
    --file (Join-Path $repoRoot 'frontend/Dockerfile') `
    (Join-Path $repoRoot 'frontend') | Out-Null

$frontendImageRef = "$(az acr show --name $acrName --resource-group $ResourceGroup --query loginServer -o tsv)/fdrates-frontend:$FrontendImageTag"

if (-not $SkipWhatIf) {
    Step '4/8 What-if preview for additive AKS + Static Web App deployment'
    az deployment group what-if `
        --resource-group $ResourceGroup `
        --template-file (Join-Path $repoRoot 'infra/aks-swa.bicep') `
        --parameters baseName=$BaseName location=$Location swaLocation=$SwaLocation `
                     existingAcrName=$acrName existingStorageAccountName=$storageName `
                     existingAiServicesName=$aiServicesName existingDocIntelligenceName=$docIntelligenceName `
                     k8sNamespace=$Namespace k8sServiceAccountName=$ServiceAccountName `
                     enableWorkloadIdentity=$workloadIdentityEnabled
}

Step '5/8 Deploy AKS + SWA + workload identity (new resources only)'
$deploymentName = "aks-swa-$(Get-Date -Format 'yyyyMMddHHmmss')"
$outputs = az deployment group create `
    --name $deploymentName `
    --resource-group $ResourceGroup `
    --template-file (Join-Path $repoRoot 'infra/aks-swa.bicep') `
    --parameters baseName=$BaseName location=$Location swaLocation=$SwaLocation `
                 existingAcrName=$acrName existingStorageAccountName=$storageName `
                 existingAiServicesName=$aiServicesName existingDocIntelligenceName=$docIntelligenceName `
                 k8sNamespace=$Namespace k8sServiceAccountName=$ServiceAccountName `
                 enableWorkloadIdentity=$workloadIdentityEnabled `
    --query properties.outputs -o json | ConvertFrom-Json

$aksName = Assert-NotEmpty $outputs.aksName.value 'AKS name'
$swaName = Assert-NotEmpty $outputs.staticWebAppName.value 'Static Web App name'
$swaHost = Assert-NotEmpty $outputs.staticWebAppDefaultHostname.value 'Static Web App hostname'
$clientId = ''
if ($outputs.PSObject.Properties.Name -contains 'workloadIdentityClientId') {
    $clientId = ($outputs.workloadIdentityClientId.value | Out-String).Trim()
}

if ($workloadIdentityEnabled -and [string]::IsNullOrWhiteSpace($clientId)) {
    throw 'Workload identity was enabled, but no workload identity client ID was returned.'
}
$projectEndpoint = Assert-NotEmpty $outputs.projectEndpoint.value 'Project endpoint'
$docEndpoint = Assert-NotEmpty $outputs.docIntelligenceEndpoint.value 'Document Intelligence endpoint'
$imageRepository = Assert-NotEmpty $outputs.imageRepository.value 'Image repository'

$imageRef = "${imageRepository}:$ImageTag"
# Frontend is served from AKS/Nginx and proxies same-origin /api calls,
# so backend CORS can be permissive without exposing backend directly.
$allowedOrigins = '"*"'

Write-Host "AKS:               $aksName"
Write-Host "SWA:               $swaName ($allowedOrigins)"
Write-Host "Backend image:     $imageRef"
Write-Host "Frontend image:    $frontendImageRef"

Step '6/8 Connect kubectl context and apply manifests'
az aks get-credentials --resource-group $ResourceGroup --name $aksName --overwrite-existing | Out-Null

$tmpDir = Join-Path $repoRoot '.tmp-aks'
New-Item -ItemType Directory -Force -Path $tmpDir | Out-Null

$serviceAccountOut = Join-Path $tmpDir 'serviceaccount.generated.yaml'
$deploymentOut = Join-Path $tmpDir 'deployment.generated.yaml'
$backendServiceOut = Join-Path $tmpDir 'service.generated.yaml'
$frontendDeploymentOut = Join-Path $tmpDir 'frontend-deployment.generated.yaml'
$frontendServiceOut = Join-Path $tmpDir 'frontend-service.generated.yaml'

$workloadAnnotation = '    # workload identity disabled'
$clientIdEnvBlock = '        # workload identity disabled'
if ($workloadIdentityEnabled) {
    $workloadAnnotation = "    azure.workload.identity/client-id: $clientId"
    $clientIdEnvBlock = "        - name: AZURE_CLIENT_ID`n          value: $clientId"
}

Render-Template (Join-Path $repoRoot 'infra/k8s/serviceaccount.yaml') $serviceAccountOut @{
    '__NAMESPACE__' = $Namespace
    '__SERVICE_ACCOUNT_NAME__' = $ServiceAccountName
    '__WORKLOAD_IDENTITY_ANNOTATION__' = $workloadAnnotation
}

Render-Template (Join-Path $repoRoot 'infra/k8s/deployment.yaml') $deploymentOut @{
    '__NAMESPACE__' = $Namespace
    '__SERVICE_ACCOUNT_NAME__' = $ServiceAccountName
    '__AZURE_CLIENT_ID_ENV__' = $clientIdEnvBlock
    '__IMAGE__' = $imageRef
    '__STORAGE_ACCOUNT_NAME__' = $storageName
    '__PROJECT_ENDPOINT__' = $projectEndpoint
    '__DOC_INTELLIGENCE_ENDPOINT__' = $docEndpoint
    '__ALLOWED_ORIGINS__' = $allowedOrigins
}

Render-Template (Join-Path $repoRoot 'infra/k8s/service.yaml') $backendServiceOut @{
    '__NAMESPACE__' = $Namespace
}

Render-Template (Join-Path $repoRoot 'infra/k8s/frontend-deployment.yaml') $frontendDeploymentOut @{
    '__NAMESPACE__' = $Namespace
    '__FRONTEND_IMAGE__' = $frontendImageRef
}

Render-Template (Join-Path $repoRoot 'infra/k8s/frontend-service.yaml') $frontendServiceOut @{
    '__NAMESPACE__' = $Namespace
}

kubectl apply -f $serviceAccountOut
kubectl apply -f $deploymentOut
kubectl apply -f $backendServiceOut
kubectl apply -f $frontendDeploymentOut
kubectl apply -f $frontendServiceOut

Step '7/8 Wait for rollout and external endpoint'
kubectl -n $Namespace rollout status deployment/fd-rates-api --timeout=600s
kubectl -n $Namespace rollout status deployment/fd-rates-web --timeout=600s

$frontendExternalIp = ''
for ($i = 0; $i -lt 30; $i++) {
    $frontendExternalIp = kubectl -n $Namespace get svc fd-rates-web -o jsonpath='{.status.loadBalancer.ingress[0].ip}'
    if (-not [string]::IsNullOrWhiteSpace($frontendExternalIp)) {
        break
    }

    Start-Sleep -Seconds 10
}

if ([string]::IsNullOrWhiteSpace($frontendExternalIp)) {
    throw 'Frontend LoadBalancer external IP was not assigned within timeout.'
}

$frontendUrl = "http://$frontendExternalIp"
$apiUrl = "${frontendUrl}/api"

Step '8/8 Output URLs and test commands'
Write-Host ''
Write-Host 'AKS deployment complete (frontend + backend in cluster).' -ForegroundColor Green
Write-Host "  AKS Cluster:   $aksName"
Write-Host "  Frontend URL:  $frontendUrl"
Write-Host "  API Base URL:  $apiUrl"
Write-Host ''
Write-Host 'Quick tests:'
Write-Host "  1) Open $frontendUrl"
Write-Host "  2) Open $apiUrl/urls"
Write-Host "  3) kubectl -n $Namespace get pods,svc"

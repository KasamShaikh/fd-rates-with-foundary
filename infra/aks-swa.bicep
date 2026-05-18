@description('Base name for AKS+SWA resources. Keep different from existing stacks to avoid collisions.')
param baseName string = 'fdratesaks'

@description('Deployment region for AKS and managed identity.')
param location string = resourceGroup().location

@description('Location for Static Web App (Free SKU availability differs by region).')
param swaLocation string = 'eastasia'

@description('Existing ACR name in this resource group (used for image pulls).')
param existingAcrName string

@description('Existing Storage Account name in this resource group.')
param existingStorageAccountName string

@description('Existing Azure AI Services account name in this resource group.')
param existingAiServicesName string

@description('Existing Azure AI Foundry project name in the AI Services account.')
param aiProjectName string = 'fdrates-project'

@description('Existing Document Intelligence account name in this resource group.')
param existingDocIntelligenceName string

@description('AKS Kubernetes version. Keep empty to use platform default.')
param kubernetesVersion string = ''

@description('Initial AKS node count for the system node pool.')
param nodeCount int = 1

@description('AKS node VM size.')
param vmSize string = 'Standard_D2s_v5'

@description('Service account name used by workload identity in AKS.')
param k8sServiceAccountName string = 'fd-rates-sa'

@description('Namespace used by workload identity in AKS.')
param k8sNamespace string = 'fd-rates-aks'

@description('Enable AKS workload identity + federated credentials. Set false when tenant does not support federated identity credentials.')
param enableWorkloadIdentity bool = false

var uniqueSuffix = uniqueString(resourceGroup().id, baseName)
var aksName = '${baseName}-aks-${uniqueSuffix}'
var dnsPrefix = '${baseName}-${substring(uniqueSuffix, 0, 8)}'
var identityName = '${baseName}-uami-${uniqueSuffix}'
var staticWebAppName = '${baseName}-web-${uniqueSuffix}'

resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' existing = {
  name: existingAcrName
}

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: existingStorageAccountName
}

resource aiServices 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' existing = {
  name: existingAiServicesName
}

resource docIntelligence 'Microsoft.CognitiveServices/accounts@2024-10-01' existing = {
  name: existingDocIntelligenceName
}

resource aks 'Microsoft.ContainerService/managedClusters@2024-02-01' = {
  name: aksName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  sku: {
    name: 'Base'
    tier: 'Standard'
  }
  properties: {
    dnsPrefix: dnsPrefix
    kubernetesVersion: empty(kubernetesVersion) ? null : kubernetesVersion
    oidcIssuerProfile: enableWorkloadIdentity ? {
      enabled: true
    } : null
    securityProfile: enableWorkloadIdentity ? {
      workloadIdentity: {
        enabled: true
      }
    } : null
    agentPoolProfiles: [
      {
        name: 'systempool'
        mode: 'System'
        count: nodeCount
        vmSize: vmSize
        osType: 'Linux'
        type: 'VirtualMachineScaleSets'
        enableAutoScaling: false
      }
    ]
    networkProfile: {
      loadBalancerSku: 'standard'
      networkPlugin: 'azure'
      networkPolicy: 'azure'
      outboundType: 'loadBalancer'
    }
  }
}

resource workloadIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = if (enableWorkloadIdentity) {
  name: identityName
  location: location
}

resource federatedIdentityCredential 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31-preview' = if (enableWorkloadIdentity) {
  parent: workloadIdentity
  name: 'fd-rates-aks-fic'
  properties: {
    audiences: [
      'api://AzureADTokenExchange'
    ]
    issuer: aks.properties.oidcIssuerProfile.issuerURL
    subject: 'system:serviceaccount:${k8sNamespace}:${k8sServiceAccountName}'
  }
}

// AcrPull for AKS kubelet identity so pods can pull from the existing ACR.
resource aksKubeletAcrPullRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, aks.id, 'aks-kubelet-acrpull')
  scope: acr
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
    principalId: aks.properties.identityProfile.kubeletidentity.objectId
    principalType: 'ServicePrincipal'
  }
}

// Storage Blob Data Contributor for workload identity used by backend pods.
resource workloadIdentityStorageRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (enableWorkloadIdentity) {
  name: guid(storageAccount.id, workloadIdentity.id, 'storage-blob-contributor')
  scope: storageAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
    principalId: workloadIdentity!.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Foundry/OpenAI roles for backend pods.
resource workloadIdentityAiOpenAIRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (enableWorkloadIdentity) {
  name: guid(aiServices.id, workloadIdentity.id, 'ai-openai-user')
  scope: aiServices
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')
    principalId: workloadIdentity!.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource workloadIdentityAiDeveloperRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (enableWorkloadIdentity) {
  name: guid(aiServices.id, workloadIdentity.id, 'ai-developer')
  scope: aiServices
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '64702f94-c441-49e6-a78b-ef80e0188fee')
    principalId: workloadIdentity!.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource workloadIdentityAiUserRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (enableWorkloadIdentity) {
  name: guid(aiServices.id, workloadIdentity.id, 'ai-user')
  scope: aiServices
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '53ca6127-db72-4b80-b1b0-d745d6d5456d')
    principalId: workloadIdentity!.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Cognitive Services User on Document Intelligence.
resource workloadIdentityDocIntelligenceRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (enableWorkloadIdentity) {
  name: guid(docIntelligence.id, workloadIdentity.id, 'doc-intelligence-user')
  scope: docIntelligence
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'a97b65f3-24c7-4388-baec-2e87135dc908')
    principalId: workloadIdentity!.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource staticWebApp 'Microsoft.Web/staticSites@2023-12-01' = {
  name: staticWebAppName
  location: swaLocation
  sku: {
    name: 'Free'
    tier: 'Free'
  }
  properties: {
    provider: 'None'
  }
}

output aksName string = aks.name
output aksFqdn string = aks.properties.fqdn
output oidcIssuerUrl string = enableWorkloadIdentity ? aks.properties.oidcIssuerProfile.issuerURL : ''
output workloadIdentityClientId string = enableWorkloadIdentity ? workloadIdentity!.properties.clientId : ''
output workloadIdentityPrincipalId string = enableWorkloadIdentity ? workloadIdentity!.properties.principalId : ''
output staticWebAppName string = staticWebApp.name
output staticWebAppDefaultHostname string = staticWebApp.properties.defaultHostname
output imageRepository string = '${acr.properties.loginServer}/fdrates-backend'
output projectEndpoint string = 'https://${aiServices.name}.services.ai.azure.com/api/projects/${aiProjectName}'
output docIntelligenceEndpoint string = docIntelligence.properties.endpoint

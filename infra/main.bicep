// ============================================================
// FD Rate Aggregator — Cloud infrastructure
//
// Compute:  Azure Container Apps (Consumption, scale-to-zero)
// Image:    Azure Container Registry (Basic SKU)
// Frontend: Azure Static Web Apps (Free SKU)
// AI:       Azure AI Services + AI Foundry project + Doc Intelligence + Bing
// Storage:  Azure Storage (Blob) — fd-rates container
// Telemetry: Application Insights + Log Analytics
//
// All compute uses system-assigned managed identity.
// All Azure SDK calls in the app use DefaultAzureCredential (no secrets).
// ============================================================

@description('Base name for all resources')
param baseName string = 'fdrates'

@description('Azure region for deployment')
param location string = 'centralindia'

@description('AI Foundry / Cognitive Services region')
param aiLocation string = 'swedencentral'

@description('Origin allowed by the backend CORS policy. Set to the deployed Static Web App URL once known.')
param allowedOrigin string = 'http://localhost:3000'

@description('Container image tag for the backend. Override per deployment (e.g. git SHA).')
param backendImageTag string = 'latest'

@description('Placeholder image used while ACR is empty (first-time bootstrap).')
param bootstrapImage string = 'mcr.microsoft.com/k8se/quickstart:latest'

@description('Set true once the backend image has been pushed to ACR. Switches the Container App from the bootstrap image to <acr>/fdrates-backend:<tag>.')
param useAcrImage bool = false

@description('Region for the Static Web App (Free SKU is not available in every region).')
param swaLocation string = 'eastasia'

@description('Client IPs allowed to access the storage account data plane. Empty = allow all.')
param allowedClientIpAddresses array = []

@description('Also deploy backend on an App Service Plan (Linux Web App for Containers) alongside Container Apps.')
param deployAppService bool = false

@description('App Service Plan SKU when deployAppService=true (e.g. B1, B2, P0v3, P1v3).')
param appServicePlanSku string = 'B1'

// ============================================================
// Variables
// ============================================================
var uniqueSuffix = uniqueString(resourceGroup().id)
var storageAccountName = toLower('${baseName}st${uniqueSuffix}')
var acrName = toLower('${baseName}acr${uniqueSuffix}')
var acaEnvName = '${baseName}-env-${uniqueSuffix}'
var containerAppName = '${baseName}-api'
var swaName = '${baseName}-web-${uniqueSuffix}'
var appInsightsName = '${baseName}-insights-${uniqueSuffix}'
var logAnalyticsName = '${baseName}-logs-${uniqueSuffix}'
var aiServicesName = '${baseName}-ai-${uniqueSuffix}'
var aiProjectName = '${baseName}-project'
var bingSearchName = '${baseName}-bing-${uniqueSuffix}'
var docIntelligenceName = '${baseName}-di-${uniqueSuffix}'
var blobContainerName = 'fd-rates'
var appServicePlanName = '${baseName}-plan-${uniqueSuffix}'
var webAppName = '${baseName}-web-app-${uniqueSuffix}'

var backendImage = useAcrImage ? '${acr.properties.loginServer}/fdrates-backend:${backendImageTag}' : bootstrapImage

// ============================================================
// Storage Account + Blob Container
// ============================================================
resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    supportsHttpsTrafficOnly: true
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: empty(allowedClientIpAddresses) ? 'Allow' : 'Deny'
      ipRules: [for ip in allowedClientIpAddresses: {
        value: ip
        action: 'Allow'
      }]
      virtualNetworkRules: []
    }
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storageAccount
  name: 'default'
}

resource blobContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: blobContainerName
  properties: { publicAccess: 'None' }
}

// ============================================================
// Log Analytics + Application Insights
// ============================================================
resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsName
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
  }
}

// ============================================================
// Azure Container Registry (Basic SKU)
// ============================================================
resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: acrName
  location: location
  sku: { name: 'Basic' }
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: 'Enabled'
  }
}

// ============================================================
// Container Apps Environment (Consumption)
// ============================================================
resource acaEnv 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: acaEnvName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
  }
}

// ============================================================
// Container App (backend) — scale-to-zero
// ============================================================
resource containerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: containerAppName
  location: location
  identity: { type: 'SystemAssigned' }
  properties: {
    managedEnvironmentId: acaEnv.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
        corsPolicy: {
          allowedOrigins: [ allowedOrigin ]
          allowedMethods: [ 'GET', 'POST', 'DELETE', 'OPTIONS' ]
          allowedHeaders: [ '*' ]
          allowCredentials: false
        }
      }
      registries: useAcrImage ? [
        {
          server: acr.properties.loginServer
          identity: 'system'
        }
      ] : []
    }
    template: {
      containers: [
        {
          name: 'backend'
          image: backendImage
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            { name: 'STORAGE_ACCOUNT_NAME', value: storageAccount.name }
            { name: 'BLOB_CONTAINER_NAME', value: blobContainerName }
            { name: 'PROJECT_ENDPOINT', value: 'https://${aiServices.name}.services.ai.azure.com/api/projects/${aiProject.name}' }
            { name: 'MODEL_DEPLOYMENT_NAME', value: 'gpt-4.1' }
            { name: 'BING_CONNECTION_NAME', value: 'bing-grounding-connection' }
            { name: 'DOC_INTELLIGENCE_ENDPOINT', value: docIntelligence.properties.endpoint }
            { name: 'ALLOWED_ORIGINS', value: allowedOrigin }
            { name: 'LOCAL_RESULTS_ENABLED', value: 'false' }
            { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
          ]
        }
      ]
      scale: {
        minReplicas: 0
        maxReplicas: 2
        rules: [
          {
            name: 'http-rule'
            http: { metadata: { concurrentRequests: '10' } }
          }
        ]
      }
    }
  }
}

// AcrPull role for the Container App's managed identity
resource acrPullRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, containerApp.id, '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  scope: acr
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d') // AcrPull
    principalId: containerApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// Storage Blob Data Contributor role for the Container App
resource storageBlobRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storageAccount.id, containerApp.id, 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
  scope: storageAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe') // Storage Blob Data Contributor
    principalId: containerApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// ============================================================
// Static Web App (frontend, Free tier)
// ============================================================
resource staticWebApp 'Microsoft.Web/staticSites@2023-12-01' = {
  name: swaName
  location: swaLocation
  sku: {
    name: 'Free'
    tier: 'Free'
  }
  properties: {
    // Built/deployed via GitHub Actions or `swa deploy`. No repo bound here.
    provider: 'None'
  }
}

// ============================================================
// Azure AI Services (Foundry resource)
// ============================================================
resource aiServices 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' = {
  name: aiServicesName
  location: aiLocation
  kind: 'AIServices'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  properties: {
    customSubDomainName: aiServicesName
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: false
    allowProjectManagement: true
  }
}

// ============================================================
// AI Foundry Project
// ============================================================
resource aiProject 'Microsoft.CognitiveServices/accounts/projects@2025-04-01-preview' = {
  parent: aiServices
  name: aiProjectName
  location: aiLocation
  kind: 'AIServices'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  properties: {}
}

// Cognitive Services OpenAI User role for Container App
resource aiServicesOpenAIRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(aiServices.id, containerApp.id, '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')
  scope: aiServices
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd') // Cognitive Services OpenAI User
    principalId: containerApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// Azure AI Developer role for Container App (agent operations)
resource aiUserRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(aiServices.id, containerApp.id, '64702f94-c441-49e6-a78b-ef80e0188fee')
  scope: aiServices
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '64702f94-c441-49e6-a78b-ef80e0188fee') // Azure AI Developer
    principalId: containerApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// Azure AI User role for Container App (Foundry agents data-plane: agents/write etc.)
resource aiAiUserRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(aiServices.id, containerApp.id, '53ca6127-db72-4b80-b1b0-d745d6d5456d')
  scope: aiServices
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '53ca6127-db72-4b80-b1b0-d745d6d5456d') // Azure AI User
    principalId: containerApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// ============================================================
// Bing Search Resource (Grounding)
// ============================================================
resource bingSearch 'Microsoft.Bing/accounts@2020-06-10' = {
  name: bingSearchName
  location: 'global'
  kind: 'Bing.Grounding'
  sku: { name: 'G1' }
}

// ============================================================
// Azure AI Document Intelligence
// ============================================================
resource docIntelligence 'Microsoft.CognitiveServices/accounts@2024-10-01' = {
  name: docIntelligenceName
  location: location
  kind: 'FormRecognizer'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  properties: {
    customSubDomainName: docIntelligenceName
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: false
  }
}

// Cognitive Services User role for Container App on Document Intelligence
resource docIntelligenceRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(docIntelligence.id, containerApp.id, 'a97b65f3-24c7-4388-baec-2e87135dc908')
  scope: docIntelligence
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'a97b65f3-24c7-4388-baec-2e87135dc908') // Cognitive Services User
    principalId: containerApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// ============================================================
// (Optional) App Service Plan + Linux Web App for Containers
// Uses the same ACR image as the Container App. Enabled by deployAppService=true.
// ============================================================
resource appServicePlan 'Microsoft.Web/serverfarms@2023-12-01' = if (deployAppService) {
  name: appServicePlanName
  location: location
  kind: 'linux'
  sku: { name: appServicePlanSku }
  properties: {
    reserved: true // required for Linux
  }
}

resource webApp 'Microsoft.Web/sites@2023-12-01' = if (deployAppService) {
  name: webAppName
  location: location
  kind: 'app,linux,container'
  identity: { type: 'SystemAssigned' }
  properties: {
    serverFarmId: appServicePlan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: useAcrImage ? 'DOCKER|${acr.properties.loginServer}/fdrates-backend:${backendImageTag}' : 'DOCKER|${bootstrapImage}'
      acrUseManagedIdentityCreds: useAcrImage
      alwaysOn: appServicePlanSku != 'F1' && appServicePlanSku != 'D1'
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      http20Enabled: true
      cors: {
        allowedOrigins: [ allowedOrigin ]
        supportCredentials: false
      }
      appSettings: [
        { name: 'WEBSITES_PORT', value: '8000' }
        { name: 'WEBSITES_ENABLE_APP_SERVICE_STORAGE', value: 'false' }
        { name: 'STORAGE_ACCOUNT_NAME', value: storageAccount.name }
        { name: 'BLOB_CONTAINER_NAME', value: blobContainerName }
        { name: 'PROJECT_ENDPOINT', value: 'https://${aiServices.name}.services.ai.azure.com/api/projects/${aiProject.name}' }
        { name: 'MODEL_DEPLOYMENT_NAME', value: 'gpt-4.1' }
        { name: 'BING_CONNECTION_NAME', value: 'bing-grounding-connection' }
        { name: 'DOC_INTELLIGENCE_ENDPOINT', value: docIntelligence.properties.endpoint }
        { name: 'ALLOWED_ORIGINS', value: allowedOrigin }
        { name: 'LOCAL_RESULTS_ENABLED', value: 'false' }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
      ]
    }
  }
}

// AcrPull for Web App MI
resource webAppAcrPullRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployAppService) {
  name: guid(acr.id, webAppName, '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  scope: acr
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
    principalId: webApp!.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// Storage Blob Data Contributor for Web App MI
resource webAppStorageRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployAppService) {
  name: guid(storageAccount.id, webAppName, 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
  scope: storageAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
    principalId: webApp!.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// Cognitive Services OpenAI User for Web App MI
resource webAppAiOpenAIRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployAppService) {
  name: guid(aiServices.id, webAppName, '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')
  scope: aiServices
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')
    principalId: webApp!.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// Azure AI Developer for Web App MI
resource webAppAiDeveloperRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployAppService) {
  name: guid(aiServices.id, webAppName, '64702f94-c441-49e6-a78b-ef80e0188fee')
  scope: aiServices
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '64702f94-c441-49e6-a78b-ef80e0188fee')
    principalId: webApp!.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// Azure AI User for Web App MI (Foundry agents data plane)
resource webAppAiUserRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployAppService) {
  name: guid(aiServices.id, webAppName, '53ca6127-db72-4b80-b1b0-d745d6d5456d')
  scope: aiServices
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '53ca6127-db72-4b80-b1b0-d745d6d5456d')
    principalId: webApp!.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// Cognitive Services User for Web App MI on Document Intelligence
resource webAppDocIntelligenceRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployAppService) {
  name: guid(docIntelligence.id, webAppName, 'a97b65f3-24c7-4388-baec-2e87135dc908')
  scope: docIntelligence
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'a97b65f3-24c7-4388-baec-2e87135dc908')
    principalId: webApp!.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// ============================================================
// Outputs
// ============================================================
output storageAccountName string = storageAccount.name
output containerRegistryName string = acr.name
output containerRegistryLoginServer string = acr.properties.loginServer
output containerAppName string = containerApp.name
output containerAppFqdn string = containerApp.properties.configuration.ingress.fqdn
output staticWebAppName string = staticWebApp.name
output staticWebAppDefaultHostname string = staticWebApp.properties.defaultHostname
output aiServicesName string = aiServices.name
output aiProjectName string = aiProject.name
output projectEndpoint string = 'https://${aiServices.name}.services.ai.azure.com/api/projects/${aiProject.name}'
output bingSearchName string = bingSearch.name
output docIntelligenceName string = docIntelligence.name
output docIntelligenceEndpoint string = docIntelligence.properties.endpoint
output containerAppPrincipalId string = containerApp.identity.principalId
output webAppName string = deployAppService ? webApp!.name : ''
output webAppDefaultHostname string = deployAppService ? webApp!.properties.defaultHostName : ''
output webAppPrincipalId string = deployAppService ? webApp!.identity.principalId : ''

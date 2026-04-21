param aiServicesName string
param projectName string

resource aiServices 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' existing = {
  name: aiServicesName
}

resource aiProject 'Microsoft.CognitiveServices/accounts/projects@2025-04-01-preview' = {
  parent: aiServices
  name: projectName
  location: resourceGroup().location
  properties: {}
}

output projectEndpoint string = 'https://${aiServicesName}.services.ai.azure.com/api/projects/${projectName}'

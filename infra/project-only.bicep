// project-only.bicep
//
// Convenience template for adding a new AI Foundry project to an EXISTING
// AI Services account. Used when the parent account is already provisioned
// (e.g., shared `prj-web-tools` setup) and only a child project is needed.
//
// Inputs : aiServicesName — existing CognitiveServices/accounts resource name
//          projectName    — new child project name
// Output : projectEndpoint — https URL to feed into PROJECT_ENDPOINT env var

param aiServicesName string
param projectName string

// Reference the pre-existing AI Services (Foundry) account in this RG.
resource aiServices 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' existing = {
  name: aiServicesName
}

// Create the child project under the existing account.
resource aiProject 'Microsoft.CognitiveServices/accounts/projects@2025-04-01-preview' = {
  parent: aiServices
  name: projectName
  location: resourceGroup().location
  properties: {}
}

// Endpoint format expected by azure-ai-agents / azure-ai-projects SDKs.
output projectEndpoint string = 'https://${aiServicesName}.services.ai.azure.com/api/projects/${projectName}'

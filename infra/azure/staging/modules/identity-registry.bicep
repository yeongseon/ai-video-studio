param location string
param namePrefix string
param logAnalyticsWorkspaceId string
param tags object

var identityNames = ['studio', 'api', 'worker', 'beat', 'migration', 'publisher']
var repositoryReaderRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b93aa761-3e63-49ed-ac28-beffa264f7ac')
var repositoryWriterRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '2a1e307c-b015-4ebd-883e-5b7698a07328')
var identityRepositories = ['studio-web', 'api', 'worker', 'worker', 'api']

resource identities 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = [for identityName in identityNames: {
  name: '${namePrefix}-stg-${identityName}-id'
  location: location
  tags: tags
}]

resource registry 'Microsoft.ContainerRegistry/registries@2025-11-01' = {
  name: '${namePrefix}stgacr'
  location: location
  tags: tags
  sku: { name: 'Premium' }
  properties: {
    adminUserEnabled: false
    anonymousPullEnabled: false
    publicNetworkAccess: 'Disabled'
    dataEndpointEnabled: false
    networkRuleBypassOptions: 'None'
    roleAssignmentMode: 'AbacRepositoryPermissions'
    policies: {
      exportPolicy: { status: 'disabled' }
      retentionPolicy: { days: 7, status: 'enabled' }
      quarantinePolicy: { status: 'disabled' }
      trustPolicy: { type: 'Notary', status: 'disabled' }
    }
  }
}

resource pullAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for (repositoryName, index) in identityRepositories: {
  name: guid(registry.id, identities[index].id, repositoryReaderRoleId)
  scope: registry
  properties: {
    principalId: identities[index].properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: repositoryReaderRoleId
    conditionVersion: '2.0'
    condition: '((!(ActionMatches{\'Microsoft.ContainerRegistry/registries/repositories/content/read\'}) AND !(ActionMatches{\'Microsoft.ContainerRegistry/registries/repositories/metadata/read\'})) OR @Request[Microsoft.ContainerRegistry/registries/repositories:name] StringEqualsIgnoreCase \'${repositoryName}\')'
  }
}]

resource publisherAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, identities[5].id, repositoryWriterRoleId)
  scope: registry
  properties: {
    principalId: identities[5].properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: repositoryWriterRoleId
    conditionVersion: '2.0'
    condition: '((!(ActionMatches{\'Microsoft.ContainerRegistry/registries/repositories/content/read\'}) AND !(ActionMatches{\'Microsoft.ContainerRegistry/registries/repositories/metadata/read\'})) OR (@Request[Microsoft.ContainerRegistry/registries/repositories:name] StringEqualsIgnoreCase \'api\' OR @Request[Microsoft.ContainerRegistry/registries/repositories:name] StringEqualsIgnoreCase \'worker\' OR @Request[Microsoft.ContainerRegistry/registries/repositories:name] StringEqualsIgnoreCase \'studio-web\'))'
  }
}

resource registryDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'send-to-log-analytics'
  scope: registry
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      { category: 'ContainerRegistryLoginEvents', enabled: true }
      { category: 'ContainerRegistryRepositoryEvents', enabled: true }
    ]
    metrics: [{ category: 'AllMetrics', enabled: true }]
  }
}

output studioPrincipalId string = identities[0].properties.principalId
output apiPrincipalId string = identities[1].properties.principalId
output workerPrincipalId string = identities[2].properties.principalId
output beatPrincipalId string = identities[3].properties.principalId
output migrationPrincipalId string = identities[4].properties.principalId
output publisherPrincipalId string = identities[5].properties.principalId
output registryId string = registry.id
output registryLoginServer string = registry.properties.loginServer

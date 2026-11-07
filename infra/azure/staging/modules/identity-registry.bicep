param location string
param namePrefix string
param tags object

var identityNames = ['studio', 'api', 'worker', 'beat', 'migration', 'publisher']
var repositoryReaderRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b93aa761-3e63-49ed-ac28-beffa264f7ac')
var repositoryWriterRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '2a1e307c-b015-4ebd-883e-5b7698d2c559')
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
    condition: '@Resource[Microsoft.ContainerRegistry/registries/repositories:name] StringEquals \'${repositoryName}\''
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
    condition: '(@Resource[Microsoft.ContainerRegistry/registries/repositories:name] StringEquals \'api\') OR (@Resource[Microsoft.ContainerRegistry/registries/repositories:name] StringEquals \'worker\') OR (@Resource[Microsoft.ContainerRegistry/registries/repositories:name] StringEquals \'studio-web\')'
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

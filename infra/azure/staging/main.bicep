targetScope = 'resourceGroup'

@description('Azure region selected and priced by the staging operator before deployment.')
param location string

@description('Globally unique lowercase prefix selected by the staging operator.')
@minLength(5)
@maxLength(12)
param namePrefix string

@description('Object ID of the approved PostgreSQL Entra administrator group.')
param postgresEntraAdministratorObjectId string

@description('Display name of the approved PostgreSQL Entra administrator group.')
param postgresEntraAdministratorName string

@allowed(['Disabled', 'SameZone', 'ZoneRedundant'])
param postgresHighAvailabilityMode string = 'Disabled'

param postgresSkuName string = 'Standard_B1ms'
param postgresSkuTier string = 'Burstable'
param postgresBackupRetentionDays int = 7
param postgresGeoRedundantBackup string = 'Disabled'
param redisCapacity int = 1
param artifactRetentionDays int = 90
param logRetentionDays int = 30

var tags = {
  environment: 'staging'
  managedBy: 'bicep'
  project: 'short-form-studio'
  workload: 'creator'
}

module network 'modules/network.bicep' = {
  name: 'network'
  params: {
    location: location
    namePrefix: namePrefix
    tags: tags
  }
}

module observability 'modules/observability.bicep' = {
  name: 'observability'
  params: {
    location: location
    namePrefix: namePrefix
    infrastructureSubnetId: network.outputs.infrastructureSubnetId
    logRetentionDays: logRetentionDays
    tags: tags
  }
}

module identityRegistry 'modules/identity-registry.bicep' = {
  name: 'identity-registry'
  params: {
    location: location
    namePrefix: namePrefix
    logAnalyticsWorkspaceId: observability.outputs.logAnalyticsWorkspaceId
    tags: tags
  }
}

module data 'modules/data.bicep' = {
  name: 'data'
  params: {
    location: location
    namePrefix: namePrefix
    postgresSubnetId: network.outputs.postgresSubnetId
    postgresPrivateDnsZoneId: network.outputs.postgresPrivateDnsZoneId
    postgresEntraAdministratorObjectId: postgresEntraAdministratorObjectId
    postgresEntraAdministratorName: postgresEntraAdministratorName
    postgresHighAvailabilityMode: postgresHighAvailabilityMode
    postgresSkuName: postgresSkuName
    postgresSkuTier: postgresSkuTier
    postgresBackupRetentionDays: postgresBackupRetentionDays
    postgresGeoRedundantBackup: postgresGeoRedundantBackup
    redisCapacity: redisCapacity
    artifactRetentionDays: artifactRetentionDays
    logAnalyticsWorkspaceId: observability.outputs.logAnalyticsWorkspaceId
    apiPrincipalId: identityRegistry.outputs.apiPrincipalId
    workerPrincipalId: identityRegistry.outputs.workerPrincipalId
    tags: tags
  }
}

module privateEndpoints 'modules/private-endpoints.bicep' = {
  name: 'private-endpoints'
  params: {
    location: location
    namePrefix: namePrefix
    privateEndpointSubnetId: network.outputs.privateEndpointSubnetId
    blobPrivateDnsZoneId: network.outputs.blobPrivateDnsZoneId
    keyVaultPrivateDnsZoneId: network.outputs.keyVaultPrivateDnsZoneId
    registryPrivateDnsZoneId: network.outputs.registryPrivateDnsZoneId
    redisPrivateDnsZoneId: network.outputs.redisPrivateDnsZoneId
    storageAccountId: data.outputs.storageAccountId
    keyVaultId: data.outputs.keyVaultId
    registryId: identityRegistry.outputs.registryId
    redisId: data.outputs.redisId
    tags: tags
  }
}

output containerAppsEnvironmentId string = observability.outputs.containerAppsEnvironmentId
output vaultUri string = data.outputs.vaultUri
output storageBlobEndpoint string = data.outputs.storageBlobEndpoint
output registryLoginServer string = identityRegistry.outputs.registryLoginServer
output postgresFqdn string = data.outputs.postgresFqdn
output redisHostname string = data.outputs.redisHostname

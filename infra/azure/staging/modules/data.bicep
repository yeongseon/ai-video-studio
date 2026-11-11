param location string
param namePrefix string
param postgresSubnetId string
param postgresPrivateDnsZoneId string
param postgresEntraAdministratorObjectId string
param postgresEntraAdministratorName string
param postgresHighAvailabilityMode string
param postgresSkuName string
param postgresSkuTier string
param postgresBackupRetentionDays int
param postgresGeoRedundantBackup string
param redisCapacity int
param artifactRetentionDays int
param logAnalyticsWorkspaceId string
param apiPrincipalId string
param workerPrincipalId string
param tags object

var blobContributorRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
var blobPrincipals = [apiPrincipalId, workerPrincipalId]

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: '${namePrefix}-stg-kv'
  location: location
  tags: tags
  properties: {
    tenantId: tenant().tenantId
    sku: { family: 'A', name: 'standard' }
    accessPolicies: []
    enableRbacAuthorization: true
    enablePurgeProtection: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    enabledForDeployment: false
    enabledForDiskEncryption: false
    enabledForTemplateDeployment: false
    publicNetworkAccess: 'Disabled'
    networkAcls: { bypass: 'None', defaultAction: 'Deny' }
  }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: '${namePrefix}stgartifacts'
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: { name: 'Standard_ZRS' }
  properties: {
    allowBlobPublicAccess: false
    allowCrossTenantReplication: false
    allowSharedKeyAccess: false
    defaultToOAuthAuthentication: true
    minimumTlsVersion: 'TLS1_2'
    publicNetworkAccess: 'Disabled'
    supportsHttpsTrafficOnly: true
    networkAcls: { bypass: 'None', defaultAction: 'Deny' }
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    isVersioningEnabled: true
    deleteRetentionPolicy: { enabled: true, days: 14 }
    containerDeleteRetentionPolicy: { enabled: true, days: 14 }
  }
}

resource artifacts 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'artifacts'
  properties: { publicAccess: 'None' }
}

resource lifecycle 'Microsoft.Storage/storageAccounts/managementPolicies@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    policy: {
      rules: [{
        name: 'artifact-retention'
        enabled: true
        type: 'Lifecycle'
        definition: {
          actions: { baseBlob: { delete: { daysAfterModificationGreaterThan: artifactRetentionDays } } }
          filters: { blobTypes: ['blockBlob'], prefixMatch: ['artifacts/'] }
        }
      }]
    }
  }
}

resource blobAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for principalId in blobPrincipals: {
  name: guid(storage.id, principalId, blobContributorRoleId)
  scope: storage
  properties: { principalId: principalId, principalType: 'ServicePrincipal', roleDefinitionId: blobContributorRoleId }
}]

resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: '${namePrefix}-stg-pg'
  location: location
  tags: tags
  sku: { name: postgresSkuName, tier: postgresSkuTier }
  properties: {
    version: '16'
    authConfig: { activeDirectoryAuth: 'Enabled', passwordAuth: 'Disabled', tenantId: tenant().tenantId }
    backup: { backupRetentionDays: postgresBackupRetentionDays, geoRedundantBackup: postgresGeoRedundantBackup }
    highAvailability: { mode: postgresHighAvailabilityMode }
    network: {
      delegatedSubnetResourceId: postgresSubnetId
      privateDnsZoneArmResourceId: postgresPrivateDnsZoneId
      publicNetworkAccess: 'Disabled'
    }
    storage: { storageSizeGB: 32, autoGrow: 'Enabled', tier: 'P4' }
  }
}

resource postgresAdministrator 'Microsoft.DBforPostgreSQL/flexibleServers/administrators@2024-08-01' = {
  parent: postgres
  name: postgresEntraAdministratorObjectId
  properties: {
    principalName: postgresEntraAdministratorName
    principalType: 'Group'
    tenantId: tenant().tenantId
  }
}

resource redis 'Microsoft.Cache/redis@2024-11-01' = {
  name: '${namePrefix}-stg-redis'
  location: location
  tags: tags
  properties: {
    sku: { name: 'Premium', family: 'P', capacity: redisCapacity }
    enableNonSslPort: false
    minimumTlsVersion: '1.2'
    publicNetworkAccess: 'Disabled'
    disableAccessKeyAuthentication: true
    redisConfiguration: {
      'aad-enabled': 'true'
      'maxmemory-policy': 'volatile-lru'
      'rdb-backup-enabled': 'false'
    }
  }
}

resource storageDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'send-to-log-analytics'
  scope: storage
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    metrics: [{ category: 'Transaction', enabled: true }]
  }
}

resource vaultDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'send-to-log-analytics'
  scope: vault
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [{ category: 'AuditEvent', enabled: true }]
    metrics: [{ category: 'AllMetrics', enabled: true }]
  }
}

resource postgresDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'send-to-log-analytics'
  scope: postgres
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [{ categoryGroup: 'audit', enabled: true }]
    metrics: [{ category: 'AllMetrics', enabled: true }]
  }
}

resource redisDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'send-to-log-analytics'
  scope: redis
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [{ categoryGroup: 'audit', enabled: true }]
    metrics: [{ category: 'AllMetrics', enabled: true }]
  }
}

output keyVaultId string = vault.id
output vaultUri string = vault.properties.vaultUri
output storageAccountId string = storage.id
output storageBlobEndpoint string = storage.properties.primaryEndpoints.blob
output postgresFqdn string = postgres.properties.fullyQualifiedDomainName
output redisId string = redis.id
output redisHostname string = redis.properties.hostName

param location string
param namePrefix string
param privateEndpointSubnetId string
param blobPrivateDnsZoneId string
param keyVaultPrivateDnsZoneId string
param registryPrivateDnsZoneId string
param redisPrivateDnsZoneId string
param storageAccountId string
param keyVaultId string
param registryId string
param redisId string
param tags object

var targets = [
  { name: 'blob', resourceId: storageAccountId, groupId: 'blob', dnsZoneId: blobPrivateDnsZoneId }
  { name: 'vault', resourceId: keyVaultId, groupId: 'vault', dnsZoneId: keyVaultPrivateDnsZoneId }
  { name: 'registry', resourceId: registryId, groupId: 'registry', dnsZoneId: registryPrivateDnsZoneId }
  { name: 'redis', resourceId: redisId, groupId: 'redisCache', dnsZoneId: redisPrivateDnsZoneId }
]

resource privateEndpoints 'Microsoft.Network/privateEndpoints@2024-05-01' = [for target in targets: {
  name: '${namePrefix}-stg-${target.name}-pe'
  location: location
  tags: tags
  properties: {
    subnet: { id: privateEndpointSubnetId }
    privateLinkServiceConnections: [{
      name: '${target.name}-connection'
      properties: {
        privateLinkServiceId: target.resourceId
        groupIds: [target.groupId]
        privateLinkServiceConnectionState: { status: 'Approved', description: 'Bicep-managed private endpoint' }
      }
    }]
  }
}]

resource dnsZoneGroups 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = [for (target, index) in targets: {
  parent: privateEndpoints[index]
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [{ name: target.name, properties: { privateDnsZoneId: target.dnsZoneId } }]
  }
}]

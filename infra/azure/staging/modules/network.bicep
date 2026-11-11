param location string
param namePrefix string
param tags object

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: '${namePrefix}-stg-vnet'
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: ['10.42.0.0/16']
    }
    subnets: [
      {
        name: 'aca-infrastructure'
        properties: {
          addressPrefix: '10.42.0.0/23'
          delegations: [{
            name: 'Microsoft.App.environments'
            properties: { serviceName: 'Microsoft.App/environments' }
          }]
        }
      }
      {
        name: 'private-endpoints'
        properties: {
          addressPrefix: '10.42.2.0/24'
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
      {
        name: 'postgresql'
        properties: {
          addressPrefix: '10.42.3.0/24'
          delegations: [{
            name: 'Microsoft.DBforPostgreSQL.flexibleServers'
            properties: { serviceName: 'Microsoft.DBforPostgreSQL/flexibleServers' }
          }]
        }
      }
    ]
  }
}

var privateDnsZoneNames = [
  'privatelink.blob.${environment().suffixes.storage}'
  'privatelink.vaultcore.azure.net'
  'privatelink.azurecr.io'
  'privatelink.redis.cache.windows.net'
  'private.postgres.database.azure.com'
]

resource privateDnsZones 'Microsoft.Network/privateDnsZones@2020-06-01' = [for zoneName in privateDnsZoneNames: {
  name: zoneName
  location: 'global'
  tags: tags
}]

resource privateDnsLinks 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = [for (zoneName, index) in privateDnsZoneNames: {
  parent: privateDnsZones[index]
  name: '${namePrefix}-stg-link'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: { id: vnet.id }
  }
}]

output infrastructureSubnetId string = resourceId('Microsoft.Network/virtualNetworks/subnets', vnet.name, 'aca-infrastructure')
output privateEndpointSubnetId string = resourceId('Microsoft.Network/virtualNetworks/subnets', vnet.name, 'private-endpoints')
output postgresSubnetId string = resourceId('Microsoft.Network/virtualNetworks/subnets', vnet.name, 'postgresql')
output blobPrivateDnsZoneId string = privateDnsZones[0].id
output keyVaultPrivateDnsZoneId string = privateDnsZones[1].id
output registryPrivateDnsZoneId string = privateDnsZones[2].id
output redisPrivateDnsZoneId string = privateDnsZones[3].id
output postgresPrivateDnsZoneId string = privateDnsZones[4].id

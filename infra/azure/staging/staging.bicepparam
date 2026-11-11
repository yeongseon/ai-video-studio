using './main.bicep'

param location = '<AZURE_REGION>'
param namePrefix = '<UNIQUE_PREFIX>'
param postgresEntraAdministratorObjectId = '<ENTRA_GROUP_OBJECT_ID>'
param postgresEntraAdministratorName = '<ENTRA_GROUP_DISPLAY_NAME>'
param postgresHighAvailabilityMode = 'Disabled'
param postgresSkuName = 'Standard_B1ms'
param postgresSkuTier = 'Burstable'
param postgresBackupRetentionDays = 7
param postgresGeoRedundantBackup = 'Disabled'
param redisCapacity = 1
param artifactRetentionDays = 90
param logRetentionDays = 30

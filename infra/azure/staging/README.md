# Azure staging foundation

This directory implements the durable, resource-group-scoped foundation from
`docs/ACA_STAGING_TOPOLOGY.md`. It does not define Container Apps, migration
execution, image publication, or deployment automation. Those remain owned by
#926–#930. `DEPLOYMENT_ENABLED` remains closed.

## Parameters and boundaries

`staging.bicepparam` intentionally contains rejected placeholders for the Azure
region, globally unique name prefix, and approved PostgreSQL Entra administrator
group. Replace them only in an operator-reviewed deployment change. The template
does not accept passwords, keys, connection strings, tenant IDs, or subscription
IDs. PostgreSQL uses Entra-only authentication; runtime and migration database
roles must be created with distinct grants during the protected #926/#927 work.

Initial planning assumptions are PostgreSQL 16 on `Standard_B1ms` with seven-day
backup retention and HA disabled, Premium Redis with TLS-only private access,
90-day artifact lifecycle deletion, and 30-day Log Analytics retention. Region,
quota, price, HA/zone support, RPO/RTO, restore behavior, and diagnostic category
availability require real-subscription validation under #927 before deployment.

## Offline validation only

The focused test builds the template and inspects the compiled ARM JSON. It also
proves that the checked-in staging parameter file is rejected while placeholders
remain:

```bash
AZURE_BICEP_DISABLE_UPDATE_CHECK=true az bicep build \
  --file infra/azure/staging/main.bicep --outfile /tmp/staging-main.json
python3 -m pytest tests/test_azure_staging_bicep.py -q
```

No `az deployment`, `azd`, what-if, login, subscription lookup, or Azure resource
operation is part of this validation. A real what-if and deployment are explicitly
outside #925 and require the approvals documented in `docs/DEPLOYMENT.md`.

## API version sources

Resource API versions were selected from the Microsoft Learn ARM/Bicep template
reference for the exact resource types. Stable GA versions are used except for
`Microsoft.Insights/diagnosticSettings@2021-05-01-preview`, for which Microsoft
publishes no stable template API.

- <https://learn.microsoft.com/azure/templates/microsoft.app/managedenvironments>
- <https://learn.microsoft.com/azure/templates/microsoft.operationalinsights/workspaces>
- <https://learn.microsoft.com/azure/templates/microsoft.insights/components>
- <https://learn.microsoft.com/azure/templates/microsoft.network/virtualnetworks>
- <https://learn.microsoft.com/azure/templates/microsoft.network/privateendpoints>
- <https://learn.microsoft.com/azure/templates/microsoft.network/privatednszones>
- <https://learn.microsoft.com/azure/templates/microsoft.managedidentity/userassignedidentities>
- <https://learn.microsoft.com/azure/templates/microsoft.authorization/roleassignments>
- <https://learn.microsoft.com/azure/templates/microsoft.keyvault/vaults>
- <https://learn.microsoft.com/azure/templates/microsoft.storage/storageaccounts>
- <https://learn.microsoft.com/azure/templates/microsoft.containerregistry/registries>
- <https://learn.microsoft.com/azure/templates/microsoft.dbforpostgresql/flexibleservers>
- <https://learn.microsoft.com/azure/templates/microsoft.cache/redis>
- <https://learn.microsoft.com/azure/templates/microsoft.insights/diagnosticsettings>

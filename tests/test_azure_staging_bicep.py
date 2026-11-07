from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest

from azure_staging_bicep_support import (
    CANONICAL_ROLES,
    FORBIDDEN_ROLES,
    JsonValue,
    bicep_build_command,
    mapping as _mapping,
    resource as _resource,
    resources as _resources,
    resources_of_type as _resources_of_type,
    templates as _templates,
)


ROOT: Final = Path(__file__).resolve().parents[1]
MAIN_BICEP: Final = ROOT / "infra/azure/staging/main.bicep"
PARAMETERS: Final = ROOT / "infra/azure/staging/staging.bicepparam"
BICEP_COMMAND: Final = os.environ.get("BICEP_CLI", "az bicep").split()
REQUIRED_TAGS: Final = {"environment", "managedBy", "project", "workload"}


def _bicep_build_command(cli: list[str], output: Path) -> list[str]:
    return bicep_build_command(cli, MAIN_BICEP, output)


@pytest.fixture(scope="module")
def compiled_template(tmp_path_factory: pytest.TempPathFactory) -> dict[str, JsonValue]:
    output = tmp_path_factory.mktemp("azure-staging-bicep") / "main.json"
    result = subprocess.run(
        _bicep_build_command(BICEP_COMMAND, output),
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "AZURE_BICEP_DISABLE_UPDATE_CHECK": "true"},
    )
    assert re.search(r"\bBCP\d{3}\b", result.stderr) is None, (
        f"Bicep build emitted diagnostics:\n{result.stderr}"
    )
    with output.open(encoding="utf-8") as template_file:
        return json.load(template_file)


def test_bicep_cli_is_available() -> None:
    assert shutil.which(BICEP_COMMAND[0]) is not None, "CI must install the pinned Bicep prerequisite"


def test_foundation_builds_without_secret_outputs(
    compiled_template: dict[str, JsonValue],
) -> None:
    outputs = compiled_template.get("outputs", {})
    assert isinstance(outputs, dict)
    assert not {
        name
        for name in outputs
        if any(secret_word in name.lower() for secret_word in ("secret", "password", "key", "connection"))
    }


def test_key_vault_is_rbac_only_private_and_recoverable(
    compiled_template: dict[str, JsonValue],
) -> None:
    vaults = _resources_of_type(compiled_template, "Microsoft.KeyVault/vaults")
    assert len(vaults) == 1
    properties = _mapping(vaults[0]["properties"])
    assert properties["enableRbacAuthorization"] is True
    assert properties["enablePurgeProtection"] is True
    assert properties["enableSoftDelete"] is True
    assert properties["softDeleteRetentionInDays"] == 90
    assert properties["publicNetworkAccess"] == "Disabled"
    assert properties["accessPolicies"] == []
    assert properties["networkAcls"] == {"bypass": "None", "defaultAction": "Deny"}


def test_storage_disables_shared_keys_and_public_access(
    compiled_template: dict[str, JsonValue],
) -> None:
    accounts = _resources_of_type(compiled_template, "Microsoft.Storage/storageAccounts")
    assert len(accounts) == 1
    properties = _mapping(accounts[0]["properties"])
    assert properties["allowSharedKeyAccess"] is False
    assert properties["publicNetworkAccess"] == "Disabled"
    assert properties["minimumTlsVersion"] == "TLS1_2"
    assert properties["supportsHttpsTrafficOnly"] is True
    assert properties["defaultToOAuthAuthentication"] is True
    assert properties["networkAcls"] == {"bypass": "None", "defaultAction": "Deny"}


def test_registry_and_redis_disable_legacy_public_credentials(
    compiled_template: dict[str, JsonValue],
) -> None:
    registries = _resources_of_type(compiled_template, "Microsoft.ContainerRegistry/registries")
    assert len(registries) == 1
    registry_properties = _mapping(registries[0]["properties"])
    assert registry_properties["adminUserEnabled"] is False
    assert registry_properties["anonymousPullEnabled"] is False
    assert registry_properties["publicNetworkAccess"] == "Disabled"

    caches = _resources_of_type(compiled_template, "Microsoft.Cache/redis")
    assert len(caches) == 1
    cache_properties = _mapping(caches[0]["properties"])
    assert "sku" not in caches[0]
    cache_sku = _mapping(cache_properties["sku"])
    assert cache_sku["name"] == "Premium"
    assert cache_sku["family"] == "P"
    assert cache_sku["capacity"] == "[parameters('redisCapacity')]"
    assert cache_properties["enableNonSslPort"] is False
    assert cache_properties["minimumTlsVersion"] == "1.2"
    assert cache_properties["publicNetworkAccess"] == "Disabled"
    assert cache_properties["disableAccessKeyAuthentication"] is True
    assert _mapping(cache_properties["redisConfiguration"])["aad-enabled"] == "true"


def test_postgresql_is_private_and_has_no_password_parameter(
    compiled_template: dict[str, JsonValue],
) -> None:
    servers = _resources_of_type(compiled_template, "Microsoft.DBforPostgreSQL/flexibleServers")
    assert len(servers) == 1
    network = _mapping(_mapping(servers[0]["properties"])["network"])
    assert network["publicNetworkAccess"] == "Disabled"
    auth = _mapping(_mapping(servers[0]["properties"])["authConfig"])
    assert auth["activeDirectoryAuth"] == "Enabled"
    assert auth["passwordAuth"] == "Disabled"
    parameters = _mapping(compiled_template["parameters"])
    assert all("password" not in parameter.lower() for parameter in parameters)


def test_role_assignments_exclude_privileged_roles_and_match_contract(
    compiled_template: dict[str, JsonValue],
) -> None:
    assignments = _resources_of_type(compiled_template, "Microsoft.Authorization/roleAssignments")
    role_ids = {
        role_id
        for assignment in assignments
        if isinstance(
            role_id := _mapping(assignment["properties"])["roleDefinitionId"],
            str,
        )
    }
    assert all(
        not any(forbidden in role_id for forbidden in FORBIDDEN_ROLES.values())
        for role_id in role_ids
    )

    serialized = json.dumps(compiled_template)
    assert CANONICAL_ROLES["Container Registry Repository Reader"] in serialized
    assert CANONICAL_ROLES["Container Registry Repository Writer"] in serialized
    assert CANONICAL_ROLES["Storage Blob Data Contributor"] in serialized
    assert CANONICAL_ROLES["Key Vault Secrets User"] not in serialized

    assert len(assignments) == 3
    assert all(
        _mapping(assignment["properties"])["principalType"] == "ServicePrincipal"
        for assignment in assignments
    )
    assert '"blobPrincipals": ["[parameters(\'apiPrincipalId\')]", "[parameters(\'workerPrincipalId\')]"]' in serialized


def test_runtime_secret_roles_are_deferred_until_secret_resources_exist(
    compiled_template: dict[str, JsonValue],
) -> None:
    assignments = _resources_of_type(compiled_template, "Microsoft.Authorization/roleAssignments")
    assert all(
        CANONICAL_ROLES["Key Vault Secrets User"]
        not in str(_mapping(assignment["properties"])["roleDefinitionId"])
        for assignment in assignments
    )


def test_diagnostics_route_aca_and_registry_logs_without_runtime_keys(
    compiled_template: dict[str, JsonValue],
) -> None:
    serialized = json.dumps(compiled_template)
    assert "listKeys" not in serialized
    environment = _resource(compiled_template, "Microsoft.App/managedEnvironments")
    assert _mapping(_mapping(environment["properties"])["appLogsConfiguration"])["destination"] == "azure-monitor"

    diagnostics = _resources_of_type(compiled_template, "Microsoft.Insights/diagnosticSettings")
    by_scope = {str(setting["scope"]): _mapping(setting["properties"]) for setting in diagnostics}
    aca = next(properties for scope, properties in by_scope.items() if "Microsoft.App/managedEnvironments" in scope)
    acr = next(properties for scope, properties in by_scope.items() if "Microsoft.ContainerRegistry/registries" in scope)
    aca_logs = aca["logs"]
    acr_logs = acr["logs"]
    assert isinstance(aca_logs, list)
    assert isinstance(acr_logs, list)
    assert {str(_mapping(log)["category"]) for log in aca_logs} == {
        "ContainerAppConsoleLogs",
        "ContainerAppSystemLogs",
    }
    assert {str(_mapping(log)["category"]) for log in acr_logs} == {
        "ContainerRegistryLoginEvents",
        "ContainerRegistryRepositoryEvents",
    }


def test_private_endpoints_cover_every_private_data_service(
    compiled_template: dict[str, JsonValue],
) -> None:
    endpoints = _resources_of_type(compiled_template, "Microsoft.Network/privateEndpoints")
    dns_groups = _resources_of_type(compiled_template, "Microsoft.Network/privateEndpoints/privateDnsZoneGroups")
    assert len(endpoints) == 1
    assert len(dns_groups) == 1
    serialized = json.dumps(_templates(compiled_template))
    expected_group_ids = ('"groupId": "blob"', '"groupId": "vault"', '"groupId": "registry"', '"groupId": "redisCache"')
    assert all(group in serialized for group in expected_group_ids)


def test_every_resource_has_required_tags(compiled_template: dict[str, JsonValue]) -> None:
    untagged_types = {
        "Microsoft.Authorization/roleAssignments",
        "Microsoft.Insights/diagnosticSettings",
        "Microsoft.Network/privateDnsZones/virtualNetworkLinks",
        "Microsoft.DBforPostgreSQL/flexibleServers/administrators",
        "Microsoft.Network/privateEndpoints/privateDnsZoneGroups",
        "Microsoft.Storage/storageAccounts/blobServices",
        "Microsoft.Storage/storageAccounts/blobServices/containers",
        "Microsoft.Storage/storageAccounts/managementPolicies",
    }
    serialized = json.dumps(compiled_template)
    assert all(tag in serialized for tag in REQUIRED_TAGS)
    for resource in _resources(compiled_template):
        if resource["type"] not in untagged_types:
            assert "tags" in resource, resource["type"]


def test_parameters_have_no_hard_coded_tenant_or_subscription_ids() -> None:
    text = PARAMETERS.read_text(encoding="utf-8")
    assert "subscriptionId" not in text
    assert "tenantId" not in text
    assert "00000000-0000-0000-0000-000000000000" not in text


def test_staging_placeholders_are_rejected() -> None:
    result = subprocess.run(
        [
            sys.executable,
            os.fspath(ROOT / "scripts/validate_azure_staging_parameters.py"),
            str(PARAMETERS),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "AZURE_BICEP_DISABLE_UPDATE_CHECK": "true"},
    )
    assert result.returncode != 0
    assert "placeholder" in (result.stdout + result.stderr).lower()

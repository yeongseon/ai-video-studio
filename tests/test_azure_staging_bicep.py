from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Final, TypeAlias

import pytest


ROOT: Final = Path(__file__).resolve().parents[1]
MAIN_BICEP: Final = ROOT / "infra/azure/staging/main.bicep"
PARAMETERS: Final = ROOT / "infra/azure/staging/staging.bicepparam"
BICEP_COMMAND: Final = os.environ.get("BICEP_CLI", "az bicep").split()
FORBIDDEN_ROLES: Final = {
    "Owner": "8e3af657-a8ff-443c-a75c-2fe8c4bcb635",
    "Contributor": "b24988ac-6180-42a0-ab88-20f7382dd24c",
    "User Access Administrator": "18d7d88d-5c53-4b4e-9f4e-67f3e72fdcf9",
}
REQUIRED_TAGS: Final = {"environment", "managedBy", "project", "workload"}
JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


@pytest.fixture(scope="module")
def compiled_template(tmp_path_factory: pytest.TempPathFactory) -> dict[str, JsonValue]:
    output = tmp_path_factory.mktemp("azure-staging-bicep") / "main.json"
    subprocess.run(
        [*BICEP_COMMAND, "build", "--file", str(MAIN_BICEP), "--outfile", str(output)],
        cwd=ROOT,
        check=True,
        env={**os.environ, "AZURE_BICEP_DISABLE_UPDATE_CHECK": "true"},
    )
    with output.open(encoding="utf-8") as template_file:
        return json.load(template_file)


def _mapping(value: JsonValue) -> dict[str, JsonValue]:
    assert isinstance(value, dict)
    return value


def _resources(template: Mapping[str, JsonValue]) -> list[dict[str, JsonValue]]:
    resources = template["resources"]
    assert isinstance(resources, list)
    flattened: list[dict[str, JsonValue]] = []
    for resource in resources:
        assert isinstance(resource, dict)
        if resource["type"] == "Microsoft.Resources/deployments":
            properties = _mapping(resource["properties"])
            nested_template = _mapping(properties["template"])
            flattened.extend(_resources(nested_template))
        else:
            flattened.append(resource)
    return flattened


def _resources_of_type(
    template: Mapping[str, JsonValue], resource_type: str
) -> list[dict[str, JsonValue]]:
    return [resource for resource in _resources(template) if resource["type"] == resource_type]


def test_bicep_cli_is_available() -> None:
    assert shutil.which(BICEP_COMMAND[0]) is not None, "CI must install the pinned Bicep prerequisite"


def test_foundation_builds_without_secret_outputs(compiled_template: dict[str, JsonValue]) -> None:
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
    assert properties["publicNetworkAccess"] == "Disabled"
    assert properties["accessPolicies"] == []


def test_storage_disables_shared_keys_and_public_access(
    compiled_template: dict[str, JsonValue],
) -> None:
    accounts = _resources_of_type(compiled_template, "Microsoft.Storage/storageAccounts")
    assert len(accounts) == 1
    properties = _mapping(accounts[0]["properties"])
    assert properties["allowSharedKeyAccess"] is False
    assert properties["publicNetworkAccess"] == "Disabled"
    assert properties["minimumTlsVersion"] == "TLS1_2"


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
    assert cache_properties["enableNonSslPort"] is False
    assert cache_properties["minimumTlsVersion"] == "1.2"
    assert cache_properties["publicNetworkAccess"] == "Disabled"


def test_postgresql_is_private_and_has_no_password_parameter(
    compiled_template: dict[str, JsonValue],
) -> None:
    servers = _resources_of_type(compiled_template, "Microsoft.DBforPostgreSQL/flexibleServers")
    assert len(servers) == 1
    network = _mapping(_mapping(servers[0]["properties"])["network"])
    assert network["publicNetworkAccess"] == "Disabled"
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

    expected_roles = {
        "Container Registry Repository Reader": "b93aa761-3e63-49ed-ac28-beffa264f7ac",
        "Container Registry Repository Writer": "2a1e307c-b015-4ebd-883e-5b7698d2c559",
        "Key Vault Secrets User": "4633458b-17de-408a-b874-0445c86b69e6",
        "Storage Blob Data Contributor": "ba92f5b4-2d11-453d-a403-e96b0029c9fe",
    }
    serialized = json.dumps(compiled_template)
    assert all(role_id in serialized for role_id in expected_roles.values())


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

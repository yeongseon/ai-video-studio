from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Final, TypeAlias

import pytest

FORBIDDEN_ROLES: Final = {
    "Owner": "8e3af657-a8ff-443c-a75c-2fe8c4bcb635",
    "Contributor": "b24988ac-6180-42a0-ab88-20f7382dd24c",
    "User Access Administrator": "18d7d88d-5c53-4b4e-9f4e-67f3e72fdcf9",
}
CANONICAL_ROLES: Final = {
    "Container Registry Repository Reader": "b93aa761-3e63-49ed-ac28-beffa264f7ac",
    "Container Registry Repository Writer": "2a1e307c-b015-4ebd-883e-5b7698a07328",
    "Key Vault Secrets User": "4633458b-17de-408a-b874-0445c86b69e6",
    "Storage Blob Data Contributor": "ba92f5b4-2d11-453d-a403-e96b0029c9fe",
}
JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


def bicep_build_command(cli: list[str], input_file: Path, output: Path) -> list[str]:
    match cli:
        case ["az", "bicep"]:
            input_arguments = ["--file", str(input_file)]
        case [standalone] if Path(standalone).name == "bicep":
            input_arguments = [str(input_file)]
        case _:
            pytest.fail(f"Unsupported Bicep CLI command: {cli}")
    return [*cli, "build", *input_arguments, "--outfile", str(output)]


def mapping(value: JsonValue) -> dict[str, JsonValue]:
    assert isinstance(value, dict)
    return value


def resources(template: Mapping[str, JsonValue]) -> list[dict[str, JsonValue]]:
    template_resources = template["resources"]
    assert isinstance(template_resources, list)
    flattened: list[dict[str, JsonValue]] = []
    for resource in template_resources:
        assert isinstance(resource, dict)
        if resource["type"] == "Microsoft.Resources/deployments":
            properties = mapping(resource["properties"])
            nested_template = mapping(properties["template"])
            flattened.extend(resources(nested_template))
        else:
            flattened.append(resource)
    return flattened


def resources_of_type(
    template: Mapping[str, JsonValue], resource_type: str
) -> list[dict[str, JsonValue]]:
    return [resource for resource in resources(template) if resource["type"] == resource_type]


def templates(template: Mapping[str, JsonValue]) -> list[dict[str, JsonValue]]:
    nested_templates = [dict(template)]
    template_resources = template["resources"]
    assert isinstance(template_resources, list)
    for resource in template_resources:
        assert isinstance(resource, dict)
        if resource["type"] == "Microsoft.Resources/deployments":
            properties = mapping(resource["properties"])
            nested_templates.extend(templates(mapping(properties["template"])))
    return nested_templates


def resource(template: Mapping[str, JsonValue], resource_type: str) -> dict[str, JsonValue]:
    matching_resources = resources_of_type(template, resource_type)
    assert len(matching_resources) == 1
    return matching_resources[0]

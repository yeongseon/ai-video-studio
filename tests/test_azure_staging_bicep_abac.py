from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Final

import pytest

from acr_abac_condition_support import evaluate_condition, role_data_actions
from azure_staging_bicep_support import (
    JsonValue,
    bicep_build_command,
    mapping,
    resources_of_type,
)


ROOT: Final = Path(__file__).resolve().parents[1]
MAIN_BICEP: Final = ROOT / "infra/azure/staging/main.bicep"
BICEP_COMMAND: Final = os.environ.get("BICEP_CLI", "az bicep").split()


@pytest.fixture(scope="module")
def compiled_template(tmp_path_factory: pytest.TempPathFactory) -> dict[str, JsonValue]:
    output = tmp_path_factory.mktemp("azure-staging-bicep-abac") / "main.json"
    result = subprocess.run(
        bicep_build_command(BICEP_COMMAND, MAIN_BICEP, output),
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "AZURE_BICEP_DISABLE_UPDATE_CHECK": "true"},
    )
    assert re.search(r"\bBCP\d{3}\b", result.stderr) is None, result.stderr
    with output.open(encoding="utf-8") as template_file:
        return json.load(template_file)


def _registry_assignments(
    compiled_template: dict[str, JsonValue],
) -> list[dict[str, JsonValue]]:
    assignments = resources_of_type(compiled_template, "Microsoft.Authorization/roleAssignments")
    return [
        assignment
        for assignment in assignments
        if "Microsoft.ContainerRegistry/registries" in str(assignment.get("scope", ""))
    ]


def test_registry_assignments_guard_every_role_data_action(
    compiled_template: dict[str, JsonValue],
) -> None:
    registry_assignments = _registry_assignments(compiled_template)
    assert len(registry_assignments) == 2

    for assignment in registry_assignments:
        properties = mapping(assignment["properties"])
        condition = properties["condition"]
        role_definition_id = properties["roleDefinitionId"]
        assert properties["conditionVersion"] == "2.0"
        assert isinstance(condition, str)
        assert isinstance(role_definition_id, str)
        assert "@Request[Microsoft.ContainerRegistry/registries/repositories:name]" in condition
        assert "StringEqualsIgnoreCase" in condition
        assert "@Resource[" not in condition
        assert all(data_action in condition for data_action in role_data_actions(role_definition_id))

    serialized = json.dumps(compiled_template)
    assert '"identityRepositories": ["studio-web", "api", "worker", "worker", "api"]' in serialized
    assert all(repository in serialized for repository in ("studio-web", "api", "worker"))


def test_registry_conditions_deny_every_role_data_action_outside_allowed_repositories(
    compiled_template: dict[str, JsonValue],
) -> None:
    for assignment in _registry_assignments(compiled_template):
        properties = mapping(assignment["properties"])
        condition = properties["condition"]
        role_definition_id = properties["roleDefinitionId"]
        assert isinstance(condition, str)
        assert isinstance(role_definition_id, str)
        allowed_repository = "api" if "'api'" in condition else "{0}"

        for data_action in role_data_actions(role_definition_id):
            assert evaluate_condition(condition, action=data_action, repository=allowed_repository)
            assert not evaluate_condition(
                condition,
                action=data_action,
                repository="outside-allowed-repositories",
            )

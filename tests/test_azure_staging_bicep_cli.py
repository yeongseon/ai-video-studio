from pathlib import Path

import pytest

from azure_staging_bicep_support import bicep_build_command

MAIN_BICEP = Path("infra/azure/staging/main.bicep")


@pytest.mark.parametrize(
    ("cli", "expected_input_arguments"),
    [
        (["/tmp/tools/bicep"], [str(MAIN_BICEP)]),
        (["az", "bicep"], ["--file", str(MAIN_BICEP)]),
    ],
)
def test_build_command_uses_cli_specific_input_grammar(
    cli: list[str], expected_input_arguments: list[str]
) -> None:
    output = Path("main.json")
    command = bicep_build_command(cli, MAIN_BICEP, output)
    assert command == [*cli, "build", *expected_input_arguments, "--outfile", str(output)]

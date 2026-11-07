from pathlib import Path
import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("path", ["apps/api/tests/test_project_settings_rollback.py",
                                  "apps/api/tests/test_project_settings_migration.py",
                                  "packages/creator-service/tests/test_project_settings_storage.py"])
def test_settings_database_when_missing_in_ci_fails(path: str) -> None:
    # Given Actions without its declared database, when running the durable contract.
    env = {key: value for key, value in os.environ.items() if key != "FIRST_SHORT_TEST_DATABASE_URL"}
    result = subprocess.run([sys.executable, "-m", "pytest", path, "-q", "-p", "no:cacheprovider"],
                            cwd=Path(__file__).resolve().parents[3], capture_output=True, text=True,
                            env={**env, "GITHUB_ACTIONS": "true", "PYTHONPATH": "apps/api/src"})
    # Then the prerequisite failure is visible, not skipped.
    assert result.returncode != 0
    assert "FIRST_SHORT_TEST_DATABASE_URL is required" in result.stdout

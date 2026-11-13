from __future__ import annotations

import os
from pathlib import Path
import subprocess
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, ConfigDict
import pytest

if TYPE_CHECKING:
    from tests.test_local_storage_migration import Migration

SECRET: Final = "synthetic-secret-value"


class ArtifactVolume(BaseModel):
    model_config = ConfigDict(frozen=True)
    source: str = ""
    target: str


class ArtifactService(BaseModel):
    model_config = ConfigDict(frozen=True)
    volumes: tuple[ArtifactVolume, ...] = ()


class ArtifactConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    services: dict[str, ArtifactService]


@pytest.mark.parametrize("overlay", [None, "local-server", "scaled-workers"])
@pytest.mark.parametrize("section", ["Switch", "Rollback"])
def test_plain_compose_when_migrated_uses_persistent_storage(
    migration: Migration, project: Path, overlay: str | None, section: str,
) -> None:
    # Given the guide's switch (and optionally rollback), without shell storage overrides.
    assert migration.run("Copy").returncode == 0
    switched = migration.run("Switch", cwd=project)
    assert switched.returncode == 0, switched.stderr
    if section == "Rollback":
        rolled_back = migration.run("Rollback", cwd=project)
        assert rolled_back.returncode == 0, rolled_back.stderr
    files = ["-f", "docker-compose.yml"]
    if overlay:
        files.extend(["-f", f"docker-compose.{overlay}.yml"])

    # When resolving exactly the plain Compose mechanism used by Makefile and cutover.
    result = subprocess.run(
        ["docker", "compose", *files, "config", "--format", "json"], cwd=project,
        env={"PATH": os.environ["PATH"], "COMPOSE_PROJECT_NAME": "migration-entrypoints"},
        capture_output=True, text=True, check=False, timeout=30,
    )

    # Then every active artifact consumer uses one tree, including every scaled worker.
    assert result.returncode == 0, result.stderr
    config = ArtifactConfig.model_validate_json(result.stdout)
    consumers = {name: [v.source.replace("$$", "$") for v in service.volumes
                        if v.target == "/app/data/artifacts"]
                 for name, service in config.services.items()
                 if any(v.target == "/app/data/artifacts" for v in service.volumes)}
    expected_names = {"api", "worker"} if overlay != "scaled-workers" else {
        "api", "worker-script", "worker-image", "worker-audio", "worker-render",
    }
    expected = migration.destination if section == "Switch" else project / "data/artifacts"
    assert consumers == dict.fromkeys(expected_names, [str(expected)])


@pytest.mark.parametrize("original", [b"API_KEY=synthetic-secret-value\n", b"API_KEY=synthetic-secret-value",
                                       b"# ARTIFACTS_HOST_DIR=ignored\r\nAPI_KEY=synthetic-secret-value\r\n"])
@pytest.mark.parametrize("mode", [0o600, 0o640])
def test_switch_when_updating_dotenv_preserves_bytes_and_permissions(
    migration: Migration, original: bytes, mode: int,
) -> None:
    # Given existing secrets, including CRLF or an unterminated final line.
    dotenv = migration.root / ".env"
    dotenv.write_bytes(original)
    dotenv.chmod(mode)
    assert migration.run("Copy").returncode == 0

    # When switching using the guide.
    result = migration.run("Switch")

    # Then only a separated, literal storage line is appended, without widening access.
    assert result.returncode == 0, result.stderr
    separator = b"" if original.endswith(b"\n") else b"\n"
    assert dotenv.read_bytes() == original + separator + f"ARTIFACTS_HOST_DIR='{migration.destination}'\n".encode()
    assert dotenv.stat().st_mode & 0o777 == mode
    assert SECRET not in result.stdout + result.stderr
    assert SECRET not in migration.env_file.read_text()
    assert migration.run("Rollback").returncode == 0
    assert dotenv.read_bytes() == original
    assert dotenv.stat().st_mode & 0o777 == mode


@pytest.mark.parametrize("definition", ["ARTIFACTS_HOST_DIR=/old", "export ARTIFACTS_HOST_DIR='/old'",
                                         "  ARTIFACTS_HOST_DIR = /old", "ARTIFACTS_HOST_DIR: /old"])
def test_switch_when_dotenv_already_defines_storage_refuses(migration: Migration, definition: str) -> None:
    # Given a pre-existing storage decision and secrets, with no trailing newline.
    dotenv = migration.root / ".env"
    original = f"API_KEY={SECRET}\r\n{definition}".encode()
    dotenv.write_bytes(original)
    assert migration.run("Copy").returncode == 0
    # When switching.
    result = migration.run("Switch")
    # Then neither secrets nor the existing choice is overwritten, and no receipt is created.
    assert result.returncode != 0
    assert dotenv.read_bytes() == original
    assert not migration.env_file.exists()
    assert SECRET not in result.stdout + result.stderr


@pytest.mark.parametrize("section", ["Switch", "Rollback"])
def test_env_receipt_when_aliasing_dotenv_is_refused(migration: Migration, section: str) -> None:
    # Given an operator accidentally selecting the secrets file as the receipt.
    assert migration.run("Copy").returncode == 0
    dotenv = migration.root / ".env"
    original = dotenv.read_bytes()
    migration.env_file.symlink_to(dotenv)
    # When running either modifying block.
    result = migration.run(section)
    # Then the realpath alias cannot truncate or change secrets.
    assert result.returncode != 0
    assert dotenv.read_bytes() == original


def test_switch_when_destination_group_is_wrong_refuses(migration: Migration) -> None:
    # Given a copied tree with a group other than the container's, where permitted by the host.
    assert migration.run("Copy").returncode == 0
    wrong_groups = [gid for gid in os.getgroups() if gid != 1000]
    if os.geteuid() == 0:
        wrong_groups.append(0)
    if not wrong_groups:
        # An unprivileged uid-1000 process cannot manufacture a different owner/group.
        return
    os.chown(migration.destination / "run-7", -1, wrong_groups[0])
    # When switching.
    result = migration.run("Switch")
    # Then wrong ownership blocks persistence.
    assert result.returncode != 0
    assert not migration.env_file.exists()
    assert (migration.root / ".env").read_text() == f"API_KEY={SECRET}\n"


def test_inspect_when_scaled_prints_every_artifact_writer(migration: Migration, project: Path) -> None:
    # Given the actual scaled topology with no limits on its stage workers.
    assert migration.run("Copy").returncode == 0
    assert migration.run("Switch", cwd=project).returncode == 0
    # When inspecting with the operator's usual overlay selection.
    result = migration.run("Inspect", cwd=project, extra_env={
        "COMPOSE_FILE": "docker-compose.yml:docker-compose.scaled-workers.yml",
    })
    # Then all actual writers appear, missing limits are explicit, and secrets stay private.
    assert result.returncode == 0, result.stderr
    assert set(result.stdout.splitlines()) == {
        f"api ['{migration.destination}'] 1024 MiB",
        *(f"worker-{stage} ['{migration.destination}'] unset" for stage in ("script", "image", "audio", "render")),
    }
    assert SECRET not in result.stdout + result.stderr

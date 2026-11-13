from __future__ import annotations

import hashlib
import json
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

    # When resolving exactly the plain Compose mechanism used by Makefile.
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
    assert migration.env_file.stat().st_mode & 0o777 == 0o600
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


@pytest.mark.parametrize("identity", ["APP_UID", "APP_GID"])
def test_switch_when_destination_group_is_wrong_refuses(migration: Migration, identity: str) -> None:
    # Given a copied tree and a configured identity different from its actual owner/group.
    assert migration.run("Copy").returncode == 0
    owner = os.getuid() if identity == "APP_UID" else os.getgid()
    # When switching.
    result = migration.run("Switch", extra_env={identity: str(owner + 1)})
    # Then wrong ownership blocks persistence.
    assert result.returncode != 0
    assert not migration.env_file.exists()
    assert (migration.root / ".env").read_text() == f"API_KEY={SECRET}\n"


def test_copy_when_configured_identity_is_unavailable_refuses(migration: Migration) -> None:
    # Given an unprivileged operator who cannot assign a different UID.
    if os.geteuid() == 0:
        pytest.skip("root can assign any UID")
    # When Copy is configured for a different application identity.
    result = migration.run("Copy", extra_env={"APP_UID": str(os.getuid() + 1)})
    # Then ownership correction fails closed rather than reporting a usable copy.
    assert result.returncode != 0
    assert not migration.env_file.exists()


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


@pytest.mark.parametrize("ending", [b"", b"\n"])
@pytest.mark.parametrize("later", [b"", b"LATER=1\n"])
def test_rollback_when_settings_are_appended_preserves_line_boundaries(
    migration: Migration, ending: bytes, later: bytes,
) -> None:
    # Given an original dotenv with or without a final newline, then a later setting.
    dotenv = migration.root / ".env"
    original = b"POSTGRES_PASSWORD=synthetic-secret-value" + ending
    dotenv.write_bytes(original)
    assert migration.run("Copy").returncode == 0
    assert migration.run("Switch").returncode == 0
    with dotenv.open("ab") as stream:
        stream.write(later)
    # When removing the migration setting.
    result = migration.run("Rollback")
    # Then later settings stay separated, while no append restores the exact original.
    assert result.returncode == 0, result.stderr
    separator = b"\n" if later and not ending else b""
    assert dotenv.read_bytes() == original + separator + later


@pytest.mark.parametrize("variable", ["ARTIFACTS_HOST_DIR", "COMPOSE_ENV_FILES", "COMPOSE_DISABLE_ENV_FILE"])
def test_switch_when_process_overrides_dotenv_refuses(migration: Migration, variable: str) -> None:
    # Given an exported override, including an empty value that still changes Compose behavior.
    assert migration.run("Copy").returncode == 0
    dotenv = migration.root / ".env"
    original = dotenv.read_bytes()
    # When switching with a conflicting process variable.
    result = migration.run("Switch", extra_env={variable: ""})
    # Then the migration cannot claim the persisted setting controls Compose.
    assert result.returncode != 0
    assert dotenv.read_bytes() == original
    assert not migration.env_file.exists()


def test_rollback_when_receipt_targets_another_setting_refuses(migration: Migration) -> None:
    # Given a receipt whose hash matches but whose addition targets a non-storage setting.
    assert migration.run("Copy").returncode == 0
    dotenv = migration.root / ".env"
    original = dotenv.read_bytes()
    addition = "LATER=1\n"
    current = original + addition.encode()
    dotenv.write_bytes(current)
    migration.env_file.write_text(json.dumps({
        "size": len(original), "sha256": hashlib.sha256(original).hexdigest(),
        "addition": addition, "separator": "", "line": addition,
    }))
    # When rolling back with the altered receipt.
    result = migration.run("Rollback")
    # Then the unrelated setting cannot be stripped.
    assert result.returncode != 0
    assert dotenv.read_bytes() == current


def test_interrupted_dotenv_temporary_when_staged_is_ignored() -> None:
    # Given the checkout ignore policy and a possible interrupted atomic-write filename.
    root = Path(__file__).resolve().parent
    # When Git checks whether that secrets-bearing path can be staged normally.
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", ".env-storage-crash-test"], cwd=root,
        env={**os.environ, "GIT_MASTER": "1"},
        capture_output=True, text=True, check=False, timeout=30,
    )
    # Then the temporary copy is ignored without creating or reading any real secrets.
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ".env-storage-crash-test"

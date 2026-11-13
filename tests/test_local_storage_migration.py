from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Final

from creator_service.object_storage import LocalStorageBackend
from local_storage_migration_cases import (
    test_env_receipt_when_aliasing_dotenv_is_refused as test_env_receipt_when_aliasing_dotenv_is_refused,
    test_copy_when_configured_identity_is_unavailable_refuses as test_copy_when_configured_identity_is_unavailable_refuses,
    test_inspect_when_scaled_prints_every_artifact_writer as test_inspect_when_scaled_prints_every_artifact_writer,
    test_interrupted_dotenv_temporary_when_staged_is_ignored as test_interrupted_dotenv_temporary_when_staged_is_ignored,
    test_plain_compose_when_migrated_uses_persistent_storage as test_plain_compose_when_migrated_uses_persistent_storage,
    test_rollback_when_settings_are_appended_preserves_line_boundaries as test_rollback_when_settings_are_appended_preserves_line_boundaries,
    test_rollback_when_receipt_targets_another_setting_refuses as test_rollback_when_receipt_targets_another_setting_refuses,
    test_switch_when_destination_group_is_wrong_refuses as test_switch_when_destination_group_is_wrong_refuses,
    test_switch_when_dotenv_already_defines_storage_refuses as test_switch_when_dotenv_already_defines_storage_refuses,
    test_switch_when_updating_dotenv_preserves_bytes_and_permissions as test_switch_when_updating_dotenv_preserves_bytes_and_permissions,
    test_switch_when_process_overrides_dotenv_refuses as test_switch_when_process_overrides_dotenv_refuses,
)
import pytest

ROOT: Final = Path(__file__).resolve().parents[1]
GUIDE: Final = ROOT / "docs/LOCAL_STORAGE.md"
SECRET: Final = "synthetic-secret-value"


def _command(section: str) -> str:
    text = GUIDE.read_text(encoding="utf-8")
    match = re.search(rf"^### {section}\n.*?```bash\n(.*?)```", text, re.MULTILINE | re.DOTALL)
    assert match is not None, f"Missing executable migration section: {section}"
    return match[1]


@dataclass(frozen=True, slots=True)
class Migration:
    root: Path
    source: Path
    destination: Path
    manifest: Path
    env_file: Path

    # Plain `bash -c`: each block must carry its own `set -euo pipefail`, so an operator who runs
    # it without those flags is still protected.
    def run(
        self, section: str, destination: Path | None = None, *,
        cwd: Path | None = None, extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "SRC": str(self.source), "DEST": str(destination or self.destination),
               "MANIFEST": str(self.manifest), "ENV_FILE": str(self.env_file),
               "APP_UID": str(os.getuid()), "APP_GID": str(os.getgid())}
        env.update(extra_env or {})
        return subprocess.run(
            ["bash", "-c", _command(section)],
            cwd=cwd or self.root, env=env, capture_output=True, text=True, check=False, timeout=30,
        )


@pytest.fixture
def migration(tmp_path: Path) -> Migration:
    for tool in ("rsync", "sha256sum"):
        if shutil.which(tool) is None:
            message = f"{tool} is required to exercise the documented migration"
            if "GITHUB_ACTIONS" in os.environ:
                pytest.fail(message, pytrace=False)
            pytest.skip(message)
    source = tmp_path / "original $artifacts"
    (source / "run-7").mkdir(parents=True)
    (source / "run-7/final video.mp4").write_bytes(b"published\x00fixture\xff")
    (source / ".hidden-input").write_bytes(b"original input")
    (source / "empty-directory").mkdir()
    destination = tmp_path / "hdd #1 $media"
    destination.mkdir()
    (tmp_path / ".env").write_text(f"API_KEY={SECRET}\n", encoding="utf-8")
    return Migration(tmp_path, source, destination, tmp_path / "source.sha256", tmp_path / "storage.env")


@pytest.fixture
def project(migration: Migration) -> Path:
    if shutil.which("docker") is None:
        message = "Docker Compose is required to inspect the documented configuration"
        if "GITHUB_ACTIONS" in os.environ:
            pytest.fail(message, pytrace=False)
        pytest.skip(message)
    directory = migration.root / "compose-project"
    directory.mkdir()
    for name in ("docker-compose.yml", "docker-compose.local-server.yml", "docker-compose.scaled-workers.yml"):
        shutil.copyfile(ROOT / name, directory / name)
    (directory / ".env").write_text(
        f"POSTGRES_DB=storage_test\nPOSTGRES_USER=storage_test\nPOSTGRES_PASSWORD={SECRET}\n"
        f"API_KEY={SECRET}\n", encoding="utf-8",
    )
    return directory


def _inspect(migration: Migration, project: Path) -> subprocess.CompletedProcess[str]:
    result = migration.run("Inspect", cwd=project, extra_env={"COMPOSE_PROJECT_NAME": "local-storage-guide"})
    assert result.returncode == 0, result.stderr
    return result


def test_copy_when_following_the_guide_preserves_originals(migration: Migration) -> None:
    # Given an artifact tree with nested, hidden and empty entries.
    before = {p.relative_to(migration.source): p.read_bytes()
              for p in migration.source.rglob("*") if p.is_file()}

    # When executing the documented manifest and copy commands.
    result = migration.run("Copy")

    # Then the original bytes and layout remain, and the copy passes the actual manifest check.
    assert result.returncode == 0, result.stderr
    assert {p.relative_to(migration.source): p.read_bytes()
            for p in migration.source.rglob("*") if p.is_file()} == before
    assert {p.relative_to(migration.destination): p.read_bytes()
            for p in migration.destination.rglob("*") if p.is_file()} == before
    assert (migration.source / "empty-directory").is_dir()
    assert (migration.destination / "empty-directory").is_dir()
    verified = migration.run("Verify")
    assert verified.returncode == 0, verified.stderr


def test_verify_when_copy_is_corrupted_blocks_switch(migration: Migration) -> None:
    # Given a copied artifact whose destination bytes were corrupted.
    copied = migration.run("Copy")
    assert copied.returncode == 0, copied.stderr
    (migration.destination / "run-7/final video.mp4").write_bytes(b"corrupt")

    # When executing the documented verification gate and switch.
    result = migration.run("Switch")

    # Then the manifest detects corruption and no storage override is written.
    assert result.returncode != 0
    assert "FAILED" in result.stdout + result.stderr
    assert not migration.env_file.exists()


@pytest.mark.parametrize("tree", ["source", "destination"])
def test_verify_when_the_file_inventories_differ_blocks_switch(migration: Migration, tree: str) -> None:
    # Given a copied tree where a writer that was not stopped added a file to one side,
    # which a manifest of the original files cannot notice.
    copied = migration.run("Copy")
    assert copied.returncode == 0, copied.stderr
    side = migration.source if tree == "source" else migration.destination
    (side / "run-7/late-upload.mp4").write_bytes(b"late")

    # When running the documented verification gate and the switch.
    verified = migration.run("Verify")
    switched = migration.run("Switch")

    # Then both refuse, and no storage override is written.
    assert verified.returncode != 0
    assert switched.returncode != 0
    assert not migration.env_file.exists()


@pytest.mark.parametrize("section", ["Verify", "Switch"])
@pytest.mark.parametrize("tree", ["source", "destination"])
@pytest.mark.skipif(os.geteuid() == 0, reason="permission checks do not apply to root")
def test_inventory_when_a_tree_cannot_be_fully_enumerated_blocks_switch(
    migration: Migration, section: str, tree: str,
) -> None:
    # Given a copied tree with an unreadable directory holding an unlisted file, so that the
    # readable file lists are still identical while enumeration itself fails.
    assert migration.run("Copy").returncode == 0
    side = migration.source if tree == "source" else migration.destination
    hidden = side / "run-7" / "unreadable"
    hidden.mkdir()
    (hidden / "late-upload.mp4").write_bytes(b"late")
    hidden.chmod(0)

    # When running the documented verification gate or the switch.
    try:
        result = migration.run(section)
    finally:
        hidden.chmod(0o755)

    # Then it refuses, and no storage override is written after an incomplete comparison.
    assert result.returncode != 0
    assert not migration.env_file.exists()


def test_rollback_when_switching_storage_preserves_storage_keys(migration: Migration) -> None:
    # Given a verified migration selected with the documented override.
    copied = migration.run("Copy")
    assert copied.returncode == 0, copied.stderr
    switched = migration.run("Switch")
    assert switched.returncode == 0, switched.stderr
    assert json.loads(migration.env_file.read_text())["addition"] == f"ARTIFACTS_HOST_DIR='{migration.destination}'\n"
    storage_key = "run-7/final video.mp4"
    original = LocalStorageBackend(str(migration.source))
    selected = LocalStorageBackend(str(migration.destination))
    assert selected.download_bytes(storage_key) == b"published\x00fixture\xff"
    assert selected.download_url(storage_key) == original.download_url(storage_key)

    # When restoring the original host directory with the documented rollback command.
    result = migration.run("Rollback")

    # Then the original remains usable under the same storage key without reverse-copying.
    assert result.returncode == 0, result.stderr
    assert (migration.root / ".env").read_text() == f"API_KEY={SECRET}\n"
    assert original.download_bytes(storage_key) == b"published\x00fixture\xff"
    assert (migration.destination / storage_key).read_bytes() == b"published\x00fixture\xff"


def test_inspect_when_switching_and_rolling_back_shows_the_literal_paths_and_no_secrets(
    migration: Migration, project: Path,
) -> None:
    # Given a verified copy, and a .env holding real-looking secrets,
    # with paths containing a space, a variable-like name and a comment marker.
    assert migration.run("Copy").returncode == 0
    assert migration.run("Switch", cwd=project).returncode == 0

    # When inspecting the persisted project configuration.
    switched = _inspect(migration, project)

    # Then Compose reads the destination literally and no secret is printed.
    assert switched.stdout.splitlines() == [
        f"api ['{migration.destination}'] 1024 MiB", f"worker ['{migration.destination}'] 4096 MiB",
    ]
    assert SECRET not in switched.stdout + switched.stderr

    # When rolling back and inspecting again.
    assert migration.run("Rollback", cwd=project).returncode == 0
    rolled_back = _inspect(migration, project)

    # Then the default project source is restored without leaking secrets.
    assert rolled_back.stdout.splitlines() == [
        f"api ['{project / 'data/artifacts'}'] 1024 MiB",
        f"worker ['{project / 'data/artifacts'}'] 4096 MiB",
    ]
    assert SECRET not in rolled_back.stdout + rolled_back.stderr


def test_variable_defaults_when_sourced_keep_private_metadata_outside_checkout(migration: Migration) -> None:
    # Given the variable setup from the first Bash block, without host-specific probes.
    text = GUIDE.read_text(encoding="utf-8")
    block = text.split("```bash\n", 1)[1].split("```", 1)[0].split("df -h", 1)[0]
    # When sourcing it into an operator shell with an isolated home and temporary directory.
    home = migration.root / "operator-home"
    home.mkdir()
    result = subprocess.run(
        ["bash", "-c", block + '\nprintf "%s\\n%s\\n" "$MANIFEST" "$ENV_FILE"'],
        cwd=migration.root, env={"PATH": os.environ["PATH"], "HOME": str(home),
                                 "TMPDIR": str(migration.destination)},
        capture_output=True, text=True, check=False, timeout=30,
    )
    # Then neither default can be accidentally staged from the checkout.
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    paths = [Path(line) for line in lines[-2:]]
    assert len(paths) == 2
    assert all(path.is_relative_to(home / ".local/state/short-form-studio-storage") for path in paths)
    assert paths[0] != paths[1]
    assert lines[0] == str(paths[0].parent)
    assert paths[0].parent.stat().st_mode & 0o777 == 0o700


def test_copy_when_destination_is_a_symlink_to_existing_media_changes_nothing(migration: Migration) -> None:
    # Given a destination that is a symlink to a directory already holding other media at
    # the same relative path as a source file.
    existing = migration.root / "existing media"
    (existing / "run-7").mkdir(parents=True)
    (existing / "run-7/final video.mp4").write_bytes(b"someone else's media")
    link = migration.root / "artifacts-link"
    link.symlink_to(existing, target_is_directory=True)

    # When running the documented copy against it.
    result = migration.run("Copy", link)

    # Then it refuses before touching anything, so the existing media is not overwritten.
    assert result.returncode != 0
    assert (existing / "run-7/final video.mp4").read_bytes() == b"someone else's media"
    assert not migration.manifest.exists()


def test_copy_when_source_is_missing_changes_nothing(migration: Migration) -> None:
    # Given a source directory that does not exist, for example a mistyped checkout path.
    shutil.rmtree(migration.source)

    # When running the documented copy.
    result = migration.run("Copy")

    # Then it fails before creating a manifest or copying a byte.
    assert result.returncode != 0
    assert not migration.manifest.exists()
    assert not any(migration.destination.iterdir())


def test_copy_when_destination_is_relative_changes_nothing(migration: Migration) -> None:
    # Given a relative destination, whose meaning depends on the directory Compose runs in.
    # When running the documented copy.
    result = migration.run("Copy", Path("hdd-artifacts"))

    # Then it fails before creating a manifest or copying a byte.
    assert result.returncode != 0
    assert not migration.manifest.exists()
    assert not any(migration.destination.iterdir())


@pytest.mark.parametrize("side", ["source", "destination"])
def test_copy_when_a_path_ends_with_a_backslash_changes_nothing(migration: Migration, side: str) -> None:
    # Given a directory whose name ends with a backslash, which would turn the closing single
    # quote of the env-file value into an escaped quote.
    awkward = migration.root / "ends-with-backslash\\"
    awkward.mkdir()
    if side == "source":
        (awkward / "run-7").mkdir()
        (awkward / "run-7/final video.mp4").write_bytes(b"published")
        migration = Migration(migration.root, awkward, migration.destination, migration.manifest, migration.env_file)

    # When running the documented copy.
    result = migration.run("Copy", awkward if side == "destination" else None)

    # Then it fails before creating a manifest or copying a byte.
    assert result.returncode != 0
    assert not migration.manifest.exists()
    assert not any(migration.destination.iterdir() if side == "source" else awkward.iterdir())


def test_copy_when_a_path_holds_a_quote_changes_nothing(migration: Migration) -> None:
    # Given a destination whose name holds a single quote, which cannot be written literally
    # to the env file.
    quoted = migration.root / "it's mine"
    quoted.mkdir()

    # When running the documented copy.
    result = migration.run("Copy", quoted)

    # Then it fails before creating a manifest or copying a byte.
    assert result.returncode != 0
    assert not migration.manifest.exists()
    assert not any(quoted.iterdir())


@pytest.mark.parametrize(
    "statement",
    ["Stop all writers", "copy, never move", "verify before switching",
     "explicitly approves deletion", "Rollback", "not run on a real host", "storage_key",
     "root-owned", "export SRC DEST MANIFEST ENV_FILE"],
)
def test_guide_when_describing_migration_keeps_safety_contract(statement: str) -> None:
    # Given the operator migration guide.
    text = GUIDE.read_text(encoding="utf-8")
    # When reviewing its safety requirements.
    # Then the essential operational constraint remains explicit.
    assert statement.casefold() in text.casefold()

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "restore_verify.py"


def test_checksummed_corrupt_dump_cannot_restore_to_fresh_database(
    tmp_path: Path,
) -> None:
    # Given
    if shutil.which("docker") is None:
        pytest.skip("docker is required for PostgreSQL restore validation")
    bundle = tmp_path / "bundle"
    (bundle / "media").mkdir(parents=True)
    dump = bundle / "database.dump"
    dump.write_bytes(b"PGDMP\x01\x0e truncated")
    digest = hashlib.sha256(dump.read_bytes()).hexdigest()
    manifest = {
        "format_version": 1,
        "created_at": "2026-10-16T00:00:00Z",
        "release_sha": "abc123def456",
        "image_digests": ["postgres@sha256:" + "d" * 64],
        "alembic_revision": "revision-1",
        "secret_fingerprint": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "database_dump": "database.dump",
        "media_root": "media",
        "files": {"database.dump": digest},
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    container = f"restore-corrupt-{uuid.uuid4().hex[:12]}"

    try:
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                container,
                "-e",
                "POSTGRES_PASSWORD=restore",
                "postgres:16.15-alpine@sha256:aa6cb33cf4c19c765cefcf48d96176258800870a9a1e930ad126200f9d825139",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        for _ in range(60):
            logs = subprocess.run(
                ["docker", "logs", container],
                capture_output=True,
                text=True,
                check=False,
            )
            ready = subprocess.run(
                [
                    "docker", "exec", container, "psql", "-U", "postgres",
                    "-d", "postgres", "-At", "-c", "SELECT 1",
                ],
                capture_output=True,
                check=False,
            )
            initialization_complete = (
                "PostgreSQL init process complete; ready for start up."
                in logs.stdout + logs.stderr
            )
            if initialization_complete and ready.returncode == 0:
                break
            time.sleep(1)
        else:
            pytest.fail("PostgreSQL did not become ready")
        subprocess.run(
            ["docker", "exec", container, "createdb", "-U", "postgres", "restore_target"],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            [
                "docker", "exec", container, "psql", "-U", "postgres", "-d", "postgres",
                "-c", "CREATE TABLE restore_sentinel(value text); INSERT INTO restore_sentinel VALUES ('preserve');",
            ],
            capture_output=True,
            check=True,
        )

        # When
        validation = subprocess.run(
            [sys.executable, str(SCRIPT), "verify", str(bundle), "--expected-alembic-revision", "revision-1", "--expected-secret-fingerprint", "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"],
            capture_output=True,
            text=True,
            check=False,
        )
        restore = subprocess.run(
            ["docker", "exec", "-i", container, "pg_restore", "-U", "postgres", "-d", "restore_target"],
            input=dump.read_bytes(),
            capture_output=True,
            check=False,
        )

        # Then
        assert validation.returncode != 0
        assert "database dump is not a valid custom-format archive" in validation.stderr
        assert restore.returncode != 0
        corruption_errors = (
            b"input file is too short",
            b"unexpected end of file",
            b"sanity check on integer size",
        )
        assert any(message in restore.stderr for message in corruption_errors)
        sentinel = subprocess.run(
            [
                "docker", "exec", container, "psql", "-U", "postgres", "-d", "postgres",
                "-At", "-c", "SELECT value FROM restore_sentinel",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        assert sentinel.stdout.strip() == "preserve"
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container], capture_output=True, check=False
        )

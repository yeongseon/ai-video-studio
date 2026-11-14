#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

# ─── How to run ───
# 1. Install the API requirements and PostgreSQL client tools.
# 2. Run: python scripts/restore_drill.py
# 3. The drill creates and removes only disposable Docker containers.
# ──────────────────

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from restore_drill_runtime import (
    DrillError,
    RuntimeFixture,
    api_request,
    docker,
    image_digest,
    launch_api,
    launch_provider_guard,
    mapped_postgres_url,
    recovery_states,
    restored_timeline,
    run,
    run_reconciler,
    seed_production_fixture,
    wait_for_api,
    wait_for_postgres,
    validate_release_manifest,
)
from restore_drill_process import ContainerNameCollisionError, child_environment
from restore_drill_render import timeline_render_program


ROOT = Path(__file__).parents[1]
POSTGRES_IMAGE = "postgres:16.16-alpine@sha256:0adbf25a9e96d38823bb813f3ac0ed41fadb665c88f1df7fe6dbfb060cbdc106"
FFMPEG_IMAGE = "jrottenberg/ffmpeg:7.1-alpine"
PASSWORD = "synthetic-local-only"
FINGERPRINT = "sha256:" + "a" * 64


class ChildProcess(Protocol):
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...


@dataclass(frozen=True, slots=True)
class ChildShutdownError(DrillError):
    pass


def create_media(media: Path) -> None:
    media.mkdir(parents=True)
    mount = f"{media.resolve()}:/media"
    docker("run", "--rm", "-v", mount, FFMPEG_IMAGE, "-f", "lavfi", "-i", "color=c=blue:s=320x240", "-frames:v", "1", "/media/image.png")
    docker("run", "--rm", "-v", mount, FFMPEG_IMAGE, "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "/media/audio.wav")
    docker("run", "--rm", "-v", mount, FFMPEG_IMAGE, "-loop", "1", "-i", "/media/image.png", "-i", "/media/audio.wav", "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "/media/final.mp4")


def render_restored_timeline(
    database_url: str, restored: Path, fixture: RuntimeFixture
) -> tuple[int, RuntimeFixture, dict[str, str | int]]:
    environment = child_environment({
        "DATABASE_URL": database_url,
        "ARTIFACT_ROOT": str(restored),
        "STORAGE_BACKEND": "local",
        "PYTHONPATH": os.pathsep.join(
            str(ROOT / path)
            for path in (
                "apps/worker-orchestrator",
                "packages/creator-domain",
                "packages/creator-service",
                "packages/creator-provider",
            )
        ),
    })
    result = run(
        sys.executable,
        "-c",
        timeline_render_program(restored, fixture),
        cwd=ROOT / "apps/worker-orchestrator",
        env=environment,
    )
    rendered = json.loads(result.stdout.splitlines()[-1])
    artifact = restored / "final.mp4"
    evidence = {
        "checksum": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "size_bytes": artifact.stat().st_size,
        "audio_path": str(rendered["audio_path"]),
    }
    return int(rendered["count"]), replace(
        fixture, artifact_id=int(rendered["artifact_id"])
    ), evidence


def verify_http(runtime: RuntimeFixture, media: Path) -> None:
    owner = {"X-API-Key": runtime.owner_key}
    foreign = {"X-API-Key": runtime.foreign_key}
    revoked = {"X-API-Key": runtime.revoked_key}
    project_path = f"/api/creator/projects/{runtime.project_id}"
    if api_request(runtime.port, project_path, owner)[0] != 200:
        raise DrillError("owner project request failed")
    if api_request(runtime.port, project_path, foreign)[0] != 404:
        raise DrillError("cross-workspace project request was not hidden")
    if api_request(runtime.port, project_path, revoked)[0] != 401:
        raise DrillError("revoked key was accepted")
    if api_request(runtime.port, project_path, {})[0] != 401:
        raise DrillError("missing key was accepted")
    timeline_path = f"{project_path}/timeline"
    timeline_status, timeline_body = api_request(runtime.port, timeline_path, owner)
    if timeline_status != 200 or json.loads(timeline_body)["id"] != "timeline-restored":
        raise DrillError("owner could not open restored Timeline")
    if api_request(runtime.port, timeline_path, foreign)[0] != 404:
        raise DrillError("foreign user could open restored Timeline")
    media_path = f"/api/creator/workspaces/{runtime.workspace_id}/assets/{runtime.media_id}/content"
    video_path = f"/api/creator/runs/{runtime.run_id}/artifacts/{runtime.artifact_id}/download"
    media_status, media_body = api_request(
        runtime.port, media_path, owner,
    )
    video_status, video_body = api_request(
        runtime.port, video_path, owner,
    )
    if media_status != 200 or video_status != 200:
        raise DrillError("real API protected download failed")
    if hashlib.sha256(media_body).digest() != hashlib.sha256((media / "image.png").read_bytes()).digest():
        raise DrillError("downloaded media checksum mismatch")
    if hashlib.sha256(video_body).digest() != hashlib.sha256((media / "final.mp4").read_bytes()).digest():
        raise DrillError("downloaded final MP4 checksum mismatch")
    for path in (media_path, video_path):
        if api_request(runtime.port, path, foreign)[0] != 404:
            raise DrillError("foreign protected download was not hidden")
        if api_request(runtime.port, path, revoked)[0] != 401:
            raise DrillError("revoked key accessed protected download")
        if api_request(runtime.port, path, {})[0] != 401:
            raise DrillError("missing key accessed protected download")
    downloaded = media / "downloaded-final.mp4"
    downloaded.write_bytes(video_body)
    docker("run", "--rm", "-v", f"{media.resolve()}:/media:ro", FFMPEG_IMAGE, "-v", "error", "-i", "/media/downloaded-final.mp4", "-f", "null", "-")
    print("real API auth: owner=200 workspace-b=404 revoked=401 missing=401 timeline=200/404")
    print("real API downloads: media=200 final-mp4=200 foreign=404 revoked=401 missing=401 checksum=verified ffprobe=verified")


def stop_process(process: ChildProcess) -> None:
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            raise ChildShutdownError("child process remained alive after kill escalation") from None


def raise_cleanup_error(cleanup_error: ChildShutdownError | None) -> None:
    if sys.exc_info()[0] is None and cleanup_error is not None:
        raise cleanup_error


def main() -> int:
    if shutil.which("docker") is None:
        print("SKIP: docker is required", file=sys.stderr)
        return 77
    suffix = hashlib.sha256(str(time.time_ns()).encode()).hexdigest()[:10]
    source, target = f"restore-source-{suffix}", f"restore-target-{suffix}"
    for container in (source, target):
        if docker("ps", "-a", "--filter", f"name=^/{container}$", "--format", "{{.Names}}").stdout.strip():
            raise ContainerNameCollisionError(f"container already exists: {container}")
    created_containers: list[str] = []
    durations: dict[str, float] = {}
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="restore-drill-") as temporary:
        root = Path(temporary)
        bundle, restored = root / "bundle", root / "restored"
        media = bundle / "media"
        api_process: subprocess.Popen[bytes] | None = None
        guard_process: subprocess.Popen[bytes] | None = None
        try:
            create_media(media)
            for container in (source, target):
                docker("run", "-d", "--name", container, "-p", "127.0.0.1::5432", "-e", "POSTGRES_USER=restore", "-e", f"POSTGRES_PASSWORD={PASSWORD}", "-e", "POSTGRES_DB=restore", POSTGRES_IMAGE)
                created_containers.append(container)
                wait_for_postgres(container)
            source_url = mapped_postgres_url(source, PASSWORD)
            target_url = mapped_postgres_url(target, PASSWORD)
            phase = time.monotonic()
            run(sys.executable, "-m", "alembic", "upgrade", "head", cwd=ROOT / "apps/api", env={"DATABASE_URL": source_url})
            alembic_head = run(
                sys.executable,
                "-m",
                "alembic",
                "current",
                cwd=ROOT / "apps/api",
                env={"DATABASE_URL": source_url},
            ).stdout.split()[0]
            fixture = seed_production_fixture(source, media)
            docker("exec", source, "pg_dump", "-U", "restore", "-d", "restore", "--format=custom", "--file=/tmp/database.dump")
            docker("cp", f"{source}:/tmp/database.dump", str(bundle / "database.dump"))
            durations["backup_seconds"] = time.monotonic() - phase
            manifest = {
                "format_version": 1, "created_at": "synthetic", "release_sha": os.environ.get("RESTORE_HEAD_SHA", run("git", "rev-parse", "HEAD", cwd=ROOT).stdout.strip()),
                "image_digests": [image_digest(POSTGRES_IMAGE), image_digest(FFMPEG_IMAGE), *validate_release_manifest(Path(os.environ["RESTORE_RELEASE_MANIFEST"]))],
                "alembic_revision": alembic_head, "secret_fingerprint": FINGERPRINT,
                "database_dump": "database.dump", "media_root": "media",
                "files": {path.relative_to(bundle).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in (bundle / "database.dump", *sorted(media.iterdir()))},
            }
            (bundle / "manifest.json").write_text(json.dumps(manifest))
            verifier = Path(__file__).with_name("restore_verify.py")
            run(sys.executable, str(verifier), "verify", str(bundle), "--expected-alembic-revision", alembic_head, "--expected-secret-fingerprint", FINGERPRINT)
            phase = time.monotonic()
            docker("cp", str(bundle / "database.dump"), f"{target}:/tmp/database.dump")
            docker("exec", target, "pg_restore", "-U", "restore", "-d", "restore", "--clean", "--if-exists", "/tmp/database.dump")
            run(sys.executable, "-m", "alembic", "upgrade", "head", cwd=ROOT / "apps/api", env={"DATABASE_URL": target_url})
            run(sys.executable, str(verifier), "restore-media", str(bundle), "--target", str(restored), "--expected-alembic-revision", alembic_head, "--expected-secret-fingerprint", FINGERPRINT)
            if os.environ.get("RESTORE_CORRUPT_AUDIO_METADATA") == "delete":
                docker("exec", target, "psql", "-U", "restore", "-d", "restore", "-c", "DELETE FROM creator_artifacts WHERE id=700")
            asset_directory = restored / "workspaces" / str(fixture.workspace_id) / "assets"
            asset_directory.mkdir(parents=True)
            shutil.copy2(restored / "image.png", asset_directory / "image.png")
            timeline = restored_timeline(target, fixture.project_id)
            (restored / "final.mp4").unlink()
            rendered_segments, fixture, registered_artifact = render_restored_timeline(
                target_url, restored, fixture
            )
            if rendered_segments != len(timeline):
                raise DrillError("production renderer did not consume the complete Timeline")
            durations["restore_seconds"] = time.monotonic() - phase
            guard_process, guard_port = launch_provider_guard(root)
            api_process, fixture = launch_api(target_url, restored, fixture, root, guard_port)
            wait_for_api(fixture.port, api_process, root / "api.log")
            verify_http(fixture, restored)
            reconciliation = run_reconciler(target_url)
            states = recovery_states(target)
            if reconciliation.get("rolled_back") != 1 or reconciliation.get("stuck_failed") != 1 or reconciliation.get("errors"):
                raise DrillError(f"unexpected reconciler result: {reconciliation}")
            state_map = dict(line.split("=", 1) for line in states.splitlines())
            if state_map != {"restore-pending": "failed", "restore-queued": "queued", "restore-running": "failed"}:
                raise DrillError(f"unexpected recovered task states: {states}")
            if (root / "provider-invoked").exists():
                raise DrillError("provider-call guard detected an invocation")
            print("production schema: migrated")
            print("post-restore render: restored Timeline image audio -> final.mp4")
            print("provider guard: armed and untouched")
            print("broker recovery: pending=failed queued=queued running=failed")
            print("provider calls: 0")
            evidence = {"restore_head_sha": manifest["release_sha"], "release_image_digests": manifest["image_digests"], "phase_durations_seconds": {**durations, "total": time.monotonic() - started}, "reconciliation": reconciliation, "recovered_task_states": states.splitlines(), "credential_free": True, "restored_timeline_segments": len(timeline), "registered_artifact": registered_artifact}
            print("RESTORE_EVIDENCE=" + json.dumps(evidence, sort_keys=True))
            print("RESTORE DRILL PASSED")
            return 0
        finally:
            cleanup_error: ChildShutdownError | None = None
            if api_process is not None:
                try:
                    stop_process(api_process)
                except ChildShutdownError as exc:
                    cleanup_error = exc
            if guard_process is not None:
                try:
                    stop_process(guard_process)
                except ChildShutdownError as exc:
                    if cleanup_error is None:
                        cleanup_error = exc
            if created_containers:
                subprocess.run(["docker", "rm", "-f", *created_containers], env=child_environment(), capture_output=True, check=False)
            raise_cleanup_error(cleanup_error)


if __name__ == "__main__":
    raise SystemExit(main())

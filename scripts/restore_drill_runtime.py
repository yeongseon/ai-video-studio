from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path

from restore_drill_process import DrillError, TIMEOUT, child_environment, docker, run

IMAGE_DIGEST = re.compile(r"[^@]+@sha256:([0-9a-f]{64})\Z")


@dataclass(frozen=True, slots=True)
class RuntimeFixture:
    owner_key: str
    foreign_key: str
    revoked_key: str
    workspace_id: int
    project_id: int
    run_id: int
    media_id: int
    artifact_id: int
    port: int = 0


def wait_for_postgres(container: str) -> None:
    for _ in range(60):
        if subprocess.run(["docker", "exec", container, "pg_isready", "-U", "restore"], env=child_environment(), capture_output=True, check=False).returncode == 0:
            return
        time.sleep(1)
    raise DrillError("postgres did not become ready")


def mapped_postgres_url(container: str, password: str) -> str:
    binding = docker("port", container, "5432/tcp").stdout.strip()
    port = binding.rsplit(":", 1)[1]
    return f"postgresql://restore:{password}@127.0.0.1:{port}/restore"


def image_digest(image: str) -> str:
    docker("pull", image)
    repo_digests = docker(
        "image", "inspect", image, "--format={{join .RepoDigests \"\\n\"}}"
    ).stdout.splitlines()
    digest = next((value for value in repo_digests if "@sha256:" in value), None)
    if digest is None:
        raise DrillError(f"image lacks registry RepoDigest: {image}")
    return digest


def validate_release_manifest(path: Path) -> list[str]:
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict) or set(raw) != {"api", "worker", "web"}:
        raise DrillError("release manifest must identify api, worker, and web images")
    digests: list[str] = []
    for name in ("api", "worker", "web"):
        value = raw[name]
        if not isinstance(value, str):
            raise DrillError(f"release image digest is invalid: {name}")
        match = IMAGE_DIGEST.fullmatch(value)
        if match is None:
            raise DrillError(f"release image digest is invalid: {name}")
        hash_value = match.group(1)
        if len(set(hash_value)) == 1:
            raise DrillError(f"release image digest is a placeholder: {name}")
        digests.append(value)
    return digests


def seed_production_fixture(container: str, media: Path) -> RuntimeFixture:
    owner, foreign, revoked = "owner-restore-key", "foreign-restore-key", "revoked-restore-key"
    hashes = [hashlib.sha256(value.encode()).hexdigest() for value in (owner, foreign, revoked)]
    image_hash = hashlib.sha256((media / "image.png").read_bytes()).hexdigest()
    audio_hash = hashlib.sha256((media / "audio.wav").read_bytes()).hexdigest()
    video_hash = hashlib.sha256((media / "final.mp4").read_bytes()).hexdigest()
    timeline = json.dumps(
        [
            {
                "id": "segment-restored",
                "scene_id": "scene-restored",
                "asset_id": 601,
                "timeline_start_seconds": 0,
                "duration_seconds": 1,
                "trim_start_seconds": None,
                "trim_end_seconds": None,
                "fit_mode": "cover",
                "transition": None,
            },
            {
                "id": "segment-restored-second",
                "scene_id": "scene-restored-second",
                "asset_id": 602,
                "timeline_start_seconds": 1,
                "duration_seconds": 1,
                "trim_start_seconds": None,
                "trim_end_seconds": None,
                "fit_mode": "cover",
                "transition": "cut",
            },
        ],
        separators=(",", ":"),
    )
    sql = f"""
INSERT INTO users(id,email,name,auth_provider,auth_subject) VALUES (101,'a@example.invalid','A','api_key','restore-a'),(102,'b@example.invalid','B','api_key','restore-b');
INSERT INTO workspaces(id,name,slug,owner_id) VALUES (201,'A','restore-a',101),(202,'B','restore-b',102);
INSERT INTO workspace_members(workspace_id,user_id,role) VALUES (201,101,'owner'),(202,102,'owner');
INSERT INTO api_keys(id,user_id,key_hash,name,revoked_at) VALUES (301,101,'{hashes[0]}','owner',NULL),(302,102,'{hashes[1]}','foreign',NULL),(303,101,'{hashes[2]}','revoked',NOW());
INSERT INTO creator_projects(id,title,source_type,status,workspace_id) VALUES (401,'Restored project','idea','active',201);
INSERT INTO creator_runs(id,project_id,current_stage,status,workspace_id,version,metadata_json) VALUES (501,401,'FINAL_REVIEW','paused',201,1,'{{"render_source":"timeline","render_timeline_revision":1}}');
INSERT INTO creator_timelines(project_id,workspace_id,timeline_id,revision,segments_json) VALUES (401,201,'timeline-restored',1,'{timeline}');
INSERT INTO creator_stage_reviews(run_id,stage_name,review_status,reviewer,notes) VALUES (501,'TIMELINE_REVIEW','approved','101','{{"timeline_id":"timeline-restored","revision":1}}');
INSERT INTO creator_media_assets(id,workspace_id,project_id,run_id,media_type,origin,storage_key,mime_type,metadata) VALUES
 (601,201,401,501,'IMAGE','UPLOADED','workspaces/201/assets/image.png','image/png','{{"sha256":"{image_hash}","storage_provider":"local"}}'),
 (602,201,401,501,'IMAGE','UPLOADED','workspaces/201/assets/image.png','image/png','{{"sha256":"{image_hash}","storage_provider":"local"}}');
INSERT INTO creator_artifacts(id,run_id,artifact_type,file_path,file_size_bytes,mime_type,storage_key,content_type,size_bytes,workspace_id,project_id,storage_provider,checksum) VALUES
 (700,501,'audio','audio.wav',0,'audio/wav','audio.wav','audio/wav',0,201,401,'local','{audio_hash}'),
 (701,501,'video','final.mp4',0,'video/mp4','final.mp4','video/mp4',0,201,401,'local','{video_hash}');
INSERT INTO creator_runs(id,project_id,current_stage,status,workspace_id,version) VALUES (502,401,'SCRIPT_GENERATING','running',201,1),(503,401,'RENDER_GENERATING','running',201,1),(504,401,'FINAL_REVIEW','paused',201,1);
INSERT INTO creator_run_tasks(run_id,task_type,celery_task_id,status,started_at,created_at,claim_token) VALUES
 (502,'generate_script','restore-pending','pending',NULL,NOW()-INTERVAL '10 minutes',NULL),
 (503,'render_video','restore-running','running',NOW()-INTERVAL '30 minutes',NOW()-INTERVAL '30 minutes','restore-claim'),
 (504,'render_video','restore-queued','queued',NULL,NOW(),NULL);
"""
    docker("exec", "-i", container, "psql", "-U", "restore", "-d", "restore", "-c", sql)
    return RuntimeFixture(owner, foreign, revoked, 201, 401, 501, 601, 701)


def restored_timeline(container: str, project_id: int) -> list[dict[str, str | int | float | None]]:
    payload = docker(
        "exec",
        container,
        "psql",
        "-U",
        "restore",
        "-d",
        "restore",
        "-At",
        "-c",
        f"SELECT segments_json FROM creator_timelines WHERE project_id={project_id} AND revision=1",
    ).stdout.strip()
    timeline = json.loads(payload)
    if not isinstance(timeline, list) or not timeline:
        raise DrillError("restored approved Timeline must contain segments")
    return timeline


def register_rendered_artifact(
    container: str, fixture: RuntimeFixture, artifact: Path
) -> dict[str, str | int]:
    checksum = hashlib.sha256(artifact.read_bytes()).hexdigest()
    size_bytes = artifact.stat().st_size
    docker(
        "exec",
        container,
        "psql",
        "-U",
        "restore",
        "-d",
        "restore",
        "-c",
        "UPDATE creator_artifacts "
        f"SET checksum='{checksum}', file_size_bytes={size_bytes}, size_bytes={size_bytes} "
        f"WHERE id={fixture.artifact_id}",
    )
    return {"checksum": checksum, "size_bytes": size_bytes}


def run_reconciler(database_url: str) -> dict[str, int | list[str]]:
    environment = child_environment({
        "DATABASE_URL": database_url,
        "PYTHONPATH": os.pathsep.join(
            str(Path(__file__).parents[1] / path)
            for path in (
                "packages/creator-domain",
                "packages/creator-service",
                "packages/creator-provider",
                "apps/worker-orchestrator",
            )
        ),
    })
    completed = run(
        os.sys.executable,
        "-c",
        "import json; from tasks.reconcile_stale_dispatches import reconcile_stale_dispatches; print(json.dumps(reconcile_stale_dispatches()))",
        cwd=Path(__file__).parents[1] / "apps/worker-orchestrator",
        env=environment,
    )
    import json

    result = json.loads(completed.stdout.splitlines()[-1])
    if not isinstance(result, dict):
        raise DrillError("reconciler returned invalid evidence")
    return result


def recovery_states(container: str) -> str:
    return docker(
        "exec", container, "psql", "-U", "restore", "-d", "restore", "-At", "-c",
        "SELECT celery_task_id || '=' || status FROM creator_run_tasks ORDER BY celery_task_id",
    ).stdout.strip()


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def launch_provider_guard(root: Path) -> tuple[subprocess.Popen[bytes], int]:
    port = _free_port()
    marker = root / "provider-invoked"
    program = (
        "from http.server import BaseHTTPRequestHandler,HTTPServer;"
        "from pathlib import Path;"
        f"marker=Path({str(marker)!r});"
        "handler=type('Guard',(BaseHTTPRequestHandler,),{"
        "'do_GET':lambda self:(marker.write_text(self.path),self.send_error(500)),"
        "'do_POST':lambda self:(marker.write_text(self.path),self.send_error(500)),"
        "'log_message':lambda *args:None});"
        f"HTTPServer(('127.0.0.1',{port}),handler).serve_forever()"
    )
    return subprocess.Popen([os.sys.executable, "-c", program], env=child_environment()), port


def launch_api(database_url: str, artifact_root: Path, fixture: RuntimeFixture, root: Path, guard_port: int) -> tuple[subprocess.Popen[bytes], RuntimeFixture]:
    port = _free_port()
    repository = Path(__file__).parents[1]
    env = child_environment({
        "DATABASE_URL": database_url, "ARTIFACT_ROOT": str(artifact_root),
        "STORAGE_BACKEND": "local", "ENVIRONMENT": "development", "REDIS_URL": "",
        "PYTHONPATH": os.pathsep.join(
            str(repository / path)
            for path in (
                "apps/api/src",
                "apps/worker-orchestrator",
                "packages/creator-domain",
                "packages/creator-service",
                "packages/creator-provider",
            )
        ),
        "OPENAI_API_KEY": "", "ANTHROPIC_API_KEY": "", "GOOGLE_API_KEY": "",
        "ELEVENLABS_API_KEY": "", "STABILITY_API_KEY": "",
        "OLLAMA_BASE_URL": f"http://127.0.0.1:{guard_port}",
        "STABLE_DIFFUSION_BASE_URL": f"http://127.0.0.1:{guard_port}",
        "TTS_QWEN3_BASE_URL": f"http://127.0.0.1:{guard_port}",
        "STT_WHISPER_BASE_URL": f"http://127.0.0.1:{guard_port}",
        "TTS_COSYVOICE_BASE_URL": f"http://127.0.0.1:{guard_port}",
    })
    log = (root / "api.log").open("wb")
    process = subprocess.Popen(
        [os.sys.executable, "-m", "uvicorn", "shorts_api.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=repository / "apps/api", env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    return process, replace(fixture, port=port)


def wait_for_api(port: int, process: subprocess.Popen[bytes], log_path: Path) -> None:
    for _ in range(100):
        if process.poll() is not None:
            raise DrillError(
                "real API exited during startup\n" + log_path.read_text(errors="replace")
            )
        if api_request(port, "/healthz", {})[0] == 200:
            return
        time.sleep(0.1)
    raise DrillError("real API did not become ready")


def api_request(port: int, path: str, headers: dict[str, str]) -> tuple[int, bytes]:
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except urllib.error.URLError:
        return 0, b""

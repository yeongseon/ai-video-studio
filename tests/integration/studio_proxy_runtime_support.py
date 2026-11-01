from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from typing import NoReturn
from uuid import uuid4

import anyio
import asyncpg
import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _change_schema(database_url: str, schema: str, drop: bool) -> None:
    connection = await asyncpg.connect(database_url)
    try:
        statement = f'DROP SCHEMA "{schema}" CASCADE' if drop else f'CREATE SCHEMA "{schema}"'
        await connection.execute(statement)
    finally:
        await connection.close()


async def _provision_identity(database_url: str, schema: str, token: str) -> int:
    connection = await asyncpg.connect(database_url, server_settings={"search_path": schema})
    try:
        user_id = await connection.fetchval(
            "INSERT INTO users (auth_subject) VALUES ($1) RETURNING id", uuid4().hex,
        )
        workspace_id = await connection.fetchval(
            "INSERT INTO workspaces (name, slug, owner_id) VALUES ('Proxy runtime', $1, $2) RETURNING id",
            uuid4().hex,
            user_id,
        )
        await connection.execute(
            "INSERT INTO workspace_members (workspace_id, user_id) VALUES ($1, $2)",
            workspace_id,
            user_id,
        )
        await connection.execute(
            "INSERT INTO api_keys (user_id, key_hash, name) VALUES ($1, $2, 'proxy-runtime')",
            user_id,
            hashlib.sha256(token.encode()).hexdigest(),
        )
        return int(workspace_id)
    finally:
        await connection.close()


def _await_health(client: httpx.Client, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        assert process.poll() is None, "runtime process exited during startup"
        try:
            if client.get("/healthz").status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.1)
    pytest.fail("runtime did not become healthy within 30 seconds")


@dataclass(frozen=True, slots=True)
class StudioProxyRuntime:
    origin: str
    api_origin: str
    workspace_id: int
    foreign_workspace_id: int
    token: str
    body_temp: Path
    staging_parent: Path
    foreign_token: str = ""
    database_url: str = ""
    schema: str = ""
    artifact_root: Path = Path()
    environment: str = "development"


@dataclass(frozen=True, slots=True)
class Prerequisites:
    nginx: str
    database_url: str
    studio_dist: Path


def _unavailable(reason: str, *, required_in_ci: bool) -> NoReturn:
    if required_in_ci and os.getenv("GITHUB_ACTIONS") == "true":
        pytest.fail(reason, pytrace=False)
    pytest.skip(reason)


def prerequisites_or_skip(*, required_in_ci: bool = False) -> Prerequisites:
    nginx = shutil.which("nginx")
    ffmpeg = shutil.which("ffmpeg")
    database_url = os.getenv("FIRST_SHORT_TEST_DATABASE_URL")
    if nginx is None or ffmpeg is None or database_url is None:
        _unavailable(
            "nginx, ffmpeg, and a disposable PostgreSQL endpoint are required", required_in_ci=required_in_ci,
        )
    studio_dist = ROOT / "apps/studio-web/dist"
    if not (studio_dist / "index.html").is_file():
        _unavailable("the built Studio is required", required_in_ci=required_in_ci)
    return Prerequisites(nginx, database_url, studio_dist)


def _production_environment(origin: str) -> dict[str, str]:
    # Satisfies the production startup safety checks. Nothing on the media routes connects to Redis.
    return {
        "ADMIN_API_KEY": f"proxy-admin-{uuid4().hex}",
        "CORS_ORIGINS": origin,
        "REDIS_URL": "redis://127.0.0.1:1/0",
    }


@contextmanager
def open_studio_proxy(
    root: Path, needs: Prerequisites, *, environment: str = "development",
) -> Iterator[StudioProxyRuntime]:
    nginx, database_url, studio_dist = needs.nginx, needs.database_url, needs.studio_dist
    schema = f"studio_proxy_{uuid4().hex}"
    token = f"proxy-runtime-{uuid4().hex}"
    foreign_token = f"proxy-foreign-{uuid4().hex}"
    artifact_root = root / "artifacts"
    body_temp = root / "body"
    artifact_root.mkdir()
    body_temp.mkdir()
    nginx_port = _free_port()
    api_port = _free_port()
    while api_port == nginx_port:
        api_port = _free_port()
    origin = f"http://127.0.0.1:{nginx_port}"
    api_origin = f"http://127.0.0.1:{api_port}"
    env = {
        **os.environ,
        "DATABASE_URL": f"{database_url}{'&' if '?' in database_url else '?'}search_path={schema}",
        "ARTIFACT_ROOT": str(artifact_root),
        "STORAGE_BACKEND": "local",
        "ENVIRONMENT": environment,
        "TMPDIR": str(root),
        "PYTHONPATH": os.pathsep.join(str(ROOT / path) for path in (
            "packages/creator-domain", "packages/creator-service", "packages/creator-provider", "apps/api/src",
        )),
        **(_production_environment(origin) if environment == "production" else {}),
    }
    anyio.run(_change_schema, database_url, schema, False)
    processes: list[subprocess.Popen[bytes]] = []
    api_log = (root / "api.log").open("wb")
    try:
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT / "apps/api",
            env={**env, "DATABASE_URL": database_url, "PGOPTIONS": f"-c search_path={schema}"},
            check=True,
            capture_output=True,
            timeout=60,
        )
        workspace_id = anyio.run(_provision_identity, database_url, schema, token)
        foreign_workspace_id = anyio.run(_provision_identity, database_url, schema, foreign_token)
        api = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "shorts_api.main:app", "--host", "127.0.0.1", "--port", str(api_port)],
            cwd=ROOT / "apps/api",
            env=env,
            stdout=api_log,
            stderr=subprocess.STDOUT,
        )
        processes.append(api)
        with httpx.Client(base_url=api_origin, timeout=5) as client:
            _await_health(client, api)

        template = (ROOT / "apps/studio-web/nginx.conf.template").read_text()
        configured = template.replace("${API_KEY}", token)
        configured = configured.replace("listen 8080;", f"listen 127.0.0.1:{nginx_port};")
        configured = configured.replace("http://api:8000", api_origin)
        configured = configured.replace("/usr/share/nginx/html", str(studio_dist))
        config = root / "nginx.conf"
        config.write_text(
            f"pid {root}/nginx.pid; error_log {root}/nginx-error.log;\n"
            f"events {{}}\nhttp {{ include /etc/nginx/mime.types; access_log off; "
            f"client_body_temp_path {body_temp}; "
            f"proxy_temp_path {root}/proxy;\n{configured}\n}}\n",
        )
        nginx_process = subprocess.Popen(
            [nginx, "-p", str(root), "-c", str(config), "-g", "daemon off;"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        processes.append(nginx_process)
        with httpx.Client(base_url=origin, timeout=5) as client:
            _await_health(client, nginx_process)
        yield StudioProxyRuntime(
            origin, api_origin, workspace_id, foreign_workspace_id, token, body_temp, root,
            foreign_token=foreign_token,
            database_url=database_url,
            schema=schema,
            artifact_root=artifact_root,
            environment=environment,
        )
    finally:
        for process in reversed(processes):
            process.terminate()
            process.wait(timeout=15)
        api_log.close()
        anyio.run(_change_schema, database_url, schema, True)


@pytest.fixture(scope="module")
def studio_proxy_runtime(tmp_path_factory: pytest.TempPathFactory) -> Iterator[StudioProxyRuntime]:
    needs = prerequisites_or_skip()
    root = tmp_path_factory.mktemp("studio-proxy-runtime")
    with open_studio_proxy(root, needs) as runtime:
        yield runtime

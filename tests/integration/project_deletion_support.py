"""Schema-isolated PostgreSQL and real API dependencies for deletion proofs."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
import hashlib
import importlib
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import asyncpg
import httpx
import pytest
import pytest_asyncio
from creator_service import db
from creator_service.postgres_project_storage import PostgresProjectStorage
from creator_service.postgres_run_storage import PostgresRunStorage
from creator_service.postgres_workspace_storage import PostgresWorkspaceStorage
from creator_service.project_service import ProjectService
from creator_service.run_service import RunService
from creator_service.workspace_service import WorkspaceService

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class DeletionDatabase:
    pool: asyncpg.Pool
    url: str
    schema: str

    def migrate(self, direction: str, revision: str) -> None:
        subprocess.run(
            [sys.executable, "-m", "alembic", direction, revision],
            cwd=ROOT / "apps/api",
            env={**os.environ, "DATABASE_URL": self.url, "PGOPTIONS": f"-c search_path={self.schema}"},
            check=True, capture_output=True, timeout=60,
        )


@pytest_asyncio.fixture
async def deletion_database(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[DeletionDatabase]:
    url = os.getenv("FIRST_SHORT_TEST_DATABASE_URL")
    if not url:
        if os.getenv("GITHUB_ACTIONS") == "true":
            pytest.fail("FIRST_SHORT_TEST_DATABASE_URL must name disposable PostgreSQL", pytrace=False)
        pytest.skip("FIRST_SHORT_TEST_DATABASE_URL must name disposable PostgreSQL")
    schema = f"project_deletion_{uuid4().hex}"
    async with asyncpg.create_pool(url, min_size=1, max_size=1) as admin:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        try:
            async with asyncpg.create_pool(
                url, min_size=2, max_size=4, server_settings={"search_path": schema},
            ) as pool:
                database = DeletionDatabase(pool, url, schema)
                database.migrate("upgrade", "head")

                async def get_pool() -> asyncpg.Pool:
                    return pool

                monkeypatch.setattr(db, "get_pool", get_pool)
                await pool.execute("""
                    INSERT INTO users (id, auth_subject) VALUES (1, 'deletion-owner'), (2, 'foreign-owner');
                    INSERT INTO workspaces (id, name, slug, owner_id) VALUES
                        (1, 'Owned', 'owned', 1), (2, 'Foreign', 'foreign', 2);
                    INSERT INTO workspace_members (workspace_id, user_id) VALUES (1, 1), (2, 2);
                    INSERT INTO creator_projects (id, workspace_id, title) VALUES
                        (1, 1, 'Delete me'), (2, 2, 'Keep me');
                """)
                for user_id, token in ((1, "deletion-owner-key"), (2, "deletion-foreign-key")):
                    await pool.execute(
                        "INSERT INTO api_keys (user_id, key_hash, name) VALUES ($1, $2, 'deletion-test')",
                        user_id, hashlib.sha256(token.encode()).hexdigest(),
                    )
                yield database
        finally:
            await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest_asyncio.fixture
async def deletion_client(
    deletion_database: DeletionDatabase, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.syspath_prepend(str(ROOT / "apps/api/src"))
    from shorts_api.main import app
    from shorts_api import auth

    projects = ProjectService(PostgresProjectStorage())
    runs = RunService(PostgresRunStorage())
    monkeypatch.setattr(importlib.import_module("creator_service.project_service"), "project_service", projects)
    monkeypatch.setattr(auth, "workspace_service", WorkspaceService(PostgresWorkspaceStorage()))
    for name in ("creator_projects", "creator_runs_core", "creator_script"):
        module = importlib.import_module(f"shorts_api.routes.{name}")
        if hasattr(module, "project_service"):
            monkeypatch.setattr(module, "project_service", projects)
        monkeypatch.setattr(module, "run_service", runs)
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    # Keep the production middleware and project-access dependency: real keys/memberships enforce IDOR.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=10,
        headers={"X-API-Key": "deletion-owner-key"},
    ) as client:
        yield client

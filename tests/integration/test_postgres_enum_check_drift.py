import re
from typing import Final, get_args

import pytest
from creator_domain.models.project import Project
from creator_domain.models.pipeline_run import PipelineRun
from creator_domain.models.run_task import RunTask
from creator_domain.models.usage import UsageEvent
from creator_domain.models.media_type import MediaType, MediaOrigin
from creator_service.media_upload_reconciliation import UploadIntentState

from project_deletion_support import DeletionDatabase, deletion_database


def domain_checks() -> dict[tuple[str, str], frozenset[str]]:
    return {
        ("creator_projects", "ck_creator_projects_status"): frozenset(get_args(Project.model_fields["status"].annotation)),
        ("creator_projects", "ck_creator_projects_source_type"): frozenset(get_args(Project.model_fields["source_type"].annotation)),
        ("creator_runs", "ck_creator_runs_status"): frozenset(get_args(PipelineRun.model_fields["status"].annotation)),
        ("creator_run_tasks", "ck_creator_run_tasks_status"): frozenset(get_args(RunTask.model_fields["status"].annotation)),
        ("usage_events", "ck_usage_events_operation_type"): frozenset(get_args(UsageEvent.model_fields["operation_type"].annotation)),
        ("workspace_quota_reservation_owners", "ck_quota_owner_operation"): frozenset(get_args(UsageEvent.model_fields["operation_type"].annotation)),
        ("creator_media_assets", "ck_media_assets_type"): frozenset(value.value for value in MediaType),
        ("creator_media_assets", "ck_media_assets_origin"): frozenset(value.value for value in MediaOrigin),
        ("creator_media_upload_intents", "ck_media_upload_intents_state"): frozenset(value.value for value in UploadIntentState.__members__.values()),
    }


# These storage-only states have no domain enum; pin their complete existing SQL contract.
STORAGE_CHECKS: Final = {
    ("creator_stage_reviews", "ck_creator_stage_reviews_review_status"): frozenset({"pending", "approved", "rejected", "skipped"}),
    ("creator_artifacts", "ck_creator_artifacts_artifact_type"): frozenset({"idea", "script", "visual_plan", "visual_asset", "audio", "subtitle", "video", "render_manifest"}),
    ("workspace_quota_reservation_owners", "ck_quota_owner_status"): frozenset({"reserved", "consumed", "settled", "cancelled", "billable_unknown"}),
    ("workspace_quota_reservation_attempts", "ck_quota_attempt_status"): frozenset({"authorized", "accounted", "unknown"}),
}


async def database_checks(database: DeletionDatabase) -> dict[tuple[str, str], frozenset[str]]:
    rows = await database.pool.fetch("""
        SELECT t.relname AS table_name, c.conname, pg_get_constraintdef(c.oid) AS definition
        FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid
        JOIN pg_namespace n ON n.oid=t.relnamespace
        WHERE c.contype='c' AND n.nspname=$1
    """, database.schema)
    # PostgreSQL normalizes IN to ANY(ARRAY[...]); ignore conditional/shape/numeric CHECKs.
    return {
        (row["table_name"], row["conname"]): frozenset(re.findall(r"'([^']*)'", row["definition"]))
        for row in rows if "ANY" in row["definition"] and "ARRAY[" in row["definition"]
    }


@pytest.mark.asyncio
async def test_persisted_enum_checks_match_domain_when_all_migrations_are_applied(
    deletion_database: DeletionDatabase,
) -> None:
    # Given the explicit domain/DB audit mapping and migrated real PostgreSQL catalog.
    expected = {**domain_checks(), **STORAGE_CHECKS}
    # When inspecting every enum-valued CHECK, including unmapped new constraints.
    actual = await database_checks(deletion_database)
    # Then missing/extra constraints and either direction of value drift fail loudly.
    assert actual == expected


@pytest.mark.asyncio
async def test_drift_guard_detects_added_database_value_when_constraint_is_mutated(
    deletion_database: DeletionDatabase,
) -> None:
    # Given a real CHECK deliberately widened with an unsupported domain value.
    await deletion_database.pool.execute("""
        ALTER TABLE creator_projects DROP CONSTRAINT ck_creator_projects_status;
        ALTER TABLE creator_projects ADD CONSTRAINT ck_creator_projects_status
            CHECK (status IN ('draft','active','completed','archived','deleting','unsupported'));
    """)
    # When introspecting the catalog rather than migration source text.
    actual = await database_checks(deletion_database)
    # Then the same comparison detects drift.
    with pytest.raises(AssertionError):
        assert actual == {**domain_checks(), **STORAGE_CHECKS}


@pytest.mark.asyncio
async def test_drift_guard_detects_removed_domain_value_when_model_is_mutated(
    deletion_database: DeletionDatabase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typing import Literal
    # Given a model annotation missing a value accepted by the database.
    monkeypatch.setattr(Project.model_fields["status"], "annotation", Literal["draft", "active", "completed", "archived"])
    # When comparing the catalog to the runtime model definition.
    actual = await database_checks(deletion_database)
    # Then model-side drift cannot silently pass.
    with pytest.raises(AssertionError):
        assert actual == {**domain_checks(), **STORAGE_CHECKS}

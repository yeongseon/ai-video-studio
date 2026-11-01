"""Schema regression tests for the api_keys table.

These tests exist because of a production-breaking bug: auth.py queried
``revoked_at`` and create_api_key.py inserted ``name``, but migration 018
created the api_keys table with neither column. The conftest stubs mocked
the DB layer so completely that the mismatch was invisible to tests.

Each test loads the ACTUAL migration files (not stubs) and asserts the
schema they produce matches what auth.py / scripts/create_api_key.py expect.
If a future migration drops or renames any of these columns, these tests
will fail at test time, not in production.

The bug fix: migration 029 adds ``name`` and ``revoked_at`` to api_keys.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

# Columns that auth.py and scripts/create_api_key.py depend on.
# - auth.py:84, auth.py:265 reference ``revoked_at`` in WHERE clauses
# - scripts/create_api_key.py:56 inserts into ``(user_id, key_hash, name)``
# - migration 018 created only ``(id, user_id, key_hash, created_at)`` — missing both
EXPECTED_API_KEYS_COLUMNS = {"id", "user_id", "key_hash", "created_at", "name", "revoked_at"}

_MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations" / "versions"


def _load_migration_module(filename: str, module_name: str) -> ModuleType:
    """Load a migration file as an isolated module so we can call upgrade() directly.

    Migration files import ``from alembic import op`` — we replace that binding
    with a MagicMock that records every op call, so we can assert what the
    migration does without running a real database migration.
    """
    path = _MIGRATIONS_DIR / filename
    if not path.exists():
        pytest.fail(f"migration file not found: {path}")
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _record_op_calls(module: ModuleType) -> list[tuple[str, tuple[object, ...]]]:
    """Replace module.op with a recorder and call upgrade().

    Returns the recorded op calls as (op_name, args, kwargs).
    """
    recorded: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def _make_recorder(op_name: str):
        def _impl(*args, **kwargs):
            recorded.append((op_name, args, kwargs))

        return _impl

    fake_op = MagicMock()
    for op_name in (
        "add_column",
        "drop_column",
        "create_table",
        "drop_table",
        "create_index",
        "drop_index",
        "create_check_constraint",
        "drop_constraint",
        "execute",
    ):
        setattr(fake_op, op_name, _make_recorder(op_name))

    module.op = fake_op  # type: ignore[attr-defined]
    module.upgrade()
    return recorded


def test_migration_018_preserves_historical_schema_before_029_repair() -> None:
    """Characterize deployed revision 018, not the desired current schema.

    Preserve historical migrations: databases already at 018 receive the missing
    columns through additive revision 029. The next test guards that repair;
    this baseline must not be used as a reason to rewrite or squash either revision.
    """
    module = _load_migration_module(
        "018_api_keys_and_quota_unit_rename.py",
        "test_migration_018",
    )
    calls = _record_op_calls(module)

    create_calls = [c for c in calls if c[0] == "create_table" and c[1] and c[1][0] == "api_keys"]
    assert create_calls, "migration 018 must create the api_keys table"
    _, args, _ = create_calls[0]  # type: ignore[misc]
    columns = args[1:]  # first positional arg is the table name
    column_names = {c.name for c in columns}
    assert column_names == {"id", "user_id", "key_hash", "created_at"}
    # The bug: these were missing in 018
    assert "name" not in column_names
    assert "revoked_at" not in column_names


def test_migration_029_adds_name_and_revoked_at_to_api_keys() -> None:
    """Migration 029 must add both name and revoked_at columns to api_keys.

    This guards the additive repair for databases already at revision 018.
    auth.py and create_api_key.py depend on these columns after upgrade.
    """
    path = _MIGRATIONS_DIR / "029_api_keys_add_name_and_revoked_at.py"
    if not path.exists():
        pytest.fail(
            "migration 029 (api_keys name + revoked_at) is missing. "
            "auth.py:84 queries revoked_at, scripts/create_api_key.py:56 inserts name — "
            "production will fail with 'column does not exist'. Create migration 029."
        )

    module = _load_migration_module(
        "029_api_keys_add_name_and_revoked_at.py",
        "test_migration_029",
    )
    calls = _record_op_calls(module)

    add_column_calls = [c for c in calls if c[0] == "add_column" and c[1] and c[1][0] == "api_keys"]
    added_column_names = {args[1].name for _, args, _ in add_column_calls}

    assert "name" in added_column_names, "migration 029 must add 'name' column to api_keys"
    assert "revoked_at" in added_column_names, (
        "migration 029 must add 'revoked_at' column to api_keys"
    )


@pytest.mark.parametrize(
    "database_url",
    ["sqlite://", os.environ.get("FIRST_SHORT_TEST_DATABASE_URL")],
    ids=["sqlite", "postgres"],
)
def test_api_key_optional_columns_remain_nullable_without_backfill_when_upgrading_029(
    database_url: str | None,
) -> None:
    # Given a real revision-018 table containing a key issued before labels existed.
    if database_url is None:
        pytest.skip("Disposable PostgreSQL endpoint required")
    engine = sa.create_engine(database_url)
    try:
        # Connection exit rolls back the migration and the isolated PostgreSQL schema.
        with engine.connect() as connection:
            if engine.dialect.name == "postgresql":
                schema = f"api_keys_581_{uuid4().hex}"
                connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
                connection.execute(sa.text(f'SET LOCAL search_path TO "{schema}"'))
            scripts = ScriptDirectory(str(_MIGRATIONS_DIR.parent))
            historical = scripts.get_revision("018")
            repair = scripts.get_revision("029")
            assert historical is not None
            assert repair is not None
            with Operations.context(MigrationContext.configure(connection)):
                historical.module.upgrade()
                connection.execute(
                    sa.text("INSERT INTO api_keys (user_id, key_hash) VALUES (7, 'existing')")
                )
                # When applying the real additive repair (not a recorded/mock operation).
                repair.module.upgrade()

            # Then the documented optional-column contract holds for schema and old rows.
            columns = {
                column["name"]: column
                for column in sa.inspect(connection).get_columns("api_keys")
            }
            assert columns["name"]["nullable"] is True
            assert columns["name"]["default"] is None
            assert columns["revoked_at"]["nullable"] is True
            assert connection.execute(
                sa.text("SELECT name, revoked_at FROM api_keys WHERE key_hash = 'existing'")
            ).one() == (None, None)
    finally:
        engine.dispose()


def test_auth_py_sql_references_match_schema() -> None:
    """Ensure auth.py only references columns that exist in the canonical api_keys schema.

    Parses auth.py for SQL against api_keys and asserts every referenced column
    exists in EXPECTED_API_KEYS_COLUMNS. Catches SQL/schema drift at test time.
    """
    import ast
    import re

    auth_path = Path(__file__).resolve().parents[1] / "src" / "shorts_api" / "auth.py"
    source = auth_path.read_text(encoding="utf-8")

    # Use AST to walk all string constants — robust to single/double/triple/f-string variants.
    # The previous regex approach only matched double-quoted strings and silently broke on refactors.
    tree = ast.parse(source)
    api_keys_sql_fragments: list[str] = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "api_keys" in node.value.lower()
    ]
    assert api_keys_sql_fragments, "expected at least one SQL statement against api_keys in auth.py"

    column_re = re.compile(r"\b([a-z_][a-z0-9_]*)\b")
    sql_keywords = {
        "select",
        "from",
        "where",
        "and",
        "or",
        "is",
        "null",
        "not",
        "insert",
        "into",
        "values",
        "update",
        "set",
        "limit",
        "api_keys",
    }
    for fragment in api_keys_sql_fragments:
        # Strip SQL parameter placeholders first so they don't leak into the column scan.
        # Supports both asyncpg ($1) and sqlalchemy text (:name) styles.
        fragment_clean = re.sub(r"[:$]\w+", " ", fragment)
        fragment_clean = re.sub(r"\b\d+\b", " ", fragment_clean)
        referenced = {
            tok.lower()
            for tok in column_re.findall(fragment_clean)
            if tok.lower() not in sql_keywords
        }
        unknown = referenced - EXPECTED_API_KEYS_COLUMNS
        assert not unknown, f"auth.py references unknown api_keys columns: {unknown}"


def test_create_api_key_script_inserts_only_known_columns() -> None:
    """scripts/create_api_key.py INSERT must only reference columns in the canonical schema."""
    import re

    script_path = Path(__file__).resolve().parents[3] / "scripts" / "create_api_key.py"
    source = script_path.read_text(encoding="utf-8")

    match = re.search(
        r"INSERT\s+INTO\s+api_keys\s*\(([^)]+)\)",
        source,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert match, "scripts/create_api_key.py must INSERT into api_keys"
    columns = [c.strip().strip('"').strip("'") for c in match.group(1).split(",")]
    columns = [c for c in columns if c]
    assert columns, "INSERT column list must not be empty"

    unknown = set(columns) - EXPECTED_API_KEYS_COLUMNS
    assert not unknown, f"create_api_key.py inserts unknown api_keys columns: {unknown}"

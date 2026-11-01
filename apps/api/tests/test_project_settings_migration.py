from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
import sqlalchemy as sa


def test_settings_migration_when_upgraded_and_downgraded_sqlite() -> None:
    # Given the previous schema with an existing project row.
    scripts = ScriptDirectory(str(Path(__file__).resolve().parents[1] / "migrations"))
    revision = scripts.get_revision("051")
    assert revision is not None
    assert revision.down_revision == "050"
    with sa.create_engine("sqlite://").begin() as connection:
        connection.execute(sa.text("CREATE TABLE creator_projects (id INTEGER PRIMARY KEY, title TEXT)"))
        connection.execute(sa.text("INSERT INTO creator_projects VALUES (1, 'Legacy')"))
        # When applying the additive revision, then old rows remain NULL with no default.
        with Operations.context(MigrationContext.configure(connection)):
            revision.module.upgrade()
        assert connection.execute(sa.text("SELECT title, settings FROM creator_projects")).one() == ("Legacy", None)
        column = next(c for c in sa.inspect(connection).get_columns("creator_projects") if c["name"] == "settings")
        assert column["nullable"] and column["default"] is None
        # When rolling back, then only the added column disappears.
        with Operations.context(MigrationContext.configure(connection)):
            revision.module.downgrade()
        assert connection.execute(sa.text("SELECT title FROM creator_projects")).scalar_one() == "Legacy"
        assert "settings" not in {c["name"] for c in sa.inspect(connection).get_columns("creator_projects")}

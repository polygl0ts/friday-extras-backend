from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine

from app.config import settings

_connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=_connect_args)

# Columns added to tables that already exist in deployed databases.
#
# SQLModel's create_all() only ever creates *missing tables* - it will not add
# a column to a table that is already there. So once this service has a
# Postgres volume on a server, a model that gains a column needs the ALTER
# doing here or every query selecting it fails at runtime.
#
# Note that this service's database is currently *empty everywhere* - it has
# never been deployed. (rCTF's database is the old, adopted one; this one is
# not. Do not read across from the rctf-docker role.) So on first deploy
# create_all() builds every table complete and all three helpers below are
# no-ops. They matter from the second schema change onward. Written to work on
# both Postgres (prod) and SQLite (dev/tests).
_ADDITIVE_COLUMNS: dict[str, dict[str, str]] = {
    "writeup": {
        # Writeups became stored markdown split into a public and a gated
        # half; rows predating that get empty bodies and keep their `url`,
        # which is what marks them as legacy at render time.
        "intro_md": "TEXT NOT NULL DEFAULT ''",
        "solution_md": "TEXT NOT NULL DEFAULT ''",
        "reject_reason": "TEXT",
    },
}

# Columns dropped from the models that still exist in deployed databases.
#
# Leaving them would be worse than cosmetic: create_all() declared them NOT
# NULL *without* a database-level default (SQLModel defaults are applied in
# Python), so once the model stops supplying a value, any INSERT into that
# table fails on the leftover column.
_REMOVED_COLUMNS: dict[str, tuple[str, ...]] = {
    "discordconfig": (
        # Per-event notification switches. Replaced by "writeups go to one
        # webhook, first bloods to the other" - no switches to get wrong.
        "notify_submitted",
        "notify_approved",
        "notify_rejected",
        "notify_first_blood",
        # ...and then first bloods stopped being this service's job at all:
        # rCTF's own blood bot announces them, configured in rCTF's config
        # file. Note this column was itself added by _ADDITIVE_COLUMNS in an
        # earlier version, so a database can arrive here having never had it,
        # having it, or having had it dropped already - all three are handled
        # by the reflection guard in _drop_removed_columns.
        "first_blood_webhook_url",
    ),
}


# Tables dropped from the models that still exist in deployed databases.
#
# Unlike a leftover column, a leftover table breaks nothing - nothing inserts
# into it any more. It is dropped anyway so "the models are the schema" keeps
# holding: create_all() only ever *adds*, so without this the table would
# outlive the code that made it forever, and the next person to read the
# database would find a first-blood ledger that has not been written to since.
#
# The data is not lost in any meaningful sense: every row was a copy of an
# answer rCTF computes itself and serves on /v2/leaderboard/challs.
_REMOVED_TABLES: tuple[str, ...] = (
    # First-blood cache, filled by a background poller. rCTF v2 serves this
    # directly and recomputes it on every accepted flag, so the frontend reads
    # it from there - see the note where the model used to be in app/models.py.
    "firstbloodseen",
)


def _add_missing_columns() -> None:
    # Reflect through the same connection that runs the ALTERs. An Inspector
    # built from the engine caches what it reflects for its own lifetime and
    # reads on a different connection, so it can report a schema that predates
    # DDL applied moments earlier - and "column missing" is exactly the stale
    # answer that makes this re-add a column that is already there.
    with engine.begin() as conn:
        inspector = inspect(conn)
        existing_tables = set(inspector.get_table_names())

        for table, columns in _ADDITIVE_COLUMNS.items():
            if table not in existing_tables:
                # create_all() just built it with every column already.
                continue
            present = {col["name"] for col in inspector.get_columns(table)}
            for name, ddl in columns.items():
                if name not in present:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def _drop_removed_columns() -> None:
    with engine.begin() as conn:
        inspector = inspect(conn)
        existing_tables = set(inspector.get_table_names())

        for table, columns in _REMOVED_COLUMNS.items():
            if table not in existing_tables:
                continue
            present = {col["name"] for col in inspector.get_columns(table)}
            for name in columns:
                # Guarded by reflection rather than `IF EXISTS`, which SQLite's
                # DROP COLUMN doesn't support.
                if name in present:
                    conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {name}"))


def _drop_removed_tables() -> None:
    with engine.begin() as conn:
        existing_tables = set(inspect(conn).get_table_names())
        for table in _REMOVED_TABLES:
            if table in existing_tables:
                conn.execute(text(f"DROP TABLE {table}"))


def create_db_and_tables() -> None:
    SQLModel.metadata.create_all(engine)
    _add_missing_columns()
    _drop_removed_columns()
    _drop_removed_tables()


def get_session():
    with Session(engine) as session:
        yield session

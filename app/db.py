from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine

from app.config import settings

_connect_args = (
    {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
)
engine = create_engine(settings.database_url, connect_args=_connect_args)

# Columns added to tables that already exist in deployed databases.
_ADDITIVE_COLUMNS: dict[str, dict[str, str]] = {
    "writeup": {
        "intro_md": "TEXT NOT NULL DEFAULT ''",
        "solution_md": "TEXT NOT NULL DEFAULT ''",
        "reject_reason": "TEXT",
    },
}

# Columns dropped from the models that still exist in deployed databases.
_REMOVED_COLUMNS: dict[str, tuple[str, ...]] = {
    "discordconfig": (
        # Per-event notification switches. Replaced by "writeups go to one
        # webhook, first bloods to the other" - no switches to get wrong.
        "notify_submitted",
        "notify_approved",
        "notify_rejected",
        "first_blood_webhook_url",
    ),
}


# Tables dropped from the models that still exist in deployed databases.
_REMOVED_TABLES: tuple[str, ...] = (
    # First-blood cache, filled by a background poller. rCTF v2 serves this
    # directly and recomputes it on every accepted flag, so the frontend reads
    # it from there.
    "firstbloodseen",
    # Slide decks. Now a public GitHub repo (polygl0ts/slides) whose CI builds
    # a decks.json the frontend reads directly, so this service no longer
    # stores or serves them.
    "deck",
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

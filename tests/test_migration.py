"""The schema-drift paths in `app.db`.

Every other test runs against tables `create_all()` just built with every
column present, so none of them exercise the branches a real deploy actually
takes: an existing database whose `writeup` table predates the markdown
columns, or one still carrying a table the models have since dropped. This
rebuilds those older shapes and checks the upgrade lands.
"""

from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, select

from app.db import _add_missing_columns, _drop_removed_tables, engine
from app.models import Writeup, WriteupStatus

# `writeup` as it was before writeups became stored markdown: a URL and a
# summary, no bodies, no reject reason.
_LEGACY_TABLE = """
CREATE TABLE writeup (
    id INTEGER NOT NULL PRIMARY KEY,
    challenge_id VARCHAR NOT NULL,
    challenge_name VARCHAR NOT NULL,
    team_id VARCHAR NOT NULL,
    team_name VARCHAR NOT NULL,
    url VARCHAR NOT NULL,
    summary VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    created_at DATETIME NOT NULL,
    reviewed_by VARCHAR,
    reviewed_at DATETIME
)
"""

_LEGACY_ROW = """
INSERT INTO writeup
    (challenge_id, challenge_name, team_id, team_name, url, summary,
     status, created_at)
VALUES
    ('c1', 'cookie_monster', 't1', 'n1ght0wl', 'https://hackmd.io/old',
     'the old way', 'published', '2026-01-01 00:00:00')
"""


def _rebuild_legacy_writeup_table() -> None:
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS writeup"))
        conn.execute(text(_LEGACY_TABLE))
        conn.execute(text(_LEGACY_ROW))


def test_adds_markdown_columns_to_a_preexisting_table() -> None:
    _rebuild_legacy_writeup_table()

    SQLModel.metadata.create_all(engine)  # no-op: the table already exists
    _add_missing_columns()

    columns = {c["name"] for c in inspect(engine).get_columns("writeup")}
    assert {"intro_md", "solution_md", "reject_reason"} <= columns


def test_legacy_rows_survive_and_stay_readable() -> None:
    _rebuild_legacy_writeup_table()
    _add_missing_columns()

    with Session(engine) as session:
        legacy = session.exec(select(Writeup)).one()

    assert legacy is not None
    # A legacy row keeps its link and carries no body - which is exactly what
    # marks it as legacy when the frontend renders it.
    assert legacy.url == "https://hackmd.io/old"
    assert legacy.intro_md == ""
    assert legacy.solution_md == ""
    assert legacy.reject_reason is None
    assert legacy.status == WriteupStatus.published


def test_running_twice_is_a_no_op() -> None:
    _rebuild_legacy_writeup_table()
    _add_missing_columns()
    _add_missing_columns()  # would raise "duplicate column" if not guarded

    columns = {c["name"] for c in inspect(engine).get_columns("writeup")}
    assert "intro_md" in columns


# --- dropped tables ---------------------------------------------------------
#
# `create_all()` only ever adds, so a table whose model is gone would otherwise
# outlive the code that wrote it indefinitely.

_LEGACY_FIRST_BLOOD_TABLE = """
CREATE TABLE firstbloodseen (
    challenge_id VARCHAR NOT NULL PRIMARY KEY,
    solver_name VARCHAR NOT NULL,
    seen_at DATETIME NOT NULL
)
"""


def test_drops_the_first_blood_cache_left_by_an_older_deploy() -> None:
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS firstbloodseen"))
        conn.execute(text(_LEGACY_FIRST_BLOOD_TABLE))
        conn.execute(
            text(
                "INSERT INTO firstbloodseen VALUES "
                "('c1', 'n1ght0wl', '2026-01-01 00:00:00')"
            )
        )

    _drop_removed_tables()

    assert "firstbloodseen" not in set(inspect(engine).get_table_names())


def test_dropping_tables_is_a_no_op_on_a_database_that_never_had_them() -> None:
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS firstbloodseen"))

    # Guarded by reflection, not `IF EXISTS` - the same reason the column path
    # is, since SQLite's DROP COLUMN has no such clause.
    _drop_removed_tables()
    _drop_removed_tables()

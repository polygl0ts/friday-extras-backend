import os
import tempfile

import pytest

# Must happen before `app.config`/`app.db` are imported anywhere, since
# pydantic-settings reads the env once at import time.
_db_fd, _db_path = tempfile.mkstemp(suffix=".db")
os.environ.setdefault("EXTRAS_DATABASE_URL", f"sqlite:///{_db_path}")


@pytest.fixture(autouse=True)
def _reset_db():
    from sqlmodel import SQLModel

    from app.db import engine

    SQLModel.metadata.drop_all(engine)
    SQLModel.metadata.create_all(engine)
    yield


@pytest.fixture(autouse=True)
def _clear_identity_cache():
    # app.auth caches resolved bearer-token -> identity lookups at module
    # scope (by design - see its docstring). Different tests reusing the
    # same literal token string with different fake identities would
    # otherwise see stale results from an earlier test.
    from app.auth import _identity_cache

    _identity_cache.clear()
    yield
    _identity_cache.clear()

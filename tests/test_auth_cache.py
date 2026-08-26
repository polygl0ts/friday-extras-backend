"""The identity cache: what it stores, and how much of it.

An rCTF auth token is a non-expiring, full-power credential for the player's
entire account - and this service is handed one on every request. It cannot
avoid seeing them, but it can avoid *keeping* a tidy dictionary of live logins,
which is what these tests pin.

(This is also all that rCTF's external-auth flow would have changed. Its
`/token` returns "the same non-expiring token issued at login" with full
account access and no revocation, so routing login through it would move an
identically powerful credential through more steps. See D5 in docs/CODEBASE.md.)
"""

import asyncio
import hashlib
import time

import pytest

from app import auth
from app.auth import _identity_cache, _resolve_token
from app.rctf_client import TeamIdentity
from tests.fake_rctf import FakeRctfClient

TOKEN = "authtok_super_secret_value"


@pytest.fixture()
def fake() -> FakeRctfClient:
    f = FakeRctfClient()
    f.identities[TOKEN] = TeamIdentity("t1", "n1ght0wl", is_admin=False)
    return f


def test_the_raw_token_is_never_a_cache_key(fake) -> None:
    asyncio.run(_resolve_token(TOKEN, fake))

    assert TOKEN not in _identity_cache
    assert hashlib.sha256(TOKEN.encode()).hexdigest() in _identity_cache


def test_the_raw_token_appears_nowhere_in_the_cache_at_all(fake) -> None:
    """Not as a key, and not smuggled into the cached value either - the check
    a future refactor is most likely to break."""
    asyncio.run(_resolve_token(TOKEN, fake))

    assert TOKEN not in repr(_identity_cache)


def test_a_cached_identity_is_reused(fake) -> None:
    calls: list[str] = []
    inner = fake.get_current_identity

    async def counting(token):
        calls.append(token)
        return await inner(token)

    fake.get_current_identity = counting  # type: ignore[method-assign]

    asyncio.run(_resolve_token(TOKEN, fake))
    asyncio.run(_resolve_token(TOKEN, fake))

    assert len(calls) == 1


def test_force_bypasses_the_cache_and_refreshes_it(fake) -> None:
    calls: list[str] = []
    inner = fake.get_current_identity

    async def counting(token):
        calls.append(token)
        return await inner(token)

    fake.get_current_identity = counting  # type: ignore[method-assign]

    asyncio.run(_resolve_token(TOKEN, fake))
    fake.solves["t1"] = {"c1"}
    refreshed = asyncio.run(_resolve_token(TOKEN, fake, force=True))

    assert len(calls) == 2
    assert refreshed.solved_challenge_ids == frozenset({"c1"})
    # ...and the refreshed value is what a subsequent cached read returns.
    assert asyncio.run(_resolve_token(TOKEN, fake)).solved_challenge_ids == frozenset({"c1"})


def test_the_cache_is_bounded(fake, monkeypatch) -> None:
    """A stream of distinct valid tokens must not grow this without limit."""
    monkeypatch.setattr(auth, "_MAX_CACHED_IDENTITIES", 8)

    for i in range(50):
        token = f"tok-{i}"
        fake.identities[token] = TeamIdentity(f"t{i}", f"team{i}", is_admin=False)
        asyncio.run(_resolve_token(token, fake))

    assert len(_identity_cache) <= 8


def test_expired_entries_are_dropped_rather_than_accumulating(fake, monkeypatch) -> None:
    monkeypatch.setattr(auth.settings, "identity_cache_seconds", 0)

    for i in range(5):
        token = f"tok-{i}"
        fake.identities[token] = TeamIdentity(f"t{i}", f"team{i}", is_admin=False)
        asyncio.run(_resolve_token(token, fake))
        time.sleep(0.001)

    # Every previous entry lapsed immediately, so only the newest can remain.
    assert len(_identity_cache) <= 1


def test_a_failed_lookup_is_not_cached(fake) -> None:
    assert asyncio.run(_resolve_token("not-a-real-token", fake)) is None
    assert _identity_cache == {}

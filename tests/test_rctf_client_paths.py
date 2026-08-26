"""Which API version each rCTF call goes to.

rCTF's v2 is additive, not a replacement. The two routes never re-issued
(`auth/login`, `challs/:id/submit`) are browser-side, so this service runs
entirely on v2 - but the challenge list is *wrong* on anything else (v1's
response schema strips `tags`, which carries the INTRO2 marker and the grid
tier), so it pins v2 itself rather than following the configured base. "Which
base does this call use" is exactly the kind of thing a later refactor flips by
accident, hence a test per call.

`RctfClient` builds its own `httpx.AsyncClient`, so there is no transport to
inject; patching the class's `get` is the least invasive way to observe the URL
the real path-building code produces.
"""

import asyncio

import httpx
import pytest

from app.rctf_client import RctfClient

ORIGIN = "https://rctf.example"


class _FakeResponse:
    def __init__(self, payload: object, status_code: int = 200) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        return self._payload


@pytest.fixture
def calls(monkeypatch):
    """Record every GET the client makes, answering each with an empty-ish
    payload of the shape that call site expects."""
    recorded: list[str] = []

    async def fake_get(self, url, **kwargs):
        recorded.append(url)
        if url.endswith("/users/me"):
            return _FakeResponse({"data": {"id": "t1", "name": "team", "perms": 0}})
        if "/solves" in url:
            return _FakeResponse({"data": {"solves": []}})
        if url.endswith("/leaderboard/challs"):
            return _FakeResponse({"data": {"challenges": {}}})
        if url.endswith("/leaderboard/now"):
            return _FakeResponse({"data": {"leaderboard": [], "total": 0}})
        return _FakeResponse({"data": []})

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    return recorded


@pytest.fixture
def client():
    return RctfClient(ORIGIN, "/api/v2")


def test_challenge_list_uses_v2(client, calls):
    """The whole point: `tags` is stripped from v1's response by its own schema,
    so reading the list over v1 leaves INTRO2 permanently empty and every
    challenge untiered."""
    asyncio.run(client.list_challenges())
    assert calls == [f"{ORIGIN}/api/v2/challs"]


def test_challenge_list_ignores_the_configured_api_base(calls):
    """v2 for this call is not a config choice - pinning api_base elsewhere must
    not drag the challenge list back to v1."""
    asyncio.run(RctfClient(ORIGIN, "/api/v1").list_challenges())
    asyncio.run(RctfClient(ORIGIN, "/api/v9").list_challenges())
    assert calls == [f"{ORIGIN}/api/v2/challs"] * 2


def test_trailing_slash_on_the_origin_does_not_double_up(calls):
    asyncio.run(RctfClient(f"{ORIGIN}/", "/api/v2").list_challenges())
    assert calls == [f"{ORIGIN}/api/v2/challs"]


def test_identity_costs_exactly_one_request(client, calls):
    """Team id/name, admin status and solve state all come out of `/users/me`.

    This used to be three requests: `/users/me`, then `/admin/challs` purely to
    read a status code, then `/users/:id` for solves. The count is the assertion
    - a regression here is invisible in behaviour and only shows up as load.
    """
    asyncio.run(client.get_current_identity("tok"))
    assert calls == [f"{ORIGIN}/api/v2/users/me"]


def test_identity_no_longer_fetches_the_admin_challenge_list(client, calls):
    """That list includes every challenge's `flag` in plaintext. Pulling it into
    this service to learn "200 or 403" was the worst part of the old probe."""
    asyncio.run(client.get_current_identity("tok"))
    assert not any("admin" in url for url in calls)


def test_leaderboard_stays_on_the_configured_base(client, calls):
    asyncio.run(client.get_leaderboard_size())
    assert calls == [f"{ORIGIN}/api/v2/leaderboard/now"]


def test_leaderboard_size_asks_for_one_entry_not_five_hundred(client, monkeypatch):
    """`leaderboard.maxLimit` defaults to 100 and a larger limit is a 400, not a
    bigger page - verified against a real instance. Asking for 500 made this
    return 0 on any stock deployment, i.e. "0 players" on the admin dashboard.
    `total` does not depend on `limit`, so one entry carries it."""
    seen: list[dict] = []

    async def fake_get(self, url, **kwargs):
        seen.append(kwargs["params"])
        if kwargs["params"]["limit"] > 100:
            return _FakeResponse({"kind": "badBody"}, status_code=400)
        return _FakeResponse({"data": {"total": 12, "leaderboard": [{"id": "t1"}]}})

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    assert asyncio.run(client.get_leaderboard_size()) == 12
    assert seen == [{"limit": 1, "offset": 0}]


def test_leaderboard_size_prefers_total_over_counting_the_page(client, monkeypatch):
    async def fake_get(self, url, **kwargs):
        # One entry returned, 128 teams ranked - counting would say 1.
        return _FakeResponse({"data": {"total": 128, "leaderboard": [{"id": "t1"}]}})

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    assert asyncio.run(client.get_leaderboard_size()) == 128


def test_the_configurable_calls_really_do_follow_the_base(calls):
    """The tests above assert v2, which is now also the default - so on their
    own they would still pass if these paths were hardcoded. An implausible base
    separates "follows config" from "pinned to v2", which is the distinction
    test_challenge_list_ignores_the_configured_api_base depends on."""
    other = RctfClient(ORIGIN, "/api/v9")
    asyncio.run(other.get_current_identity("tok"))
    asyncio.run(other.get_leaderboard_size())

    assert calls == [
        f"{ORIGIN}/api/v9/users/me",
        f"{ORIGIN}/api/v9/leaderboard/now",
    ]


def test_v2_null_tags_do_not_crash_the_caller(client, monkeypatch):
    """v2 sends `tags: null` rather than omitting the key. The list itself is
    returned verbatim; this pins that the client does not choke on it, so the
    `or []` guards downstream are the only thing that has to cope."""

    async def fake_get(self, url, **kwargs):
        return _FakeResponse({"data": [{"id": "c1", "tags": None, "sortWeight": None}]})

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    assert asyncio.run(client.list_challenges()) == [
        {"id": "c1", "tags": None, "sortWeight": None}
    ]


def test_non_list_challenge_payload_degrades_to_empty(client, monkeypatch):
    async def fake_get(self, url, **kwargs):
        return _FakeResponse({"data": {"kind": "badNotStarted"}})

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    assert asyncio.run(client.list_challenges()) == []


# --- admin detection from `perms` (D4) --------------------------------------
#
# The bit values are not in rCTF's docs. They were read off a real instance by
# promoting throwaway users with `rctf user promote --perms <name>`:
#   challsRead=1  challsWrite=2  challsSolveWrite=8  usersWrite=16
#   settingsWrite=32   full admin=63   unpromoted=0
# These tests encode that table, so if a future rCTF renumbers the bits they
# fail here rather than silently handing admin to the wrong people.


def _me(monkeypatch, payload: dict):
    async def fake_get(self, url, **kwargs):
        return _FakeResponse({"data": {"id": "t1", "name": "team", **payload}})

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)


@pytest.mark.parametrize(
    ("perms", "expected", "why"),
    [
        (0, False, "unpromoted account - what a real instance returns"),
        (None, False, "what the docs claim a standard user returns"),
        (1, True, "challsRead alone, exactly what the old probe accepted"),
        (63, True, "full admin"),
        (17, True, "challsRead + usersWrite"),
        (2, False, "challsWrite only - cannot read /admin/challs, so not admin"),
        (32, False, "settingsWrite only"),
    ],
)
def test_admin_is_read_from_the_perms_bitmask(client, monkeypatch, perms, expected, why):
    _me(monkeypatch, {"perms": perms})
    identity = asyncio.run(client.get_current_identity("tok"))
    assert identity is not None
    assert identity.is_admin is expected, why


def test_a_non_numeric_perms_value_is_not_admin(client, monkeypatch):
    """Fail closed. A shape change in `perms` must not grant admin."""
    _me(monkeypatch, {"perms": "all"})
    assert asyncio.run(client.get_current_identity("tok")).is_admin is False


# --- solve state from the same response (R1) --------------------------------


def test_solved_ids_come_from_the_users_me_solves_list(client, monkeypatch):
    _me(monkeypatch, {
        "perms": 0,
        "solves": [
            {"id": "baby-rev", "createdAt": 1},
            {"id": "sql_ninja", "createdAt": 2},
        ],
    })
    identity = asyncio.run(client.get_current_identity("tok"))
    assert identity.solved_challenge_ids == frozenset({"baby-rev", "sql_ninja"})


def test_no_solves_key_means_no_solves_rather_than_a_crash(client, monkeypatch):
    _me(monkeypatch, {"perms": 0})
    assert asyncio.run(client.get_current_identity("tok")).solved_challenge_ids == frozenset()


def test_a_null_solves_list_is_tolerated(client, monkeypatch):
    _me(monkeypatch, {"perms": 0, "solves": None})
    assert asyncio.run(client.get_current_identity("tok")).solved_challenge_ids == frozenset()


def test_solve_rows_without_an_id_are_skipped(client, monkeypatch):
    _me(monkeypatch, {"perms": 0, "solves": [{"id": "c1"}, {"createdAt": 2}, "junk"]})
    assert asyncio.run(client.get_current_identity("tok")).solved_challenge_ids == frozenset({"c1"})


# --- banned, which only v2 reports (D6) -------------------------------------


def test_banned_is_read_from_users_me(client, monkeypatch):
    """v2's `/users/me` types `banned` as a plain boolean and always sends it."""
    _me(monkeypatch, {"perms": 0, "banned": True})
    assert asyncio.run(client.get_current_identity("tok")).banned is True


def test_a_missing_banned_key_reads_as_not_banned(client, monkeypatch):
    """v1's response schema has no `banned` field at all, so it is stripped on
    the way out. Absent must mean "not banned" - the answer v1 gave before the
    question could be asked - rather than locking every team out on a
    misconfigured base."""
    _me(monkeypatch, {"perms": 0})
    assert asyncio.run(client.get_current_identity("tok")).banned is False

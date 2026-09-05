"""Cached identity vs. fresh identity.

`solved_challenge_ids` now rides along on the cached `TeamIdentity`, which buys
a round trip on most requests but introduces a hazard the old code did not have:
solve state can be up to `identity_cache_seconds` stale. Where that would turn
into a *wrong rejection* - "solve this challenge first" moments after solving
it, or an INTRO2 step refusing to advance - the route must re-ask rCTF.

The frontend refetches both of those immediately after a correct flag, i.e.
exactly when the cache is guaranteed to be behind, so this is the realistic
path rather than a corner case.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.rctf_client import TeamIdentity, get_rctf_client
from tests.fake_rctf import FakeRctfClient

BODY = {"body_md": "intro\n:::solution\nthe answer", "summary": "s"}


@pytest.fixture()
def fake() -> FakeRctfClient:
    f = FakeRctfClient()
    f.identities["tok"] = TeamIdentity("t1", "n1ght0wl", is_admin=False)
    f.challenges = [
        {
            "id": "c1",
            "name": "cookie_monster",
            "category": "web",
            "tags": ["intro2"],
            "sortWeight": 1,
        }
    ]
    app.dependency_overrides[get_rctf_client] = lambda: f
    yield f
    app.dependency_overrides.clear()


@pytest.fixture()
def client(fake: FakeRctfClient) -> TestClient:
    with TestClient(app) as c:
        yield c


HEADERS = {"Authorization": "Bearer tok"}


def _warm_the_cache(client: TestClient) -> None:
    """Any cached-identity route will do - /api/me is the cheapest."""
    assert client.get("/api/me", headers=HEADERS).status_code == 200


def test_submitting_right_after_solving_is_not_rejected_by_a_stale_cache(client, fake) -> None:
    _warm_the_cache(client)          # cached: no solves
    fake.solves["t1"] = {"c1"}       # ...then the player solves it

    res = client.post("/api/writeups/c1/submit", json=BODY, headers=HEADERS)

    assert res.status_code == 200, res.json()


def test_the_intro2_track_advances_right_after_solving(client, fake) -> None:
    _warm_the_cache(client)
    fake.solves["t1"] = {"c1"}

    tracks = client.get("/api/intro2/tracks", headers=HEADERS).json()

    assert [s["status"] for t in tracks for s in t["steps"]] == ["done"]


def test_submission_is_still_refused_when_the_challenge_really_is_unsolved(client, fake) -> None:
    """The fresh lookup must not become a rubber stamp."""
    res = client.post("/api/writeups/c1/submit", json=BODY, headers=HEADERS)

    assert res.status_code == 403
    assert "Solve this challenge" in res.json()["detail"]


def test_reading_a_writeup_uses_the_cached_identity(client, fake) -> None:
    """Redaction is allowed to lag - it fails safe (stays hidden), and paying a
    round trip on every card open is what this change set out to remove."""
    fake.solves["t1"] = {"c1"}
    submitted = client.post("/api/writeups/c1/submit", json=BODY, headers=HEADERS).json()

    fake.identities["other"] = TeamIdentity("t2", "someone-else", is_admin=False)
    before = len(fake.calls)
    body = client.get(f"/api/writeups/item/{submitted['id']}", headers={"Authorization": "Bearer other"})

    # 404 because it is still pending, but the point is the call count below.
    assert body.status_code in (200, 404)
    assert fake.calls[before:] == [], "reading a writeup should cost no rCTF calls"

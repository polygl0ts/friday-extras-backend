import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.rctf_client import TeamIdentity, get_rctf_client
from tests.fake_rctf import FakeRctfClient


@pytest.fixture()
def fake_client() -> FakeRctfClient:
    fake = FakeRctfClient()
    app.dependency_overrides[get_rctf_client] = lambda: fake
    yield fake
    app.dependency_overrides.clear()


@pytest.fixture()
def client(fake_client: FakeRctfClient) -> TestClient:
    with TestClient(app) as c:
        yield c


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_intro2_sequential_unlock(client: TestClient, fake_client: FakeRctfClient) -> None:
    fake_client.identities["player-token"] = TeamIdentity("t1", "n1ght0wl", is_admin=False)
    fake_client.challenges = [
        {"id": "i1", "name": "Your First Flag", "tags": ["intro2"], "sortWeight": 1},
        {"id": "i2", "name": "Inspect Element", "tags": ["intro2"], "sortWeight": 2},
        {"id": "i3", "name": "Base What?", "tags": ["intro2"], "sortWeight": 3},
        {"id": "i4", "name": "Cookie Jar", "tags": ["intro2"], "sortWeight": 4},
        {"id": "other", "name": "not intro2", "tags": ["web"], "sortWeight": 1},
    ]
    fake_client.solves["t1"] = {"i1", "i2"}

    resp = client.get("/api/intro2/track", headers=auth("player-token"))
    assert resp.status_code == 200
    statuses = {s["challenge_id"]: s["status"] for s in resp.json()}
    assert statuses == {"i1": "done", "i2": "done", "i3": "in_progress", "i4": "locked"}


def test_intro2_carries_what_the_challenge_modal_needs(
    client: TestClient, fake_client: FakeRctfClient
) -> None:
    """The track has to hand over category and attachments, not just a title:
    without them the page can show a step but nobody can solve it."""
    fake_client.identities["player-token"] = TeamIdentity("t1", "n1ght0wl", is_admin=False)
    fake_client.challenges = [
        {
            "id": "i1",
            "name": "Your First Flag",
            "description": "Find the flag format.",
            "category": "intro",
            "tags": ["intro2"],
            "sortWeight": 1,
            "files": [{"name": "hint.txt", "url": "/uploads/abc/hint.txt", "size": 12}],
        },
    ]

    step = client.get("/api/intro2/track", headers=auth("player-token")).json()[0]
    assert step["category"] == "intro"
    assert step["description"] == "Find the flag format."
    assert step["files"] == [{"name": "hint.txt", "url": "/uploads/abc/hint.txt", "size": 12}]


def test_intro2_tolerates_junk_in_the_files_field(
    client: TestClient, fake_client: FakeRctfClient
) -> None:
    """`files` comes from rCTF, so its shape is not ours to trust - a bad entry
    must not 500 the whole track."""
    fake_client.identities["player-token"] = TeamIdentity("t1", "n1ght0wl", is_admin=False)
    fake_client.challenges = [
        {"id": "a", "name": "no files key", "tags": ["intro2"], "sortWeight": 1},
        {"id": "b", "name": "null", "tags": ["intro2"], "sortWeight": 2, "files": None},
        {"id": "c", "name": "not a list", "tags": ["intro2"], "sortWeight": 3, "files": "x"},
        {
            "id": "d",
            "name": "partly usable",
            "tags": ["intro2"],
            "sortWeight": 4,
            "files": [
                {"name": "ok.txt", "url": "/uploads/a/ok.txt", "size": None},
                {"name": "no url"},
                "not-a-dict",
            ],
        },
    ]

    resp = client.get("/api/intro2/track", headers=auth("player-token"))
    assert resp.status_code == 200
    steps = {s["challenge_id"]: s["files"] for s in resp.json()}
    assert steps["a"] == [] and steps["b"] == [] and steps["c"] == []
    assert steps["d"] == [{"name": "ok.txt", "url": "/uploads/a/ok.txt", "size": None}]


def test_intro2_is_empty_on_a_v1_shaped_response(
    client: TestClient, fake_client: FakeRctfClient
) -> None:
    """v1's response schema strips `tags`, so reading the challenge list over v1
    yields challenges with no tags at all and an INTRO2 page that is silently
    empty rather than broken. This is the failure mode that made INTRO2
    unreachable, so pin it: if the client ever drifts back to v1, this test is
    the one that says why the page went blank."""
    fake_client.identities["player-token"] = TeamIdentity("t1", "n1ght0wl", is_admin=False)
    fake_client.challenges = [
        {"id": "i1", "name": "Your First Flag", "sortWeight": 1},
        {"id": "i2", "name": "Inspect Element", "sortWeight": 2},
    ]

    resp = client.get("/api/intro2/track", headers=auth("player-token"))
    assert resp.status_code == 200
    assert resp.json() == []


def test_intro2_sorts_when_v2_sends_null_sort_weights(
    client: TestClient, fake_client: FakeRctfClient
) -> None:
    """v2 always emits `sortWeight`, using `null` for challenges that have none
    (v1 omitted the key instead, so a `.get(key, 0)` default used to cover it).
    A mix of null and numeric weights must not raise TypeError comparing None to
    an int - that would be a 500 on this endpoint."""
    fake_client.identities["player-token"] = TeamIdentity("t1", "n1ght0wl", is_admin=False)
    fake_client.challenges = [
        {"id": "b", "name": "beta", "tags": ["intro2"], "sortWeight": None},
        {"id": "c", "name": "gamma", "tags": ["intro2"], "sortWeight": 2},
        {"id": "a", "name": "alpha", "tags": ["intro2"], "sortWeight": None},
    ]

    resp = client.get("/api/intro2/track", headers=auth("player-token"))
    assert resp.status_code == 200
    # Null weights collapse to 0 and therefore sort first, tie-broken by name.
    assert [s["challenge_id"] for s in resp.json()] == ["a", "b", "c"]
    assert [s["step"] for s in resp.json()] == [1, 2, 3]


def test_intro2_tolerates_v2_null_tags(
    client: TestClient, fake_client: FakeRctfClient
) -> None:
    """v2 sends `tags: null` for an untagged challenge, where v1 omitted the
    key. Both have to read as "not on the track"."""
    fake_client.identities["player-token"] = TeamIdentity("t1", "n1ght0wl", is_admin=False)
    fake_client.challenges = [
        {"id": "x", "name": "untagged", "tags": None, "sortWeight": None},
        {"id": "i1", "name": "step one", "tags": ["intro2"], "sortWeight": 1},
    ]

    resp = client.get("/api/intro2/track", headers=auth("player-token"))
    assert resp.status_code == 200
    assert [s["challenge_id"] for s in resp.json()] == ["i1"]


def test_decks_crud_requires_admin_for_writes(client: TestClient, fake_client: FakeRctfClient) -> None:
    fake_client.identities["admin-token"] = TeamIdentity("t1", "admin", is_admin=True)
    fake_client.identities["player-token"] = TeamIdentity("t2", "n1ght0wl", is_admin=False)

    forbidden = client.post(
        "/api/decks",
        json={"title": "Web 101", "meta": "24 slides", "file_url": "https://x/y.pdf"},
        headers=auth("player-token"),
    )
    assert forbidden.status_code == 403

    created = client.post(
        "/api/decks",
        json={"title": "Web 101", "meta": "24 slides", "file_url": "https://x/y.pdf"},
        headers=auth("admin-token"),
    )
    assert created.status_code == 200

    listed = client.get("/api/decks")
    assert len(listed.json()) == 1
    assert listed.json()[0]["title"] == "Web 101"


def test_discord_config_roundtrip(client: TestClient, fake_client: FakeRctfClient) -> None:
    fake_client.identities["admin-token"] = TeamIdentity("t1", "admin", is_admin=True)

    put = client.put(
        "/api/admin/discord-config",
        json={"webhook_url": "https://discord.com/api/webhooks/1/2"},
        headers=auth("admin-token"),
    )
    assert put.status_code == 200

    # The URL is write-only by design (it is a credential): the response says
    # only whether one is set, and must never echo it back.
    got = client.get("/api/admin/discord-config", headers=auth("admin-token"))
    assert "webhook_url" not in got.json()
    assert got.json()["webhook_configured"] is True


def test_admin_stats(client: TestClient, fake_client: FakeRctfClient) -> None:
    fake_client.identities["admin-token"] = TeamIdentity("t1", "admin", is_admin=True)
    fake_client.identities["player-token"] = TeamIdentity("t2", "n1ght0wl", is_admin=False)
    fake_client.challenges = [{"id": "c1", "name": "cookie_monster"}, {"id": "c2", "name": "ret2win"}]
    fake_client.solves["t2"] = {"c1"}
    # Set, but must not show up below: player and challenge counts are rCTF's
    # to answer and the frontend asks it directly.
    fake_client.leaderboard_size = 42

    forbidden = client.get("/api/admin/stats", headers=auth("player-token"))
    assert forbidden.status_code == 403

    client.post(
        "/api/writeups/c1/submit",
        json={"body_md": "approach\n\n:::solution\n\nexploit", "summary": "..."},
        headers=auth("player-token"),
    )

    stats = client.get("/api/admin/stats", headers=auth("admin-token"))
    assert stats.status_code == 200
    body = stats.json()
    assert body == {"submissions": 1, "pending_writeups": 1}


def test_patching_a_deck_leaves_omitted_fields_alone(
    client: TestClient, fake_client: FakeRctfClient
) -> None:
    fake_client.identities["admin-token"] = TeamIdentity("t1", "admin", is_admin=True)
    created = client.post(
        "/api/decks",
        json={"title": "Web 101", "meta": "24 slides", "file_url": "https://x/y.pdf", "sort_order": 3},
        headers=auth("admin-token"),
    ).json()

    patched = client.patch(
        f"/api/decks/{created['id']}",
        json={"title": "Web 102"},
        headers=auth("admin-token"),
    )
    assert patched.status_code == 200
    assert patched.json() == {**created, "title": "Web 102"}

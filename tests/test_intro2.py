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


def test_intro2_tracks_are_independent(client: TestClient, fake_client: FakeRctfClient) -> None:
    """The whole point of per-category tracks: each category unlocks on its own.
    Two solves deep into pwn must not open web's second step, and web's first
    step is open from the start even with nothing solved in it."""
    fake_client.identities["player-token"] = TeamIdentity("t1", "n1ght0wl", is_admin=False)
    fake_client.challenges = [
        {"id": "p1", "name": "Stack Smash", "category": "pwn", "tags": ["intro2"], "sortWeight": 1},
        {"id": "p2", "name": "Ret2win", "category": "pwn", "tags": ["intro2"], "sortWeight": 2},
        {"id": "p3", "name": "ROP", "category": "pwn", "tags": ["intro2"], "sortWeight": 3},
        {"id": "w1", "name": "Inspect Element", "category": "web", "tags": ["intro2"], "sortWeight": 1},
        {"id": "w2", "name": "Cookie Jar", "category": "web", "tags": ["intro2"], "sortWeight": 2},
        {"id": "other", "name": "not intro2", "category": "web", "tags": ["web"], "sortWeight": 1},
    ]
    fake_client.solves["t1"] = {"p1", "p2"}

    resp = client.get("/api/intro2/tracks", headers=auth("player-token"))
    assert resp.status_code == 200
    tracks = {t["category"]: t["steps"] for t in resp.json()}
    assert set(tracks) == {"pwn", "web"}

    assert {s["challenge_id"]: s["status"] for s in tracks["pwn"]} == {
        "p1": "done",
        "p2": "done",
        "p3": "in_progress",
    }
    # web is untouched by pwn's progress: its own step 1 is in progress.
    assert {s["challenge_id"]: s["status"] for s in tracks["web"]} == {
        "w1": "in_progress",
        "w2": "locked",
    }


def test_intro2_numbers_steps_from_one_per_track(
    client: TestClient, fake_client: FakeRctfClient
) -> None:
    """Steps are numbered within their track, not across the whole tag - the
    page prints "STEP 01" at the top of every category."""
    fake_client.identities["player-token"] = TeamIdentity("t1", "n1ght0wl", is_admin=False)
    fake_client.challenges = [
        {"id": "p1", "name": "a", "category": "pwn", "tags": ["intro2"], "sortWeight": 1},
        {"id": "p2", "name": "b", "category": "pwn", "tags": ["intro2"], "sortWeight": 2},
        {"id": "r1", "name": "c", "category": "rev", "tags": ["intro2"], "sortWeight": 1},
    ]

    tracks = {t["category"]: t["steps"] for t in client.get(
        "/api/intro2/tracks", headers=auth("player-token")
    ).json()}
    assert [s["step"] for s in tracks["pwn"]] == [1, 2]
    assert [s["step"] for s in tracks["rev"]] == [1]


def test_intro2_groups_categories_case_insensitively(
    client: TestClient, fake_client: FakeRctfClient
) -> None:
    """rCTF does not normalise the category an author types, so `Web` and `web`
    would otherwise be two tracks with two step 1s."""
    fake_client.identities["player-token"] = TeamIdentity("t1", "n1ght0wl", is_admin=False)
    fake_client.challenges = [
        {"id": "w1", "name": "a", "category": "Web", "tags": ["intro2"], "sortWeight": 1},
        {"id": "w2", "name": "b", "category": " web ", "tags": ["intro2"], "sortWeight": 2},
    ]

    tracks = client.get("/api/intro2/tracks", headers=auth("player-token")).json()
    assert [t["category"] for t in tracks] == ["web"]
    assert [s["challenge_id"] for s in tracks[0]["steps"]] == ["w1", "w2"]
    # The step keeps rCTF's own casing: it is what the challenge modal opens.
    assert tracks[0]["steps"][0]["category"] == "Web"


def test_intro2_skips_a_challenge_with_no_category(
    client: TestClient, fake_client: FakeRctfClient
) -> None:
    """Category *is* the track now, so a tagged challenge without one has no
    track to belong to. It is left out rather than inventing a bucket for it."""
    fake_client.identities["player-token"] = TeamIdentity("t1", "n1ght0wl", is_admin=False)
    fake_client.challenges = [
        {"id": "x", "name": "no category", "tags": ["intro2"], "sortWeight": 1},
        {"id": "y", "name": "null category", "category": None, "tags": ["intro2"], "sortWeight": 2},
        {"id": "w1", "name": "fine", "category": "web", "tags": ["intro2"], "sortWeight": 3},
    ]

    tracks = client.get("/api/intro2/tracks", headers=auth("player-token")).json()
    assert [t["category"] for t in tracks] == ["web"]
    assert [s["challenge_id"] for s in tracks[0]["steps"]] == ["w1"]


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
            "category": "misc",
            "tags": ["intro2"],
            "sortWeight": 1,
            "files": [{"name": "hint.txt", "url": "/uploads/abc/hint.txt", "size": 12}],
        },
    ]

    tracks = client.get("/api/intro2/tracks", headers=auth("player-token")).json()
    step = tracks[0]["steps"][0]
    assert step["category"] == "misc"
    assert step["description"] == "Find the flag format."
    assert step["files"] == [{"name": "hint.txt", "url": "/uploads/abc/hint.txt", "size": 12}]


def test_intro2_tolerates_junk_in_the_files_field(
    client: TestClient, fake_client: FakeRctfClient
) -> None:
    """`files` comes from rCTF, so its shape is not ours to trust - a bad entry
    must not 500 the whole track."""
    fake_client.identities["player-token"] = TeamIdentity("t1", "n1ght0wl", is_admin=False)
    fake_client.challenges = [
        {"id": "a", "name": "no files key", "category": "web", "tags": ["intro2"], "sortWeight": 1},
        {"id": "b", "name": "null", "category": "web", "tags": ["intro2"], "sortWeight": 2, "files": None},
        {"id": "c", "name": "not a list", "category": "web", "tags": ["intro2"], "sortWeight": 3, "files": "x"},
        {
            "id": "d",
            "name": "partly usable",
            "category": "web",
            "tags": ["intro2"],
            "sortWeight": 4,
            "files": [
                {"name": "ok.txt", "url": "/uploads/a/ok.txt", "size": None},
                {"name": "no url"},
                "not-a-dict",
            ],
        },
    ]

    resp = client.get("/api/intro2/tracks", headers=auth("player-token"))
    assert resp.status_code == 200
    steps = {s["challenge_id"]: s["files"] for s in resp.json()[0]["steps"]}
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
        {"id": "i1", "name": "Your First Flag", "category": "web", "sortWeight": 1},
        {"id": "i2", "name": "Inspect Element", "category": "web", "sortWeight": 2},
    ]

    resp = client.get("/api/intro2/tracks", headers=auth("player-token"))
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
        {"id": "b", "name": "beta", "category": "rev", "tags": ["intro2"], "sortWeight": None},
        {"id": "c", "name": "gamma", "category": "rev", "tags": ["intro2"], "sortWeight": 2},
        {"id": "a", "name": "alpha", "category": "rev", "tags": ["intro2"], "sortWeight": None},
    ]

    resp = client.get("/api/intro2/tracks", headers=auth("player-token"))
    assert resp.status_code == 200
    steps = resp.json()[0]["steps"]
    # Null weights collapse to 0 and therefore sort first, tie-broken by name.
    assert [s["challenge_id"] for s in steps] == ["a", "b", "c"]
    assert [s["step"] for s in steps] == [1, 2, 3]


def test_intro2_tolerates_v2_null_tags(
    client: TestClient, fake_client: FakeRctfClient
) -> None:
    """v2 sends `tags: null` for an untagged challenge, where v1 omitted the
    key. Both have to read as "not on the track"."""
    fake_client.identities["player-token"] = TeamIdentity("t1", "n1ght0wl", is_admin=False)
    fake_client.challenges = [
        {"id": "x", "name": "untagged", "category": "web", "tags": None, "sortWeight": None},
        {"id": "i1", "name": "step one", "category": "web", "tags": ["intro2"], "sortWeight": 1},
    ]

    resp = client.get("/api/intro2/tracks", headers=auth("player-token"))
    assert resp.status_code == 200
    assert [s["challenge_id"] for s in resp.json()[0]["steps"]] == ["i1"]


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

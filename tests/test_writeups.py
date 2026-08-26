from datetime import datetime, timedelta

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

INTRO = "The nonce is reused between messages 3 and 7."
SOLUTION = "```python\nkey = xor(c3, c7)\n```"
BODY = f"{INTRO}\n\n:::solution\n\n{SOLUTION}"


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def setup_teams(fake: FakeRctfClient) -> None:
    fake.identities["player-token"] = TeamIdentity("t1", "n1ght0wl", is_admin=False)
    fake.identities["admin-token"] = TeamIdentity("t2", "admin", is_admin=True)
    fake.identities["outsider-token"] = TeamIdentity("t3", "outsider", is_admin=False)
    fake.challenges = [{"id": "c1", "name": "cookie_monster"}]
    fake.solves["t1"] = {"c1"}


def submit(client: TestClient, body: str = BODY, summary: str = "HMAC truncated") -> int:
    resp = client.post(
        "/api/writeups/c1/submit",
        json={"body_md": body, "summary": summary},
        headers=auth("player-token"),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def publish(client: TestClient, writeup_id: int) -> None:
    resp = client.post(f"/api/writeups/{writeup_id}/approve", headers=auth("admin-token"))
    assert resp.status_code == 200, resp.text


def test_requires_auth(client: TestClient) -> None:
    assert client.get("/api/writeups/mine").status_code == 401
    assert client.get("/api/writeups").status_code == 401


def test_submit_blocked_when_not_solved(client: TestClient, fake_client: FakeRctfClient) -> None:
    setup_teams(fake_client)
    resp = client.post(
        "/api/writeups/c1/submit",
        json={"body_md": BODY, "summary": "..."},
        headers=auth("outsider-token"),
    )
    assert resp.status_code == 403


def test_submit_without_marker_is_rejected(client: TestClient, fake_client: FakeRctfClient) -> None:
    setup_teams(fake_client)
    resp = client.post(
        "/api/writeups/c1/submit",
        json={"body_md": "just prose", "summary": "..."},
        headers=auth("player-token"),
    )
    assert resp.status_code == 422
    assert ":::solution" in resp.json()["detail"]


def test_solution_never_reaches_a_team_that_has_not_solved(
    client: TestClient, fake_client: FakeRctfClient
) -> None:
    """The load-bearing test: a non-solver gets the intro and *no trace* of
    the gated half anywhere in the payload."""
    setup_teams(fake_client)
    writeup_id = submit(client)
    publish(client, writeup_id)

    resp = client.get(f"/api/writeups/item/{writeup_id}", headers=auth("outsider-token"))
    assert resp.status_code == 200
    payload = resp.json()

    assert payload["redacted"] is True
    assert payload["solution_md"] is None
    assert payload["intro_md"] == INTRO
    # Not just the field - the text must not appear anywhere in the response.
    assert "xor(c3, c7)" not in resp.text


def test_solver_gets_the_full_writeup(client: TestClient, fake_client: FakeRctfClient) -> None:
    setup_teams(fake_client)
    fake_client.solves["t3"] = {"c1"}
    writeup_id = submit(client)
    publish(client, writeup_id)

    payload = client.get(
        f"/api/writeups/item/{writeup_id}", headers=auth("outsider-token")
    ).json()
    assert payload["redacted"] is False
    assert payload["solution_md"] == SOLUTION


def test_author_and_admin_read_their_own_unpublished_writeup(
    client: TestClient, fake_client: FakeRctfClient
) -> None:
    setup_teams(fake_client)
    writeup_id = submit(client)

    author = client.get(f"/api/writeups/item/{writeup_id}", headers=auth("player-token"))
    assert author.json()["solution_md"] == SOLUTION

    admin = client.get(f"/api/writeups/item/{writeup_id}", headers=auth("admin-token"))
    assert admin.json()["solution_md"] == SOLUTION

    # Everyone else cannot even see that it exists until it is published.
    assert (
        client.get(f"/api/writeups/item/{writeup_id}", headers=auth("outsider-token")).status_code
        == 404
    )


def test_cards_list_only_published_and_carries_no_body(
    client: TestClient, fake_client: FakeRctfClient
) -> None:
    setup_teams(fake_client)
    writeup_id = submit(client)

    assert client.get("/api/writeups", headers=auth("outsider-token")).json() == []

    publish(client, writeup_id)
    resp = client.get("/api/writeups", headers=auth("outsider-token"))
    cards = resp.json()
    assert len(cards) == 1
    # No challenge_name on the wire - the client joins it from rCTF's list.
    assert cards[0]["challenge_id"] == "c1"
    assert "challenge_name" not in cards[0]
    assert cards[0]["team_name"] == "n1ght0wl"
    assert "solution_md" not in cards[0]
    assert "xor(c3, c7)" not in resp.text
    assert INTRO not in resp.text

    assert len(client.get("/api/writeups?challenge_id=c1", headers=auth("player-token")).json()) == 1
    assert client.get("/api/writeups?challenge_id=nope", headers=auth("player-token")).json() == []


def test_flags_are_scrubbed_from_the_public_half(
    client: TestClient, fake_client: FakeRctfClient
) -> None:
    setup_teams(fake_client)
    leaky = f"Turns out the answer is friday{{th1s_1s_th3_fl4g}} btw.\n\n:::solution\n\n{SOLUTION}"
    writeup_id = submit(client, body=leaky)
    publish(client, writeup_id)

    resp = client.get(f"/api/writeups/item/{writeup_id}", headers=auth("outsider-token"))
    assert "th1s_1s_th3_fl4g" not in resp.text
    assert "friday{[redacted]}" in resp.json()["intro_md"]


def test_reject_requires_a_reason_and_shows_it_to_the_author_only(
    client: TestClient, fake_client: FakeRctfClient
) -> None:
    setup_teams(fake_client)
    writeup_id = submit(client)

    no_reason = client.post(f"/api/writeups/{writeup_id}/reject", json={}, headers=auth("admin-token"))
    assert no_reason.status_code == 422
    blank = client.post(
        f"/api/writeups/{writeup_id}/reject", json={"reason": ""}, headers=auth("admin-token")
    )
    assert blank.status_code == 422

    rejected = client.post(
        f"/api/writeups/{writeup_id}/reject",
        json={"reason": "The flag is in the intro - move it below the marker."},
        headers=auth("admin-token"),
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"

    mine = client.get("/api/writeups/mine", headers=auth("player-token")).json()
    assert mine[0]["reject_reason"].startswith("The flag is in the intro")


def test_author_can_edit_a_rejected_writeup_and_it_returns_to_pending(
    client: TestClient, fake_client: FakeRctfClient
) -> None:
    setup_teams(fake_client)
    writeup_id = submit(client)
    client.post(
        f"/api/writeups/{writeup_id}/reject",
        json={"reason": "needs more detail"},
        headers=auth("admin-token"),
    )

    fixed = client.put(
        f"/api/writeups/item/{writeup_id}",
        json={"body_md": f"Better intro.\n\n:::solution\n\n{SOLUTION}", "summary": "v2"},
        headers=auth("player-token"),
    )
    assert fixed.status_code == 200
    assert fixed.json()["status"] == "pending"
    assert fixed.json()["reject_reason"] is None
    assert fixed.json()["intro_md"] == "Better intro."

    assert len(client.get("/api/writeups/queue", headers=auth("admin-token")).json()) == 1


def test_published_writeups_are_frozen_and_others_cannot_edit(
    client: TestClient, fake_client: FakeRctfClient
) -> None:
    setup_teams(fake_client)
    writeup_id = submit(client)

    other = client.put(
        f"/api/writeups/item/{writeup_id}",
        json={"body_md": BODY, "summary": "hijacked"},
        headers=auth("outsider-token"),
    )
    assert other.status_code == 404

    publish(client, writeup_id)
    frozen = client.put(
        f"/api/writeups/item/{writeup_id}",
        json={"body_md": BODY, "summary": "sneaky edit"},
        headers=auth("player-token"),
    )
    assert frozen.status_code == 409


def test_queue_shows_both_halves_to_admins_only(
    client: TestClient, fake_client: FakeRctfClient
) -> None:
    setup_teams(fake_client)
    submit(client)

    assert client.get("/api/writeups/queue", headers=auth("player-token")).status_code == 403

    queue = client.get("/api/writeups/queue", headers=auth("admin-token")).json()
    assert len(queue) == 1
    assert queue[0]["solution_md"] == SOLUTION


def test_timestamps_carry_a_utc_offset(client: TestClient, fake_client: FakeRctfClient) -> None:
    """Without one, a browser reads the value as local time."""
    setup_teams(fake_client)
    writeup_id = submit(client)
    client.post(f"/api/writeups/{writeup_id}/approve", headers=auth("admin-token"))

    detail = client.get(f"/api/writeups/item/{writeup_id}", headers=auth("player-token")).json()
    card = client.get("/api/writeups", headers=auth("player-token")).json()[0]

    for value in (detail["created_at"], detail["reviewed_at"], card["created_at"]):
        assert datetime.fromisoformat(value).utcoffset() == timedelta(0), value


def test_a_maximum_length_rejection_reason_still_notifies(
    client: TestClient, fake_client: FakeRctfClient, monkeypatch
) -> None:
    """`reason` allows 2000 characters; a Discord field value allows 1024."""
    from app.config import settings

    monkeypatch.setattr(settings, "discord_webhook_url", "https://discord.example/hook")
    setup_teams(fake_client)
    writeup_id = submit(client)

    payloads: list[dict] = []

    class Resp:
        status_code = 204

    async def fake_post(self, url, json):
        payloads.append(json)
        return Resp()

    monkeypatch.setattr("httpx.AsyncClient.post", fake_post)

    rejected = client.post(
        f"/api/writeups/{writeup_id}/reject",
        json={"reason": "r" * 2000},
        headers=auth("admin-token"),
    )
    assert rejected.status_code == 200
    values = [f["value"] for f in payloads[0]["embeds"][0]["fields"]]
    assert values and all(1 <= len(v) <= 1024 for v in values), [len(v) for v in values]


# `/delete` is a misnomer kept for the admin UI: nothing is destroyed, the
# writeup goes back to `pending`. The tests below pin that, because a route
# named delete that silently started deleting would pass a laxer suite.


def test_admin_delete_sends_a_published_writeup_back_to_the_queue(
    client: TestClient, fake_client: FakeRctfClient
) -> None:
    setup_teams(fake_client)
    writeup_id = submit(client)
    publish(client, writeup_id)
    assert len(client.get("/api/writeups", headers=auth("player-token")).json()) == 1

    sent_back = client.post(
        f"/api/writeups/{writeup_id}/delete", headers=auth("admin-token")
    )
    assert sent_back.status_code == 200, sent_back.text
    body = sent_back.json()
    assert body["status"] == "pending"
    # Unreviewed again: a leftover stamp would credit whoever approved it last
    # time for a decision that has just been taken back.
    assert body["reviewed_by"] is None
    assert body["reviewed_at"] is None
    assert body["reject_reason"] is None

    # Off the public grid, back in front of the admins - and still readable by
    # its author, which is what makes this recoverable rather than a deletion.
    assert client.get("/api/writeups", headers=auth("player-token")).json() == []
    queue = client.get("/api/writeups/queue", headers=auth("admin-token")).json()
    assert [w["id"] for w in queue] == [writeup_id]
    mine = client.get("/api/writeups/mine", headers=auth("player-token")).json()
    assert [w["id"] for w in mine] == [writeup_id]
    assert mine[0]["solution_md"] == SOLUTION


def test_delete_is_admin_only(client: TestClient, fake_client: FakeRctfClient) -> None:
    setup_teams(fake_client)
    writeup_id = submit(client)
    publish(client, writeup_id)

    assert client.post(f"/api/writeups/{writeup_id}/delete").status_code == 401
    # Not even the author gets to pull their own published writeup down.
    for token in ("player-token", "outsider-token"):
        resp = client.post(f"/api/writeups/{writeup_id}/delete", headers=auth(token))
        assert resp.status_code == 403, (token, resp.text)

    still_up = client.get("/api/writeups", headers=auth("player-token")).json()
    assert [w["id"] for w in still_up] == [writeup_id]


def test_delete_only_applies_to_published_writeups(
    client: TestClient, fake_client: FakeRctfClient
) -> None:
    """A pending or rejected writeup has nothing to come back from.

    The 409 is what stops a double-click from wiping a rejection reason the
    author has not read yet.
    """
    setup_teams(fake_client)
    writeup_id = submit(client)

    pending = client.post(
        f"/api/writeups/{writeup_id}/delete", headers=auth("admin-token")
    )
    assert pending.status_code == 409

    client.post(
        f"/api/writeups/{writeup_id}/reject",
        json={"reason": "The flag is in the intro."},
        headers=auth("admin-token"),
    )
    rejected = client.post(
        f"/api/writeups/{writeup_id}/delete", headers=auth("admin-token")
    )
    assert rejected.status_code == 409

    mine = client.get("/api/writeups/mine", headers=auth("player-token")).json()
    assert mine[0]["status"] == "rejected"
    assert mine[0]["reject_reason"] == "The flag is in the intro."


def test_delete_of_an_unknown_writeup_is_404(
    client: TestClient, fake_client: FakeRctfClient
) -> None:
    setup_teams(fake_client)
    assert client.post("/api/writeups/999/delete", headers=auth("admin-token")).status_code == 404


def test_delete_unfreezes_the_author_and_edits_work_again(
    client: TestClient, fake_client: FakeRctfClient
) -> None:
    """The loop `edit_writeup`'s 409 message promises.

    It tells the author to ask an admin to unpublish - so unpublishing has to
    actually make the edit go through, or the advice is a dead end.
    """
    setup_teams(fake_client)
    writeup_id = submit(client)
    publish(client, writeup_id)

    frozen = client.put(
        f"/api/writeups/item/{writeup_id}",
        json={"body_md": BODY, "summary": "v2"},
        headers=auth("player-token"),
    )
    assert frozen.status_code == 409

    client.post(f"/api/writeups/{writeup_id}/delete", headers=auth("admin-token"))

    edited = client.put(
        f"/api/writeups/item/{writeup_id}",
        json={"body_md": f"Rewritten intro.\n\n:::solution\n\n{SOLUTION}", "summary": "v2"},
        headers=auth("player-token"),
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["intro_md"] == "Rewritten intro."


def test_votes_survive_a_trip_through_the_queue(
    client: TestClient, fake_client: FakeRctfClient
) -> None:
    """Unpublishing is editorial, not a score reset.

    Votes are rows, not a column on the writeup, so nothing here touches them
    - the tally is merely frozen while the writeup is out of the grid
    (`_apply_vote` refuses anything not published) and comes back intact.
    """
    setup_teams(fake_client)
    writeup_id = submit(client)
    publish(client, writeup_id)
    voted = client.post(
        f"/api/writeups/item/{writeup_id}/vote", headers=auth("outsider-token")
    )
    assert voted.status_code == 200
    assert voted.json()["votes"] == 1

    client.post(f"/api/writeups/{writeup_id}/delete", headers=auth("admin-token"))
    # Out of the grid, the vote cannot be changed either way.
    assert (
        client.post(
            f"/api/writeups/item/{writeup_id}/vote", headers=auth("outsider-token")
        ).status_code
        == 404
    )

    publish(client, writeup_id)
    card = client.get("/api/writeups", headers=auth("player-token")).json()[0]
    assert card["votes"] == 1


def test_delete_notifies_discord_as_an_unpublish(
    client: TestClient, fake_client: FakeRctfClient, monkeypatch
) -> None:
    """The embed has to read as the inverse of "approved", not as a deletion -
    `discord.post` falls back to the raw event name when the title is missing,
    which would put a bare lowercase "unpublished" in the channel."""
    from app.config import settings

    monkeypatch.setattr(settings, "discord_webhook_url", "https://discord.example/hook")
    # Without an origin `_review_url()` is None and the embed carries no link
    # at all - set one, so the assertion below tests where it points.
    monkeypatch.setattr(settings, "web_origin", "https://ctf.example")
    setup_teams(fake_client)
    writeup_id = submit(client)
    publish(client, writeup_id)

    payloads: list[dict] = []

    class Resp:
        status_code = 204

    async def fake_post(self, url, json):
        payloads.append(json)
        return Resp()

    monkeypatch.setattr("httpx.AsyncClient.post", fake_post)

    assert (
        client.post(
            f"/api/writeups/{writeup_id}/delete", headers=auth("admin-token")
        ).status_code
        == 200
    )

    embed = payloads[-1]["embeds"][0]
    assert "Writeup unpublished" in embed["title"]
    fields = {f["name"]: f["value"] for f in embed["fields"]}
    assert fields["Challenge"] == "cookie_monster"
    assert fields["Author"] == "n1ght0wl"
    assert fields["Removed by"] == "admin"
    # Linked at the review queue, since that is where the writeup now is.
    assert embed["url"] == "https://ctf.example/admin"

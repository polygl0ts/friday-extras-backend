import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import make_transient_to_detached
from sqlmodel import Session

from app.db import engine
from app.main import app
from app.models import WriteupVote
from app.rctf_client import TeamIdentity, get_rctf_client
from tests.fake_rctf import FakeRctfClient

BODY = "Approach.\n\n:::solution\n\nExploit."


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


@pytest.fixture()
def published(client: TestClient, fake_client: FakeRctfClient) -> int:
    fake_client.identities["author-token"] = TeamIdentity("t1", "author", is_admin=False)
    fake_client.identities["admin-token"] = TeamIdentity("t2", "admin", is_admin=True)
    fake_client.identities["reader-token"] = TeamIdentity("t3", "reader", is_admin=False)
    fake_client.identities["reader2-token"] = TeamIdentity("t4", "reader2", is_admin=False)
    fake_client.challenges = [{"id": "c1", "name": "cookie_monster"}]
    fake_client.solves["t1"] = {"c1"}

    submitted = client.post(
        "/api/writeups/c1/submit",
        json={"body_md": BODY, "summary": "s"},
        headers=auth("author-token"),
    )
    writeup_id = submitted.json()["id"]
    client.post(f"/api/writeups/{writeup_id}/approve", headers=auth("admin-token"))
    return writeup_id


def test_upvote_is_counted_and_reflected_back(client: TestClient, published: int) -> None:
    voted = client.post(f"/api/writeups/item/{published}/vote", headers=auth("reader-token"))
    assert voted.status_code == 200
    assert voted.json()["votes"] == 1
    assert voted.json()["voted"] is True

    # A second team's vote adds; it does not replace.
    assert (
        client.post(f"/api/writeups/item/{published}/vote", headers=auth("reader2-token")).json()[
            "votes"
        ]
        == 2
    )


def test_upvoting_twice_does_not_double_count(client: TestClient, published: int) -> None:
    client.post(f"/api/writeups/item/{published}/vote", headers=auth("reader-token"))
    again = client.post(f"/api/writeups/item/{published}/vote", headers=auth("reader-token"))
    assert again.json()["votes"] == 1


def test_vote_can_be_taken_back_and_removing_twice_is_safe(
    client: TestClient, published: int
) -> None:
    client.post(f"/api/writeups/item/{published}/vote", headers=auth("reader-token"))

    removed = client.delete(f"/api/writeups/item/{published}/vote", headers=auth("reader-token"))
    assert removed.json()["votes"] == 0
    assert removed.json()["voted"] is False

    again = client.delete(f"/api/writeups/item/{published}/vote", headers=auth("reader-token"))
    assert again.json()["votes"] == 0


def test_cannot_upvote_your_own_writeup(client: TestClient, published: int) -> None:
    resp = client.post(f"/api/writeups/item/{published}/vote", headers=auth("author-token"))
    assert resp.status_code == 403


def test_cannot_vote_on_an_unpublished_writeup(
    client: TestClient, fake_client: FakeRctfClient, published: int
) -> None:
    pending = client.post(
        "/api/writeups/c1/submit",
        json={"body_md": BODY, "summary": "second"},
        headers=auth("author-token"),
    ).json()["id"]

    assert (
        client.post(f"/api/writeups/item/{pending}/vote", headers=auth("reader-token")).status_code
        == 404
    )


def test_voting_needs_no_solve(client: TestClient, fake_client: FakeRctfClient, published: int) -> None:
    # reader has solved nothing: the intro is readable, so the vote counts.
    assert fake_client.solves.get("t3") is None
    resp = client.post(f"/api/writeups/item/{published}/vote", headers=auth("reader-token"))
    assert resp.status_code == 200
    # ...but the gated half stayed gated all the same.
    assert resp.json()["solution_md"] is None
    assert "Exploit." not in resp.text


def test_cards_carry_the_tally_and_sort_by_new_then_top(
    client: TestClient, fake_client: FakeRctfClient, published: int
) -> None:
    second = client.post(
        "/api/writeups/c1/submit",
        json={"body_md": BODY, "summary": "newer but unloved"},
        headers=auth("author-token"),
    ).json()["id"]
    client.post(f"/api/writeups/{second}/approve", headers=auth("admin-token"))
    client.post(f"/api/writeups/item/{published}/vote", headers=auth("reader-token"))

    by_new = client.get("/api/writeups", headers=auth("reader-token")).json()
    assert [w["id"] for w in by_new] == [second, published], "newest first by default"
    assert by_new[1]["votes"] == 1
    assert by_new[1]["voted"] is True

    by_top = client.get("/api/writeups?sort=top", headers=auth("reader-token")).json()
    assert [w["id"] for w in by_top] == [published, second], "most upvoted first"

    # Someone else's view of the same list knows they haven't voted.
    other = client.get("/api/writeups", headers=auth("reader2-token")).json()
    assert other[1]["votes"] == 1
    assert other[1]["voted"] is False


def test_bad_sort_value_is_rejected(client: TestClient, published: int) -> None:
    assert client.get("/api/writeups?sort=lolno", headers=auth("reader-token")).status_code == 422


def test_my_writeups_show_their_tally(client: TestClient, published: int) -> None:
    client.post(f"/api/writeups/item/{published}/vote", headers=auth("reader-token"))
    mine = client.get("/api/writeups/mine", headers=auth("author-token")).json()
    assert mine[0]["votes"] == 1
    assert mine[0]["voted"] is False


def test_losing_the_upvote_race_is_not_an_error(
    client: TestClient, published: int, monkeypatch
) -> None:
    """Another request inserts the same row between this one's lookup and its
    commit. The primary key catches it, and 'voted' is still the outcome."""
    with Session(engine) as session:
        session.add(WriteupVote(writeup_id=published, team_id="t3"))
        session.commit()

    real_get = Session.get

    def blind_to_votes(self, entity, *args, **kwargs):
        if entity is WriteupVote:
            return None
        return real_get(self, entity, *args, **kwargs)

    monkeypatch.setattr(Session, "get", blind_to_votes)

    voted = client.post(f"/api/writeups/item/{published}/vote", headers=auth("reader-token"))
    assert voted.status_code == 200
    assert voted.json()["votes"] == 1
    assert voted.json()["voted"] is True


@pytest.mark.filterwarnings("ignore::sqlalchemy.exc.SAWarning")
def test_losing_the_unvote_race_is_not_an_error(
    client: TestClient, published: int, monkeypatch
) -> None:
    """The mirror case: the row is already gone by the time this one deletes."""
    real_get = Session.get

    def pretend_a_vote_exists(self, entity, *args, **kwargs):
        if entity is WriteupVote:
            # A row this session believes is there, but the database no longer
            # has - which is what the losing side of the race is holding.
            vote = WriteupVote(writeup_id=published, team_id="t3")
            make_transient_to_detached(vote)
            self.add(vote)
            return vote
        return real_get(self, entity, *args, **kwargs)

    monkeypatch.setattr(Session, "get", pretend_a_vote_exists)

    unvoted = client.delete(f"/api/writeups/item/{published}/vote", headers=auth("reader-token"))
    assert unvoted.status_code == 200
    assert unvoted.json()["votes"] == 0
    assert unvoted.json()["voted"] is False

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import engine
from app.main import app
from app.models import WriteupGrade
from app.rctf_client import TeamIdentity, get_rctf_client
from tests.fake_rctf import FakeRctfClient

BODY = "Approach.\n\n:::solution\n\nExploit."

# A full sheet: five rated 1-5, two yes/no checks.
SHEET = {
    "technical": 5,
    "clarity": 4,
    "completeness": 3,
    "originality": 2,
    "narrative": 1,
    "reproducibility": True,
    "format": False,
}
# (5+4+3+2+1 + 5 + 1) / 7
SHEET_SCORE = 3.0


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
def pending(client: TestClient, fake_client: FakeRctfClient) -> int:
    fake_client.identities["author-token"] = TeamIdentity("t1", "author", is_admin=False)
    fake_client.identities["admin-token"] = TeamIdentity("t2", "admin", is_admin=True)
    fake_client.identities["admin2-token"] = TeamIdentity("t3", "admin2", is_admin=True)
    fake_client.identities["reader-token"] = TeamIdentity("t4", "reader", is_admin=False)
    fake_client.challenges = [{"id": "c1", "name": "cookie_monster"}]
    fake_client.solves["t1"] = {"c1"}

    return client.post(
        "/api/writeups/c1/submit",
        json={"body_md": BODY, "summary": "s"},
        headers=auth("author-token"),
    ).json()["id"]


@pytest.fixture()
def published(client: TestClient, pending: int) -> int:
    client.post(f"/api/writeups/{pending}/approve", headers=auth("admin-token"))
    return pending


def grade(client: TestClient, writeup_id: int, token: str, sheet: dict = SHEET):
    return client.put(
        f"/api/writeups/item/{writeup_id}/grade", json={"scores": sheet}, headers=auth(token)
    )


def test_criteria_describe_the_sheet(client: TestClient, pending: int) -> None:
    resp = client.get("/api/writeups/criteria", headers=auth("admin-token"))
    assert resp.status_code == 200
    assert resp.json() == {
        "rated": ["technical", "clarity", "completeness", "originality", "narrative"],
        "checks": ["reproducibility", "format"],
        "min": 1,
        "max": 5,
    }
    assert client.get("/api/writeups/criteria", headers=auth("reader-token")).status_code == 403


def test_a_sheet_scores_the_writeup_and_comes_back_as_my_grade(
    client: TestClient, pending: int
) -> None:
    resp = grade(client, pending, "admin-token")
    assert resp.status_code == 200
    body = resp.json()
    assert body["score"] == SHEET_SCORE
    assert body["graders"] == 1
    assert body["my_grade"] == SHEET
    assert body["grades"] == [{"grader_team_id": "t2", "scores": SHEET}]


def test_score_is_the_mean_over_admins(client: TestClient, pending: int) -> None:
    grade(client, pending, "admin-token")
    perfect = {k: (True if isinstance(v, bool) else 5) for k, v in SHEET.items()}
    body = grade(client, pending, "admin2-token", perfect).json()
    assert body["graders"] == 2
    assert body["score"] == (SHEET_SCORE + 5) / 2
    # Each admin sees their own sheet under my_grade, and everyone's under grades.
    assert body["my_grade"] == perfect
    assert {g["grader_team_id"] for g in body["grades"]} == {"t2", "t3"}


def test_regrading_replaces_rather_than_adds(client: TestClient, pending: int) -> None:
    grade(client, pending, "admin-token")
    lower = {**SHEET, "technical": 1, "reproducibility": False}
    body = grade(client, pending, "admin-token", lower).json()
    assert body["graders"] == 1
    assert body["my_grade"] == lower
    assert body["score"] == round((1 + 4 + 3 + 2 + 1 + 1 + 1) / 7, 2)


def test_only_admins_grade_and_never_their_own(
    client: TestClient, fake_client: FakeRctfClient, pending: int
) -> None:
    assert grade(client, pending, "reader-token").status_code == 403
    # An admin who wrote the writeup does not get to grade it either.
    fake_client.identities["author-token"] = TeamIdentity("t1", "author", is_admin=True)
    assert grade(client, pending, "author-token").status_code == 403
    assert grade(client, 9999, "admin-token").status_code == 404


def test_grading_works_on_pending_and_published_alike(
    client: TestClient, published: int
) -> None:
    # `published` approved the `pending` writeup; the fixture above graded
    # nothing, so this is the first sheet on an already-published writeup.
    assert grade(client, published, "admin-token").json()["score"] == SHEET_SCORE


@pytest.mark.parametrize(
    "broken",
    [
        {k: v for k, v in SHEET.items() if k != "format"},  # missing a criterion
        {**SHEET, "style": 3},  # unknown criterion
        {**SHEET, "technical": 0},  # below the scale
        {**SHEET, "technical": 6},  # above the scale
        {**SHEET, "technical": True},  # a tick in a rated box
        {**SHEET, "format": 3},  # a mark in a checkbox
        {**SHEET, "clarity": "4"},  # wrong type altogether
    ],
)
def test_incomplete_or_malformed_sheets_are_rejected(
    client: TestClient, pending: int, broken: dict
) -> None:
    assert grade(client, pending, "admin-token", broken).status_code == 422
    with Session(engine) as session:
        assert session.get(WriteupGrade, (pending, "t2")) is None


def test_players_see_the_score_but_not_the_sheets(client: TestClient, published: int) -> None:
    grade(client, published, "admin-token")

    item = client.get(f"/api/writeups/item/{published}", headers=auth("reader-token")).json()
    assert item["score"] == SHEET_SCORE
    assert item["graders"] == 1
    assert item["my_grade"] is None
    assert item["grades"] is None

    # The author is an owner_view but not an admin: same rule.
    mine = client.get("/api/writeups/mine", headers=auth("author-token")).json()
    assert mine[0]["score"] == SHEET_SCORE
    assert mine[0]["grades"] is None

    card = client.get("/api/writeups", headers=auth("reader-token")).json()[0]
    assert card["score"] == SHEET_SCORE
    assert card["graders"] == 1
    assert "grades" not in card

    # An admin reading it back gets the breakdown, and their own sheet.
    admin = client.get(f"/api/writeups/item/{published}", headers=auth("admin-token")).json()
    assert admin["my_grade"] == SHEET
    assert admin["grades"] == [{"grader_team_id": "t2", "scores": SHEET}]
    # ...while an admin who hasn't graded yet sees no `my_grade` but everyone else's.
    other = client.get(f"/api/writeups/item/{published}", headers=auth("admin2-token")).json()
    assert other["my_grade"] is None
    assert other["grades"] == [{"grader_team_id": "t2", "scores": SHEET}]


def test_ungraded_writeup_has_no_score(client: TestClient, published: int) -> None:
    item = client.get(f"/api/writeups/item/{published}", headers=auth("admin-token")).json()
    assert item["score"] is None
    assert item["graders"] == 0
    assert item["my_grade"] is None
    assert item["grades"] == []


def test_queue_carries_each_admins_own_sheet(client: TestClient, pending: int) -> None:
    grade(client, pending, "admin-token")
    queue = client.get("/api/writeups/queue", headers=auth("admin2-token")).json()
    assert queue[0]["id"] == pending
    assert queue[0]["score"] == SHEET_SCORE
    assert queue[0]["my_grade"] is None
    assert queue[0]["grades"][0]["grader_team_id"] == "t2"


def test_losing_the_insert_race_still_stores_this_sheet(
    client: TestClient, pending: int, monkeypatch
) -> None:
    """The same admin's other request inserted between this one's lookup and
    its commit. The primary key catches it, and this sheet wins."""
    with Session(engine) as session:
        session.add(WriteupGrade(writeup_id=pending, grader_team_id="t2", scores=SHEET))
        session.commit()

    real_get = Session.get
    seen: list[type] = []

    def blind_once(self, entity, *args, **kwargs):
        if entity is WriteupGrade and not seen:
            seen.append(entity)
            return None
        return real_get(self, entity, *args, **kwargs)

    monkeypatch.setattr(Session, "get", blind_once)

    lower = {**SHEET, "technical": 1}
    resp = grade(client, pending, "admin-token", lower)
    assert resp.status_code == 200
    assert resp.json()["graders"] == 1
    assert resp.json()["my_grade"] == lower

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


def test_events_crud_requires_admin_for_writes(client: TestClient, fake_client: FakeRctfClient) -> None:
    fake_client.identities["admin-token"] = TeamIdentity("t1", "admin", is_admin=True)
    fake_client.identities["player-token"] = TeamIdentity("t2", "n1ght0wl", is_admin=False)
    event = {"title": "Pwn workshop", "starts_at": "2026-10-09T16:15:00Z", "location": "BC 410"}

    assert client.post("/api/events", json=event, headers=auth("player-token")).status_code == 403
    created = client.post("/api/events", json=event, headers=auth("admin-token"))
    assert created.status_code == 200
    event_id = created.json()["id"]

    assert client.delete(f"/api/events/{event_id}", headers=auth("player-token")).status_code == 403
    assert client.delete(f"/api/events/{event_id}", headers=auth("admin-token")).status_code == 204
    assert client.get("/api/events").json() == []


def test_events_list_is_public_sorted_and_keeps_utc(
    client: TestClient, fake_client: FakeRctfClient
) -> None:
    """Listed oldest first whatever the insert order, and every time carries
    its offset - SQLite hands it back naive, and a naive time on the wire is
    read by the browser as local time."""
    fake_client.identities["admin-token"] = TeamIdentity("t1", "admin", is_admin=True)
    for title, starts_at in [("later", "2026-11-06T16:15:00Z"), ("sooner", "2026-10-09T16:15:00Z")]:
        client.post(
            "/api/events",
            json={"title": title, "starts_at": starts_at},
            headers=auth("admin-token"),
        )

    listed = client.get("/api/events")
    assert listed.status_code == 200
    assert [e["title"] for e in listed.json()] == ["sooner", "later"]
    assert listed.json()[0]["starts_at"] in ("2026-10-09T16:15:00Z", "2026-10-09T16:15:00+00:00")

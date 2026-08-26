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


def test_me_requires_auth(client: TestClient) -> None:
    assert client.get("/api/me").status_code == 401


def test_me_reflects_admin_flag(client: TestClient, fake_client: FakeRctfClient) -> None:
    fake_client.identities["admin-token"] = TeamIdentity("t1", "admin", is_admin=True)
    fake_client.identities["player-token"] = TeamIdentity("t2", "n1ght0wl", is_admin=False)

    admin_resp = client.get("/api/me", headers={"Authorization": "Bearer admin-token"})
    assert admin_resp.json() == {"team_id": "t1", "team_name": "admin", "is_admin": True}

    player_resp = client.get("/api/me", headers={"Authorization": "Bearer player-token"})
    assert player_resp.json()["is_admin"] is False

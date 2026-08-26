"""Where writeup notifications go, and what the config endpoint refuses to say.

This service posts one kind of notification - the writeup lifecycle - down one
webhook. First bloods used to have a second webhook here; rCTF's own blood bot
announces them now, so the only thing left to pin is the admin-override /
vault-fallback precedence and the fact that neither layer ever echoes a URL
back to a client.
"""

import pytest
from fastapi.testclient import TestClient

from app import discord
from app.main import app
from app.models import DiscordConfig
from app.rctf_client import TeamIdentity, get_rctf_client
from tests.fake_rctf import FakeRctfClient

MAIN = "https://discord.com/api/webhooks/main"
VAULT = "https://discord.com/api/webhooks/vault"


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


@pytest.fixture()
def admin(fake_client: FakeRctfClient) -> dict[str, str]:
    fake_client.identities["admin-token"] = TeamIdentity("t1", "admin", is_admin=True)
    return {"Authorization": "Bearer admin-token"}


@pytest.fixture()
def settings(monkeypatch):
    from app.config import settings as real

    monkeypatch.setattr(real, "discord_webhook_url", "", raising=False)
    return real


def test_every_writeup_event_uses_the_one_webhook(settings) -> None:
    config = DiscordConfig(id=1, webhook_url=MAIN)

    for event in ("submitted", "approved", "rejected", "test"):
        assert discord.effective_webhook_url(config) == MAIN, event


def test_first_blood_is_no_longer_an_event_this_service_posts() -> None:
    """rCTF's blood bot owns the announcement. A title here would mean this
    service had started posting them again - i.e. every blood posted twice."""
    assert "first-blood" not in discord._EVENT_TITLES


def test_the_vault_value_is_used_when_nothing_is_set_in_the_admin_ui(settings) -> None:
    settings.discord_webhook_url = VAULT

    assert discord.effective_webhook_url(DiscordConfig(id=1)) == VAULT


def test_an_admin_override_beats_the_vault(settings) -> None:
    settings.discord_webhook_url = VAULT

    assert discord.effective_webhook_url(DiscordConfig(id=1, webhook_url=MAIN)) == MAIN


def test_nothing_configured_anywhere_means_no_notification(settings) -> None:
    assert discord.effective_webhook_url(DiscordConfig(id=1)) == ""


def test_the_url_is_never_echoed_back_by_the_api(client, admin) -> None:
    client.put("/api/admin/discord-config", json={"webhook_url": MAIN}, headers=admin)

    body = client.get("/api/admin/discord-config", headers=admin).json()
    assert body == {"webhook_configured": True}
    assert MAIN not in str(body)


def test_a_blank_save_keeps_the_stored_url(client, admin) -> None:
    """The form never receives the secret, so saving it back must not wipe it."""
    client.put("/api/admin/discord-config", json={"webhook_url": MAIN}, headers=admin)

    saved = client.put("/api/admin/discord-config", json={}, headers=admin).json()
    assert saved == {"webhook_configured": True}


def test_clearing_is_its_own_verb(client, admin, settings) -> None:
    client.put("/api/admin/discord-config", json={"webhook_url": MAIN}, headers=admin)

    cleared = client.delete("/api/admin/discord-config/webhook", headers=admin).json()
    assert cleared == {"webhook_configured": False}


def test_clearing_falls_back_to_the_vault_rather_than_going_silent(
    client, admin, settings
) -> None:
    settings.discord_webhook_url = VAULT
    client.put("/api/admin/discord-config", json={"webhook_url": MAIN}, headers=admin)

    cleared = client.delete("/api/admin/discord-config/webhook", headers=admin).json()
    # Still configured - the override is gone, the vault value underneath is not.
    assert cleared == {"webhook_configured": True}


def test_a_test_posts_to_the_effective_webhook(client, admin, monkeypatch) -> None:
    client.put("/api/admin/discord-config", json={"webhook_url": MAIN}, headers=admin)

    posted: list[str] = []

    async def fake_post(webhook_url, event, fields, url=None):
        posted.append(webhook_url)
        return None

    monkeypatch.setattr(discord, "post", fake_post)

    assert client.post("/api/admin/discord-config/test", headers=admin).json()["ok"] is True
    assert posted == [MAIN]


def test_a_test_with_nothing_configured_reports_why(client, admin, settings) -> None:
    result = client.post("/api/admin/discord-config/test", headers=admin).json()

    assert result["ok"] is False
    assert "No webhook configured" in result["detail"]


@pytest.mark.parametrize(
    "value, expected",
    [
        ("", "-"),
        ("   ", "-"),
        ("x" * 1024, "x" * 1024),
        ("x" * 1025, "x" * 1023 + "\N{HORIZONTAL ELLIPSIS}"),
    ],
)
def test_field_values_stay_within_what_discord_accepts(value: str, expected: str) -> None:
    """An empty or over-long field makes Discord reject the whole embed."""
    assert discord._field_value(value) == expected


import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from nacl.signing import SigningKey
from sqlmodel import Session

from app import discord, mail
from app.db import engine
from app.main import app
from app.models import Invite, InviteStatus
from app.rctf_client import TeamIdentity, get_rctf_client
from app.routers.invites import accept
from tests.fake_rctf import FakeRctfClient

ADMIN = {"Authorization": "Bearer admin-token"}
PLAYER = {"Authorization": "Bearer player-token"}


@pytest.fixture()
def fake_client() -> FakeRctfClient:
    fake = FakeRctfClient()
    fake.identities["admin-token"] = TeamIdentity("t1", "admin", is_admin=True)
    fake.identities["player-token"] = TeamIdentity("t2", "n1ght0wl", is_admin=False)
    app.dependency_overrides[get_rctf_client] = lambda: fake
    yield fake
    app.dependency_overrides.clear()


@pytest.fixture()
def client(fake_client: FakeRctfClient) -> TestClient:
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def sent(monkeypatch) -> list[tuple[str, str, str]]:
    """Every email that would have gone out, as (team_name, email, token)."""
    emails: list[tuple[str, str, str]] = []
    monkeypatch.setattr(mail, "send_invite", lambda *args: emails.append(args))
    return emails


@pytest.fixture()
def posted(monkeypatch) -> list[tuple]:
    """Every invite that would have been posted to Discord."""
    messages: list[tuple] = []

    async def fake_post(*args) -> None:
        messages.append(args)

    monkeypatch.setattr(discord, "post_invite", fake_post)
    return messages


@pytest.fixture()
def signing_key(monkeypatch) -> SigningKey:
    from app.config import settings

    key = SigningKey.generate()
    monkeypatch.setattr(settings, "discord_public_key", key.verify_key.encode().hex())
    return key


def create_root(
    client: TestClient, team_name: str = "root", email: str = "root@example.com"
) -> dict:
    resp = client.post(
        "/api/admin/invites",
        json={"team_name": team_name, "email": email},
        headers=ADMIN,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def submit(
    client: TestClient,
    token: str,
    team_name: str = "guest",
    email: str = "guest@example.com",
):
    return client.post(
        "/api/invites", json={"token": token, "team_name": team_name, "email": email}
    )


def pending_id(client: TestClient, team_name: str = "guest") -> int:
    rows = client.get("/api/admin/invites", headers=ADMIN).json()
    return next(r["id"] for r in rows if r["team_name"] == team_name)


def interaction(key: SigningKey, payload: dict) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(payload).encode()
    timestamp = str(int(time.time()))
    signature = key.sign(timestamp.encode() + body).signature.hex()
    return body, {
        "X-Signature-Ed25519": signature,
        "X-Signature-Timestamp": timestamp,
        "Content-Type": "application/json",
    }


def click(client: TestClient, key: SigningKey, custom_id: str, user: str = "orga"):
    body, headers = interaction(
        key,
        {
            "type": 3,
            "data": {"custom_id": custom_id},
            "member": {"user": {"username": user}},
            "message": {"content": "**root** invites **guest** (guest@example.com)"},
        },
    )
    return client.post("/api/discord/interactions", content=body, headers=headers)


def test_root_team_is_accepted_and_emailed(client: TestClient, sent) -> None:
    root = create_root(client)

    assert root["status"] == "accepted"
    assert root["token"]
    assert root["invited_by_id"] is None
    assert root["invited_by"] == "LakeOrga"
    assert root["decided_by"] == "admin"
    assert sent == [("root", "root@example.com", root["token"])]


def test_only_admins_manage_invites(client: TestClient, sent) -> None:
    body = {"team_name": "root", "email": "root@example.com"}
    assert (
        client.post("/api/admin/invites", json=body, headers=PLAYER).status_code == 403
    )
    assert client.get("/api/admin/invites", headers=PLAYER).status_code == 403
    assert client.get("/api/admin/invites").status_code == 401


def test_whoami_names_the_inviter(client: TestClient, sent) -> None:
    root = create_root(client)

    resp = client.get("/api/invites/whoami", params={"token": root["token"]})
    assert resp.status_code == 200
    assert resp.json() == {"team_name": "root"}
    assert (
        client.get("/api/invites/whoami", params={"token": "nope"}).status_code == 404
    )


def test_submit_stores_a_pending_invite_and_posts_it(
    client: TestClient, sent, posted
) -> None:
    root = create_root(client)

    assert submit(client, root["token"]).status_code == 204

    rows = client.get("/api/admin/invites", headers=ADMIN).json()
    guest = next(r for r in rows if r["team_name"] == "guest")
    assert guest["status"] == "pending"
    assert guest["token"] is None
    assert guest["invited_by_id"] == root["id"]
    assert guest["invited_by"] == "root"
    assert posted == [(guest["id"], "root", "guest", "guest@example.com")]
    assert len(sent) == 1


def test_pending_rejected_and_revoked_tokens_are_refused(
    client: TestClient, sent, posted
) -> None:
    root = create_root(client)
    submit(client, root["token"], team_name="pending")
    submit(client, root["token"], team_name="rejected")
    rejected_id = pending_id(client, "rejected")
    client.post(f"/api/admin/invites/{rejected_id}/reject", headers=ADMIN)
    client.post(f"/api/admin/invites/{root['id']}/revoke", headers=ADMIN)

    # Only an accepted row ever gets a token; plant some anyway, so it is the
    # status check that refuses them rather than the missing token.
    with Session(engine) as session:
        for invite_id, token in (
            (pending_id(client, "pending"), "pending-tok"),
            (rejected_id, "rejected-tok"),
        ):
            invite = session.get(Invite, invite_id)
            invite.token = token
            session.add(invite)
        session.commit()

    for token in (root["token"], "pending-tok", "rejected-tok", "", "garbage"):
        assert (
            client.get("/api/invites/whoami", params={"token": token}).status_code
            == 404
        )
        assert submit(client, token).status_code == 403


def test_revoke_only_affects_that_team(client: TestClient, sent, posted) -> None:
    root = create_root(client)
    submit(client, root["token"])
    guest = client.post(
        f"/api/admin/invites/{pending_id(client)}/accept", headers=ADMIN
    ).json()

    client.post(f"/api/admin/invites/{root['id']}/revoke", headers=ADMIN)

    assert submit(client, root["token"], team_name="other").status_code == 403
    assert submit(client, guest["token"], team_name="other").status_code == 204


def test_admin_accept_emails_a_token_once(client: TestClient, sent, posted) -> None:
    root = create_root(client)
    submit(client, root["token"])
    invite_id = pending_id(client)

    first = client.post(f"/api/admin/invites/{invite_id}/accept", headers=ADMIN)
    assert first.status_code == 200
    assert first.json()["status"] == "accepted"
    assert first.json()["invited_by"] == "root"
    token = first.json()["token"]

    second = client.post(f"/api/admin/invites/{invite_id}/accept", headers=ADMIN)
    assert second.status_code == 409
    assert (
        client.post(f"/api/admin/invites/{invite_id}/reject", headers=ADMIN).status_code
        == 409
    )

    with Session(engine) as session:
        assert session.get(Invite, invite_id).token == token
    assert sent == [
        ("root", "root@example.com", root["token"]),
        ("guest", "guest@example.com", token),
    ]


def test_a_second_accept_changes_nothing(client: TestClient, sent, posted) -> None:
    root = create_root(client)
    submit(client, root["token"])
    invite_id = pending_id(client)

    with Session(engine) as session:
        first = accept(session, invite_id, "alice")
        token = first.token
    with Session(engine) as session:
        assert accept(session, invite_id, "bob") is None
        invite = session.get(Invite, invite_id)
        assert invite.token == token
        assert invite.decided_by == "alice"


def test_admin_reject(client: TestClient, sent, posted) -> None:
    root = create_root(client)
    submit(client, root["token"])
    invite_id = pending_id(client)

    resp = client.post(f"/api/admin/invites/{invite_id}/reject", headers=ADMIN)
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"
    assert resp.json()["token"] is None
    assert len(sent) == 1


def test_unknown_invite_is_404(client: TestClient) -> None:
    for action in ("accept", "reject", "revoke"):
        assert (
            client.post(f"/api/admin/invites/999/{action}", headers=ADMIN).status_code
            == 404
        )


@pytest.mark.parametrize(
    "team_name, email",
    [
        ("", "guest@example.com"),
        ("   ", "guest@example.com"),
        ("x" * 101, "guest@example.com"),
        ("guest\n✅ accepted by admin", "guest@example.com"),
        ("guest", "not-an-email"),
        ("guest", "a@b@c"),
        ("guest", "a@b.com, c@d.com"),
        ("guest", "a,b@c.com"),
        ("guest", "a@b.com\nBcc: c@d.com"),
        ("guest", "a@" + "b" * 260 + ".com"),
    ],
)
def test_bad_input_is_422(
    client: TestClient, sent, posted, team_name: str, email: str
) -> None:
    root = create_root(client)
    assert (
        submit(client, root["token"], team_name=team_name, email=email).status_code
        == 422
    )
    resp = client.post(
        "/api/admin/invites",
        json={"team_name": team_name, "email": email},
        headers=ADMIN,
    )
    assert resp.status_code == 422


def test_input_is_stripped(client: TestClient, sent, posted) -> None:
    root = create_root(client)
    submit(client, root["token"], team_name="  guest  ", email=" guest@example.com ")
    assert posted[0][2:] == ("guest", "guest@example.com")


def test_unsigned_interaction_is_401(client: TestClient, signing_key) -> None:
    body = json.dumps({"type": 1}).encode()
    assert client.post("/api/discord/interactions", content=body).status_code == 401

    _, headers = interaction(signing_key, {"type": 1})
    tampered = json.dumps({"type": 1, "x": 1}).encode()
    resp = client.post("/api/discord/interactions", content=tampered, headers=headers)
    assert resp.status_code == 401

    other_key = SigningKey.generate()
    body, headers = interaction(other_key, {"type": 1})
    assert (
        client.post(
            "/api/discord/interactions", content=body, headers=headers
        ).status_code
        == 401
    )


def test_interactions_refused_without_a_public_key(
    client: TestClient, monkeypatch
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "discord_public_key", "")
    body, headers = interaction(SigningKey.generate(), {"type": 1})
    assert (
        client.post(
            "/api/discord/interactions", content=body, headers=headers
        ).status_code
        == 401
    )


def test_ping_is_answered(client: TestClient, signing_key) -> None:
    body, headers = interaction(signing_key, {"type": 1})
    resp = client.post("/api/discord/interactions", content=body, headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"type": 1}


def test_accept_button(client: TestClient, sent, posted, signing_key) -> None:
    root = create_root(client)
    submit(client, root["token"])
    invite_id = pending_id(client)

    resp = click(client, signing_key, f"invite:accept:{invite_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["type"] == 7
    assert data["data"]["components"] == []
    assert data["data"]["allowed_mentions"] == {"parse": []}
    assert data["data"]["content"].endswith("accepted by orga")

    with Session(engine) as session:
        invite = session.get(Invite, invite_id)
        assert invite.status == InviteStatus.accepted
        assert invite.decided_by == "orga"
    assert sent[-1] == ("guest", "guest@example.com", invite.token)


def test_a_late_click_only_reports_the_decision(
    client: TestClient, sent, posted, signing_key
) -> None:
    root = create_root(client)
    submit(client, root["token"])
    invite_id = pending_id(client)
    client.post(f"/api/admin/invites/{invite_id}/reject", headers=ADMIN)

    resp = click(client, signing_key, f"invite:accept:{invite_id}")
    assert resp.json()["data"]["content"].endswith("already rejected by admin")
    assert resp.json()["data"]["components"] == []
    with Session(engine) as session:
        assert session.get(Invite, invite_id).status == InviteStatus.rejected
    assert len(sent) == 1


def test_reject_button(client: TestClient, sent, posted, signing_key) -> None:
    root = create_root(client)
    submit(client, root["token"])
    invite_id = pending_id(client)

    resp = click(client, signing_key, f"invite:reject:{invite_id}")
    assert resp.json()["data"]["content"].endswith("rejected by orga")
    with Session(engine) as session:
        assert session.get(Invite, invite_id).status == InviteStatus.rejected


@pytest.mark.parametrize(
    "custom_id",
    ["", "invite:accept", "invite:ban:1", "other:accept:1", "invite:accept:x"],
)
def test_unknown_button_is_400(client: TestClient, signing_key, custom_id: str) -> None:
    assert click(client, signing_key, custom_id).status_code == 400


class FakeSmtp:
    instances: list["FakeSmtp"] = []

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.address = (host, port)
        self.calls: list[str] = []
        self.messages: list = []
        FakeSmtp.instances.append(self)

    def __enter__(self) -> "FakeSmtp":
        return self

    def __exit__(self, *exc) -> None:
        pass

    def starttls(self) -> None:
        self.calls.append("starttls")

    def login(self, user: str, password: str) -> None:
        self.calls.append(f"login {user}")

    def send_message(self, msg) -> None:
        self.messages.append(msg)


@pytest.fixture()
def smtp(monkeypatch):
    from app.config import settings

    FakeSmtp.instances = []
    monkeypatch.setattr(mail.smtplib, "SMTP", FakeSmtp)
    monkeypatch.setattr(settings, "smtp_host", "ssl0.ovh.net")
    monkeypatch.setattr(settings, "smtp_user", "no-reply@polygl0ts.ch")
    monkeypatch.setattr(settings, "web_origin", "https://friday.polygl0ts.ch")
    return FakeSmtp.instances


def test_mail_uses_starttls_and_carries_the_link(smtp) -> None:
    mail.send_invite("guest", "guest@example.com", "tok123")

    [conn] = smtp
    assert conn.address == ("ssl0.ovh.net", 587)
    assert conn.calls == ["starttls", "login no-reply@polygl0ts.ch"]
    [msg] = conn.messages
    assert msg["To"] == "guest@example.com"
    assert msg["From"] == "no-reply@polygl0ts.ch"
    assert "https://friday.polygl0ts.ch/lake-quals?token=tok123" in msg.get_content()


def test_mail_failure_never_raises(smtp, monkeypatch) -> None:
    def refuse(self) -> None:
        raise OSError("connection refused")

    monkeypatch.setattr(FakeSmtp, "starttls", refuse)
    mail.send_invite("guest", "guest@example.com", "tok123")


def test_mail_skipped_without_smtp(smtp, monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "smtp_host", "")
    mail.send_invite("guest", "guest@example.com", "tok123")
    assert smtp == []


def test_invite_post_pings_nobody(monkeypatch) -> None:
    from app.config import settings

    requests: list = []

    async def fake_post(self, url, *, json, headers):
        requests.append((url, json, headers))
        return httpx.Response(200)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr(settings, "discord_bot_token", "bot-tok")
    monkeypatch.setattr(settings, "discord_invite_channel_id", "42")

    asyncio.run(discord.post_invite(7, "root", "@everyone", "guest@example.com"))

    [(url, payload, headers)] = requests
    assert url == "https://discord.com/api/v10/channels/42/messages"
    assert headers == {"Authorization": "Bot bot-tok"}
    assert payload["allowed_mentions"] == {"parse": []}
    ids = [b["custom_id"] for b in payload["components"][0]["components"]]
    assert ids == ["invite:accept:7", "invite:reject:7"]

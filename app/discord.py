import logging

import httpx
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey
from sqlmodel import Session

from app.models import DiscordConfig

logger = logging.getLogger(__name__)

_COLOR = 0xFF2B3E  # matches the Polygl0ts CTF brand red used in the design

# Every event this service posts is part of the writeup lifecycle. First
# bloods are deliberately absent: rCTF's blood bot announces those, and adding
# them back here would post each one twice.
_EVENT_TITLES = {
    "submitted": "\N{MEMO} New writeup pending",
    "approved": "\N{WHITE HEAVY CHECK MARK} Writeup published",
    "rejected": "\N{CROSS MARK} Writeup rejected",
    "unpublished": "\N{LEFTWARDS ARROW WITH HOOK} Writeup unpublished",
    "test": "\N{SATELLITE ANTENNA} Webhook test",
}

# Discord rejects an embed field whose value is empty or over 1024 characters.
_MAX_FIELD_VALUE = 1024


def _field_value(value: str) -> str:
    value = value.strip() if value else ""
    if not value:
        return "-"
    if len(value) > _MAX_FIELD_VALUE:
        return value[: _MAX_FIELD_VALUE - 1] + "\N{HORIZONTAL ELLIPSIS}"
    return value


def effective_webhook_url(config: DiscordConfig) -> str:
    """The URL a notification actually goes to.

    A value set through the admin API wins; otherwise the vault-injected
    settings value is used
    """
    from app.config import settings

    return config.webhook_url or settings.discord_webhook_url


def get_config(session: Session) -> DiscordConfig:
    config = session.get(DiscordConfig, 1)
    if config is None:
        config = DiscordConfig(id=1)
        session.add(config)
        session.commit()
        session.refresh(config)
    return config


async def post(
    webhook_url: str,
    event: str,
    fields: dict[str, str],
    url: str | None = None,
) -> str | None:
    """Post one embed. Returns None on success, or why it failed.

    Callers in the request path throw the reason away (a Discord outage must
    never break a writeup submission), but the admin's "send test" surfaces
    it - otherwise a mistyped webhook is indistinguishable from a working one,
    which is exactly how you end up believing notifications are configured
    when they are silently going nowhere.
    """
    embed: dict = {
        "title": _EVENT_TITLES.get(event, event),
        "color": _COLOR,
        "fields": [{"name": k, "value": _field_value(v)} for k, v in fields.items()],
    }
    if url:
        embed["url"] = url

    payload = {"username": "Polygl0ts", "embeds": [embed]}

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(webhook_url, json=payload)
    except httpx.HTTPError as exc:
        return f"Could not reach Discord: {exc.__class__.__name__}"

    if resp.status_code >= 400:
        return f"Discord rejected it ({resp.status_code}) - is the webhook still valid?"
    return None


async def notify(
    session: Session,
    event: str,
    fields: dict[str, str],
    url: str | None = None,
) -> None:
    """Fire-and-forget notification for the configured webhook.

    `url` makes the embed title a hyperlink - used to point reviewers straight
    at the admin queue.
    """
    webhook_url = effective_webhook_url(get_config(session))
    if not webhook_url:
        return
    await post(webhook_url, event, fields, url)


# Team names come from a public form, so nothing in an invite message may ping.
NO_MENTIONS: dict = {"parse": []}


async def post_invite(invite_id: int, inviter: str, team_name: str, email: str) -> None:
    """Post a pending invite with Accept / Reject buttons, via the Lake Quals bot."""
    from app.config import settings

    if not (settings.discord_bot_token and settings.discord_invite_channel_id):
        return

    payload = {
        "content": f"**{inviter}** invites **{team_name}** ({email})",
        "allowed_mentions": NO_MENTIONS,
        "components": [
            {
                "type": 1,
                "components": [
                    {
                        "type": 2,
                        "style": 3,
                        "label": "Accept",
                        "custom_id": f"invite:accept:{invite_id}",
                    },
                    {
                        "type": 2,
                        "style": 4,
                        "label": "Reject",
                        "custom_id": f"invite:reject:{invite_id}",
                    },
                ],
            }
        ],
    }
    url = f"https://discord.com/api/v10/channels/{settings.discord_invite_channel_id}/messages"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={"Authorization": f"Bot {settings.discord_bot_token}"},
            )
    except httpx.HTTPError as exc:
        logger.warning(
            "Could not post invite %s: %s", invite_id, exc.__class__.__name__
        )
        return
    if resp.status_code >= 400:
        logger.warning(
            "Discord refused invite %s (%s): %s", invite_id, resp.status_code, resp.text
        )


def signature_valid(
    public_key: str, signature: str, timestamp: str, body: bytes
) -> bool:
    """Discord's Ed25519 check on an interaction request."""
    try:
        VerifyKey(bytes.fromhex(public_key)).verify(
            timestamp.encode() + body, bytes.fromhex(signature)
        )
    except (BadSignatureError, ValueError):
        return False
    return True

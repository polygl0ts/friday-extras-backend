from fastapi import APIRouter, Depends
from sqlmodel import Session, func, select

from app import discord
from app.auth import require_admin
from app.db import get_session
from app.discord import effective_webhook_url, get_config
from app.models import DiscordConfig, Writeup, WriteupStatus
from app.rctf_client import TeamIdentity
from app.schemas import (
    AdminStatsOut,
    DiscordConfigIn,
    DiscordConfigOut,
    DiscordTestResult,
)

router = APIRouter(prefix="/admin", tags=["admin"])


def _config_out(config: DiscordConfig) -> DiscordConfigOut:
    return DiscordConfigOut(webhook_configured=bool(effective_webhook_url(config)))


@router.get("/discord-config", response_model=DiscordConfigOut)
def read_discord_config(
    _: TeamIdentity = Depends(require_admin),
    session: Session = Depends(get_session),
) -> DiscordConfigOut:
    return _config_out(get_config(session))


@router.put("/discord-config", response_model=DiscordConfigOut)
def write_discord_config(
    body: DiscordConfigIn,
    _: TeamIdentity = Depends(require_admin),
    session: Session = Depends(get_session),
) -> DiscordConfigOut:
    config = get_config(session)
    # Write-only, and only when actually supplied: an omitted or blank field
    # means "keep the current URL"
    if body.webhook_url:
        config.webhook_url = body.webhook_url.strip()
    session.add(config)
    session.commit()
    session.refresh(config)
    return _config_out(config)


@router.delete("/discord-config/webhook", response_model=DiscordConfigOut)
def clear_webhook(
    _: TeamIdentity = Depends(require_admin),
    session: Session = Depends(get_session),
) -> DiscordConfigOut:
    """Forget the stored webhook.

    Falls back to the vault-injected value if there is one, and otherwise
    silences writeup notifications entirely.
    """
    config = get_config(session)
    config.webhook_url = ""
    session.add(config)
    session.commit()
    session.refresh(config)
    return _config_out(config)


@router.post("/discord-config/test", response_model=DiscordTestResult)
async def test_discord_webhook(
    _: TeamIdentity = Depends(require_admin),
    session: Session = Depends(get_session),
) -> DiscordTestResult:
    """Post a real message to the webhook and report what happened."""
    webhook_url = effective_webhook_url(get_config(session))
    if not webhook_url:
        return DiscordTestResult(ok=False, detail="No webhook configured")

    error = await discord.post(webhook_url, "test", {"Channel": "writeups"})
    if error:
        return DiscordTestResult(ok=False, detail=error)
    return DiscordTestResult(ok=True, detail="Sent - check the channel")


@router.get("/stats", response_model=AdminStatsOut)
def stats(
    _: TeamIdentity = Depends(require_admin),
    session: Session = Depends(get_session),
) -> AdminStatsOut:
    """Writeup counts only, and no rCTF round trip.

    Player and challenge counts came from here until they didn't need to: both
    are rCTF's own answers (`leaderboard/now`'s `total` and the length of the
    challenge list, which this pulled in full just to count), and the frontend
    reads them straight from rCTF. That also keeps those two tiles working
    when this service isn't.
    """
    submissions = session.exec(select(func.count()).select_from(Writeup)).one()
    pending = session.exec(
        select(func.count())
        .select_from(Writeup)
        .where(Writeup.status == WriteupStatus.pending)
    ).one()

    return AdminStatsOut(submissions=submissions, pending_writeups=pending)

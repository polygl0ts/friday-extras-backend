"""The Lake Quals invitation chain.

Anyone holding a valid token can propose another team; an admin accepts or
rejects it, from the Discord buttons or the admin panel, and an accepted team
is emailed a token of its own.
"""

import json
import secrets
from datetime import datetime, timezone

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
    status,
)
from sqlmodel import Session, select, update

from app import discord, mail
from app.auth import require_admin
from app.config import settings
from app.db import get_session
from app.models import Invite, InviteStatus
from app.rctf_client import TeamIdentity
from app.schemas import InviteOut, InviteRequest, InviteSubmitRequest, InviteWhoamiOut

router = APIRouter(prefix="/invites", tags=["invites"])
admin_router = APIRouter(prefix="/admin/invites", tags=["admin"])
discord_router = APIRouter(prefix="/discord", tags=["discord"])

# Shown as the inviter of a root team. In the DB a root team has no inviter.
ROOT_INVITER = "LakeOrga"


def _decide(
    session: Session, invite_id: int, decided_by: str, new_status: InviteStatus
) -> Invite | None:
    """Move a pending invite to `new_status`, or return None if it isn't pending.

    One conditional UPDATE rather than read-check-write, so two simultaneous
    clicks (two admins, or Discord and the admin panel) can't both win - which
    would mint two tokens and send two emails.
    """
    values: dict = {
        "status": new_status,
        "decided_by": decided_by,
        "decided_at": datetime.now(timezone.utc),
    }
    if new_status == InviteStatus.accepted:
        values["token"] = secrets.token_urlsafe(24)
    result = session.exec(
        update(Invite)
        .where(Invite.id == invite_id, Invite.status == InviteStatus.pending)
        .values(**values)
    )
    session.commit()
    if result.rowcount != 1:
        return None
    return session.get(Invite, invite_id)


def accept(session: Session, invite_id: int, decided_by: str) -> Invite | None:
    """Accept a pending invite. The caller emails the token on success."""
    return _decide(session, invite_id, decided_by, InviteStatus.accepted)


def reject(session: Session, invite_id: int, decided_by: str) -> Invite | None:
    return _decide(session, invite_id, decided_by, InviteStatus.rejected)


def _email_token(background: BackgroundTasks, invite: Invite) -> None:
    background.add_task(mail.send_invite, invite.team_name, invite.email, invite.token)


def _inviter(session: Session, token: str) -> Invite | None:
    return session.exec(
        select(Invite).where(
            Invite.token == token,
            Invite.status == InviteStatus.accepted,
            Invite.revoked == False,  # noqa: E712
        )
    ).first()


def _out(invite: Invite, names: dict[int, str]) -> InviteOut:
    return InviteOut(
        id=invite.id,
        team_name=invite.team_name,
        email=invite.email,
        status=invite.status,
        token=invite.token,
        revoked=invite.revoked,
        invited_by_id=invite.invited_by_id,
        invited_by=names.get(invite.invited_by_id, "")
        if invite.invited_by_id
        else ROOT_INVITER,
        created_at=invite.created_at,
        decided_by=invite.decided_by,
        decided_at=invite.decided_at,
    )


def _one_out(session: Session, invite: Invite) -> InviteOut:
    inviter = (
        session.get(Invite, invite.invited_by_id) if invite.invited_by_id else None
    )
    return _out(invite, {inviter.id: inviter.team_name} if inviter else {})


def _get_or_404(session: Session, invite_id: int) -> Invite:
    invite = session.get(Invite, invite_id)
    if invite is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invite not found")
    return invite


# ---------- public: the token is the auth ----------


@router.get("/whoami", response_model=InviteWhoamiOut)
def whoami(
    token: str = Query(max_length=100),
    session: Session = Depends(get_session),
) -> InviteWhoamiOut:
    inviter = _inviter(session, token)
    if inviter is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invalid or revoked token")
    return InviteWhoamiOut(team_name=inviter.team_name)


@router.post("", status_code=status.HTTP_204_NO_CONTENT)
async def submit_invite(
    body: InviteSubmitRequest,
    session: Session = Depends(get_session),
) -> None:
    inviter = _inviter(session, body.token)
    if inviter is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid or revoked token")

    invite = Invite(
        team_name=body.team_name, email=body.email, invited_by_id=inviter.id
    )
    session.add(invite)
    session.commit()
    session.refresh(invite)

    await discord.post_invite(
        invite.id, inviter.team_name, invite.team_name, invite.email
    )


# ---------- admin ----------


@admin_router.get("", response_model=list[InviteOut])
def list_invites(
    _: TeamIdentity = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[InviteOut]:
    invites = list(
        session.exec(
            select(Invite).order_by(Invite.created_at.desc(), Invite.id.desc())
        )
    )
    names = {i.id: i.team_name for i in invites}
    return [_out(i, names) for i in invites]


@admin_router.post("", response_model=InviteOut, status_code=status.HTTP_201_CREATED)
def create_root_invite(
    body: InviteRequest,
    background: BackgroundTasks,
    identity: TeamIdentity = Depends(require_admin),
    session: Session = Depends(get_session),
) -> InviteOut:
    """Seed a team at the top of the chain: no inviter, accepted at once."""
    invite = Invite(team_name=body.team_name, email=body.email)
    session.add(invite)
    session.commit()
    session.refresh(invite)

    invite = accept(session, invite.id, identity.team_name)
    _email_token(background, invite)
    return _one_out(session, invite)


@admin_router.post("/{invite_id}/accept", response_model=InviteOut)
def accept_invite(
    invite_id: int,
    background: BackgroundTasks,
    identity: TeamIdentity = Depends(require_admin),
    session: Session = Depends(get_session),
) -> InviteOut:
    _get_or_404(session, invite_id)
    invite = accept(session, invite_id, identity.team_name)
    if invite is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Invite was already decided")
    _email_token(background, invite)
    return _one_out(session, invite)


@admin_router.post("/{invite_id}/reject", response_model=InviteOut)
def reject_invite(
    invite_id: int,
    identity: TeamIdentity = Depends(require_admin),
    session: Session = Depends(get_session),
) -> InviteOut:
    _get_or_404(session, invite_id)
    invite = reject(session, invite_id, identity.team_name)
    if invite is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Invite was already decided")
    return _one_out(session, invite)


@admin_router.post("/{invite_id}/revoke", response_model=InviteOut)
def revoke_invite(
    invite_id: int,
    _: TeamIdentity = Depends(require_admin),
    session: Session = Depends(get_session),
) -> InviteOut:
    """Stop this one team from inviting. Teams it already invited keep their
    own tokens."""
    invite = _get_or_404(session, invite_id)
    invite.revoked = True
    session.add(invite)
    session.commit()
    session.refresh(invite)
    return _one_out(session, invite)


# ---------- Discord button clicks ----------

_INTERACTION_PING = 1
_INTERACTION_COMPONENT = 3
_RESPONSE_PONG = 1
_RESPONSE_UPDATE_MESSAGE = 7


@discord_router.post("/interactions")
async def discord_interaction(
    request: Request,
    background: BackgroundTasks,
    session: Session = Depends(get_session),
) -> dict:
    # Verified against the raw bytes: re-serialized JSON wouldn't match.
    body = await request.body()
    if not settings.discord_public_key or not discord.signature_valid(
        settings.discord_public_key,
        request.headers.get("X-Signature-Ed25519", ""),
        request.headers.get("X-Signature-Timestamp", ""),
        body,
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid request signature")

    payload = json.loads(body)
    if payload.get("type") == _INTERACTION_PING:
        return {"type": _RESPONSE_PONG}
    if payload.get("type") != _INTERACTION_COMPONENT:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unsupported interaction")

    parts = payload.get("data", {}).get("custom_id", "").split(":")
    if (
        len(parts) != 3
        or parts[0] != "invite"
        or parts[1] not in ("accept", "reject")
        or not parts[2].isdigit()
    ):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown button")
    action, invite_id = parts[1], int(parts[2])

    # In a server channel the clicker is nested under `member`; `user` alone is
    # only set in DMs.
    user = (payload.get("member") or payload).get("user", {}).get("username", "unknown")

    if action == "accept":
        invite = accept(session, invite_id, user)
        if invite:
            _email_token(background, invite)
            note = f"\N{WHITE HEAVY CHECK MARK} accepted by {user}"
    else:
        invite = reject(session, invite_id, user)
        if invite:
            note = f"\N{CROSS MARK} rejected by {user}"

    if invite is None:
        current = session.get(Invite, invite_id)
        note = (
            f"already {current.status.value} by {current.decided_by}"
            if current
            else "this invite no longer exists"
        )

    original = payload.get("message", {}).get("content", "")
    return {
        "type": _RESPONSE_UPDATE_MESSAGE,
        "data": {
            "content": f"{original} \N{EM DASH} {note}",
            "components": [],
            "allowed_mentions": discord.NO_MENTIONS,
        },
    }

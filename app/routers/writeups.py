from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.engine import ScalarResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError
from sqlmodel import Session, func, select
from sqlmodel.sql.expression import SelectOfScalar

from app import discord
from app.auth import get_current_identity, get_fresh_identity, require_admin
from app.config import settings
from app.db import get_session
from app.models import Writeup, WriteupStatus, WriteupVote
from app.rctf_client import RctfClient, TeamIdentity, get_rctf_client
from app.schemas import (
    WriteupCardOut,
    WriteupOut,
    WriteupRejectRequest,
    WriteupSubmitRequest,
)
from app.writeup_md import WriteupFormatError, split_writeup

router: APIRouter = APIRouter(prefix="/writeups", tags=["writeups"])


def _review_url() -> str | None:
    """Deep link to the admin review queue, or None if no frontend origin is
    configured (in which case notifications just carry no link)."""
    origin: str = settings.web_origin.rstrip("/")
    return f"{origin}/admin" if origin else None


def _writeup_url(writeup_id: int) -> str | None:
    origin: str = settings.web_origin.rstrip("/")
    return f"{origin}/writeups?w={writeup_id}" if origin else None


async def _challenge_name(client: RctfClient, challenge_id: str) -> str:
    chall: dict
    for chall in await client.list_challenges():
        if str(chall.get("id")) == challenge_id:
            return chall.get("name", challenge_id)
    return challenge_id


def _split_or_422(body_md: str) -> tuple[str, str]:
    try:
        return split_writeup(body_md)
    except WriteupFormatError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


def _vote_state(
    session: Session, team_id: str
) -> tuple[dict[int, int], set[int]]:
    """(votes per writeup, the ids this team voted for).

    Two aggregate queries for the whole page rather than a count per writeup.
    The tally is always derived from the vote rows - there is no cached score
    column that could drift away from them.
    """
    counts: dict[int, int] = dict(
        session.exec(
            select(WriteupVote.writeup_id, func.count()).group_by(WriteupVote.writeup_id)
        ).all()
    )
    mine: set[int] = set(
        session.exec(
            select(WriteupVote.writeup_id).where(WriteupVote.team_id == team_id)
        ).all()
    )
    return counts, mine


def _viewer_flags(writeup: Writeup, identity: TeamIdentity) -> tuple[bool, bool]:
    """(unlocked, owner_view) for this viewer.

    Reading the full solution takes solving the challenge - or being its
    author (you wrote it) or an admin (you have to read it to review it).

    No rCTF call: the solve set arrives on the identity. It may be up to
    `identity_cache_seconds` old, which is acceptable here - the worst case is
    that a solution stays redacted a few seconds longer than necessary, and a
    refresh fixes it. Submission, where staleness would wrongly *reject*, uses
    `get_fresh_identity` instead.
    """
    is_author: bool = writeup.team_id == identity.team_id
    owner_view: bool = is_author or identity.is_admin
    if owner_view:
        return True, True
    return writeup.challenge_id in identity.solved_challenge_ids, False


@router.post("/{challenge_id}/submit", response_model=WriteupOut)
async def submit_writeup(
    challenge_id: str,
    body: WriteupSubmitRequest,
    identity: TeamIdentity = Depends(get_fresh_identity),
    client: RctfClient = Depends(get_rctf_client),
    session: Session = Depends(get_session),
) -> WriteupOut:
    # Fresh identity, not the cached one: a team that solved this seconds ago
    # must not be told to go and solve it.
    if challenge_id not in identity.solved_challenge_ids:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Solve this challenge before posting a writeup"
        )

    intro_md, solution_md = _split_or_422(body.body_md)

    writeup: Writeup = Writeup(
        challenge_id=challenge_id,
        challenge_name=await _challenge_name(client, challenge_id),
        team_id=identity.team_id,
        team_name=identity.team_name,
        summary=body.summary,
        intro_md=intro_md,
        solution_md=solution_md,
        status=WriteupStatus.pending,
    )
    session.add(writeup)
    session.commit()
    session.refresh(writeup)

    await discord.notify(
        session,
        "submitted",
        {
            "Challenge": writeup.challenge_name,
            "Author": writeup.team_name,
            "Status": "awaiting review",
        },
        # The only actionable notification of the three - link the title
        # straight at the review queue so an admin is one tap from approving.
        url=_review_url(),
    )
    return WriteupOut.for_viewer(writeup, unlocked=True, owner_view=True)


@router.get("", response_model=list[WriteupCardOut])
def list_writeups(
    challenge_id: str | None = None,
    sort: str = Query(default="new", pattern="^(new|top)$"),
    identity: TeamIdentity = Depends(get_current_identity),
    session: Session = Depends(get_session),
) -> list[WriteupCardOut]:
    """Every published writeup, for the grid.

    Deliberately unfiltered by solve state: a writeup's existence, challenge,
    author and summary are public to anyone logged in - only its gated half
    isn't. Cards carry no body, so this needs no rCTF round trip at all.

    `sort=new` (the default) is newest-first; `sort=top` ranks by upvotes and
    breaks ties by recency. Ordering happens in Python rather than as an outer
    join + group by: this table holds tens of rows, not millions, and the
    query stays legible.
    """
    query: SelectOfScalar[Writeup] = select(Writeup).where(
        Writeup.status == WriteupStatus.published
    )
    if challenge_id is not None:
        query = query.where(Writeup.challenge_id == challenge_id)

    counts, mine = _vote_state(session, identity.team_id)
    writeups: list[Writeup] = list(session.exec(query))
    writeups.sort(
        key=lambda w: (counts.get(w.id, 0), w.created_at) if sort == "top" else (w.created_at,),
        reverse=True,
    )
    return [
        WriteupCardOut.for_viewer(w, votes=counts.get(w.id, 0), voted=w.id in mine)
        for w in writeups
    ]


@router.get("/mine", response_model=list[WriteupOut])
def my_writeups(
    identity: TeamIdentity = Depends(get_current_identity),
    session: Session = Depends(get_session),
) -> list[WriteupOut]:
    writeups: list[Writeup] = list(
        session.exec(
            select(Writeup)
            .where(Writeup.team_id == identity.team_id)
            .order_by(Writeup.created_at.desc())
        )
    )
    counts, _mine = _vote_state(session, identity.team_id)
    # Your own writeups, including why any of them were turned down, and how
    # they landed with everyone else. `voted` stays false - you can't vote for
    # your own, so there is nothing to reflect back.
    return [
        WriteupOut.for_viewer(w, unlocked=True, owner_view=True, votes=counts.get(w.id, 0))
        for w in writeups
    ]


@router.get("/queue", response_model=list[WriteupOut])
def writeup_queue(
    _: TeamIdentity = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[WriteupOut]:
    pending: ScalarResult[Writeup] = session.exec(
        select(Writeup).where(Writeup.status == WriteupStatus.pending)
    )
    # Admins review the whole document, both halves.
    return [WriteupOut.for_viewer(w, unlocked=True, owner_view=True) for w in pending]


# `/item/{id}` rather than `/{id}`: writeup ids and challenge ids are both
# path-shaped, and a bare `/writeups/{x}` GET would be ambiguous between them.
@router.get("/item/{writeup_id}", response_model=WriteupOut)
async def read_writeup(
    writeup_id: int,
    identity: TeamIdentity = Depends(get_current_identity),
    session: Session = Depends(get_session),
) -> WriteupOut:
    writeup: Writeup | None = session.get(Writeup, writeup_id)
    if writeup is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Writeup not found")

    unlocked, owner_view = _viewer_flags(writeup, identity)
    # An unpublished writeup is visible to its author and to admins only -
    # both of which `owner_view` already means.
    if writeup.status != WriteupStatus.published and not owner_view:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Writeup not found")

    counts, mine = _vote_state(session, identity.team_id)
    return WriteupOut.for_viewer(
        writeup,
        unlocked=unlocked,
        owner_view=owner_view,
        votes=counts.get(writeup.id, 0),
        voted=writeup.id in mine,
    )


def _apply_vote(
    writeup_id: int,
    add: bool,
    identity: TeamIdentity,
    session: Session,
) -> WriteupOut:
    """Shared body of the two vote verbs.

    Voting is open to anyone logged in, whether or not they solved the
    challenge: the intro is readable by everyone, so everyone can judge
    whether a writeup was worth reading.
    """
    writeup: Writeup | None = session.get(Writeup, writeup_id)
    if writeup is None or writeup.status != WriteupStatus.published:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Writeup not found")
    if writeup.team_id == identity.team_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You can't upvote your own writeup")

    # A concurrent request may reach the state first, between the lookup below
    # and the commit. That is the state we wanted, so it is not an error.
    existing: WriteupVote | None = session.get(
        WriteupVote, (writeup_id, identity.team_id)
    )
    if add and existing is None:
        session.add(WriteupVote(writeup_id=writeup_id, team_id=identity.team_id))
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
    elif not add and existing is not None:
        session.delete(existing)
        try:
            session.commit()
        except StaleDataError:
            session.rollback()

    unlocked, owner_view = _viewer_flags(writeup, identity)
    counts, mine = _vote_state(session, identity.team_id)
    return WriteupOut.for_viewer(
        writeup,
        unlocked=unlocked,
        owner_view=owner_view,
        votes=counts.get(writeup_id, 0),
        voted=writeup_id in mine,
    )


# Two verbs rather than one toggle, so both are idempotent: a double-tap or a
# retried request lands on the same state instead of flipping the vote back.
@router.post("/item/{writeup_id}/vote", response_model=WriteupOut)
def upvote_writeup(
    writeup_id: int,
    identity: TeamIdentity = Depends(get_current_identity),
    session: Session = Depends(get_session),
) -> WriteupOut:
    return _apply_vote(writeup_id, True, identity, session)


@router.delete("/item/{writeup_id}/vote", response_model=WriteupOut)
def remove_vote(
    writeup_id: int,
    identity: TeamIdentity = Depends(get_current_identity),
    session: Session = Depends(get_session),
) -> WriteupOut:
    return _apply_vote(writeup_id, False, identity, session)


@router.put("/item/{writeup_id}", response_model=WriteupOut)
async def edit_writeup(
    writeup_id: int,
    body: WriteupSubmitRequest,
    identity: TeamIdentity = Depends(get_current_identity),
    session: Session = Depends(get_session),
) -> WriteupOut:
    """Author edits their own writeup and sends it back for review.

    Published writeups are frozen: editing one in place would put unreviewed
    content behind an already-approved card, which is the whole thing the
    review step exists to prevent. An admin has to send it back to the queue
    first - that is what `delete_writeup` below is for, and what the 409 here
    tells the author to go and ask for.
    """
    writeup: Writeup | None = session.get(Writeup, writeup_id)
    if writeup is None or writeup.team_id != identity.team_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Writeup not found")
    if writeup.status == WriteupStatus.published:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "A published writeup can't be edited - ask an admin to unpublish it first",
        )

    writeup.intro_md, writeup.solution_md = _split_or_422(body.body_md)
    writeup.summary = body.summary
    writeup.status = WriteupStatus.pending
    writeup.reject_reason = None
    writeup.reviewed_by = None
    writeup.reviewed_at = None
    session.add(writeup)
    session.commit()
    session.refresh(writeup)

    await discord.notify(
        session,
        "submitted",
        {
            "Challenge": writeup.challenge_name,
            "Author": writeup.team_name,
            "Status": "resubmitted after edit",
        },
        url=_review_url(),
    )
    return WriteupOut.for_viewer(writeup, unlocked=True, owner_view=True)


@router.post("/{writeup_id}/approve", response_model=WriteupOut)
async def approve_writeup(
    writeup_id: int,
    identity: TeamIdentity = Depends(require_admin),
    session: Session = Depends(get_session),
) -> WriteupOut:
    """
    Approved writeup needs discord notification and state modification.
    """
    writeup: Writeup | None = session.get(Writeup, writeup_id)
    if writeup is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Writeup not found")

    writeup.status = WriteupStatus.published
    writeup.reject_reason = None
    writeup.reviewed_by = identity.team_name
    writeup.reviewed_at = datetime.now(timezone.utc)
    session.add(writeup)
    session.commit()
    session.refresh(writeup)

    await discord.notify(
        session,
        "approved",
        {"Challenge": writeup.challenge_name, "Author": writeup.team_name},
        url=_writeup_url(writeup_id),
    )
    return WriteupOut.for_viewer(writeup, unlocked=True, owner_view=True)


@router.post("/{writeup_id}/reject", response_model=WriteupOut)
async def reject_writeup(
    writeup_id: int,
    body: WriteupRejectRequest,
    identity: TeamIdentity = Depends(require_admin),
    session: Session = Depends(get_session),
) -> WriteupOut:
    """
    Rejecting a writeup needs discord notification and state modification.
    """
    writeup: Writeup | None = session.get(Writeup, writeup_id)
    if writeup is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Writeup not found")

    writeup.status = WriteupStatus.rejected
    writeup.reject_reason = body.reason
    writeup.reviewed_by = identity.team_name
    writeup.reviewed_at = datetime.now(timezone.utc)
    session.add(writeup)
    session.commit()
    session.refresh(writeup)

    await discord.notify(
        session,
        "rejected",
        {
            "Challenge": writeup.challenge_name,
            "Author": writeup.team_name,
            "Reviewer": writeup.reviewed_by or "-",
            "Reason": body.reason,
        },
    )
    return WriteupOut.for_viewer(writeup, unlocked=True, owner_view=True)


@router.post("/{writeup_id}/delete", response_model=WriteupOut)
async def delete_writeup(
    writeup_id: int,
    identity: TeamIdentity = Depends(require_admin),
    session: Session = Depends(get_session)
) -> WriteupOut:
    """
    Sends the writeup back to pending state, sends discord notification along.
    """

    writeup: Writeup | None = session.get(Writeup, writeup_id)
    if writeup is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Writeup not found")

    if writeup.status != WriteupStatus.published:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Only published writeup can be deleted."
        )

    writeup.status = WriteupStatus.pending
    writeup.reject_reason = None
    writeup.reviewed_by = None
    writeup.reviewed_at = None

    session.add(writeup)
    session.commit()
    session.refresh(writeup)

    await discord.notify(
        session,
        "unpublished",
        {
            "Challenge": writeup.challenge_name,
            "Author": writeup.team_name,
            "Status": "Sends back by admin.",
            "Removed by": identity.team_name
        },
        url=_review_url(),
    )
    return WriteupOut.for_viewer(writeup, unlocked=True, owner_view=True)

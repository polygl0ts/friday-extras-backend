from datetime import datetime, timezone
from typing import Annotated, Optional

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from app.models import Writeup, WriteupStatus
from app.writeup_md import scrub_flags


def _stamp_utc(value: datetime) -> datetime:
    """Stored timestamps are always UTC, but SQLite hands them back naive.

    Without an offset on the wire, `new Date(...)` reads the value as local
    time and every timestamp in the UI shifts.
    """
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


UtcDatetime = Annotated[datetime, AfterValidator(_stamp_utc)]


class WriteupSubmitRequest(BaseModel):
    # One markdown document containing a `:::solution` marker; the server
    # splits it.
    body_md: str = Field(max_length=100_000)
    summary: str = Field(max_length=500)


class WriteupRejectRequest(BaseModel):
    # Required: a rejection the author cannot act on is just a deletion.
    reason: str = Field(min_length=1, max_length=2000)


class WriteupCardOut(BaseModel):
    """Grid view. Carries no body at all, so listing every published writeup
    to everyone is safe regardless of who solved what."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    #: No `challenge_name`: the client resolves it from rCTF's challenge list,
    #: which it already holds. The stored copy is a submit-time snapshot (see
    #: `models.Writeup`) and would show the old name after a rename.
    challenge_id: str
    team_name: str
    summary: str
    created_at: UtcDatetime
    votes: int = 0
    #: Whether the requesting team has upvoted this one.
    voted: bool = False

    @classmethod
    def for_viewer(
        cls, writeup: Writeup, *, votes: int, voted: bool
    ) -> "WriteupCardOut":
        return cls(
            id=writeup.id,
            challenge_id=writeup.challenge_id,
            team_name=writeup.team_name,
            summary=writeup.summary,
            created_at=writeup.created_at,
            votes=votes,
            voted=voted,
        )


class WriteupOut(BaseModel):
    id: int
    #: Resolved to a name client-side - see `WriteupCardOut.challenge_id`.
    challenge_id: str
    team_id: str
    team_name: str
    summary: str
    intro_md: str
    #: The gated half. `None` whenever the viewer may not read it - the field
    #: is left unset rather than blanked, so an empty solution and a hidden
    #: one are distinguishable by the client.
    solution_md: Optional[str] = None
    #: Legacy external link, solver-gated like the solution. Empty for every
    #: writeup submitted since bodies became stored markdown.
    url: Optional[str] = None
    redacted: bool
    status: WriteupStatus
    created_at: UtcDatetime
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[UtcDatetime] = None
    #: Only ever populated for the writeup's own author (and admins).
    reject_reason: Optional[str] = None
    votes: int = 0
    voted: bool = False

    @classmethod
    def for_viewer(
        cls,
        writeup: Writeup,
        *,
        unlocked: bool,
        owner_view: bool,
        votes: int = 0,
        voted: bool = False,
    ) -> "WriteupOut":
        """The one place a writeup is turned into a response.

        This function *is* the redaction boundary: `solution_md` is read out
        of the row only when `unlocked`, so gated bytes never reach the
        serializer, let alone the wire. Every route goes through here; nothing
        builds a `WriteupOut` any other way, and a test asserts the gated text
        appears nowhere in a locked response.

        `unlocked` - the viewer solved the challenge, wrote this writeup, or
        is an admin reviewing it. `owner_view` - author or admin, who may see
        why a writeup was rejected.
        """
        return cls(
            id=writeup.id,
            challenge_id=writeup.challenge_id,
            team_id=writeup.team_id,
            team_name=writeup.team_name,
            summary=writeup.summary,
            intro_md=scrub_flags(writeup.intro_md),
            solution_md=writeup.solution_md if unlocked else None,
            url=writeup.url if unlocked else None,
            redacted=not unlocked,
            status=writeup.status,
            created_at=writeup.created_at,
            reviewed_by=writeup.reviewed_by,
            reviewed_at=writeup.reviewed_at,
            reject_reason=writeup.reject_reason if owner_view else None,
            votes=votes,
            voted=voted,
        )


class DiscordConfigOut(BaseModel):
    # Deliberately no URL: it is a credential injected from the vault, and an
    # admin-readable endpoint that echoes one back is how it leaks. Callers
    # only need to know whether it is configured.
    webhook_configured: bool


class DiscordConfigIn(BaseModel):
    # Write-only: accepted here, never present in DiscordConfigOut. None or
    # empty means "leave whatever is stored alone", so the form can be saved
    # without re-typing a secret the server never sent it. Clearing it is its
    # own DELETE endpoint rather than a magic value.
    webhook_url: Optional[str] = None


class DiscordTestResult(BaseModel):
    ok: bool
    detail: str


class Intro2FileOut(BaseModel):
    """A challenge attachment, passed through from rCTF's v2 challenge list.

    `url` is whatever rCTF reported: origin-relative for the local upload
    provider, absolute for the S3/GCS ones. The frontend resolves it.
    """

    name: str
    url: str
    size: Optional[int] = None


class Intro2StepOut(BaseModel):
    challenge_id: str
    step: int
    title: str
    author: str = ""
    description: str
    status: str  # "done" | "in_progress" | "locked"
    # Carried so the INTRO2 page can open the same challenge modal the grid
    # uses - without these it could show a step but not let anyone solve it.
    category: str = ""
    files: list[Intro2FileOut] = []


class Intro2TrackOut(BaseModel):
    """One category's INTRO2 track."""

    category: str
    steps: list[Intro2StepOut] = []


class AdminStatsOut(BaseModel):
    """Only what this service owns.

    Player and challenge counts used to be here too, proxied straight from
    rCTF - the frontend reads those from rCTF itself now, the same way the
    home page always has.
    """

    submissions: int
    pending_writeups: int

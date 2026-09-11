from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlalchemy import Column, DateTime
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utc_column(*, nullable: bool = False) -> Column:
    """A timestamp column that keeps its offset.

    A plain `DateTime` drops the tzinfo, and the naive value then serializes
    with no offset - which a browser parses as *local* time.
    """
    return Column(DateTime(timezone=True), nullable=nullable)


class WriteupStatus(str, Enum):
    pending = "pending"
    published = "published"
    rejected = "rejected"


class Writeup(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    challenge_id: str = Field(index=True)
    # Submit-time snapshot of the name, kept for the Discord notifications
    # (a message sent in the past should read as it did then) and for the
    # moderation ones, which would otherwise need an rCTF call each. It is
    # deliberately *not* in any response: rCTF owns the current name, and a
    # renamed challenge would leave this stale. Clients join on challenge_id.
    challenge_name: str
    team_id: str = Field(index=True)
    team_name: str
    # Card preview text. Always public - it is the one part of a writeup that
    # a team who hasn't solved the challenge sees in the grid.
    summary: str
    # The two halves of the submitted document, split on its `:::solution`
    # marker at submit time.
    intro_md: str = ""
    solution_md: str = ""
    # Retired: writeups used to be an external link rather than stored
    # markdown.
    url: str = ""
    status: WriteupStatus = Field(default=WriteupStatus.pending, index=True)
    created_at: datetime = Field(default_factory=_utcnow, sa_column=_utc_column())
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = Field(
        default=None, sa_column=_utc_column(nullable=True)
    )
    reject_reason: Optional[str] = None


class WriteupVote(SQLModel, table=True):
    """One team's upvote on one writeup.

    Upvote-only, and the composite primary key is the uniqueness constraint -
    a team either has a row here or doesn't, so a double-tap can't double-count
    and there is no "score" column to drift out of sync with the votes.
    """

    writeup_id: int = Field(foreign_key="writeup.id", primary_key=True)
    team_id: str = Field(primary_key=True)
    created_at: datetime = Field(default_factory=_utcnow, sa_column=_utc_column())


class DiscordConfig(SQLModel, table=True):
    """Where notifications go. One webhook, no per-event switches.

    Everything this service notifies about is the writeup lifecycle, and it
    all goes here. First bloods are announced by rCTF's own blood bot, whose
    webhook lives in rCTF's config file rather than in this table.
    """

    id: Optional[int] = Field(default=1, primary_key=True)
    # Admin-set override for the vault-injected default. Write-only over the
    # API: settable via PUT, never returned by GET, so it cannot leak back out
    # to a client. Empty means "fall back to settings.discord_webhook_url".
    webhook_url: str = ""

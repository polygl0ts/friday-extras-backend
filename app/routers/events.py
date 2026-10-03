from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select

from app.auth import require_admin
from app.db import get_session
from app.models import Event
from app.rctf_client import TeamIdentity
from app.schemas import EventIn, EventOut

router = APIRouter(prefix="/events", tags=["events"])


@router.get("", response_model=list[EventOut])
def list_events(session: Session = Depends(get_session)) -> list[Event]:
    """Every event, oldest first. No auth, like `/decks`: the calendar is what
    we tell people who haven't joined yet, so `/calendar` works logged out.
    Splitting upcoming from past is left to the frontend - it has the clock
    the visitor actually reads.
    """
    return list(session.exec(select(Event).order_by(Event.starts_at)))


@router.post("", response_model=EventOut)
def create_event(
    body: EventIn,
    _: TeamIdentity = Depends(require_admin),
    session: Session = Depends(get_session),
) -> Event:
    event = Event.model_validate(body)
    session.add(event)
    session.commit()
    session.refresh(event)
    return event


# No PATCH: an event is four fields, so fixing a typo is delete and re-add.
@router.delete("/{event_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_event(
    event_id: int,
    _: TeamIdentity = Depends(require_admin),
    session: Session = Depends(get_session),
) -> None:
    event = session.get(Event, event_id)
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")
    session.delete(event)
    session.commit()

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select

from app.auth import require_admin
from app.db import get_session
from app.models import Deck
from app.rctf_client import TeamIdentity
from app.schemas import DeckIn, DeckOut, DeckPatch

router = APIRouter(prefix="/decks", tags=["decks"])


@router.get("", response_model=list[DeckOut])
def list_decks(session: Session = Depends(get_session)) -> list[Deck]:
    """The deck list, and the **one read route in this service with no auth**.

    Deliberate, not an oversight: workshop slides are the material we hand out
    at the meetings, the frontend fetches this with `auth: false`
    (`api/extras.ts`), and `/slides` is reachable without logging in.
    """
    return list(session.exec(select(Deck).order_by(Deck.sort_order)))


@router.post("", response_model=DeckOut)
def create_deck(
    body: DeckIn,
    _: TeamIdentity = Depends(require_admin),
    session: Session = Depends(get_session),
) -> Deck:
    deck = Deck.model_validate(body)
    session.add(deck)
    session.commit()
    session.refresh(deck)
    return deck


@router.patch("/{deck_id}", response_model=DeckOut)
def update_deck(
    deck_id: int,
    body: DeckPatch,
    _: TeamIdentity = Depends(require_admin),
    session: Session = Depends(get_session),
) -> Deck:
    deck = session.get(Deck, deck_id)
    if deck is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Deck not found")
    for field, value in body.model_dump(exclude_unset=True, exclude_none=True).items():
        setattr(deck, field, value)
    session.add(deck)
    session.commit()
    session.refresh(deck)
    return deck


@router.delete("/{deck_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_deck(
    deck_id: int,
    _: TeamIdentity = Depends(require_admin),
    session: Session = Depends(get_session),
) -> None:
    deck = session.get(Deck, deck_id)
    if deck is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Deck not found")
    session.delete(deck)
    session.commit()

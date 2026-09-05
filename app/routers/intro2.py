from typing import Any

from fastapi import APIRouter, Depends

from app.auth import get_fresh_identity
from app.rctf_client import RctfClient, TeamIdentity, get_rctf_client
from app.schemas import Intro2FileOut, Intro2StepOut, Intro2TrackOut

router = APIRouter(prefix="/intro2", tags=["intro2"])

INTRO2_TAG = "intro2"


def _sort_key(chall: dict[str, Any]) -> tuple[int, str]:
    return (chall.get("sortWeight") or 0, chall.get("name") or "")


def _files(chall: dict[str, Any]) -> list[Intro2FileOut]:
    """Attachments, passed through verbatim so the frontend can render the same
    downloads the tiered grid does.
    """
    raw_files = chall.get("files")
    return [
        Intro2FileOut(
            name=str(f.get("name") or ""),
            url=str(f.get("url") or ""),
            size=f.get("size"),
        )
        for f in (raw_files if isinstance(raw_files, list) else [])
        if isinstance(f, dict) and f.get("name") and f.get("url")
    ]


def _steps(challs: list[dict[str, Any]], solved: frozenset[str]) -> list[Intro2StepOut]:
    """One track's challenges as numbered steps, in order."""
    result: list[Intro2StepOut] = []
    unlocked = True  # first step of every track is always unlocked
    for index, chall in enumerate(sorted(challs, key=_sort_key), start=1):
        chall_id = str(chall.get("id"))
        if chall_id in solved:
            state = "done"
        elif unlocked:
            state = "in_progress"
            unlocked = False
        else:
            state = "locked"

        result.append(
            Intro2StepOut(
                challenge_id=chall_id,
                step=index,
                title=chall.get("name", chall_id),
                description=chall.get("description", ""),
                status=state,
                category=str(chall.get("category") or ""),
                files=_files(chall),
            )
        )
    return result


@router.get("/tracks", response_model=list[Intro2TrackOut])
async def intro2_tracks(
    identity: TeamIdentity = Depends(get_fresh_identity),
    client: RctfClient = Depends(get_rctf_client),
) -> list[Intro2TrackOut]:
    """Every INTRO2 track, one per category, in a single request."""
    challenges = await client.list_challenges()

    by_category: dict[str, list[dict[str, Any]]] = {}
    for chall in challenges:
        if INTRO2_TAG not in (chall.get("tags") or []):
            continue
        category = str(chall.get("category") or "").strip().lower()
        if not category:
            continue
        by_category.setdefault(category, []).append(chall)

    solved = identity.solved_challenge_ids

    return [
        Intro2TrackOut(category=category, steps=_steps(by_category[category], solved))
        for category in sorted(by_category)
    ]

from fastapi import APIRouter, Depends

from app.auth import get_fresh_identity
from app.rctf_client import RctfClient, TeamIdentity, get_rctf_client
from app.schemas import Intro2FileOut, Intro2StepOut

router = APIRouter(prefix="/intro2", tags=["intro2"])

INTRO2_TAG = "intro2"


@router.get("/track", response_model=list[Intro2StepOut])
async def intro2_track(
    identity: TeamIdentity = Depends(get_fresh_identity),
    client: RctfClient = Depends(get_rctf_client),
) -> list[Intro2StepOut]:
    challenges = await client.list_challenges()
    steps = [c for c in challenges if INTRO2_TAG in (c.get("tags") or [])]
    # `or 0`, not `.get("sortWeight", 0)`: v2 always emits the key and sends
    # `null` when a challenge has no weight (v1 omitted it instead), so the
    # default never applies and a mix of weighted and unweighted challenges
    # would raise TypeError comparing None to an int - a 500 on this endpoint.
    steps.sort(key=lambda c: (c.get("sortWeight") or 0, c.get("name") or ""))

    # Fresh identity: the frontend refetches this track the instant a flag is
    # accepted, so a cached solve set would show the step the player just
    # finished as still in progress.
    solved = identity.solved_challenge_ids

    result: list[Intro2StepOut] = []
    unlocked = True  # first step is always unlocked
    for index, chall in enumerate(steps, start=1):
        chall_id = str(chall.get("id"))
        if chall_id in solved:
            state = "done"
        elif unlocked:
            state = "in_progress"
            unlocked = False  # only one step is ever "in progress" at a time
        else:
            state = "locked"

        # Attachments are passed through verbatim so the frontend can render
        # the same downloads the tiered grid does. Guarded because the shape is
        # rCTF's, not ours: a non-list, or entries missing name/url, must not
        # take the whole track down with a validation error.
        raw_files = chall.get("files")
        files = [
            Intro2FileOut(
                name=str(f.get("name") or ""),
                url=str(f.get("url") or ""),
                size=f.get("size"),
            )
            for f in (raw_files if isinstance(raw_files, list) else [])
            if isinstance(f, dict) and f.get("name") and f.get("url")
        ]

        result.append(
            Intro2StepOut(
                challenge_id=chall_id,
                step=index,
                title=chall.get("name", chall_id),
                description=chall.get("description", ""),
                status=state,
                category=str(chall.get("category") or ""),
                files=files,
            )
        )
    return result

import hashlib
import time
from typing import Optional

from fastapi import Depends, Header, HTTPException, status

from app.config import settings
from app.rctf_client import RctfClient, TeamIdentity, get_rctf_client

# sha256(token) -> (identity, expires_at_monotonic). Process-local by design -
# this is a small internal tool, not something that needs a real cache backend.
#
# Keyed by a hash rather than the token itself. An rCTF auth token is a
# full-power, non-expiring credential for the player's whole account, so the
# difference matters: anything that dumps or inspects this dict - a heap dump,
# a debugger, a stray `repr()` in an error path - yields digests instead of a
# set of live logins. The token is still in memory transiently while a request
# is being served; what this removes is a *stored* copy with an obvious name.
#
# (This is also the entirety of what rCTF's external-auth flow would have
# bought us. Its `/token` hands back "the same non-expiring token issued at
# login" with full account access and no revocation, so routing the login
# through it would move an identically powerful credential through more steps.
# See the D5 note in docs/CODEBASE.md.)
_identity_cache: dict[str, tuple[TeamIdentity, float]] = {}

# Bounded so a stream of invalid-but-well-formed bearers cannot grow it without
# limit. One entry per active team, so this is far above any real usage.
_MAX_CACHED_IDENTITIES = 512


def _cache_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _evict(now: float) -> None:
    """Drop expired entries, then oldest-first until there is room.

    Entries all share one TTL, so insertion order is expiry order and the
    front of the dict is the soonest to lapse.
    """
    for key in [k for k, (_, expires) in _identity_cache.items() if expires <= now]:
        del _identity_cache[key]
    while len(_identity_cache) >= _MAX_CACHED_IDENTITIES:
        _identity_cache.pop(next(iter(_identity_cache)))


async def _resolve_token(
    token: str, client: RctfClient, *, force: bool = False
) -> Optional[TeamIdentity]:
    key = _cache_key(token)
    now = time.monotonic()

    if not force:
        cached = _identity_cache.get(key)
        if cached and cached[1] > now:
            return cached[0]

    identity = await client.get_current_identity(token)
    if identity is not None:
        # Re-inserted rather than updated in place, so a refreshed entry moves
        # to the back and insertion order stays expiry order.
        _identity_cache.pop(key, None)
        _evict(now)
        _identity_cache[key] = (identity, now + settings.identity_cache_seconds)
    return identity


def _bearer(authorization: Optional[str]) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    return authorization.split(" ", 1)[1].strip()


async def _identity(
    authorization: Optional[str], client: RctfClient, *, force: bool
) -> TeamIdentity:
    identity = await _resolve_token(_bearer(authorization), client, force=force)
    if identity is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired rCTF session")
    return identity


async def get_current_identity(
    authorization: Optional[str] = Header(default=None),
    client: RctfClient = Depends(get_rctf_client),
) -> TeamIdentity:
    """The cheap one: served from the cache for `identity_cache_seconds`.

    Fine for anything that only needs to know *who* is calling, and for
    solve-gating that is allowed to be a little behind - reading an already
    published writeup, voting.
    """
    return await _identity(authorization, client, force=False)


async def get_fresh_identity(
    authorization: Optional[str] = Header(default=None),
    client: RctfClient = Depends(get_rctf_client),
) -> TeamIdentity:
    """Always re-asks rCTF, and refreshes the cache on the way through.

    For routes where a stale `solved_challenge_ids` would be *wrong* rather
    than merely dated. The solve set now rides along on the cached identity, so
    without this a team that solved a challenge seconds ago would be told to
    "solve this challenge before posting a writeup", and the INTRO2 track would
    refuse to advance - for up to `identity_cache_seconds`. Both are refetched
    by the frontend immediately after a correct flag, which is exactly when the
    cache is guaranteed to be behind.

    Costs one request, which is what these routes paid anyway when solve state
    was its own `/users/:id` call.
    """
    return await _identity(authorization, client, force=True)


async def require_admin(
    identity: TeamIdentity = Depends(get_current_identity),
) -> TeamIdentity:
    if not identity.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin access required")
    return identity


# `TeamIdentity.banned` is populated (rCTF v2's `/users/me` reports it; v1's
# response schema omits the field entirely, which is why this service reads v2)
# but deliberately not acted on yet: whether a team banned in rCTF may still
# post writeups or upvote is a moderation policy for the committee to set, not
# a detail of reading the API. rCTF itself only stops a banned team from
# ranking - it still authenticates.
#
# To enforce it, add a dependency here and swap it in on the write routes
# (writeups submit/edit/vote), leaving reads open:
#
#     async def get_unbanned_identity(
#         identity: TeamIdentity = Depends(get_current_identity),
#     ) -> TeamIdentity:
#         if identity.banned:
#             raise HTTPException(status.HTTP_403_FORBIDDEN, "Team is banned")
#         return identity

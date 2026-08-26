"""
Thin async client wrapping the bits of rCTF's REST API this service needs.

Every endpoint path/response-shape assumption used elsewhere in the app is
isolated to this one file on purpose: if a path or field name turns out to be
wrong, this is the only file that needs to change.

Status of those assumptions, verified against otter-sec/rctf `main`:

- `/challs` (v2) and `/users/me` - confirmed against the published API
  reference at https://rctf.osec.io/api/.
- every path this file calls exists on **both** v1 and v2, so `_base` is a
  free choice; it defaults to v2 because only v2's `/users/me` reports
  `banned`. rCTF re-parses each response against a per-version zod schema on
  the way out (`apps/api/src/lib/router.ts`), so a field missing from a
  version's schema is silently stripped rather than merely undocumented -
  which is the mechanism behind `tags` on v1 and `banned` on v1 alike.
- `/leaderboard/now` - the wrapper shape (`{leaderboard, total}` vs a bare
  list) is still handled defensively; the mock and the real instance differ.
- `perms` and `total` were both checked against a real rCTF instance rather
  than only read about; see the notes on `_PERM_CHALLS_READ` and
  `get_leaderboard_size`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import httpx

from app.config import settings

# rCTF permission bits, as returned in `/users/me`'s `perms` bitmask.
#
# The numbers are not in rCTF's docs (which name the permissions but never
# print their values), so they were derived by promoting throwaway users with
# `rctf user promote --perms <name>` on a real instance and reading `perms`
# back: challsRead=1, challsWrite=2, challsSolveWrite=8, usersWrite=16,
# settingsWrite=32, and a full admin is 63 - so bit 4 is a sixth permission the
# CLI does not name. Only the one bit this service actually tests is defined
# here; adding the rest would be guessing at which is which.
_PERM_CHALLS_READ = 1 << 0


@dataclass
class TeamIdentity:
    team_id: str
    team_name: str
    is_admin: bool
    #: Whether rCTF has banned this team. v2-only - v1's `/users/me` response
    #: schema has no `banned` field, so on a v1 base this is always False.
    #: A banned team still authenticates against rCTF; it only stops ranking.
    #: Nothing here refuses it yet - see the note in app/auth.py.
    banned: bool = False
    #: Challenge ids this team has solved, read from the same `/users/me`
    #: response as everything else here. Beware staleness: an identity served
    #: from `app.auth`'s cache carries the solve set as of up to
    #: `identity_cache_seconds` ago, which is why solve-gated routes depend on
    #: `get_fresh_identity` instead.
    solved_challenge_ids: frozenset[str] = field(default_factory=frozenset)


def _unwrap(raw: object) -> object:
    """rCTF wraps most responses as `{kind, message, data}`, but not every
    endpoint/fork does (the mock server used for local dev returns some
    endpoints as a bare list) - unwrap defensively rather than assuming a
    dict, since a bare list has no `.get` and would otherwise blow up every
    call site below with an AttributeError."""
    if isinstance(raw, dict) and "data" in raw:
        return raw["data"]
    return raw


class RctfClient:
    def __init__(self, origin: str, api_base: str) -> None:
        self._origin = origin.rstrip("/")
        self._base = f"{self._origin}{api_base}"
        # rCTF's v2 API is additive, not a replacement: routes that did not need
        # to change were never re-issued, so `auth/login` and
        # `challs/:id/submit` exist only on v1. Neither is called from here -
        # they are browser-side - so `_base` now defaults to v2 (see
        # app/config.py) and every path below exists on both versions.
        #
        # `_v2` is kept even so: the challenge list is wrong on anything but v2
        # (v1 strips `tags`) and must not follow a reconfigured `_base` back.
        # tests/test_rctf_client_paths.py pins it.
        self._v2 = f"{self._origin}/api/v2"

    async def get_current_identity(self, bearer_token: str) -> Optional[TeamIdentity]:
        """Resolve a player-supplied bearer token to a team identity.

        Forwards the token straight to rCTF's own `/users/me` and trusts the
        response - this service never sees or needs rCTF's token-signing
        secret.

        **One request, three answers.** `/users/me` returns the team id/name,
        the `perms` bitmask *and* the full `solves[]` list, so admin status and
        solve state both come out of this response instead of the two extra
        round trips they used to cost:

        - admin was a probe against `/admin/challs`, reading only its status
          code. That worked, but it fetched every challenge - including every
          `flag` in plaintext, which the admin challenge list returns - across
          the network into this container purely to learn "200 or 403".
        - solve state was a second call to the *public* `/users/:id`. Every
          caller asked about its own team, so it was asking a public endpoint
          for data the authenticated one had already sent.
        """
        headers = {"Authorization": f"Bearer {bearer_token}"}
        async with httpx.AsyncClient(timeout=5.0) as client:
            try:
                resp = await client.get(f"{self._base}/users/me", headers=headers)
            except httpx.HTTPError:
                return None
            if resp.status_code != 200:
                return None
            data = _unwrap(resp.json())
            if not isinstance(data, dict):
                return None
            team_id = str(data.get("id", ""))
            team_name = data.get("name", "")
            if not team_id:
                return None

            # `perms` is documented as `number | null`, null meaning a standard
            # user - but a real instance returns 0 for an unpromoted account,
            # so both have to read as "no permissions".
            #
            # Testing challsRead specifically keeps this meaning exactly what
            # the old `/admin/challs` probe meant, since that route is gated on
            # challsRead. It is a loose definition of "admin" for a service
            # whose admin actions are writeup moderation - a challenge author
            # holding only challsRead qualifies - but tightening it (say to
            # usersWrite) is a policy change, not part of dropping the probe.
            perms = data.get("perms") or 0
            is_admin = bool(int(perms) & _PERM_CHALLS_READ) if isinstance(perms, int) else False

            solves = data.get("solves") or []
            solved = frozenset(
                str(s["id"])
                for s in solves
                if isinstance(s, dict) and s.get("id") is not None
            )

            return TeamIdentity(
                team_id=team_id,
                team_name=team_name,
                is_admin=is_admin,
                # v2 types this as a plain boolean and always sends it. On a v1
                # base the key is absent, which reads as "not banned" - the same
                # answer v1 gave before it could be asked.
                banned=bool(data.get("banned")),
                solved_challenge_ids=solved,
            )

    async def list_challenges(self) -> list[dict]:
        """Challenge list, from **v2 specifically** rather than `_base`.

        `tags` - which carries the INTRO2 marker and the grid tier - exists
        only on v2. The v1 handler does spread it into its payload, but v1's
        response schema has no `tags` field and rCTF re-parses every response
        against that schema on the way out, so on v1 it is silently stripped
        and the INTRO2 track is permanently empty. v2 also types it as
        `string[] | null` (key always present), hence the `or []` guards at the
        call sites.

        Contrary to what the comments here used to claim, GET /challs is
        `authRequired: false` on both v1 and v2. It is `onlyWhenStarted: true`,
        though, and the bypass for that is keyed on `Permissions.challsRead` -
        so forwarding a token only helps before the CTF opens, and only if that
        token's user actually holds that permission. With `startTime: 0` in the
        infra repo the gate never trips and the token is redundant; it is still
        forwarded when configured because it costs nothing and makes v2 fill in
        the per-team `yourScore` fields.

        Falls back to [] on any error so the rest of the app degrades
        gracefully if rCTF is briefly unreachable.
        """
        headers = (
            {"Authorization": f"Bearer {settings.rctf_admin_token}"}
            if settings.rctf_admin_token
            else {}
        )
        async with httpx.AsyncClient(timeout=5.0) as client:
            try:
                resp = await client.get(f"{self._v2}/challs", headers=headers)
            except httpx.HTTPError:
                return []
            if resp.status_code != 200:
                return []
            data = _unwrap(resp.json())
            return data if isinstance(data, list) else []

    async def get_leaderboard_size(self) -> int:
        """Number of ranked teams, from `total` - not by counting entries.

        **No caller in this service any more**: the admin dashboard's player
        count is rCTF's answer, so the frontend now asks rCTF for it directly.
        Kept because the pagination lesson below is the expensive part, and any
        future server-side reader of the leaderboard needs it.

        This used to ask for 500 entries and `len()` them, which was not merely
        wasteful but **broken**: `leaderboard.maxLimit` defaults to 100 and a
        larger `limit` is rejected outright. Verified against a real instance -
        `limit=500` answers `400 badBody {"reason": "Invalid limit or offset"}`,
        as does 101. The error path here returns 0, so on any stock deployment
        the admin dashboard silently read "0 players".

        `total` is independent of `limit` (checked at 1, 5 and 100 against the
        same instance: all returned `total=12`), so one entry is enough to
        carry it.

        Note this counts *ranked* teams, which is not the same as registered
        ones - banned teams are excluded, and a team with no score may not
        rank. Registered-team counts live behind `/v2/admin/users`, which needs
        `usersWrite`.
        """
        async with httpx.AsyncClient(timeout=5.0) as client:
            try:
                resp = await client.get(
                    f"{self._base}/leaderboard/now", params={"limit": 1, "offset": 0}
                )
            except httpx.HTTPError:
                return 0
            if resp.status_code != 200:
                return 0
            data = _unwrap(resp.json())
            if isinstance(data, dict) and data.get("total") is not None:
                return int(data["total"])
            # No `total`: the dev mock and every documented rCTF response carry
            # one, so this is a shape surprise rather than an empty scoreboard.
            return 0


_default_client = RctfClient(settings.rctf_origin, settings.rctf_api_base)


def get_rctf_client() -> RctfClient:
    return _default_client

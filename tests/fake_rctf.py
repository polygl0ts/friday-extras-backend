import dataclasses
from typing import Optional

from app.rctf_client import TeamIdentity


class FakeRctfClient:
    """Stand-in for RctfClient used across the test suite via a FastAPI
    dependency_overrides swap - lets tests drive team identity, solve state
    and the public challenge list without a real rCTF instance."""

    def __init__(self) -> None:
        self.identities: dict[str, TeamIdentity] = {}
        self.solves: dict[str, set[str]] = {}
        self.challenges: list[dict] = []
        self.leaderboard_size = 0
        #: Every call recorded, so tests can assert the *shape* of the traffic
        #: rather than only its result.
        self.calls: list[str] = []

    async def get_current_identity(self, bearer_token: str) -> Optional[TeamIdentity]:
        """Merge `self.solves` onto the stored identity.

        The real client reads solve state out of the same `/users/me` response
        as the id and name, so the fake has to as well - otherwise a route
        reading `identity.solved_challenge_ids` sees nothing regardless of what
        a test put in `solves`. Tests keep setting `fake.solves[team] = {...}`
        as before; only the plumbing moved.
        """
        identity = self.identities.get(bearer_token)
        if identity is None:
            return None
        return dataclasses.replace(
            identity,
            solved_challenge_ids=frozenset(self.solves.get(identity.team_id, set())),
        )

    async def list_challenges(self) -> list[dict]:
        self.calls.append("list_challenges")
        return self.challenges

    async def get_leaderboard_size(self) -> int:
        return self.leaderboard_size

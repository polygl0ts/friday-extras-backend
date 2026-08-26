"""Splitting a submitted writeup into its public and gated halves.

A writeup is one markdown document carrying an explicit boundary marker::

    The service reuses a prime across two of the three moduli, so a
    pairwise GCD recovers p without factoring anything.

    :::solution

    ```python
    p = gcd(n1, n2)
    ```

Everything above the marker is readable by anyone; everything below is only
served to teams that solved the challenge. The split happens once, at submit
time, into two columns - so the gating boundary elsewhere in the app is "did
we read this column", never "did a parser correctly classify this text".
That is deliberate: an explicit marker has exactly one edge case to get right
(below), where redacting code fences out of arbitrary markdown would have as
many as CommonMark has ways to write a code block.
"""

import re

from app.config import settings

MARKER = ":::solution"


class WriteupFormatError(ValueError):
    """Raised when a submitted document cannot be split. Surfaces as a 422."""


def split_writeup(body_md: str) -> tuple[str, str]:
    """Return (intro_md, solution_md).

    The marker counts only when it sits at column 0 and outside a code fence,
    so a writeup that *documents* this syntax - or one whose exploit happens
    to print `:::solution` - does not split in the wrong place.
    """
    lines = body_md.splitlines()
    fence: str | None = None
    split_at: int | None = None

    for i, line in enumerate(lines):
        stripped = line.strip()
        if fence is not None:
            if stripped.startswith(fence):
                fence = None
            continue
        if stripped[:3] in ("```", "~~~"):
            fence = stripped[:3]
            continue
        # `rstrip` only: an indented marker is inside an indented code block
        # (or a list item), not a boundary.
        if line.rstrip() == MARKER:
            split_at = i
            break

    if split_at is None:
        raise WriteupFormatError(
            f"Writeup must contain a line reading exactly `{MARKER}`, marking "
            "where the spoiler-free part ends and the full solution begins."
        )

    intro = "\n".join(lines[:split_at]).strip()
    solution = "\n".join(lines[split_at + 1 :]).strip()

    if not intro:
        raise WriteupFormatError(
            "The part above the marker is empty - write at least a short "
            "spoiler-free description of the approach."
        )
    if not solution:
        raise WriteupFormatError("The part below the marker is empty.")

    return intro, solution


def join_writeup(intro_md: str, solution_md: str) -> str:
    """Rebuild the single document an author edits. Only the two halves are
    stored, so this is what the edit form is populated from - keeping one
    source of truth and no chance of the stored document and the stored
    halves drifting apart."""
    return f"{intro_md}\n\n{MARKER}\n\n{solution_md}"


def _flag_pattern() -> re.Pattern[str]:
    prefixes = "|".join(re.escape(p) for p in settings.flag_prefixes if p)
    return re.compile(rf"(?i)\b({prefixes})\{{[^}}]{{0,200}}\}}")


def scrub_flags(text: str) -> str:
    """Blank out anything shaped like a flag.

    Defence in depth for the public half only - admin review is the actual
    control. This catches the common accident (pasting the flag into the
    intro) without pretending to catch a determined leak.
    """
    if not settings.flag_prefixes:
        return text
    return _flag_pattern().sub(r"\1{[redacted]}", text)

import pytest

from app.writeup_md import MARKER, WriteupFormatError, join_writeup, split_writeup

BODY = """The modulus is reused across two keys.

:::solution

```python
p = gcd(n1, n2)
```
"""


def test_splits_on_marker() -> None:
    intro, solution = split_writeup(BODY)
    assert intro == "The modulus is reused across two keys."
    assert solution == "```python\np = gcd(n1, n2)\n```"


def test_marker_inside_a_code_fence_is_not_a_boundary() -> None:
    body = f"""Intro prose.

```text
{MARKER}
```

{MARKER}

The real solution.
"""
    intro, solution = split_writeup(body)
    # The fenced occurrence stays in the intro; the bare one splits.
    assert MARKER in intro
    assert solution == "The real solution."


def test_marker_inside_a_tilde_fence_is_not_a_boundary() -> None:
    body = f"~~~\n{MARKER}\n~~~\n\n{MARKER}\n\nreal\n"
    intro, solution = split_writeup(body)
    assert MARKER in intro
    assert solution == "real"


def test_indented_marker_is_not_a_boundary() -> None:
    # Four-space indent makes it an indented code block, not a boundary.
    body = f"Intro.\n\n    {MARKER}\n\n{MARKER}\n\nreal\n"
    intro, solution = split_writeup(body)
    assert MARKER in intro
    assert solution == "real"


def test_trailing_whitespace_after_marker_still_splits() -> None:
    intro, solution = split_writeup(f"Intro.\n\n{MARKER}   \n\nreal\n")
    assert intro == "Intro."
    assert solution == "real"


@pytest.mark.parametrize(
    "body",
    [
        "Just prose, no marker anywhere.",
        f"{MARKER}\n\nsolution only, no intro",
        f"intro only\n\n{MARKER}\n",
    ],
)
def test_unsplittable_documents_are_rejected(body: str) -> None:
    with pytest.raises(WriteupFormatError):
        split_writeup(body)


def test_join_round_trips() -> None:
    intro, solution = split_writeup(BODY)
    assert split_writeup(join_writeup(intro, solution)) == (intro, solution)

"""Collision-resistant platform identifier generators.

Identifier types intentionally use separate shapes: PID is nine characters,
RID is ten characters, and the panelist UID is nineteen characters with
hyphens. That makes cross-type equality impossible before database uniqueness
is even considered.
"""

import secrets
import string


PID_ALPHABET = string.ascii_letters + string.digits


def generate_platform_pid(length: int | None = None) -> str:
    """Return a 6-9 character PID with upper, lower and numeric characters."""

    length = secrets.randbelow(4) + 6 if length is None else length
    if length < 6 or length > 9:
        raise ValueError("PID length must be between 6 and 9 characters.")
    characters = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        *(secrets.choice(PID_ALPHABET) for _ in range(length - 3)),
    ]
    secrets.SystemRandom().shuffle(characters)
    return "".join(characters)

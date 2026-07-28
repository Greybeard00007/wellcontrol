"""Class codes, instructor login and signed tokens.

**Deliberately not user accounts.** A class is a temporary thing: fifteen people
turn up, fly a kill, and leave. Creating and deleting fifteen accounts per class
means password resets during a lesson and a permanent user table full of people
who came once. So the only durable credential in the system is the instructor's,
and students get in with a **class code** that expires on its own.

Three pieces, all standard library so the module tests without the web stack:

* :func:`class_code` -- the code students type. Eight characters from an
  alphabet with no O/0 or I/1, because it gets read off a whiteboard.
* :func:`hash_password` / :func:`verify_password` -- PBKDF2, for the one
  credential that persists.
* :class:`TokenSigner` -- signed, expiring tokens carrying who the bearer is.
  Signed rather than stored, so a server restart does not log everybody out
  mid-exercise and there is no session table to keep.

## On exposing this to the internet

A code short enough to read aloud is short enough to guess, so
:class:`RateLimiter` throttles code entry per client. Without it, an eight
character code from a 32 letter alphabet is not the barrier it looks like when
an attacker can try it a thousand times a second.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Callable

#: No O/0, I/1, or S/5. Codes get read off a whiteboard and typed by someone
#: at the back of the room.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRTUVWXY2346789"

#: Characters per code, before the hyphen goes in.
CODE_LENGTH = 8

#: PBKDF2 rounds. Slow enough to matter, fast enough that a login is instant.
PBKDF2_ROUNDS = 240_000

#: How long a student's token is good for. Longer than any single exercise, so
#: a reload mid-kill does not throw them out.
STUDENT_TOKEN_TTL_S = 12 * 3600

#: The instructor's, which is shorter because it carries more authority.
INSTRUCTOR_TOKEN_TTL_S = 8 * 3600


# --------------------------------------------------------------------------
# Class codes
# --------------------------------------------------------------------------

def class_code(length: int = CODE_LENGTH) -> str:
    """A fresh class code, formatted ``XXXX-XXXX``.

    Uses :mod:`secrets` rather than :mod:`random`: this is a credential, even
    if it is a short-lived one.
    """
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))
    half = length // 2
    return f"{raw[:half]}-{raw[half:]}" if half else raw


def normalise_code(code: str) -> str:
    """Fold a typed code to canonical form.

    Students type ``kick7q2m``, ``KICK 7Q2M`` and ``KICK-7Q2M`` and mean the
    same thing. Hyphens and spaces come out, case goes up.
    """
    return code.replace("-", "").replace(" ", "").strip().upper()


# --------------------------------------------------------------------------
# The one password
# --------------------------------------------------------------------------

def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """Encode a password as ``pbkdf2_sha256$rounds$salt$hash``."""
    salt = salt if salt is not None else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS
    )
    return "$".join((
        "pbkdf2_sha256",
        str(PBKDF2_ROUNDS),
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    ))


def verify_password(password: str, encoded: str) -> bool:
    """Check a password against an encoding from :func:`hash_password`.

    Returns False rather than raising on a malformed encoding: a corrupt
    config should fail closed, not 500.
    """
    try:
        scheme, rounds, salt_b64, digest_b64 = encoded.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, int(rounds)
    )
    return hmac.compare_digest(actual, expected)


# --------------------------------------------------------------------------
# Tokens
# --------------------------------------------------------------------------

def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


@dataclass
class TokenSigner:
    """Signs and verifies short-lived bearer tokens.

    The token carries its own payload and expiry and is signed with HMAC, so
    there is nothing to look up and nothing to invalidate on restart. That is
    the right trade here: an exercise lasts hours, a class lasts a day, and
    losing everybody's login because uvicorn reloaded would be worse than the
    theoretical inability to revoke a single token early. Dropping a student
    from the roster still locks them out, because the roster is checked on
    every request.

    Args:
        secret: Signing key. Generate with ``secrets.token_bytes(32)``.
        clock: Source of seconds. Injected so expiry is tested without waiting.
    """

    secret: bytes
    clock: Callable[[], float] = time.time

    def sign(self, payload: dict, ttl_s: float) -> str:
        body = dict(payload)
        body["exp"] = self.clock() + ttl_s
        encoded = _b64(json.dumps(body, separators=(",", ":"), sort_keys=True).encode())
        signature = hmac.new(self.secret, encoded.encode("ascii"), hashlib.sha256)
        return f"{encoded}.{_b64(signature.digest())}"

    def verify(self, token: str) -> dict | None:
        """Return the payload, or None if the token is bad, altered or expired."""
        try:
            encoded, signature = token.split(".")
        except (ValueError, AttributeError):
            return None
        expected = hmac.new(self.secret, encoded.encode("ascii"), hashlib.sha256)
        if not hmac.compare_digest(_b64(expected.digest()), signature):
            return None
        try:
            payload = json.loads(_unb64(encoded))
        except (ValueError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("exp", 0) < self.clock():
            return None
        return payload


# --------------------------------------------------------------------------
# Throttling
# --------------------------------------------------------------------------

@dataclass
class RateLimiter:
    """Fixed-window attempt counter, keyed by whatever identifies a caller.

    Guards code entry and instructor login. A class code is short by design --
    it has to be readable across a room -- and a short code is only a barrier
    if guesses are expensive.

    Args:
        limit: Attempts allowed inside one window.
        window_s: Length of the window.
        clock: Injected for testing.
    """

    limit: int = 10
    window_s: float = 60.0
    clock: Callable[[], float] = time.monotonic
    _hits: dict[str, list[float]] = field(default_factory=dict, repr=False)

    def allow(self, key: str) -> bool:
        """Record an attempt. False once the caller is over the limit."""
        now = self.clock()
        cutoff = now - self.window_s
        hits = [t for t in self._hits.get(key, ()) if t > cutoff]
        hits.append(now)
        self._hits[key] = hits
        return len(hits) <= self.limit

    def reset(self, key: str) -> None:
        """Forget a caller's attempts. Called after a successful login."""
        self._hits.pop(key, None)


def _main() -> None:
    """``python -m wellcontrol.sim.auth`` -- print a hash for the env file.

    Prompts rather than taking an argument so the plaintext does not end up in
    the shell history.
    """
    import getpass

    password = getpass.getpass("Instructor password: ")
    if password != getpass.getpass("Again: "):
        raise SystemExit("They do not match.")
    if not password:
        raise SystemExit("Empty password refused.")
    print()
    print("WELLCONTROL_INSTRUCTOR_PASSWORD_HASH=" + hash_password(password))


if __name__ == "__main__":
    _main()

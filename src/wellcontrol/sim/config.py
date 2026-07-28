"""Deployment settings, read from the environment.

Kept out of :mod:`~wellcontrol.sim.api` so it can be tested without the web
stack, and out of the repository entirely so no credential is ever committed.

Three settings, all optional, all with a safe default:

``WELLCONTROL_INSTRUCTOR_PASSWORD_HASH``
    Output of :func:`~wellcontrol.sim.auth.hash_password`. The preferred form:
    the plaintext never exists on the server.

``WELLCONTROL_INSTRUCTOR_PASSWORD``
    Plaintext, hashed at startup. Convenient for a laptop, worse for a server
    because it sits in the process environment and in whatever started it.

``WELLCONTROL_SECRET_KEY``
    Signs tokens. Set it in production, or a restart logs everybody out.

``WELLCONTROL_TRUST_PROXY``
    Set when something like Nginx Proxy Manager sits in front. **This matters
    more than it looks.** Behind a proxy every request arrives from the proxy's
    own address, so per-caller rate limiting would throttle the entire class as
    one caller the moment anyone mistyped a code. With it set, the forwarded
    address is used instead. Leave it unset when the app is directly exposed,
    because a client can put anything in that header.

**With nothing set, a password is generated and printed to the log.** That is
deliberate: the alternative is either an unprotected instructor console or a
server that refuses to start on a laptop. Printing a one-time password is the
same move Jupyter makes, and for the same reason -- it fails visibly rather
than silently leaving the door open.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass

from .auth import hash_password

#: Words a generated password is built from. Readable over a shoulder, which
#: matters when it is printed to a terminal and typed into a browser.
_WORDS = (
    "annulus", "barite", "casing", "choke", "driller", "influx", "kelly",
    "kick", "mudline", "packer", "riser", "shoe", "stripping", "tally",
    "volumetric", "wellhead",
)


def _generated_password() -> str:
    return "-".join(secrets.choice(_WORDS) for _ in range(3))


@dataclass
class Settings:
    """Resolved settings, plus whether anything had to be invented."""

    password_hash: str
    secret_key: bytes
    #: Set when no password was configured, so the caller can print it once.
    generated_password: str | None = None
    #: Set when the signing key was invented, so the caller can warn that a
    #: restart will invalidate every token.
    generated_secret: bool = False
    #: Whether to believe X-Forwarded-For. True only behind a proxy you own.
    trust_proxy: bool = False

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Settings":
        env = os.environ if env is None else env

        generated_password = None
        password_hash = env.get("WELLCONTROL_INSTRUCTOR_PASSWORD_HASH", "").strip()
        if not password_hash:
            plaintext = env.get("WELLCONTROL_INSTRUCTOR_PASSWORD", "").strip()
            if not plaintext:
                plaintext = generated_password = _generated_password()
            password_hash = hash_password(plaintext)

        raw_secret = env.get("WELLCONTROL_SECRET_KEY", "").strip()
        generated_secret = not raw_secret
        secret_key = (
            raw_secret.encode("utf-8") if raw_secret else secrets.token_bytes(32)
        )

        trust_proxy = env.get("WELLCONTROL_TRUST_PROXY", "").strip().lower() in (
            "1", "true", "yes", "on"
        )

        return cls(
            password_hash=password_hash,
            secret_key=secret_key,
            generated_password=generated_password,
            generated_secret=generated_secret,
            trust_proxy=trust_proxy,
        )

    def startup_notes(self) -> list[str]:
        """Lines worth putting in the log at boot. Empty when fully configured."""
        notes: list[str] = []
        if self.generated_password:
            notes.append(
                "No instructor password configured. This run's password is:  "
                f"{self.generated_password}"
            )
            notes.append(
                "Set WELLCONTROL_INSTRUCTOR_PASSWORD_HASH to keep one across "
                "restarts (wellcontrol.sim.auth.hash_password generates it)."
            )
        if self.generated_secret:
            notes.append(
                "No WELLCONTROL_SECRET_KEY set: tokens are signed with a random "
                "key, so restarting will sign everybody out mid-exercise."
            )
        return notes

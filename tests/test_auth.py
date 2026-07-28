"""Class codes, the instructor password and signed tokens.

Standard library only, so this runs in the dependency-free suite alongside the
physics. Everything time-dependent takes an injected clock -- no sleeping.
"""

import pytest

from wellcontrol.sim.auth import (
    CODE_ALPHABET,
    RateLimiter,
    TokenSigner,
    class_code,
    hash_password,
    normalise_code,
    verify_password,
)


class FakeClock:
    """A clock a test advances by hand."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += seconds


class TestClassCodes:
    def test_no_ambiguous_characters(self):
        """A code gets read off a whiteboard by someone at the back.

        O/0, I/1 and S/5 are the pairs that get mistyped, so none of them are
        in the alphabet at all.
        """
        for banned in "O0I1S5":
            assert banned not in CODE_ALPHABET

    def test_formatted_in_two_halves(self):
        code = class_code()
        assert len(code) == 9
        assert code[4] == "-"

    def test_codes_differ(self):
        codes = {class_code() for _ in range(200)}
        assert len(codes) > 190, "codes should not collide at this rate"

    @pytest.mark.parametrize(
        "typed", ["kick7q2m", "KICK-7Q2M", "kick 7q2m", " KICK-7Q2M "]
    )
    def test_normalise_accepts_what_students_actually_type(self, typed):
        assert normalise_code(typed) == "KICK7Q2M"


class TestPassword:
    def test_round_trip(self):
        encoded = hash_password("correct horse")
        assert verify_password("correct horse", encoded)

    def test_wrong_password_rejected(self):
        assert not verify_password("wrong", hash_password("right"))

    def test_salted(self):
        """Two encodings of the same password must differ."""
        assert hash_password("same") != hash_password("same")

    @pytest.mark.parametrize(
        "junk", ["", "nonsense", "pbkdf2_sha256$notanint$x$y", "a$b$c$d"]
    )
    def test_malformed_encoding_fails_closed(self, junk):
        """A corrupt config should refuse the login, not raise a 500."""
        assert not verify_password("anything", junk)


class TestTokens:
    def signer(self, clock=None):
        return TokenSigner(secret=b"test-secret-key", clock=clock or FakeClock())

    def test_round_trip(self):
        signer = self.signer()
        token = signer.sign({"student_id": "abc", "classroom_id": "xyz"}, ttl_s=60)
        payload = signer.verify(token)
        assert payload["student_id"] == "abc"
        assert payload["classroom_id"] == "xyz"

    def test_expiry(self):
        clock = FakeClock()
        signer = self.signer(clock)
        token = signer.sign({"who": "student"}, ttl_s=60)
        clock.tick(59)
        assert signer.verify(token) is not None
        clock.tick(2)
        assert signer.verify(token) is None

    def test_tampering_rejected(self):
        """Flipping the payload must invalidate the signature."""
        signer = self.signer()
        token = signer.sign({"role": "student"}, ttl_s=60)
        body, signature = token.split(".")
        forged = f"{body[:-1]}{'A' if body[-1] != 'A' else 'B'}.{signature}"
        assert signer.verify(forged) is None

    def test_a_different_key_is_rejected(self):
        """A token signed with one key must not verify under another."""
        token = TokenSigner(secret=b"key-one").sign({"role": "instructor"}, 60)
        assert TokenSigner(secret=b"key-two").verify(token) is None

    @pytest.mark.parametrize("junk", ["", "no-dot", "a.b.c", "!!!.???"])
    def test_malformed_token_rejected(self, junk):
        assert self.signer().verify(junk) is None


class TestRateLimiter:
    def test_blocks_past_the_limit(self):
        """A code short enough to read aloud is only a barrier if guesses cost."""
        limiter = RateLimiter(limit=3, window_s=60, clock=FakeClock())
        assert [limiter.allow("1.2.3.4") for _ in range(4)] == [
            True, True, True, False
        ]

    def test_callers_are_counted_separately(self):
        limiter = RateLimiter(limit=1, window_s=60, clock=FakeClock())
        assert limiter.allow("a")
        assert limiter.allow("b")
        assert not limiter.allow("a")

    def test_window_expires(self):
        clock = FakeClock()
        limiter = RateLimiter(limit=1, window_s=60, clock=clock)
        assert limiter.allow("a")
        assert not limiter.allow("a")
        clock.tick(61)
        assert limiter.allow("a")

    def test_reset_after_success(self):
        limiter = RateLimiter(limit=1, window_s=60, clock=FakeClock())
        limiter.allow("a")
        limiter.reset("a")
        assert limiter.allow("a")

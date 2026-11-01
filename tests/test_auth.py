"""Auth dataclasses must not leak secrets through ``repr()``."""

import dataclasses

from stint.client.auth import APITokenAuth, BasicAuth, PATAuth


def test_repr_masks_secrets() -> None:
    pa = PATAuth("s3cret-token")
    ba = BasicAuth("user", "s3cret-password")
    at = APITokenAuth("you@example.com", "s3cret-token")

    assert repr(pa) == "PATAuth()"
    assert repr(ba) == "BasicAuth(username='user')"
    assert repr(at) == "APITokenAuth(email='you@example.com')"

    for secret in ("s3cret-token", "s3cret-password"):
        assert secret not in repr(pa)
        assert secret not in repr(ba)
        assert secret not in repr(at)


def test_secrets_still_participate_in_equality() -> None:
    # Masking repr must not change hashequality semantics of the frozen
    # dataclasses: two credentials differ when their secrets differ.
    assert PATAuth("a") == PATAuth("a")
    assert PATAuth("a") != PATAuth("b")
    assert BasicAuth("u", "p1") != BasicAuth("u", "p2")
    assert APITokenAuth("a@b.com", "t1") != APITokenAuth("a@b.com", "t2")


def test_asdict_still_exposes_secrets() -> None:
    # Serialization paths that consciously want the value still get it.
    assert dataclasses.asdict(PATAuth("t")) == {"token": "t"}
    assert dataclasses.asdict(APITokenAuth("a@b.com", "t")) == {
        "email": "a@b.com",
        "token": "t",
    }

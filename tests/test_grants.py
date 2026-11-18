"""Unit tests for the download-grant token module (no network)."""

import base64
import hashlib
import hmac
import json
import time

import pytest

from relay.grants import (
    MAX_TTL_SECONDS,
    GrantReject,
    derive_grant_secret,
    issue_grant,
    verify_grant,
)

SECRET = "unit-test-secret"
NOW = 1_700_000_000


def issue(**overrides):
    params = dict(
        generation=3,
        method="GET",
        path="/artifact",
        query="bytes=10&chunk=5",
        ttl_seconds=60,
        now=NOW,
    )
    params.update(overrides)
    return issue_grant(SECRET, **params)


def check(token, **overrides):
    params = dict(
        generation=3, method="GET", path="/artifact", query="bytes=10&chunk=5", now=NOW + 1
    )
    params.update(overrides)
    return verify_grant(SECRET, token, **params)


def make_token(payload: dict, secret: str = SECRET) -> str:
    """Hand-roll a token, bypassing issue_grant's own validation."""
    body = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).rstrip(b"=").decode()
    signing_input = f"grv1.{body}"
    sig = base64.urlsafe_b64encode(
        hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    return f"{signing_input}.{sig}"


def test_roundtrip_returns_payload():
    token, payload = issue()
    assert token.startswith("grv1.")
    assert token.count(".") == 2
    out = check(token)
    assert out["gen"] == 3
    assert out["m"] == "GET"
    assert out["p"] == "/artifact"
    assert out["q"] == "bytes=10&chunk=5"
    assert out["exp"] - out["iat"] == 60
    assert payload["exp"] == NOW + 60


def test_head_grant_roundtrip():
    token, _ = issue(method="HEAD")
    out = check(token, method="HEAD")
    assert out["m"] == "HEAD"


def test_query_with_percent_escapes_and_special_chars():
    query = "name=a%20b.bin&x=%2F%26%3D&empty="
    token, _ = issue(path="/fixtures/a%20b.bin", query=query)
    out = check(token, path="/fixtures/a%20b.bin", query=query)
    assert out["q"] == query


def test_two_grants_have_unique_nonces():
    _, p1 = issue()
    _, p2 = issue()
    assert p1["n"] != p2["n"]


def test_tampered_signature_rejected():
    token, _ = issue()
    bad = token[:-1] + ("A" if token[-1] != "A" else "B")
    with pytest.raises(GrantReject) as excinfo:
        check(bad)
    assert excinfo.value.reason == "bad_signature"
    assert excinfo.value.status == 401


def test_tampered_payload_rejected():
    token, _ = issue()
    head, body, sig = token.split(".")
    flipped = ("A" if body[0] != "A" else "B") + body[1:]
    with pytest.raises(GrantReject) as excinfo:
        check(f"{head}.{flipped}.{sig}")
    assert excinfo.value.reason == "bad_signature"


def test_wrong_secret_rejected():
    token, _ = issue()
    with pytest.raises(GrantReject) as excinfo:
        verify_grant(
            "another-secret",
            token,
            generation=3,
            method="GET",
            path="/artifact",
            query="bytes=10&chunk=5",
            now=NOW + 1,
        )
    assert excinfo.value.reason == "bad_signature"


@pytest.mark.parametrize("token", ["", "abc", "grv1.onlyone", "grv2.a.b", "grv1.a.b.c"])
def test_malformed_tokens_rejected(token):
    with pytest.raises(GrantReject) as excinfo:
        check(token)
    assert excinfo.value.reason == "malformed"
    assert excinfo.value.status == 401


def test_non_json_payload_rejected():
    body = base64.urlsafe_b64encode(b"not json").rstrip(b"=").decode()
    signing_input = f"grv1.{body}"
    sig = base64.urlsafe_b64encode(
        hmac.new(SECRET.encode(), signing_input.encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    with pytest.raises(GrantReject) as excinfo:
        check(f"{signing_input}.{sig}")
    assert excinfo.value.reason == "malformed"


def test_expired_grant_rejected():
    token, _ = issue(now=NOW - 120, ttl_seconds=60)  # expired a minute ago
    with pytest.raises(GrantReject) as excinfo:
        check(token, now=NOW)
    assert excinfo.value.reason == "expired"
    assert excinfo.value.status == 401


def test_grant_valid_until_exp_then_not():
    token, _ = issue(ttl_seconds=10)
    check(token, now=NOW + 9)  # still valid
    with pytest.raises(GrantReject):
        check(token, now=NOW + 10)  # exp reached => expired


def test_issue_refuses_ttl_over_cap():
    with pytest.raises(ValueError):
        issue(ttl_seconds=MAX_TTL_SECONDS + 1)


def test_verify_rejects_overlong_lifetime_even_with_valid_signature():
    # A token minted outside issue_grant (e.g. with a leaked secret) must
    # still not exceed the 10 minute cap.
    token = make_token(
        {"v": 1, "gen": 3, "m": "GET", "p": "/artifact", "q": "bytes=10&chunk=5",
         "iat": NOW, "exp": NOW + MAX_TTL_SECONDS + 60, "n": "ab"}
    )
    with pytest.raises(GrantReject) as excinfo:
        check(token)
    assert excinfo.value.reason == "ttl_exceeded"


def test_generation_mismatch_is_403():
    token, _ = issue(generation=3)
    with pytest.raises(GrantReject) as excinfo:
        check(token, generation=4)
    assert excinfo.value.reason == "generation_mismatch"
    assert excinfo.value.status == 403


def test_method_mismatch_is_403():
    token, _ = issue(method="GET")
    with pytest.raises(GrantReject) as excinfo:
        check(token, method="HEAD")
    assert excinfo.value.reason == "method_mismatch"
    assert excinfo.value.status == 403


def test_path_mismatch_is_403():
    token, _ = issue()
    with pytest.raises(GrantReject) as excinfo:
        check(token, path="/other")
    assert excinfo.value.reason == "target_mismatch"
    assert excinfo.value.status == 403


def test_query_mismatch_is_403():
    token, _ = issue()
    with pytest.raises(GrantReject) as excinfo:
        check(token, query="bytes=10&chunk=6")
    assert excinfo.value.reason == "target_mismatch"


def test_empty_query_binds_exactly():
    token, _ = issue(query="")
    check(token, query="")
    with pytest.raises(GrantReject):
        check(token, query="bytes=10&chunk=5")


def test_issue_validates_inputs():
    with pytest.raises(ValueError):
        issue(method="POST")
    with pytest.raises(ValueError):
        issue(path="no-leading-slash")
    with pytest.raises(ValueError):
        issue(ttl_seconds=0)


def test_derived_secret_differs_from_admin_key_but_is_stable():
    first = derive_grant_secret("dev-admin-key-change-me")
    second = derive_grant_secret("dev-admin-key-change-me")
    assert first == second
    assert first != "dev-admin-key-change-me"
    assert derive_grant_secret("other-key") != first

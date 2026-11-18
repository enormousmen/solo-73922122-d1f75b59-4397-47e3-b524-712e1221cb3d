"""Download grant tokens for the relay's protection mode.

A grant is a compact HMAC-SHA256 signed bearer token::

    grv1.<base64url(payload-json)>.<base64url(signature)>

The payload binds the token to everything the proxy re-checks before
forwarding a request upstream:

* ``gen`` — the switch generation that was current when the grant was
  issued.  After a switch the old generation's grants are useless.
* ``m``   — exactly one method, GET or HEAD.
* ``p``/``q`` — the raw path and the raw query string of the request,
  compared byte for byte (percent-encoding is not normalised).
* ``iat``/``exp`` — issued-at / expiry.  A grant lives at most
  ``MAX_TTL_SECONDS`` (10 minutes); the cap is enforced both when the
  token is minted and again when it is verified.

Grants are checked by the relay only and are never forwarded upstream.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

VERSION = "grv1"
MAX_TTL_SECONDS = 600  # hard cap: a grant is valid for at most 10 minutes
METHODS = ("GET", "HEAD")


class GrantReject(Exception):
    """A presented grant failed verification.

    ``reason`` is a stable machine-readable code for the JSON error body,
    ``status`` the HTTP status the proxy answers with.
    """

    def __init__(self, reason: str, status: int, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.status = status
        self.detail = detail


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def derive_grant_secret(admin_key: str) -> str:
    """Grant-signing secret deterministically derived from the admin key.

    Used when ``RELAY_GRANT_SECRET`` is not set explicitly, so the signing
    secret is never the same string as the key operators hand out, while
    only one value has to be configured.
    """

    return hmac.new(
        admin_key.encode("utf-8"), b"artifact-relay/grant-secret/v1", hashlib.sha256
    ).hexdigest()


def _sign(secret: str, signing_input: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256)
    return _b64encode(digest.digest())


def issue_grant(
    secret: str,
    *,
    generation: int,
    method: str,
    path: str,
    query: str,
    ttl_seconds: int,
    now: float | None = None,
) -> tuple[str, dict[str, Any]]:
    """Mint a grant token; returns ``(token, payload)``."""
    if ttl_seconds <= 0 or ttl_seconds > MAX_TTL_SECONDS:
        raise ValueError(f"ttl_seconds must be within 1..{MAX_TTL_SECONDS}")
    method = method.upper()
    if method not in METHODS:
        raise ValueError(f"method must be one of {METHODS}")
    if not path.startswith("/"):
        raise ValueError("path must start with '/'")
    issued = int(now if now is not None else time.time())
    payload: dict[str, Any] = {
        "v": 1,
        "gen": generation,
        "m": method,
        "p": path,
        "q": query,
        "iat": issued,
        "exp": issued + ttl_seconds,
        "n": secrets.token_hex(8),
    }
    body = _b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signing_input = f"{VERSION}.{body}"
    return f"{signing_input}.{_sign(secret, signing_input)}", payload


def verify_grant(
    secret: str,
    token: str,
    *,
    generation: int,
    method: str,
    path: str,
    query: str,
    now: float | None = None,
) -> dict[str, Any]:
    """Check a presented grant against the live request; return its payload.

    Raises :class:`GrantReject` with a 401 reason for anything wrong with
    the token itself (missing structure, bad signature, expiry) and a 403
    reason when an otherwise valid token does not cover this request
    (generation, method, path/query).
    """
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != VERSION:
        raise GrantReject("malformed", 401, "not a grv1 grant token")
    signing_input = f"{parts[0]}.{parts[1]}"
    # The signature covers the exact payload string, so decoding quirks
    # below cannot be used to smuggle a different payload past the check.
    if not hmac.compare_digest(_sign(secret, signing_input), parts[2]):
        raise GrantReject("bad_signature", 401, "grant signature does not match")
    try:
        payload = json.loads(_b64decode(parts[1]))
    except (ValueError, UnicodeDecodeError) as exc:
        raise GrantReject("malformed", 401, "grant payload is not valid JSON") from exc
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise GrantReject("malformed", 401, "grant payload has an unknown shape")
    iat, exp = payload.get("iat"), payload.get("exp")
    if not isinstance(iat, int) or not isinstance(exp, int):
        raise GrantReject("malformed", 401, "grant payload lacks integer iat/exp")
    if exp - iat > MAX_TTL_SECONDS:
        raise GrantReject("ttl_exceeded", 401, "grant lifetime exceeds the 10 minute cap")
    moment = now if now is not None else time.time()
    if moment >= exp:
        raise GrantReject("expired", 401, "grant has expired")
    if payload.get("gen") != generation:
        raise GrantReject(
            "generation_mismatch",
            403,
            f"grant was issued for generation {payload.get('gen')}, current is {generation}",
        )
    if payload.get("m") != method:
        raise GrantReject(
            "method_mismatch", 403, f"grant covers {payload.get('m')}, not {method}"
        )
    if payload.get("p") != path or payload.get("q") != query:
        raise GrantReject(
            "target_mismatch", 403, "grant does not cover this exact path and query string"
        )
    return payload
